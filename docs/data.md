# Data and verification

The archive contains six historical exploratory runs: OPEN and V15 for each seed 17, 23 and 42. OPEN keeps the ramp signal green. V15 uses the archived ALINEA implementation. Both arms use matching demand and vehicle attributes within each seed. These runs cover one demand setting, not a formal experiment or a calibrated real road. Seed 17 was also used during implementation development.

## What is included

`data/exploratory_stage6/` contains 123 scientific output files. Each OPEN directory contains 18 XML files (including compressed FCD); each V15 directory adds five CSV files. `MANIFEST.json` records the relative historical source, source SHA-256, public-copy SHA-256, size, transformation and omitted auxiliary files. The local project prefix was replaced only in generated XML configuration comments. CSV bytes were preserved. Original file hashes and public-copy hashes therefore have different purposes.

The XML files record vehicle trajectories (`fcd.xml.gz`), lane changes, queue observations, detector observations, signal states, simulation summaries, completed trips (`tripinfo.xml`) and vehicle routes (`vehroute.xml`). The V15 CSV files record controller steps, detector events, feedback updates, service windows and vehicle metadata. Consult their column headers for individual fields. Detector observations and controller commands are not interchangeable with actual vehicle throughput.

Auxiliary process logs and historical execution receipts are not included in the scientific archive. This is an explicit selection, not a claim that every file produced by the original runs is published. Reproduction inputs live in `config/reproduction/`; their provenance is recorded separately there. New replay outputs are kept outside the archived data.

## Vehicle population and paired table

Each run requests exactly 4,050 vehicles: 3,000 mainline (`M`), 600 ramp-bound (`R`), 300 urban through (`U`) and 150 urban cross-traffic (`X`). These are total requested vehicle counts for the demand interval, not hourly flow rates. No vehicles are filtered out of the paired table. All requested vehicles must appear once in both trip and route outputs and arrive by 4,200 seconds for this verification to pass.

`results/paired_results.csv` has one row for each seed and class (12 rows):

- `requested`: number of planned vehicles in that class.
- `open_system_seconds` and `v15_system_seconds`: sum of arrival time minus planned departure time, in vehicle-seconds. This includes time waiting to enter the network.
- `v15_minus_open_system_seconds`: V15 total minus OPEN total for the same seed and class.
- `open_source_wait_seconds` and `v15_source_wait_seconds`: sum of actual departure time minus planned departure time, in vehicle-seconds. This is source insertion delay, not solely physical queueing on roads within the simulated network.

For every vehicle, the verifier independently checks that source waiting equals SUMO's `departDelay`, that arrival minus actual departure equals trip `duration`, and that trip/route departure and arrival agree. Planned IDs come from the supplied demand files; observed outputs cannot define or shrink the population. Duplicate, missing, unexpected, unfinished or vaporized vehicles fail the check. Decimal arithmetic avoids binary floating-point rounding in these totals. There is no statistical significance test or new scientific acceptance threshold here.

## Check the archive or a replay

From the repository root, check archived file hashes, XML/CSV structure, all six vehicle populations and the 12 paired rows:

```bash
python3 scripts/verify_stage6.py --report archive_check.json
```

To additionally compare six completed replay directories with the archive:

```bash
python3 scripts/verify_stage6.py --replay-root /path/to/replays --report replay_check.json
```

The replay directory must contain `open_s17/outputs/`, `v15_s17/outputs/` and the equivalent directories for seeds 23 and 42. The report path must not exist. A successful run prints `PASS_ARCHIVE_ONLY` or `PASS_REPLAY_AND_ARCHIVE`; failures return a nonzero exit code and a failure report. An archive-only pass does not establish that a replay was performed.

XML comparisons preserve element order, every observed attribute and non-whitespace text. Gzip is decompressed before parsing; generated comments, indentation and attribute ordering are ignored. The only attribute excluded from the scientific digest is `duration` on `step` elements in **`sumo_summary.xml`**: this measures computer wall-clock processing time, not traffic travel time. Its separate digest and whether it changed are reported. Trip durations and all other traffic fields remain in the comparison. CSV headers, row order and every field are compared exactly without numeric tolerances.

A replay pass means the archived observation records match at their stored precision in the tested software environment. It does not prove that these exploratory settings are optimal, that other SUMO versions produce identical results, or that simulation outcomes generalize to real traffic.

## Recorded replay check

[The recorded check](replay_check.json) compares all 123 archived scientific files with six local replays on 24 October 2026 using the same SUMO 1.26.0 binary build. All observation records matched under the rules above, all six 4,050-vehicle populations were complete, and the 12 paired rows matched independently recomputed totals. All six native simulation processes exited successfully.

Five wrapper runs completed normally. The V15 seed 17 wrapper retained a failed receipt because a Python import failed during its post-run check, after the simulation had finished. After fixing that import, its existing outputs passed separate structural validation and the full independent comparison; that simulation was not rerun. The report preserves this distinction, original receipt hashes, both wrapper versions and the unchanged simulation-code hashes. It does not describe all six wrapper executions as successful.
