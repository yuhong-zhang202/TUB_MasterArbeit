# Reproduce the six archived runs

This guide replays the historical OPEN/V15 comparison at one demand setting.
The inputs and traffic-control logic come from that comparison. The public
entry point resolves paths locally and writes fresh run directories.

## Requirements

- Python 3.10 or later for the replay scripts; the recorded local checks use Python 3.13.
- SUMO **1.26.0**, with `sumo` available on `PATH` or supplied through `--sumo-binary`.
- The matching TraCI module from that SUMO installation's `tools` directory.
- A POSIX system for the runner's process-group cleanup. Windows is not supported by this runner.

Set `SUMO_HOME` to the SUMO installation directory containing `tools/traci`, or
pass `--sumo-home /path/to/sumo`. The replay does not rebuild the network with
netconvert: it uses the supplied historical compiled network. No third-party
Python package is needed beyond the TraCI module shipped with SUMO.

From the repository root:

```bash
python3 scripts/reproduce_stage6.py --check
```

This checks input checksums, vehicle counts, matching demand, network connections,
SUMO version and TraCI availability. It does not run traffic. The output also
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

Generated XML comments and formatting are ignored. The only excluded observation
attribute is `step@duration` in `sumo_summary.xml`, which measures computer
processing time. It is reported separately. All traffic attributes, including
trip durations, and all CSV values are compared exactly at their stored precision.
See [the data guide](data.md) for the full definition.

## What was preserved

The historical compiled network is included with local paths removed from its
generated comment. Each demand file is byte-identical to its historical source.
Input/output references in `.sumocfg` and additional files are relocated; traffic
settings are preserved. The historical feedback and safety modules are included
unchanged. The V15 runtime retains the historical per-step control loop.
Source and copy checksums are in
[`config/reproduction/PROVENANCE.json`](../config/reproduction/PROVENANCE.json).

OPEN holds the ramp signal green. V15 starts control at 600 s, uses the completed
30-second downstream occupancy windows from 630 through 4170 s, and controls the
signal through the 4200 s horizon. Its occupancy target is 11%, gain is
70 vehicles/hour per percentage point, and requested release range is 300–900
vehicles/hour, starting at 900. These are the archived settings, not calibrated
optimal values or a guarantee of actual discharge capacity.

Mainline, urban-through and cross-traffic requests span `[0,3000)` seconds;
ramp requests span `[600,3000)`. Each run plans M=3000, R=600, U=300 and X=150
vehicles. The remaining period to 4200 s allows the network to clear, with
unfinished vehicles retained in the output if any occur.

## Tests

The default test command runs offline checks only:

```bash
python3 -B -m unittest discover -s tests -v
```

The older basic-example integration tests require explicit
`RUN_SUMO_INTEGRATION=1`; they run additional simulations and are separate from
the six archival replays. One legacy static test needs a temporary reference
network and is skipped when that reference is unavailable.

## Recorded replay check

The six archived cases were replayed once each on macOS with SUMO 1.26.0 and
the historical binary checked by SHA-256. No case was retried. Each stayed below
120 seconds and 250 MB. All 123 scientific output files matched the archive
under the comparison rules in [data.md](data.md): XML configuration comments and
formatting are ignored, as is only the wall-clock `step@duration` attribute in
`sumo_summary.xml`. Traffic observations and all CSV fields are compared exactly.
Each case contained all 4,050 planned and arrived vehicles; all 12 summary rows
were independently recalculated and matched.

Five wrapper receipts report `COMPLETED`. For V15 seed 17, the simulation process
finished with exit code 0, but the wrapper failed while importing its post-run
validator. Its original `FAILED` receipt is retained. The import was corrected,
and the existing outputs passed a separate offline validation without rerunning
traffic. The other four cases then used the corrected wrapper; the traffic
runtime was unchanged. [replay_check.json](replay_check.json) records this
distinction, the version hashes and file comparisons.

Offline tests completed with 19 passes and 6 predefined skips (25 tests total).
These checks establish reproduction of this historical exploratory batch on the
checked environment; they do not validate the model against real traffic or test
other operating systems.
