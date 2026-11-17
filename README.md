# Jessie MCU carrier and AutoPCB

Jessie v4.1 is the SRILab/BOSUN microcontroller carrier and its KiCad design workspace. AutoPCB is the accompanying set of scripts for inspecting, planning, checking, and reviewing PCB work.

The board is a **work in progress and remains on manufacturing hold**. The review images are snapshots of saved files; they do not establish electrical safety, connector fit, or fabrication readiness.

[View the board and schematic](../deliverables/review/index.html) · [Routing progress](../deliverables/review/routing_progress.png) · [Algorithm PDF](docs/algorithm.pdf) · [Algorithm source](docs/algorithm.md)

## Using this repository

For a new checkout, clone this repository as the KiCad project's lower-case
`autopcb/` folder, beside the native KiCad files and project libraries. AutoPCB
includes the scripts, configuration, design inputs and its own documentation.
The parent project supplies the PCB, schematic, custom KiCad libraries and
deliverables. Routing runs are generated locally in `autopcb/results/`.

Run `python -B autopcb/scripts/run.py status` from the KiCad project directory
to verify the working project and its release inputs.

## Design at a glance

- One KiCad schematic, `v4.1.kicad_sch`, and one six-layer PCB, `v4.1.kicad_pcb`.
- Teensy 4.1 and RTC modules on a carrier with separate regulated 5 V and 12 V inputs.
- Lighting, sensors, timing, and control interfaces are described in [design](design/). Connector and harness compatibility still need physical verification.
- Current working rules keep the schematic, all component placement, and front silkscreen fixed. Working saves independently reject changes to bottom footprints as well as top footprints.
- The active policy considers all six copper layers at equal routing cost, and a through via has the same transition cost between any pair. Native layer access and reference checks derive the physical order from the enabled stack. Actual stackup, local reference-copper reservations, and electrical rules still apply.
- The active working-connectivity stage routes the explicitly listed native net classes under saved width/clearance rules. USB/Ethernet pair copper, intentional no-connects and user exclusions remain protected. This stage does not supply missing current, voltage, protection or signal-integrity evidence, and cannot satisfy electrical or manufacturing completion gates.
- Inner-layer high-speed traces reserve local space on both adjacent layers. Sampled ground coverage remains an unverified geometric check, not an impedance or return-path qualification.

Open `../v4.1.kicad_pro` in KiCad to inspect or edit the design. The requirements in `docs/requirements.md` and materials under `design/` include historical decisions; check their source dates and hashes against the saved design before relying on them. The routing progress table records measurements from the saved PCB.

## What AutoPCB can do

- Inspect KiCad objects and prepare source-bound JSON campaigns by reference or UUID; review the changes before applying them. Batch lookups are indexed once per campaign.
- Plan bounded-pin routing checkpoints from the saved PCB and its rules, with configurable priorities and search budgets. Each selected pin can also be reached through existing copper on its own connected island.
- Search whole nets or individual checkpoints on temporary copies; evaluate candidate connectivity and KiCad DRC without automatically changing the live PCB. Optional topology guidance and weighted search can find candidates faster, without proving a global optimum. A bounded cleanup can remove extra bends while preserving vias and existing contacts. Checkpoints also try cleanup after subtracting already-connected copper, retaining the original as a fallback for native validation. Configure the per-path limit with `search.smoothing_trials`; set it to zero to disable the pass. When a new branch meets an ordinary same-net track, these planners can split the existing unlocked, ungrouped segment into equivalent pieces so KiCad sees the junction; nonstandard or protected segments are refused.
- Try explicitly permitted bottom-side poses with routing alternatives, and create source-bound campaigns for reviewed changes.
- Inspect libraries, copper, routing, power, and placement; maintain review PDFs, PNGs, and the progress chart.
- Build selected Markdown documents as PDFs from `config/pdf_build.json`.

The checkpoint planner currently handles **one net per checkpoint**. Its ordering is a bounded heuristic. A checked, equal-open rearrangement can be tried on a disposable branch with up to two fresh follow-ups; only an independently rechecked reduction in opens can replace the retained preview. Global board-level lookahead, runtime prediction, and silkscreen optimization remain unfinished. A search that finds no route does not prove that a route is impossible. Current and voltage limits, critical signal paths, protection, and physical fit must be verified independently.

Within each saved-rule profile, consecutive clearance checks for the same track
reuse its courtyard-intersection geometry. The cache holds one track at a time;
obstacle-specific rules and uncertain courtyards are still evaluated separately.

Unexpected errors and unsupported repair requests stop the run and retain the
last committed preview and queue. Exhausted or time-limited searches remain
unresolved results rather than evidence that a route is impossible.
Native candidate validation also rejects newly introduced routing-style
defects. An exactly restored existing staircase can be used temporarily, but
existing staircases still prevent final completion.

Standard two-chamfer doglegs such as `0 -> 45 -> 90 -> 45 -> 0` are allowed.
The staircase check examines the complete neighboring runs, so it distinguishes
those doglegs from repeated ladder steps. Search, cleanup and independent saved
board checks share this exception. The odd-multiple-of-45-degree bend rule,
native clearance rules and electrical completion holds are unchanged.
The default 0.20 mm search width is subject to the saved net-class and custom
rules and any explicit electrical floor. In the present v4.1 rules, ordinary
core signals use 0.20 mm and ordinary field signals use 0.25 mm outside saved
local exceptions. These dimensions are design rules, not electrical qualification.

`python run.py pcb branch-widths` previews terminal-aware width rules;
add `--apply` to publish them after native checks. The maintained
`config/branch_widths.json` distinguishes local control rails, eFuse outputs,
Ouster feeds and named sensing/indicator terminals. The graph includes direct
contacts between pads, tracks, vias and net-assigned copper graphics. A branch
cut must leave the main feed connected and isolate only bounded terminals. A
group of traces can share one retained landing when all secondary contacts
lie inside that landing. The C40 branch on the raw LiDAR rail keeps both
1.45 mm floors under this check.
The generated rules match exact saved geometry. After editing a branch,
rerun the compiler and recheck any affected per-segment floors. Fixed geometry
rules for shared copper remain in the rule file. AutoPCB saves only the rules.
Explicit net-specific custom minima override net-class routing presets in search;
independently supplied electrical floors remain hard limits. These are normal-load
design targets, with fault, thermal, via and manufacturing qualification still open.
The 3V3 distribution target is 0.30 mm minimum / 0.50 mm preferred for the
250 mA external-load allocation. Timing distribution is 0.40 / 0.50 mm for the
500 mA regulator sizing case; documented package escapes remain narrower.
These allocations do not establish actual load, source-fault limits or voltage drop.

`python run.py pcb repair-widths --apply` widens existing ordinary segments in
checked batches. It tries configured preferred widths, falls back to required
native minima, and partitions interfering edits. It preserves centerlines, vias,
all footprints, front silkscreen and the schematic. Every retained batch must
preserve native connectivity, other DRC findings and sampled high-speed reference
copper. Obstructed traces remain violations for subsequent rerouting; they are
not waived. The repair command writes its result to `results/width_repair.json`.

`python run.py pcb repair-widths --prune-covered --apply` removes flagged segments
whose entire copper shape is already covered by retained pads, tracks, always
flashed plated lands, or freshly filled zones on the same net and layer.
The script checks retained routing, plated lands and the next native zone fill.

