# From demand exploration to queue protection

The study asks when restricting an entrance ramp helps freeway traffic, and how
much waiting it transfers to ramp-bound and urban-through vehicles. The following
stages progressively narrowed the comparison in one synthetic network. They use
three seeds for demand and target sensitivity, and one matched seed for each
queue-protection test.

[The experiment index](../results/development/run_index.csv) links individual
runs to their conditions, status and result files. [Data and measures](data.md)
explains the common time-cost and discharge definitions. The earlier public
OPEN/V15 replay sample is a separate dataset: V15 is an implementation label,
not a 15% occupancy target.

## 1. Uncontrolled Pilot and demand selection

Before comparing controllers, the study needed demand conditions ranging from a
lighter load to persistent freeway congestion. Mainline demand was held at 3,600
vehicles/hour across the two freeway lanes. Nine new OPEN runs—no ramp metering—
covered ramp demands of 800, 850 and 950 vehicles/hour with seeds 17, 23 and 42. Six
existing runs at 750 and 900 provided the endpoints.

The results showed a transition that depended on the seed. At 750, none of the
three runs met the severe-congestion condition. At 800, one did; at 850, two did;
at 900, all three had prolonged severe congestion. Seed 42 was severe at 800 but
not 850, so these observations do not define a single deterministic threshold.
At 950, congestion and waiting to enter the mainline increased further.

![Mainline speed across the OPEN demand comparison](../results/development/figures/demand_location.png)

*Five demand levels by three seeds, over 0–4,200 seconds. Colours show mainline
vehicle-time-weighted speed in 100-metre by 30-second bins, with a shared 0–45 m/s
scale. Grey marks bins without mainline observations, rather than stopped traffic.*

A subsequent three-seed check at 700 supplied a lighter reference. Together, these
studies motivated the proposed 700/800/850/900 demand set: one lighter point, two
seed-sensitive transition points and a heavier endpoint. The
[demand table](../results/development/demand_selection.csv) retains each seed.
Different demand levels contain different numbers of vehicles, so their raw total
time costs are not a matched estimate of a controller's effect.

## 2. ALINEA target-occupancy sensitivity: 9%, 11%, 13% and 15%

The next question was how strongly to protect the freeway. ALINEA adjusts the
requested ramp release rate using downstream occupancy—the fraction of time a
detector is occupied. Above the target it reduces the request; below the target it
increases it. The tests used a 30-second feedback interval and a 300–900 vehicles/hour
request range at mainline/ramp demand 3,600/900 vehicles/hour.

The 9/11/13% comparison used matched seeds 17, 23 and 42. The 11% seed 17 result reused
a compatible earlier run. Lower targets protected the mainline more strongly but
increased ramp and urban costs. At 9%, vehicles were still waiting to enter or
remaining in the network at 4,200 seconds. Raising the target to 13% substantially
reduced total costs relative to 11% in all three cases, making 13% the working reference for the
queue-protection tests.

![Earlier ALINEA 9/11/13% cost comparison](../results/development/figures/alinea_costs.png)

*The retained figure shows the earlier 9/11/13% comparison. Seed-level 15% results
are included in the table below and the [complete sensitivity CSV](../results/development/alinea_sensitivity.csv).*

After the queue-protection studies described below, three additional 15% runs
completed the target comparison. They reduced ramp and urban time costs relative
to 13%, but mainline costs increased in every seed. Total cost increased for
seeds 17 and 23 and fell slightly for seed 42:

| Seed | 13% total cost (vehicle-s) | 15% total cost (vehicle-s) | 13% mainline cost (s/planned vehicle) | 15% mainline cost (s/planned vehicle) |
|---|---:|---:|---:|---:|
|17|519,232|569,608|85.15|138.09|
|23|437,638|545,011|78.57|126.70|
|42|364,995|359,979|76.52|77.97|

Compared with OPEN, 13% reduced total cost for seeds 17 and 42 but increased it
for seed 23. Its benefit was therefore seed-dependent even at this demand point.
The target comparison supported retaining 13% as the proposed compromise,
rather than extending the target search. It does not identify a global optimum.
The interrupted 9% seed 23 attempt and its pre-start failure are retained in the
index; only its complete replacement contributes a full 4,200-second comparison.
Existing discharge-accuracy failures remain in the record and were not grounds
for removing a run.

