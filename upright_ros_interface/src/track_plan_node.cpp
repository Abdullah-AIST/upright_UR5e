#include <mobile_manipulation_central/kalman_filter.h>
#include <mobile_manipulation_central/robot_interfaces.h>
#include <ocs2_mpc/SystemObservation.h>
#include <ocs2_msgs/mpc_observation.h>
#include <ocs2_pinocchio_interface/PinocchioEndEffectorKinematics.h>
#include <ocs2_ros_interfaces/mrt/MRT_ROS_Interface.h>
#include <pybind11/embed.h>
#include <realtime_tools/realtime_publisher.h>
#include <ros/init.h>
#include <ros/package.h>
#include <signal.h>
#include <upright_control/constraint/obstacle_constraint.h>
#include <upright_control/controller_interface.h>
#include <upright_control/reference_trajectory.h>
#include <upright_ros_interface/parsing.h>
#include <upright_ros_interface/safety.h>

#include <iostream>

using namespace upright;

// Robot is a global variable so we can send it a brake command in the SIGINT
// handler
std::unique_ptr<mm::RobotROSInterface> robot_ptr;

// Custom SIGINT handler
void sigint_handler(int sig) {
    std::cerr << "Received SIGINT." << std::endl;
    std::cerr << "Braking robot." << std::endl;
    robot_ptr->brake();
    ros::shutdown();
}

