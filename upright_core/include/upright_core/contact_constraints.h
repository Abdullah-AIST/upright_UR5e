#pragma once

#include <Eigen/Eigen>

#include "upright_core/contact.h"
#include "upright_core/rigid_body.h"
#include "upright_core/types.h"
#include "upright_core/util.h"

namespace upright {

// Number of friction cone constraints per contact. One constraint for the
// normal force to be non-negative; one for the friction cone.
const size_t NUM_FRICTION_CONSTRAINTS_PER_CONTACT = 2;

// Or we can linear the friction cone
const size_t NUM_LINEARIZED_FRICTION_CONSTRAINTS_PER_CONTACT = 5;

// Three for linear and three for rotation.
const size_t NUM_DYNAMICS_CONSTRAINTS_PER_OBJECT = 6;

template <typename Scalar>
VecX<Scalar> compute_contact_force_constraints(
    const std::vector<ContactPoint<Scalar>>& contacts,
    const VecX<Scalar>& forces) {
    const size_t n = NUM_FRICTION_CONSTRAINTS_PER_CONTACT;
    VecX<Scalar> constraints(contacts.size() * n);

    for (int i = 0; i < contacts.size(); ++i) {
        auto& contact = contacts[i];
        Vec3<Scalar> f = forces.segment(i * 3, 3);

        // normal force
        Scalar f_n = contact.normal.dot(f);

        // squared magnitude of tangential force
        Scalar f_t_squared = f.dot(f) - f_n * f_n;

        // constrain the normal force to be non-negative
        constraints(i * n) = f_n;

        // non-linear exact version of Coulomb friction cone
        constraints(i * n + 1) =
            contact.mu * contact.mu * f_n * f_n - f_t_squared;
    }
    return constraints;
}

template <typename Scalar>
VecX<Scalar> compute_contact_force_constraints_linearized(
    const std::vector<ContactPoint<Scalar>>& contacts,
    const VecX<Scalar>& forces) {
    const size_t n = NUM_LINEARIZED_FRICTION_CONSTRAINTS_PER_CONTACT;
    VecX<Scalar> constraints(contacts.size() * n);

    for (int i = 0; i < contacts.size(); ++i) {
        auto& contact = contacts[i];
        Vec3<Scalar> f = forces.segment(i * 3, 3);

        // normal force
        Scalar f_n = contact.normal.dot(f);

        // tangential force is obtained by projecting onto the nullspace of the
        // normal vector
        Vec2<Scalar> f_t = contact.span * f;

        // constrain the normal force to be non-negative
        constraints(i * n) = f_n;

        // linearized version
        constraints(i * n + 1) = contact.mu * f_n - f_t(0) - f_t(1);
        constraints(i * n + 2) = contact.mu * f_n - f_t(0) + f_t(1);
        constraints(i * n + 3) = contact.mu * f_n + f_t(0) - f_t(1);
        constraints(i * n + 4) = contact.mu * f_n + f_t(0) + f_t(1);
    }
    return constraints;
}

template <typename Scalar>
Wrench<Scalar> compute_object_dynamics_constraint(
    const RigidBody<Scalar>& body, const Wrench<Scalar>& wrench,
    const RigidBodyState<Scalar>& state, const Vec3<Scalar>& gravity,
    const Scalar scale) {
    Scalar m = body.mass;
    Mat3<Scalar> C_ew = state.pose.orientation.transpose();
    Vec3<Scalar> gravito_inertial_force =
        m * C_ew *
        (state.acceleration.linear + dC_dtt(state) * body.com - gravity);

    Vec3<Scalar> angular_vel_e = C_ew * state.velocity.angular;
    Vec3<Scalar> angular_acc_e = C_ew * state.acceleration.angular;
    Mat3<Scalar> I_e = body.inertia;
    Vec3<Scalar> inertial_torque =
        angular_vel_e.cross(I_e * angular_vel_e) + I_e * angular_acc_e;

    // Normalize the entire constraint by the mass to make the constraint
    // values invariant to object mass
    Wrench<Scalar> constraints;
    constraints.force = (scale * gravito_inertial_force - wrench.force) / m;
    constraints.torque = (scale * inertial_torque - wrench.torque) / m;
    return constraints;
}

// Sum the forces and torques acting on each object at their various contact
// points.
template <typename Scalar>
std::map<std::string, Wrench<Scalar>> compute_object_wrenches(
    const std::map<std::string, RigidBody<Scalar>>& bodies,
    const std::vector<ContactPoint<Scalar>>& contacts,
    const VecX<Scalar>& forces) {
    const bool frictionless = (forces.size() == contacts.size());
    std::map<std::string, Wrench<Scalar>> object_wrenches;
    for (int i = 0; i < contacts.size(); ++i) {
        auto& contact = contacts[i];
        Vec3<Scalar> f;
        if (frictionless) {
            f = forces(i) * contact.normal;
        } else {
            f = forces.segment(i * 3, 3);
        }

        // TODO this relies on object frame having same orientation as EE frame

        // The first object may be the EE, which we do not store in the bodies
        // map because we do not need to compute a wrench or constraints for it
        if (bodies.find(contact.object1_name) != bodies.end()) {
            Vec3<Scalar> com1 = bodies.at(contact.object1_name).com;
            Vec3<Scalar> lever1 = contact.r_co_o1 - com1;

            if (object_wrenches.find(contact.object1_name) ==
                object_wrenches.end()) {
                object_wrenches[contact.object1_name].force = f;
                object_wrenches[contact.object1_name].torque = lever1.cross(f);
            } else {
                object_wrenches[contact.object1_name].force += f;
                object_wrenches[contact.object1_name].torque += lever1.cross(f);
            }
        }

        {
            Vec3<Scalar> com2 = bodies.at(contact.object2_name).com;
            Vec3<Scalar> lever2 = contact.r_co_o2 - com2;

            // For the second object, the forces are negative
            if (object_wrenches.find(contact.object2_name) ==
                object_wrenches.end()) {
                object_wrenches[contact.object2_name].force = -f;
                object_wrenches[contact.object2_name].torque = lever2.cross(-f);
            } else {
                object_wrenches[contact.object2_name].force -= f;
                object_wrenches[contact.object2_name].torque +=
                    lever2.cross(-f);
            }
        }
    }
    return object_wrenches;
}

// Compute the dynamics constraints for all objects given the contact points
// and corresponding vector of contact forces.
template <typename Scalar>
VecX<Scalar> compute_object_dynamics_constraints(
    const std::map<std::string, RigidBody<Scalar>>& bodies,
    const std::vector<ContactPoint<Scalar>>& contacts,
    const VecX<Scalar>& forces, const RigidBodyState<Scalar>& state,
    const Vec3<Scalar>& gravity) {
    const std::map<std::string, Wrench<Scalar>> object_wrenches =
        compute_object_wrenches(bodies, contacts, forces);

    VecX<Scalar> constraints(NUM_DYNAMICS_CONSTRAINTS_PER_OBJECT *
                             bodies.size());

    // Scale force by square root of number of contacts so that L2-penalized
    // soft constraint is invariant to number of contacts
    // const Scalar force_scale(1. / sqrt(contacts.size()));
    const Scalar force_scale(1.0);
    // const Scalar force_scale(1. / contacts.size());

    size_t i = 0;
    for (const auto& kv : bodies) {
        auto& name = kv.first;
        auto& body = kv.second;

        Wrench<Scalar> wrench = object_wrenches.at(name);
        Wrench<Scalar> constraint = compute_object_dynamics_constraint(
            body, wrench, state, gravity, force_scale);
        constraints.segment(i * NUM_DYNAMICS_CONSTRAINTS_PER_OBJECT,
                            NUM_DYNAMICS_CONSTRAINTS_PER_OBJECT)
            << constraint.force,
            constraint.torque;
        i++;
    }
    return constraints;
}

}  // namespace upright
