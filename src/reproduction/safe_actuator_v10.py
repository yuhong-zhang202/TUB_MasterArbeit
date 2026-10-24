"""Pure prospective safety checks for the Stage 6 V10 ramp-meter pilot.

The caller supplies live TraCI observations. These checks cannot predict SUMO's
stochastic next step; the post-green interlock is therefore mandatory.
"""

from dataclasses import dataclass
import math
from typing import Mapping


STOPLINE_MARGIN_M = 1.1  # Reuse the prospectively reviewed V9 guard margin.
STEP_S = 1.0


@dataclass(frozen=True)
class VehicleState:
    position_m: float
    speed_m_s: float
    length_m: float
    min_gap_m: float
    accel_m_s2: float
    normal_decel_m_s2: float


@dataclass(frozen=True)
class LeaderState:
    vehicle_id: str
    lane_id: str
    gap_excluding_ego_min_gap_m: float
    speed_m_s: float
    emergency_decel_m_s2: float


def _validate_vehicle(state: VehicleState) -> None:
    values = (state.position_m, state.speed_m_s, state.length_m,
              state.min_gap_m, state.accel_m_s2, state.normal_decel_m_s2)
    if (not all(math.isfinite(value) for value in values) or
            state.speed_m_s < 0 or state.length_m <= 0 or
            state.min_gap_m < 0 or state.accel_m_s2 <= 0 or
            state.normal_decel_m_s2 <= 0):
        raise ValueError("invalid vehicle dynamics or geometry")


def normal_red_stop_requirement_m(state: VehicleState,
                                  margin_m: float = STOPLINE_MARGIN_M) -> float:
    """V9 one-step acceleration plus normal-braking envelope.

    An already stopped vehicle needs no additional stopping distance. Moving
    vehicles are checked conservatively against a full acceleration step.
    """
    _validate_vehicle(state)
    if not math.isfinite(margin_m) or margin_m < 0:
        raise ValueError("invalid stop margin")
    if state.speed_m_s < 0.1:
        return 0.0
    next_speed = state.speed_m_s + state.accel_m_s2 * STEP_S
    return margin_m + next_speed * STEP_S + next_speed ** 2 / (2 * state.normal_decel_m_s2)


def immediate_red_stop_requirement_m(state: VehicleState,
                                     margin_m: float = STOPLINE_MARGIN_M) -> float:
    """Post-G bound when red is commanded before the very next SUMO step.

    Allow one reaction step at the *observed* post-G speed, then normal
    braking. The pre-G follower check above intentionally keeps its stronger
    acceleration-step bound while the follower may still see green.
    """
    _validate_vehicle(state)
    if not math.isfinite(margin_m) or margin_m < 0:
        raise ValueError("invalid stop margin")
    if state.speed_m_s < 0.1:
        return 0.0
    return (margin_m + state.speed_m_s * STEP_S +
            state.speed_m_s ** 2 / (2 * state.normal_decel_m_s2))


def secure_gap_query(front_id: str, front: VehicleState,
                     leader: LeaderState) -> dict:
    """Arguments for TraCI vehicle.getSecureGap, in its bumper-gap convention.

    getLeader.gap already excludes ego minGap. The returned getSecureGap value
    is compared to that gap directly; do not subtract minGap a second time.
    Leader emergencyDecel covers the stronger leader-braking case, while the
    follower's own red-stop guard continues to use normalDecel.
    """
    if not front_id or not leader.vehicle_id or leader.vehicle_id == front_id:
        raise ValueError("invalid front/leader identity")
    _validate_vehicle(front)
    if (not all(math.isfinite(value) for value in
                (leader.gap_excluding_ego_min_gap_m, leader.speed_m_s,
                 leader.emergency_decel_m_s2)) or
            leader.gap_excluding_ego_min_gap_m < 0 or leader.speed_m_s < 0 or
            leader.emergency_decel_m_s2 <= 0):
        raise ValueError("invalid leader state")
    return {"vehID": front_id,
            "speed": front.speed_m_s + front.accel_m_s2 * STEP_S,
            "leaderSpeed": leader.speed_m_s,
            "leaderMaxDecel": leader.emergency_decel_m_s2,
            "leaderID": leader.vehicle_id}


