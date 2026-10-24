"""Static tests for the exploratory minimal uncontrolled SUMO scenario."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

from src.scenarios.run_minimal_uncontrolled import (
    E2_NAMED_LANE_COVERAGE,
    EXPECTED_ROUTES,
    MAINLINE_MERGE_ENTRY_E1,
    PROFILES,
    REQUIRED_OBSERVATION_DETECTORS,
    RunWindows,
    analyze_departure_realization,
    build_network,
    make_custom_profile,
    prepare_runtime,
    resolve_binary,
    resolve_run_windows,
    resolve_sumo_home,
    reanalyze_existing_summary,
    run_simulation,
    static_validate_sources,
    summarize_interval_records,
    validate_mainline_merge_entry_e1,
    validate_runtime_inputs,
    with_demand_end,
    write_demand,
)


class MinimalUncontrolledScenarioTests(unittest.TestCase):
    def test_source_topology_and_fixed_signal(self) -> None:
        result = static_validate_sources()
        self.assertEqual(result["traffic_light"]["type"], "static")
        self.assertEqual(
            result["finite_storage_boundary"]["shared_edge"],
            "shared_approach",
        )
        self.assertIn(
            ["shared_approach", "ramp_storage"],
            result["required_connections"],
        )
        self.assertIn(
            ["shared_approach", "urban_out"],
            result["required_connections"],
        )
        self.assertEqual(
            set(result["merge_observation_detectors"]),
            REQUIRED_OBSERVATION_DETECTORS,
        )
        self.assertEqual(
            result["e2_source_fallback"]["detectors"], E2_NAMED_LANE_COVERAGE
        )
        self.assertEqual(
            result["merge_upstream_semantics"], "origin_insertion_contaminated"
        )
        additional = ET.parse(
            Path("config/scenarios/minimal_uncontrolled/scenario.add.xml")
        ).getroot()
        for detector_id, lane_id in E2_NAMED_LANE_COVERAGE.items():
            detector = additional.find(f"./laneAreaDetector[@id='{detector_id}']")
            self.assertIsNotNone(detector)
            self.assertEqual(detector.get("lane"), lane_id)
            self.assertEqual(detector.get("pos"), "0")
            self.assertEqual(detector.get("endPos"), "-0.1")
            self.assertNotIn("length", detector.attrib)
        for detector_id, expected in MAINLINE_MERGE_ENTRY_E1.items():
            detector = additional.find(
                f"./inductionLoop[@id='{detector_id}']"
            )
            self.assertIsNotNone(detector)
            self.assertEqual(detector.get("lane"), expected["lane_id"])
            self.assertEqual(float(detector.get("pos")), expected["position_m"])
            self.assertEqual(int(detector.get("period")), expected["period_s"])

    def test_existing_compiled_topology_accepts_mainline_entry_mapping(self) -> None:
        source = Path("/private/tmp/minimal_uncontrolled_8egb0qb1")
        if not (source / "network.net.xml").is_file():
            self.skipTest("approved reference runtime is unavailable")
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            (runtime / "outputs").mkdir()
            (runtime / "network.net.xml").write_bytes(
                (source / "network.net.xml").read_bytes()
            )
            additional = ET.parse(
                Path("config/scenarios/minimal_uncontrolled/scenario.add.xml")
            )
            for element in additional.getroot():
                if element.get("id"):
                    element.set(
                        "file", str(runtime / "outputs" / f"{element.get('id')}.xml")
                    )
            additional.write(runtime / "scenario.add.xml")
            verified = validate_mainline_merge_entry_e1(runtime)
        self.assertEqual(set(verified), set(MAINLINE_MERGE_ENTRY_E1))
        self.assertTrue(
            all(
                item["compiled_connection_state"] == "M"
                for item in verified.values()
            )
        )

    def test_routes_distinguish_m_r_u(self) -> None:
        self.assertEqual(EXPECTED_ROUTES["M"], ("main_up", "main_down"))
        self.assertIn("ramp_storage", EXPECTED_ROUTES["R"])
        self.assertNotIn("ramp_storage", EXPECTED_ROUTES["U"])
        self.assertEqual(EXPECTED_ROUTES["R"][:2], EXPECTED_ROUTES["U"][:2])

    def test_generated_demand_uses_required_insertion_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            write_demand(runtime, PROFILES["low"])
            result = validate_runtime_inputs(runtime, PROFILES["low"])
            self.assertEqual(result["planned_counts"], PROFILES["low"].planned)
            self.assertEqual(
                result["requested_demand_vehph"],
                PROFILES["low"].requested_demand_vehph,
            )
            self.assertEqual(result["insertion_settings"]["departPos"], "last")
            self.assertEqual(result["insertion_settings"]["departLane"], "best")
            self.assertEqual(result["insertion_settings"]["departSpeed"], "max")

            root = ET.parse(runtime / "demand.rou.xml").getroot()
            vehicle_type = root.find("./vType[@id='technical_passenger']")
            self.assertIsNotNone(vehicle_type)
            for attribute in ("carFollowModel", "tau", "sigma", "accel", "decel"):
                self.assertNotIn(attribute, vehicle_type.attrib)

    def test_profiles_are_explicitly_small_technical_runs(self) -> None:
        self.assertEqual(set(PROFILES), {"low", "stress"})
        self.assertLessEqual(PROFILES["low"].duration_s, 600)
        self.assertLessEqual(PROFILES["stress"].duration_s, 900)
        self.assertGreater(PROFILES["stress"].planned["R"], PROFILES["low"].planned["R"])

    def test_custom_q_main_and_q_ramp_keep_urban_placeholders_fixed(self) -> None:
        custom = make_custom_profile(
            PROFILES["low"], q_main_vehph=1440.0, q_ramp_vehph=480.0
        )
        self.assertEqual(
            custom.requested_demand_vehph,
            {"M": 1440.0, "R": 480.0, "U": 360.0, "X": 180.0},
        )
        self.assertEqual(custom.planned, {"M": 240, "R": 80, "U": 60, "X": 30})

    def test_empty_generated_route_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            write_demand(runtime, PROFILES["low"])
            tree = ET.parse(runtime / "demand.rou.xml")
            tree.getroot().find("./route[@id='U_route']").set("edges", "")
            tree.write(runtime / "demand.rou.xml")
            with self.assertRaisesRegex(RuntimeError, "missing or changed"):
                validate_runtime_inputs(runtime, PROFILES["low"])

    def test_stage2_windows_are_explicit_and_default_is_backward_compatible(self) -> None:
        default_args = argparse.Namespace(
            warmup_s=0,
            measurement_duration_s=None,
            demand_end_s=None,
            post_demand_clearance_s=0,
        )
        profile, windows = resolve_run_windows(default_args, PROFILES["low"])
        self.assertEqual(profile.duration_s, 600)
        self.assertEqual(windows.definitions()["measurement"], {
            "begin_s": 0,
            "end_s": 600,
            "duration_s": 600,
        })
        self.assertEqual(windows.definitions()["clearance"]["duration_s"], 0)

        candidate_args = argparse.Namespace(
            warmup_s=300,
            measurement_duration_s=600,
            demand_end_s=900,
            post_demand_clearance_s=600,
        )
        profile, windows = resolve_run_windows(candidate_args, PROFILES["low"])
        self.assertEqual(profile.duration_s, 900)
        self.assertEqual(windows.measurement_begin_s, 300)
        self.assertEqual(windows.measurement_end_s, 900)
        self.assertEqual(windows.simulation_end_s, 1500)

    def test_half_window_speed_uses_vehicle_contribution_weights(self) -> None:
        records = [
            {
                "begin_s": 0.0,
                "end_s": 30.0,
                "vehicle_contributions": 1,
                "vehicles_entered": 1,
                "flow_vehph": 120.0,
                "occupancy_percent": 1.0,
                "speed_mps": 10.0,
            },
            {
                "begin_s": 30.0,
                "end_s": 60.0,
                "vehicle_contributions": 3,
                "vehicles_entered": 3,
                "flow_vehph": 360.0,
                "occupancy_percent": 3.0,
                "speed_mps": 20.0,
            },
            {
                "begin_s": 60.0,
                "end_s": 90.0,
                "vehicle_contributions": 2,
                "vehicles_entered": 2,
                "flow_vehph": 240.0,
                "occupancy_percent": 2.0,
                "speed_mps": 30.0,
            },
            {
                "begin_s": 90.0,
                "end_s": 120.0,
                "vehicle_contributions": 2,
                "vehicles_entered": 2,
                "flow_vehph": 240.0,
                "occupancy_percent": 2.0,
                "speed_mps": 40.0,
            },
        ]
        result = summarize_interval_records(
            records, {"begin_s": 0, "end_s": 120, "duration_s": 120}
        )
        trend = result["initial_trend_comparison"]
        self.assertEqual(
            trend["first_half_vehicle_weighted_mean_speed_mps"], 17.5
        )
        self.assertEqual(
            trend["second_half_vehicle_weighted_mean_speed_mps"], 35.0
        )
        self.assertEqual(trend["first_half_speed_vehicle_contributions"], 4)


@unittest.skipUnless(
    os.environ.get("RUN_SUMO_INTEGRATION") == "1",
    "Set RUN_SUMO_INTEGRATION=1 to allow additional SUMO traffic simulations",
)
class MinimalUncontrolledHeadlessIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        resolve_sumo_home()
        cls.sumo = resolve_binary(None, "sumo")
        cls.netconvert = resolve_binary(None, "netconvert")
        cls.results: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
        cls.runtimes: dict[str, Path] = {}
        profiles = {
            "low": (PROFILES["low"], None),
            "stress": (PROFILES["stress"], None),
            "custom": (
                make_custom_profile(
                    PROFILES["low"], q_main_vehph=1440.0, q_ramp_vehph=480.0
                ),
                None,
            ),
            "low_clearance": (
                with_demand_end(PROFILES["low"], 900),
                RunWindows(
                    warmup_end_s=300,
                    demand_end_s=900,
                    simulation_end_s=1500,
                ),
            ),
        }
        for profile_name, (profile, windows) in profiles.items():
            runtime = prepare_runtime()
            _, network = build_network(cls.netconvert, runtime)
            write_demand(runtime, profile)
            validate_runtime_inputs(runtime, profile)
            _, simulation = run_simulation(
                cls.sumo,
                runtime,
                profile_name,
                profile,
                seed=17,
                windows=windows,
            )
            cls.results[profile_name] = (network, simulation)
            cls.runtimes[profile_name] = runtime

    def test_low_profile_accounting_ids_and_insertion(self) -> None:
        network, simulation = self.results["low"]
        self.assertEqual(
            network["netconvert_diagnostics"]["warning_line_count"], 0
        )
        self.assertTrue(simulation["all_planned_vehicles_finally_inserted"])
        self.assertTrue(all(simulation["vehicle_accounting_balances"].values()))
        self.assertTrue(all(simulation["vehicle_id_set_checks"].values()))
        self.assertEqual(
            set(simulation["vehroute_output_route_samples"]), set(EXPECTED_ROUTES)
        )
        self.assertTrue(
            simulation["data_quality_gate"][
                "fixed_window_detector_descriptive_eligibility"
            ]["eligible"]
        )
        self.assertFalse(
            simulation["data_quality_gate"][
                "vehicle_outcome_eligibility"
            ]["eligible"]
        )
        self.assertAlmostEqual(
            simulation["data_quality_gate"][
                "end_in_network_fraction_by_class"
            ]["R"],
            12 / 60,
        )
        self.assertEqual(
            set(simulation["merge_observations"]["detectors"]),
            REQUIRED_OBSERVATION_DETECTORS,
        )
        for detector in simulation["merge_observations"]["detectors"].values():
            self.assertGreater(detector["interval_count"], 0)
            self.assertIsNotNone(detector["mean_interval_flow_vehph"])
            self.assertIsNotNone(detector["mean_interval_occupancy_percent"])
        for group in simulation["merge_observations"]["groups"].values():
            self.assertEqual(
                group["vehicles_entered"],
                sum(
                    simulation["merge_observations"]["detectors"][detector_id][
                        "vehicles_entered"
                    ]
                    for detector_id in group["detectors"]
                ),
            )
        self.assertIn(
            "nVehEntered",
            simulation["merge_observations"]["field_notes"]["vehicles_entered"],
        )
        coverage = network["e2_named_lane_coverage"]
        runtime_additional = ET.parse(
            self.runtimes["low"] / "scenario.add.xml"
        ).getroot()
        for detector_id, lane_id in E2_NAMED_LANE_COVERAGE.items():
            self.assertEqual(coverage[detector_id]["lane_id"], lane_id)
            self.assertEqual(coverage[detector_id]["begin_pos_m"], 0.0)
            self.assertEqual(
                coverage[detector_id]["end_pos_m"], coverage[detector_id]["length_m"]
            )
            runtime_detector = runtime_additional.find(
                f"./laneAreaDetector[@id='{detector_id}']"
            )
            self.assertIsNotNone(runtime_detector)
            self.assertNotIn("length", runtime_detector.attrib)
            self.assertAlmostEqual(
                float(runtime_detector.get("endPos")),
                float(coverage[detector_id]["end_pos_m"]),
            )
        lane_observations = simulation["fcd_lane_observations"]
        self.assertTrue(
            lane_observations["accounting"]["reconciliation"]
            ["all_compiled_lanes_grouped_once"]
        )
        # v2 keeps raw-lane and mutually exclusive atomic-group counts in
        # distinct maps.  Both must reconcile to the instantaneous samples;
        # this prevents the former mixed ``group_class_counts`` schema from
        # silently returning in a future runner import.
        for timestep in lane_observations["instantaneous_by_timestep"]:
            self.assertNotIn("group_class_counts", timestep)
            self.assertNotIn("stopped_group_class_counts", timestep)
            lane_counts = timestep["lane_class_counts"]
            group_counts = timestep["atomic_group_class_counts"]
            self.assertEqual(sum(lane_counts.values()), timestep["vehicle_samples"])
            known_lane_samples = sum(
                count
                for key, count in lane_counts.items()
                if not key.startswith("unknown_network_lane/")
            )
            self.assertEqual(sum(group_counts.values()), known_lane_samples)

    def test_stress_profile_records_censoring_and_simultaneous_spillback(self) -> None:
        network, simulation = self.results["stress"]
        self.assertEqual(
            network["netconvert_diagnostics"]["warning_line_count"], 0
        )
        self.assertFalse(simulation["all_planned_vehicles_finally_inserted"])
        self.assertFalse(simulation["capacity_analysis_eligible"])
        self.assertGreater(simulation["not_departed_by_simulation_end"]["R"], 0)
        self.assertGreater(simulation["not_departed_by_simulation_end"]["U"], 0)
        self.assertTrue(all(simulation["vehicle_accounting_balances"].values()))
        self.assertTrue(all(simulation["vehicle_id_set_checks"].values()))
        self.assertGreater(
            simulation["finite_storage"][
                "seconds_with_stopped_R_and_U_on_shared"
            ],
            0,
        )
        self.assertTrue(
            simulation["data_quality_gate"][
                "fixed_window_detector_descriptive_eligibility"
            ]["eligible"]
        )
        self.assertIn(
            "traffic actually observed",
            simulation["data_quality_gate"][
                "fixed_window_detector_descriptive_eligibility"
            ]["purpose"],
        )
        self.assertFalse(
            simulation["data_quality_gate"][
                "vehicle_outcome_eligibility"
            ]["eligible"]
        )

    def test_custom_profile_is_parameterized_and_quality_gated(self) -> None:
        network, simulation = self.results["custom"]
        self.assertEqual(
            network["netconvert_diagnostics"]["warning_line_count"], 0
        )
        self.assertEqual(
            simulation["requested_demand_vehph"],
            {"M": 1440.0, "R": 480.0, "U": 360.0, "X": 180.0},
        )
        self.assertEqual(
            simulation["planned"], {"M": 240, "R": 80, "U": 60, "X": 30}
        )
        self.assertTrue(simulation["all_planned_vehicles_finally_inserted"])
        self.assertTrue(all(simulation["vehicle_accounting_balances"].values()))
        self.assertTrue(all(simulation["vehicle_id_set_checks"].values()))
        self.assertTrue(
            simulation["data_quality_gate"][
                "fixed_window_detector_descriptive_eligibility"
            ]["eligible"]
        )
        self.assertFalse(
            simulation["data_quality_gate"][
                "vehicle_outcome_eligibility"
            ]["eligible"]
        )
        self.assertAlmostEqual(
            simulation["data_quality_gate"][
                "end_in_network_fraction_by_class"
            ]["R"],
            35 / 80,
        )
        self.assertIn(
            "all_classes_below_end_in_network_fraction_threshold",
            simulation["data_quality_gate"][
                "vehicle_outcome_eligibility"
            ]["failed_checks"],
        )
        self.assertFalse(simulation["capacity_analysis_eligible"])

    def test_clearance_run_reports_windows_time_series_and_outcome_gate(self) -> None:
        network, simulation = self.results["low_clearance"]
        self.assertEqual(
            network["netconvert_diagnostics"]["warning_line_count"], 0
        )
        self.assertEqual(
            simulation["time_windows"]["warmup"],
            {"begin_s": 0, "end_s": 300, "duration_s": 300},
        )
        self.assertEqual(
            simulation["time_windows"]["measurement"],
            {"begin_s": 300, "end_s": 900, "duration_s": 600},
        )
        self.assertEqual(
            simulation["time_windows"]["clearance"],
            {"begin_s": 900, "end_s": 1500, "duration_s": 600},
        )
        self.assertTrue(
            simulation["data_quality_gate"]["final_insertion_completeness"][
                "complete"
            ]
        )
        self.assertTrue(
            simulation["data_quality_gate"][
                "fixed_window_detector_descriptive_eligibility"
            ]["eligible"]
        )
        self.assertEqual(
            simulation["data_quality_gate"]["clearance_status"]["status"],
            "cleared",
        )
        self.assertEqual(simulation["clearance_status"]["status"], "cleared")
        realization = simulation["departure_realization"]
        self.assertIsNone(
            realization["demand_period_realization_eligibility"]["eligible"]
        )
        for group in EXPECTED_ROUTES:
            self.assertEqual(
                realization["waiting_to_insert_at_demand_end_by_class"][group],
                realization["clearance_late_departures_by_class"][group]
                + realization["not_departed_by_simulation_end_by_class"][group],
            )
        for detector in simulation["merge_observations"]["detectors"].values():
            measurement = detector["window_summaries"]["measurement"]
            self.assertTrue(measurement["complete_interval_coverage"])
            self.assertGreater(measurement["interval_count"], 0)
            self.assertGreater(len(detector["time_series"]), 0)
        comparison = simulation["queue_window_summaries"]["clearance"][
            "trailing_window_comparison"
        ]
        self.assertIn("observed_delta_direction", comparison)
        self.assertNotIn("still_growing_at_window_end", comparison)
        self.assertIn("does not establish stability", comparison["status"])

    def test_existing_summary_reanalysis_is_read_only_and_supersedes_old_gate(self) -> None:
        runtime = self.runtimes["low_clearance"]
        _, simulation = self.results["low_clearance"]
        source_path = runtime / "source_summary_for_reanalysis.json"
        source = {
            "runtime_directory": str(runtime),
            "simulation": simulation,
        }
        source_text = json.dumps(source, indent=2, sort_keys=True) + "\n"
        source_path.write_text(source_text, encoding="utf-8")
        analysis_path, analysis = reanalyze_existing_summary(source_path)
        self.assertNotEqual(analysis_path.parent, runtime)
        self.assertEqual(
            source_path.read_text(encoding="utf-8"), source_text
        )
        self.assertTrue(analysis_path.is_file())
        self.assertIn("source_assessment_superseded", analysis)
        self.assertIn("fcd_lane_observations", analysis)
        self.assertTrue(
            analysis["fcd_lane_observations"]["accounting"]["reconciliation"]
            ["all_compiled_lanes_grouped_once"]
        )
        self.assertIsNone(
            analysis["revised_technical_assessment"]
            ["demand_period_realization_eligibility"]["eligible"]
        )


if __name__ == "__main__":
    unittest.main()
