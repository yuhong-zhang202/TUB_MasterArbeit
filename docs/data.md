# Data and measures

The repository provides two kinds of material: a replayable six-run historical
sample, and selected results from the later development studies. Start with the
[research narrative](development_findings.md) for why the experiments were done.

## Find an experiment or result

The [run index](../results/development/run_index.csv) identifies each case's stage,
strategy, demand, target, seed, planned duration, observed endpoint, original
status and result files. A completed run and an interrupted or unstarted attempt
have separate entries. A known code identity is recorded where available;
`UNKNOWN` means that the execution or analysis commit has not been established.
A report's commit is not a substitute for the version that executed the run.

| File | What to compare |
|---|---|
| [demand_selection.csv](../results/development/demand_selection.csv) | OPEN demand levels, individual seeds, congestion measures and time costs |
| [alinea_sensitivity.csv](../results/development/alinea_sensitivity.csv) | All twelve complete 9/11/13/15% cases, including terminal residuals |
| [qp900_case.csv](../results/development/qp900_case.csv) | Matched ALINEA/QP900 costs, actual crossings, activation and accuracy windows |
| [qp1080_negative.csv](../results/development/qp1080_negative.csv) | ALINEA/QP900/QP1080 comparison, nominal versus actual service, stock and risk |
| [paired_results.csv](../results/paired_results.csv) | The earlier replay sample: three seeds and four vehicle classes |

The QP files combine several small source tables. Use `record_type` to select
runs, class costs, cycles, windows or transitions; blank fields belong to other
record types. `source_table` identifies the original relative table name, not a
promised file in this checkout. [Source notes](../results/development/SOURCES.md)
explain the selection and provenance. The complete later controllers and large
raw development outputs are not distributed here. Questions about additional
material can be raised through this repository's Issues; access is not automatic.

## Supporting data and figures

The [source notes](../results/development/SOURCES.md) describe fields, units and
selection rules for four additional files: Pilot speed cells, QP900 time series
and meter-passage intervals, and the complete QP1080 cycle table. They retain
missing observations and partial cycles. Together with the summary tables,
these CSVs supply the three published development figures. The
[plotting script](../results/development/plot_figures.py) can redraw them using
only public data; the source notes give the command and dependencies.

Large raw outputs for the later Pilot, sensitivity and QP studies are retained
in private archives. Requests for specific material can be made through Issues
and considered individually. Six early Stage2 runs have missing original raw
files; retained diagnostic summaries do not make those runs replayable. The
six-run Stage6 sample linked below is a separate, publicly supplied dataset.

## Conditions and vehicle groups

`M` denotes freeway mainline vehicles, `R` ramp-bound vehicles, `U` urban-through
vehicles on the shared approach, and `X` cross traffic. Mainline demand is the sum
across both freeway lanes. Comparisons between strategies match demand and seed;
comparisons across demand levels also change the number of planned vehicles.

The later runs use a 4,200-second horizon. Mainline, urban-through and cross-traffic
demand is scheduled over [0,3000) seconds; ramp demand over [600,3000). Some nominal
ramp rates need whole-vehicle adjustment: 700/800/850/950 vehicles/hour correspond
to 467/533/567/633 scheduled ramp vehicles, or 700.5/799.5/850.5/949.5 vehicles/hour
within the loading interval. Scheduled demand, actual network entry and actual
meter discharge are three different quantities.

## Time costs and vehicles remaining at the end

A vehicle's **system time** starts at its scheduled departure. It includes waiting
outside the simulated road network (**source waiting**) and time travelling or
waiting inside it. Totals are in **vehicle-seconds**: one vehicle spending ten
seconds contributes ten vehicle-seconds. Source waiting is not physical queueing
on a road within the network.

Development cost tables cover [0,4200). Vehicles that have not arrived by the end
contribute their observed time up to 4,200 seconds; vehicles still waiting to enter
also remain in the accounting. Class means divide by all planned vehicles in that
class, not just those that arrived. Thus a 9% case with residual vehicles has a
finite-horizon cost, not a completed-trip mean. Its eventual total cost would
include additional time after the observation ends.

