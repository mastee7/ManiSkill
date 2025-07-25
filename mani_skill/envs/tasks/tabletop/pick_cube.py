from typing import Any, Dict, List, Union

import numpy as np
import sapien
import torch

import mani_skill.envs.utils.randomization as randomization
from mani_skill.agents.robots import SO100, Fetch, Panda, WidowXAI, XArm6Robotiq
from mani_skill.envs.sapien_env import BaseEnv
from mani_skill.envs.tasks.tabletop.pick_cube_cfgs import PICK_CUBE_CONFIGS
from mani_skill.sensors.camera import CameraConfig        # Build cubes separately for each parallel environment to enable domain randomization

from mani_skill.utils import sapien_utils
from mani_skill.utils.building import actors
from mani_skill.utils.registration import register_env
from mani_skill.utils.scene_builder.table import TableSceneBuilder
from mani_skill.utils.structs.pose import Pose
from mani_skill.utils.structs import Actor

from sapien.physx import PhysxRigidBodyComponent
from sapien.render import RenderBodyComponent

PICK_CUBE_DOC_STRING = """**Task Description:**
A simple task where the objective is to grasp a red cube with the {robot_id} robot and move it to a target goal position. This is also the *baseline* task to test whether a robot with manipulation
capabilities can be simulated and trained properly. Hence there is extra code for some robots to set them up properly in this environment as well as the table scene builder.

**Randomizations:**
- the cube's xy position is randomized on top of a table in the region [0.1, 0.1] x [-0.1, -0.1]. It is placed flat on the table
- the cube's z-axis rotation is randomized to a random angle
- the target goal position (marked by a green sphere) of the cube has its xy position randomized in the region [0.1, 0.1] x [-0.1, -0.1] and z randomized in [0, 0.3]

**Success Conditions:**
- the cube position is within `goal_thresh` (default 0.025m) euclidean distance of the goal position
- the robot is static (q velocity < 0.2)
"""


