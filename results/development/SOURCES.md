# Sources for the selected development results

These tables are extracts of existing exploratory analyses. `source_table` identifies the original table within the research project; it is a provenance label, not a file bundled in this public repository. See [data and metrics](../../docs/data.md) for definitions and [research findings](../../docs/development_findings.md) for interpretation.

| Public file | Existing source and selection |
| --- | --- |
| `demand_selection.csv` | All 15 rows of `pilot_uncontrolled_transition_20261028_v1/v2/runs.csv`, plus the three R700 rows of `r700_alinea_sensitivity_20261028_v1/phase_a/runs.csv`; selected columns retain the original values. Both source folders are under `results/tables/`. |
| `alinea_sensitivity.csv` | Nine rows from `r700_alinea_sensitivity_20261028_v1/phase_b_v2/runs.csv` and three from `alinea_o15_20261029_v1/final_v1/runs.csv`, under `results/tables/`; Run10 supplies the reused 11%, seed17 observation. |
| `qp900_case.csv` | Selected records from `results/tables/queue_protection_20261029_v1/paired_v1/`; record types retain run, class, window, transition, tracking and flow results. |
| `qp1080_negative.csv` | Selected records from `results/tables/qp1080_20261029_v1/paired_v1/`; includes matched baseline results and the negative candidate result. |
| `run_index.csv` | Run identities from these tables and the existing Stage6 sample; original execution/bridge receipts, the sensitivity completion record and QP900 report provide statuses. It also lists the interrupted 9%, seed23 attempt, its prelaunch failure and the unexecuted QP900 V1 card. |

The QP exports combine different record types in a wide table: blank cells mean that a field does not apply to that record. Added run identifiers were cross-checked against the QP1080 comparison table (`run_id_source_table`). This links reused observations; it does not imply new simulation runs. Every populated field shared with its source table was checked for exact agreement during this documentation update. No metrics were recomputed.

The three existing figures are retained unchanged: `demand_location.png` shows the original R750–R950 pilot, `alinea_costs.png` the 9/11/13% comparison, and `qp900_mechanism.png` the paired QP900 case. The later R700 and 15% results are available in the tables and narrative rather than added to these historical figures.

`run_index.csv` covers the selected stages, not every historical development attempt. `COMPLETED` means that the run reached its planned horizon, not that all vehicles exited or all actuator diagnostics passed. The original status remains in a separate column. Failed or unexecuted identities have no complete-run result file. The earlier Stage6 sample and later development runs use different execution implementations.

Known execution commits are recorded only where the run report explicitly identifies them: Run10 (`660795988ceb417a45c2262481fa280a3d812fdb`), QP1080 (`2f81aaed750737fc15a042e13a0fc6452d493620`) and 15% sensitivity (`2891d622cc7100f15cf26ceaa9ef6b400d2b211a`). Other execution commits are `UNKNOWN`; report commits are not substituted. These identifiers document provenance but do not imply that the corresponding execution code is published here. The public Stage6 replay adaptation is described by [its provenance manifest](../../config/reproduction/PROVENANCE.json), which preserves historical file hashes rather than an established execution commit. See [reproduction](../../docs/reproduction.md) for the runnable public scope.

## Public inputs behind the figures and cycle diagnosis

The following small derived datasets make the plotted values and the cycle-level
negative result available without the private raw trajectories. Values are selected
from existing analysis outputs; no simulation metrics were recalculated.

