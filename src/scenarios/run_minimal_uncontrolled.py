"""Build and run the project-owned minimal uncontrolled SUMO scenario.

Every value in this module is a technical placeholder for smoke/stress
testing. The runs are exploratory technical checks, not formal experiments
or thesis evidence.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import xml.etree.ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.analysis.internal_lane_accounting import analyze_fcd_accounting


SCENARIO_ROOT = PROJECT_ROOT / "config" / "scenarios" / "minimal_uncontrolled"
DEFAULT_SUMO_HOME = Path(
    "/Library/Frameworks/EclipseSUMO.framework/Versions/1.26.0/"
    "EclipseSUMO/share/sumo"
)
SOURCE_FILES = (
    "scenario.nod.xml",
    "scenario.edg.xml",
    "scenario.con.xml",
    "scenario.tll.xml",
    "scenario.add.xml",
    "scenario.sumocfg",
)
EXPECTED_ROUTES = {
    "M": ("main_up", "main_down"),
    "R": (
        "urban_in",
        "shared_approach",
        "ramp_storage",
        "ramp_accel",
        "main_down",
    ),
    "U": ("urban_in", "shared_approach", "urban_out"),
    "X": ("cross_in", "cross_out"),
}
VEHICLE_CLASSES = tuple(EXPECTED_ROUTES)
HALTING_SPEED_MPS = 0.1
TECHNICAL_Q_URBAN_VEHPH = 360.0
TECHNICAL_Q_CROSS_VEHPH = 180.0
TECHNICAL_MAX_END_IN_NETWORK_FRACTION = 0.15
TECHNICAL_QUEUE_TREND_WINDOW_S = 60
LEGACY_MERGE_E1_DETECTORS = {
    "merge_upstream_e1_l0",
    "merge_upstream_e1_l1",
    "merge_downstream_e1_l0",
    "merge_downstream_e1_l1",
}
MAINLINE_MERGE_ENTRY_E1 = {
    "mainline_merge_entry_e1_l0": {
        "lane_id": ":freeway_merge_1_0",
        "position_m": 4.32,
        "period_s": 30,
        "from_edge": "main_up",
        "to_edge": "main_down",
        "from_lane": "0",
        "to_lane": "0",
    },
    "mainline_merge_entry_e1_l1": {
        "lane_id": ":freeway_merge_1_1",
        "position_m": 4.32,
        "period_s": 30,
        "from_edge": "main_up",
        "to_edge": "main_down",
        "from_lane": "1",
        "to_lane": "1",
    },
}
REQUIRED_OBSERVATION_DETECTORS = {
    *LEGACY_MERGE_E1_DETECTORS,
    *MAINLINE_MERGE_ENTRY_E1,
}
E2_NAMED_LANE_COVERAGE = {
    "shared_boundary_e2": "shared_approach_0",
    "ramp_storage_e2": "ramp_storage_0",
}


@dataclass(frozen=True)
class TechnicalProfile:
    """A fixed technical demand profile; never a scientific parameter set."""

    duration_s: int
    planned: dict[str, int]
    requested_demand_vehph: dict[str, float]
    purpose: str


@dataclass(frozen=True)
class RunWindows:
    """Exploratory timing windows; every value remains a placeholder."""

    warmup_end_s: int
    demand_end_s: int
    simulation_end_s: int

    @property
    def measurement_begin_s(self) -> int:
        return self.warmup_end_s

    @property
    def measurement_end_s(self) -> int:
        return self.demand_end_s

    @property
    def clearance_duration_s(self) -> int:
        return self.simulation_end_s - self.demand_end_s

    def definitions(self) -> dict[str, dict[str, int]]:
        windows = {
            "warmup": (0, self.warmup_end_s),
            "demand": (0, self.demand_end_s),
            "measurement": (self.measurement_begin_s, self.measurement_end_s),
            "clearance": (self.demand_end_s, self.simulation_end_s),
            "simulation": (0, self.simulation_end_s),
        }
        return {
            name: {
                "begin_s": begin,
                "end_s": end,
                "duration_s": end - begin,
            }
            for name, (begin, end) in windows.items()
        }


PROFILES = {
    "low": TechnicalProfile(
        duration_s=600,
        planned={"M": 180, "R": 60, "U": 60, "X": 30},
        requested_demand_vehph={"M": 1080.0, "R": 360.0, "U": 360.0, "X": 180.0},
        purpose="low-load functional smoke test",
    ),
    "stress": TechnicalProfile(
        duration_s=900,
        planned={"M": 800, "R": 300, "U": 90, "X": 45},
        requested_demand_vehph={"M": 3200.0, "R": 1200.0, "U": 360.0, "X": 180.0},
        purpose="queue-propagation technical stress test",
    ),
}


def nonnegative_finite_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0 or not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a non-negative finite number")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def planned_count(rate_vehph: float, duration_s: int) -> int:
    return int(round(rate_vehph * duration_s / 3600.0))


def make_custom_profile(
    base: TechnicalProfile,
    q_main_vehph: float,
    q_ramp_vehph: float,
) -> TechnicalProfile:
    rates = {
        "M": q_main_vehph,
        "R": q_ramp_vehph,
        "U": TECHNICAL_Q_URBAN_VEHPH,
        "X": TECHNICAL_Q_CROSS_VEHPH,
    }
    return TechnicalProfile(
        duration_s=base.duration_s,
        planned={
            name: planned_count(rate, base.duration_s)
            for name, rate in rates.items()
        },
        requested_demand_vehph=rates,
        purpose=(
            "custom qMain/qRamp technical test with fixed technical qUrban/X; "
            "not a demand-grid point"
        ),
    )


def with_demand_end(profile: TechnicalProfile, demand_end_s: int) -> TechnicalProfile:
    return TechnicalProfile(
        duration_s=demand_end_s,
        planned={
            name: planned_count(rate, demand_end_s)
            for name, rate in profile.requested_demand_vehph.items()
        },
        requested_demand_vehph=profile.requested_demand_vehph,
        purpose=profile.purpose,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build or run the minimal uncontrolled technical SUMO scenario; "
            "outputs go only to /private/tmp."
        )
    )
    parser.add_argument(
        "--profile",
        choices=tuple(PROFILES),
        default="low",
        help="Fixed technical profile (default: low).",
    )
    parser.add_argument(
        "--q-main",
        type=nonnegative_finite_float,
        help=(
            "Custom technical qMain request in veh/h. Must be supplied together "
            "with --q-ramp; uses the selected profile duration."
        ),
    )
    parser.add_argument(
        "--q-ramp",
        type=nonnegative_finite_float,
        help=(
            "Custom technical qRamp request in veh/h. Must be supplied together "
            "with --q-main; qUrban/X remain fixed placeholders."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=17,
        help="Technical placeholder SUMO seed (default: 17).",
    )
    parser.add_argument(
        "--warmup-s",
        type=nonnegative_int,
        default=0,
        help=(
            "Exploratory warm-up duration in seconds (default: 0, preserving "
            "the Stage 1 behavior). Demand is active during warm-up."
        ),
    )
    parser.add_argument(
        "--measurement-duration-s",
        type=positive_int,
        help=(
            "Exploratory measurement duration in seconds. If omitted, the "
            "measurement window ends at --demand-end-s or the profile default."
        ),
    )
    parser.add_argument(
        "--demand-end-s",
        type=positive_int,
        help=(
            "Exploratory demand end time in seconds. When supplied together "
            "with --measurement-duration-s it must equal warm-up plus measurement."
        ),
    )
    parser.add_argument(
        "--post-demand-clearance-s",
        type=nonnegative_int,
        default=0,
        help=(
            "Exploratory post-demand clearance duration in seconds (default: 0, "
            "preserving the Stage 1 behavior)."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate sources and build the network without running traffic.",
    )
    parser.add_argument(
        "--reanalyze-summary",
        type=Path,
        help=(
            "Read an existing technical run summary and its runtime outputs, "
            "then write a corrected post-run analysis to a new /private/tmp "
            "directory without rerunning SUMO or overwriting the source."
        ),
    )
    parser.add_argument("--sumo-binary", help="Explicit sumo executable.")
    parser.add_argument("--netconvert-binary", help="Explicit netconvert executable.")
    return parser.parse_args()


def resolve_profile(args: argparse.Namespace) -> tuple[str, TechnicalProfile]:
    custom_values = (args.q_main, args.q_ramp)
    if (args.q_main is None) != (args.q_ramp is None):
        raise ValueError("--q-main and --q-ramp must be supplied together")
    if args.q_main is None:
        return args.profile, PROFILES[args.profile]
    return "custom", make_custom_profile(
        PROFILES[args.profile],
        q_main_vehph=args.q_main,
        q_ramp_vehph=args.q_ramp,
    )


def resolve_run_windows(
    args: argparse.Namespace, profile: TechnicalProfile
) -> tuple[TechnicalProfile, RunWindows]:
    if args.measurement_duration_s is not None:
        derived_demand_end = args.warmup_s + args.measurement_duration_s
        if (
            args.demand_end_s is not None
            and args.demand_end_s != derived_demand_end
        ):
            raise ValueError(
                "--demand-end-s must equal --warmup-s plus "
                "--measurement-duration-s"
            )
        demand_end_s = derived_demand_end
    else:
        demand_end_s = args.demand_end_s or profile.duration_s
    if args.warmup_s >= demand_end_s:
        raise ValueError("--warmup-s must be smaller than the demand end")
    windows = RunWindows(
        warmup_end_s=args.warmup_s,
        demand_end_s=demand_end_s,
        simulation_end_s=demand_end_s + args.post_demand_clearance_s,
    )
    return with_demand_end(profile, demand_end_s), windows


def resolve_sumo_home() -> Path:
    candidates: list[Path] = []
    configured = os.environ.get("SUMO_HOME")
    if configured:
        configured_path = Path(configured).expanduser()
        candidates.extend((configured_path, configured_path / "share" / "sumo"))
    candidates.append(DEFAULT_SUMO_HOME)

    for candidate in candidates:
        if (candidate / "bin").is_dir() and (candidate / "tools").is_dir():
            resolved = candidate.resolve()
            os.environ["SUMO_HOME"] = str(resolved)
            bin_path = str(resolved / "bin")
            path_parts = os.environ.get("PATH", "").split(os.pathsep)
            if bin_path not in path_parts:
                os.environ["PATH"] = os.pathsep.join((bin_path, *path_parts))
            return resolved
    raise RuntimeError(f"No usable SUMO_HOME found; checked: {candidates}")


def resolve_binary(explicit: str | None, default_name: str) -> Path:
    requested = explicit or default_name
    located = shutil.which(requested)
    if located is None:
        path = Path(requested).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            located = str(path)
    if located is None:
        raise FileNotFoundError(f"Executable not found: {requested}")
    return Path(located).resolve()


def xml_root(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def static_validate_sources() -> dict[str, object]:
    missing = [name for name in SOURCE_FILES if not (SCENARIO_ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing scenario source files: {missing}")

    nodes = {node.get("id"): node for node in xml_root(SCENARIO_ROOT / "scenario.nod.xml")}
    required_nodes = {
        "main_in",
        "freeway_merge",
        "main_out",
        "urban_in",
        "urban_tls",
        "urban_diverge",
        "urban_out",
        "cross_in",
        "cross_out",
        "ramp_mid",
    }
    if not required_nodes.issubset(nodes):
        raise RuntimeError(f"Missing nodes: {sorted(required_nodes.difference(nodes))}")
    if nodes["urban_tls"].get("type") != "traffic_light":
        raise RuntimeError("urban_tls is not configured as a traffic light")

    edges = {edge.get("id"): edge for edge in xml_root(SCENARIO_ROOT / "scenario.edg.xml")}
    route_edges = {edge for route in EXPECTED_ROUTES.values() for edge in route}
    if not route_edges.issubset(edges):
        raise RuntimeError(f"Missing route edges: {sorted(route_edges.difference(edges))}")
    if edges["main_up"].get("numLanes") != "2" or edges["main_down"].get("numLanes") != "2":
        raise RuntimeError("Technical freeway must have two lanes upstream and downstream")

    connections = {
        (item.get("from"), item.get("to"))
        for item in xml_root(SCENARIO_ROOT / "scenario.con.xml")
    }
    required_connections = {
        pair
        for route in EXPECTED_ROUTES.values()
        for pair in zip(route, route[1:])
    }
    if not required_connections.issubset(connections):
        raise RuntimeError(
            "Missing route connections: "
            f"{sorted(required_connections.difference(connections))}"
        )

    tls = xml_root(SCENARIO_ROOT / "scenario.tll.xml").find("./tlLogic")
    if tls is None or tls.get("id") != "urban_tls" or tls.get("type") != "static":
        raise RuntimeError("Missing fixed-time urban_tls program")
    phases = [phase.get("state", "") for phase in tls.findall("phase")]
    if len(phases) < 4 or not any("y" in state for state in phases):
        raise RuntimeError(f"Traffic-light phases lack explicit yellow clearance: {phases}")

    detectors = {
        detector.get("id"): detector
        for detector in xml_root(SCENARIO_ROOT / "scenario.add.xml")
    }
    required_detectors = {
        *E2_NAMED_LANE_COVERAGE,
        *REQUIRED_OBSERVATION_DETECTORS,
    }
    if not required_detectors.issubset(detectors):
        raise RuntimeError(
            f"Missing detectors: {sorted(required_detectors.difference(detectors))}"
        )
    for detector_id, lane_id in E2_NAMED_LANE_COVERAGE.items():
        detector = detectors[detector_id]
        if detector.get("lane") != lane_id or detector.get("pos") != "0":
            raise RuntimeError(
                f"Unexpected E2 anchor for {detector_id}: {detector.attrib}"
            )
        if detector.get("length") is not None or detector.get("endPos") != "-0.1":
            raise RuntimeError(
                "E2 source fallback must use endPos=-0.1 without length: "
                f"{detector_id}"
            )
    for detector_id, expected in MAINLINE_MERGE_ENTRY_E1.items():
        detector = detectors[detector_id]
        observed = {
            "lane_id": detector.get("lane"),
            "position_m": float(detector.get("pos", "nan")),
            "period_s": int(detector.get("period", "-1")),
        }
        for field in ("lane_id", "position_m", "period_s"):
            if observed[field] != expected[field]:
                raise RuntimeError(
                    f"Unexpected mainline merge-entry E1 {field} for "
                    f"{detector_id}: {observed[field]!r}"
                )

    config = xml_root(SCENARIO_ROOT / "scenario.sumocfg")
    inputs = {
        child.tag: child.get("value")
        for child in config.findall("./input/*")
    }
    expected_inputs = {
        "net-file": "network.net.xml",
        "route-files": "demand.rou.xml",
        "additional-files": "scenario.add.xml",
    }
    if inputs != expected_inputs:
        raise RuntimeError(f"Unexpected scenario config inputs: {inputs}")

    return {
        "required_nodes": sorted(required_nodes),
        "route_edges": sorted(route_edges),
        "required_connections": [list(pair) for pair in sorted(required_connections)],
        "traffic_light": {
            "id": "urban_tls",
            "type": "static",
            "phase_states": phases,
        },
        "finite_storage_boundary": {
            "node": "urban_diverge",
            "ramp_edge": "ramp_storage",
            "shared_edge": "shared_approach",
            "technical_crossing_observable": (
                "stopped R vehicle on shared_approach"
            ),
        },
        "detectors": sorted(required_detectors),
        "e2_source_fallback": {
            "status": (
                "source endPos=-0.1 stays on its named lane; runtime coverage "
                "is rewritten from compiled lane lengths after netconvert"
            ),
            "detectors": dict(E2_NAMED_LANE_COVERAGE),
        },
        "merge_observation_detectors": sorted(REQUIRED_OBSERVATION_DETECTORS),
        "mainline_merge_entry_e1_mapping": MAINLINE_MERGE_ENTRY_E1,
        "merge_upstream_semantics": "origin_insertion_contaminated",
        "merge_observation_status": (
            "positions and 30 s aggregation are technical placeholders"
        ),
        "config_inputs": inputs,
    }


def prepare_runtime() -> Path:
    run_root = Path(
        tempfile.mkdtemp(prefix="minimal_uncontrolled_", dir="/private/tmp")
    )
    for name in SOURCE_FILES:
        shutil.copy2(SCENARIO_ROOT / name, run_root / name)
    output_dir = run_root / "outputs"
    output_dir.mkdir()

    detector_tree = ET.parse(run_root / "scenario.add.xml")
    for element in detector_tree.getroot():
        if element.tag == "timedEvent":
            element.set("dest", str(output_dir / "tls_states.xml"))
            continue
        detector_id = element.get("id")
        if not detector_id:
            raise RuntimeError("Detector without id")
        element.set("file", str(output_dir / f"{detector_id}.xml"))
    detector_tree.write(
        run_root / "scenario.add.xml", encoding="UTF-8", xml_declaration=True
    )
    return run_root


def run_checked(command: list[str], stdout_path: Path, stderr_path: Path) -> None:
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed with exit {completed.returncode}: {command}; "
            f"stderr: {stderr_path}"
        )


def build_network(netconvert: Path, run_root: Path) -> tuple[list[str], dict[str, object]]:
    command = [
        str(netconvert),
        "--node-files",
        str(run_root / "scenario.nod.xml"),
        "--edge-files",
        str(run_root / "scenario.edg.xml"),
        "--connection-files",
        str(run_root / "scenario.con.xml"),
        "--tllogic-files",
        str(run_root / "scenario.tll.xml"),
        "--output-file",
        str(run_root / "network.net.xml"),
        "--no-turnarounds",
        "true",
        "--junctions.corner-detail",
        "5",
    ]
    run_checked(
        command,
        run_root / "netconvert.stdout.log",
        run_root / "netconvert.stderr.log",
    )
    netconvert_diagnostics = diagnostic_summary(
        [run_root / "netconvert.stdout.log", run_root / "netconvert.stderr.log"]
    )

    root = xml_root(run_root / "network.net.xml")
    ordinary_edges = {
        edge.get("id") for edge in root.findall("./edge") if edge.get("function") is None
    }
    expected_edges = {edge for route in EXPECTED_ROUTES.values() for edge in route}
    if not expected_edges.issubset(ordinary_edges):
        raise RuntimeError(
            f"Built network is missing edges: {sorted(expected_edges.difference(ordinary_edges))}"
        )
    tls = root.find("./tlLogic[@id='urban_tls']")
    if tls is None:
        raise RuntimeError("Built network is missing urban_tls logic")
    phases = [phase.get("state") for phase in tls.findall("phase")]
    if phases != ["Gr", "yr", "rG", "ry"]:
        raise RuntimeError(f"Built network changed the fixed-time phases: {phases}")
    e2_coverage = configure_e2_named_lane_coverage(run_root)
    mainline_merge_entry_e1 = validate_mainline_merge_entry_e1(run_root)
    return command, {
        "network_file": str(run_root / "network.net.xml"),
        "ordinary_edges": sorted(ordinary_edges),
        "urban_tls_phase_states": phases,
        "e2_named_lane_coverage": e2_coverage,
        "mainline_merge_entry_e1": mainline_merge_entry_e1,
        "netconvert_diagnostics": netconvert_diagnostics,
        "technical_expectations": {
            "no_netconvert_warnings": (
                netconvert_diagnostics["warning_line_count"] == 0
            ),
            "no_netconvert_errors": (
                netconvert_diagnostics["error_line_count"] == 0
            ),
        },
    }


def validate_mainline_merge_entry_e1(
    run_root: Path,
) -> dict[str, dict[str, object]]:
    """Verify runtime E1 paths and their compiled mainline-only connections."""

    network_root = xml_root(run_root / "network.net.xml")
    additional_root = xml_root(run_root / "scenario.add.xml")
    verified: dict[str, dict[str, object]] = {}
    for detector_id, expected in MAINLINE_MERGE_ENTRY_E1.items():
        lane_id = str(expected["lane_id"])
        lane = network_root.find(
            f"./edge[@function='internal']/lane[@id='{lane_id}']"
        )
        if lane is None:
            raise RuntimeError(
                f"Compiled network is missing internal E1 lane: {lane_id}"
            )
        lane_length_m = float(lane.get("length", "-1"))
        position_m = float(expected["position_m"])
        if not 0 <= position_m <= lane_length_m:
            raise RuntimeError(
                f"E1 position {position_m} is outside {lane_id} length "
                f"{lane_length_m}"
            )
        connections = [
            item
            for item in network_root.findall("./connection")
            if item.get("via") == lane_id
        ]
        if len(connections) != 1:
            raise RuntimeError(
                f"Expected one compiled connection via {lane_id}, got "
                f"{len(connections)}"
            )
        expected_connection = {
            "from_edge": str(expected["from_edge"]),
            "to_edge": str(expected["to_edge"]),
            "from_lane": str(expected["from_lane"]),
            "to_lane": str(expected["to_lane"]),
        }
        connection = connections[0]
        observed_connection = {
            "from_edge": connection.get("from"),
            "to_edge": connection.get("to"),
            "from_lane": connection.get("fromLane"),
            "to_lane": connection.get("toLane"),
        }
        if observed_connection != expected_connection or connection.get("state") != "M":
            raise RuntimeError(
                f"Unexpected compiled connection via {lane_id}: "
                f"{connection.attrib}"
            )
        detector = additional_root.find(
            f"./inductionLoop[@id='{detector_id}']"
        )
        expected_output = run_root / "outputs" / f"{detector_id}.xml"
        if detector is None:
            raise RuntimeError(f"Runtime additional is missing E1: {detector_id}")
        if (
            detector.get("lane") != lane_id
            or float(detector.get("pos", "nan")) != position_m
            or int(detector.get("period", "-1")) != expected["period_s"]
            or Path(detector.get("file", "")) != expected_output
        ):
            raise RuntimeError(
                f"Unexpected runtime mainline merge-entry E1: {detector.attrib}"
            )
        verified[detector_id] = {
            "lane_id": lane_id,
            "position_m": position_m,
            "compiled_lane_length_m": lane_length_m,
            "period_s": expected["period_s"],
            "output_path": str(expected_output),
            "compiled_connection": observed_connection,
            "compiled_connection_state": connection.get("state"),
            "vehicle_class_boundary": (
                "mainline route M only by declared and compiled topology; "
                "per-vehicle detector identity is not available"
            ),
        }
    return verified


def configure_e2_named_lane_coverage(run_root: Path) -> dict[str, dict[str, object]]:
    """Set runtime E2 endpoints to exact compiled named-lane lengths.

    Source additional XML uses a non-extending ``endPos=-0.1`` fallback because
    compiled lane lengths do not exist until netconvert completes. This writes
    the runtime copy only; it neither changes source configuration nor extends
    an E2 across an internal junction lane.
    """

    network_root = xml_root(run_root / "network.net.xml")
    lane_lengths: dict[str, float] = {}
    for lane_id in E2_NAMED_LANE_COVERAGE.values():
        lane = network_root.find(f".//lane[@id='{lane_id}']")
        if lane is None:
            raise RuntimeError(f"Compiled network is missing E2 lane: {lane_id}")
        lane_lengths[lane_id] = float(lane.get("length", "-1"))
        if lane_lengths[lane_id] <= 0:
            raise RuntimeError(f"Invalid compiled E2 lane length: {lane_id}")

    additional_path = run_root / "scenario.add.xml"
    tree = ET.parse(additional_path)
    detectors = {
        detector.get("id"): detector
        for detector in tree.getroot().findall("laneAreaDetector")
    }
    coverage: dict[str, dict[str, object]] = {}
    for detector_id, lane_id in E2_NAMED_LANE_COVERAGE.items():
        detector = detectors.get(detector_id)
        if detector is None:
            raise RuntimeError(f"Runtime additional is missing E2: {detector_id}")
        detector.set("lane", lane_id)
        detector.set("pos", "0")
        detector.attrib.pop("length", None)
        detector.set("endPos", format(lane_lengths[lane_id], ".12g"))
        coverage[detector_id] = {
            "lane_id": lane_id,
            "begin_pos_m": 0.0,
            "end_pos_m": lane_lengths[lane_id],
            "length_m": lane_lengths[lane_id],
            "coverage": "exact compiled named lane only; no internal successor",
        }
    tree.write(additional_path, encoding="UTF-8", xml_declaration=True)
    return coverage


def write_demand(run_root: Path, profile: TechnicalProfile) -> Path:
    routes = ET.Element(
        "routes",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xsi:noNamespaceSchemaLocation": "https://sumo.dlr.de/xsd/routes_file.xsd",
        },
    )
    # No car-following parameters are overridden: SUMO's default type is used.
    ET.SubElement(routes, "vType", {"id": "technical_passenger", "vClass": "passenger"})
    for vehicle_class, edges in EXPECTED_ROUTES.items():
        ET.SubElement(
            routes,
            "route",
            {"id": f"{vehicle_class}_route", "edges": " ".join(edges)},
        )
    for vehicle_class in VEHICLE_CLASSES:
        ET.SubElement(
            routes,
            "flow",
            {
                "id": f"{vehicle_class}_flow",
                "type": "technical_passenger",
                "route": f"{vehicle_class}_route",
                "begin": "0",
                "end": str(profile.duration_s),
                "number": str(profile.planned[vehicle_class]),
                "departPos": "last",
                "departLane": "best",
                "departSpeed": "max",
            },
        )
    ET.indent(routes)
    path = run_root / "demand.rou.xml"
    ET.ElementTree(routes).write(path, encoding="UTF-8", xml_declaration=True)
    return path


def validate_runtime_inputs(run_root: Path, profile: TechnicalProfile) -> dict[str, object]:
    demand_root = xml_root(run_root / "demand.rou.xml")
    flows = {flow.get("id"): flow for flow in demand_root.findall("flow")}
    configured_routes = {
        vehicle_class: tuple(
            demand_root.find(f"./route[@id='{vehicle_class}_route']").get(
                "edges", ""
            ).split()
        )
        for vehicle_class in VEHICLE_CLASSES
    }
    missing_or_changed_routes = {
        vehicle_class: list(edges)
        for vehicle_class, edges in configured_routes.items()
        if not edges or edges != EXPECTED_ROUTES[vehicle_class]
    }
    if missing_or_changed_routes:
        raise RuntimeError(
            f"Generated configured routes are missing or changed: {missing_or_changed_routes}"
        )
    realized_plan: dict[str, int] = {}
    for vehicle_class in VEHICLE_CLASSES:
        flow = flows.get(f"{vehicle_class}_flow")
        if flow is None:
            raise RuntimeError(f"Missing generated {vehicle_class} flow")
        settings = {
            "departPos": flow.get("departPos"),
            "departLane": flow.get("departLane"),
            "departSpeed": flow.get("departSpeed"),
        }
        if settings != {
            "departPos": "last",
            "departLane": "best",
            "departSpeed": "max",
        }:
            raise RuntimeError(f"Unexpected insertion settings: {settings}")
        realized_plan[vehicle_class] = int(flow.get("number", "0"))
    if realized_plan != profile.planned:
        raise RuntimeError(
            f"Generated planned counts differ: {realized_plan} != {profile.planned}"
        )
    vtype = demand_root.find("./vType[@id='technical_passenger']")
    forbidden_overrides = {
        "carFollowModel",
        "tau",
        "sigma",
        "accel",
        "decel",
        "minGap",
    }
    present = forbidden_overrides.intersection(vtype.attrib if vtype is not None else {})
    if present:
        raise RuntimeError(f"Unexpected car-following overrides: {sorted(present)}")
    return {
        "planned_counts": realized_plan,
        "requested_demand_vehph": profile.requested_demand_vehph,
        "declared_routes_from_generated_demand": {
            name: list(edges) for name, edges in configured_routes.items()
        },
        "insertion_settings": {
            "departPos": "last",
            "departLane": "best",
            "departSpeed": "max",
        },
        "car_following": "SUMO default type; no parameters overridden",
    }


def vehicle_class(vehicle_id: str) -> str:
    prefix = vehicle_id.split("_flow.", 1)[0]
    if prefix not in VEHICLE_CLASSES:
        raise RuntimeError(f"Unexpected vehicle id/class: {vehicle_id}")
    return prefix


def summarize_numeric_values(values: list[float]) -> dict[str, float | int | None]:
    """Return descriptive values without applying an acceptance threshold."""

    return {
        "count": len(values),
        "minimum_s": min(values) if values else None,
        "mean_s": sum(values) / len(values) if values else None,
        "maximum_s": max(values) if values else None,
    }


def analyze_departure_realization(
    tripinfo_path: Path,
    profile: TechnicalProfile,
    windows: RunWindows,
) -> dict[str, object]:
    """Describe planned and actual insertion timing from an existing run.

    This function is side-effect free and can be imported by an independent
    data review. It deliberately does not decide an acceptable departDelay.
    """

    window_definitions = windows.definitions()
    records: dict[str, dict[str, float | str]] = {}
    records_by_class: dict[str, list[dict[str, float | str]]] = {
        name: [] for name in VEHICLE_CLASSES
    }
    for item in xml_root(tripinfo_path).findall(".//tripinfo"):
        vehicle_id = item.get("id", "")
        group = vehicle_class(vehicle_id)
        depart_s = float(item.get("depart", "-1"))
        if depart_s < 0:
            continue
        record: dict[str, float | str] = {
            "id": vehicle_id,
            "class": group,
            "depart_s": depart_s,
            "depart_delay_s": float(item.get("departDelay", "0")),
        }
        records[vehicle_id] = record
        records_by_class[group].append(record)

    planned_schedule: dict[str, list[tuple[str, float]]] = {}
    for group in VEHICLE_CLASSES:
        count = profile.planned[group]
        planned_schedule[group] = [
            (
                f"{group}_flow.{index}",
                profile.duration_s * index / count,
            )
            for index in range(count)
        ] if count else []

    summary_windows = {
        name: window_definitions[name]
        for name in ("warmup", "measurement", "clearance", "demand", "simulation")
    }
    by_window_and_class: dict[str, dict[str, object]] = {}
    for window_name, window in summary_windows.items():
        begin_s = window["begin_s"]
        end_s = window["end_s"]
        by_window_and_class[window_name] = {}
        for group in VEHICLE_CLASSES:
            planned_in_window = [
                vehicle_id
                for vehicle_id, scheduled_s in planned_schedule[group]
                if begin_s <= scheduled_s < end_s
            ]
            actual_in_window = [
                record
                for record in records_by_class[group]
                if begin_s <= float(record["depart_s"]) < end_s
            ]
            delays = [
                float(record["depart_delay_s"])
                for record in actual_in_window
            ]
            by_window_and_class[window_name][group] = {
                "planned_departure_slots": len(planned_in_window),
                "actual_departures": len(actual_in_window),
                "actual_minus_planned_slots": (
                    len(actual_in_window) - len(planned_in_window)
                ),
                "depart_delay_for_actual_departures": summarize_numeric_values(
                    delays
                ),
            }

    waiting_at_demand_end: dict[str, int] = {}
    clearance_late_departures: dict[str, int] = {}
    not_departed_by_simulation_end: dict[str, int] = {}
    final_departed: dict[str, int] = {}
    reported_schedule_discrepancy_s: dict[str, dict[str, float | int | None]] = {}
    for group in VEHICLE_CLASSES:
        waiting_ids = []
        clearance_ids = []
        missing_ids = []
        schedule_discrepancies = []
        for vehicle_id, nominal_scheduled_s in planned_schedule[group]:
            record = records.get(vehicle_id)
            if record is None:
                missing_ids.append(vehicle_id)
                waiting_ids.append(vehicle_id)
                continue
            depart_s = float(record["depart_s"])
            depart_delay_s = float(record["depart_delay_s"])
            reported_scheduled_s = depart_s - depart_delay_s
            schedule_discrepancies.append(
                abs(reported_scheduled_s - nominal_scheduled_s)
            )
            if depart_s >= windows.demand_end_s:
                waiting_ids.append(vehicle_id)
            if windows.demand_end_s <= depart_s < windows.simulation_end_s:
                clearance_ids.append(vehicle_id)
        waiting_at_demand_end[group] = len(waiting_ids)
        clearance_late_departures[group] = len(clearance_ids)
        not_departed_by_simulation_end[group] = len(missing_ids)
        final_departed[group] = len(records_by_class[group])
        reported_schedule_discrepancy_s[group] = summarize_numeric_values(
            schedule_discrepancies
        )

    final_insertion_rate = {
        group: (
            final_departed[group] / profile.planned[group]
            if profile.planned[group]
            else 1.0
        )
        for group in VEHICLE_CLASSES
    }
    demand_period_departed = {
        group: by_window_and_class["demand"][group]["actual_departures"]
        for group in VEHICLE_CLASSES
    }
    demand_period_realization_rate = {
        group: (
            demand_period_departed[group] / profile.planned[group]
            if profile.planned[group]
            else 1.0
        )
        for group in VEHICLE_CLASSES
    }
    all_finally_inserted = all(
        final_departed[group] == profile.planned[group]
        for group in VEHICLE_CLASSES
    )
    all_departed_by_demand_end = all(
        waiting_at_demand_end[group] == 0 for group in VEHICLE_CLASSES
    )
    return {
        "status": (
            "descriptive post-run insertion timing; no acceptable "
            "departDelay threshold is defined"
        ),
        "planned_departure_slot_note": (
            "planned slots use the nominal uniform schedule implied by each "
            "SUMO flow's begin, end, and number attributes; the reported "
            "scheduled-time discrepancy quantifies differences from "
            "tripinfo depart minus departDelay"
        ),
        "time_windows": summary_windows,
        "by_window_and_class": by_window_and_class,
        "waiting_to_insert_at_demand_end_by_class": waiting_at_demand_end,
        "clearance_late_departures_by_class": clearance_late_departures,
        "not_departed_by_simulation_end_by_class": (
            not_departed_by_simulation_end
        ),
        "final_departed_by_class": final_departed,
        "final_insertion_rate_by_class": final_insertion_rate,
        "demand_period_departed_by_class": demand_period_departed,
        "demand_period_realization_rate_by_class": (
            demand_period_realization_rate
        ),
        "all_planned_vehicles_finally_inserted": all_finally_inserted,
        "all_planned_vehicles_departed_by_demand_end": (
            all_departed_by_demand_end
        ),
        "reported_vs_nominal_scheduled_time_absolute_difference_s": (
            reported_schedule_discrepancy_s
        ),
        "demand_period_realization_eligibility": {
            "status": "not_evaluated",
            "eligible": None,
            "reason": (
                "no approved maximum departDelay or late-insertion criterion "
                "is defined; exact counts are reported instead"
            ),
        },
    }


def diagnostic_summary(paths: list[Path]) -> dict[str, object]:
    text = "\n".join(path.read_text(encoding="utf-8", errors="replace") for path in paths if path.is_file())
    lines = [line for line in text.splitlines() if line.strip()]
    warnings = [line for line in lines if "warning" in line.lower()]
    errors = [line for line in lines if re.search(r"\berror\b", line, re.IGNORECASE)]
    return {
        "warning_line_count": len(warnings),
        "error_line_count": len(errors),
        "collision_line_count": sum("collision" in line.lower() for line in lines),
        "teleport_line_count": sum("teleport" in line.lower() for line in lines),
        "emergency_braking_line_count": sum(
            "emergency braking" in line.lower() for line in lines
        ),
        "route_error_line_count": sum(
            bool(re.search(r"(no route|route.*(error|invalid|not found)|disconnected)", line, re.IGNORECASE))
            for line in lines
        ),
        "warning_samples": warnings[:30],
        "error_samples": errors[:30],
    }


def summarize_interval_records(
    records: list[dict[str, float | int]],
    window: dict[str, int],
) -> dict[str, object]:
    begin_s = window["begin_s"]
    end_s = window["end_s"]
    requested_duration_s = window["duration_s"]
    selected = [
        record
        for record in records
        if record["begin_s"] >= begin_s and record["end_s"] <= end_s
    ]
    covered_duration_s = sum(
        float(record["end_s"]) - float(record["begin_s"])
        for record in selected
    )
    valid_speeds: list[tuple[float, int]] = []
    for record in selected:
        if record["speed_mps"] >= 0 and record["vehicle_contributions"] > 0:
            valid_speeds.append(
                (
                    float(record["speed_mps"]),
                    int(record["vehicle_contributions"]),
                )
            )
    speed_weight = sum(weight for _, weight in valid_speeds)
    midpoint_s = begin_s + requested_duration_s / 2
    first_half = [record for record in selected if record["end_s"] <= midpoint_s]
    second_half = [record for record in selected if record["begin_s"] >= midpoint_s]

    def mean(records_in_half: list[dict[str, float | int]], field: str) -> float | None:
        if not records_in_half:
            return None
        return sum(float(record[field]) for record in records_in_half) / len(
            records_in_half
        )

    first_flow = mean(first_half, "flow_vehph")
    second_flow = mean(second_half, "flow_vehph")
    def vehicle_weighted_speed(
        records_in_half: list[dict[str, float | int]],
    ) -> tuple[float | None, int]:
        pairs = [
            (
                float(record["speed_mps"]),
                int(record["vehicle_contributions"]),
            )
            for record in records_in_half
            if record["speed_mps"] >= 0
            and record["vehicle_contributions"] > 0
        ]
        weight = sum(item_weight for _, item_weight in pairs)
        return (
            sum(speed * item_weight for speed, item_weight in pairs) / weight
            if weight
            else None,
            weight,
        )

    first_speed, first_speed_weight = vehicle_weighted_speed(first_half)
    second_speed, second_speed_weight = vehicle_weighted_speed(second_half)
    first_occupancy = mean(first_half, "occupancy_percent")
    second_occupancy = mean(second_half, "occupancy_percent")
    return {
        "begin_s": begin_s,
        "end_s": end_s,
        "requested_duration_s": requested_duration_s,
        "covered_duration_s": covered_duration_s,
        "coverage_fraction": (
            covered_duration_s / requested_duration_s
            if requested_duration_s
            else 1.0
        ),
        "complete_interval_coverage": (
            requested_duration_s == 0
            or math.isclose(covered_duration_s, requested_duration_s)
        ),
        "interval_count": len(selected),
        "vehicle_contributions": sum(
            int(record["vehicle_contributions"]) for record in selected
        ),
        "vehicles_entered": sum(
            int(record["vehicles_entered"]) for record in selected
        ),
        "mean_interval_flow_vehph": (
            mean(selected, "flow_vehph")
        ),
        "mean_interval_occupancy_percent": (
            mean(selected, "occupancy_percent")
        ),
        "vehicle_weighted_mean_speed_mps": (
            sum(speed * weight for speed, weight in valid_speeds) / speed_weight
            if speed_weight
            else None
        ),
        "initial_trend_comparison": {
            "purpose": (
                "compare first and second halves to inspect whether initial "
                "flow/speed/occupancy transients diminish; no direction is assumed"
            ),
            "first_half_mean_flow_vehph": first_flow,
            "second_half_mean_flow_vehph": second_flow,
            "second_minus_first_flow_vehph": (
                second_flow - first_flow
                if first_flow is not None and second_flow is not None
                else None
            ),
            "speed_aggregation": (
                "vehicle-weighted by E1 nVehContrib, matching the main speed summary"
            ),
            "first_half_speed_vehicle_contributions": first_speed_weight,
            "second_half_speed_vehicle_contributions": second_speed_weight,
            "first_half_vehicle_weighted_mean_speed_mps": first_speed,
            "second_half_vehicle_weighted_mean_speed_mps": second_speed,
            "second_minus_first_vehicle_weighted_mean_speed_mps": (
                second_speed - first_speed
                if first_speed is not None and second_speed is not None
                else None
            ),
            "first_half_mean_occupancy_percent": first_occupancy,
            "second_half_mean_occupancy_percent": second_occupancy,
            "second_minus_first_occupancy_percent": (
                second_occupancy - first_occupancy
                if first_occupancy is not None and second_occupancy is not None
                else None
            ),
        },
    }


def summarize_induction_loop(
    path: Path, window_definitions: dict[str, dict[str, int]]
) -> dict[str, object]:
    time_series = [
        {
            "begin_s": float(interval.get("begin", "0")),
            "end_s": float(interval.get("end", "0")),
            "vehicle_contributions": int(
                float(interval.get("nVehContrib", "0"))
            ),
            "vehicles_entered": int(float(interval.get("nVehEntered", "0"))),
            "flow_vehph": float(interval.get("flow", "0")),
            "occupancy_percent": float(interval.get("occupancy", "0")),
            "speed_mps": float(interval.get("speed", "-1")),
        }
        for interval in xml_root(path).findall(".//interval")
    ]
    window_summaries = {
        name: summarize_interval_records(time_series, window)
        for name, window in window_definitions.items()
    }
    whole_simulation = window_summaries["simulation"]
    return {
        "path": str(path),
        "interval_count": len(time_series),
        "vehicle_contributions": whole_simulation["vehicle_contributions"],
        "vehicles_entered": whole_simulation["vehicles_entered"],
        "mean_interval_flow_vehph": whole_simulation["mean_interval_flow_vehph"],
        "mean_interval_occupancy_percent": whole_simulation[
            "mean_interval_occupancy_percent"
        ],
        "vehicle_weighted_mean_speed_mps": whole_simulation[
            "vehicle_weighted_mean_speed_mps"
        ],
        "window_summaries": window_summaries,
        "time_series": time_series,
    }


def combine_detector_window_summaries(
    summaries: list[dict[str, object]], detector_ids: tuple[str, ...]
) -> dict[str, object]:
    speed_pairs = [
        (
            summary["vehicle_weighted_mean_speed_mps"],
            summary["vehicle_contributions"],
        )
        for summary in summaries
        if summary["vehicle_weighted_mean_speed_mps"] is not None
        and summary["vehicle_contributions"] > 0
    ]
    total_speed_weight = sum(weight for _, weight in speed_pairs)

    def combined_trend(field: str, operation: str) -> float | None:
        values = [
            summary["initial_trend_comparison"][field]
            for summary in summaries
        ]
        if any(value is None for value in values):
            return None
        if operation == "sum":
            return sum(values)
        return sum(values) / len(values)

    def combined_weighted_trend(value_field: str, weight_field: str) -> float | None:
        pairs = [
            (
                summary["initial_trend_comparison"][value_field],
                summary["initial_trend_comparison"][weight_field],
            )
            for summary in summaries
            if summary["initial_trend_comparison"][value_field] is not None
            and summary["initial_trend_comparison"][weight_field] > 0
        ]
        weight = sum(item_weight for _, item_weight in pairs)
        return (
            sum(value * item_weight for value, item_weight in pairs) / weight
            if weight
            else None
        )

    first_half_group_speed = combined_weighted_trend(
        "first_half_vehicle_weighted_mean_speed_mps",
        "first_half_speed_vehicle_contributions",
    )
    second_half_group_speed = combined_weighted_trend(
        "second_half_vehicle_weighted_mean_speed_mps",
        "second_half_speed_vehicle_contributions",
    )

    return {
        "detectors": list(detector_ids),
        "begin_s": summaries[0]["begin_s"],
        "end_s": summaries[0]["end_s"],
        "requested_duration_s": summaries[0]["requested_duration_s"],
        "covered_duration_s": min(
            summary["covered_duration_s"] for summary in summaries
        ),
        "coverage_fraction": min(
            summary["coverage_fraction"] for summary in summaries
        ),
        "complete_interval_coverage": all(
            summary["complete_interval_coverage"] for summary in summaries
        ),
        "interval_count_per_detector": {
            detector_id: summary["interval_count"]
            for detector_id, summary in zip(detector_ids, summaries)
        },
        "vehicle_contributions": sum(
            summary["vehicle_contributions"] for summary in summaries
        ),
        "vehicles_entered": sum(
            summary["vehicles_entered"] for summary in summaries
        ),
        "mean_sum_lane_flow_vehph": sum(
            summary["mean_interval_flow_vehph"] or 0.0
            for summary in summaries
        ),
        "mean_lane_occupancy_percent": sum(
            summary["mean_interval_occupancy_percent"] or 0.0
            for summary in summaries
        )
        / len(summaries),
        "vehicle_weighted_mean_speed_mps": (
            sum(speed * weight for speed, weight in speed_pairs)
            / total_speed_weight
            if total_speed_weight
            else None
        ),
        "initial_trend_comparison": {
            "purpose": (
                "descriptive first-half versus second-half comparison only; "
                "a real congestion or queue trend is not a warm-up transient"
            ),
            "first_half_mean_sum_lane_flow_vehph": combined_trend(
                "first_half_mean_flow_vehph", "sum"
            ),
            "second_half_mean_sum_lane_flow_vehph": combined_trend(
                "second_half_mean_flow_vehph", "sum"
            ),
            "second_minus_first_sum_lane_flow_vehph": combined_trend(
                "second_minus_first_flow_vehph", "sum"
            ),
            "first_half_mean_lane_occupancy_percent": combined_trend(
                "first_half_mean_occupancy_percent", "mean"
            ),
            "second_half_mean_lane_occupancy_percent": combined_trend(
                "second_half_mean_occupancy_percent", "mean"
            ),
            "second_minus_first_lane_occupancy_percent": combined_trend(
                "second_minus_first_occupancy_percent", "mean"
            ),
            "speed_aggregation": (
                "vehicle-weighted by E1 nVehContrib across intervals and lanes, "
                "matching the main speed summary"
            ),
            "first_half_speed_vehicle_contributions": combined_trend(
                "first_half_speed_vehicle_contributions", "sum"
            ),
            "second_half_speed_vehicle_contributions": combined_trend(
                "second_half_speed_vehicle_contributions", "sum"
            ),
            "first_half_vehicle_weighted_mean_speed_mps": (
                first_half_group_speed
            ),
            "second_half_vehicle_weighted_mean_speed_mps": (
                second_half_group_speed
            ),
            "second_minus_first_vehicle_weighted_mean_speed_mps": (
                second_half_group_speed - first_half_group_speed
                if second_half_group_speed is not None
                and first_half_group_speed is not None
                else None
            ),
        },
    }


def summarize_merge_observations(
    outputs: Path, windows: RunWindows
) -> dict[str, object]:
    window_definitions = windows.definitions()
    detector_summaries = {
        detector_id: summarize_induction_loop(
            outputs / f"{detector_id}.xml", window_definitions
        )
        for detector_id in sorted(REQUIRED_OBSERVATION_DETECTORS)
    }
    groups = {
        "merge_upstream": ("merge_upstream_e1_l0", "merge_upstream_e1_l1"),
        "mainline_merge_entry": (
            "mainline_merge_entry_e1_l0",
            "mainline_merge_entry_e1_l1",
        ),
        "merge_downstream": (
            "merge_downstream_e1_l0",
            "merge_downstream_e1_l1",
        ),
    }
    group_summaries: dict[str, dict[str, object]] = {}
    for group_name, detector_ids in groups.items():
        summaries = [detector_summaries[item] for item in detector_ids]
        window_summaries = {
            window_name: combine_detector_window_summaries(
                [
                    summary["window_summaries"][window_name]
                    for summary in summaries
                ],
                detector_ids,
            )
            for window_name in window_definitions
        }
        group_summaries[group_name] = {
            **window_summaries["simulation"],
            "window_summaries": window_summaries,
            "measurement_semantics": (
                "origin_insertion_contaminated"
                if group_name == "merge_upstream"
                else "mainline_only_internal_connection_observation"
                if group_name == "mainline_merge_entry"
                else "downstream_mixed_mainline_and_ramp_flow"
            ),
        }
    return {
        "status": "technical descriptive summaries; not capacity metrics",
        "aggregation_period_s": 30,
        "parameter_status": (
            "detector positions and aggregation period are technical placeholders"
        ),
        "interpretation_boundary": (
            "time series and windows are descriptive; no Breakdown or Capacity "
            "Drop state is classified automatically"
        ),
        "window_definitions": window_definitions,
        "field_notes": {
            "vehicle_contributions": (
                "sum of E1 nVehContrib: vehicles contributing a completed "
                "detector measurement in an interval"
            ),
            "vehicles_entered": (
                "sum of E1 nVehEntered: vehicles entering the detector during "
                "an interval; this may differ from nVehContrib"
            ),
        },
        "detectors": detector_summaries,
        "groups": group_summaries,
    }


def refresh_data_quality_gate(gate: dict[str, object]) -> None:
    evaluated_section_keys = (
        "final_insertion_completeness",
        "fixed_window_detector_descriptive_eligibility",
        "vehicle_outcome_eligibility",
    )
    eligibility: dict[str, bool] = {}
    for key in evaluated_section_keys:
        section = gate[key]
        checks = section["checks"]
        failed_checks = sorted(
            name for name, passed in checks.items() if not passed
        )
        eligible = not failed_checks
        result_field = "complete" if key == "final_insertion_completeness" else "eligible"
        section.update(
            {
                "status": "pass" if eligible else "fail",
                result_field: eligible,
                "failed_checks": failed_checks,
            }
        )
        eligibility[key] = eligible
    passed_count = sum(eligibility.values())
    if gate["demand_period_realization_eligibility"]["eligible"] is None:
        gate["status"] = "not_fully_evaluated"
    else:
        gate["status"] = (
            "pass"
            if passed_count == len(eligibility)
            else "partial"
            if passed_count
            else "fail"
        )


def summarize_queue_window(
    samples: list[dict[str, float | int]],
    window: dict[str, int],
) -> dict[str, object]:
    begin_s = window["begin_s"]
    end_s = window["end_s"]
    selected = [
        sample
        for sample in samples
        if begin_s <= sample["time_s"] < end_s
    ]
    trend_window_s = min(
        TECHNICAL_QUEUE_TREND_WINDOW_S,
        max(0, window["duration_s"] // 2),
    )
    previous = [
        sample
        for sample in selected
        if end_s - 2 * trend_window_s
        <= sample["time_s"]
        < end_s - trend_window_s
    ]
    final = [
        sample
        for sample in selected
        if end_s - trend_window_s <= sample["time_s"] < end_s
    ]

    def mean_queue(items: list[dict[str, float | int]]) -> float | None:
        if not items:
            return None
        return sum(float(item["stopped_R_queue"]) for item in items) / len(items)

    previous_mean = mean_queue(previous)
    final_mean = mean_queue(final)
    delta = (
        final_mean - previous_mean
        if previous_mean is not None and final_mean is not None
        else None
    )
    direction = (
        "increasing"
        if delta is not None and delta > 0
        else "decreasing"
        if delta is not None and delta < 0
        else "unchanged"
        if delta == 0
        else "unavailable"
    )
    return {
        "begin_s": begin_s,
        "end_s": end_s,
        "sample_count": len(selected),
        "max_stopped_R_queue": max(
            (int(sample["stopped_R_queue"]) for sample in selected),
            default=0,
        ),
        "max_stopped_R_on_shared": max(
            (int(sample["stopped_R_on_shared"]) for sample in selected),
            default=0,
        ),
        "max_stopped_U_on_shared": max(
            (int(sample["stopped_U_on_shared"]) for sample in selected),
            default=0,
        ),
        "trailing_window_comparison": {
            "status": (
                "descriptive difference between two trailing windows; it does "
                "not establish stability, growth persistence, Breakdown, or "
                "Capacity Drop"
            ),
            "segment_duration_s": trend_window_s,
            "previous_segment_mean_stopped_R": previous_mean,
            "final_segment_mean_stopped_R": final_mean,
            "final_minus_previous_mean_stopped_R": delta,
            "observed_delta_direction": direction,
        },
    }


def convert_queue_summaries_to_descriptive_comparisons(
    queue_summaries: dict[str, object],
) -> dict[str, object]:
    """Remove legacy stability/growth claims while retaining observed values."""

    converted: dict[str, object] = {}
    for window_name, raw_summary in queue_summaries.items():
        summary = dict(raw_summary)
        comparison = summary.pop(
            "trailing_window_comparison",
            summary.pop("trailing_trend", {}),
        )
        direction = comparison.get(
            "observed_delta_direction",
            comparison.get("direction", "unavailable"),
        )
        converted[window_name] = {
            **summary,
            "trailing_window_comparison": {
                "status": (
                    "descriptive difference between two trailing windows; it "
                    "does not establish stability, growth persistence, "
                    "Breakdown, or Capacity Drop"
                ),
                "segment_duration_s": comparison.get("segment_duration_s"),
                "previous_segment_mean_stopped_R": comparison.get(
                    "previous_segment_mean_stopped_R"
                ),
                "final_segment_mean_stopped_R": comparison.get(
                    "final_segment_mean_stopped_R"
                ),
                "final_minus_previous_mean_stopped_R": comparison.get(
                    "final_minus_previous_mean_stopped_R"
                ),
                "observed_delta_direction": direction,
            },
        }
    return converted


def reanalyze_existing_summary(
    source_summary_path: Path,
) -> tuple[Path, dict[str, object]]:
    """Reanalyse immutable runtime outputs into a new temporary directory."""

    source_summary_path = source_summary_path.resolve()
    source_bytes = source_summary_path.read_bytes()
    source = json.loads(source_bytes)
    simulation = source["simulation"]
    time_windows = simulation["time_windows"]
    profile = TechnicalProfile(
        duration_s=int(time_windows["demand"]["end_s"]),
        planned={
            name: int(simulation["planned"][name]) for name in VEHICLE_CLASSES
        },
        requested_demand_vehph={
            name: float(simulation["requested_demand_vehph"][name])
            for name in VEHICLE_CLASSES
        },
        purpose=str(simulation.get("profile_purpose", "existing technical run")),
    )
    windows = RunWindows(
        warmup_end_s=int(time_windows["warmup"]["end_s"]),
        demand_end_s=int(time_windows["demand"]["end_s"]),
        simulation_end_s=int(time_windows["simulation"]["end_s"]),
    )
    source_runtime = Path(source["runtime_directory"]).resolve()
    outputs = source_runtime / "outputs"
    required_outputs = [
        outputs / "tripinfo.xml",
        outputs / "fcd.xml",
        *[
            outputs / f"{detector_id}.xml"
            for detector_id in sorted(REQUIRED_OBSERVATION_DETECTORS)
        ],
    ]
    missing = [str(path) for path in required_outputs if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Existing runtime outputs are missing: {missing}")
    network_path = source_runtime / "network.net.xml"
    if not network_path.is_file():
        raise FileNotFoundError(f"Existing runtime network is missing: {network_path}")

    departure_realization = analyze_departure_realization(
        outputs / "tripinfo.xml", profile, windows
    )
    merge_observations = summarize_merge_observations(outputs, windows)
    fcd_lane_observations = analyze_fcd_accounting(
        outputs / "fcd.xml",
        network_path,
        stopped_speed_mps=HALTING_SPEED_MPS,
    )
    queue_summaries = convert_queue_summaries_to_descriptive_comparisons(
        simulation.get("queue_window_summaries", {})
    )
    clearance_late_departures = departure_realization[
        "clearance_late_departures_by_class"
    ]
    analysis = {
        "classification": (
            "corrected post-run technical reanalysis of existing exploratory "
            "outputs; no SUMO rerun and not thesis evidence"
        ),
        "source_summary_path": str(source_summary_path),
        "source_summary_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source_runtime_directory": str(source_runtime),
        "source_outputs_modified": False,
        "source_assessment_superseded": (
            "yes: any legacy insertion_eligibility/pass field in the source "
            "summary means only eventual insertion and must not be interpreted "
            "as timely realization of the requested demand"
        ),
        "time_windows": {
            **windows.definitions(),
            "status": "exploratory candidates; not frozen",
        },
        "requested_demand_vehph": profile.requested_demand_vehph,
        "planned": profile.planned,
        "departure_realization": departure_realization,
        "corrected_detector_summaries": merge_observations,
        "fcd_lane_observations": fcd_lane_observations,
        "corrected_queue_window_summaries": queue_summaries,
        "revised_technical_assessment": {
            "final_insertion_complete": departure_realization[
                "all_planned_vehicles_finally_inserted"
            ],
            "demand_period_realization_eligibility": departure_realization[
                "demand_period_realization_eligibility"
            ],
            "clearance_has_no_late_departures": all(
                count == 0 for count in clearance_late_departures.values()
            ),
            "capacity_analysis_eligible": False,
        },
    }
    destination = Path(
        tempfile.mkdtemp(
            prefix="minimal_uncontrolled_reanalysis_", dir="/private/tmp"
        )
    )
    destination_path = destination / "reanalysis.json"
    destination_path.write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return destination_path, analysis


def run_simulation(
    sumo_binary: Path,
    run_root: Path,
    profile_name: str,
    profile: TechnicalProfile,
    seed: int,
    windows: RunWindows | None = None,
) -> tuple[list[str], dict[str, object]]:
    if windows is None:
        windows = RunWindows(
            warmup_end_s=0,
            demand_end_s=profile.duration_s,
            simulation_end_s=profile.duration_s,
        )
    if windows.demand_end_s != profile.duration_s:
        raise ValueError("profile duration must equal the demand-end window")
    window_definitions = windows.definitions()
    outputs = run_root / "outputs"
    command = [
        str(sumo_binary),
        "-c",
        str(run_root / "scenario.sumocfg"),
        "--end",
        str(windows.simulation_end_s),
        "--seed",
        str(seed),
        "--start",
        "--quit-on-end",
        "--no-step-log",
        "true",
        "--xml-validation",
        "always",
        "--summary-output",
        str(outputs / "sumo_summary.xml"),
        "--tripinfo-output",
        str(outputs / "tripinfo.xml"),
        "--tripinfo-output.write-unfinished",
        "true",
        "--vehroute-output",
        str(outputs / "vehroute.xml"),
        "--vehroute-output.write-unfinished",
        "true",
        "--fcd-output",
        str(outputs / "fcd.xml"),
        "--queue-output",
        str(outputs / "queues.xml"),
        "--log",
        str(outputs / "sumo.log"),
        "--error-log",
        str(outputs / "sumo_error.log"),
    ]
    run_checked(command, outputs / "sumo.stdout.log", outputs / "sumo.stderr.log")

    departure_realization = analyze_departure_realization(
        outputs / "tripinfo.xml", profile, windows
    )
    tripinfos = xml_root(outputs / "tripinfo.xml").findall(".//tripinfo")
    tripinfo_departed_ids: set[str] = set()
    departed = Counter({name: 0 for name in VEHICLE_CLASSES})
    arrived = Counter({name: 0 for name in VEHICLE_CLASSES})
    in_network = Counter({name: 0 for name in VEHICLE_CLASSES})
    for item in tripinfos:
        group = vehicle_class(item.get("id", ""))
        depart = float(item.get("depart", "-1"))
        arrival = float(item.get("arrival", "-1"))
        if depart >= 0:
            tripinfo_departed_ids.add(item.get("id", ""))
            departed[group] += 1
            if arrival >= 0:
                arrived[group] += 1
            else:
                in_network[group] += 1

    planned = profile.planned
    not_departed = {name: planned[name] - departed[name] for name in VEHICLE_CLASSES}
    accounting_ok = {
        name: departed[name] == arrived[name] + in_network[name]
        for name in VEHICLE_CLASSES
    }

    route_match = {name: True for name in VEHICLE_CLASSES}
    vehroute_counts = Counter({name: 0 for name in VEHICLE_CLASSES})
    realized_vehroute_samples: dict[str, list[str]] = {}
    vehroute_ids: set[str] = set()
    for vehicle in xml_root(outputs / "vehroute.xml").findall(".//vehicle"):
        vehicle_id = vehicle.get("id", "")
        group = vehicle_class(vehicle_id)
        vehroute_ids.add(vehicle_id)
        vehroute_counts[group] += 1
        route = vehicle.find("route")
        edges = tuple(route.get("edges", "").split()) if route is not None else ()
        route_match[group] = route_match[group] and edges == EXPECTED_ROUTES[group]
        realized_vehroute_samples.setdefault(group, list(edges))
    for group in VEHICLE_CLASSES:
        route_match[group] = route_match[group] and vehroute_counts[group] > 0

    max_edge_vehicles = Counter()
    max_r_halted_on_shared = 0
    max_r_halted_on_ramp = 0
    max_u_halted_on_shared = 0
    boundary_crossing_steps = 0
    simultaneous_r_u_halt_steps = 0
    first_boundary_crossing_s: float | None = None
    edge_ids = {edge for route in EXPECTED_ROUTES.values() for edge in route}
    ordered_edge_ids = sorted(edge_ids, key=len, reverse=True)
    fcd_ids: set[str] = set()
    fcd_sample_vehicle: dict[str, str] = {}
    fcd_observed_paths: dict[str, list[str]] = {
        name: [] for name in VEHICLE_CLASSES
    }
    queue_samples: list[dict[str, float | int]] = []
    for timestep in ET.iterparse(outputs / "fcd.xml", events=("end",)):
        element = timestep[1]
        if element.tag != "timestep":
            continue
        counts = Counter()
        r_shared = r_ramp = u_shared = 0
        time_s = float(element.get("time", "0"))
        for vehicle in element.findall("vehicle"):
            vehicle_id = vehicle.get("id", "")
            group = vehicle_class(vehicle_id)
            fcd_ids.add(vehicle_id)
            fcd_sample_vehicle.setdefault(group, vehicle_id)
            lane = vehicle.get("lane", "")
            edge = next(
                (item for item in ordered_edge_ids if lane.startswith(f"{item}_")),
                "",
            )
            if edge:
                counts[edge] += 1
                if fcd_sample_vehicle[group] == vehicle_id:
                    path = fcd_observed_paths[group]
                    if not path or path[-1] != edge:
                        path.append(edge)
            speed = float(vehicle.get("speed", "0"))
            if group == "R" and edge == "shared_approach" and speed <= HALTING_SPEED_MPS:
                r_shared += 1
            if group == "R" and edge in {"ramp_storage", "ramp_accel"} and speed <= HALTING_SPEED_MPS:
                r_ramp += 1
            if group == "U" and edge == "shared_approach" and speed <= HALTING_SPEED_MPS:
                u_shared += 1
        for edge, count in counts.items():
            max_edge_vehicles[edge] = max(max_edge_vehicles[edge], count)
        max_r_halted_on_shared = max(max_r_halted_on_shared, r_shared)
        max_r_halted_on_ramp = max(max_r_halted_on_ramp, r_ramp)
        max_u_halted_on_shared = max(max_u_halted_on_shared, u_shared)
        if r_shared:
            boundary_crossing_steps += 1
            if first_boundary_crossing_s is None:
                first_boundary_crossing_s = time_s
        if r_shared and u_shared:
            simultaneous_r_u_halt_steps += 1
        queue_samples.append(
            {
                "time_s": time_s,
                "stopped_R_queue": r_shared + r_ramp,
                "stopped_R_on_shared": r_shared,
                "stopped_U_on_shared": u_shared,
            }
        )
        element.clear()

    tls_elements = xml_root(outputs / "tls_states.xml").findall(".//tlsState")
    tls_states: list[str] = []
    tls_transition_count = 0
    previous_state: str | None = None
    for item in tls_elements:
        state = item.get("state", "")
        if state not in tls_states:
            tls_states.append(state)
        if previous_state is not None and state != previous_state:
            tls_transition_count += 1
        previous_state = state

    log_diagnostics = diagnostic_summary(
        [outputs / "sumo.log", outputs / "sumo_error.log", outputs / "sumo.stderr.log"]
    )

    merge_observations = summarize_merge_observations(outputs, windows)
    detector_outputs_complete = all(
        summary["interval_count"] > 0
        and Path(summary["path"]).is_file()
        and Path(summary["path"]).stat().st_size > 0
        for summary in merge_observations["detectors"].values()
    )

    last_summary = xml_root(outputs / "sumo_summary.xml").findall("step")[-1]
    collisions = int(last_summary.get("collisions", "0"))
    teleports = int(last_summary.get("teleports", "0"))
    final_insertion_rate = {
        name: departed[name] / planned[name] if planned[name] else 1.0
        for name in VEHICLE_CLASSES
    }
    all_planned_vehicles_finally_inserted = all(
        departed[name] == planned[name] for name in VEHICLE_CLASSES
    )
    total_departed = sum(departed.values())
    total_in_network = sum(in_network.values())
    end_in_network_fraction = (
        total_in_network / total_departed if total_departed else 0.0
    )
    end_in_network_fraction_by_class = {
        name: (
            in_network[name] / departed[name]
            if departed[name]
            else None
        )
        for name in VEHICLE_CLASSES
    }
    clearance_configured = windows.clearance_duration_s > 0
    network_empty_at_end = total_in_network == 0
    clearance_status = {
        "status": (
            "not_configured"
            if not clearance_configured
            else "cleared"
            if network_empty_at_end
            else "incomplete"
        ),
        "configured": clearance_configured,
        "duration_s": windows.clearance_duration_s,
        "network_empty_at_end": network_empty_at_end,
        "vehicles_in_network_at_end": total_in_network,
        "vehicles_in_network_at_end_by_class": dict(in_network),
        "end_in_network_fraction_by_class": end_in_network_fraction_by_class,
        "late_departures_during_clearance_by_class": departure_realization[
            "clearance_late_departures_by_class"
        ],
        "insertion_free": all(
            count == 0
            for count in departure_realization[
                "clearance_late_departures_by_class"
            ].values()
        ),
    }
    queue_window_summaries = {
        name: summarize_queue_window(queue_samples, window)
        for name, window in window_definitions.items()
    }
    fcd_lane_observations = analyze_fcd_accounting(
        outputs / "fcd.xml",
        run_root / "network.net.xml",
        stopped_speed_mps=HALTING_SPEED_MPS,
    )
    id_set_checks = {
        "tripinfo_equals_vehroute": tripinfo_departed_ids == vehroute_ids,
        "tripinfo_equals_fcd": tripinfo_departed_ids == fcd_ids,
    }
    id_set_difference_samples = {
        "tripinfo_not_vehroute": sorted(tripinfo_departed_ids - vehroute_ids)[:20],
        "vehroute_not_tripinfo": sorted(vehroute_ids - tripinfo_departed_ids)[:20],
        "tripinfo_not_fcd": sorted(tripinfo_departed_ids - fcd_ids)[:20],
        "fcd_not_tripinfo": sorted(fcd_ids - tripinfo_departed_ids)[:20],
    }

    expectations = {
        "all_classes_present": all(departed[name] > 0 for name in VEHICLE_CLASSES),
        "all_realized_vehroutes_match_configured": all(route_match.values()),
        "vehicle_id_sets_match_outputs": all(id_set_checks.values()),
        "vehicle_accounting_balances": all(accounting_ok.values()),
        "fixed_signal_changed_state": tls_transition_count > 0 and len(tls_states) >= 4,
        "no_collisions": collisions == 0 and log_diagnostics["collision_line_count"] == 0,
        "no_teleports": teleports == 0,
        "no_emergency_stops": log_diagnostics["emergency_braking_line_count"] == 0,
        "no_route_errors": log_diagnostics["route_error_line_count"] == 0,
        "merge_detector_outputs_complete": detector_outputs_complete,
    }
    if profile_name == "low":
        expectations["all_planned_vehicles_finally_inserted"] = (
            all_planned_vehicles_finally_inserted
        )
    elif profile_name == "stress":
        expectations["finite_storage_boundary_crossed"] = max_r_halted_on_shared > 0
        expectations["urban_through_traffic_halted_on_shared"] = max_u_halted_on_shared > 0
        expectations["R_and_U_halted_simultaneously_on_shared"] = (
            simultaneous_r_u_halt_steps > 0
        )

    data_quality_gate: dict[str, object] = {
        "status": "pending_network_diagnostics",
        "final_insertion_completeness": {
            "purpose": (
                "whether all planned vehicles had entered by simulation end; "
                "this does not establish demand-period realization"
            ),
            "checks": {
                "all_planned_vehicles_finally_inserted": (
                    all_planned_vehicles_finally_inserted
                ),
                "all_class_final_insertion_rates_complete": all(
                    math.isclose(rate, 1.0)
                    for rate in final_insertion_rate.values()
                ),
                "no_vehicles_missing_at_simulation_end": all(
                    count == 0 for count in not_departed.values()
                ),
            },
        },
        "demand_period_realization_eligibility": {
            **departure_realization["demand_period_realization_eligibility"],
            "observations": {
                "all_planned_vehicles_departed_by_demand_end": (
                    departure_realization[
                        "all_planned_vehicles_departed_by_demand_end"
                    ]
                ),
                "waiting_to_insert_at_demand_end_by_class": (
                    departure_realization[
                        "waiting_to_insert_at_demand_end_by_class"
                    ]
                ),
                "clearance_late_departures_by_class": (
                    departure_realization["clearance_late_departures_by_class"]
                ),
            },
        },
        "fixed_window_detector_descriptive_eligibility": {
            "purpose": (
                "integrity eligibility to describe the traffic actually observed "
                "by fixed-window E1 measurements only; it does not establish "
                "realization of requested demand, vehicle-outcome eligibility, "
                "or capacity-analysis eligibility"
            ),
            "checks": {
                "no_sumo_warnings": log_diagnostics["warning_line_count"] == 0,
                "no_sumo_errors": log_diagnostics["error_line_count"] == 0,
                "no_collisions": (
                    collisions == 0
                    and log_diagnostics["collision_line_count"] == 0
                ),
                "no_teleports": (
                    teleports == 0
                    and log_diagnostics["teleport_line_count"] == 0
                ),
                "no_emergency_braking": (
                    log_diagnostics["emergency_braking_line_count"] == 0
                ),
                "no_route_errors": (
                    log_diagnostics["route_error_line_count"] == 0
                ),
                "merge_detector_outputs_complete": detector_outputs_complete,
                "measurement_window_e1_coverage_complete": all(
                    detector["window_summaries"]["measurement"][
                        "complete_interval_coverage"
                    ]
                    for detector in merge_observations["detectors"].values()
                ),
            },
        },
        "vehicle_outcome_eligibility": {
            "purpose": (
                "eligibility to describe end-of-run vehicle outcomes; remains "
                "exploratory and not capacity-analysis eligibility"
            ),
            "checks": {
                "no_sumo_warnings": log_diagnostics["warning_line_count"] == 0,
                "no_sumo_errors": log_diagnostics["error_line_count"] == 0,
                "no_collisions": (
                    collisions == 0
                    and log_diagnostics["collision_line_count"] == 0
                ),
                "no_teleports": (
                    teleports == 0
                    and log_diagnostics["teleport_line_count"] == 0
                ),
                "no_emergency_braking": (
                    log_diagnostics["emergency_braking_line_count"] == 0
                ),
                "no_route_errors": (
                    log_diagnostics["route_error_line_count"] == 0
                ),
                "all_planned_vehicles_finally_inserted": (
                    all_planned_vehicles_finally_inserted
                ),
                "no_departures_during_clearance": all(
                    count == 0
                    for count in departure_realization[
                        "clearance_late_departures_by_class"
                    ].values()
                ),
                "post_demand_clearance_configured": clearance_configured,
                "output_vehicle_id_sets_match": all(id_set_checks.values()),
                "vehicle_accounting_balances": all(accounting_ok.values()),
                "all_realized_vehroutes_match_declared": all(
                    route_match.values()
                ),
                "all_classes_below_end_in_network_fraction_threshold": all(
                    fraction is not None
                    and fraction <= TECHNICAL_MAX_END_IN_NETWORK_FRACTION
                    for fraction in end_in_network_fraction_by_class.values()
                ),
            },
        },
        "thresholds": {
            "max_end_in_network_fraction_per_class": (
                TECHNICAL_MAX_END_IN_NETWORK_FRACTION
            ),
            "status": (
                "technical placeholder for exploratory checks; not a formal "
                "outcome threshold"
            ),
        },
        "end_in_network_fraction_overall": end_in_network_fraction,
        "end_in_network_fraction_by_class": end_in_network_fraction_by_class,
        "clearance_status": clearance_status,
        "departure_realization": departure_realization,
    }
    refresh_data_quality_gate(data_quality_gate)
    capacity_ineligibility_reasons = [
        "Stage 2 exploratory single-seed run with placeholder settings",
        "formal experiment protocol is not frozen",
        "no automatic Breakdown or Capacity Drop classification exists",
        *(
            ["final insertion completeness check failed"]
            if not data_quality_gate["final_insertion_completeness"]["complete"]
            else []
        ),
        "demand-period realization eligibility is not evaluated because no "
        "approved departDelay or late-insertion criterion is defined",
        *(
            ["fixed-window detector descriptive gate failed"]
            if not data_quality_gate[
                "fixed_window_detector_descriptive_eligibility"
            ]["eligible"]
            else []
        ),
        *(
            ["vehicle-outcome gate failed"]
            if not data_quality_gate["vehicle_outcome_eligibility"]["eligible"]
            else []
        ),
    ]

    return command, {
        "profile": profile_name,
        "profile_purpose": profile.purpose,
        "duration_s": profile.duration_s,
        "time_windows": {
            **window_definitions,
            "status": (
                "all time values are exploratory candidates and are not frozen"
            ),
        },
        "seed": seed,
        "requested_demand_vehph": profile.requested_demand_vehph,
        "planned": planned,
        "departed_by_simulation_end": dict(departed),
        "final_insertion_rate_by_class": final_insertion_rate,
        "all_planned_vehicles_finally_inserted": (
            all_planned_vehicles_finally_inserted
        ),
        "departure_realization": departure_realization,
        "arrived": dict(arrived),
        "not_departed_by_simulation_end": not_departed,
        "in_network_at_end": dict(in_network),
        "vehicle_accounting_balances": accounting_ok,
        "vehroute_output_route_samples": realized_vehroute_samples,
        "vehroute_output_routes_match_declared": route_match,
        "vehroute_output_vehicle_count": dict(vehroute_counts),
        "fcd_sample_vehicle": fcd_sample_vehicle,
        "fcd_observed_edge_sequence_samples": fcd_observed_paths,
        "vehicle_id_set_checks": id_set_checks,
        "vehicle_id_set_difference_samples": id_set_difference_samples,
        "fixed_signal": {
            "id": "urban_tls",
            "states_observed": tls_states,
            "transition_count": tls_transition_count,
        },
        "finite_storage": {
            "boundary_node": "urban_diverge",
            "first_stopped_R_on_shared_s": first_boundary_crossing_s,
            "seconds_with_stopped_R_on_shared": boundary_crossing_steps,
            "seconds_with_stopped_R_and_U_on_shared": (
                simultaneous_r_u_halt_steps
            ),
            "max_stopped_R_on_shared": max_r_halted_on_shared,
            "max_stopped_R_on_ramp": max_r_halted_on_ramp,
            "max_stopped_U_on_shared": max_u_halted_on_shared,
        },
        "queue_window_summaries": queue_window_summaries,
        "fcd_lane_observations": fcd_lane_observations,
        "clearance_status": clearance_status,
        "max_edge_vehicle_count": dict(max_edge_vehicles),
        "merge_observations": merge_observations,
        "collision_count": collisions,
        "teleport_count": teleports,
        "emergency_braking_warning_count": log_diagnostics["emergency_braking_line_count"],
        "log_diagnostics": log_diagnostics,
        "data_quality_gate": data_quality_gate,
        "capacity_analysis_eligible": False,
        "capacity_analysis_gate": {
            "status": "blocked",
            "eligible": False,
            "reasons": capacity_ineligibility_reasons,
        },
        "capacity_analysis_ineligibility_reasons": capacity_ineligibility_reasons,
        "technical_expectations": expectations,
        "technical_expectations_passed": all(expectations.values()),
        "output_files": {
            path.name: str(path)
            for path in sorted(outputs.iterdir())
            if path.is_file()
        },
    }


def version_line(binary: Path) -> str:
    completed = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=True
    )
    return completed.stdout.splitlines()[0]


def main() -> int:
    args = parse_args()
    if args.reanalyze_summary is not None:
        try:
            analysis_path, analysis = reanalyze_existing_summary(
                args.reanalyze_summary
            )
        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(analysis, indent=2, sort_keys=True))
        print(f"Reanalysis written to: {analysis_path}")
        return 0
    run_root: Path | None = None
    summary: dict[str, object] = {
        "status": "starting",
        "classification": (
            "exploratory technical smoke/stress test; not a formal experiment; "
            "not thesis evidence"
        ),
        "parameter_status": "all geometry, demand, duration, signal, and seed values are technical placeholders",
    }
    try:
        profile_name, profile = resolve_profile(args)
        profile, windows = resolve_run_windows(args, profile)
        source_validation = static_validate_sources()
        sumo_home = resolve_sumo_home()
        sumo_binary = resolve_binary(args.sumo_binary, "sumo")
        netconvert = resolve_binary(args.netconvert_binary, "netconvert")
        run_root = prepare_runtime()
        network_command, network_validation = build_network(netconvert, run_root)
        write_demand(run_root, profile)
        input_validation = validate_runtime_inputs(run_root, profile)
        summary.update(
            {
                "runtime_directory": str(run_root),
                "sumo_home": str(sumo_home),
                "sumo_version": version_line(sumo_binary),
                "netconvert_version": version_line(netconvert),
                "source_validation": source_validation,
                "network_validation": network_validation,
                "input_validation": input_validation,
                "selected_profile": profile_name,
                "base_profile": args.profile,
                "time_windows": {
                    **windows.definitions(),
                    "status": (
                        "all time values are exploratory candidates and are not frozen"
                    ),
                },
                "netconvert_command": network_command,
            }
        )
        network_expectations = network_validation["technical_expectations"]
        summary["technical_expectations"] = dict(network_expectations)
        if args.validate_only:
            summary["technical_expectations_passed"] = all(
                network_expectations.values()
            )
            summary["status"] = (
                "validated"
                if summary["technical_expectations_passed"]
                else "validated_with_failed_expectations"
            )
        else:
            sumo_command, simulation = run_simulation(
                sumo_binary,
                run_root,
                profile_name,
                profile,
                args.seed,
                windows=windows,
            )
            summary["sumo_command"] = sumo_command
            simulation["technical_expectations"].update(network_expectations)
            simulation["technical_expectations_passed"] = all(
                simulation["technical_expectations"].values()
            )
            gate = simulation["data_quality_gate"]
            gate["fixed_window_detector_descriptive_eligibility"][
                "checks"
            ].update(network_expectations)
            gate["vehicle_outcome_eligibility"]["checks"].update(
                network_expectations
            )
            refresh_data_quality_gate(gate)
            gate_reasons = []
            if not gate["fixed_window_detector_descriptive_eligibility"][
                "eligible"
            ]:
                gate_reasons.append(
                    "fixed-window detector descriptive gate failed"
                )
            if not gate["vehicle_outcome_eligibility"]["eligible"]:
                gate_reasons.append("vehicle-outcome gate failed")
            for reason in gate_reasons:
                if reason not in simulation[
                    "capacity_analysis_ineligibility_reasons"
                ]:
                    simulation["capacity_analysis_ineligibility_reasons"].append(
                        reason
                    )
            simulation["capacity_analysis_gate"]["reasons"] = list(
                simulation["capacity_analysis_ineligibility_reasons"]
            )
            summary["simulation"] = simulation
            summary["technical_expectations"] = simulation[
                "technical_expectations"
            ]
            summary["technical_expectations_passed"] = simulation[
                "technical_expectations_passed"
            ]
            summary["status"] = (
                "completed"
                if simulation["technical_expectations_passed"]
                else "completed_with_failed_expectations"
            )
    except Exception as error:
        summary["status"] = "failed"
        summary["error_type"] = type(error).__name__
        summary["error"] = str(error)
        summary["traceback"] = traceback.format_exc()
        if run_root is None:
            run_root = Path(
                tempfile.mkdtemp(prefix="minimal_uncontrolled_failed_", dir="/private/tmp")
            )
        summary_path = run_root / "summary.json"
        summary_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(json.dumps(summary, indent=2, sort_keys=True), file=sys.stderr)
        print(f"Failure summary written to: {summary_path}", file=sys.stderr)
        return 1

    summary_path = run_root / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Summary written to: {summary_path}")
    if summary["status"] in {
        "completed_with_failed_expectations",
        "validated_with_failed_expectations",
    }:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
