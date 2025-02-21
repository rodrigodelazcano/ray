"""Tools and utils to create RLlib-ready recommender system envs using RecSim.

For examples on how to generate a RecSim env class (usable in RLlib):
See ray.rllib.examples.env.recsim_recommender_system_envs.py

For more information on google's RecSim itself:
https://github.com/google-research/recsim
"""

from collections import OrderedDict
import gym
from gym.spaces import Dict, Discrete, MultiDiscrete
import numpy as np
from recsim.document import AbstractDocumentSampler
from recsim.simulator import environment, recsim_gym
from recsim.user import AbstractUserModel, AbstractResponse
from typing import Callable, List, Optional, Type

from ray.rllib.env.env_context import EnvContext
from ray.rllib.utils.annotations import override
from ray.rllib.utils.error import UnsupportedSpaceException
from ray.rllib.utils.spaces.space_utils import convert_element_to_space_type


class RecSimObservationSpaceWrapper(gym.ObservationWrapper):
    """Fix RecSim environment's observation space

    In RecSim's observation spaces, the "doc" field is a dictionary keyed by
    document IDs. Those IDs are changing every step, thus generating a
    different observation space in each time. This causes issues for RLlib
    because it expects the observation space to remain the same across steps.

    This environment wrapper fixes that by reindexing the documents by their
    positions in the list.
    """

    def __init__(self, env: gym.Env):
        super().__init__(env)
        obs_space = self.env.observation_space
        doc_space = Dict(
            OrderedDict(
                [
                    (str(k), doc)
                    for k, (_, doc) in enumerate(obs_space["doc"].spaces.items())
                ]
            )
        )
        self.observation_space = Dict(
            OrderedDict(
                [
                    ("user", obs_space["user"]),
                    ("doc", doc_space),
                    ("response", obs_space["response"]),
                ]
            )
        )
        self._sampled_obs = self.observation_space.sample()

    def observation(self, obs):
        new_obs = OrderedDict()
        new_obs["user"] = obs["user"]
        new_obs["doc"] = {str(k): v for k, (_, v) in enumerate(obs["doc"].items())}
        new_obs["response"] = obs["response"]
        new_obs = convert_element_to_space_type(new_obs, self._sampled_obs)
        return new_obs


class RecSimResetWrapper(gym.Wrapper):
    """Fix RecSim environment's reset() and close() function

    RecSim's reset() function returns an observation without the "response"
    field, breaking RLlib's check. This wrapper fixes that by assigning a
    random "response".

    RecSim's close() function raises NotImplementedError. We change the
    behavior to doing nothing.
    """

    def __init__(self, env: gym.Env):
        super().__init__(env)
        self._sampled_obs = self.env.observation_space.sample()

    def reset(self):
        obs = super().reset()
        obs["response"] = self.env.observation_space["response"].sample()
        obs = convert_element_to_space_type(obs, self._sampled_obs)
        return obs

    def close(self):
        pass


class MultiDiscreteToDiscreteActionWrapper(gym.ActionWrapper):
    """Convert the action space from MultiDiscrete to Discrete

    At this moment, RLlib's DQN algorithms only work on Discrete action space.
    This wrapper allows us to apply DQN algorithms to the RecSim environment.
    """

    def __init__(self, env: gym.Env):
        super().__init__(env)

        if not isinstance(env.action_space, MultiDiscrete):
            raise UnsupportedSpaceException(
                f"Action space {env.action_space} "
                f"is not supported by {self.__class__.__name__}"
            )
        self.action_space_dimensions = env.action_space.nvec
        self.action_space = Discrete(np.prod(self.action_space_dimensions))

    def action(self, action: int) -> List[int]:
        """Convert a Discrete action to a MultiDiscrete action"""
        multi_action = [None] * len(self.action_space_dimensions)
        for idx, n in enumerate(self.action_space_dimensions):
            action, dim_action = divmod(action, n)
            multi_action[idx] = dim_action
        return multi_action


