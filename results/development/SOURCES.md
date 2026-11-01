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