`python run.py pcb repair-widths --trim-terminal-ends --apply` removes short
redundant tips only when a proven low-current trace already lands on the same
surface-mount pad and the tip contacts no other conductor. It preserves the
saved zone fills and rechecks native connectivity, spacing, and return copper
with a private refill.

`python run.py pcb repair-widths --reroute --apply --max-checks 12`
instead searches local detours around obstructing copper at the native required
width. It replaces uniform-width series runs together, stopping at pads, vias,
branches and protected copper, and emits the saved wider distribution and
short package-escape dimensions separately. Every request is derived from the current DRC inventory. This bounded
stage preserves all component poses and text and adds no vias.
At an unbranched SMD contact it can try other landings on the same pad. The
candidate must retain the same native copper contacts and pass the same checks.
It may move a pure series run between its existing end vias onto another enabled
layer only when both used barrel paths shorten, copper is at least as thick,
and no additional current branch is connected. It
skips USB/Ethernet signal nets, and cannot increase nominal track resistance.
The search considers the saved six-layer stack with equal layer costs and
uses one layer per candidate, with no intermediate transitions. Native DRC, branch proofs,
routing style and sampled reference copper must all pass before saving a batch.
No candidate means unresolved search, not proof of infeasibility or completion.

`python run.py pcb repair-widths --series --apply` looks for a complete
via-to-via run, including existing width changes and overlapping chamfers.
It tests alternate layers using the same vias and a width at least as large
as every existing segment in the run. With `--same-layer`, it instead tests
a complete unbranched run on its existing layer, between the same pads or vias.

`python run.py pcb repair-widths --shift --apply` tries small perpendicular
shifts of straight traces while widening them. The original copper contacts
must remain connected. Interior shifts must also cover the entire old trace.
No vias are moved or added.

`python run.py pcb repair-widths --ground-stitch --apply` tries centerline
widenings that clear the native spacing checks. If a wider trace isolates
part of the ground fill, the existing ground stitch planner tries to restore
it on the private board. A saved result must retain zero opens and pass
the independent DRC and reference-copper checks.

`python run.py pcb repair-widths --coordinated --apply` uses native clearance
findings from private trial widenings to find obstructing tracks. It can
widen the target and detour eligible neighbors together without adding vias,
moving footprints, reducing existing rules, or changing the schematic. A
candidate is saved only if it removes a width finding and passes the same
connectivity, DRC, and return-copper checks.
Reference reservations are compatible with GND copper, which reinforces the
return path. Foreign nets remain blocked there, and real PCB keepouts and
physical clearances still apply to GND. Native reference checks remain required.

Terminal rules distinguish the LTC4367 controller's VIN/VOUT supply and sense
pins from the external MOSFET load path. Exact pin-current evidence and the
larger normal-current design bounds are recorded in `config/branch_widths.json`.
Multiple copper items at one proven endpoint form one upstream junction;
separate ports, positive overlaps and unknown contacts receive no tap allowance.
Tap selectors use a 2 nm coordinate window to avoid KiCad's numeric conversion
rounding. The compiler omits the allowance if another trace or arc could match.

`python run.py pcb clean-silkscreen --apply` applies the maintained
`config/silkscreen.json` presentation rules. It aligns connector captions, binds
test-point and fuse labels to their actual footprints without renumbering them,
removes labels for absent parts, and keeps outline artwork clear of the board
edge. Only silkscreen fields and artwork may change; local library artwork is
kept consistent. Owned display fields retain KiCad's native save order.

`python run.py pcb clean-silkscreen --top-alignment --apply` aligns only the
board-level front connector captions. Each bank uses a shared column and row
spacing where clearance allows. It preserves label text and font, all
footprints, the back silkscreen, and copper. KiCad checks the private board
before anything is saved.

`python run.py pcb repair-drc --apply` corrects reported off-center via contacts
by intersecting their existing horizontal, vertical or 45-degree support lines.
It can remove an unused single-layer via after independent checks. It preserves
all component placement and rejects new connectivity, clearance, width, style,
branch-proof or sampled-reference defects. Courtyard conflicts needing component
movement remain unresolved under the fixed-placement boundary.

`python run.py pcb repair-drc --keepouts --apply` uses native findings to
repair a short USB/Ethernet crossing at fixed pads and vias. It keeps width,
layer, other copper and physical keepout geometry fixed. The solver models
footprint-owned rule areas at their actual boundary and retains ordinary
copper and drill clearances. The refilled board must show continuous adjacent
ground beneath the new copper outside the original endpoint via clearance.
The local repair does not certify the entire pair's impedance or timing.

Native KiCad 10.0.6 limits most reported error types to 199 findings per run.
Width inventory now exhausts this cap using geometry-identical temporary copies
and merges the native findings. No inventory group or temporary check override
is published to the design. The progress recorder uses the same complete width
inventory; older capped measurements remain historical reported counts.
External-router exports use the same conservative whole-net width bounds. Nets
with different bounds receive separate temporary classes with inherited via and
cross-class clearance settings; a local pad escape never becomes a trunk width.
After an intentional rule update, `pcb save-working-preview --run RUN_ID --adopt-rules`
rechecks the exact saved preview under the new rules in the same checkpoint.
Changed circuitry, another preview, altered libraries or a changed schematic
still stop adoption; ordinary `--adopt-saved` continues to reject rule changes.
`pcb autoroute run --search-cells 8000000 --search-expansions 1000000`
can retain the requested grid on larger six-layer windows. These runtime caps
are recorded in the same checkpoint and apply to new and recovered searches;
they never alter widths, clearances, costs or the aggregate memory ceiling.
`--search-grid-mm 0.05 --search-heuristic-weight 8` selects a finer, greedier
search; the actual cost function and acceptance checks stay unchanged. These
controls also propagate to per-net overrides and temporary follow-up searches.
The grid setting is a ceiling; it preserves finer recovery and maintained escape grids.
When alternative access points remain, `search.endpoint_seconds` caps an
individual attempt at five seconds by default. The parent deadline remains in
force, and a timed-out attempt cannot be cached as proof that no route exists.
The configurable `first_feasible` mode stops at an acceptable warm-start path;
the configured weighted search favors speed. They may leave endpoint
alternatives unsearched and do not prove optimality.
Both on-grid and off-grid paths are checked against the existing route tree
before acceptance. Off-grid search can retry a grid access after an illegal
endpoint join; an inconclusive context-limited search remains unresolved.
After repeated context rejections, the search also tries a bounded number of
detours through the same clearance and geometry checks. For a staircase, it
first tries to avoid added copper identified in the conflicting chain. These detours are
feasible candidates, not proof of an optimal route or of electrical suitability.
If those detours fail, sampled alternative endpoint headings get a turn before
the unrestricted retry; the original search remains a fallback within the budget.
First-feasible search also tests a small set of exact straight and
horizontal–45-degree–vertical paths before using the grid. It checks the same
clearances, endpoint directions and route-tree context; a found path is a warm
solution, not proof of optimality. Nearby off-grid accesses receive a deeper
initial search under the same total time and expansion limits. Exhaustive mode
retains its broader initial sharing of the budget.
For endpoints on different layers, the warm start also samples a bounded set of
single-through-via paths, including eligible existing vias. It checks all-layer
via clearance, exact track geometry, endpoint headings and route-tree context;
if no sample works, the grid search still runs.
For same-layer endpoints, it also tries a bounded pair of via escapes and a
straight or chamfered path on another permitted layer. The escape legs cannot
cross or touch the opposite via; the through vias and all tracks must pass the
same obstacle and context checks. The bounded local sample includes off-axis
sites and prefers eligible existing vias. This is a heuristic, and every
candidate still requires native validation before it can be retained or saved.
If the simple transit paths are obstructed, it also tries up to three escape
pairs per alternative layer with a finer single-layer detour search. The same
cell cap, shared deadline, saved dimensions and complete route-tree checks
apply. A bounded unsuccessful detour remains unresolved; the ordinary
multilayer search remains available.
Detour budgets reserve a share for every remaining layer; congestion affects
search order without changing layer costs. Longer warm paths can also sample
complete standard doglegs, allowing an interior cardinal corridor between two
45-degree chamfers when the simpler endpoint corridors are obstructed.
External-proposal cleanup aligns consistent nanometre-scale via rounding,
rejects new isolated copper and temporarily defers unproductive repairs. It
also prunes proven dangling proposal branches across vias in one transaction,
stopping at existing copper, pads, junctions and nets with zones. Native
DRC and protected-net checks still apply. New off-angle external traces can be
replaced by checked, endpoint-preserving 45-degree candidates. An attributable
native or ambiguous-geometry rejection can trigger a bounded net-subset retry;
unidentified errors still stop the check.
Connectivity checks can roll back a cleanup that worsens the candidate.
Shortcut selection also avoids overlapping untouched same-net tracks while
allowing connections at shared endpoints.

