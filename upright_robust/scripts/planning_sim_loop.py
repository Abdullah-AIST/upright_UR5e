#!/usr/bin/env python3
"""Closed-loop upright simulation using Pybullet."""
import argparse
import copy
import datetime

import numpy as np
import pybullet as pyb
from pyb_utils.frame import debug_frame_world
import matplotlib.pyplot as plt
import rigeo as rg
import cvxpy as cp

from upright_core.logging import DataLogger, DataPlotter
import upright_sim as sim
import upright_core as core
import upright_control as ctrl
import upright_cmd as cmd

import IPython


OBJECT_NAME = "sim_block"
PLOT = False
MU = 0.2


def compute_trajectory(config):
    """Compute and unroll the planned trajectory."""
    ctrl_config = config["controller"]
    ctrl_manager = ctrl.manager.ControllerManager.from_config(ctrl_config)
    mpc = ctrl_manager.mpc
    model = ctrl_manager.model
    dims = model.settings.dims
    ref = ctrl_manager.ref

    dt = config["simulation"]["timestep"]

    x0 = model.settings.initial_state
    target = ref.get_desired_pose(model.settings.mpc.time_horizon)[0]

    t = 0.0
    x = x0
    xd = np.zeros_like(x)
    u = np.zeros(dims.u())

    mpc.setObservation(t, x, u)
    mpc.advanceMpc()

    step = 0
    xds = []
    while t <= model.settings.mpc.time_horizon:
        if step % 100 == 0:
            mpc.evaluateMpcSolution(t, x, xd, u)
            xds.append(xd.copy())
        step += 1
        t = step * dt
    model.update(xds[-1], u)
    rd = model.robot.link_pose()[0]
    err = np.linalg.norm(target - rd)
    return np.array(xds), err