def assess_release(*, vehicles: Mapping[str, VehicleState],
                   storage_length_m: float, route_coverage_ok: bool,
                   leader_lookahead_m: float, connected_path_length_m: float,
                   allowed_downstream_lanes: frozenset[str],
                   leader: LeaderState | None,
                   secure_gap_m: float | None,
                   nearest_internal_vehicle_id: str | None,
                   nearest_internal_rear_clearance_m: float,
                   margin_m: float = STOPLINE_MARGIN_M) -> dict:
    """Assess one potential G step; motion reachability is only a heuristic.

    `nearest_internal_rear_clearance_m` is the nearest internal-lane vehicle's
    rear position in metres after the stop line, or +inf when that lane is
    empty. `secure_gap_m` must be the result of secure_gap_query followed by
    TraCI getSecureGap, not an invented geometric substitute.
    """
    if (not math.isfinite(storage_length_m) or storage_length_m <= 0 or
            not math.isfinite(connected_path_length_m) or connected_path_length_m <= 0 or
            not math.isfinite(leader_lookahead_m) or leader_lookahead_m <= 0 or
            not math.isfinite(margin_m) or margin_m < 0):
        raise ValueError("invalid checked geometry")
    if not vehicles:
        return {"allowed": False, "reason": "NO_FRONT", "front_id": ""}
    for state in vehicles.values():
        _validate_vehicle(state)
    front_id = max(vehicles, key=lambda vehicle_id: vehicles[vehicle_id].position_m)
    front = vehicles[front_id]
    gap = storage_length_m - front.position_m
    if gap < 0:
        raise ValueError("vehicle beyond stop line on storage lane")

    unsafe_followers = tuple(sorted(vehicle_id for vehicle_id, state in vehicles.items()
                                    if vehicle_id != front_id and
                                    storage_length_m - state.position_m + 1e-9 <
                                    normal_red_stop_requirement_m(state, margin_m)))
    if unsafe_followers:
        return {"allowed": False, "reason": "FOLLOWER_STOP_DISTANCE",
                "front_id": front_id, "unsafe_followers": unsafe_followers}

    basic_receiver = (nearest_internal_vehicle_id is None or
                      nearest_internal_rear_clearance_m >=
                      front.length_m + front.min_gap_m)
    if front.speed_m_s < 0.1:
        return {"allowed": gap <= margin_m and basic_receiver,
                "reason": "STOPPED_READY" if gap <= margin_m and basic_receiver
                else "STOPPED_FRONT_OR_RECEIVER_NOT_READY", "front_id": front_id,
                "branch": "stopped"}

    max_next_speed = front.speed_m_s + front.accel_m_s2 * STEP_S
    possible_one_step_crossing = gap <= max_next_speed * STEP_S
    required_lookahead = max(
        connected_path_length_m,
        front.length_m + front.min_gap_m + margin_m +
        max_next_speed * STEP_S + max_next_speed ** 2 /
        (2 * front.normal_decel_m_s2))
    if not possible_one_step_crossing:
        reason = "FRONT_NOT_ONE_STEP_REACHABLE"
    elif not route_coverage_ok or leader_lookahead_m + 1e-9 < required_lookahead:
        reason = "DOWNSTREAM_COVERAGE_UNKNOWN"
    elif nearest_internal_vehicle_id is not None and (
            leader is None or leader.vehicle_id != nearest_internal_vehicle_id):
        reason = "INTERNAL_LEADER_MISMATCH"
    elif leader is not None and leader.lane_id not in allowed_downstream_lanes:
        reason = "LEADER_OFF_CHECKED_PATH"
    elif leader is not None and (
            secure_gap_m is None or not math.isfinite(secure_gap_m) or secure_gap_m < 0):
        reason = "SECURE_GAP_UNAVAILABLE"
    else:
        reason = "MOVING_READY"
        if leader is not None:
            secure_gap_query(front_id, front, leader)  # Validate actual leader input.
            if leader.gap_excluding_ego_min_gap_m + 1e-9 < secure_gap_m + margin_m:
                reason = "LEADER_SECURE_GAP"
        if reason == "MOVING_READY" and nearest_internal_vehicle_id is not None:
            if not math.isfinite(nearest_internal_rear_clearance_m):
                reason = "INTERNAL_CLEARANCE_UNKNOWN"
            else:
                # The explicit internal-lane coordinate gives bumper gap to
                # leader rear as rear_clearance + stopline_gap. Subtract ego
                # minGap exactly once to match getLeader/getSecureGap.
                internal_gap_excluding_min_gap = (
                    nearest_internal_rear_clearance_m + gap - front.min_gap_m)
                max_entry = max(0.0, max_next_speed * STEP_S - gap)
                if (internal_gap_excluding_min_gap + 1e-9 < secure_gap_m + margin_m or
                        nearest_internal_rear_clearance_m + 1e-9 <
                        front.length_m + front.min_gap_m + max_entry + margin_m):
                    reason = "INTERNAL_RECEIVER_CLEARANCE"
    return {"allowed": reason == "MOVING_READY", "reason": reason,
            "front_id": front_id, "branch": "moving",
            "stopline_gap_m": gap, "max_next_speed_m_s": max_next_speed,
            "required_lookahead_m": required_lookahead,
            "leader_id": leader.vehicle_id if leader else "",
            "secure_gap_m": secure_gap_m}


def post_green_red_interlock(*, expected_front_id: str,
                             crossing_ids: tuple[str, ...],
                             remaining_storage: Mapping[str, VehicleState],
                             storage_length_m: float,
                             margin_m: float = STOPLINE_MARGIN_M) -> dict:
    """Decide whether the next step may be red after exactly one G step.

    Abort means stop the technical run *before* another simulation step. Never
    force red across a vehicle that cannot normally stop, and never extend G
    to clear a front while a second vehicle could enter.
    """
    if not expected_front_id or not math.isfinite(storage_length_m) or storage_length_m <= 0:
        raise ValueError("invalid green-slot identity or geometry")
    crossings = tuple(crossing_ids)
    if crossings and crossings != (expected_front_id,):
        return {"safe_to_red": False, "abort": True,
                "reason": "WRONG_OR_MULTIPLE_CROSSING", "empty_green": False}
    unsafe = []
    for vehicle_id, state in remaining_storage.items():
        _validate_vehicle(state)
        if (storage_length_m - state.position_m + 1e-9 <
                immediate_red_stop_requirement_m(state, margin_m)):
            unsafe.append(vehicle_id)
    if unsafe:
        return {"safe_to_red": False, "abort": True,
                "reason": "UNSAFE_GREEN_TO_RED", "unsafe_ids": tuple(sorted(unsafe)),
                "empty_green": not crossings}
    return {"safe_to_red": True, "abort": False,
            "reason": "EMPTY_GREEN_SAFE_RED" if not crossings else "ONE_FRONT_CROSSED_SAFE_RED",
            "empty_green": not crossings}
