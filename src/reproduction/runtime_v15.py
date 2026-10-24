"""Archived V15 traffic loop, adapted only for portable input/output binding.

The feedback, detector accounting, safety decision and step sequence are copied
from the historical Stage 6 V15 runner (original SHA-256 cb05feb9...).
This module is launched only by reproduce_stage6.py after input checks.
"""
from __future__ import annotations
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from src.reproduction.control import Feedback, FeedbackParameters, PulseScheduler, classify_service_window
from src.reproduction.safe_actuator_v10 import VehicleState, LeaderState, assess_release, post_green_red_interlock, secure_gap_query

STARTUP_DEADLINE_S = 60.0
PARAMETERS = dict(target_pct=11.0, gain_veh_h_per_pct=70.0,
                  minimum_veh_h=300.0, maximum_veh_h=900.0,
                  initial_veh_h=900.0)
DETECTORS = ("p1_main_down_20_l0", "p1_main_down_20_l1")
STORAGE_LANE, INTERNAL_LANE, TLS = "ramp_storage_0", ":ramp_mid_0_0", "ramp_mid"
HORIZON = 4200
NETWORK = None
SUMO = None
SUMO_HOME = None

def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()



def now():
    return datetime.now(timezone.utc).isoformat()



def write_json_exclusive(path, obj):
    with Path(path).open("x") as stream:
        json.dump(obj, stream, indent=2, sort_keys=True)
        stream.write("\n")



def checked_downstream_geometry():
    root = ET.parse(NETWORK).getroot()
    names = (INTERNAL_LANE, "ramp_accel_0", ":freeway_merge_2_0", "merge_section_0")
    lengths = {}
    for name in names:
        lane = root.find(f".//lane[@id='{name}']")
        if lane is None:
            raise ValueError(f"missing checked downstream lane: {name}")
        lengths[name] = float(lane.get("length"))
    connected_length = lengths[INTERNAL_LANE] + lengths["ramp_accel_0"]
    lookahead = sum(lengths.values())
    if abs(connected_length - 181.05) > 0.02 or abs(lookahead - 478.67) > 0.02:
        raise ValueError("checked downstream geometry changed")
    return connected_length, lookahead



def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        return sock.getsockname()[1]



def connect_traci(traci_module,port,process,trace,deadline_s=STARTUP_DEADLINE_S,
                  clock=None,sleeper=None,enforce_alarm=True):
    """Bounded TraCI handshakes with timestamped child-status evidence.

    Each attempt is one TraCI protocol connection. No separate TCP probe may
    consume the server's single-client slot. ``proc`` makes early exit visible.
    """
    clock=clock or time.monotonic
    sleeper=sleeper or time.sleep
    started=clock()
    attempt=0
    while True:
        attempt+=1
        before=clock()
        remaining=deadline_s-(before-started)
        if remaining <= 0:
            raise TimeoutError(f"TraCI startup deadline {deadline_s}s expired")
        poll_before=process.poll()
        if poll_before is not None:
            raise RuntimeError(f"SUMO exited before TraCI attempt {attempt}: {poll_before}")
        try:
            if enforce_alarm:
                old_handler=signal.getsignal(signal.SIGALRM)
                def deadline_expired(signum,frame):
                    raise TimeoutError(f"TraCI startup deadline {deadline_s}s expired during handshake")
                signal.signal(signal.SIGALRM,deadline_expired)
                signal.setitimer(signal.ITIMER_REAL,remaining)
            try:
                conn=traci_module.connect(port=port,host="localhost",proc=process,
                                          numRetries=0,waitBetweenRetries=0)
            finally:
                if enforce_alarm:
                    signal.setitimer(signal.ITIMER_REAL,0)
                    signal.signal(signal.SIGALRM,old_handler)
            trace({"attempt":attempt,"at_utc":now(),"elapsed_since_start_s":before-started,
                   "startup_deadline_monotonic_s":started+deadline_s,
                   "duration_s":clock()-before,"poll_before":poll_before,
                   "poll_after":process.poll(),"outcome":"CONNECTED"})
            return conn
        except Exception as exc:
            polled=process.poll()
            trace({"attempt":attempt,"at_utc":now(),"elapsed_since_start_s":before-started,
                   "startup_deadline_monotonic_s":started+deadline_s,
                   "duration_s":clock()-before,"poll_before":poll_before,
                   "poll_after":polled,"outcome":"FAILED","error_type":type(exc).__name__,
                   "error":str(exc)})
            if polled is not None:
                raise RuntimeError(f"SUMO exited during TraCI startup: {polled}") from exc
            if isinstance(exc,TimeoutError):
                raise
            if clock()-started >= deadline_s:
                raise TimeoutError(f"TraCI startup deadline {deadline_s}s expired after {attempt} attempts") from exc
            if type(exc).__name__ != "FatalTraCIError":
                raise
            sleeper(min(0.1,max(0.0,deadline_s-(clock()-started))))