def run_simulation(config, video, logname, use_gui=True):
    sim_config = config["simulation"]
    ctrl_config = config["controller"]

    # start the simulation
    print("starting sim")
    timestamp = datetime.datetime.now()
    env = sim.simulation.UprightSimulation(
        config=sim_config,
        timestamp=timestamp,
        video_name=video,
        gui=use_gui,
        extra_gui=sim_config.get("extra_gui", False),
    )

    # fixture all objects
    env.fixture_objects(do_all=False)

    # brake the robot
    env.robot.command_velocity(np.zeros(env.robot.nv), bodyframe=False)

    # initial time, state, input
    t = 0.0
    q, v = env.robot.joint_states()
    a = np.zeros(env.robot.nv)
    x_obs = env.dynamic_obstacle_state()
    x = np.concatenate((q, v, a, x_obs))

    # controller
    ctrl_manager = ctrl.manager.ControllerManager.from_config(ctrl_config, x0=x)
    mpc = ctrl_manager.mpc
    model = ctrl_manager.model
    dims = model.settings.dims
    ref = ctrl_manager.ref

    u = np.zeros(dims.u())
    xd = np.zeros_like(x)

    # make sure PyBullet (simulation) and Pinocchio (controller) models agree
    r_pyb, Q_pyb = env.robot.link_pose()
    r_pin, Q_pin = model.robot.link_pose()
    assert np.allclose(r_pyb, r_pin)
    assert np.allclose(Q_pyb, Q_pin)

    # data logging
    logger = DataLogger(config)

    logger.add("sim_timestep", env.timestep)
    logger.add("duration", env.duration)
    logger.add("ctrl_timestep", ctrl_manager.timestep)
    logger.add("object_names", [str(name) for name in env.objects.keys()])

    logger.add("nq", ctrl_config["robot"]["dims"]["q"])
    logger.add("nv", ctrl_config["robot"]["dims"]["v"])
    logger.add("nx", ctrl_config["robot"]["dims"]["x"])
    logger.add("nu", ctrl_config["robot"]["dims"]["u"])

    # frames for desired waypoints
    if sim_config.get("show_debug_frames", False):
        for r_ew_w_d, Q_we_d in ref.poses():
            debug_frame_world(0.2, list(r_ew_w_d), orientation=Q_we_d, line_width=3)

    v_cmd = np.zeros_like(v)
    a_est = np.zeros_like(a)

    Kp = np.eye(dims.robot.v)

    # make the plan
    mpc.setObservation(t, x, u)
    mpc.advanceMpc()
    print("Ready to start.")

    # compute initial object offset from tray
    r_ew_w, Q_we = env.robot.link_pose()
    r_ow_ws, Q_wos = env.object_poses()
    C_we = core.math.quat_to_rot(Q_we)
    r_oe_e0 = C_we.T @ (r_ow_ws[0] - r_ew_w)

    # simulation loop
    step = 0
    while t <= env.duration:
        # get the true robot feedback
        # instead of a proper estimator (e.g. Kalman filter) we're being lazy
        # assuming the model tracks well enough that real acceleration is
        # roughly perfect
        q, v = env.robot.joint_states(add_noise=False)
        x_obs = env.dynamic_obstacle_state()
        x = np.concatenate((q, v, a_est, x_obs))

        # now get the noisy version for use in the controller
        q_noisy, v_noisy = env.robot.joint_states(add_noise=True)
        x_noisy = np.concatenate((q_noisy, v_noisy, a_est, x_obs))

        if t <= model.settings.mpc.time_horizon:
            mpc.evaluateMpcSolution(t, x_noisy, xd, u)
            xd_robot = xd[: dims.robot.x]
            u_cmd = u[: dims.robot.u]
            f = u[-dims.f() :]

            if np.isnan(u).any():
                print("NaN value in input!")
                IPython.embed()
                break

            # simple P controller + feedforward
            qd = xd_robot[: dims.robot.q]
            vd = xd_robot[dims.robot.q : dims.robot.q + dims.robot.v]
            v_cmd = Kp @ (qd - q_noisy) + vd

            # estimated acceleration
            a_est = a_est + env.timestep * u_cmd
        else:
            v_cmd = np.zeros(dims.robot.u)

        # generated velocity is in the world frame
        env.robot.command_velocity(v_cmd, bodyframe=False)

        # TODO more logger reforms to come
        if step % 100 == 0:
            # log sim stuff
            r_ew_w, Q_we = env.robot.link_pose()
            v_ew_w, ω_ew_w = env.robot.link_velocity()
            r_ow_ws, Q_wos = env.object_poses()
            logger.append("ts", t)
            logger.append("us", u_cmd)
            logger.append("xs", x)
            logger.append("xds", xd)
            logger.append("uds", u)
            logger.append("r_ew_ws", r_ew_w)
            logger.append("Q_wes", Q_we)
            logger.append("v_ew_ws", v_ew_w)
            logger.append("ω_ew_ws", ω_ew_w)
            logger.append("cmd_vels", v_cmd)
            logger.append("r_ow_ws", r_ow_ws)
            logger.append("Q_wos", Q_wos)

            # log controller stuff
            r_ew_w_d, Q_we_d = ctrl_manager.ref.get_desired_pose(t)
            logger.append("r_ew_w_ds", r_ew_w_d)
            logger.append("Q_we_ds", Q_we_d)

            logger.append("solve_times", ctrl_manager.mpc.getLastSolveTime())

            # compute current object offset from tray
            C_we = core.math.quat_to_rot(Q_we)
            r_oe_e = C_we.T @ (r_ow_ws[0] - r_ew_w)
            obj_err = np.linalg.norm(r_oe_e - r_oe_e0)
            # print(f"obj_err = {obj_err}")

            logger.append("cost", ctrl_manager.mpc.cost(t, x, u))

            if model.settings.balancing_settings.enabled:
                model.update(x, u)
                logger.append("contact_forces", f)
                object_dynamics_constraints = (
                    ctrl_manager.mpc.getStateInputEqualityConstraintValue(
                        "object_dynamics", t, x, u
                    )
                )
                logger.append(
                    "object_dynamics_constraints", object_dynamics_constraints
                )

        # NOTE I am now manually incrementing time for more accuracy
        env.step(t, step_robot=False)
        step += 1
        t = step * env.timestep

    # save logged data
    if logname is not None:
        logger.save(timestamp, name=logname)

    if env.video_manager.save:
        print(f"Saved video to {env.video_manager.path}")

    # visualize data
    if PLOT:
        DataPlotter.from_logger(logger).plot_all(show=False)

        # plotting for desired trajectories
        prop_cycle = plt.rcParams["axes.prop_cycle"]
        colors = prop_cycle.by_key()["color"]

        ts = np.array(logger.data["ts"])
        xds = np.array(logger.data["xds"])
        xs = np.array(logger.data["xs"])
        uds = np.array(logger.data["uds"])
        us = np.array(logger.data["us"])

        plt.figure()
        for i in range(env.robot.nq):
            plt.plot(ts, xds[:, i], label=f"qd_{i}", linestyle="--")
        for i in range(env.robot.nq):
            plt.plot(ts, xs[:, i], label=f"q_{i}", color=colors[i])
        plt.xlabel("Time [s]")
        plt.ylabel("Joint position")
        plt.title("Desired vs. Actual Joint Positions")
        plt.legend()
        plt.grid()

        plt.figure()
        for i in range(env.robot.nv):
            plt.plot(ts, xds[:, env.robot.nq + i], label=f"vd_{i}", linestyle="--")
        for i in range(env.robot.nv):
            plt.plot(ts, xs[:, env.robot.nq + i], label=f"v_{i}", color=colors[i])
        plt.xlabel("Time [s]")
        plt.ylabel("Joint velocity")
        plt.title("Desired vs. Actual Joint Velocities")
        plt.legend()
        plt.grid()

        plt.figure()
        for i in range(env.robot.nv):
            plt.plot(
                ts,
                xds[:, env.robot.nq + env.robot.nv + i],
                label=f"ad_{i}",
                linestyle="--",
            )
        for i in range(env.robot.nv):
            plt.plot(
                ts,
                xs[:, env.robot.nq + env.robot.nv + i],
                label=f"a_{i}",
                color=colors[i],
            )
        plt.xlabel("Time [s]")
        plt.ylabel("Joint acceleration")
        plt.title("Desired vs. Actual Joint Acceleration")
        plt.legend()
        plt.grid()

        plt.figure()
        for i in range(env.robot.nv):
            plt.plot(ts, uds[:, i], label=f"ud_{i}", linestyle="--")
        for i in range(env.robot.nv):
            plt.plot(ts, us[:, i], label=f"u_{i}", color=colors[i])
        plt.xlabel("Time [s]")
        plt.ylabel("Joint jerk")
        plt.title("Desired vs. Actual Joint Jerk Input")
        plt.legend()
        plt.grid()

        plt.show()

    pyb.disconnect(env.client_id)