int main(int argc, char** argv) {
    const std::string robot_name = "mobile_manipulator";

    if (argc < 2) {
        throw std::runtime_error("Config path is required.");
    }

    // Initialize ros node
    ros::init(argc, argv, robot_name + "_mrt");
    ros::NodeHandle nh;
    std::string config_path = std::string(argv[1]);

    // controller interface
    // Python interpreter required for now because we actually load the control
    // settings and the target trajectories using Python - not ideal but easier
    // than re-implementing the parsing logic in C++
    py::scoped_interpreter guard{};
    ControllerSettings settings = parse_control_settings(config_path);
    std::cout << settings << std::endl;
    ControllerInterface interface(settings);
    const auto& r = settings.dims.robot;

    SafetyMonitor monitor(settings, interface.get_pinocchio_interface());

    // MRT
    ocs2::MRT_ROS_Interface mrt(robot_name);
    mrt.initRollout(&interface.get_rollout());
    mrt.launchNodes(nh);

    // When debugging, we publish the desired state and input planned by the
    // MPC at each timestep.
    realtime_tools::RealtimePublisher<ocs2_msgs::mpc_observation> mpc_plan_pub(
        nh, robot_name + "_mpc_plan", 1);

    realtime_tools::RealtimePublisher<ocs2_msgs::mpc_observation> cmd_pub(
        nh, robot_name + "_cmds", 1);

    // Initialize the robot interface
    if (r.q == 3) {
        robot_ptr.reset(new mm::RidgebackROSInterface(nh));
    } else if (r.q == 6) {
        robot_ptr.reset(new mm::UR10ROSInterface(nh));
    } else if (r.q == 9) {
        robot_ptr.reset(new mm::MobileManipulatorROSInterface(nh));
    } else {
        throw std::runtime_error("Unsupported base type.");
    }

    // Set up a custom SIGINT handler to brake the robot before shutting down
    // (this is why we set it up after the robot is initialized)
    signal(SIGINT, sigint_handler);

    ros::Rate rate(settings.tracking.rate);

    // Wait until we get feedback from the robot to do remaining setup.
    while (ros::ok()) {
        ros::spinOnce();
        if (robot_ptr->ready()) {
            break;
        }
        rate.sleep();
    }
    std::cout << "Received feedback from robot." << std::endl;

    // Update initial state with robot feedback
    VecXd x0 = interface.get_initial_state();
    x0.head(r.q) = robot_ptr->q();

    // Reset MPC with our desired target trajectory
    ocs2::TargetTrajectories target = parse_target_trajectory(config_path, x0);
    mrt.resetMpcNode(target);

    // Initial state and input
    VecXd x = x0;
    VecXd xd = VecXd::Zero(x.size());
    VecXd u = VecXd::Zero(settings.dims.u());
    size_t mode = 0;

    ocs2::SystemObservation observation;
    observation.state = x0;
    observation.input = u;

    ros::Duration policy_update_delay(settings.tracking.min_policy_update_time);
    const ocs2::scalar_t dt0 = 1 / settings.tracking.rate;
    const ocs2::scalar_t dt_warn = 1.5 / settings.tracking.rate;

    // Position gain
    const MatXd Kp = MatXd::Identity(r.q, r.q);

    // Commands
    VecXd v_cmd = VecXd::Zero(r.v);
    VecXd u_cmd = VecXd::Zero(r.u);

    const VecXd original_target_state = target.stateTrajectory[0];

    // Let MPC generate the initial plan
    // TODO: problem: this now starts from the original time but we actually
    // want to update t0 later on
    observation.time = 0;
    mrt.setCurrentObservation(observation);
    while (ros::ok()) {
        mrt.spinMRT();
        if (mrt.initialPolicyReceived()) {
            break;
        }
        rate.sleep();
    }
    mrt.updatePolicy();
    std::cout << "Received first policy." << std::endl;

    // Initialize time
    ros::Time now = ros::Time::now();
    ros::Time last_policy_update_time = now;
    ocs2::scalar_t t = now.toSec();
    ocs2::scalar_t last_t = t;
    const ocs2::scalar_t t0 = t;

    // Now that we're all set up and have an initial policy, we can get started
    // moving the robot.
    while (ros::ok()) {
        now = ros::Time::now();
        last_t = t;
        t = now.toSec();
        ocs2::scalar_t dt = t - last_t;

        if (dt >= dt_warn) {
            ROS_WARN_STREAM("Loop is slow: dt = " << 1000 * dt << " ms.");
        }

        // Robot feedback
        // TODO maybe filter a bit if needed?
        VecXd q = robot_ptr->q();

        // Compute optimal state and input using current policy.
        // Note that we evaluate w.r.t. t = 0.
        if (t - t0 <= settings.mpc.timeHorizon_) {
            mrt.evaluatePolicy(t - t0, x, xd, u, mode);

            VecXd qd = xd.head(r.q);
            VecXd vd = xd.segment(r.q, r.v);
            v_cmd = Kp * (qd - q) + vd;
        } else {
            // After the plan expires, we just send zero commands.
            std::cout << "outside plan horizon" << std::endl;
            v_cmd = VecXd::Zero(r.v);
        }

        if (settings.debug) {
            // Publish planned state and input
            if (mpc_plan_pub.trylock()) {
                VecX<float> xf = xd.cast<float>();
                VecX<float> uf = u.cast<float>();

                mpc_plan_pub.msg_.time = t;
                mpc_plan_pub.msg_.state.value =
                    std::vector<float>(xf.data(), xf.data() + xf.size());
                mpc_plan_pub.msg_.input.value =
                    std::vector<float>(uf.data(), uf.data() + uf.size());
                mpc_plan_pub.unlockAndPublish();
            }

            // Publish commanded (integrated) velocity and acceleration
            if (cmd_pub.trylock()) {
                VecX<float> xf = VecX<float>::Zero(r.x);
                xf.segment(r.q, r.v) = v_cmd.cast<float>();

                cmd_pub.msg_.time = t;
                cmd_pub.msg_.state.value =
                    std::vector<float>(xf.data(), xf.data() + xf.size());
                cmd_pub.unlockAndPublish();
            }
        }

        // Check that controller is not making the end effector leave allowed
        // region
        if (settings.tracking.enforce_ee_position_limits &&
            monitor.end_effector_position_violated(target, t, xd)) {
            break;
        }

        // Check that the controller has provided sane values.
        // Check monitor first so we can still log violations
        if (settings.tracking.enforce_state_limits &&
            monitor.state_limits_violated(xd)) {
            break;
        }
        if (settings.tracking.enforce_input_limits &&
            monitor.input_limits_violated(u)) {
            break;
        }

        // TODO probably should be a real-time publisher
        robot_ptr->publish_cmd_vel(v_cmd, /* bodyframe = */ false);

        ros::spinOnce();
        rate.sleep();
    }

    // stop the robot when we're done
    std::cout << "Braking robot." << std::endl;
    robot_ptr->brake();
    ros::shutdown();

    // Successful exit
    return 0;
}