def passive_startup_probe(pid,output,mark_s,deadline_at,run=None,clock=None):
    """Inspect one existing SUMO child; never open a TraCI/TCP connection."""
    run=run or subprocess.run
    clock=clock or time.monotonic
    startup_at=deadline_at-STARTUP_DEADLINE_S
    checks=[("tcp_listeners",["/usr/sbin/lsof","-nP","-a","-p",str(pid),"-iTCP"],0.5,0.1),
            ("process_state",["/bin/ps","-o","pid,ppid,state,wchan,command","-p",str(pid)],0.5,0.1)]
    if mark_s==6:
        checks.append(("stack_sample",["/usr/bin/sample",str(pid),"1","10","-file",
                                       str(output/"sumo_startup_sample_6s.txt")],1.75,1.25))
    result={"at_utc":now(),"mark_s":mark_s,"pid":pid,"checks":{}}
    for name,command,timeout_s,minimum_start_s in checks:
        started_at=clock()
        remaining=deadline_at-started_at-0.05
        if remaining<minimum_start_s:
            result["checks"][name]={"command":command,"status":"SKIPPED_DEADLINE",
                                    "at_utc":now(),"elapsed_since_start_s":started_at-startup_at,
                                    "remaining_s":max(0,remaining)}
            continue
        command_timeout=min(timeout_s,remaining)
        try:
            completed=run(command,capture_output=True,text=True,timeout=command_timeout)
            result["checks"][name]={"command":command,"return_code":completed.returncode,
                                    "at_utc":now(),"elapsed_since_start_s":started_at-startup_at,
                                    "duration_s":clock()-started_at,"timeout_s":command_timeout,
                                    "stdout":completed.stdout[-16000:],
                                    "stderr":completed.stderr[-16000:]}
        except subprocess.TimeoutExpired as exc:
            result["checks"][name]={"command":command,"status":"TIMEOUT",
                                    "at_utc":now(),"elapsed_since_start_s":started_at-startup_at,
                                    "duration_s":clock()-started_at,"timeout_s":command_timeout,
                                    "stdout_tail":str(exc.stdout)[-16000:],
                                    "stderr_tail":str(exc.stderr)[-16000:]}
        except Exception as exc:
            result["checks"][name]={"command":command,"status":"ERROR",
                                    "at_utc":now(),"elapsed_since_start_s":started_at-startup_at,
                                    "duration_s":clock()-started_at,"timeout_s":command_timeout,
                                    "error":f"{type(exc).__name__}: {exc}"}
    return result



def due_startup_probes(record,completed_marks):
    if record.get("outcome")!="FAILED": return []
    return [mark for mark in (2,6) if mark not in completed_marks and
            record["elapsed_since_start_s"] >= mark]



