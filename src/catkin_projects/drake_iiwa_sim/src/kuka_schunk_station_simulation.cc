#include <limits>

#include <gflags/gflags.h>
#include "common_utils/system_utils.h"

#include "drake_iiwa_sim/kuka_schunk_station.h"
#include "drake_iiwa_sim/schunk_wsg_ros_actionserver.h"
#include "drake_iiwa_sim/ros_scene_graph_visualizer.h"

#include "drake/common/eigen_types.h"
#include "drake/common/find_resource.h"
#include "drake/common/is_approx_equal_abstol.h"
#include "drake/examples/kuka_iiwa_arm/iiwa_lcm.h"
#include "drake/geometry/geometry_visualization.h"
#include "drake/lcm/drake_lcm.h"
#include "drake/lcmt_iiwa_command.hpp"
#include "drake/lcmt_iiwa_status.hpp"
#include "drake/lcmt_schunk_wsg_command.hpp"
#include "drake/lcmt_schunk_wsg_status.hpp"
#include "drake/manipulation/schunk_wsg/schunk_wsg_lcm.h"
#include "drake/multibody/multibody_tree/parsing/multibody_plant_sdf_parser.h"
#include "drake/multibody/multibody_tree/parsing/multibody_plant_urdf_parser.h"
#include "drake/systems/analysis/simulator.h"
#include "drake/systems/framework/diagram.h"
#include "drake/systems/framework/diagram_builder.h"
#include "drake/systems/lcm/lcm_publisher_system.h"
#include "drake/systems/lcm/lcm_subscriber_system.h"
#include "drake/systems/primitives/demultiplexer.h"
#include "drake/systems/primitives/matrix_gain.h"
#include "drake/systems/sensors/dev/rgbd_camera.h"

#include <ros/ros.h>