For congested partial routes, `pcb freeroute-rework --renew-net-trees NETS`
rebuilds complete permitted unfinished net trees in batches on a private board.
It retains only an independently checked reduction in opens that restores all
prior required connectivity. Component placement stays fixed, and saved native
width/clearance rules are compiled again. The same run checkpoint continues
after an accepted working save. External trial files are cleaned up after the
operation, including failure or interruption.
`--include-connected` considers neighboring trees in the explicitly requested
classes. It ranks actual same-layer copper separation, including via copper,
rather than overlapping diagonal bounding boxes; protected nets remain excluded.
Fixed-placement copper repair uses the requested working classes, including
explicitly permitted power nets. Bottom-placement trials retain their narrower
signal-only gate. In working-connectivity mode, mixed old trunk/escape widths
do not become a blanket track floor; saved scoped rules are materialized again.
Explicit electrical width/via limits and existing via floors remain mandatory.

`pcb copper-prune --run RUN_ID --isolated-warnings` can propose removing tiny
isolated straight tracks reported as dangling; `--isolated-errors` further requires
a native error on each selected track. Both modes require no same-net zone,
pad, via, neighboring copper, group, or lock, and exclude user-protected and
high-speed nets. The save guard independently rechecks isolation and all usual
native and reference checks remain mandatory.
`pcb freeroute-retain --engine TraceMaker --router BINARY` can identify and check
an externally generated candidate; TraceMaker search is not a built-in command.

## Quick start

### Terminal output

`pcb autoroute prepare`, `run`, `status`, and `verify`, along with `review progress`,
`review refresh`, project `status`, and `docs build-pdf`, show readable progress and
summaries by default. Long stages report activity every 50 seconds. Routing output
separates saved-board opens from private-preview opens, names the current nets,
reports batch outcomes, and groups repeated diagnostics. It does not print source
hashes or object UUIDs in normal output; the underlying checks and records remain.

Use `--verbose` for additional named nets, geometry findings and search statistics.
It also shows the selected checkpoint, effective grid spacing, recovery stage,
and specific geometry/path rejection examples. Repeated examples are bounded.
For autoroute commands, `--full` is an alias for readable verbose output. Use
`--json` for the complete machine-readable result on stdout; progress events go to
stderr, so redirected result files remain valid JSON. All output uses plain text
without cursor animations and wraps to the terminal width, including in Git Bash.
Printed resume commands remain one logical line for copying into the terminal.

```bash
python autopcb/scripts/run.py pcb autoroute prepare
python autopcb/scripts/run.py pcb autoroute run --workers 2 --max-rounds 10 --verbose
python autopcb/scripts/run.py pcb autoroute pair-probe --seconds 45
python autopcb/scripts/run.py pcb autoroute pair-probe --engine krt --router-grid-mm 0.2 --seconds 120 --memory-gib 3
python autopcb/scripts/run.py review progress
python autopcb/scripts/run.py review refresh
```

The board-wide runner currently produces private previews only. `--apply` is
disabled while electrical checks, paired-signal routing and the remaining
completion pipeline are unfinished. Resume an unchanged run with `run --resume RUN_ID`.
`pair-probe` tries fastroute's paired mode by default, or KRT's paired mode
with `--engine krt`, on a disposable copy. It reports native DRC, geometry,
new via-in-pad overlaps and sampled-reference findings, and discards the
candidate. Configured gaps are targets; skew and impedance are not qualified.
With `--verbose`, the report attributes opens and incomplete sampled reference
segments to each pair; those counts do not certify an individual pair.
`--pair-index N` limits a trial to one discovered pair while preserving the
other pairs' rules and including all high-speed nets in the reference check.
A verbose trial also lists nearby non-ground copper around segments with
incomplete sampled reference; these are conservative screening candidates,
not proven causes of the gaps.
A KRT timeout is reported as an unresolved search and its partial result is discarded.
Add `--run RUN_ID` to record the diagnostic time on that current preview's
progress plot; the discarded trial is not counted as saved routing progress.
After updating routing code, use `run --renew RUN_ID`: it independently checks
the retained candidate and rebuilds its work queue using the current code.
Pending escalation stages and queue order survive renewal. Search settings and
removal candidates are regenerated; a geometry-free preference helps resume
the pending repair kind, and obsolete removal UUIDs are never replayed.
Successful renewal creates a new run and preserves the previous record. It does
not replay old requests or campaigns. Changes to saved design inputs, libraries,
or routing policy require a new run from the saved board.

The default aggregate memory ceiling remains the lesser of 6 GiB and half the
available RAM. This KiCad 10 workflow requires a launch budget of at least 2 GiB
for PCB and schematic checks; that guard does not guarantee every board will fit.
If the automatic budget is too small, AutoPCB stops before search. Use
`--wait-for-memory 60` to wait for up to a minute, or `--memory-gib 3` to explicitly
allow a 3 GiB aggregate commit budget. This includes the coordinator, search
workers, and native checks. The explicit budget must fit available Windows commit
capacity with headroom; it does not disable the memory limiter. Available RAM
and Windows commit capacity are different limits, and other applications can
still consume memory during a run.

From `autopcb/scripts/`, for a retained preview after a code update:

```bash
python run.py pcb autoroute run --renew RUN_ID --memory-gib 3 --workers 2 --seconds-per-batch 30 --max-rounds 10 --verbose
```

Use the **new** run ID printed at the end for subsequent `--resume` commands.
Native errors include their exit status and diagnostic output. A memory failure
during resume or routing records `resource_blocked` and retains the last accepted
candidate and pending work. Renewal verification failures leave the previous
record untouched. Missing, stale, or failed native reports never count as passing
checks.

