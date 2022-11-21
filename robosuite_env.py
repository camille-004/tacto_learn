from collections import deque, OrderedDict
from typing import Any, NamedTuple

import numpy as np

import dm_env
from dm_env import StepType, specs, TimeStep
import robosuite as suite


class ExtendedTimeStep(NamedTuple):
    """
    ExtendedTimeStep object includes (a_t, r_t, gamma_t, o_{t+1})
    """
    step_type: Any
    reward: Any
    discount: Any
    observation: Any
    action: Any

    def first(self):
        return self.step_type == StepType.FIRST

    def mid(self):
        return self.step_type == StepType.MID

    def last(self):
        return self.step_type == StepType.LAST

    def __getitem__(self, attr):
        if isinstance(attr, str):
            return getattr(self, attr)
        else:
            return tuple.__getitem__(self, attr)

class DMEWrapper(dm_env.Environment):
    """
    Create an dm_env interface
    observation/observation_spec is always a dictionary even if there is only one mode
    """
    def __init__(self, env, keys=None):
        self._env = env
        
        if keys is None:
            keys = []
            # Add object obs if requested
            if self._env.use_object_obs:
                keys += ["object-state"]
            # Add image obs if requested
            if self._env.use_camera_obs:
                keys += [f"{cam_name}_image" for cam_name in self._env.camera_names]
            # Iterate over all robots to add to state
            for idx in range(len(self._env.robots)):
                keys += ["robot{}_proprio-state".format(idx)]
        self.keys = keys

        # robosuite returns an observation as observation specification
        ob = self._env.observation_spec()
        self._observation_spec = OrderedDict()
        for k in self.keys:
            assert k in ob.keys(), f"Observation key {k} not in robosuite observation"
            self._observation_spec[k] = specs.Array(ob[k].shape, ob[k].dtype, name=k)

        self._action_spec = specs.BoundedArray(
            (self._env.action_dim,), 
            np.float32, 
            minimum=self._env.action_spec[0],
            maximum=self._env.action_spec[1],
            name='action'
        )

    def _extract_obs(self, obs_dict):
        """
        Filter keys of interest out in observation
        """
        new_obs_dict = OrderedDict()
        for key in self.keys:
            new_obs_dict[key] = obs_dict[key]
        return new_obs_dict


    def observation_spec(self):
        return self._observation_spec

    def action_spec(self):
        return self._action_spec

    def reset(self, sim_reset=True):
        if sim_reset:
            observation = self._env.reset()
        else:
            observation = self._env._get_observations(force_update=True)
        observation = self._extract_obs(observation)
        return TimeStep(
            step_type=StepType.FIRST,
            reward=None,
            discount=None,
            observation=observation,
        )

    def step(self, action):
        observation, reward, done, info = self._env.step(action)
        observation = self._extract_obs(observation)
        step_type = StepType.LAST if done else StepType.MID
        return TimeStep(
            step_type=step_type, 
            reward=reward, 
            discount=1.0,             # discount not an attribute of robosuite MujocoEnv
            observation=observation,
        )

    def _get_observations(self, force_update=False):
        """
        Grabs observations from the environment.
        Args:
            force_update (bool): If True, will force all the observables to update their internal values to the newest
                value. This is useful if, e.g., you want to grab observations when directly setting simulation states
                without actually stepping the simulation.
        Returns:
            OrderedDict: OrderedDict containing observations [(name_string, np.array), ...]
        """
        observation = self._env._get_observations(force_update=force_update)
        observation = self._extract_obs(observation)
        return observation


    def __getattr__(self, name):
        return getattr(self._env, name)


class FrameStackWrapper(dm_env.Environment):
    """
    Frame stack and make channel first for image observations
    Vector observations will be cast to float32
    Assumes only one image observation mode
    """
    def __init__(self, env, num_frames):
        self._env = env
        self._num_frames = num_frames
        self._frames = deque([], maxlen=num_frames)

        wrapped_obs_spec = env.observation_spec()
        assert isinstance(wrapped_obs_spec, dict), "Env not providing a dictionary observation"

        self._keys = list(wrapped_obs_spec.keys())
        self._obs_spec = OrderedDict()
        self._image_key = None
        for k, spec in wrapped_obs_spec.items():
            if len(spec.shape) > 1:
                self._image_key = k
                image_shape = spec.shape

                # remove batch dim
                if len(image_shape) == 4:
                    image_shape = image_shape[1:]
    
                self._image_spec = specs.BoundedArray(
                    shape=np.concatenate(
                        [[image_shape[2] * num_frames], image_shape[:2]], 
                        axis=0
                    ),
                    dtype=np.uint8,
                    minimum=0,
                    maximum=255,
                    name=k
                )
                self._obs_spec[k] = self._image_spec
            else:
                self._obs_spec[k] = spec.replace(dtype=np.float32)


    def _transform_observation(self, time_step):
        """
        Stack frames for image and cast type for vector
        """
        ob = time_step.observation
        for k, v in ob.items():
            if k == self._image_key:
                assert len(self._frames) == self._num_frames
                vis_obs = np.concatenate(list(self._frames), axis=0)
                ob[k] = vis_obs
            else:
                ob[k] = v.astype(np.float32)
        return time_step._replace(observation=ob)

    def _extract_image(self, time_step):
        if self._image_key is None:
            return None
        else:
            image = time_step.observation[self._image_key]
            # remove batch dim
            if len(image.shape) == 4:
                image = image[0]
            return image.transpose(2, 0, 1).copy()

    def reset(self, **kwargs):
        time_step = self._env.reset(**kwargs)
        image = self._extract_image(time_step)
        for _ in range(self._num_frames):
            self._frames.append(image)
        return self._transform_observation(time_step)

    def step(self, action):
        time_step = self._env.step(action)
        image = self._extract_image(time_step)
        self._frames.append(image)
        return self._transform_observation(time_step)

    def observation_spec(self):
        return self._obs_spec

    def action_spec(self):
        return self._env.action_spec()


    def __getattr__(self, name):
        return getattr(self._env, name)


