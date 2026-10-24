"""Explicit, exploratory FCD lane accounting for the minimal scenario.

The module intentionally measures observations, not queue storage, delay, or
causation.  Atomic lane groups are mutually exclusive; the named R path is a
separate composite so its components are never silently conflated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable, Iterable


KNOWN_CLASSES = ("M", "R", "U", "X")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def vehicle_class_from_id(vehicle_id: str) -> str:
    """Return the configured class prefix, or a surfaced ``unknown_id`` class."""
    prefix = vehicle_id.split("_", 1)[0]
    return prefix if prefix in KNOWN_CLASSES else "unknown_id"


def compiled_lane_inventory(network_path: Path) -> dict[str, dict[str, str]]:
    """Map every compiled lane ID to its edge ID and edge function."""
    root = ET.parse(network_path).getroot()
    lanes: dict[str, dict[str, str]] = {}
    for edge in root.findall("edge"):
        edge_id = edge.get("id")
        if not edge_id:
            raise ValueError("compiled network edge missing id")
        function = edge.get("function", "external")
        for lane in edge.findall("lane"):
            lane_id = lane.get("id")
            if not lane_id:
                raise ValueError(f"compiled network lane on {edge_id} missing id")
            if lane_id in lanes:
                raise ValueError(f"duplicate compiled lane id: {lane_id}")
            lanes[lane_id] = {"edge_id": edge_id, "function": function}
    if not lanes:
        raise ValueError("compiled network contains no lanes")
    return lanes


def default_lane_groups(lanes: dict[str, dict[str, str]]) -> dict[str, set[str]]:
    """Return mutually exclusive atomic groups for the compiled minimal network.

    The R path from the shared-boundary split to the ramp end is represented by
    two internal components and two named ramp lanes.  It excludes the shared
    approach, urban_in, merge-internal lane and downstream freeway lane.
    """
    atomic = {
        "upstream_and_shared_external": {"urban_in_0", "shared_approach_0"},
        "ramp_path_internal_upstream": {":urban_diverge_1_0"},
        "ramp_path_named_external": {"ramp_storage_0", "ramp_accel_0"},
        "ramp_path_internal_downstream": {":ramp_mid_0_0"},
        "merge_internal": {":freeway_merge_0_0", ":freeway_merge_1_0", ":freeway_merge_1_1"},
        "downstream_freeway_external": {"main_down_0", "main_down_1"},
    }
    used = set().union(*atomic.values())
    internal = {lane for lane, value in lanes.items() if value["function"] == "internal"}
    external = set(lanes) - internal
    atomic["other_internal"] = internal - used
    atomic["other_external"] = external - used
    return atomic


def _time_window_contains(time_s: float, time_window: tuple[float, float] | None) -> bool:
    return time_window is None or time_window[0] <= time_s < time_window[1]


def _serialise_nested(counter: dict[str, Counter]) -> dict[str, dict[str, int]]:
    return {key: dict(sorted(value.items())) for key, value in sorted(counter.items())}


def analyze_fcd_accounting(
    fcd_path: Path | str,
    network_path: Path | str,
    *,
    time_window: tuple[float, float] | None = None,
    stopped_speed_mps: float = 0.1,
    vehicle_class: Callable[[str], str] = vehicle_class_from_id,
) -> dict[str, Any]:
    """Account every FCD vehicle observation against explicit compiled lanes.

    Counts are FCD samples.  ``stopped_vehicle_seconds`` is emitted only after
    validating uniform sampling; it equals samples times the observed timestep.
    Vehicles on unknown lanes or with unrecognised IDs remain in explicit
    accounting buckets.  The function never reads tripinfo, so incomplete runs
    remain analysable as FCD-observation diagnostics.
    """
    fcd_path, network_path = Path(fcd_path), Path(network_path)
    if not fcd_path.is_file() or not network_path.is_file():
        raise FileNotFoundError("FCD and compiled network files are required")
    if not math.isfinite(stopped_speed_mps) or stopped_speed_mps < 0:
        raise ValueError("stopped_speed_mps must be finite and non-negative")
    if time_window is not None:
        if len(time_window) != 2 or not all(math.isfinite(v) for v in time_window):
            raise ValueError("time_window must be a finite (begin_s, end_s) pair")
        if time_window[1] <= time_window[0]:
            raise ValueError("time_window end must be after begin")

    lane_inventory = compiled_lane_inventory(network_path)
    groups = default_lane_groups(lane_inventory)
    membership: dict[str, list[str]] = defaultdict(list)
    for group, group_lanes in groups.items():
        for lane in group_lanes:
            membership[lane].append(group)
    overlap = {lane: names for lane, names in membership.items() if len(names) != 1}
    if overlap:
        raise ValueError(f"atomic lane groups are not exclusive: {overlap}")

    total_by_lane_class: Counter[tuple[str, str]] = Counter()
    stopped_by_lane_class: Counter[tuple[str, str]] = Counter()
    total_by_group_class: Counter[tuple[str, str]] = Counter()
    stopped_by_group_class: Counter[tuple[str, str]] = Counter()
    unknown_lane_by_class: Counter[str] = Counter()
    unknown_lane_ids: Counter[str] = Counter()
    unmatched_id_samples: Counter[str] = Counter()
    timestep_counts: list[dict[str, Any]] = []
    timestep_times: list[float] = []
    all_vehicle_samples = selected_vehicle_samples = 0
    duplicate_vehicle_time_samples = 0
    malformed_vehicles = 0

    root_seen = False
    for _, elem in ET.iterparse(fcd_path, events=("end",)):
        if elem.tag == "fcd-export":
            root_seen = True
        if elem.tag != "timestep":
            continue
        try:
            time_s = float(elem.get("time", ""))
        except ValueError as error:
            raise ValueError("FCD timestep missing numeric time") from error
        if not math.isfinite(time_s):
            raise ValueError("FCD timestep time must be finite")
        if not _time_window_contains(time_s, time_window):
            elem.clear()
            continue
        timestep_times.append(time_s)
        present_ids: set[str] = set()
        lane_instantaneous: Counter[tuple[str, str]] = Counter()
        stopped_lane_instantaneous: Counter[tuple[str, str]] = Counter()
        group_instantaneous: Counter[tuple[str, str]] = Counter()
        stopped_group_instantaneous: Counter[tuple[str, str]] = Counter()
        for vehicle in elem.findall("vehicle"):
            all_vehicle_samples += 1
            vehicle_id, lane = vehicle.get("id"), vehicle.get("lane")
            try:
                speed = float(vehicle.get("speed", ""))
            except ValueError:
                speed = float("nan")
            if not vehicle_id or not lane or not math.isfinite(speed):
                malformed_vehicles += 1
                continue
            selected_vehicle_samples += 1
            if vehicle_id in present_ids:
                duplicate_vehicle_time_samples += 1
            present_ids.add(vehicle_id)
            category = vehicle_class(vehicle_id)
            if category == "unknown_id":
                unmatched_id_samples[vehicle_id] += 1
            lane_key = lane if lane in lane_inventory else "unknown_network_lane"
            total_by_lane_class[(lane_key, category)] += 1
            lane_instantaneous[(lane_key, category)] += 1
            is_stopped = speed <= stopped_speed_mps
            if is_stopped:
                stopped_by_lane_class[(lane_key, category)] += 1
                stopped_lane_instantaneous[(lane_key, category)] += 1
            if lane_key == "unknown_network_lane":
                unknown_lane_by_class[category] += 1
                unknown_lane_ids[lane] += 1
                continue
            group = membership[lane][0]
            total_by_group_class[(group, category)] += 1
            group_instantaneous[(group, category)] += 1
            if is_stopped:
                stopped_by_group_class[(group, category)] += 1
                stopped_group_instantaneous[(group, category)] += 1
        timestep_counts.append({
            "time_s": time_s,
            "vehicle_samples": len(present_ids),
            "lane_class_counts": {f"{lane}/{category}": n for (lane, category), n in sorted(lane_instantaneous.items())},
            "stopped_lane_class_counts": {f"{lane}/{category}": n for (lane, category), n in sorted(stopped_lane_instantaneous.items())},
            "atomic_group_class_counts": {f"{group}/{category}": n for (group, category), n in sorted(group_instantaneous.items())},
            "stopped_atomic_group_class_counts": {f"{group}/{category}": n for (group, category), n in sorted(stopped_group_instantaneous.items())},
        })
        elem.clear()
    if not root_seen:
        raise ValueError("not an FCD export with a closing fcd-export element")
    if not timestep_times:
        raise ValueError("no FCD timesteps in requested window")
    if any(later <= earlier for earlier, later in zip(timestep_times, timestep_times[1:])):
        raise ValueError("FCD timestep times must be strictly increasing")
    deltas = [round(later - earlier, 12) for earlier, later in zip(timestep_times, timestep_times[1:])]
    sampling_step_s = deltas[0] if deltas else None
    uniform_sampling = bool(deltas) and all(delta == sampling_step_s for delta in deltas)
    if len(timestep_times) == 1:
        uniform_sampling = False

    def lane_rows() -> list[dict[str, Any]]:
        keys = sorted(set(total_by_lane_class) | set(stopped_by_lane_class))
        rows = []
        for lane, category in keys:
            stopped_samples = stopped_by_lane_class[(lane, category)]
            rows.append({
                "lane_id": lane,
                "vehicle_class": category,
                "vehicle_samples": total_by_lane_class[(lane, category)],
                "stopped_samples": stopped_samples,
                "stopped_vehicle_seconds": stopped_samples * sampling_step_s if uniform_sampling else None,
            })
        return rows

    def group_rows() -> list[dict[str, Any]]:
        keys = sorted(set(total_by_group_class) | set(stopped_by_group_class))
        rows = []
        for group, category in keys:
            stopped_samples = stopped_by_group_class[(group, category)]
            rows.append({
                "group": group,
                "vehicle_class": category,
                "vehicle_samples": total_by_group_class[(group, category)],
                "stopped_samples": stopped_samples,
                "stopped_vehicle_seconds": stopped_samples * sampling_step_s if uniform_sampling else None,
            })
        return rows

    ramp_path_groups = ("ramp_path_internal_upstream", "ramp_path_named_external", "ramp_path_internal_downstream")
    ramp_path_lanes = sorted(set().union(*(groups[group] for group in ramp_path_groups)))
    declared_lanes_missing_from_network = sorted(set(membership) - set(lane_inventory))
    missing_ramp_path_lanes = sorted(set(ramp_path_lanes) - set(lane_inventory))
    ramp_path_rows = []
    for category in sorted({category for _, category in total_by_lane_class} | {category for _, category in stopped_by_lane_class}):
        samples = sum(total_by_lane_class[(lane, category)] for lane in ramp_path_lanes)
        stopped = sum(stopped_by_lane_class[(lane, category)] for lane in ramp_path_lanes)
        if samples or stopped:
            ramp_path_rows.append({"vehicle_class": category, "vehicle_samples": samples,
                                   "stopped_samples": stopped,
                                   "stopped_vehicle_seconds": stopped * sampling_step_s if uniform_sampling else None})

    accounted_known_lane_samples = sum(total_by_group_class.values())
    return {
        "classification": "exploratory FCD observation accounting; not a queue-storage, delay, or causal metric",
        "sources": {"fcd_path": str(fcd_path), "fcd_sha256": sha256(fcd_path),
                    "compiled_network_path": str(network_path), "compiled_network_sha256": sha256(network_path)},
        "definitions": {"stopped": f"FCD speed <= {stopped_speed_mps} m/s", "sample": "one vehicle record in one FCD timestep",
                        "vehicle_seconds": "stopped samples × validated uniform FCD timestep", "time_window": "[begin_s, end_s)"},
        "time": {"selected_window_s": list(time_window) if time_window else None, "timestep_count": len(timestep_times),
                 "first_s": timestep_times[0], "last_s": timestep_times[-1], "sampling_step_s": sampling_step_s,
                 "uniform_sampling": uniform_sampling, "vehicle_seconds_available": uniform_sampling},
        "atomic_lane_groups": {group: sorted(group_lanes) for group, group_lanes in sorted(groups.items())},
        "ramp_path_scope": {"description": "shared-boundary split to ramp end; excludes shared_approach, urban_in, merge-internal and downstream freeway", "atomic_groups": list(ramp_path_groups), "lanes": ramp_path_lanes, "missing_declared_lanes": missing_ramp_path_lanes, "coverage_status": "complete_for_declared_lanes" if not missing_ramp_path_lanes else "not_verified_missing_declared_lanes", "composite_rows": ramp_path_rows},
        "per_lane_class": lane_rows(), "per_atomic_group_class": group_rows(), "instantaneous_by_timestep": timestep_counts,
        "accounting": {"raw_selected_vehicle_samples": selected_vehicle_samples, "known_lane_samples_in_atomic_groups": accounted_known_lane_samples,
                       "unknown_network_lane_samples": sum(unknown_lane_by_class.values()), "unknown_network_lane_by_class": dict(sorted(unknown_lane_by_class.items())),
                       "unknown_network_lane_ids": dict(sorted(unknown_lane_ids.items())), "unrecognized_vehicle_id_samples": sum(unmatched_id_samples.values()),
                       "unrecognized_vehicle_id_observations": dict(sorted(unmatched_id_samples.items())), "duplicate_vehicle_time_samples": duplicate_vehicle_time_samples,
                       "malformed_vehicle_records_not_counted": malformed_vehicles, "declared_lanes_missing_from_network": declared_lanes_missing_from_network,
                       "reconciliation": {"known_plus_unknown_equals_selected": accounted_known_lane_samples + sum(unknown_lane_by_class.values()) == selected_vehicle_samples,
                                          "atomic_groups_are_mutually_exclusive": not overlap,
                                          "all_compiled_lanes_grouped_once": set(membership) >= set(lane_inventory),
                                          "raw_vehicle_records_seen": all_vehicle_samples}},
        "limitations": ["FCD-only: no tripinfo completeness or arrival requirement.", "A stopped sample is not a unique vehicle, queue length, effective storage, delay, or causal effect.", "Composite ramp-path totals overlap their atomic component rows by design; do not sum both."],
    }


def audit_run_observation_outputs(run_root: Path | str) -> dict[str, Any]:
    """Independently audit FCD, tripinfo, E1 and TLS output semantics for one run."""
    run_root = Path(run_root)
    outputs = run_root / "outputs"
    fcd, tripinfo, tls = (outputs / name for name in ("fcd.xml", "tripinfo.xml", "tls_states.xml"))
    network = run_root / "network.net.xml"
    paths = [fcd, tripinfo, tls, network]
    e1_paths = sorted(outputs.glob("merge_*_e1_l*.xml"))
    if len(e1_paths) != 4:
        raise ValueError(f"expected four merge E1 files, found {len(e1_paths)}")
    paths.extend(e1_paths)
    if not all(path.is_file() for path in paths):
        raise FileNotFoundError("paired-run observation output is incomplete")
    fcd_ids, times, fcd_records = set(), [], 0
    for _, node in ET.iterparse(fcd, events=("end",)):
        if node.tag != "timestep":
            continue
        times.append(float(node.get("time", "")))
        for vehicle in node.findall("vehicle"):
            fcd_ids.add(vehicle.get("id", ""))
            fcd_records += 1
        node.clear()
    trip_ids = {node.get("id", "") for node in ET.parse(tripinfo).getroot().findall("tripinfo")}
    tls_times = [float(node.get("time", "")) for node in ET.parse(tls).getroot().findall("tlsState")]
    def uniform(values: list[float]) -> float | None:
        if len(values) < 2:
            return None
        deltas = [round(b - a, 12) for a, b in zip(values, values[1:])]
        return deltas[0] if all(delta == deltas[0] for delta in deltas) else None
    e1 = {}
    for path in e1_paths:
        intervals = ET.parse(path).getroot().findall("interval")
        begins = [float(node.get("begin", "")) for node in intervals]
        ends = [float(node.get("end", "")) for node in intervals]
        e1[path.name] = {"interval_count": len(intervals), "interval_step_s": uniform(begins),
                         "contiguous": all(a == b for a, b in zip(ends, begins[1:])),
                         "fields_present": all(all(key in node.attrib for key in ("nVehContrib", "nVehEntered", "speed")) for node in intervals)}
    return {"run_root": str(run_root), "source_sha256": {str(path): sha256(path) for path in paths},
            "fcd": {"timestep_count": len(times), "first_s": times[0] if times else None, "last_s": times[-1] if times else None,
                    "sampling_step_s": uniform(times), "vehicle_record_count": fcd_records, "unique_vehicle_ids": len(fcd_ids)},
            "tripinfo": {"unique_vehicle_ids": len(trip_ids)},
            "tls": {"state_record_count": len(tls_times), "sampling_step_s": uniform(tls_times)}, "e1": e1,
            "semantic_checks": {"fcd_ids_equal_tripinfo_ids": fcd_ids == trip_ids,
                                "fcd_times_strictly_increasing": all(b > a for a, b in zip(times, times[1:])),
                                "tls_times_strictly_increasing": all(b > a for a, b in zip(tls_times, tls_times[1:])),
                                "all_four_e1_have_required_fields": all(item["fields_present"] for item in e1.values()),
                                "all_four_e1_are_contiguous": all(item["contiguous"] for item in e1.values())}}


def _canonical_element_bytes(element: ET.Element) -> bytes:
    """Canonicalise one record, retaining every XML tag and actual attribute."""
    payload = {"tag": element.tag, "attributes": sorted(element.attrib.items()),
               "text": (element.text or "").strip(),
               "children": [_canonical_element_bytes(child).decode("utf-8") for child in element]}
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def compare_xml_record_semantics(reference_path: Path | str, candidate_path: Path | str, record_tag: str) -> dict[str, Any]:
    """Compare ordered XML records after excluding only comments/root metadata.

    This intentionally retains all attributes and nested children of the record:
    for example, every FCD timestep/vehicle attribute and tripinfo child is in
    the digest.  Root comments and generator/configuration headers are excluded.
    """
    def records(path: Path) -> Iterable[tuple[str, str]]:
        for _, element in ET.iterparse(path, events=("end",)):
            if element.tag == record_tag:
                canonical = _canonical_element_bytes(element)
                yield hashlib.sha256(canonical).hexdigest(), canonical.decode("utf-8")
                element.clear()
    reference_path, candidate_path = Path(reference_path), Path(candidate_path)
    differing, reference_count, candidate_count, samples = 0, 0, 0, []
    sentinel = object()
    for index, pair in enumerate(zip_longest(records(reference_path), records(candidate_path), fillvalue=sentinel)):
        reference, candidate = pair
        if reference is not sentinel:
            reference_count += 1
        if candidate is not sentinel:
            candidate_count += 1
        if reference is sentinel or candidate is sentinel or reference[0] != candidate[0]:
            differing += 1
            if len(samples) < 3:
                samples.append({"record_index": index,
                                "reference_digest": None if reference is sentinel else reference[0],
                                "candidate_digest": None if candidate is sentinel else candidate[0]})
    return {"reference_path": str(reference_path), "candidate_path": str(candidate_path),
            "record_tag": record_tag, "reference_sha256": sha256(reference_path),
            "candidate_sha256": sha256(candidate_path), "reference_record_count": reference_count,
            "candidate_record_count": candidate_count, "different_record_count": differing,
            "equal": differing == 0 and reference_count == candidate_count,
            "difference_samples": samples}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fcd", required=True, type=Path)
    parser.add_argument("--network", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stopped-speed-mps", type=float, default=0.1)
    parser.add_argument("--begin-s", type=float)
    parser.add_argument("--end-s", type=float)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    if (args.begin_s is None) != (args.end_s is None):
        raise ValueError("--begin-s and --end-s must be provided together")
    result = analyze_fcd_accounting(args.fcd, args.network, time_window=(args.begin_s, args.end_s) if args.begin_s is not None else None, stopped_speed_mps=args.stopped_speed_mps)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2)
    print(args.output)


if __name__ == "__main__":
    main()
