import unittest
import subprocess
import psutil
import sys
import os
import numpy as np
import time
import socket

# ROS
import rospy
import actionlib
import sensor_msgs.msg
import geometry_msgs.msg
import trajectory_msgs.msg
import rosgraph
import std_srvs.srv
import tf2_ros
import tf


# spartan
from spartan.utils.ros_utils import SimpleSubscriber, JointStateSubscriber
from spartan.utils.ros_utils import RobotService
import spartan.utils.ros_utils as ros_utils
import spartan.utils.utils as spartan_utils
import spartan.utils.transformations as transformations
import robot_control.control_utils as control_utils
from spartan.manipulation.schunk_driver import SchunkDriver
# spartan ROS
import robot_msgs.msg
import geometry_msgs.msg
import sensor_msgs.msg
import tf2_msgs.msg

import math


from teleop_mouse_manager import TeleopMouseManager


def make_cartesian_gains_msg(kp_rot, kp_trans):
    msg = robot_msgs.msg.CartesianGain()

    msg.rotation.x = kp_rot
    msg.rotation.y = kp_rot
    msg.rotation.z = kp_rot

    msg.translation.x = kp_trans
    msg.translation.y = kp_trans
    msg.translation.z = kp_trans

    return msg

def make_force_guard_msg(scale):
    msg = robot_msgs.msg.ForceGuard()
    external_force = robot_msgs.msg.ExternalForceGuard()

    body_frame = "iiwa_link_ee"
    expressed_in_frame = "iiwa_link_ee"
    force_vec = scale*np.array([-1,0,0])

    external_force.force.header.frame_id = expressed_in_frame
    external_force.body_frame = body_frame
    external_force.force.vector.x = force_vec[0]
    external_force.force.vector.y = force_vec[1]
    external_force.force.vector.z = force_vec[2]

    msg.external_force_guards.append(external_force)

    return msg

def ro(quat):
    return [quat[1], quat[2], quat[3], quat[0]]

def tf_matrix_from_pose(pose):
    trans, quat = pose
    mat = transformations.quaternion_matrix(quat)
    mat[:3, 3] = trans
    return mat

