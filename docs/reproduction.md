# Reproduce the six archived runs

This guide covers the earlier six-run OPEN/V15 sample and the smaller basic
example. For the later development results, start with [research progress](development_findings.md).
V15 is the historical implementation label; its target is 11%.

## Requirements

- Python 3.10 or later for the replay scripts; the recorded local checks use Python 3.13.
- SUMO **1.26.0**, with `sumo` available on `PATH` or supplied through `--sumo-binary`.
- The matching TraCI module from that SUMO installation's `tools` directory.
- A POSIX system for the runner's process-group cleanup. Windows is not supported by this runner.

Set `SUMO_HOME` to the SUMO installation directory containing `tools/traci`, or
pass `--sumo-home /path/to/sumo`. The replay does not rebuild the network with
netconvert: it uses the supplied historical compiled network. No third-party
Python package is needed beyond the TraCI module shipped with SUMO.

## Check files before running traffic

With Python alone, check the supplied archive and write a new report:

```bash
python3 scripts/verify_stage6.py --report archive_check.json
```

This reads the archive without invoking SUMO. Once SUMO is installed, check the
replay inputs and environment:

```bash
python3 scripts/reproduce_stage6.py --check
```

This checks input checksums, vehicle counts, matching demand, network connections,
SUMO version and TraCI availability. It calls `sumo --version` and imports TraCI,
but does not simulate traffic. The output also
reports the SUMO binary checksum. To require the exact historical binary build:

```bash
python3 scripts/reproduce_stage6.py --check --require-historical-build
```

That stricter option is useful in the original environment. A SUMO 1.26.0 binary
built for another machine can have a different checksum; matching the version
alone is not a guarantee of identical outputs.

## Run all six cases

Each destination must be new and lie under the system temporary directory. The
runner rejects destinations inside the source/archive package and existing run
directories. Create one temporary parent, then run all six cases beneath it:

```bash
replay_root="$(python3 -c 'import tempfile; print(tempfile.mkdtemp(prefix="tub-replay-"))')"
python3 scripts/reproduce_stage6.py --mode OPEN --seed 17 --run-dir "$replay_root/open_s17"
python3 scripts/reproduce_stage6.py --mode V15  --seed 17 --run-dir "$replay_root/v15_s17"
python3 scripts/reproduce_stage6.py --mode OPEN --seed 23 --run-dir "$replay_root/open_s23"
python3 scripts/reproduce_stage6.py --mode V15  --seed 23 --run-dir "$replay_root/v15_s23"
python3 scripts/reproduce_stage6.py --mode OPEN --seed 42 --run-dir "$replay_root/open_s42"
python3 scripts/reproduce_stage6.py --mode V15  --seed 42 --run-dir "$replay_root/v15_s42"
```

Every run has a 120-second wall-clock limit and a 250 MB directory-size limit,
including copied inputs. These are safeguards for this small reproduction package,
not simulation-time settings. A slower machine may reach the time limit. A stopped
or failed run is recorded and must not be counted as a successful replay.

Each run directory contains the input snapshot, `outputs/`, and `receipt.json`
with software identity, input checksums, exit status and output checksums. Run
records contain local paths; keep newly generated directories out of a public
commit unless those paths have been reviewed.

## Compare outputs and the paired table

After the six runs finish:

```bash
python3 scripts/verify_stage6.py --replay-root "$replay_root" --report "$replay_root/comparison.json"
```

The report file must not already exist. The verifier compares all 123 archived
scientific files with their replay counterparts and independently rebuilds the
12 rows of `results/paired_results.csv` from the full planned vehicle population.
It fails on missing, duplicated, unexpected or unfinished vehicles.

See [comparison rules](data.md#historical-replay-files-and-verification) for the
XML/CSV matching rules and the treatment of computer processing time.

## What was preserved

The historical compiled network is included with local paths removed from its
generated comment. Each demand file is byte-identical to its historical source.
Input/output references in `.sumocfg` and additional files are relocated; traffic
settings are preserved. The historical feedback and safety modules are included
unchanged. The V15 runtime retains the historical per-step control loop.
Source and copy checksums are in
[`config/reproduction/PROVENANCE.json`](../config/reproduction/PROVENANCE.json).

OPEN holds the ramp signal green. V15 starts control at 600 seconds and uses
completed 30-second downstream occupancy windows from 630 through 4170 seconds.
Its archived target is 11%, its gain is 70 vehicles/hour per percentage point,
and its request range is 300–900 vehicles/hour, starting at 900. The run ends at
4200 seconds. These settings reproduce the historical sample. Population and
loading intervals are defined in [the data guide](data.md#conditions-and-vehicle-groups).

## Explore the basic example

The basic scenario builds its own smaller network and also needs `netconvert`
on `PATH`. For this example, `SUMO_HOME` must point to a directory containing
both `bin` and `tools`. It has been checked on macOS with Python 3.13 and SUMO 1.26;
its output directory uses the macOS temporary area.

```bash
python3 src/scenarios/run_minimal_uncontrolled.py --validate-only
python3 src/scenarios/run_minimal_uncontrolled.py --profile low
```

The first command compiles a network with netconvert, generates inputs and checks
versions without simulating traffic. The second runs the low-demand example.
Both print their new temporary output location. Use the replay commands above
for the six archived cases; the basic example is a different scenario.

## Tests

The default test command runs offline checks only:

```bash
python3 -B -m unittest discover -s tests -v
```

The older basic-example integration tests require explicit
`RUN_SUMO_INTEGRATION=1`; they run additional simulations and are separate from
the six archival replays. One legacy static test needs a temporary reference
network and is skipped when that reference is unavailable.

## Recorded replay check: 24 October 2026

[replay_check.json](replay_check.json) records six local replays on macOS with
the historical SUMO 1.26.0 binary. All 123 scientific output files matched under
the documented comparison rules. Each case contained all 4050 planned and arrived
vehicles; all twelve paired result rows matched independently rebuilt totals.

Five wrapper receipts were `COMPLETED`. For V15 seed 17, SUMO exited successfully,
but the wrapper failed to import its post-run validator. That `FAILED` receipt
is retained. After fixing the import, the existing outputs passed offline
validation; the simulation was not rerun. The report records the wrapper versions
and unchanged traffic-code hashes.

The recorded offline suite had 19 passes and six predefined skips. These results
refer to that historical check, not to a new replay performed during the current
documentation update. Matching outputs in this environment does not establish
identical results on other SUMO builds or a calibrated real-road model.