class ExtendedTimeStepWrapper(dm_env.Environment):
    """
    ExtendedTimeStep object includes (a_t, r_t, gamma_t, o_{t+1})
    """
    def __init__(self, env):
        self._env = env

    def reset(self, **kwargs):
        time_step = self._env.reset(**kwargs)
        return self._augment_time_step(time_step)

    def step(self, action):
        time_step = self._env.step(action)
        return self._augment_time_step(time_step, action)

    def _augment_time_step(self, time_step, action=None):
        if action is None:
            action_spec = self.action_spec()
            action = np.zeros(action_spec.shape, dtype=action_spec.dtype)
        return ExtendedTimeStep(observation=time_step.observation,
                                step_type=time_step.step_type,
                                action=action,
                                reward=time_step.reward or 0.0,
                                discount=time_step.discount or 1.0)

    def observation_spec(self):
        return self._env.observation_spec()

    def action_spec(self):
        return self._env.action_spec()

    def __getattr__(self, name):
        return getattr(self._env, name)


class ActionRepeatWrapper(dm_env.Environment):
    def __init__(self, env, num_repeats):
        self._env = env
        self._num_repeats = num_repeats

    def step(self, action):
        reward = 0.0
        discount = 1.0
        for i in range(self._num_repeats):
            time_step = self._env.step(action)
            reward += (time_step.reward or 0.0) * discount
            discount *= time_step.discount
            if time_step.last():
                break

        return time_step._replace(reward=reward, discount=discount)

    def observation_spec(self):
        return self._env.observation_spec()

    def action_spec(self):
        return self._env.action_spec()

    def reset(self):
        return self._env.reset()

    def __getattr__(self, name):
        return getattr(self._env, name)


class ActionDTypeWrapper(dm_env.Environment):
    def __init__(self, env, dtype):
        self._env = env
        wrapped_action_spec = env.action_spec()
        self._action_spec = specs.BoundedArray(wrapped_action_spec.shape,
                                               dtype,
                                               wrapped_action_spec.minimum,
                                               wrapped_action_spec.maximum,
                                               'action')

    def step(self, action):
        action = action.astype(self._env.action_spec().dtype)
        return self._env.step(action)

    def observation_spec(self):
        return self._env.observation_spec()

    def action_spec(self):
        return self._action_spec

    def reset(self):
        return self._env.reset()

    def __getattr__(self, name):
        return getattr(self._env, name)


class ObsDTypeWrapper(dm_env.Environment):
    def __init__(self, env, dtype):
        self._env = env
        wrapped_obs_spec = env.observation_spec()

        self._obs_spec = OrderedDict()
        for k, v in wrapped_obs_spec.items():
            self._obs_spec[k] = specs.Array(v.shape, np.float32, name=k)

    def _transform_observation(self, time_step):
        """
        Stack frames for image and cast type for vector
        """
        ob = time_step.observation
        for k, v in ob.items():
            ob[k] = v.astype(np.float32)
        return time_step._replace(observation=ob)

    def reset(self):
        time_step = self._env.reset()
        return self._transform_observation(time_step)

    def step(self, action):
        time_step = self._env.step(action)
        return self._transform_observation(time_step)
            
    def observation_spec(self):
        return self._obs_spec

    def action_spec(self):
        return self._env.action_spec()

    def __getattr__(self, name):
        return getattr(self._env, name)

def make(env_cfg, frame_stack):

    # if env_cfg.name in ['cheetah_run']:
    #     from dm_control import suite
    #     from dm_control.suite.wrappers import action_scale
    #     domain, task = env_cfg.name.split('_', 1)
    #     env = suite.load(
    #         domain,
    #         task,
    #         task_kwargs={'random': env_cfg.seed},
    #         visualize_reward=False
    #     )
    #     env = ActionDTypeWrapper(env, np.float32)
    #     env = ObsDTypeWrapper(env, np.float32)
    #     env = ActionRepeatWrapper(env, env_cfg.action_repeat)
    #     env = action_scale.Wrapper(env, minimum=-1.0, maximum=+1.0)
    #     env = ExtendedTimeStepWrapper(env)
    #     return env


    # Always use OSC controller
    controller_configs = suite.load_controller_config(default_controller="OSC_POSE")

    env = suite.make(
        **env_cfg,
        controller_configs=controller_configs
    )
    
    # Always include robot proprioceptive states
    obs_keys = ['robot0_proprio-state']
    if env_cfg.use_camera_obs:
        obs_keys.append('agentview_image')
    if env_cfg.use_object_obs:
        obs_keys.append('object-state')
    if env_cfg.use_touch_obs:
        obs_keys.append('robot0_touch-state')
    # TODO: Add other observation keys, e.g. tactile/touch

    # add wrappers
    env = DMEWrapper(env, obs_keys)
    env = FrameStackWrapper(env, frame_stack)
    env = ExtendedTimeStepWrapper(env)
    return env
