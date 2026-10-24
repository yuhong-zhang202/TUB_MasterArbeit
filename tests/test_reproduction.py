"""Offline checks for the public six-case reproduction package."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "reproduce_stage6", ROOT / "scripts" / "reproduce_stage6.py"
)
assert SPEC and SPEC.loader
REPLAY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPLAY)


class ReproductionPackageTests(unittest.TestCase):
    def test_six_cases_are_hash_bound_and_paired(self) -> None:
        checked = REPLAY.validate_inputs()
        self.assertEqual(set(checked["cases"]), set(REPLAY.EXPECTED_CASES))
        for seed in (17, 23, 42):
            self.assertEqual(
                REPLAY.sha256(REPLAY.INPUTS / f"open_s{seed}" / "demand.rou.xml"),
                REPLAY.sha256(REPLAY.INPUTS / f"v15_s{seed}" / "demand.rou.xml"),
            )

    def test_input_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            manifest = json.loads((REPLAY.INPUTS / "PROVENANCE.json").read_text())
            manifest["files"][0]["copy_sha256"] = "0" * 64
            (target / "PROVENANCE.json").write_text(json.dumps(manifest))
            (target / "network.net.xml").write_bytes(
                (REPLAY.INPUTS / "network.net.xml").read_bytes()
            )
            original = REPLAY.INPUTS
            try:
                REPLAY.INPUTS = target
                with self.assertRaisesRegex(ValueError, "hash mismatch"):
                    REPLAY.validate_inputs()
            finally:
                REPLAY.INPUTS = original

    def test_materialization_relocates_paths_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "v15_s23"
            run_dir.mkdir()
            hashes = REPLAY.materialize("v15_s23", run_dir)
            self.assertEqual(hashes["demand.rou.xml"], REPLAY.sha256(
                REPLAY.INPUTS / "v15_s23" / "demand.rou.xml"))
            config = ET.parse(run_dir / "scenario.sumocfg").getroot()
            self.assertEqual(config.find("./input/net-file").get("value"),
                             str(run_dir / "network.net.xml"))
            self.assertEqual(config.find("./random_number/seed").get("value"), "23")
            additional = ET.parse(run_dir / "scenario.add.xml").getroot()
            detector = additional.find("inductionLoop")
            self.assertTrue(detector.get("file").startswith(str(run_dir / "outputs")))
            self.assertTrue((run_dir / "outputs").is_dir())

    def test_missing_open_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "missing or empty OPEN output"):
                REPLAY.validate_open_output(Path(temporary))

    def test_direct_script_launch_can_import_v15_auditor(self) -> None:
        # -I excludes the working directory from sys.path, as in the failing
        # direct CLI launch where only scripts/ was initially importable.
        with tempfile.TemporaryDirectory() as temporary:
            code = (
                "import runpy; from pathlib import Path; "
                f"m=runpy.run_path({str(ROOT / 'scripts' / 'reproduce_stage6.py')!r}); "
                f"m['validate_v15_output'](Path({temporary!r}))"
            )
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code],
                                    cwd=temporary, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("missing/empty output", result.stderr)
            self.assertNotIn("ModuleNotFoundError", result.stderr)

    def test_replay_location_rejects_source_and_existing_directories(self) -> None:
        with self.assertRaisesRegex(ValueError, "source/archive package"):
            REPLAY.validate_run_location(ROOT / "data" / "exploratory_stage6" / "new")
        with self.assertRaisesRegex(ValueError, "temporary root"):
            REPLAY.validate_run_location(Path.home() / "Desktop" / "Masterarbeit-Ramp-Metering"
                                         / "data" / "raw" / "new")
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(FileExistsError):
                REPLAY.validate_run_location(Path(temporary))
            self.assertEqual(REPLAY.validate_run_location(Path(temporary) / "new"),
                             (Path(temporary) / "new").resolve())

    def test_size_limit_counts_inputs_as_well_as_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "network.net.xml").write_bytes(b"abc")
            (root / "outputs").mkdir()
            (root / "outputs" / "fcd.xml.gz").write_bytes(b"12345")
            self.assertEqual(REPLAY.directory_bytes(root), 8)
            receipt = {"status": "COMPLETED"}
            REPLAY.settle_receipt_size(receipt, root)
            REPLAY.write_new_json(root / "receipt.json", receipt)
            self.assertEqual(REPLAY.directory_bytes(root),
                             receipt["run_bytes_including_receipt"])

    def test_wrong_sumo_binary_build_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            fake = Path(temporary) / "sumo"
            fake.write_text("#!/bin/sh\necho 'Eclipse SUMO sumo 1.26.0'\n")
            fake.chmod(0o755)
            with self.assertRaisesRegex(ValueError, "differs from the archived"):
                REPLAY.resolve_runtime(str(fake), None, require_traci=False,
                                       require_historical_build=True)
            comparable = REPLAY.resolve_runtime(str(fake), None, require_traci=False)
            self.assertFalse(comparable["historical_build_match"])
            self.assertIn("compare outputs", comparable["replay_equivalence_scope"])


if __name__ == "__main__":
    unittest.main()