def box_face_centers(box):
    x, y, z = box.half_extents
    return [[x, 0, 0], [-x, 0, 0], [0, y, 0], [0, -y, 0], [0, 0, z], [0, 0, -z]]


def max_min_eig_inertia(box, com, diag=True):
    """Find the inertia matrix about the CoM with the maximum smallest eigenvalue.

    The masses are places at the vertices of the box.
    """
    μs = cp.Variable(8)
    vs = [np.append(v, 1) for v in box.vertices]
    J = cp.sum([μ * np.outer(v, v) for μ, v in zip(μs, vs)])

    Hc = J[:3, :3] - np.outer(com, com)  # Hc is about the CoM
    λ = cp.Variable(1)
    objective = cp.Maximize(λ)
    drip_constraints = [
        Hc >> 0,
        cp.sum(μs) == 1,
        μs >= 0,
        λ >= 0,
    ]

    # if diag=True, only optimize over the diagonal of I
    if diag:
        Ic = cp.Variable(3)
        constraints = [
            λ * np.ones(3) <= Ic,
            cp.diag(Ic) == cp.trace(Hc) * np.eye(3) - Hc,
        ] + drip_constraints
    else:
        Ic = cp.Variable((3, 3))
        constraints = [
            λ * np.eye(3) << Ic,
            Ic == cp.trace(Hc) * np.eye(3) - Hc,
        ] + drip_constraints

    problem = cp.Problem(objective, constraints)
    problem.solve(cp.CLARABEL)
    # print(Ic.value)
    # print(problem.value)

    return Ic.value


def max_trace_inertia(box, com, about_com=True):
    """Find the inertia matrix with the maximum trace.

    The masses are places at the vertices of the box.
    """
    raise NotImplementedError()
    μ = cp.Variable(8)
    I = cp.Variable((3, 3))
    H = cp.Variable((3, 3))
    objective = cp.Maximize(cp.trace(I))
    constraints = [
        I == cp.trace(H) * np.eye(3) - H,
        H == cp.sum([m * np.outer(v, v) for m, v in zip(μ, box.vertices)]),
        com == cp.sum([m * v for m, v in zip(μ, box.vertices)]),
        1 == cp.sum(μ),
        μ >= 0,
    ]
    problem = cp.Problem(objective, constraints)
    problem.solve(cp.CLARABEL)
    assert problem.status == "optimal"

    if about_com:
        params = rg.InertialParameters(mass=1.0, com=np.array(com), H=H.value)
        return params.Ic
    return I.value


def make_arrangement_config(object_names, x_offset, mu):
    """Generate the config the arrangement of objects."""
    objects_config = [
        {
            "name": name,
            "type": name,
            "parent": "ee",
            "offset": {"x": x_offset},
        }
        for name in object_names
    ]
    contacts_config = [
        {
            "first": "ee",
            "second": name,
            "mu": mu,
            "support_area_inset": 0.0,
        }
        for name in object_names
    ]

    return {
        "objects": objects_config,
        "contacts": contacts_config,
    }


