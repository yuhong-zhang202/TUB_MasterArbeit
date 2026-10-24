"""Pure, one-second occupancy feedback and ramp-slot scheduling.

Research values are supplied by the reviewed card; this module freezes only
units, interval semantics, and actuator bookkeeping. No SUMO import or launch.
"""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class FeedbackParameters:
    target_pct: float
    gain_veh_h_per_pct: float
    minimum_veh_h: float
    maximum_veh_h: float
    initial_veh_h: float

    def __post_init__(self):
        values = tuple(vars(self).values())
        if not all(math.isfinite(x) for x in values):
            raise ValueError("nonfinite feedback parameter")
        if not (0 <= self.target_pct <= 100 and self.gain_veh_h_per_pct > 0):
            raise ValueError("invalid target or gain")
        if not (0 < self.minimum_veh_h <= self.initial_veh_h <= self.maximum_veh_h <= 1200):
            raise ValueError("invalid or unschedulable rate bounds")


class Feedback:
    def __init__(self, parameters: FeedbackParameters):
        self.p = parameters
        self.rate = parameters.initial_veh_h
        self.last_update = 600

    def update(self, time_s: int, interval_begin_s: int, interval_end_s: int,
               occupancy_l0_pct: float, occupancy_l1_pct: float):
        if time_s != self.last_update + 30 or time_s > 4170:
            raise ValueError("unexpected feedback update time")
        if (interval_begin_s, interval_end_s) != (time_s - 30, time_s):
            raise ValueError("feedback interval must be previous complete 30 s")
        if not all(math.isfinite(x) and 0 <= x <= 100 for x in
                   (occupancy_l0_pct, occupancy_l1_pct)):
            raise ValueError("invalid lane occupancy")
        mean = (occupancy_l0_pct + occupancy_l1_pct) / 2
        previous = self.rate
        raw = previous + self.p.gain_veh_h_per_pct * (self.p.target_pct - mean)
        self.rate = min(self.p.maximum_veh_h, max(self.p.minimum_veh_h, raw))
        self.last_update = time_s
        return {"update_time_s": time_s, "interval_begin_s": interval_begin_s,
                "interval_end_s": interval_end_s, "occ_l0_pct": occupancy_l0_pct,
                "occ_l1_pct": occupancy_l1_pct, "occ_mean_pct": mean,
                "rate_previous_veh_h": previous, "rate_raw_veh_h": raw,
                "rate_clipped_veh_h": self.rate}


class PulseScheduler:
    """No initial credit: at 900 veh/h, first green is [603,604)."""
    def __init__(self):
        self.credit = 0.0
        self.last_green_s = None
        self.next_time_s = 600
        self.dropped_credit = 0.0

    def step(self, time_s: int, rate_veh_h: float, guard_allowed: bool = True):
        if time_s != self.next_time_s or not (600 <= time_s < 4200):
            raise ValueError("nonsequential actuator step")
        if not math.isfinite(rate_veh_h) or not 0 < rate_veh_h <= 1200:
            raise ValueError("invalid actuator rate")
        before = self.credit
        self.credit += rate_veh_h / 3600
        allowed = self.last_green_s is None or time_s - self.last_green_s >= 3
        nominal = self.credit >= 1 - 1e-10 and allowed
        slot = nominal and guard_allowed
        if slot:
            self.credit -= 1
            self.last_green_s = time_s
        # A denied slot retains at most one credit; excess accrual is lost,
        # so reopening cannot release a backlog burst.
        if self.credit > 1:
            self.dropped_credit += self.credit - 1
            self.credit = 1
        self.next_time_s += 1
        return {"credit_before": before, "credit_after": self.credit,
                "nominal_slot_scheduled": nominal, "slot_scheduled": slot,
                "credit_deferred": bool(nominal and not guard_allowed),
                "requested_state": "G" if slot else "r",
                "dropped_credit_total": self.dropped_credit}