@register_env("PickCube-v1", max_episode_steps=50)
class PickCubeEnv(BaseEnv):

    _sample_video_link = "https://github.com/haosulab/ManiSkill/raw/main/figures/environment_demos/PickCube-v1_rt.mp4"
    SUPPORTED_ROBOTS = [
        "panda",
        "fetch",
        "xarm6_robotiq",
        "so100",
        "widowxai",
    ]
    agent: Union[Panda, Fetch, XArm6Robotiq, SO100, WidowXAI]
    cube_half_size = 0.02
    goal_thresh = 0.1
    cube_spawn_half_size = 0.05
    cube_spawn_center = (0, 0)

    def __init__(self, *args, robot_uids="panda", robot_init_qpos_noise=0.02, **kwargs):
        self.robot_init_qpos_noise = robot_init_qpos_noise
        if robot_uids in PICK_CUBE_CONFIGS:
            cfg = PICK_CUBE_CONFIGS[robot_uids]
        else:
            cfg = PICK_CUBE_CONFIGS["panda"]
        self.cube_half_size = cfg["cube_half_size"]
        self.goal_thresh = cfg["goal_thresh"]
        self.cube_spawn_half_size = cfg["cube_spawn_half_size"]
        self.cube_spawn_center = cfg["cube_spawn_center"]
        self.max_goal_height = cfg["max_goal_height"]
        self.sensor_cam_eye_pos = cfg["sensor_cam_eye_pos"]
        self.sensor_cam_target_pos = cfg["sensor_cam_target_pos"]
        self.human_cam_eye_pos = cfg["human_cam_eye_pos"]
        self.human_cam_target_pos = cfg["human_cam_target_pos"]
        super().__init__(*args, robot_uids=robot_uids, **kwargs)

    @property
    def _default_sensor_configs(self):
        pose = sapien_utils.look_at(
            eye=self.sensor_cam_eye_pos, target=self.sensor_cam_target_pos
        )
        return [CameraConfig("base_camera", pose, 128, 128, np.pi / 2, 0.01, 100)]

    @property
    def _default_human_render_camera_configs(self):
        pose = sapien_utils.look_at(
            eye=self.human_cam_eye_pos, target=self.human_cam_target_pos
        )
        return CameraConfig("render_camera", pose, 512, 512, 1, 0.01, 100)

    def _load_agent(self, options: dict):
        super()._load_agent(options, sapien.Pose(p=[-0.615, 0, 0]))

    def _load_scene(self, options: dict):
        self.table_scene = TableSceneBuilder(
            self, robot_init_qpos_noise=self.robot_init_qpos_noise, custom_table=True
        )
        self.table_scene.build()
        self.cube = actors.build_cube(
            self.scene,
            half_size=self.cube_half_size,
            color=[1, 0, 0, 1],
            name="cube",
            initial_pose=sapien.Pose(p=[0, 0, self.cube_half_size]),
        )
        self.goal_site = actors.build_sphere(
            self.scene,
            radius=self.goal_thresh,
            color=[0, 1, 0, 1],
            name="goal_site",
            body_type="kinematic",
            add_collision=False,
            initial_pose=sapien.Pose(),
        )
        self._hidden_objects.append(self.goal_site)

    def _initialize_episode(self, env_idx: torch.Tensor, options: dict):
        with torch.device(self.device):
            b = len(env_idx)
            self.table_scene.initialize(env_idx)
            xyz = torch.zeros((b, 3))
            xyz[:, :2] = (
                torch.rand((b, 2)) * self.cube_spawn_half_size * 2
                - self.cube_spawn_half_size
            )
            xyz[:, 0] += self.cube_spawn_center[0]
            xyz[:, 1] += self.cube_spawn_center[1]

            xyz[:, 2] = self.cube_half_size
            qs = randomization.random_quaternions(b, lock_x=True, lock_y=True, lock_z=True)
            self.cube.set_pose(Pose.create_from_pq(xyz, qs))

            goal_xyz = torch.zeros((b, 3))
            goal_xyz[:, :2] = (
                torch.rand((b, 2)) * self.cube_spawn_half_size * 2
                - self.cube_spawn_half_size
            )
            goal_xyz[:, 0] += self.cube_spawn_center[0]
            goal_xyz[:, 1] += self.cube_spawn_center[1]
            goal_xyz[:, 2] = torch.rand((b)) * self.max_goal_height + xyz[:, 2]
            self.goal_site.set_pose(Pose.create_from_pq(goal_xyz))

    def _get_obs_extra(self, info: Dict):
        # in reality some people hack is_grasped into observations by checking if the gripper can close fully or not
        obs = dict(
            is_grasped=info["is_grasped"],
            # tcp_pose=self.agent.tcp.pose.raw_pose,
            # goal_pos=self.goal_site.pose.p,
        )
        if "state" in self.obs_mode:
            obs.update(
                # obj_pose=self.cube.pose.raw_pose,
                tcp_to_obj_pos=self.cube.pose.p - self.agent.tcp_pose.p,
                obj_to_goal_pos=self.goal_site.pose.p - self.cube.pose.p,
            )
        return obs
    
    def _strip_finger_vel(self, qvel):
        if self.robot_uids in ["panda", "widowxai", "xarm6_robotiq"]:
            return qvel[..., :-2]       
        elif self.robot_uids == "so100":
            return qvel[..., :-1]       
        return qvel
    
    def _debug_static_check(self, tag=""):
        qvel_full = self.agent.robot.get_qvel()
        qvel_strip = self._strip_finger_vel(qvel_full)

        print(f"{tag}"
            f"  vel_full={torch.linalg.norm(qvel_full,  dim=1)[0]:.3f}"
            f"  vel_strp={torch.linalg.norm(qvel_strip, dim=1)[0]:.3f}"
            f"  obj_goal={torch.linalg.norm(self.goal_site.pose.p - self.cube.pose.p, dim=1)[0]:.3f}")


    def evaluate(self):
        obj_goal_dist = torch.linalg.norm(
            self.goal_site.pose.p - self.cube.pose.p, dim=1
        )                               # shape (B,)
        is_obj_placed = obj_goal_dist <= self.goal_thresh
        is_grasped = self.agent.is_grasping(self.cube)

        qvel = self._strip_finger_vel(self.agent.robot.get_qvel())
        static_thresh = 1.0 if self.robot_uids.startswith("xarm6") else 0.2
        vel_norm = torch.linalg.norm(qvel, dim=1)
        is_robot_static = torch.linalg.norm(qvel, dim=1) < static_thresh
        
        # DEBUG
        # self._debug_static_check(tag="eval")

        return {
            "success": is_obj_placed & is_robot_static,
            "is_obj_placed": is_obj_placed,
            "is_robot_static": is_robot_static,
            "is_grasped": is_grasped,
            "obj_goal_dist":   obj_goal_dist,
            "vel_norm":        vel_norm,
        }

    def staged_rewards(self, obs: Any, action: torch.Tensor, info: Dict):
        tcp_to_obj_dist = torch.linalg.norm(
            self.cube.pose.p - self.agent.tcp.pose.p, axis=1
        )
        reaching = 1 - torch.tanh(5 * tcp_to_obj_dist)

        is_grasped = info["is_grasped"]

        obj_to_goal_dist = torch.linalg.norm(
            self.goal_site.pose.p - self.cube.pose.p, axis=1
        )
        placing = (1 - torch.tanh(5 * obj_to_goal_dist)) * is_grasped

        qvel = self._strip_finger_vel(self.agent.robot.get_qvel())
        static = 1 - torch.tanh(5 * torch.linalg.norm(qvel, axis=1))
        static *= info["is_obj_placed"]

        return reaching.mean(), is_grasped.mean(), placing.mean(), static.mean()

    def compute_dense_reward(self, obs: Any, action: torch.Tensor, info: Dict):
        tcp_to_obj_dist = torch.linalg.norm(
            self.cube.pose.p - self.agent.tcp_pose.p, axis=1
        )
        reaching_reward = 1 - torch.tanh(5 * tcp_to_obj_dist)
        reward = reaching_reward

        is_grasped = info["is_grasped"]
        reward += is_grasped

        obj_to_goal_dist = torch.linalg.norm(
            self.goal_site.pose.p - self.cube.pose.p, axis=1
        )
        place_reward = 1 - torch.tanh(5 * obj_to_goal_dist)
        reward += place_reward * is_grasped

        qvel = self.agent.robot.get_qvel()
        if self.robot_uids in ["panda", "widowxai", "xarm6_robotiq"]:
            qvel = qvel[..., :-2]
        elif self.robot_uids == "so100":
            qvel = qvel[..., :-1]
        static_reward = 1 - torch.tanh(5 * torch.linalg.norm(qvel, axis=1))
        reward += static_reward * info["is_obj_placed"]

        reward[info["success"]] = 5
        return reward

    def compute_normalized_dense_reward(
        self, obs: Any, action: torch.Tensor, info: Dict
    ):
        return self.compute_dense_reward(obs=obs, action=action, info=info) / 5