def recsim_gym_wrapper(
    recsim_gym_env: gym.Env, convert_to_discrete_action_space: bool = False
) -> gym.Env:
    """Makes sure a RecSim gym.Env can ba handled by RLlib.

    In RecSim's observation spaces, the "doc" field is a dictionary keyed by
    document IDs. Those IDs are changing every step, thus generating a
    different observation space in each time. This causes issues for RLlib
    because it expects the observation space to remain the same across steps.

    Also, RecSim's reset() function returns an observation without the
    "response" field, breaking RLlib's check. This wrapper fixes that by
    assigning a random "response".

    Args:
        recsim_gym_env: The RecSim gym.Env instance. Usually resulting from a
            raw RecSim env having been passed through RecSim's utility function:
            `recsim.simulator.recsim_gym.RecSimGymEnv()`.
        convert_to_discrete_action_space: Optional bool indicating, whether
            the action space of the created env class should be Discrete
            (rather than MultiDiscrete, even if slate size > 1). This is useful
            for algorithms that don't support MultiDiscrete action spaces,
            such as RLlib's DQN. If None, `convert_to_discrete_action_space`
            may also be provided via the EnvContext (config) when creating an
            actual env instance.

    Returns:
        An RLlib-ready gym.Env instance.
    """
    env = RecSimResetWrapper(recsim_gym_env)
    env = RecSimObservationSpaceWrapper(env)
    if convert_to_discrete_action_space:
        env = MultiDiscreteToDiscreteActionWrapper(env)
    return env


def make_recsim_env(
    recsim_user_model_creator: Callable[[EnvContext], AbstractUserModel],
    recsim_document_sampler_creator: Callable[[EnvContext], AbstractDocumentSampler],
    reward_aggregator: Callable[[List[AbstractResponse]], float],
) -> Type[gym.Env]:
    """Creates a RLlib-ready gym.Env class given RecSim user and doc models.

    See https://github.com/google-research/recsim for more information on how to
    build the required components from scratch in python using RecSim.

    Args:
        recsim_user_model_creator: A callable taking an EnvContext and returning
            a RecSim AbstractUserModel instance to use.
        recsim_document_sampler_creator: A callable taking an EnvContext and
            returning a RecSim AbstractDocumentSampler
            to use. This will include a AbstractDocument as well.
        reward_aggregator: Callable taking a list of RecSim
            AbstractResponse instances and returning a float (aggregated
            reward).

    Returns:
        An RLlib-ready gym.Env class to use inside a Trainer.
    """

    class _RecSimEnv(gym.Env):
        def __init__(self, env_ctx: Optional[EnvContext] = None):
            # Override with default values, in case they are not set by the user.
            default_config = {
                "num_candidates": 10,
                "slate_size": 2,
                "resample_documents": True,
                "seed": 0,
                "convert_to_discrete_action_space": False,
            }
            if env_ctx is None or isinstance(env_ctx, dict):
                env_ctx = EnvContext(env_ctx or default_config, worker_index=0)
            env_ctx.set_defaults(default_config)

            # Create the RecSim user model instance.
            recsim_user_model = recsim_user_model_creator(env_ctx)
            # Create the RecSim document sampler instance.
            recsim_document_sampler = recsim_document_sampler_creator(env_ctx)

            # Create a raw RecSim environment (not yet a gym.Env!).
            raw_recsim_env = environment.SingleUserEnvironment(
                recsim_user_model,
                recsim_document_sampler,
                env_ctx["num_candidates"],
                env_ctx["slate_size"],
                resample_documents=env_ctx["resample_documents"],
            )
            # Convert raw RecSim env to a gym.Env.
            gym_env = recsim_gym.RecSimGymEnv(raw_recsim_env, reward_aggregator)

            # Fix observation space and - if necessary - convert to discrete
            # action space (from multi-discrete).
            self.env = recsim_gym_wrapper(
                gym_env, env_ctx["convert_to_discrete_action_space"]
            )
            self.observation_space = self.env.observation_space
            self.action_space = self.env.action_space

        @override(gym.Env)
        def reset(self):
            return self.env.reset()

        @override(gym.Env)
        def step(self, actions):
            return self.env.step(actions)

        @override(gym.Env)
        def seed(self, seed=None):
            return self.env.seed(seed)

        @override(gym.Env)
        def render(self, mode="human"):
            return self.env.render(mode)

    return _RecSimEnv
