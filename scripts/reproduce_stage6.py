"""Replay one archived Stage 6 OPEN or V15 exploratory case.

This package uses the historical network, demand, detector settings, feedback
logic and V15 safety guard. Output paths and public execution bookkeeping are
new. A replay is a technical comparison, not a formal thesis experiment.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / "config" / "reproduction"
EXPECTED_CASES = tuple(f"{mode}_s{seed}" for mode in ("open", "v15") for seed in (17, 23, 42))
HISTORICAL_CODE = {
    "control.py": "a282302ecc5fefd8d1810c07d1340f9bd1dbe0e3316b718ce991e5c5bea185ab",
    "safe_actuator_v10.py": "c48b743972e44cdac8d53b6276efeed75c8bcd45a36c13940735e9d3f317f2d7",
}
MAX_WALL_S = 120.0
MAX_OUTPUT_BYTES = 250_000_000
SOFT_STOP_BYTES = MAX_OUTPUT_BYTES - 1_000_000  # space for final logs and receipt
POLL_S = 0.1
HISTORICAL_SUMO_SHA256 = "3bd1eaa3b167fb527ac33b154dfb57b41b9a40d5bc3e26246489824dd7977179"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_new_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def directory_bytes(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def settle_receipt_size(receipt: dict, run_dir: Path) -> None:
    before = directory_bytes(run_dir)
    receipt["run_bytes_including_receipt"] = 0
    for _ in range(8):
        encoded = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
        total = before + len(encoded)
        if receipt["run_bytes_including_receipt"] == total:
            return
        receipt["run_bytes_including_receipt"] = total
    raise RuntimeError("receipt byte total did not converge")


def validate_inputs() -> dict:
    provenance_path = INPUTS / "PROVENANCE.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    if provenance.get("schema") != "STAGE6_PUBLIC_REPLAY_INPUTS_V1":
        raise ValueError("unsupported input provenance")
    records = provenance.get("files")
    if not isinstance(records, list) or len(records) != 19:
        raise ValueError("expected one network and eighteen case input files")
    expected_paths = {"network.net.xml"} | {
        f"{case}/{name}" for case in EXPECTED_CASES
        for name in ("demand.rou.xml", "scenario.add.xml", "scenario.sumocfg")
    }
    if {record.get("copy") for record in records} != expected_paths:
        raise ValueError("input inventory differs from the six reviewed cases")
    for record in records:
        relative = Path(record["copy"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe input path: {relative}")
        path = INPUTS / relative
        if sha256(path) != record["copy_sha256"]:
            raise ValueError(f"input copy hash mismatch: {relative}")
        if b"/Users/" in path.read_bytes():
            raise ValueError(f"private absolute path remains: {relative}")
    for name, expected in HISTORICAL_CODE.items():
        if sha256(ROOT / "src" / "reproduction" / name) != expected:
            raise ValueError(f"historical traffic logic changed: {name}")
    network = ET.parse(INPUTS / "network.net.xml").getroot()
    open_signal = network.find("./tlLogic[@id='ramp_mid'][@programID='A_OPEN']")
    if open_signal is None or [x.get("state") for x in open_signal.findall("phase")] != ["G"]:
        raise ValueError("historical open ramp program changed")
    connection = [x for x in network.findall("connection") if x.get("tl") == "ramp_mid"]
    if len(connection) != 1 or tuple(connection[0].get(k) for k in
        ("from", "to", "fromLane", "toLane", "via", "linkIndex")) != (
        "ramp_storage", "ramp_accel", "0", "0", ":ramp_mid_0_0", "0"):
        raise ValueError("ramp controlled connection changed")
    checked = {}
    for case in EXPECTED_CASES:
        seed = int(case.rsplit("s", 1)[1])
        folder = INPUTS / case
        demand = ET.parse(folder / "demand.rou.xml").getroot()
        vehicles = demand.findall("vehicle")
        counts = {letter: sum(v.get("id", "").startswith(letter + "_flow.") for v in vehicles)
                  for letter in "MRUX"}
        if counts != {"M": 3000, "R": 600, "U": 300, "X": 150}:
            raise ValueError(f"unexpected planned population in {case}: {counts}")
        config = ET.parse(folder / "scenario.sumocfg").getroot()
        expected_config = {"net-file": "network.net.xml", "route-files": "demand.rou.xml",
                           "additional-files": "scenario.add.xml"}
        if any(config.find(f"./input/{tag}").get("value") != value
               for tag, value in expected_config.items()):
            raise ValueError(f"input configuration changed in {case}")
        if (config.find("./time/begin").get("value") != "0" or
                config.find("./time/end").get("value") != "4200" or
                config.find("./time/step-length").get("value") != "1" or
                config.find("./random_number/seed").get("value") != str(seed)):
            raise ValueError(f"time/seed mismatch in {case}")
        additional = ET.parse(folder / "scenario.add.xml").getroot()
        selector = additional.find("WAUT")
        if selector is None or selector.get("startProg") != "A_OPEN":
            raise ValueError(f"wrong pre-control signal in {case}")
        checked[case] = {"seed": seed, "vehicle_counts": counts}
    for seed in (17, 23, 42):
        if sha256(INPUTS / f"open_s{seed}" / "demand.rou.xml") != sha256(
            INPUTS / f"v15_s{seed}" / "demand.rou.xml"):
            raise ValueError(f"paired demand differs for seed {seed}")
    return {"status": "INPUTS_VALID", "cases": checked,
            "provenance_sha256": sha256(provenance_path)}


def resolve_runtime(sumo_option: str | None, sumo_home_option: str | None,
                    require_traci: bool, require_historical_build: bool = False) -> dict:
    located = shutil.which(sumo_option or "sumo") or sumo_option
    if not located:
        raise FileNotFoundError("SUMO binary not found; use --sumo-binary")
    sumo = Path(located).expanduser().resolve(strict=True)
    version = subprocess.run([str(sumo), "--version"], capture_output=True,
                             text=True, timeout=10, check=True).stdout.splitlines()[0]
    if not re.search(r"\b1\.26\.0\b", version):
        raise ValueError(f"historical SUMO version 1.26.0 required: {version}")
    binary_hash = sha256(sumo)
    build_match = binary_hash == HISTORICAL_SUMO_SHA256
    if require_historical_build and not build_match:
        raise ValueError("SUMO binary differs from the archived 1.26.0 executable")
    candidates = [Path(sumo_home_option).expanduser()] if sumo_home_option else []
    if os.environ.get("SUMO_HOME"):
        candidates.append(Path(os.environ["SUMO_HOME"]).expanduser())
    candidates.extend((sumo.parent.parent / "share" / "sumo", sumo.parent.parent))
    home = next((candidate.resolve() for candidate in candidates
                 if (candidate / "tools" / "traci").is_dir()), None)
    if require_traci and home is None:
        raise FileNotFoundError("SUMO_HOME/tools/traci not found; use --sumo-home")
    if home is not None:
        sys.path.insert(0, str(home / "tools"))
        if require_traci:
            import traci  # noqa: F401 - dependency check, no simulation
    return {"sumo_binary": str(sumo), "sumo_binary_sha256": binary_hash,
            "sumo_version": version, "sumo_home": str(home) if home else None,
            "python_version": sys.version.split()[0],
            "historical_build_match": build_match,
            "historical_build_required": require_historical_build,
            "replay_equivalence_scope": (
                "same archived SUMO binary" if build_match else
                "different SUMO 1.26.0 binary; compare outputs before any equivalence claim"
            )}


def materialize(case: str, run_dir: Path) -> dict:
    shutil.copyfile(INPUTS / "network.net.xml", run_dir / "network.net.xml")
    original = INPUTS / case
    shutil.copyfile(original / "demand.rou.xml", run_dir / "demand.rou.xml")
    output = run_dir / "outputs"
    output.mkdir(exist_ok=False)
    add = ET.parse(original / "scenario.add.xml")
    for item in add.getroot().iter():
        for key in ("file", "dest"):
            value = item.get(key)
            if value and value.startswith("outputs/"):
                item.set(key, str(run_dir / value))
    add.write(run_dir / "scenario.add.xml", encoding="utf-8", xml_declaration=True)
    cfg = ET.parse(original / "scenario.sumocfg")
    for tag, name in (("net-file", "network.net.xml"),
                      ("route-files", "demand.rou.xml"),
                      ("additional-files", "scenario.add.xml")):
        cfg.find(f"./input/{tag}").set("value", str(run_dir / name))
    for item in cfg.findall("./output/*") + cfg.findall("./report/*"):
        value = item.get("value")
        if value and value.startswith("outputs/"):
            item.set("value", str(run_dir / value))
    cfg.write(run_dir / "scenario.sumocfg", encoding="utf-8", xml_declaration=True)
    return {name: sha256(run_dir / name) for name in
            ("network.net.xml", "demand.rou.xml", "scenario.add.xml", "scenario.sumocfg")}


def stop_group(child: subprocess.Popen) -> None:
    if child.poll() is not None:
        return
    os.killpg(child.pid, signal.SIGTERM)
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait(timeout=3)


def validate_run_location(run_dir: Path) -> Path:
    """This release only writes a new case under the operating-system temp area."""
    target = run_dir.expanduser().resolve(strict=False)
    allowed_roots = {Path(tempfile.gettempdir()).resolve(), Path("/private/tmp").resolve()}
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise ValueError("replay output must be a new directory under a temporary root")
    if target.is_relative_to(ROOT.resolve()):
        raise ValueError("replay output may not be inside this source/archive package")
    if target.exists():
        raise FileExistsError(f"replay output already exists: {target}")
    return target


def validate_open_output(output: Path) -> None:
    required = {"fcd.xml.gz", "tripinfo.xml", "vehroute.xml", "lanechanges.xml",
                "queues.xml", "sumo_summary.xml", "tls_states.xml",
                "shared_boundary_e2.xml", "ramp_storage_e2.xml"}
    required |= {f"p1_{edge}_{pos}_l{lane}.xml"
                 for edge, pos, lanes in (("main_up", 1300, 2),
                                          ("merge_section", 20, 3),
                                          ("main_down", 20, 2),
                                          ("main_down", 200, 2))
                 for lane in range(lanes)}
    for name in sorted(required):
        path = output / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"missing or empty OPEN output: {name}")
        with (gzip.open(path, "rb") if name.endswith(".gz") else path.open("rb")) as stream:
            for _, node in ET.iterparse(stream, events=("end",)):
                node.clear()


def validate_v15_output(output: Path) -> None:
    # A direct `python scripts/reproduce_stage6.py` launch adds scripts/, not
    # the package root, to the wrapper's import path. The child sets its own
    # PYTHONPATH; the parent also needs the root for read-only output audit.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from src.reproduction.runtime_v15 import validate_output
    validate_output(output)


def run_case(mode: str, seed: int, run_dir: Path, runtime: dict, checked: dict) -> dict:
    case = f"{mode.lower()}_s{seed}"
    if case not in EXPECTED_CASES:
        raise ValueError("only the six archived cases are supported")
    if os.name != "posix":
        raise RuntimeError("the bounded process-group runner currently requires POSIX")
    run_dir = validate_run_location(run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    started_at = now_utc()
    started = time.monotonic()
    child = None
    reason = None
    failure = None
    process_finished_wall_s = None
    materialized = {}
    try:
        materialized = materialize(case, run_dir)
        output = run_dir / "outputs"
        card = {
            "schema": "STAGE6_PUBLIC_REPLAY_CARD_V1", "classification": "EXPLORATORY_REPLAY",
            "run_id": case, "mode": "ALINEA" if mode == "V15" else "OPEN", "seed": seed,
            "package": str(run_dir), "output": str(output),
            "network": str(run_dir / "network.net.xml"),
            "sumo_binary": runtime["sumo_binary"], "sumo_home": runtime["sumo_home"],
            "input_sha256": materialized, "source_provenance_sha256": checked["provenance_sha256"],
            "feedback": dict(target_pct=11.0, gain_veh_h_per_pct=70.0,
                             minimum_veh_h=300.0, maximum_veh_h=900.0,
                             initial_veh_h=900.0),
            "startup_deadline_s": 60.0,
            "meter_controlled_link": {"storage_length_m": 204.49},
            "limits": {"wall_s": MAX_WALL_S, "run_bytes": MAX_OUTPUT_BYTES,
                       "soft_stop_bytes": SOFT_STOP_BYTES},
        }
        write_new_json(run_dir / "run_card.json", card)
        if mode == "OPEN":
            command = [runtime["sumo_binary"], "-c", str(run_dir / "scenario.sumocfg")]
            stdout_path, stderr_path = output / "stdout.log", output / "stderr.log"
        else:
            command = [sys.executable, "-m", "src.reproduction.runtime_v15",
                       str(run_dir / "run_card.json")]
            stdout_path, stderr_path = output / "worker_stdout.log", output / "worker_stderr.log"
        env = os.environ.copy()
        if runtime["sumo_home"]:
            env["SUMO_HOME"] = runtime["sumo_home"]
            env["PYTHONPATH"] = os.pathsep.join((str(ROOT),
                str(Path(runtime["sumo_home"]) / "tools"), env.get("PYTHONPATH", "")))
        else:
            env["PYTHONPATH"] = os.pathsep.join((str(ROOT), env.get("PYTHONPATH", "")))
        if mode == "V15":
            env["STAGE6_REPLAY_GUARD"] = sha256(run_dir / "run_card.json")
        with stdout_path.open("x") as out, stderr_path.open("x") as err:
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=out, stderr=err,
                                     start_new_session=True)
            while child.poll() is None:
                if time.monotonic() - started >= MAX_WALL_S:
                    reason = "WALL_LIMIT"
                elif directory_bytes(run_dir) >= SOFT_STOP_BYTES:
                    reason = "OUTPUT_LIMIT"
                if reason:
                    stop_group(child)
                    break
                time.sleep(POLL_S)
            if not reason and time.monotonic() - started >= MAX_WALL_S:
                reason = "WALL_LIMIT_AT_EXIT"
            if not reason and directory_bytes(run_dir) >= SOFT_STOP_BYTES:
                reason = "OUTPUT_LIMIT_AT_EXIT"
            if not reason and child.returncode != 0:
                reason = "PROCESS_FAILED"
        process_finished_wall_s = time.monotonic() - started
        if not reason:
            if mode == "V15":
                validate_v15_output(output)
            else:
                validate_open_output(output)
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        if child is not None:
            stop_group(child)
    output = run_dir / "outputs"
    files = {str(path.relative_to(run_dir)): {"bytes": path.stat().st_size,
        "sha256": sha256(path)} for path in sorted(output.rglob("*")) if path.is_file()} if output.exists() else {}
    audit_finished_wall_s = time.monotonic() - started
    if audit_finished_wall_s >= MAX_WALL_S and not reason:
        reason = "WALL_LIMIT_DURING_AUDIT"
    receipt = {"status": "COMPLETED" if child and child.returncode == 0 and not reason and not failure else "FAILED",
               "case": case, "classification": "EXPLORATORY_REPLAY", "started_at": started_at,
               "finished_at": now_utc(), "wall_s": time.monotonic() - started,
               "process_finished_wall_s": process_finished_wall_s,
               "audit_finished_wall_s": audit_finished_wall_s,
               "process_return_code": child.returncode if child else None,
               "stop_reason": reason, "failure": failure, "command": command if child else None,
               "runtime": runtime, "source_provenance_sha256": checked["provenance_sha256"],
               "input_sha256": materialized, "output_manifest": files,
               "run_bytes_before_receipt": directory_bytes(run_dir),
               "output_bytes": sum(item["bytes"] for item in files.values())}
    settle_receipt_size(receipt, run_dir)
    if receipt["run_bytes_including_receipt"] >= MAX_OUTPUT_BYTES:
        receipt["status"] = "FAILED"
        receipt["stop_reason"] = receipt["stop_reason"] or "RUN_BYTES_LIMIT_AT_RECEIPT"
        settle_receipt_size(receipt, run_dir)
    write_new_json(run_dir / "receipt.json", receipt)
    if directory_bytes(run_dir) != receipt["run_bytes_including_receipt"]:
        raise RuntimeError("final replay byte total differs from the receipt")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate all inputs and runtime without running traffic")
    parser.add_argument("--mode", choices=("OPEN", "V15"))
    parser.add_argument("--seed", type=int, choices=(17, 23, 42))
    parser.add_argument("--run-dir", type=Path, help="Fresh output directory; must not exist")
    parser.add_argument("--sumo-binary", help="SUMO 1.26.0 executable, otherwise locate in PATH")
    parser.add_argument("--sumo-home", help="SUMO installation directory containing tools/traci")
    parser.add_argument("--require-historical-build", action="store_true",
                        help="Require the exact archived SUMO 1.26.0 binary SHA-256")
    args = parser.parse_args()
    if not args.check and (args.mode is None or args.seed is None):
        parser.error("--mode and --seed are required unless --check is used")
    if args.check and (args.mode is not None or args.seed is not None or args.run_dir is not None):
        parser.error("--check does not select a traffic run")
    checked = validate_inputs()
    runtime = resolve_runtime(args.sumo_binary, args.sumo_home,
                              require_traci=True,
                              require_historical_build=args.require_historical_build)
    if args.check:
        print(json.dumps({**checked, "runtime": runtime}, indent=2, sort_keys=True))
        return 0
    run_dir = args.run_dir or (Path(tempfile.gettempdir()) / "stage6_replay" /
                               f"{args.mode.lower()}_s{args.seed}_{uuid.uuid4().hex[:8]}")
    receipt = run_case(args.mode, args.seed, run_dir, runtime, checked)
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0 if receipt["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