PickCubeEnv.__doc__ = PICK_CUBE_DOC_STRING.format(robot_id="Panda")


@register_env("PickCubeSO100-v1", max_episode_steps=50)
class PickCubeSO100Env(PickCubeEnv):
    _sample_video_link = "https://github.com/haosulab/ManiSkill/raw/main/figures/environment_demos/PickCubeSO100-v1_rt.mp4"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, robot_uids="so100", **kwargs)


PickCubeSO100Env.__doc__ = PICK_CUBE_DOC_STRING.format(robot_id="SO100")


@register_env("PickCubeWidowXAI-v1", max_episode_steps=50)
class PickCubeWidowXAIEnv(PickCubeEnv):
    _sample_video_link = "https://github.com/haosulab/ManiSkill/raw/main/figures/environment_demos/PickCubeWidowXAI-v1_rt.mp4"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, robot_uids="widowxai", **kwargs)


PickCubeWidowXAIEnv.__doc__ = PICK_CUBE_DOC_STRING.format(robot_id="WidowXAI")


@register_env("PickCubeDR-v1", max_episode_steps=50)
class PickCubeDR(PickCubeEnv):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def _load_scene(self, options: dict):
        '''
            Custom load_scene where every parallel environment has a different color for the cube.
        '''
        self.table_scene = TableSceneBuilder(
            self, robot_init_qpos_noise=self.robot_init_qpos_noise, custom_table=True
        )
        self.table_scene.build()
        self.goal_site = actors.build_sphere(
            self.scene,
            radius=self.goal_thresh,
            color=[0, 1, 0, 1],
            name="goal_site",
            body_type="kinematic",
            add_collision=False,
            initial_pose=sapien.Pose(),
        )
        self._hidden_objects.append(self.goal_site)

        # Build cubes separately for each parallel environment to enable domain randomization        
        self._cubes: List[Actor] = []
        for i in range(self.num_envs):
            builder = self.scene.create_actor_builder()
            builder.add_box_collision(half_size=[self.cube_half_size] * 3)
            builder.add_box_visual(
                half_size=[self.cube_half_size] * 3, 
                material=sapien.render.RenderMaterial(
                    base_color=self._batched_episode_rng[i].uniform(low=0., high=1., size=(3, )).tolist() + [1]
                )
            )
            builder.initial_pose = sapien.Pose(p=[0, 0, self.cube_half_size])
            builder.set_scene_idxs([i])
            self._cubes.append(builder.build(name=f"cube_{i}"))
            self.remove_from_state_dict_registry(self._cubes[-1])  # remove individual cube from state dict

        # Merge all cubes into a single Actor object
        self.cube = Actor.merge(self._cubes, name="cube")
        print(f"number of cubes: {len(self._cubes)}")
        self.add_to_state_dict_registry(self.cube)  # add merged cube to state dict


PickCubeDR.__doc__ = PICK_CUBE_DOC_STRING.format(robot_id="Panda")