Failed local searches now use distinct island sets: a new starting pair cannot
silently repeat the same three-island checkpoint. For the same candidate board,
identical search requests are remembered across resume. A changed time budget or
accepted copper change permits a new attempt. Recovery tries pairs in a wider
window, a finer grid, and then a board-wide detour window. These remain finite
searches; they preserve widths, clearance, vias, layer eligibility, electrical
settings, and the time budget. Wide recovery windows run separately from parallel
batches. The optional `recovery` object in `config/autopcb.json` controls stage
count, alternatives, margin, cell/expansion budgets, and path alternatives.
With `recovery.early_rework` enabled, a failed local target also schedules an
early obstacle-repair attempt while retaining the wider and finer searches.
When a batch has different net orders to try, each order leaves time for the
others; a timed-out order does not establish that routing is impossible.
For each tree, `search.connections_per_step` sets the primary access-pair
sample (default 12). If it cannot extend the tree,
`search.fallback_connections_per_step` allows a larger bounded sample
(default 96); the normal path-call and time budgets still apply.
Older configurations with an explicit primary limit keep it as a hard cap
unless they explicitly specify a larger fallback.
Repair nets that already have one surviving island are checked first and do
not build a grid for an empty route. The remaining search budget is then
available to nets that need copper; a refinement timeout retains the candidate
found before it.

After those searches fail, a bounded copper-replacement pass identifies nearby
ordinary tracks or through vias by UUID, including eligible copper already on
the target net. It tries the target and affected neighbors together, preserving
every connection that the removal breaks.
The candidate is rejected if native connectivity, DRC, protected-item or
reference-copper checks fail. It never splits the repair into unrelated jobs.
If no component moved, the original removed traces and matching through-vias
can seed a repair at their exact original dimensions, with native checks still
required. Compatible round pads use circular obstacles; centered, axis-aligned
oval and rounded-rectangle pads use conservative rounded envelopes. Other pad
shapes retain bounding boxes with Euclidean clearance. Hole and non-plated-slot
obstacles also honor the saved hole, hole-to-hole and copper-edge minima during
track and via search.
For centered circular drills in supported plated pads, the guaranteed copper
margin is subtracted from the drill-clearance requirement, rather than counted
twice. Offset, slotted, chamfered/custom and non-plated cases keep the conservative
fallback. Native DRC independently checks the resulting proposals.
When other nets remain, testing alternatives to a valid seed is time-bounded
so the transaction can continue; a blocked seed does not suppress full search.
Selection includes bounded clusters around blocked endpoints as well as single
items. The board-wide runner currently excludes unqualified power, ground,
protection and critical-pair copper from removal. A candidate does not by itself
verify current capacity or the electrical behavior of a modified circuit.
Defaults are 3 candidates, 12 removed items, 3 repair nets, a 1.5 mm endpoint
radius and 12 repair islands. Configure these with `recovery.rip_up_candidates`,
`rip_up_items`, `rip_up_nets`, `rip_up_radius_mm` and `repair_islands`. The ordinary
three-island checkpoint limit does not discard fragments that a repair breaks.
Recovery returns to a local window after the board-wide detour stage and gives
repair neighbors the current recovery search limits, avoiding an accidentally
coarsened whole-board grid for package access.
Relaxed-path diagnosis now identifies exact copper intersecting sampled corridors
before attempting a fully constrained repair. If the full-board diagnosis finds
no witness, a bounded local pad-to-via search can identify eligible copper in
the escape corridor. These paths are diagnostic only; protected copper remains
in place, and normal routing and independent checks are still required.
The diagnostic reserves adjacent reference copper around USB/Ethernet nets,
not around every electrically protected net; all protected copper is still a
hard obstacle and is excluded from the proposed removals.
`obstruction_trials` (default 4) and `obstruction_seconds` (default 10) bound this
step. Bottom recovery also samples permitted package and package-pair poses,
reconnects all incident nets and checks previous connectivity in one transaction.
`placement_candidates` defaults to 6. A placement transaction can include up to
12 affected nets by default (`recovery.placement_nets`, maximum 32); this limit
is independent of `rip_up_nets`. Every affected net is included in the repair.
Set both `rip_up_candidates` and
`placement_candidates` to zero to disable automatic physical recovery.
Deferred recovery retries count distinct other pending target nets, so repeated
attempts on one target do not crowd out unrelated work.
The queue also favors less-explored targets before retries, with section
priority breaking ties. Attempt counts survive resumes and code renewals of the
same preview, and reset on an improved preview or a new sweep. Renewing still
allows earlier search requests to be retried with the updated code.
With two workers, independent retries or repair alternatives can run on
separate temporary boards at the same time. After both search workers exit,
native validations run one at a time to stay within the memory budget. The
controller selects at most one checked result, never combines competing
alternatives, and requeues an unselected retry against the updated preview.

When an access point touches an older off-angle track outside a pad or via,
search treats that point as blocked and may try another point or a copper
repair. It does not accept a new off-angle bend.

The progress chart distinguishes saved measurements from private-preview
measurements. Accepted batches and exit refresh it automatically; preview data
never overwrites the saved-board CSV. If the progress export fails, routing stops
and retains the last committed checkpoint so the export can be fixed before
resuming. To recover the display for an older run:

```bash
python run.py review progress --preview-run RUN_ID
```

When grouped searches repeatedly reach their time slices, start a new run with
`python run.py pcb autoroute run --single-net-search --workers 2`. Each worker
then searches an individual net; compatible candidate batches are still checked
together. The mode stays with that run when it is resumed or renewed, and does
not guarantee that a limited search will find a route. Native recovery also
tries replacing the complete existing copper of a target net when every item
is eligible and the separate `whole_net_rip_up_items` limit permits it (32 by
default; 0 disables it; maximum 64). The replacement remains a costed private
candidate subject to the same connectivity and design checks. Local search
windows expand within their scheduled bounds when a requested margin would not
leave room for track, via and grid access at the endpoints.