`unfinished` counts vehicles still in the network; `undeparted` counts planned
vehicles not yet inserted. These are distinct and neither is discarded. In the
historical six-run replay sample, all 4,050 planned vehicles arrived, so its
`system_seconds` totals cover complete journeys. Its population is M=3,000,
R=600, U=300 and X=150 per run. Later tables retain their own population counts.

The main diagnostic window is [1200,3000), lasting 1,800 seconds. Window exposure
costs are time accumulated within the interval, not the journey times of only
vehicles entering during it. Whole-run and window costs should be read separately.

## Requests, signal quotas and actual discharge

`C` integrates the requested metering rate over time into vehicle-equivalents.
`E` records the signal's nominal vehicle allowance; scheduled and executed
allowances are separate fields. `N` counts actual vehicle fronts crossing the
meter. The observed rate is `3600 * N / window_seconds`. A command or allowance
is not a measurement of capacity.

For ALINEA with QP, the nominal ALINEA request and the final overridden request
are both retained. The QP1080 complete-cycle rate uses the 234 candidate cycles;
its main-window rate uses [1200,3000), which also includes time outside that mode.
Incomplete terminal cycles remain marked as partial rather than credited with a
full allowance.

The original local accuracy diagnostic was `abs(N - C) <= max(2, 0.05 * C)`
vehicles per evaluation window. Failed windows remain failures and their runs
remain in the traffic comparison. This is a project diagnostic, not an industry
accuracy standard. Whole-period totals can also reflect a lack of vehicles to
serve, especially after loading ends.

## Congestion, stock and risk

The tables retain three exploratory congestion labels, P/L/S, based on existing
space-time rules. The research narrative uses S, the severe-state measure.
Mainline observations are grouped into 100-metre by 30-second cells across the
two mainline lanes, including straight internal connections and excluding the
auxiliary lane. Vehicle speed is divided by its speed factor times the lane's
speed limit; that reference is not a measured free-flow speed.

P/L/S use relative-speed thresholds of 0.7/0.8/0.6 and minimum continuous durations
of 90/60/120 seconds. A qualifying cell requires at least two distinct mainline
vehicles, at least half its vehicle-time at or below the threshold, and at least
15 seconds with two slow mainline vehicles present together. Spatial support
requires a persistent neighbouring-cell pair involving the 1,300–1,800-metre
core, or a core cell with density at least 1.25 times its eligible [300,600)
baseline median. Support seconds count the union of qualifying intervals within
[1200,3000). They do not mean that every part of the freeway was congested for
that duration, and three seeds do not establish a congestion probability.

QP's risk measure is the furthest upstream rear position of slow ramp-bound
vehicles in the monitored storage and diverge lanes, expressed as a distance
from the meter. It is a spatial extent rather than a queue count. The source-to-meter ramp population includes moving
vehicles as well as waiting ones; its change follows actual insertions minus meter
crossings. Source waiting is recorded separately, so a flat in-network population
cannot be mistaken for queue clearance when vehicles are blocked outside.

## Historical replay files and verification

The [archive manifest](../data/exploratory_stage6/MANIFEST.json) lists 123 scientific
files: 18 XML outputs per OPEN run and five additional control CSVs per V15 run.
XML contains trajectories, lane changes, queues, detector measurements, simulation
summaries, trips and routes. The manifest records source/copy hashes and the
replacement of local project paths in generated comments. Scientific CSV contents
are unchanged. Auxiliary process logs are not part of this selected data package.

[Replay provenance](../config/reproduction/PROVENANCE.json) separately records
input and code copies. The current public wrapper adapts paths and output handling;
its published version is not assumed to be the original execution wrapper.

XML comparisons preserve element order, every observed attribute and non-whitespace text. Gzip is decompressed before parsing; generated comments, indentation and attribute ordering are ignored. The only attribute excluded from the scientific digest is `duration` on `step` elements in **`sumo_summary.xml`**: this measures computer wall-clock processing time, not traffic travel time. Its separate digest and whether it changed are reported. Trip durations and all other traffic fields remain in the comparison. CSV headers, row order and every field are compared exactly without numeric tolerances.

A replay pass means the archived observation records match at their stored precision in the tested software environment. Compatibility with another SUMO build or platform requires its own check.


The [reproduction guide](reproduction.md) gives the archive-check and replay
commands and the single recorded replay history, including the wrapper failure
that occurred after one simulation had finished.