def do_main():
    rospy.init_node('simple_teleop', anonymous=True)

    # setup listener for tf2s (used for ee and controller poses)
    tfBuffer = tf2_ros.Buffer()
    listener = tf2_ros.TransformListener(tfBuffer)
    robotSubscriber = JointStateSubscriber("/joint_states")
    
    # wait until robot state found
    robotSubscriber = ros_utils.JointStateSubscriber("/joint_states")
    print("Waiting for full kuka state...")
    while len(robotSubscriber.joint_positions.keys()) < 3:
        rospy.sleep(0.1)
    print("got full state")

    # init gripper
    handDriver = SchunkDriver()
    
    # init mouse manager
    mouse_manager = TeleopMouseManager()
    roll_goal = 0.0
    yaw_goal = 0.0
    pitch_goal = 0.0

    # Start by moving to an above-table pregrasp pose that we know
    # EE control will work well from (i.e. far from singularity)

    stored_poses_dict = spartan_utils.getDictFromYamlFilename("../station_config/RLG_iiwa_1/stored_poses.yaml")
    above_table_pre_grasp = stored_poses_dict["Grasping"]["above_table_pre_grasp"]
    
    robotService = ros_utils.RobotService.makeKukaRobotService()
    success = robotService.moveToJointPosition(above_table_pre_grasp, timeout=5)
    print("Moved to position")

    gripper_goal_pos = 0.0
    handDriver.sendGripperCommand(gripper_goal_pos, speed=0.01, timeout=0.01)
    print("sent close goal to gripper")
    time.sleep(1)
    gripper_goal_pos = 0.1
    handDriver.sendGripperCommand(gripper_goal_pos, speed=0.01, timeout=0.01)
    print("sent open goal to gripper")

    frame_name = "iiwa_link_ee" # end effector frame name

    for i in range(10):
        if i == 9:
            print "Couldn't find robot pose"
            sys.exit(0)
        try:
            ee_pose_above_table = ros_utils.poseFromROSTransformMsg(
                tfBuffer.lookup_transform("base", frame_name, rospy.Time()).transform)
            ee_tf_above_table = tf_matrix_from_pose(ee_pose_above_table)
            break
        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
            print("Troubling looking up robot pose...")
            time.sleep(0.1)

    # Then kick off task space streaming
    sp = rospy.ServiceProxy('plan_runner/init_task_space_streaming',
        robot_msgs.srv.StartStreamingPlan)
    init = robot_msgs.srv.StartStreamingPlanRequest()
    init.force_guard.append(make_force_guard_msg(20.))
    res = sp(init)
    
    # save plan number so we know when plan has ended
    plan_number = res.plan_number

    # set up client that checks plan number periodically
    client = actionlib.SimpleActionClient("plan_runner/GetPlanNumber", robot_msgs.msg.GetPlanNumberAction)
    client.wait_for_server()


    print("Started task space streaming")
    pub = rospy.Publisher('plan_runner/task_space_streaming_setpoint',
        robot_msgs.msg.CartesianGoalPoint, queue_size=1);


    def cleanup():
        rospy.wait_for_service("plan_runner/stop_plan")
        sp = rospy.ServiceProxy('plan_runner/stop_plan',
            std_srvs.srv.Trigger)
        init = std_srvs.srv.TriggerRequest()
        print sp(init)
        print("Done cleaning up and stopping streaming plan")


    rospy.on_shutdown(cleanup)
    br = tf.TransformBroadcaster()
    rate = rospy.Rate(100) # max rate at which control should happen

    try:

        # control loop
        while not rospy.is_shutdown():

            # Check if plan ended
            goal = robot_msgs.msg.GetPlanNumberGoal()
            client.send_goal(goal)   
            client.wait_for_result()
            result = client.get_result()

            if (result.plan_number != plan_number):
                print "Illegal move, restarting plan"
                sp = rospy.ServiceProxy('plan_runner/init_task_space_streaming',
                    robot_msgs.srv.StartStreamingPlan)
                init = robot_msgs.srv.StartStreamingPlanRequest()
                init.force_guard.append(make_force_guard_msg(20.))
                res = sp(init)
                
                plan_number = res.plan_number                


            # get current tf from ros world to ee
            try:
                ee_pose_current = ros_utils.poseFromROSTransformMsg(
                    tfBuffer.lookup_transform("base", frame_name, rospy.Time()).transform)
                ee_tf_current = tf_matrix_from_pose(ee_pose_current)
            except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
                print("Troubling looking up robot pose...")
                rate.sleep()
                continue

            # get teleop mouse
            mouse_events = mouse_manager.get_mouse_events()

            if mouse_events["r"]:
                success = robotService.moveToJointPosition(above_table_pre_grasp, timeout=5)
                continue

            
            scale_down = 0.01
            delta_x = mouse_events["delta_x"]*scale_down
            delta_y = mouse_events["delta_y"]*-scale_down

            delta_forward = 0.0
            forward_scale = 0.03
            if mouse_events["w"]:
                delta_forward -= forward_scale
            if mouse_events["s"]:
                delta_forward += forward_scale

            # extract and normalize quat from tf
            if mouse_events["rotate_left"]:
                roll_goal += 0.01
            if mouse_events["rotate_right"]:
                roll_goal -= 0.01
            roll_goal = np.clip(roll_goal, a_min = -0.9, a_max = 0.9)
            
            if mouse_events["side_button_back"]:
                yaw_goal += 0.02
                print("side button back")
            if mouse_events["side_button_forward"]:
                yaw_goal -= 0.02
                print("side side_button_forward")
            yaw_goal = np.clip(yaw_goal, a_min = -1.314, a_max = 1.314)

            if mouse_events["d"]:
                pitch_goal += 0.02
            if mouse_events["a"]:
                pitch_goal -= 0.02
            pitch_goal = np.clip(pitch_goal, a_min = -1.314, a_max = 1.314)


            R = transformations.euler_matrix(pitch_goal, roll_goal, yaw_goal, 'syxz')
            # third is "yaw", when in above table pre-grasp
            # second is "roll", ''
            # first must be "pitch"

            above_table_quat_ee = transformations.quaternion_from_matrix(R.dot(ee_tf_above_table))
            above_table_quat_ee = np.array(above_table_quat_ee) / np.linalg.norm(above_table_quat_ee)

            # calculate controller position delta and add to start position to get target ee position
            target_translation = np.asarray([delta_forward, delta_x, delta_y])
            target_trans_ee = ee_tf_current[:3, 3] + target_translation

            # publish target pose as cartesian goal point
            new_msg = robot_msgs.msg.CartesianGoalPoint()
            new_msg.xyz_point.header.frame_id = "world"
            new_msg.xyz_point.point.x = target_trans_ee[0]
            new_msg.xyz_point.point.y = target_trans_ee[1]
            new_msg.xyz_point.point.z = target_trans_ee[2]
            new_msg.xyz_d_point.x = 0.0
            new_msg.xyz_d_point.y = 0.0
            new_msg.xyz_d_point.z = 0.0
            new_msg.quaternion.w = above_table_quat_ee[0]
            new_msg.quaternion.x = above_table_quat_ee[1]
            new_msg.quaternion.y = above_table_quat_ee[2]
            new_msg.quaternion.z = above_table_quat_ee[3]
            new_msg.gain = make_cartesian_gains_msg(5., 10.)
            new_msg.ee_frame_id = frame_name
            pub.publish(new_msg)

            # gripper
            if mouse_events["mouse_wheel_up"]:
                gripper_goal_pos += 0.01
            if mouse_events["mouse_wheel_down"]:
                gripper_goal_pos -= 0.01
            if gripper_goal_pos < 0:
                gripper_goal_pos = 0.0
            if gripper_goal_pos > 0.1:
                gripper_goal_pos = 0.1
            handDriver.sendGripperCommand(gripper_goal_pos, speed=0.1, timeout=0.01)

            rate.sleep()



    except Exception as e:
        print "Suffered exception ", e

if __name__ == "__main__":
    do_main()