This revalidates the retained preview without applying it. Normal PCB PDFs and
PNGs still show the saved board. The researched implementation roadmap and
acceptance gates are in [Algorithm, Section 9](docs/algorithm.md#9-completion-plan-and-acceptance-gates).

For faster private experiments, the router adapter can try several nets per
batch from an existing preview. From `autopcb/scripts/`:

```bash
python run.py pcb freeroute-fast --engine fastroute --run RUN_ID --batches 4 --items 80 --timeout 180
python run.py pcb freeroute-fast --engine krt --run RUN_ID --router-grid-mm 0.2 --batches 4 --items 0 --timeout 90
python run.py pcb freeroute-rework --engine krt --run RUN_ID --focus-open-class CORE_SIGNAL --focus-open-class FIELD_SIGNAL
```

The external adapters permit one routing pass per 15 seconds of search budget,
up to eight passes, within the same timeout. The independent acceptance checks
still apply to the result.

`krt` uses the pinned KiCadRoutingTools 0.23.7 checkout in the local routing-tool
cache. It requires a Python with NumPy, SciPy and Shapely; `--router-python` can
specify that interpreter. Windows Application Control has blocked version 0.23.8
and, on the latest check, the Python environment's Shapely native dependency.
KRT is therefore unavailable in that environment; use an available backend such
as fastroute. The adapter accepts
an explicitly supplied, permitted checkout through `--router` for
`freeroute-fast` or `--krt-router` for `freeroute-rework`; the default remains
0.23.7. It routes eligible ordinary-signal nets on a private
native-board copy with equal copper-layer costs and rule relaxation disabled.
Its fast proposal is filtered and independently checked before retention; this
does not yet implement accepted power, ground or differential-pair routing.
The adapter also disables per-layer direction preferences. `--router-grid-mm`
changes only the search resolution; it does not reduce widths or clearances.
For a longer KRT search, add `--router-full`; this enables its recovery and
finishing passes within the specified timeout. The default uses its quick mode.
The local adapter clears the disposable router's coordinate-dependent rescue
cache before mirrored or rotated searches. It leaves the pinned checkout and
the original board untouched.
The `krt` search uses a 10 mm-equivalent via penalty and a turn penalty. Its
internal search cost does not exactly represent AutoPCB's full diagonal and
symmetry objective, so an accepted preview is progress, not proof of optimality.
The coordinated-rework command can temporarily remove selected signal copper,
then use `krt` to reconnect the affected nets. It retains a result only after
checking the original connectivity, protected copper, geometry and native DRC.
Before retaining or saving a proposal, AutoPCB rejects newly introduced via
drills that overlap copper pads on any enabled copper layer. Existing
overlaps remain completion blockers. This check does not qualify filled or
plated via-in-pad construction and is not a substitute for fabrication checks.
Local repair includes vias already proposed in the batch as search obstacles,
and may reuse a same-net via at its actual center. It uses a first-feasible
search, then checks the combined result; it does not prove optimality. Repeated
repairs reuse one native obstacle model only while the board, project rules,
net, and refill setting match. After a partial repair stalls, one alternate
order is tried if the cleanup budget allows. In an ordinary-signal additive
batch, a strict-size refusal may be screened by discarding every net identified
in the router's complete final size report and independently checking the
remaining copper. Net numbers are mapped using the pinned router's own parser
on the unchanged input. A missing or ambiguous report discards the whole proposal.
Coordinated rework can use the same complete report only for requested replacement
trees: it discards every refused tree's new copper, restores the original trees,
and independently checks the combined result. An unexpected net stops the trial.
Pair searches do not use partial salvage; no refused net counts as routed.

For a broader pass, use `--items 0 --timeout 70`: fastroute has up to 60
seconds to search before saving its best private result. Filtering and
independent checks run afterwards and can take longer than the search itself.
`--starts 2` asks fastroute to try a second order within its time limit; it is
optional and does not guarantee a faster or better accepted result.
`--optimizer` requests the external router's optimization stage before the
same independent checks. The output says whether that stage was observed;
requesting it does not mean it ran or improved the result.
For difficult package exits, `--search-pad-neckdown` lets fastroute or
Freerouting propose a narrower connection at a small pad. This changes search
only: AutoPCB still checks every proposed segment against the saved class and
package-local width rules, then runs native DRC and the usual acceptance gates.
The resolver honors saved package-local overrides to Board Setup defaults only
inside their proved scope. Explicit electrical width or clearance floors remain
hard constraints; the board defaults still apply outside an override.
Proposed tracks use the same width materializer as native search: it splits
package and trunk regions and widens undersized pieces before clearance and
native checks. It never thins a proposal or edits existing copper. Fragmentation
is bounded to 50,000 proposed segments; an exhausted bound stops the candidate.
An undersized proposal is discarded; this option does not authorize thinner
power trunks or qualify current capacity. It is off by default and is not a
KRT option.
Proposal cleanup can simplify paths across width transitions when the saved-rule
materializer is supplied. It preserves pads, vias, branches and old copper
contacts, then restores the required widths on each shortcut. A necessary
width transition is not counted as a redundant collinear joint.
`--ground-contact-guard-mm` changes the extra search-only margin around filled
ground near pads (default 0.5 mm; permitted range 0 to 2 mm). Zero removes that
extra margin, while physical pads, clearances and local USB/Ethernet reference
reservations remain. Existing ground connectivity must still survive. Both
search settings are included in failed-attempt memory, so a changed search
does not silently reuse an exhausted result.
Cleanup defaults to a 60-second budget (`--cleanup-seconds` changes it); an
in-progress native check is allowed to finish. At the limit, the adapter checks
whether other proposed nets still provide a safe improvement. Nets whose local
repairs failed are temporarily deferred and revisited after three improvements;
their net-class dimensions and via rules are preserved. A time limit never
turns an unresolved candidate into an accepted route.

`--defer-class` and `--defer-net` are temporary: the adapter revisits them
after progress. Use `--exclude-class` or `--exclude-net` when a class or net
must stay out of the entire fast run. Both `Default` and KiCad's exported
`kicad_default` name refer to the same class.
When only part of a class is deferred, its temporary alias inherits the
exported cross-class clearance relationships as well as its own class rules.
The DSN export can omit other custom rules, so native validation is still required.
The fast adapter also automatically defers identified protection and safety
nets and non-signal or unclassified nets, including configured exclusions.

To concentrate a batch on unfinished nets in one or more classes, add, for
example, `--focus-open-class CORE_SIGNAL`. AutoPCB checks the current private
board with KiCad, identifies the unfinished nets in that class, and defers the
other nets for that batch. Missing or ambiguous net attribution stops the
batch; this option does not claim that omitted classes are complete.
For a focused run, unsuccessful deferrals and exhaustion are remembered only
for the same preview, inputs, code, router and search settings. Repeating an
bounded attempt with the same settings returns `deferred_repeat` without repeating the work;
`--retry-exhausted` explicitly retries it. A changed board, tool or budget
starts a fresh search. A bounded attempt never means the board is unroutable.

The default selects the cached fastroute executable when present, otherwise
Freerouting. `--engine Freerouting` uses the locally installed Freerouting 2.5.0 JAR and a
compatible Java runtime; `--engine fastroute` uses the verified local 0.1.14
Windows executable. Both receive equal-cost DSN settings for the saved board's
enabled physical copper stack, including all six layers on the current board. Freerouting
reports its effective costs in the log; fastroute's release source supports
those settings, but its binary does not report the effective layer costs.
For temporary exports, signal-layer plane forms backed by KiCad copper zones
are omitted so the external router does not treat an interrupted fill as an
already-connected plane. The zones stay in the native board; candidate
connectivity, clearance and reference copper still require native checks.
If fastroute reaches the external timeout, its last structurally complete
checkpoint can proceed as a private trial through those same checks. A missing
or interrupted checkpoint is rejected.
When a raw preview requests optimization, the temporary DSN enables it and the
result reports whether fastroute's log showed that the stage actually started.
An item or time limit can stop routing before that stage.

The adapter progressively refines candidate chamfer positions within its cleanup
budget, and screens attempted shortcuts against existing copper and drilled holes before
native checking. This is a conservative performance filter, not a substitute for DRC;
a route that passes it still needs all native and protected-item checks. New narrow
copper connection warnings also block a proposal when KiCad attributes them only
to existing objects. Relocated markers can match an old warning only with proof
that the complete net's copper and pads are unchanged. The adapter
can repair a defective section of a chain while preserving neighboring contacts,
widen a repair across a bounded number of unanchored proposed neighbors, and
center escape segments already contacting same-net via copper without changing
their allowed headings. Captured movements are bounded and checked again.
For a single proposed escape between an anchored pad or via and an off-center
same-net via, cleanup also tries the shortest checked octilinear alternatives.
It keeps both anchors and refuses changes that would disturb intervening
same-net copper; native clearance, connectivity and style checks still decide
whether the resulting batch can be retained.
A new proposed via can also be centered on a unique existing trace endpoint
when its other contacts and proposed escapes can be preserved. The trial skips
ambiguous connections or pad contact and still requires native checks.
Local repair tries bounded 0.2, 0.1 and 0.05 mm search grids; finer grids do not
reduce track width, clearance or other physical rules. Time or cell limits leave
the result unresolved.
An in-flight prune receives its pending native check even if the cleanup budget
expires. Every such change remains part of a disposable candidate until validated.
The adapter
filters an additive proposal and retains only a separately verified reduction in opens. Existing copper
is not ripped up by this adapter; USB and Ethernet routing, electrical limits,
and the rest of the completion checks still require further work. The adapter
reserves project-classified USB/Ethernet nets before ordinary single-net search
and does not write the live PCB. A failed or exhausted search does not establish
that a route is impossible. The upstream fastroute KiCad exporter reports many
of this design's custom rules as unsupported; its output must not be used as
proof those rules were enforced.

When a direct batch stalls, a bounded coordinated-repair experiment can
temporarily remove nearby copper and route the affected nets together:

```bash
python run.py pcb freeroute-rework --run RUN_ID --batches 3 --timeout 75 --focus-open-class CORE_SIGNAL --focus-open-class FIELD_SIGNAL
```

`--renew-net-trees NETS` rebuilds complete unfinished, permitted net trees while
keeping component placement fixed. `--ground-first` also supports ordinary
fixed-placement copper repair. It may reconnect ground access on the private
board before and after rerouting, provided the policy enables working-connectivity ground
proposals. The final transaction must restore all earlier required connectivity
and pass the ordinary preservation, geometry, size and native checks. Intermediate
ground access alone is not an accepted result or an electrical qualification.
This permission allows checked ground additions only. Every pre-existing ground
track and via must retain its geometry, and all other protected copper remains
unchanged. Added joins require a fresh native check before retention.

For a broader fixed-placement repair, add `--include-connected` with
`--renew-net-trees NETS`. The batch includes unfinished trees first, then nearby
permitted connected signal trees, within the same 64-net maximum. Ground,
high-speed pairs and other protected nets stay out. A replacement tree may keep
the same connection count so it can free space for another net, but every prior
required connection must survive and the combined board must reduce opens.
Failed replacements are restored and checked together before retention.

If an explicitly requested `--followup-engine` differs from the first engine,
it can also retry an initial proposal rejected by native checks or a bounded
filter/search result. The alternate gets the identical private input, never
the rejected copper. The controller tries each engine at most once in this
initial recovery step, records the actual stages, and stops on changed inputs,
malformed results or unexpected execution failures. A native zone-width error
that cannot be attributed to new traces rejects the entire candidate.

Reference comparisons refill both private boards under identical saved project
and custom rules, even when the prior board has a different filename. Neither
side is allowed to fall back to KiCad defaults because its filename changed.

Unsuccessful coordinated repairs remember the exact removed items and poses in
the current run, so restarting advances to untried neighborhoods. Retry memory
applies only to the same preview, saved inputs, rules, code, router and search
budget. It retains at most 128 failed plan identities; reaching that limit
reports unresolved work. Changed inputs or budgets start a fresh context;
`--retry-exhausted` explicitly repeats earlier plans. An unsuccessful bounded
attempt never establishes that a route is impossible.
The batch index does not skip additional neighborhoods already accounted for
by failed-plan history. `--optimize` enables the external backend's post-route
optimizer within the same wall-time budget; its output still passes the full
proposal filter and independent native checks.

Add `--diagnose-obstructions` to use the configured bounded relaxed-path
search when choosing copper to replace. Its paths are removal hypotheses only;
same-net copper is not treated as a foreign trace obstruction, and new vias
still account for nearby existing holes. The selected nets must be rerouted and
pass all normal checks. The default fast selection omits this diagnosis.
Local repairs consider other proposed tracks and vias and check whether a new
path creates a prohibited bend or staircase with same-net copper. They can
merge collinear proposal-only pieces without moving their electrical contacts;
the combined candidate still needs independent native validation.
`--followup-engine krt` can try KiCadRoutingTools on an intermediate fastroute
repair (or use `--engine krt --followup-engine fastroute` in the other order).
`Freerouting` can also be selected for either stage; its exact JAR is checked
throughout the attempt, and an expired search remains an unresolved result.
`--followup-rounds 2` permits a second search from a changed intermediate
layout. Identical copper stops the repeat, and each intermediate must pass the
native error checks; only an independently checked improvement can be retained.
The retained preview records both router stages and is accepted only after the
complete combined candidate passes the normal checks.

The selected target and neighboring nets must belong to explicitly named signal
classes. Ground, unclassified and power classes, user-excluded nets, classified
USB/Ethernet nets, and identified protection and actuator-safety nets are kept
out of both routing stages. Protected copper is compared again before retention.
Trials rotate across eligible targets. The repair search combines pending work
with a fresh inventory of all eligible unfinished nets, so consuming the
ordinary routing queue does not hide remaining repair targets. A checked baseline is reused during a
batch only while the saved inputs, tools, policy, preview, blocked nets and
search scope match. Candidate checks remain independent.
Identification uses available footprint descriptions and signal names; it is a
conservative filter, not proof that every safety role has been identified. The search rebuilds
its list of unfinished nets after a temporary rip-up, including newly
disconnected neighbors. A temporary repair may change an intermediate connection; the final candidate
must be checked against the original retained board before it can be accepted.
A retained preview must reduce opens, preserve earlier
connectivity and protected items, introduce no new native or style violations,
and not replace selected copper with narrower tracks or smaller vias. The
independent reference-copper comparison also applies. If a repair restores the
same copper without improving connectivity, an identical follow-up scope is
tried once per batch sequence and subsequent repeats are skipped. Nets whose
local repairs fail are deferred only within the current
batch sequence, and are still included whenever they are the target or an
affected neighbor. `--start-index` tries a
later candidate; a bounded failure is not proof of infeasibility. This is a
work-in-progress search, not electrical or manufacturing qualification, and it
does not itself write the saved PCB.

`--bottom-placement` also tries native poses for eligible bottom-side signal
resistors. A private trial can rotate a two-pad resistor by 180 degrees around
an unchanged, symmetric protected pad when the other pad is on an eligible signal.
Protected copper is never removed; all affected signal copper must be rebuilt and
checked. A singleton footprint group may be preserved; shared, nested or locked
groups are excluded. The working-save guard still rejects changes to footprints
touching unqualified nets, so this experiment cannot by itself publish the move.
Every top footprint and front-silkscreen item remains protected.
`--start-target` and `--target-window` bound the target groups; batches advance
to the next group. If the board-wide inventory has no eligible resistors, the
command stops early instead of repeating empty searches. Every moved-part net
and previously connected island must pass the same independent checks before a
preview can be retained.

The printed batch rate covers that accepted batch's routing and checking time.
The progress chart separately shows elapsed time between improvements and the
recorded run-time rate. New unsuccessful batches advance the recorded time and
refresh the chart without changing the open count; idle time and earlier
unrecorded experiments are excluded from that rate. The command prints the
newest run ID for the next invocation.

`python run.py pcb zone-stitch --run RUN_ID` can propose short pad-to-plane
ground connections on a private preview. It uses the board's ground-class
dimensions, permits direct or single-chamfer paths, and checks local copper,
holes and USB/Ethernet reference reservations. Each pad needs a source-bound
track and via current/voltage-drop assessment covering normal, transient and
fault scenarios in `autopcb.json`. The assessment must identify the exact saved
board and every copper item on the disconnected island, so a pad cannot stand in
for an unassessed shared return. A qualified pad can be selected from an open
island even when KiCad reports a track rather than the pad as the endpoint.
Missing or stale evidence blocks the proposal. The current assessor checks the
arithmetic and source fingerprints, but cannot verify the cited values, their
applicability to the copper dimensions, or fault conditions. It therefore cannot
qualify a ground stitch, even when its recorded inputs pass the calculation.
The command keeps the saved board unchanged.

`python run.py pcb via-prune --run RUN_ID --net-class CORE_SIGNAL` screens
ordinary single-layer signal vias and their dangling spurs on a private copy.
Repeat `--net-class` for other classes. It skips ground, protected high-speed
nets, nets with zones, pad-connected vias, and locked or grouped copper.
Retention requires fewer vias, no new copper or longer tracks, preserved
connectivity and no new native or routing-style issues. The displayed
two-segment-joint count can rise when a removed via reveals an existing copper
junction; that does not add a new physical joint.

`python run.py pcb copper-prune --run RUN_ID --net-class CORE_SIGNAL --net-class FIELD_SIGNAL --dangling-tracks`
can remove reported dead-end signal tails on a private preview. It excludes
`GND`, classified high-speed nets, nets with zones, and locked or grouped
copper. It automatically defers nets that would introduce new dangling
warnings or geometry problems. For an unambiguous interior T-junction, it can
also trim the dead end and add one short 45-degree corner. This exception has
an independent check of the exact old and new geometry; all other cleanup is
removal-only. Retention requires fewer warnings, no increased opens or vias,
shorter copper, and independent checks of existing connectivity, protected items
and sampled high-speed reference copper.

`python run.py pcb copper-prune --run RUN_ID --net-class CORE_SIGNAL --net-class FIELD_SIGNAL --repair-rounding`
searches for an off-angle signal track whose unanchored two-track joint can be
corrected by at most 0.05 mm, using exact horizontal, vertical and 45-degree
intersections. It rejects protected nets, branches, pads,
vias, locked or grouped copper, and any candidate with a new style issue. The
independent retain and save checks recompute the endpoint-only proof, native DRC,
existing connectivity, protected items and sampled reference copper. This is a
style repair; it does not claim that an open connection has been routed.

`python run.py pcb copper-prune --run RUN_ID --net-class CORE_SIGNAL --net-class FIELD_SIGNAL --repair-staircases`
automatically finds shorter, unbranched signal-chain shortcuts and checks
independent nets together, splitting a batch if needed. It skips protected,
high-speed, locked and grouped copper. An independent proof checks each chain's
exact endpoints, net, layer and width, and confirms that staircases decrease;
native DRC, connectivity, routing style and reference-copper checks still apply.
This is a style repair and does not by itself close an open connection.
These cleanup checks can evaluate a work-in-progress baseline with identified
native errors, but reject new or changed errors; the board remains incomplete.
When a parity conflict identifies both nets, dangling-track and staircase
cleanup defer both nets and preserve the existing conflict. Unidentified
conflicts still stop the check.
Their non-track comparison tolerates KiCad reordering top-level records while
requiring the contents and number of those records to remain unchanged.

`python run.py pcb sync-net-aliases` checks whether a complete PCB net with
existing copper can be renamed to its unambiguous schematic net. It checks
schematic parity, DRC, existing connectivity, protected items and sampled
reference copper on a private board. Add `--apply` to save a passing result
with a recoverable pre-save checkpoint. Only whole, identified pad-and-copper
groups are eligible; zones and ambiguous mappings are deferred. Correcting a
name can reveal an open connection that was previously hidden, so the open
count may increase. The command does not route or establish electrical or
manufacturing readiness.

`python run.py pcb save-working-preview --run RUN_ID` independently rechecks a
retained work-in-progress preview. Add `--apply` to atomically save a qualifying
improvement after the editor-lock and source checks pass; a recoverable
pre-save checkpoint is recorded. This is a working save, not fabrication
approval. The current save guard rejects changed copper on identified protection
or actuator-safety nets, user-excluded nets, and non-signal or unclassified net
classes while their electrical qualification is incomplete. It also rejects
footprint changes on those nets. Transistor terminal nets are included in the
conservative protection screen regardless of their names. Refresh the review views and progress chart
after a save.

Search and generated copper now share a saved-rule dimension model. It reads
native net classes and supported width/clearance conditions from `.kicad_dru`,
including rule precedence and courtyard-local package escapes. A neck widens
again outside its package; the absolute Board Setup minima and any electrical
floor cannot be reduced by a local allowance. New copper never inherits a legacy group or exact-coordinate
exception. Unsupported expressions stop the affected request explicitly.
Native DRC remains authoritative for all candidate geometry and other rule types.

Repair alternatives are interleaved with other pending work using
`recovery.defer_rounds` (default 2). An accepted preview change discards old
repair UUIDs and poses; the affected targets are recompiled on the new board.
Removing redundant copper no longer rejects a repair merely because the
neighbor remained connected; native checks still guard all existing connections.

Grid vertices use a lower-bound obstacle mask, followed by swept-track checks
on every actual edge and off-grid connection. This removes artificial half-cell
padding without weakening the physical clearance. Spatial indexes and bounded
edge caches keep these checks local. Multi-net searches share the remaining
batch time among the nets still to be searched, so unused time carries forward;
identical independent failures are reused across order trials and advance to
recovery when a batch is split.
Checkpoint selection considers all retained access points when choosing the
nearest pair; the retained set itself is bounded.
When nearby off-grid access vertices are blocked or cannot lead to a route,
the router also tries farther escapes under the same clearance and angle rules,
capped at twelve grid rings and 2,048 pairs per expanded ring. A capped or
exhausted sample still leaves
feasibility unknown. A clear direct route reaching a proven relaxed cost bound
does not spend time searching alternative grids.
Endpoint heading restrictions apply at the true anchor, while the grid entry
and exit permit the legal turns that join each stub to its grid path. The
assembled route is checked again for angles, spacing and staircases.
Endpoint-grid pairs get bounded initial searches so one difficult pair does
not consume every expansion before alternatives are tried. When a ring has
multiple access pairs, these searches use the ordinary geometric lower bound
instead of rebuilding a reverse-grid guide for every pair. Single-pair and
on-grid searches retain the configured guide. This changes search order and
may change the candidate found within a time limit; it changes no hard rule.
The optional reverse-grid guide now stops after reaching the start or a bounded
work limit. Unexplored cells contribute a safe zero lower bound; they are not
declared unreachable. The guide permits diagonals between blocked vertices when
the detailed swept-edge check may allow them.

Existing copper is reusable, not immutable. Candidate ranking includes the
ordinary routing cost plus physical change costs. The optional top-level
`rework` object in `config/autopcb.json` defaults to
`{"track": 1.0, "length_mm": 1.0, "via": 10.0, "placement_mm": 1.0, "quarter_turn": 1.0}`: a changed old track,
each millimetre no longer reproduced at the same width/layer, and a changed old
via. Equivalent collinear splits or replacing only a UUID are free.
Bottom movement is priced by displacement and the shortest angular change.
Verbose output separates routing cost from existing-copper change cost.

Ordinary two-segment bends obey the 45-degree rule. T-junctions and other branch
points are treated as branches by both the optimizer and independent checker;
pads and vias are connection anchors. Endpoint direction constraints prevent a
new route from forming an invalid bend when it reaches the end of existing
copper. Staircase screening also includes the adjacent saved copper, while
ignoring unrelated old patterns and distinguishing real branching junctions.
When a sampled path fails that context check, the router can retry with other
endpoint headings within its existing search budget.
The independent checker identifies each staircase by its local turns and
directions, so extending a chain or splitting a track does not turn an unchanged
old defect into a new one. Existing defects still prevent routing completion.
A repair can straighten a removable uniform-width chain or include adjoining
eligible staircase tracks in a bounded reroute. When shortcuts fail, it can
search a local window and try alternate endpoint headings. A straight continuation
of existing copper is allowed; a right-angle bend or a retrace of the existing
segment is not. The normal clearance, style, connectivity and native DRC
checks still apply.
Reused source copper is distinguished from newly drawn portions, and candidate
vias are retained in the materialized repair.
Repair contacts account for actual pad and via copper and unsampled track
contacts. Nearly axis-aligned or 45-degree legacy tracks may be offered as
bounded exact-angle replacements, with their rework cost and normal checks.
Automatic recovery tries repairing damaged neighbor nets before the new target,
while retaining alternative net orders for later searches.
After earlier nets are placed, a small-window search is attempted before the
existing wider search, within the same resource budget.
A branch that replaces a trace with split pieces retains its original
electrical anchor. Invalid candidate endpoints are recoverable rejections;
infrastructure failures still stop the run with their diagnostic. General
coordinated bottom placement and unrestricted board-wide rip-up remain unfinished.

Campaigns edit the PCB text directly. KiCad is used headlessly for parsing,
connectivity, copper fills and native checks; no editor is opened. The board-wide
search can recompute zone fills on temporary boards and use their existing
connections. It can also sample short via connections to an existing filled
polygon; native validation refills again after proposed changes and checks
existing connectivity. This finite sample does not cover all plane attachments.
The board-wide
runner isolates native work in disposable processes because the installed KiCad
10 bindings retain loaded boards and cannot safely release some native objects.
Processes are recycled between transactions instead of accumulating native
boards in a persistent coordinator. Up to two independent searches can run
together; the aggregate memory ceiling and all validation checks still apply.

From the KiCad project folder above `autopcb/`, in Git Bash:

```bash
python autopcb/scripts/run.py --help
python autopcb/scripts/run.py status
python autopcb/scripts/run.py check tools
python autopcb/scripts/run.py check code
python autopcb/scripts/run.py pcb checkpoints run --sequential --limit 3
```

The last command previews up to three attempts on a temporary board. Accepted routes carry forward into later attempts, and checkpoints are replanned after each improvement. The saved PCB is not changed. Omit `--limit 3` to continue until the configured search cannot find another validated improvement. Progress appears during the search; Ctrl+C preserves a resumable progress record. The summary reports the record path, open-connection counts, termination reason, and whether electrical limits were assessed. A completed preview is not a completed board.

To resume a paused or interrupted preview, use the `sequence_preview.json` path printed by the previous run:

```bash
python autopcb/scripts/run.py pcb checkpoints run --sequential --resume "autopcb/results/autopcb/<run-id>/sequence_preview.json"
```

Resuming rechecks the saved sources, configuration and tools, then reconstructs and validates every accepted step on a fresh temporary copy. If those inputs changed, start a fresh preview instead. An optional `--limit` on a resumed run counts new attempts. Without `--sequential`, `pcb checkpoints run` previews each checkpoint independently against the same saved board.

For a coordinated rearrangement, add `--seed "autopcb/config/<seed>.json"` to a new sequential run. A seed contains source-bound copper changes and their connection contracts; it may leave the open count unchanged but may not increase it. AutoPCB checks the geometry, existing connectivity, and KiCad DRC before carrying it forward on the temporary board. The report stores the seed for revalidation on resume, so `--seed` is not repeated when resuming. Seed and subsequent routes remain previews, and electrical checks are still required.

For `pcb plan-route`, `plan-batch`, `plan-nets`, and `optimize`, a relative `--config` path is relative to the directory where you ran `run.py`, including in Git Bash.

Selected plated-through pins can be approached on any configured copper layer they occupy, without counting them as extra selected pins. Search timing excludes the subsequent KiCad validation.

To create a plan without attempting it, or try a particular checkpoint:

```bash
python autopcb/scripts/run.py pcb checkpoints plan
python autopcb/scripts/run.py pcb checkpoints attempt "autopcb/results/autopcb/<run-id>/checkpoints/0001.json"
```

Replace `<run-id>` with the directory printed by the plan command. An attempt records its result in that checkpoint and does not apply it. The initial configuration in `config/autopcb.json` covers selected timing and driver nets; its generic geometry is provisional. Replan after the saved board, rules, libraries, configuration, or routing tools change. Saving a new routing campaign additionally requires supplied whole-net electrical limits and passing native validation. An unresolved search is not proof that a route is impossible; coordinated changes to other copper or bottom-side placement may be required.

For review and documentation:

```bash
python autopcb/scripts/run.py review progress --redraw
python autopcb/scripts/run.py docs build-pdf
python autopcb/scripts/run.py test
```

`--redraw` uses existing measurements without rerunning KiCad checks. When a preview overlay exists, it reads the saved inputs to verify that the overlay still belongs to this design. Remove `--redraw` to record a new saved-board check; add `--label "description"` if you want to name it. The PDF builder processes only the files listed in `config/pdf_build.json`. The test command runs native KiCad tests and then the PDF-builder tests using the Python that launched it. If that Python lacks `markdown-it-py`, install the dependency or supply `test --document-python <path>`; a missing dependency makes the command fail instead of silently counting skipped PDF tests as coverage.

## Requirements

- KiCad 10 and its bundled Python for native board operations. The launcher uses the configured KiCad Python path or `--kicad-python`.
- Python with Ruff for the code-health check, and Pillow for review graphics. PDF builds also need `markdown-it-py`, LaTeX, `dvisvgm`, and `pdfinfo`. They prefer PySide6 WebEngine plus PyMuPDF when available and fall back to Chrome or Edge.
- Java and a Freerouting JAR are optional for isolated comparison trials. Freerouting previews are not applied to the board automatically.

Use an action followed by `--help` for its options. The comparison in [the algorithm document](docs/algorithm.md) explains the relationship to [Freerouting](https://github.com/freerouting/freerouting) and [RePlAce](https://vlsicad.ucsd.edu/Publications/Journals/j126.pdf); AutoPCB has not yet been benchmarked as superior to either.

## Project files

| Location | Purpose |
| --- | --- |
| `../v4.1.kicad_pro`, `.kicad_sch`, `.kicad_pcb`, `.kicad_dru` | Saved KiCad project, schematic, board, and custom rules. |
| `design/` | Interface, power, placement, procurement, and stackup material. |
| [`../libraries/`](../libraries/) | Project-level KiCad symbols, footprints and 3D models. `L.pretty/` is registered as `Layout_Legend`. |
| `scripts/core/` | Command dispatch, file paths, and guarded editing shared by the other tools. |
| `scripts/pcb/` | Board routing, placement, copper repairs, and silkscreen tools. |
| `scripts/sch/` | Schematic layout, labels, connectivity cleanup, and library synchronization. Use `run.py sch` or `run.py schematic`. |
| `scripts/release/` | Build manufacturing review inputs and export Gerbers, drills, assembly files, and technical attachments. |
| `scripts/review/` | Render PCB and schematic views, update routing progress, and build the engineering preview package. |
| `scripts/verification/` | Read-only design audits and isolated regression tests. |
| `scripts/utils/` | PDF build helpers shared by the document tools. |
| `config/` | Active settings. New edit campaign recipes are written to `config/campaigns/`. |
| `results/` | Disposable routing plans, previews, and checkpoints. Routing tools create output directories as needed. |
| `docs/` | The mathematical formulation, workflow, and generated PDF. |
| `../deliverables/review/` | Current review images, PDFs, gallery, and progress history. |

Historical versions and backups are retained separately. Nothing in this workspace by itself authorizes a purchase or fabrication order.