## 3. QP900 validation

The 13% cases still left room to ask whether the ramp could be protected from
excessive accumulation. Queue Protection (QP) temporarily overrides ALINEA's
request when vehicles extend too far upstream. Separate activation and release
conditions prevent rapid switching. The first candidate raised the request to 900
vehicles/hour while protection was active.

One natural test used the same 3,600/900 demand,13% target and seed 17 as its ALINEA
baseline. Seed 17 was selected from existing trajectories because it offered a
clear opportunity to increase the requested rate. Protection activated at 1,277 s;
the request was applied at the next eligible cycle boundary,1,286 s.

![QP900 activation, requests and queue-risk observations](../results/development/figures/qp900_mechanism.png)

*The mechanism plot distinguishes ALINEA's nominal request from the request after
QP, and follows the spatial risk measure. A request is not an observed discharge.*

During the 1,200–3,000 s comparison window, actual meter crossings increased from 352
to 419 vehicles, or 704 to 838 vehicles/hour. Over the whole run, both cases served the
same 600 ramp vehicles; QP changed their timing. Total time cost fell from 519,232
to 434,894 vehicle-seconds. Ramp and urban costs fell, while mean mainline cost rose
slightly from 85.151 to 85.437 seconds per planned vehicle.

These observations established a useful mechanism case for the next comparison.
Protection released at 3,229 s, after scheduled demand ended at 3,000 s. That timing
left an unresolved question: could a 900 request reduce an existing backlog while
arrivals continued? The [QP900 table](../results/development/qp900_case.csv)
contains the paired costs, crossings, transitions and accuracy windows for this
single case.

## 4. QP1080 negative result

The remaining question was whether a higher temporary request could provide enough
actual service to reduce the backlog during loading. The fixed candidate allowed
three vehicles per 10-second cycle: at most 5 seconds green,3 seconds yellow and at
least 2 seconds red. The nominal rate was therefore 1,080 vehicles/hour. Ordinary
ALINEA settings, traffic inputs and the QP trigger stayed the same.

In the one matched seed 17 test, every one of 234 complete candidate cycles served
only two vehicles. The actual rate over these cycles was 720 vehicles/hour, despite
at least three vehicles being visible near the stop line at each cycle start.
The third-vehicle passage and its yellow transition were therefore not observed.

| Strategy | Meter crossings, 1,200–3,000 s (vehicles) | Rate over that window (vehicles/hour) | Total cost, 0–4,200 s (vehicle-s) | Source waiting, 0–4,200 s (vehicle-s) |
|---|---:|---:|---:|---:|
| ALINEA 13% |352|704|519,232|31,682|
| ALINEA 13% + QP900 |419|838|434,894|1|
| ALINEA 13% + QP1080 |351|702|535,492|64,395|

The 702 vehicles/hour window rate and the 720 vehicles/hour complete-cycle rate use
different time intervals. Neither is the nominal 1,080 request.

From QP activation to the end of scheduled loading,368 ramp vehicles entered and
344 crossed the meter. The ramp-bound in-network population upstream of the meter
rose from 42 to 66, while another 62 vehicles were waiting outside the network by
3,000 s. The release-risk threshold was not reached during that interval. These
observations show accumulation, not queue clearance.

The higher nominal request thus produced less useful service and greater total
cost in this fixed case. The candidate was stopped and retained as a negative
result; this does not rule out every possible three-vehicle timing scheme. The
[QP1080 table](../results/development/qp1080_negative.csv) preserves the matched
comparison, complete-cycle counts, stock changes and failed accuracy windows.

## Next comparison and available material

The proposed next study compares OPEN, ALINEA and ALINEA with QP at four ramp
demands, using ten new matched seeds:120 runs. The target setting and treatment
of QP's service limitation are still being finalised; formal experiments have not
started.

The public development material consists of selected tables and figures with
[source notes](../results/development/SOURCES.md). The complete later controllers
and large raw development outputs remain outside this public release. For a
runnable example, use the earlier [six-case reproduction package](reproduction.md).