def main():
    np.set_printoptions(precision=5, suppress=True)

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, help="Path to configuration file.")
    parser.add_argument(
        "--log",
        action="store_true",
        help="Log data. Optionally specify prefix for log directoy.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Use PyBullet GUI. Otherwise, use DIRECT interface.",
    )
    parser.add_argument(
        "--com",
        choices=["center", "top", "bottom", "robust", "exact"],
        required=True,
        help="Where the controller should put the CoM.",
    )
    parser.add_argument(
        "--height",
        required=True,
        type=int,
        help="Height of the balanced object, in centimeters.",
    )
    parser.add_argument("--video", action="store_true", help="Record a video.")
    args = parser.parse_args()

    use_robust = args.com == "robust"
    use_exact = args.com == "exact"

    # load configuration
    master_config = core.parsing.load_config(args.config)

    # waypoints from the original paper
    waypoints = [[0.65, 0.0, 0.25]]

    h_cm = args.height
    h_m = args.height / 100
    h2_m = 0.5 * h_m
    b = 0.06  # 12cm x 12cm bounding box for the CoM

    ctrl_obj_config = {
        "mass": 1.0,
        "shape": "cuboid",
        "side_lengths": [0.15, 0.15, h_m],
        "color": [1, 0, 0, 1],
        "bounds": {
            "approx": {
                "com_lower": [-b, -b, -h2_m],
                "com_upper": [b, b, h2_m],
            },
            "realizable": {
                "com_lower": [-b, -b, -h2_m],
                "com_upper": [b, b, h2_m],
            },
        },
    }

    sim_obj_config = {
        "mass": 1.0,
        "shape": "cuboid",
        "side_lengths": ctrl_obj_config["side_lengths"],
        "color": [1, 0, 0, 1],
    }

    # box = rg.Box.from_side_lengths(ctrl_obj_config["side_lengths"])
    box = rg.Box.from_side_lengths(sim_obj_config["side_lengths"])
    sim_com_box = rg.Box.from_two_vertices(
        ctrl_obj_config["bounds"]["realizable"]["com_lower"],
        ctrl_obj_config["bounds"]["realizable"]["com_upper"],
    )
    ctrl_com_box = rg.Box.from_two_vertices(
        ctrl_obj_config["bounds"]["approx"]["com_lower"],
        ctrl_obj_config["bounds"]["approx"]["com_upper"],
    )

    # build the nominal arrangement
    # this is even built in the robust case for later post-processing
    # NOTE: we do not require a deepcopy here unless we were changing values in
    # the nested dicts (i.e., the bounds), or directly modifying elements of
    # the nested lists
    nom_ctrl_obj_config = ctrl_obj_config.copy()
    if args.com == "center" or use_robust:
        nom_ctrl_obj_config["com_offset"] = [0, 0, 0]
    elif args.com == "bottom":
        nom_ctrl_obj_config["com_offset"] = [0, 0, -float(ctrl_com_box.half_extents[2])]
    elif args.com == "top":
        nom_ctrl_obj_config["com_offset"] = [0, 0, float(ctrl_com_box.half_extents[2])]
    elif not use_exact:
        raise ValueError("unexpected value for --com")
    master_config["controller"]["objects"][OBJECT_NAME] = nom_ctrl_obj_config

    x_offset = 0
    nom_ctrl_arrangement_config = make_arrangement_config(
        [OBJECT_NAME], x_offset=x_offset, mu=MU
    )
    master_config["controller"]["arrangements"]["nominal"] = nom_ctrl_arrangement_config

    if use_robust:
        # make a config for each CoM we care about
        x, y, z = ctrl_com_box.half_extents.tolist()
        ctrl_com_offsets = ctrl_com_box.vertices

        object_names = [OBJECT_NAME + f"_{i+1}" for i in range(len(ctrl_com_offsets))]

        params_center = box.uniform_density_params(mass=1)

        # add to the main config
        for name, com in zip(object_names, ctrl_com_offsets):
            config = ctrl_obj_config.copy()
            config["com_offset"] = com.tolist()
            master_config["controller"]["objects"][name] = config

        rob_ctrl_arrangement_config = make_arrangement_config(
            object_names, x_offset=x_offset, mu=MU
        )
        master_config["controller"]["arrangements"][
            "robust"
        ] = rob_ctrl_arrangement_config
        master_config["controller"]["balancing"]["arrangement"] = "robust"
    else:
        master_config["controller"]["balancing"]["arrangement"] = "nominal"

    # simulation arrangement
    sim_arrangement_name = "nominal"
    sim_arrangement_config = make_arrangement_config(
        [OBJECT_NAME], x_offset=x_offset, mu=MU
    )
    master_config["simulation"]["arrangements"][
        sim_arrangement_name
    ] = sim_arrangement_config
    master_config["simulation"]["arrangement"] = sim_arrangement_name

    sim_com_offsets = (
        [[0, 0, 0]]
        + box_face_centers(sim_com_box)
        + [list(v) for v in sim_com_box.vertices]
    )

    # mass-normalized inertias (about the CoM)
    sim_inertias_diag = [
        sim_obj_config["mass"] * max_min_eig_inertia(box, c, diag=True)
        for c in sim_com_offsets
    ]
    sim_inertia_scales = [1.0, 0.5, 0.1]

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dirname = f"{args.com}_h{h_cm}_{timestamp}"

    # video
    if args.video:
        com_idx = 0  # similar to real experiment
        com_offset = sim_com_offsets[com_idx]
        inertia_diag = sim_inertia_scales[0] * sim_inertias_diag[com_idx]
        waypoint = waypoints[0]

        config = copy.deepcopy(master_config)
        config["controller"]["waypoints"] = [
            {"time": 0, "position": waypoint, "orientation": [0, 0, 0, 1]}
        ]
        sim_obj_config["com_offset"] = np.array(com_offset).tolist()
        sim_obj_config["inertia_diag"] = inertia_diag.tolist()
        config["simulation"]["objects"][OBJECT_NAME] = sim_obj_config

        # config["simulation"]["extra_gui"] = True
        run_simulation(
            config=config, video=f"{args.com}_h{h_cm}", logname=None, use_gui=True
            #config=config, video=None, logname=None, use_gui=True
        )
        return

    # desired = {0 : [], 1: [], 2: []}
    # ee_errs = {0: [], 1: [], 2: []}
    # for iter in range(1, 11):
    #     for i, waypoint in enumerate(waypoints):
    #         config = copy.deepcopy(master_config)
    #         config["controller"]["sqp"]["init_sqp_iteration"] = iter
    #         config["controller"]["waypoints"] = [
    #             {"time": 0, "position": waypoint, "orientation": [0, 0, 0, 1]}
    #         ]
    #         # config["simulation"]["objects"][OBJECT_NAME] = sim_obj_config
    #
    #         if i > 0:
    #             config["controller"]["recompile_libraries"] = False
    #
    #         xds, err = compute_trajectory(config=config)
    #         desired[i].append(xds)
    #         ee_errs[i].append(err)
    #
    # deltas = {0: [], 1: [], 2: []}
    # for i in range(3):
    #     for j in range(len(desired[i]) - 1):
    #         err = np.sum(np.linalg.norm(desired[i][j+1] - desired[i][j], axis=1))
    #         deltas[i].append(err)
    #
    # IPython.embed()
    # return

    run = 1
    for com_offset, inertia_diag in zip(sim_com_offsets, sim_inertias_diag):
        for s in sim_inertia_scales:
            for waypoint in waypoints:
                print(f"run = {run}")

                config = copy.deepcopy(master_config)
                config["controller"]["waypoints"] = [
                    {"time": 0, "position": waypoint, "orientation": [0, 0, 0, 1]}
                ]

                # NOTE: no need to copy since we update every changing field
                # for each run
                sim_obj_config["com_offset"] = np.array(com_offset).tolist()
                sim_obj_config["inertia_diag"] = (s * inertia_diag).tolist()
                config["simulation"]["objects"][OBJECT_NAME] = sim_obj_config

                # when using the exact inertial parameters, we need to update
                # them for each run
                if use_exact:
                    nom_ctrl_obj_config["com_offset"] = np.array(com_offset).tolist()
                    nom_ctrl_obj_config["inertia"] = (s * inertia_diag).tolist()
                    config["controller"]["objects"][OBJECT_NAME] = nom_ctrl_obj_config

                # inspect a particular run
                # if run != 2:
                #     run += 1
                #     continue
                # IPython.embed()

                # only compile at most once
                if run > 1:
                    config["controller"]["recompile_libraries"] = False

                if args.log:
                    name = f"{dirname}/run_{run}"
                else:
                    name = None

                run_simulation(
                    config=config, video=None, logname=name, use_gui=args.gui
                )
                run += 1


if __name__ == "__main__":
    main()