| File | Contents and units | Source selection |
| --- | --- | --- |
| `pilot_speed_cells.csv` | 46,200 rows: 15 runs × 22 spatial cells × 140 time bins. `begin`/`end` are seconds, `cell` is a zero-based 100-metre spatial index, `x_start` is metres, `speed_mps` is M vehicle-second-weighted speed in m/s, and `samples` counts contributing vehicle-seconds. Run, seed and nominal ramp demand (veh/h) identify each panel. | `mainline_cells.csv` under `data/processed/pilot_uncontrolled_transition_20261028_v1/<run_id>/`; only `lane_track=pooled`, as in the original plot. Empty speed values are retained as missing observations, not zero speed. R700 is outside this historical figure. |
| `qp900_timeseries.csv` | 3,600 one-second observations, `time_s=600…4199`. `qp900_*` fields contain the nominal ALINEA request, final QP request (veh/h), active flag and risk extent (m). `alinea13_*` fields contain the matched pure-ALINEA request and risk. | QP900 `paired_v1/queue_decisions.csv`, joined by `time_s` to the seed17 baseline `shadow_selection_v1/DEV_20261028_M3600_R900_ALINEA_O13_S17_shadow.csv`, both under `results/tables/queue_protection_20261029_v1/`. Only the baseline's observed risk and nominal request are used; its hypothetical shadow activation is not presented as an actual intervention. |
| `qp900_meter_passages.csv` | 1,200 event rows, 600 per run. `time_begin_s` and `time_end_s` delimit the one-second front-passage observation interval. Multiple events in one interval remain separate rows; vehicle identifiers are omitted. | Existing `meter_entries` arrays in each run's `CONTROL_MEASUREMENTS.json`, under `data/processed/r700_alinea_sensitivity_20261028_v1/<baseline_run>_control/` and `data/processed/queue_protection_20261029_v1/<QP900_run>_control/`. |
| `qp1080_cycles.csv` | All 1,216 cycle records across ALINEA13, QP900 and QP1080. Times and durations use seconds; `quota`, `N`, `N_G`, `N_y`, `N_r`, `deficit`, `excess`, initial near-line supply and streaks use vehicle/cycle counts as named. | Complete `results/tables/qp1080_20261029_v1/paired_v1/cycles.csv`; no filtering of incomplete, denied or deficient cycles. |

The demand-selection table now also includes each class's existing
`*_planned_4200` count and `*_system_time_mean_s` (seconds per planned vehicle).
Its R900 means match the OPEN values in the original sensitivity plot. The
existing sensitivity table supplies all controlled plotted means; the retained
cost figure still covers OPEN and 9/11/13%, while 15% remains in the table.

In the cycle table, `quota` is the scheduled allowance, and `N` counts actual
front passages (`N_G`, `N_y`, `N_r` partition these by signal state). `executed`
and `complete` distinguish observed full cycles from denied or partial cycles;
do not treat every row as a full executed allowance. `initial_nearline_R` counts
visible ramp vehicles in the last 20 metres at cycle start, not supply throughout
the cycle. `receiver_decisions` preserves the recorded READY/WAIT/UNOBSERVED counts
in JSON text. WAIT alone does not establish a downstream blockage, and UNOBSERVED
is not evidence of free reception. The corresponding observed/unobserved sample
counts remain explicit. Blank denial reasons mean no recorded denial; other
blank fields are not converted to zeros. The existing `qp1080_negative.csv`
contains the complementary fixed-window C/E/N, complete-cycle groups and
source-to-meter stock/risk comparisons, so those records are not duplicated here.

### Replotting only

From the repository root, with NumPy and Matplotlib available:

```bash
python results/development/plot_figures.py --output-dir /tmp/ramp-development-figures
```

The output directory must not already exist. The script reads only public CSVs
and writes three images without overwriting the retained originals. It reuses the
historical plotting logic, including the Pilot 0–45 m/s scale and the QP figure's
cumulative count reconstructed from passage-event times. This is a plot check,
not a rerun of the original simulation or trajectory analysis. Rendering can
vary with plotting-library versions.

The source plotting functions were checked against these file SHA-256 values;
they identify files, not execution commits:

| Original plotting source | SHA-256 |
| --- | --- |
| `scripts/analysis/pilot_uncontrolled_transition_20261028/synthesize.py` | `8eff11165ff5ee8346009550e9b95c70f69504a3d2d22872d7e0d44bd976a1fd` |
| `scripts/analysis/r700_alinea_sensitivity_20261028/synthesize_phase_b.py` | `c6a867153bd2bb955e6f2f0d88f298dbaaf45349f4eaedd94c8b36499e80d60c` |
| `scripts/analysis/queue_protection_20261029/analyze_paired_run.py` | `34c25d9e762f4c20cb204d74ebba10f5ae5ecf5e0892430baabf23965ecb634b` |

The index's `analysis_commit` is `UNKNOWN` where no analysis execution commit was
established. Run10 records its verified retained measurement-reader commit
`ed8573221d49e9061774c569a1e82237f09283fd`; this does not identify every subsequent
comparison or plotting script. These historical versions are distinct from the
new public replotting helper.