def csv_writer(path, fields):
    stream = path.open("x", newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    return stream, writer



class DetectorEventLedger:
    """Keep E1 detector events across XML reset boundaries, with no time union."""
    def __init__(self, begin_s=600):
        self.begin_s = begin_s
        self.next_step_end_s = begin_s + 1
        self.events = {detector:{} for detector in DETECTORS}

    def add_step(self, step_end_s, detector_events):
        if step_end_s != self.next_step_end_s or set(detector_events) != set(DETECTORS):
            raise ValueError("missing, out-of-order, or wrong-detector E1 event step")
        normalized = {detector:[] for detector in DETECTORS}
        for detector in DETECTORS:
            seen = set()
            prior_active = {key for key,event in self.events[detector].items() if event[3] == -1}
            for item in detector_events[detector]:
                if len(item) != 5:
                    raise ValueError("invalid E1 vehicle data tuple")
                vehicle_id,length,entry,leave,type_id = item
                if (not isinstance(vehicle_id,str) or not vehicle_id or
                        not isinstance(type_id,str) or not type_id or
                        not all(math.isfinite(v) for v in (length,entry,leave)) or
                        length <= 0 or entry > step_end_s + 1e-6 or
                        (leave != -1 and (leave < entry or leave > step_end_s + 1e-6))):
                    raise ValueError("invalid E1 vehicle data values")
                key = (vehicle_id,entry)
                if key in seen:
                    raise ValueError("duplicate E1 vehicle event in one step")
                seen.add(key)
                previous = self.events[detector].get(key)
                if previous is not None:
                    if previous[1] != length or previous[4] != type_id:
                        raise ValueError("inconsistent E1 event metadata")
                    old_leave = previous[3]
                    if old_leave != -1 and abs(old_leave-leave) > 1e-6:
                        raise ValueError("conflicting completed E1 event")
                    if old_leave == -1 and leave != -1:
                        self.events[detector][key] = (vehicle_id,length,entry,leave,type_id)
                else:
                    self.events[detector][key] = (vehicle_id,length,entry,leave,type_id)
                normalized[detector].append((vehicle_id,length,entry,leave,type_id))
            if prior_active - seen:
                raise ValueError("ongoing E1 vehicle disappeared without completion event")
        self.next_step_end_s += 1
        return normalized

    def finish_window(self, end_s):
        if end_s != self.begin_s + 30 or self.next_step_end_s != end_s + 1:
            raise ValueError("incomplete 30-step E1 vehicle-event window")
        result = []
        for detector in DETECTORS:
            occupied_s = 0.0
            contributors = 0
            retained = {}
            for key,event in self.events[detector].items():
                _,_,entry,leave,_ = event
                segment = max(0.0,min(end_s,end_s if leave == -1 else leave)-max(self.begin_s,entry))
                if segment:
                    occupied_s += segment
                    contributors += 1
                if leave == -1 or leave > end_s:
                    retained[key] = event
            result.append((occupied_s / 30 * 100,contributors))
            self.events[detector] = retained
        self.begin_s = end_s
        return result



def validate_output(output):
    required = {"fcd.xml.gz","tripinfo.xml","vehroute.xml","lanechanges.xml",
                "queues.xml","sumo_summary.xml","tls_states.xml",
                "shared_boundary_e2.xml","ramp_storage_e2.xml",
                "controller_steps.csv","feedback_updates.csv","detector_vehicle_events.csv",
                "service_windows.csv",
                "vehicle_metadata.csv"}
    required |= {f"p1_{edge}_{pos}_l{lane}.xml"
                 for edge,pos,lanes in (("main_up",1300,2),("merge_section",20,3),
                                        ("main_down",20,2),("main_down",200,2))
                 for lane in range(lanes)}
    for name in sorted(required):
        path=output/name
        if not path.is_file() or path.stat().st_size==0:
            raise ValueError(f"missing/empty output {name}")
        if name.endswith(".xml") or name.endswith(".xml.gz"):
            with (gzip.open(path,"rb") if name.endswith(".gz") else path.open("rb")) as stream:
                for _,node in ET.iterparse(stream,events=("end",)):
                    node.clear()



def worker(card_path):
    """Run one V15 case under the bounded public replay wrapper."""
    import traci  # intentionally lazy: S1 imports and tests cannot launch SUMO
    card = json.loads(Path(card_path).read_text())
    package, output, mode = Path(card["package"]), Path(card["output"]), card["mode"]
    if not output.is_dir() or package != output.parent:
        raise ValueError("worker requires a new prepared replay directory")
    port = free_port()
    env = dict(os.environ, SUMO_HOME=str(SUMO_HOME))
    command = [str(SUMO),"-c",str(package/"scenario.sumocfg"),"--remote-port",str(port)]
    write_json_exclusive(output/"startup_context.json",
        {"worker_command":command,"historical_baseline_mode":"standalone SUMO",
         "worker_cwd":os.getcwd(),"sumo_home":env["SUMO_HOME"],
         "environment_override_keys":["SUMO_HOME"],
         "other_environment":"inherited unchanged from guardian",
         "traci_host":"localhost","port":port,"startup_deadline_s":STARTUP_DEADLINE_S,
         "baseline_runner_mode":"standalone SUMO without --remote-port"})
    sumo_stdout, sumo_stderr = (output/"sumo_stdout.log").open("x"), (output/"sumo_stderr.log").open("x")
    proc = subprocess.Popen(command, stdout=sumo_stdout, stderr=sumo_stderr, env=env)
    conn = None
    startup_trace=(output/"traci_startup.jsonl").open("x")
    probe_trace=(output/"startup_probe.jsonl").open("x")
    completed_probes=set()
    def trace_startup(record):
        startup_trace.write(json.dumps(record,sort_keys=True)+"\n")
        startup_trace.flush()
        for mark in due_startup_probes(record,completed_probes):
            completed_probes.add(mark)
            evidence=passive_startup_probe(proc.pid,output,mark,
                                           record["startup_deadline_monotonic_s"])
            probe_trace.write(json.dumps(evidence,sort_keys=True)+"\n")
            probe_trace.flush()
    steps_fields = ["time_begin_s","time_end_s","mode","command_rate_veh_h","credit_before",
                    "credit_after","nominal_slot_scheduled","guard_allowed","guard_reason",
                    "guard_rejected_slot","credit_deferred","guard_vehicle_states_json",
                    "guard_failed_follower_ids_json","slot_scheduled","requested_state","observed_state",
                    "queue_vehicle_count","queued_unblocked_slot","front_queued_vehicle_id",
                    "stopline_gap_m","downstream_rear_clearance_m","required_clearance_m",
                    "guard_decision_json","post_green_interlock_json",
                    "internal_before_ids_json",
                    "crossing_bracket_ids_json","red_crossing_bracket_ids_json",
                    "sampling_uncertain","dropped_credit_total"]
    feedback_fields = ["decision_time_s","application_time_s","interval_begin_s","interval_end_s",
                       "detector_l0","detector_l1","occupancy_source","sample_count",
                       "occ_l0_pct","occ_l1_pct","occ_mean_pct",
                       "event_contributors_l0","event_contributors_l1",
                       "legacy_last_interval_l0_pct","legacy_last_interval_l1_pct",
                       "n_vehicle_l0","n_vehicle_l1","valid","target_pct","gain_veh_h_per_pct",
                       "rate_previous_veh_h","rate_raw_veh_h","rate_clipped_veh_h"]
    service_fields = ["window_begin_s","window_end_s","nominal_slots","guard_rejected_slots",
                      "scheduled_slots","queued_unblocked_slots",
                      "crossings_in_slots","empty_slots","red_crossings","repeated_slots",
                      "wrong_vehicle_slots","classification"]
    sf, sw = csv_writer(output/"controller_steps.csv",steps_fields)
    ff, fw = csv_writer(output/"feedback_updates.csv",feedback_fields)
    of, ow = csv_writer(output/"detector_vehicle_events.csv",
                        ["time_begin_s","time_end_s","detector_id","event_count","vehicle_id",
                         "vehicle_length_m","entry_time_s","leave_time_s","type_id"])
    vf, vw = csv_writer(output/"service_windows.csv",service_fields)
    mf, mw = csv_writer(output/"vehicle_metadata.csv",
                        ["vehicle_id","class","first_departed_step_end_s","type_id",
                         "vehicle_length_m","min_gap_m"])
    feedback = Feedback(FeedbackParameters(**card["feedback"]))
    occupancy_events = DetectorEventLedger()
    pulses = PulseScheduler()
    window = dict(nominal_slots=0,guard_rejected_slots=0,
                  scheduled_slots=0, queued_unblocked_slots=0, crossings_in_slots=0,
                  empty_slots=0, red_crossings=0, repeated_slots=0, wrong_vehicle_slots=0)
    cumulative = dict(nominal_slots=0,guard_rejected_slots=0,scheduled_slots=0,
                      queued_unblocked_slots=0,crossings_in_slots=0,red_crossings=0,
                      repeated_slots=0,wrong_vehicle_slots=0)
    vehicle_type_dynamics = {}
    try:
        conn = connect_traci(traci,port,proc,trace_startup,
                             deadline_s=card["startup_deadline_s"])
        lane_length = card["meter_controlled_link"]["storage_length_m"]
        connected_path_length, leader_lookahead = checked_downstream_geometry()
        for t in range(HORIZON):
            if abs(conn.simulation.getTime()-t) > 1e-6:
                raise ValueError(f"TraCI time misalignment at {t}")
            if t in range(630,4171,30):
                event_result = occupancy_events.finish_window(t)
                occ = [result[0] for result in event_result]
                legacy_occ = [conn.inductionloop.getLastIntervalOccupancy(d) for d in DETECTORS]
                counts = [conn.inductionloop.getLastIntervalVehicleNumber(d) for d in DETECTORS]
                if mode == "ALINEA":
                    update = feedback.update(t,t-30,t,*occ)
                    update.pop("update_time_s")
                else:
                    update = dict(interval_begin_s=t-30,interval_end_s=t,
                                  occ_l0_pct=occ[0],occ_l1_pct=occ[1],
                                  occ_mean_pct=(occ[0]+occ[1])/2,
                                  rate_previous_veh_h="",rate_raw_veh_h="",rate_clipped_veh_h="")
                fw.writerow(dict(decision_time_s=t,application_time_s=t,detector_l0=DETECTORS[0],
                                 detector_l1=DETECTORS[1],occupancy_source="vehicle_event_residence_30",
                                 sample_count=30,
                                 event_contributors_l0=event_result[0][1],
                                 event_contributors_l1=event_result[1][1],
                                 legacy_last_interval_l0_pct=legacy_occ[0],
                                 legacy_last_interval_l1_pct=legacy_occ[1],
                                 n_vehicle_l0=counts[0],n_vehicle_l1=counts[1],
                                 valid=True,target_pct=PARAMETERS["target_pct"],
                                 gain_veh_h_per_pct=PARAMETERS["gain_veh_h_per_pct"],**update))
            before_ids = set(conn.lane.getLastStepVehicleIDs(INTERNAL_LANE)) if mode == "ALINEA" and t >= 600 else set()
            if mode == "ALINEA" and t >= 600:
                queue_ids = conn.lane.getLastStepVehicleIDs(STORAGE_LANE)
                vehicle_states = {}
                safety_states = {}
                for vid in queue_ids:
                    type_id=conn.vehicle.getTypeID(vid)
                    if type_id not in vehicle_type_dynamics:
                        accel=conn.vehicletype.getAccel(type_id)
                        decel=conn.vehicletype.getDecel(type_id)
                        emergency_decel=conn.vehicletype.getEmergencyDecel(type_id)
                        tau=conn.vehicletype.getTau(type_id)
                        action_step=conn.vehicletype.getActionStepLength(type_id)
                        if (not all(math.isfinite(x) for x in (accel,decel,emergency_decel,tau,action_step)) or
                                accel <= 0 or decel <= 0 or emergency_decel <= 0 or
                                abs(tau-1.0)>1e-9 or abs(action_step-1.0)>1e-9):
                            raise ValueError("actual type dynamics violate one-second guard contract")
                        vehicle_type_dynamics[type_id]=(accel,decel,emergency_decel)
                    accel,decel,_=vehicle_type_dynamics[type_id]
                    position=conn.vehicle.getLanePosition(vid)
                    speed=conn.vehicle.getSpeed(vid)
                    vehicle_states[vid]=(position,speed,conn.vehicle.getLength(vid),
                                         conn.vehicle.getMinGap(vid))
                    safety_states[vid]=VehicleState(position,speed,
                        vehicle_states[vid][2],vehicle_states[vid][3],accel,decel)
                front=max(safety_states,key=lambda vid:safety_states[vid].position_m) if safety_states else ""
                internal_rears={vid:conn.vehicle.getLanePosition(vid)-conn.vehicle.getLength(vid)
                                for vid in before_ids}
                nearest_internal=min(internal_rears,key=internal_rears.get) if internal_rears else None
                clearance=internal_rears[nearest_internal] if nearest_internal else math.inf
                required_clearance=(vehicle_states[front][2]+vehicle_states[front][3]) if front else ""
                stopline_gap=lane_length-safety_states[front].position_m if front else ""
                leader=None; secure_gap=None; route_coverage_ok=False; route=()
                if front:
                    route=tuple(conn.vehicle.getRoute(front))
                    route_coverage_ok=(conn.vehicle.getLaneID(front)==STORAGE_LANE and
                        any(route[i:i+3]==("ramp_storage","ramp_accel","merge_section")
                            for i in range(max(0,len(route)-2))))
                    raw_leader=conn.vehicle.getLeader(front,leader_lookahead)
                    if raw_leader is not None:
                        leader_id,leader_gap=raw_leader
                        leader_type=conn.vehicle.getTypeID(leader_id)
                        leader=LeaderState(leader_id,conn.vehicle.getLaneID(leader_id),
                            leader_gap,conn.vehicle.getSpeed(leader_id),
                            conn.vehicletype.getEmergencyDecel(leader_type))
                        secure_gap=conn.vehicle.getSecureGap(**secure_gap_query(
                            front,safety_states[front],leader))
                decision=assess_release(vehicles=safety_states,storage_length_m=lane_length,
                    route_coverage_ok=route_coverage_ok,leader_lookahead_m=leader_lookahead,
                    connected_path_length_m=connected_path_length,
                    allowed_downstream_lanes=frozenset((INTERNAL_LANE,"ramp_accel_0",
                        ":freeway_merge_2_0","merge_section_0","merge_section_1","merge_section_2")),
                    leader=leader,secure_gap_m=secure_gap,
                    nearest_internal_vehicle_id=nearest_internal,
                    nearest_internal_rear_clearance_m=clearance)
                decision["input_evidence"]={
                    "route":route,"route_coverage_ok":route_coverage_ok,
                    "leader_lookahead_m":leader_lookahead,
                    "connected_path_length_m":connected_path_length,
                    "leader":None if leader is None else {
                        "vehicle_id":leader.vehicle_id,"lane_id":leader.lane_id,
                        "gap_excluding_ego_min_gap_m":leader.gap_excluding_ego_min_gap_m,
                        "speed_m_s":leader.speed_m_s,
                        "emergency_decel_m_s2":leader.emergency_decel_m_s2},
                    "secure_gap_m":secure_gap,
                    "nearest_internal_vehicle_id":nearest_internal,
                    "nearest_internal_rear_clearance_m":clearance if math.isfinite(clearance) else None}
                front_ready=decision["allowed"]
                safety={"allowed":decision["allowed"],"reason":decision["reason"],
                        "unsafe_follower_ids":list(decision.get("unsafe_followers",())),
                        "vehicle_states":[{"vehicle_id":vid,"position_m":state.position_m,
                            "speed_m_s":state.speed_m_s,"type_id":conn.vehicle.getTypeID(vid),
                            "accel_m_s2":state.accel_m_s2,"decel_m_s2":state.normal_decel_m_s2}
                            for vid,state in safety_states.items()]}
                pulse = pulses.step(t,feedback.rate,safety["allowed"])
                guard_reason=(safety["reason"] if pulse["nominal_slot_scheduled"]
                              else "NOT_DUE_OR_MIN_RED")
                requested = pulse["requested_state"]
                conn.trafficlight.setRedYellowGreenState(TLS,requested)
                actual = conn.trafficlight.getRedYellowGreenState(TLS)
                if actual != requested:
                    raise ValueError("commanded and observed signal differ before step")
            else:
                front=""; front_ready=False; clearance=""; required_clearance=""; stopline_gap=""
                queue_ids=[]; safety={"allowed":"","reason":"","unsafe_follower_ids":[],"vehicle_states":[]}
                decision={};safety_states={}
                guard_reason="";pulse={"credit_before":"","credit_after":"",
                                "nominal_slot_scheduled":False,"credit_deferred":False,"slot_scheduled":False,
                                                 "dropped_credit_total":""}
                requested=""; actual=conn.trafficlight.getRedYellowGreenState(TLS)
            if mode == "NOOP" or t < 600:
                if actual != "G":
                    raise ValueError("pre-control/no-op ramp not continuously green")
            conn.simulationStep(t+1)
            if 600 <= t < 4170:
                raw_events = {detector:conn.inductionloop.getVehicleData(detector)
                              for detector in DETECTORS}
                observed = occupancy_events.add_step(t+1,raw_events)
                for detector in DETECTORS:
                    if not observed[detector]:
                        ow.writerow(dict(time_begin_s=t,time_end_s=t+1,detector_id=detector,
                                         event_count=0))
                    for vehicle_id,length,entry,leave,type_id in observed[detector]:
                        ow.writerow(dict(time_begin_s=t,time_end_s=t+1,detector_id=detector,
                                         event_count=len(observed[detector]),vehicle_id=vehicle_id,
                                         vehicle_length_m=length,entry_time_s=entry,
                                         leave_time_s=leave,type_id=type_id))
            for vid in conn.simulation.getDepartedIDList():
                mw.writerow({"vehicle_id":vid,"class":vid.split("_",1)[0],
                             "first_departed_step_end_s":t+1,
                             "type_id":conn.vehicle.getTypeID(vid),
                             "vehicle_length_m":conn.vehicle.getLength(vid),
                             "min_gap_m":conn.vehicle.getMinGap(vid)})
            after_ids = set(conn.lane.getLastStepVehicleIDs(INTERNAL_LANE)) if mode == "ALINEA" and t >= 600 else set()
            crossing = sorted(after_ids-before_ids)
            interlock = {}
            if mode == "ALINEA" and t >= 600:
                slot = bool(pulse["slot_scheduled"])
                nominal = bool(pulse["nominal_slot_scheduled"])
                queued_unblocked = bool(slot and front_ready)
                if slot:
                    remaining={}
                    for vid in conn.lane.getLastStepVehicleIDs(STORAGE_LANE):
                        type_id=conn.vehicle.getTypeID(vid)
                        if type_id not in vehicle_type_dynamics:
                            vehicle_type_dynamics[type_id]=(
                                conn.vehicletype.getAccel(type_id),
                                conn.vehicletype.getDecel(type_id),
                                conn.vehicletype.getEmergencyDecel(type_id))
                        accel,decel,_=vehicle_type_dynamics[type_id]
                        remaining[vid]=VehicleState(conn.vehicle.getLanePosition(vid),
                            conn.vehicle.getSpeed(vid),conn.vehicle.getLength(vid),
                            conn.vehicle.getMinGap(vid),accel,decel)
                    interlock=post_green_red_interlock(expected_front_id=front,
                        crossing_ids=tuple(crossing),remaining_storage=remaining,
                        storage_length_m=lane_length)
                    interlock["input_evidence"]={
                        "expected_front_id":front,"crossing_ids":crossing,
                        "storage_length_m":lane_length,"margin_m":1.1,
                        "remaining_storage":{vid:{
                            "position_m":state.position_m,"speed_m_s":state.speed_m_s,
                            "length_m":state.length_m,"min_gap_m":state.min_gap_m,
                            "accel_m_s2":state.accel_m_s2,
                            "normal_decel_m_s2":state.normal_decel_m_s2}
                            for vid,state in remaining.items()}}
                window["nominal_slots"] += int(nominal)
                window["guard_rejected_slots"] += int(nominal and not slot)
                window["scheduled_slots"] += int(slot)
                window["queued_unblocked_slots"] += int(queued_unblocked)
                unique_front = bool(queued_unblocked and crossing == [front])
                window["crossings_in_slots"] += int(unique_front)
                window["empty_slots"] += int(slot and not crossing)
                window["red_crossings"] += len(crossing) if not slot else 0
                window["repeated_slots"] += int(slot and len(crossing)>1)
                window["wrong_vehicle_slots"] += int(slot and queued_unblocked and crossing and not unique_front)
                cumulative["nominal_slots"] += int(nominal)
                cumulative["guard_rejected_slots"] += int(nominal and not slot)
                cumulative["scheduled_slots"] += int(slot)
                cumulative["queued_unblocked_slots"] += int(queued_unblocked)
                cumulative["crossings_in_slots"] += int(unique_front)
                cumulative["red_crossings"] += len(crossing) if not slot else 0
                cumulative["repeated_slots"] += int(slot and len(crossing)>1)
                cumulative["wrong_vehicle_slots"] += int(slot and queued_unblocked and crossing and not unique_front)
            sw.writerow(dict(time_begin_s=t,time_end_s=t+1,mode=mode,
                            command_rate_veh_h=feedback.rate if mode=="ALINEA" and t>=600 else "",
                            credit_before=pulse["credit_before"],credit_after=pulse["credit_after"],
                            nominal_slot_scheduled=pulse["nominal_slot_scheduled"],
                            guard_allowed=safety["allowed"],guard_reason=guard_reason,
                            guard_rejected_slot=bool(mode=="ALINEA" and t>=600 and
                                                     pulse["nominal_slot_scheduled"] and not pulse["slot_scheduled"]),
                            credit_deferred=pulse["credit_deferred"],
                            guard_vehicle_states_json=json.dumps(safety["vehicle_states"]),
                            guard_failed_follower_ids_json=json.dumps(safety["unsafe_follower_ids"]),
                            slot_scheduled=pulse["slot_scheduled"],requested_state=requested,
                            observed_state=actual,queue_vehicle_count=len(queue_ids),
                            queued_unblocked_slot=bool(mode=="ALINEA" and t>=600 and queued_unblocked),
                            front_queued_vehicle_id=front,
                            stopline_gap_m=stopline_gap,
                            downstream_rear_clearance_m=clearance,
                            required_clearance_m=required_clearance,
                            guard_decision_json=json.dumps(decision,sort_keys=True),
                            post_green_interlock_json=json.dumps(interlock,sort_keys=True),
                            internal_before_ids_json=json.dumps(sorted(before_ids)),
                            crossing_bracket_ids_json=json.dumps(crossing),
                            red_crossing_bracket_ids_json=json.dumps(crossing if mode=="ALINEA" and t>=600 and not pulse["slot_scheduled"] else []),
                            sampling_uncertain=bool(crossing),dropped_credit_total=pulse["dropped_credit_total"]))
            if mode == "ALINEA" and t >= 600 and pulse["slot_scheduled"] and interlock["abort"]:
                sf.flush(); ff.flush(); of.flush(); vf.flush(); mf.flush()
                raise RuntimeError(f"V10 post-green safe-red interlock stopped at t={t+1}: {interlock}")
            if mode == "ALINEA" and t >= 600 and (t+1-600)%120 == 0:
                classification = classify_service_window(**{k:window[k] for k in
                    ("queued_unblocked_slots","crossings_in_slots","red_crossings","repeated_slots","wrong_vehicle_slots")})
                vw.writerow(dict(window_begin_s=t+1-120,window_end_s=t+1,
                                 classification=classification,**window))
                window = dict.fromkeys(window,0)
            if (t+1)%30 == 0:
                sf.flush(); ff.flush(); of.flush(); vf.flush(); mf.flush()
        if mode == "ALINEA" and feedback.last_update != 4170:
            raise ValueError("missing terminal feedback update")
        if abs(conn.simulation.getTime()-4200)>1e-6:
            raise ValueError("incomplete horizon")
        if mode == "ALINEA":
            classification=classify_service_window(**{k:cumulative[k] for k in
                ("queued_unblocked_slots","crossings_in_slots","red_crossings",
                 "repeated_slots","wrong_vehicle_slots")})
            vw.writerow(dict(window_begin_s=600,window_end_s=4200,
                             empty_slots="ALL",classification=classification,**cumulative))
    except Exception as exc:
        print(f"SUMO_WORKER_EXCEPTION pid={proc.pid} poll_returncode={proc.poll()} "
              f"error={type(exc).__name__}: {exc}",file=sys.stderr,flush=True)
        raise
    finally:
        for stream in (sf,ff,of,vf,mf,startup_trace,probe_trace): stream.close()
        if conn is not None:
            try: conn.close(False)
            except Exception: pass
        if proc.poll() is None:
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=3)
        sumo_stdout.close(); sumo_stderr.close()
        print(f"SUMO_CHILD_EXIT pid={proc.pid} returncode={proc.returncode}",file=sys.stderr,flush=True)
        for label,path in (("sumo_stdout",output/"sumo_stdout.log"),
                           ("sumo_stderr",output/"sumo_stderr.log"),
                           ("sumo_log",output/"sumo.log"),
                           ("sumo_error_log",output/"sumo_error.log")):
            if path.exists():
                with path.open("rb") as stream:
                    stream.seek(max(0,path.stat().st_size-4096))
                    tail=stream.read().decode("utf-8","replace")
                print(f"SUMO_OUTPUT_STATUS {label} bytes={path.stat().st_size} tail={tail!r}",
                      file=sys.stderr,flush=True)
            else:
                print(f"SUMO_OUTPUT_STATUS {label} missing",file=sys.stderr,flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"SUMO exited {proc.returncode}")
    validate_output(output)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m src.reproduction.runtime_v15 RUN_CARD_JSON")
    card_path = Path(sys.argv[1]).resolve(strict=True)
    if os.environ.get("STAGE6_REPLAY_GUARD") != sha(card_path):
        raise ValueError("run this module only through reproduce_stage6.py")
    card = json.loads(card_path.read_text(encoding="utf-8"))
    if card.get("mode") != "ALINEA" or card.get("seed") not in (17, 23, 42):
        raise ValueError("unreviewed V15 replay case")
    for name, expected in card["input_sha256"].items():
        if sha(Path(card["package"]) / name) != expected:
            raise ValueError(f"prepared V15 input changed: {name}")
    global NETWORK, SUMO, SUMO_HOME
    NETWORK = Path(card["network"]).resolve(strict=True)
    SUMO = Path(card["sumo_binary"]).resolve(strict=True)
    SUMO_HOME = Path(card["sumo_home"]).resolve(strict=True)
    worker(card_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