def assess_storage_safety(vehicle_states, front_id, front_ready, lane_length_m,
                          acceleration_by_type, deceleration_by_type,
                          margin_m=1.1, step_s=1.0):
    """Conservative one-step reach then comfortable-braking check.

    vehicle_states maps ID to (front position, speed, type ID). The stopped
    front/receiving check is supplied by front_at_stopline. This is an
    actuator guard, not an exact reproduction of SUMO car-following.
    """
    if not (math.isfinite(lane_length_m) and lane_length_m > 0 and
            math.isfinite(margin_m) and margin_m >= 0 and step_s == 1.0):
        raise ValueError("invalid safety geometry or step")
    report = []
    unsafe = []
    for vehicle_id, (position, speed, type_id) in sorted(vehicle_states.items()):
        if not all(math.isfinite(x) for x in (position, speed)) or speed < 0:
            raise ValueError("invalid storage vehicle state")
        if type_id not in acceleration_by_type or type_id not in deceleration_by_type:
            raise ValueError("missing actual vehicle-type dynamics")
        accel, decel = acceleration_by_type[type_id], deceleration_by_type[type_id]
        if not all(math.isfinite(x) and x > 0 for x in (accel, decel)):
            raise ValueError("invalid actual vehicle-type dynamics")
        speed_after_step = speed + accel * step_s
        required = margin_m + speed_after_step * step_s + speed_after_step ** 2 / (2 * decel)
        gap = lane_length_m - position
        safe = vehicle_id == front_id or gap + 1e-9 >= required
        report.append({"vehicle_id":vehicle_id,"position_m":position,"speed_m_s":speed,
                       "type_id":type_id,"accel_m_s2":accel,"decel_m_s2":decel,
                       "stopline_gap_m":gap,"required_stop_gap_m":required,
                       "follower_safe":safe})
        if vehicle_id != front_id and not safe:
            unsafe.append(vehicle_id)
    if not front_id:
        reason = "NO_FRONT"
    elif not front_ready:
        reason = "FRONT_OR_RECEIVER_NOT_READY"
    elif unsafe:
        reason = "FOLLOWER_STOP_DISTANCE"
    else:
        reason = "ALLOW"
    return {"allowed":reason == "ALLOW", "reason":reason,
            "unsafe_follower_ids":unsafe,"vehicle_states":report}


def front_at_stopline(vehicle_states, downstream_states, lane_length_m,
                      distance_m=1.1, maximum_speed_m_s=0.1):
    """Return true lead ID, readiness and rear clearance at the stop line.

    vehicle_states: ID -> (front position, speed, length, minGap).
    downstream_states: ID -> (front position, length) on the internal lane.
    This is a pre-slot snapshot; actual crossing must be measured separately.
    """
    if not vehicle_states:
        return "", False, math.inf, math.nan
    if not math.isfinite(lane_length_m) or lane_length_m <= 0:
        raise ValueError("invalid lane length")
    front, (position, speed, length, min_gap) = max(vehicle_states.items(), key=lambda item:item[1][0])
    if not all(math.isfinite(x) for x in (position,speed,length,min_gap)) or length <= 0 or min_gap < 0:
        raise ValueError("nonfinite front state")
    clearance = min((pos - other_length for pos,other_length in downstream_states.values()),default=math.inf)
    required = length + min_gap
    ready = (lane_length_m-distance_m <= position <= lane_length_m and
             0 <= speed < maximum_speed_m_s and clearance >= required)
    return front, ready, clearance, required


def classify_service_window(*, queued_unblocked_slots: int, crossings_in_slots: int,
                            red_crossings: int, repeated_slots: int,
                            wrong_vehicle_slots: int = 0,
                            minimum_slots: int = 20, minimum_ratio: float = 0.9):
    """Technical qualification, not a behavioral or scientific effect test.

    The caller must show a vehicle waiting at the stop line and an empty
    internal connector before each counted slot. Insufficient opportunities
    remain INDETERMINATE, never pass. All crossings must be reported.
    """
    if min(queued_unblocked_slots, crossings_in_slots, red_crossings,
           repeated_slots, wrong_vehicle_slots) < 0:
        raise ValueError("negative service count")
    if red_crossings or repeated_slots or wrong_vehicle_slots:
        return "FAIL_RED_MULTIPLE_OR_WRONG_VEHICLE"
    if queued_unblocked_slots < minimum_slots:
        return "NOT_SUFFICIENTLY_TESTED"
    if crossings_in_slots > queued_unblocked_slots:
        return "FAIL_ACCOUNTING"
    if crossings_in_slots / queued_unblocked_slots < minimum_ratio:
        return "FAIL_SATURATED_SERVICE"
    return "PASS_TECHNICAL_SERVICE"