namespace drake_iiwa_sim {
namespace {

// Runs a simulation of the kuka schunk station, spoofing
// the setup used in Spartan by:
// Exposing the LCM command ports of the robot
// Exposing the ROS control ports of the Schunk gripper
// TODO: Spoofing camera info + images on appropriate camera channels

using namespace drake;

using Eigen::VectorXd;
using math::RigidTransform;
using math::RollPitchYaw;
using multibody::parsing::AddModelFromSdfFile;
using multibody::parsing::AddModelFromUrdfFile;

DEFINE_double(target_realtime_rate, 1.0,
              "Playback speed.  See documentation for "
              "Simulator::set_target_realtime_rate() for details.");
DEFINE_double(duration, std::numeric_limits<double>::infinity(),
              "Simulation duration.");
DEFINE_string(config, "", "Sim config filename (required).");

int do_main(int argc, char* argv[]) {
  ros::init(argc, argv, "kuka_schunk_station_simulation");

  gflags::ParseCommandLineFlags(&argc, &argv, true);

  YAML::Node station_config = YAML::LoadFile(FLAGS_config);
  

  systems::DiagramBuilder<double> builder;

  // Create the Kuka + Schunk.
  auto station = builder.AddSystem<KukaSchunkStation>(
    station_config, 0.002, IiwaCollisionModel::kPolytopeCollision);

  // Add a work table in front of the robot, and to its side.
  const double dz_table_top_robot_base = 0.736 + 0.057 / 2.;
  const std::string table_sdf_path = FindResourceOrThrow(
      "drake/examples/kuka_iiwa_arm/models/table/extra_heavy_duty_table_surface_only_collision.sdf");
  auto plant = &station->get_mutable_multibody_plant();
  auto scene_graph = &station->get_mutable_scene_graph();
  const auto table_front =
      AddModelFromSdfFile(table_sdf_path, "table_front", plant);
  plant->WeldFrames(
      plant->world_frame(), plant->GetFrameByName("link", table_front),
      RigidTransform<double>(
          Eigen::Vector3d(0.75, 0, -dz_table_top_robot_base))
          .GetAsIsometry3());
  const auto table_left =
      AddModelFromSdfFile(table_sdf_path, "table_left", plant);
  plant->WeldFrames(
      plant->world_frame(), plant->GetFrameByName("link", table_left),
      RigidTransform<double>(
          Eigen::Vector3d(0.0, 0.8, -dz_table_top_robot_base))
          .GetAsIsometry3());

  // TODO(gizatt) Merge into Schunk station, or its own class?
  {
    for (const auto& node : station_config["instances"]) {
	const auto pose = node["q0"].as<std::vector<double>>();
	Eigen::Vector3d xyz(pose[0], pose[1], pose[2]);
	Eigen::Vector3d rpy(pose[3], pose[4], pose[5]);
	RigidTransform<double> object_tf(RollPitchYaw<double>(rpy), xyz);

	const auto object_class = node["model"].as<std::string>();
        auto object_class_node = station_config["models"][object_class];
        DRAKE_DEMAND(object_class_node);
        std::string full_path = expandEnvironmentVariables(object_class_node.as<std::string>());
        // TODO: replace with unique name
	std::string model_name = node["model"].as<std::string>();
        // TODO: handle URDFs too
        const auto object = AddModelFromUrdfFile(full_path, model_name, plant);
        
	if (node["fixed"].as<bool>()) {
	 
	     // Cludgy, but default behavior in AddModelFromSdfFile is to
             // make a frame at the root of the added model with the same name
             // as the added model.
	     plant->WeldFrames(plant->world_frame(), plant->GetFrameByName(model_name),
			       object_tf.GetAsIsometry3());
	}
    }
  }

  auto object = multibody::parsing::AddModelFromSdfFile(
      FindResourceOrThrow(
          "drake/examples/manipulation_station/models/061_foam_brick.sdf"),
      "brick", plant, scene_graph);

  station->Finalize();

  // TODO(gizatt) Merge this into the Schunk Station, or its own
  // class?
  {
    if (station_config["cameras"]) {
      for (const auto camera_config : station_config["cameras"]) {
        DRAKE_DEMAND(camera_config["name"]);
        DRAKE_DEMAND(camera_config["channel"]);
        DRAKE_DEMAND(camera_config["config_base_dir"]);
        YAML::Node camera_extrinsics_yaml =
            YAML::LoadFile(expandEnvironmentVariables(
		camera_config["config_base_dir"].as<std::string>() +
                           "/camera_info.yaml"));
        YAML::Node rgb_camera_info_yaml =
            YAML::LoadFile(expandEnvironmentVariables(
		camera_config["config_base_dir"].as<std::string>() +
                           "/rgb_camera_info.yaml"));
        YAML::Node depth_camera_info_yaml =
            YAML::LoadFile(expandEnvironmentVariables(
		camera_config["config_base_dir"].as<std::string>() +
                           "/depth_camera_info.yaml"));

        auto camera_name = camera_config["name"].as<std::string>();
        drake::geometry::dev::render::DepthCameraProperties camera_properties(
            640, 480, M_PI_4, geometry::dev::render::Fidelity::kLow, 0.1, 2.0);
	const auto body_node_index = plant->GetBodyByName(
		camera_extrinsics_yaml["depth"]["extrinsics"]["reference_link_name"]
		.as<std::string>()).index();
        const auto depth_camera_frame_id = plant->GetBodyFrameIdOrThrow(body_node_index);

        const auto tf_yaml =
            camera_extrinsics_yaml["depth"]["extrinsics"]
                                  ["transform_to_reference_link"];
        RigidTransform<double> depth_camera_tf(
            Quaternion<double>(tf_yaml["rotation"]["w"].as<double>(),
                               tf_yaml["rotation"]["x"].as<double>(),
                               tf_yaml["rotation"]["y"].as<double>(),
                               tf_yaml["rotation"]["z"].as<double>()),
            Eigen::Vector3d(tf_yaml["translation"]["x"].as<double>(),
                            tf_yaml["translation"]["y"].as<double>(),
                            tf_yaml["translation"]["z"].as<double>()));
        auto camera =
            builder.template AddSystem<systems::sensors::dev::RgbdCamera>(
                camera_name, depth_camera_frame_id, depth_camera_tf.GetAsIsometry3(),
                camera_properties, false);
        builder.Connect(scene_graph->get_query_output_port(),
                        camera->query_object_input_port());

        builder.ExportOutput(camera->color_image_output_port(),
                             camera_name + "_rgb_image");
        builder.ExportOutput(camera->depth_image_output_port(),
                             camera_name + "_depth_image");
        builder.ExportOutput(camera->label_image_output_port(),
                             camera_name + "_label_image");
      }
    }
  }

  // Visualizers
  geometry::ConnectDrakeVisualizer(&builder, station->get_scene_graph(),
                                   station->GetOutputPort("pose_bundle"));
  auto ros_visualizer = builder.AddSystem<RosSceneGraphVisualizer>(
    station->get_scene_graph());
  builder.Connect(station->GetOutputPort("pose_bundle"),
                  ros_visualizer->get_pose_bundle_input_port());

  drake::lcm::DrakeLcm lcm;
  lcm.StartReceiveThread();

  // TODO(russt): IiwaCommandReceiver should output positions, not
  // state.  (We are adding delay twice in this current implementation).
  auto iiwa_command_subscriber = builder.AddSystem(
      systems::lcm::LcmSubscriberSystem::Make<drake::lcmt_iiwa_command>(
          "IIWA_COMMAND", &lcm));
  auto iiwa_command = builder.AddSystem<examples::kuka_iiwa_arm::IiwaCommandReceiver>();
  builder.Connect(iiwa_command_subscriber->get_output_port(),
                  iiwa_command->get_input_port(0));

  // Pull the positions out of the state.
  auto demux = builder.AddSystem<systems::Demultiplexer>(14, 7);
  builder.Connect(iiwa_command->get_commanded_state_output_port(),
                  demux->get_input_port(0));
  builder.Connect(demux->get_output_port(0),
                  station->GetInputPort("iiwa_position"));
  builder.Connect(iiwa_command->get_commanded_torque_output_port(),
                  station->GetInputPort("iiwa_feedforward_torque"));

  auto iiwa_status = builder.AddSystem<examples::kuka_iiwa_arm::IiwaStatusSender>();
  // The IiwaStatusSender input port wants size 14, but only uses the first 7.
  // TODO(russt): Consider cleaning up the IiwaStatusSender.
  auto zero_padding =
      builder.AddSystem<systems::MatrixGain>(Eigen::MatrixXd::Identity(14, 7));
  builder.Connect(station->GetOutputPort("iiwa_position_commanded"),
                  zero_padding->get_input_port());
  builder.Connect(zero_padding->get_output_port(),
                  iiwa_status->get_command_input_port());
  builder.Connect(station->GetOutputPort("iiwa_state_estimated"),
                  iiwa_status->get_state_input_port());
  builder.Connect(station->GetOutputPort("iiwa_torque_commanded"),
                  iiwa_status->get_commanded_torque_input_port());
  builder.Connect(station->GetOutputPort("iiwa_torque_measured"),
                  iiwa_status->get_measured_torque_input_port());
  builder.Connect(station->GetOutputPort("iiwa_torque_external"),
                  iiwa_status->get_external_torque_input_port());
  auto iiwa_status_publisher = builder.AddSystem(
      systems::lcm::LcmPublisherSystem::Make<drake::lcmt_iiwa_status>(
          "IIWA_STATUS", &lcm));
  iiwa_status_publisher->set_publish_period(0.005);
  builder.Connect(iiwa_status->get_output_port(0),
                  iiwa_status_publisher->get_input_port());

  auto wsg_ros_actionserver = builder.AddSystem<SchunkWsgActionServer>(
    "/wsg50_driver/wsg50/gripper_control/",
    "/wsg50_driver/wsg50/status");
  builder.Connect(wsg_ros_actionserver->get_position_output_port(),
                  station->GetInputPort("wsg_position"));
  builder.Connect(wsg_ros_actionserver->get_force_limit_output_port(),
                  station->GetInputPort("wsg_force_limit"));
  builder.Connect(station->GetOutputPort("wsg_state_measured"),
                  wsg_ros_actionserver->get_measured_state_input_port());
  builder.Connect(station->GetOutputPort("wsg_force_measured"),
                  wsg_ros_actionserver->get_measured_force_input_port());
  auto diagram = builder.Build();

  systems::Simulator<double> simulator(*diagram);
  auto& context = simulator.get_mutable_context();
  auto& station_context =
      diagram->GetMutableSubsystemContext(*station, &context);

  // Set initial conditions for the IIWA:
  VectorXd q0(7);
  // A comfortable pose inside the workspace of the workcell.
  q0 << 0, 0.6, 0, -1.75, 0, 1.0, 0;
  iiwa_command->set_initial_position(
      &diagram->GetMutableSubsystemContext(*iiwa_command, &context), q0);
  station->SetIiwaPosition(q0, &station_context);
  const VectorXd qdot0 = VectorXd::Zero(7);
  station->SetIiwaVelocity(qdot0, &station_context);

  // Place the object in the center of the table in front of the robot.
  Eigen::Isometry3d pose = Eigen::Isometry3d::Identity();
  pose.translation() = Eigen::Vector3d(.6, 0, 0);
  station->get_multibody_plant().tree().SetFreeBodyPoseOrThrow(
      station->get_multibody_plant().GetBodyByName("base_link",
                                                           object),
      pose, &station->GetMutableSubsystemContext(
                station->get_multibody_plant(), &station_context));

// Yuck, repeating this iteration from above is unfortunate.
// Maybe store a dict up above of initial poses we should set,
// or see if I can do this before finalization is done on the plant diagram?
    for (const auto& node : station_config["instances"]) {
	const auto pose = node["q0"].as<std::vector<double>>();
	Eigen::Vector3d xyz(pose[0], pose[1], pose[2]);
	Eigen::Vector3d rpy(pose[3], pose[4], pose[5]);
	RigidTransform<double> object_tf(RollPitchYaw<double>(rpy), xyz);

	const auto object_class = node["model"].as<std::string>();
        auto object_class_node = station_config["models"][object_class];
	std::string model_name = node["model"].as<std::string>();
        
	if (!node["fixed"].as<bool>()) {
	 plant->tree().SetFreeBodyPoseOrThrow(
		plant->GetBodyByName(model_name),
		object_tf.GetAsIsometry3(),
		&station->GetMutableSubsystemContext(
                	*plant, &station_context));
	}
    }

  simulator.set_publish_every_time_step(false);
  simulator.set_target_realtime_rate(FLAGS_target_realtime_rate);
  simulator.Initialize();
  simulator.StepTo(FLAGS_duration);

  return 0;
}

}  // namespace
}  // namespace drake_iiwa_sim

int main(int argc, char* argv[]) {
  return drake_iiwa_sim::do_main(argc, argv);
}