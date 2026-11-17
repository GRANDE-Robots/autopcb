# AutoPCB: placement and routing

I want the code to choose component positions, checkpoint order, tracks, vias, and allowed silkscreen positions together. The goal is a connected board with short, simple routes that satisfy the electrical, manufacturing, and placement constraints.

The current v4.1 working board has six copper layers. All component positions and
orientations, front silkscreen and the schematic are fixed for this routing pass.
The explicit `working_scope` in the active configuration permits connectivity
development on its named net classes while preserving USB/Ethernet pair copper,
user exclusions, native width/clearance rules and independent candidate checks.
This permission supplies no electrical ratings. Zero native open connections is
a connectivity milestone; unknown electrical limits, existing DRC findings and
reference-copper issues still prevent qualified completion and manufacturing.
Working saves use one current recovery journal and rebind the same run checkpoint
to the exact accepted saved board. The review set derives its inner-layer
PDF/PNG names and gallery count from the saved copper stack.

## 1. The full problem

Let $B_t$ be the board at checkpoint $t$, $x$ the allowed component placement, $z$ the allowed silkscreen positions, $q_t$ the pins and connections considered at that checkpoint, and $r_t$ the resulting tracks and vias. Let $H$ be the number of checkpoints and $k$ the maximum number of distinct pins considered in one checkpoint. A checkpoint may also connect a pin to existing copper on the same net.

For the final board, let $U$ be unintended open connections, $L$ track length, $L_{45}$ diagonal length, $V$ vias, $J$ bends after collinear merging, $D$ the layer-use penalty, $S$ symmetry error, and $M$ permitted placement movement. Let $C_{\mathrm{silk}}$ be the silkscreen cost in Section 4. Connectivity and symmetry take priority over the remaining weighted cost:

$$
C(B_H,x,z)=w_L L+w_{45}L_{45}+w_V V+w_J J+w_D D+w_M M+w_I C_{\mathrm{silk}}.
$$

Let $T_{\mathrm{search}}$ be the time spent selecting checkpoints, trying routes, and checking results. $\lambda_T$ sets how much computation time matters relative to the layout. The full problem is:

$$
\begin{aligned}
\min_{x,z,H,\{q_t,r_t\}}^{\mathrm{lex}}\quad & \left(U(B_H),S(B_H),C(B_H,x,z)+\lambda_T T_{\mathrm{search}}\right) \\
\text{subject to}\quad & B_0=B_{\mathrm{saved}}(x),\qquad B_{t+1}=A(B_t,q_t,r_t), \\
& q_t\in Q(B_t),\quad |P(q_t)|\le k,\quad r_t\in R(B_t,q_t), \\
& x\in X_{\mathrm{allowed}},\quad z\in Z_{\mathrm{allowed}},\quad B_H\in F_{\mathrm{complete}}.
\end{aligned}
$$

$A$ applies an accepted checkpoint to the board. $Q(B_t)$ is the set of allowed checkpoint choices, $P(q_t)$ is the set of pins in one choice, and $R(B_t,q_t)$ is the set of legal routes for it. $X_{\mathrm{allowed}}$ and $Z_{\mathrm{allowed}}$ fix protected components and text. $F_{\mathrm{complete}}$ requires all intended connections and the applicable rules to be satisfied. The time estimate depends on the search method and resources; actual time includes rejected candidates too.

Lexicographic order compares $U$ first, then $S$, then cost; a lower-priority score cannot compensate for a worse higher-priority score. Hard constraints cannot be traded against any score. At completion $U=0$. Default routing costs are 1 per millimetre, 10 per via, 1 per bend, and an extra 1 per diagonal millimetre. All six enabled copper layers have the same default layer cost. Equivalent channels require connectivity/role evidence and fixed local frames; the comparison tolerance is 0.05 mm. Unrelated nets are never matched by name alone.

Each $q_t$ and $r_t$ is part of the first minimization. A route changes $B_t$, which changes the options for the next checkpoint. A good route for one checkpoint must therefore leave room for the rest of the board. Section 7 distinguishes this target formulation from the implemented preview search.

## 2. Placement

For a movable component $i$, let $x_i=(p_i,\theta_i,\mathrm{side}_i)$ describe its position, rotation, and side. Fixed components keep their saved positions. For the current board, front-side placement and front silkscreen are protected; only explicitly allowed items can move.

A quick placement estimate is the weighted sum of each net's half-perimeter wirelength ($\operatorname{HPWL}$), congestion, important component spacing, and movement. HPWL is the width plus the height of the smallest rectangle containing that net's pins:

$$
\widehat C_{\mathrm{place}}(x)=\sum_n w_n\operatorname{HPWL}_n(x)
+w_C C_{\mathrm{congestion}}+w_P C_{\mathrm{proximity}}+w_M M.
$$

This estimates routing difficulty. It is not the actual routed length, and HPWL is not necessarily a lower bound when 45-degree tracks are allowed. Promising placements still need routing and rule checks.

## 3. Checkpoint order and routing

Let $s=(q_0,q_1,\ldots,q_{H-1})$ be a checkpoint order. The outer optimizer estimates the final layout cost $\widehat C$ and search time $\widehat T$ for candidate orders, then chooses an order:

$$
s^* \approx \arg\min_{s\in S_k(B_0)}
\left[\widehat C(s,x)+\lambda_T\widehat T(s,x)\right].
$$

$S_k(B_0)$ contains candidate orders that respect the pin limit. The estimates depend on the inner routing problem. I want the code to compare useful orders without generating every combination, and to update the order when the board changes. A net with more than $k$ pins can be connected over multiple checkpoints.

At checkpoint $t$, the inner optimizer chooses tracks and vias. Let $\Delta C$ be the change in layout cost and $\widehat V$ the estimated cost of completing the board afterward:

$$
r_t^* \approx \arg\min_{r\in R(B_t,q_t)}
\left[\Delta C(B_t,r)+\widehat V\bigl(A(B_t,q_t,r)\bigr)\right].
$$

If $\widehat V$ is zero, this only optimizes the current checkpoint. The code should retain alternatives or look ahead when a locally short route could block later work. Search limits or a failed candidate do not prove that the board is impossible to route.

Existing tracks and vias are decision variables too. A candidate may remove or
replace them, but must restore all previously required connections before it is
accepted. Let $E_t$ be the existing tracks selected for replacement and $V_t$ the
selected existing vias. For track $e$, let $d_e$ be the length in millimetres not
reproduced at the same position, layer and width in the candidate. Let $h_v=1$
when an existing via is not reproduced with the same position, drill, layers and
diameters, and zero otherwise. The physical rework penalty is:

$$
R(B_t,r)=w_{\mathrm{track}}\sum_{e\in E_t}\mathbf{1}[d_e>0]
+w_{\mathrm{length}}\sum_{e\in E_t}d_e
+w_{\mathrm{via}}\sum_{v\in V_t}h_v.
$$

The default rework weights are 1 per changed track, 1 per changed millimetre, and
10 per changed via. They are separate from the ordinary route-length, bend and
via costs. Identical copper represented by different UUIDs or collinear splits
has zero rework penalty. Candidate tree and board-beam ranking uses the routing
cost plus $R$; terminal refinement must reduce that total. This makes reuse
preferable when it offers a similar solution, while permitting replacement when
it helps connect the board. Hard constraints always take precedence. The target
board objective remains lexicographic: unresolved connections first, symmetry
second, routing and rework cost third. Full board-wide symmetry optimization is
not yet implemented in the preview runner.

### Constraints for each route

For a segment $s$, let $\theta_s$ be its heading, $w_s$ its width, and $\ell_s$ its layer. At bend $j$, let $\Delta\theta_j$ be the directed change in heading. Let $u\sim_t v$ mean that $u$ and $v$ are connected on board $B_t$. For each requested net $n$, let $\mathcal A_n(q_t)$ contain its selected pins and, when used, its selected existing-copper anchor. Anchors do not count toward the pin limit.

| Requirement | Mathematical statement |
| --- | --- |
| Connect every selected pin and anchor **within each net group**. Preserve existing terminal connections. | $u,v\in\mathcal A_n(q_t)\;\Rightarrow\;u\sim_{t+1}v$<br>$u\sim_t v\;\Rightarrow\;u\sim_{t+1}v$ |
| Do not short different nets; allow only explicitly modeled net ties. | $C_n^\ell\cap C_m^\ell\subseteq T_{nm}^\ell,\quad n\ne m$ |
| Each track runs horizontally, vertically, or at 45 degrees. | $\theta_s=m_s\pi/4,\quad m_s\in\mathbb Z$ |
| Every actual bend changes direction by an odd multiple of 45 degrees. Here a bend is a degree-two copper vertex away from pads and vias; degree-three or higher vertices are branching junctions. For a normal chamfer, two 45-degree turns give two 135-degree interior corners. | $\Delta\theta_j\equiv\pi/4\pmod{\pi/2}$ |
| Do not make repeated staircase patterns. | $N_{\mathrm{stair}}(r_t)=0$ |
| Use only permitted copper layers and via types. | $\ell_s\in\mathcal L_{\mathrm{allowed}},\quad v\in\mathcal V_{\mathrm{allowed}}$ |
| Keep the required gaps between unrelated copper, holes, keepouts, and the board edge. | $d(C_i,C_j)\ge c_{ij}^{\min}$ |
| Size tracks and vias for their verified current, copper thickness, temperature rise, and fabrication limits. Check voltage clearance and load voltage drop. | $w_s\ge w_{\min}(I_s,t_{\ell},\Delta T)$<br>$I_v\le I_{v,\max},\quad |\Delta V|\le\Delta V_{\max}$<br>$d(C_i,C_j)\ge c_{\min}(|V_i-V_j|)$ |
| Keep front-side components and front silkscreen at their saved poses. Leave objects unlocked and traces ungrouped. | $x_i=x_i^0\ (i\in\mathcal F)$<br>$z_j=z_j^0\ (j\in\mathcal S_F)$<br>$N_{\mathrm{locked}}=N_{\mathrm{trace\ groups}}=0$ |
| Protect the specified differential pairs, ESD paths, bypasses, and return copper. | $r_t\in\mathcal R_{\mathrm{SI,ESD}}$ |

Here $C_n^\ell$ is the copper of net $n$ on layer $\ell$; $T_{nm}^\ell$ is the explicitly permitted tie region between nets $n$ and $m$, or the empty set if no tie is allowed. For other clearances, $C_i$ and $C_j$ are the applicable physical features and $c_{ij}^{\min}$ is their required separation. $\mathcal F$ and $\mathcal S_F$ identify the fixed components and front text. The allowed sets, electrical ratings, and signal-integrity and ESD criteria must come from verified requirements; unknown limits are not guessed. The table states the intended constraints, and the scripts do not yet verify every one of them.

#### Counting staircases

First merge collinear pieces into straight segments. For one unbranched, same-layer path of $m$ consecutive segments, number each segment's direction $d_i$ in eighth-turns: $d_i=\theta_i/(\pi/4)\pmod 8$. The cardinal directions are $\mathcal H=\{0,2,4,6\}$ and diagonals are $\mathcal D=\{1,3,5,7\}$. The indicator $[\cdot]$ equals one when its condition holds and zero otherwise.

Standard two-chamfer doglegs are allowed. Let $E_i$ equal one when the triplet at $i$ is the middle of a complete five-run dogleg and zero otherwise, including at chain boundaries. Let $a$ be its first and last cardinal heading, $c$ its perpendicular middle heading, and $d$ the shared diagonal heading. Its five headings satisfy:

$$
(a,d,c,d,a),\qquad a,c\in\mathcal H,\quad d\in\mathcal D,
\quad c-a\pmod8\in\{2,6\},\quad d-a\pmod8,\ d-c\pmod8\in\{1,7\}.
$$

Count the remaining staircase windows:

$$
N_{\mathrm{stair}}=\sum_{i=1}^{m-2}
\left[d_i\in\mathcal D\;\land\;d_{i+1}\in\mathcal H\;\land\;d_{i+2}=d_i\right](1-E_i).
$$

Thus an isolated $45^\circ\!\to0^\circ\!\to45^\circ$ still counts once, while the standard dogleg $0^\circ\!\to45^\circ\!\to90^\circ\!\to45^\circ\!\to0^\circ$ counts zero. The repeated ladder $45^\circ\!\to0^\circ\!\to45^\circ\!\to0^\circ\!\to45^\circ$ counts twice. Overlapping windows are counted separately. For a branched route, stop chains at pads, vias and branch points; count each same-layer window once. Search carries at most three prior run headings and checks the complete candidate at the goal; cleanup and the independent native-track audit use the same dogleg exception. Unsupported geometry is reported separately and cannot establish completion.

Within those constraints, I prefer straight horizontal and vertical runs. The cost term $w_{45}L_{45}$ discourages long diagonal runs while allowing short 45-degree transitions. It is a preference, not a ban on diagonals; route length, vias, joints, symmetry, and electrical requirements still matter.

## 4. Silkscreen placement

For each text item $i$, let $z_i$ describe its position and orientation, $a_i(z_i)$ its anchor point, and $K_i(x)$ the set occupied by its component. I want the text close to that component, so use the point-to-set distance:

$$
d_i=\inf_{u\in K_i(x)}\|a_i(z_i)-u\|_2.
$$

Let $G_i(z_i)$ be the actual ink occupied by the text strokes, excluding whitespace inside and between characters. Let $M_{\mathrm{mask}}(B_H)$ be the regions where the fabrication rules remove silkscreen, $O_i(x)$ the projected component bodies that can hide it, and $G_{-i}(z)$ the other silkscreen ink. With $\operatorname{area}$ measured in square millimeters:

$$
\begin{aligned}
a_{\mathrm{clip},i} &= \operatorname{area}(G_i\cap M_{\mathrm{mask}}), \\
a_{\mathrm{hidden},i} &= \operatorname{area}((G_i\setminus M_{\mathrm{mask}})\cap O_i), \\
a_{\mathrm{overlap},i} &= \operatorname{area}(G_i\cap G_{-i}).
\end{aligned}
$$

The silkscreen objective combines distance and lost or overlapping ink. Its coefficients control the tradeoffs:

$$
C_{\mathrm{silk}}=\sum_i\left(\alpha_i d_i+\beta_i a_{\mathrm{clip},i}
+\gamma_i a_{\mathrm{hidden},i}+\delta_i a_{\mathrm{overlap},i}\right).
$$

Use the actual text strokes, not just a rectangular text box. A component body can hide text without removing it. A covered copper trace does not by itself remove silkscreen; solder-mask openings and the actual fabrication rules determine clipping. Prevent prohibited overlaps as hard constraints when required. Protected text, including the current front silkscreen, stays fixed.

For a completed board and placement, the allowed silkscreen pass is:

$$
z^*\approx\arg\min_{z\in Z_{\mathrm{allowed}}} C_{\mathrm{silk}}(z;x,B_H).
$$

## 5. Mathematical workflow

At each checkpoint, the output board is the input to the next:

$$
B_0(x)\;\xrightarrow{\;q_0,r_0\;}\;B_1
\;\xrightarrow{\;q_1,r_1\;}\;\cdots
\;\xrightarrow{\;q_{H-1},r_{H-1}\;}\;B_H.
$$

<div style="width:82%;margin:0.10in auto;break-inside:avoid;font-family:Arial,sans-serif;font-size:8.6pt;line-height:1.35;">
<div style="border:1px solid #39778b;border-radius:6px;background:#edf7f3;padding:2px 9px;text-align:center;break-inside:avoid;">Start with the saved board and allowed placement<br>$B_0=B_{\mathrm{saved}}(x)$</div>
<div style="text-align:center;color:#24637a;line-height:1.0;font-size:9pt;">↓</div>
<div style="border:1px solid #39778b;border-radius:6px;background:#edf7f3;padding:2px 9px;text-align:center;break-inside:avoid;">Estimate candidate checkpoint orders and their effort<br>$s^*\approx\arg\min_{s\in S_k}[\widehat C(s,x)+\lambda_T\widehat T(s,x)]$</div>
<div style="text-align:center;color:#24637a;line-height:1.0;font-size:9pt;">↓</div>
<div style="border:1px solid #39778b;border-radius:6px;background:#edf7f3;padding:2px 9px;text-align:center;break-inside:avoid;">Choose the next bounded checkpoint<br>$q_t\in s^*,\quad |P(q_t)|\le k$</div>
<div style="text-align:center;color:#24637a;line-height:1.0;font-size:9pt;">↓</div>
<div style="border:1px solid #39778b;border-radius:6px;background:#edf7f3;padding:2px 9px;text-align:center;break-inside:avoid;">Search candidate tracks and vias<br>$r_t^*\approx\arg\min_{r\in R(B_t,q_t)}[\Delta C(B_t,r)+\widehat V(A(B_t,q_t,r))]$</div>
<div style="text-align:center;color:#24637a;line-height:1.0;font-size:9pt;">↓</div>
<div style="border:1px solid #39778b;border-radius:6px;background:#edf7f3;padding:2px 9px;text-align:center;break-inside:avoid;">Check connectivity, geometry, and electrical limits; retry if needed<br>$r_t\in R(B_t,q_t)$</div>
<div style="text-align:center;color:#24637a;line-height:1.0;font-size:9pt;">↓</div>
<div style="border:1px solid #39778b;border-radius:6px;background:#edf7f3;padding:2px 9px;text-align:center;break-inside:avoid;">Accept the result and repeat with the new board state<br>$B_{t+1}=A(B_t,q_t,r_t)$</div>
<div style="text-align:center;color:#24637a;line-height:1.0;font-size:9pt;">↓</div>
<div style="border:1px solid #39778b;border-radius:6px;background:#edf7f3;padding:2px 9px;text-align:center;break-inside:avoid;">Optimize permitted silkscreen and run closing checks<br>$z^*\approx\arg\min_{z\in Z_{\mathrm{allowed}}}C_{\mathrm{silk}},\quad B_H\in F_{\mathrm{complete}}$</div>
</div>

## 6. Running and retaining checkpoints

The primary workflow is `pcb autoroute prepare`, `run`, `status` and `verify`
through `run.py`. Durable policy lives in `config/autopcb.json`; generated
preview checkpoints, pending work and validation records live in
`results/autoroute/<run-id>.json`. A checkpoint is bound to its saved design,
dependencies, policy and implementation. It never changes the saved PCB.

Use `--resume RUN_ID` when those inputs match. After a code change, use
`--renew RUN_ID`: revalidate the retained candidate and rebuild its queue with
current code. Keep pending priorities, escalation stages, and a geometry-free
repair preference, but regenerate dimensions and removal candidates. Renewal
creates a new run and preserves the
old record. Changed design inputs or policy require a new run.

Unexpected errors and unsupported repair requests stop the run and preserve
the last committed preview and queue. A bounded search that finds no candidate
remains unresolved; it does not prove the board cannot be routed.

The legacy `pcb checkpoints` commands remain available for bounded per-net
experiments and sequential previews. Their three-pin limit is a local
decomposition, not a board-wide connection limit. Do not replay historical
campaigns automatically. A board-wide repair transaction can include several
nets and all the fragments whose connectivity must be restored.

## 7. Implemented behavior and current limits

**Search and rules.** The runner inventories every net, ignoring the old six-net
include list as a coverage limit. It schedules independent windows in batches.
An optional single-net search mode gives each worker one ordinary net instead
of dividing its search slice among a group. Independent candidates can still be
checked together; coordinated repairs remain multi-net transactions. The mode
is recorded in the run state and retained across resumption.
Selected copper-island sets and exact failed requests are deduplicated. A changed
candidate or budget permits another attempt; exhausted offsets do not wrap.
Fourteen classified USB/Ethernet nets remain explicitly deferred.
Unqualified power, protection, safety and unclassified nets remain in the
inventory with explicit blockers; the main planner and reference-repair search
do not schedule them until the electrical model can qualify the work. Automatic
obstruction repair also excludes their existing copper from rip-up candidates.

In first-feasible mode, a bounded warm start samples direct chamfered routes
through one existing or new via when the endpoints are on different layers.
The via clearance model includes copper on every layer, and each candidate still
passes exact edge, endpoint-heading and route-tree checks. A failed sample falls
back to the grid search; a successful one is not a proof of optimality.
For same-layer endpoints, the warm start also samples two via escapes and a
straight or chamfered path on another permitted layer. The two escape legs may
not cross or touch the opposite via. The bounded local sample includes off-axis
sites and prefers eligible existing vias. This search uses the same obstacle and
endpoint checks, and a proposal still needs independent native validation.
When those simple transit paths are obstructed, the same escape sites feed a
bounded single-layer detour search. It tries at most three pairs on each
alternative layer, with half the coarse grid step when the existing cell cap
and exact integer lattice permit it. Congestion orders the alternative layers
without changing their costs, and a reserved share prevents early layers from
exhausting the complete detour budget. This extra search uses at most 40,000 expansions, capped at one
quarter of the configured per-search expansion limit, and remains subject to
the shared deadline. The complete route, including both escapes and vias, is
checked against its existing tree before acceptance. The original multilayer
search remains a fallback; exhausting this sample does not prove infeasibility.
For longer warm paths, bounded standard-dogleg samples can use an interior
horizontal or vertical corridor between two 45-degree chamfers. The simple
paths are tried first, and both families use the same exact edge, heading,
minimum-segment and complete-context checks.

The optional zone-stitch operation refills the private board before identifying
ground islands. A zone UUID can represent several disconnected polygons, so the
search also inventories the smaller pad/copper islands rather than relying only
on DRC endpoint names. It first tries plane-joining vias, then short octilinear
pad exits. Track pieces follow the saved package-neck and trunk rules. Native
connectivity must improve before a trial survives; independent DRC, drill/pad,
reference-copper and preservation checks still govern retention.

Outside the explicit working-connectivity scope, the operation requires matching,
source-bound normal, transient and fault assessments for the entire island.
The present branch calculator cannot independently qualify those inputs, so
electrical acceptance remains blocked. Within the explicit working scope, a
connectivity proposal may use the class via or a smaller ordinary through via
meeting the saved drill, diameter and annular minimums. These are geometric
design limits, not a verified current rating. Search is bounded and may leave
an island unresolved; it never reports that result as electrical completion.

The external rework command also supports `--renew-net-trees NETS`: remove the
editable copper of a batch of unfinished, permitted nets on a private board and
route those complete trees again. Protected nets, grouped/locked copper and
component placement remain unchanged. The combined result must restore every
previously required connection, pass the same independent checks and reduce
opens before replacing the retained checkpoint. This allows topology replacement
without a separate framework or individual prepared routes. Temporary external
router work is owned by a cleanup context outside the private project.

With `--include-connected`, this same bounded batch can rebuild nearby permitted
connected signal trees as well. Unfinished trees come first; remaining capacity
selects nearby signal copper using native bounding-box distances. Ground,
high-speed pairs and protected nets are excluded. A replacement with equal
connectivity may remain in the temporary layout to free space for other nets;
lost prior connections or extra islands force restoration. Final acceptance
still requires fewer total opens and the independent native checks. The batch
maximum remains 64 nets and no component pose changes.

With complete-tree renewal, `--ground-first` can try short ground access before
and after rebuilding the removed trees. It requires an explicit working-connectivity ground
scope. Every original required connection must be restored in the combined
transaction; an intermediate ground join cannot be retained by itself. This is
geometric routing work, not a verified ground-current or protection assessment.

Native reference-copper comparisons refill both boards under the same saved
project and custom rules. A renamed prior board is copied into an owned temporary
context with the same project basename; missing basename-matched settings cannot
silently cause a comparison against KiCad defaults. Both temporary contexts are
cleaned on success and failure.

Completed unsuccessful rework attempts record the exact removed-item and pose
identities in the existing run state. Restarting the same search skips those
plans and explores other neighborhoods. This memory is bound to the current
preview, saved inputs, rules, code, router and search budget, and holds at most
128 failed identities. Changed contexts invalidate it; an explicit
`--retry-exhausted` can request a repeat. Reaching a bound leaves the search
unresolved and cannot establish infeasibility or completion.

If a large additive batch breaks an existing connection, bounded recovery tests
balanced sub-batches rather than assuming one removed net can resolve every
conflict. Each subset still passes the full native checks. `--keep-placement`
persists a full-footprint placement/geometry guard in the native run and disables
bottom-placement recovery for that run.

Native pad, hole and via inventories use the enabled physical stack. Via reuse
checks every observed annulus and all required physical layers, without requiring
nonexistent layers. Short ground access uses the same saved via floors for
barrel-only and trace-plus-via candidates. When inflated search envelopes reject
an exact copper gap, local proposals can use KiCad's native copper-shape collision
test; drill/keepout envelopes and final independent DRC remain enforced.

External proposals also screen ordinary tracks and through-vias against local
USB/Ethernet reference reservations before native validation. Inner-layer
high-speed tracks reserve local corridors on both adjacent layers; the sampled
check selects the best covered adjacent ground polygon and flags loss of a
previously continuous reference even if the selected layer changes. Sampling
does not establish impedance or return-current performance.
Paired-route diagnostics can also list nearby non-ground copper around segments
with incomplete sampled reference. Those conservative candidates are not proof
that a particular item caused a reference gap.

A separate via
pruning objective can retain a private candidate at the same open count only
if it adds no copper, reduces vias, does not increase track length, preserves
connectivity and introduces no native or style violations. A removed via can
reclassify an existing copper junction as a two-segment joint in the progress
metric; the exact removal-only copper comparison ensures that no new joint was
created. The via-pruning search skips ground, critical nets, nets with zones,
pad-connected vias, and locked or grouped items, and stops a dead-end spur at
interior or two-ended contacts.

Reported dangling signal-track tails can also be screened for removal. The
cleanup defers entire affected nets if removing their tails creates a new
dangling warning or exposes a routing-style issue. Endpoint evidence detects a
new dangling end even when KiCad retains the same track ID. A retained equal-open
candidate must remove only tracks, reduce warnings, preserve existing
connections and protected items, and pass the sampled-reference comparison.
When a combined cleanup fails, bounded retries test smaller independent branch
groups; overlapping tails remain together and every subset gets the same checks.
When a routed junction splits existing copper, tail cleanup preserves any
endpoint required by that candidate's connection contracts.
The separate guarded working-save operation repeats the native checks and
records a recoverable pre-save checkpoint; it does not approve manufacture.
It currently rejects changed copper on identified protection and actuator-safety
nets, user-excluded nets, and non-signal or unclassified net classes, as well
as changes to footprints on those nets. A narrowly scoped exception permits a
removal-only cleanup of a track no longer than 0.05 mm or one tenth of its width,
whichever is smaller, when it is individually isolated from all same-net tracks,
vias, pads and zones. The candidate selector requires a native dangling warning, and its
error-only mode also requires a native error; the save guard independently
rechecks isolation, and excluded or high-speed nets remain protected. Native DRC,
existing connectivity and sampled reference checks still apply. This is a
conservative screen, not complete electrical qualification.
Cleanup may evaluate a work-in-progress baseline with attributable native
errors, provided the candidate introduces no new or changed error and passes
the independent checks. For a fully identified schematic parity conflict,
dangling-track and staircase cleanup also defer both implicated nets; an
unidentified conflict stops the check. Its non-track proof compares complete top-level records
and their multiplicities, so KiCad's record reordering is not mistaken for a
physical change. Existing errors still prevent completion.
Shortcut selection rejects positive-length overlap with untouched same-net
copper and may try a different path; shared endpoints remain permitted.

An independent style-repair objective tests exact horizontal, vertical and
45-degree intersections for an unanchored two-track joint. Its displacement is
bounded by 0.05 mm. It excludes protected and high-speed nets and refuses
branches, pads, vias, arcs, locked or grouped tracks and new style defects. Retention and working save each recompute the track-attribute and
endpoint proof, compare native DRC and existing connectivity, and check sampled
reference copper. The repair reduces an off-angle finding; unchanged open
counts are not counted as newly routed connections. Transistor terminal nets
are conservatively protected even when their names do not reveal their role.

For export-rounding offsets of at most two nanometres, new copper can be
translated to existing vias when all nearby via anchors agree; existing copper
is unchanged and the translated proposal still requires the usual checks.
For an unambiguous endpoint within one micrometre of a via, an unanchored
two-segment joint may also shift to center that endpoint while preserving both
octilinear headings and the other endpoint. Branches, existing contacts and
ambiguous nearby vias prevent this adjustment. The same adjustment may capture
an endpoint already inside a same-net via's round copper, with movement bounded
to 0.5 mm; native clearance, connectivity and style checks still apply.
For a single proposed terminal track whose other endpoint is anchored to a pad
or via, cleanup may instead try the shortest octilinear paths between that
anchor and the via center. It screens the alternatives for conflicts and
intermediate same-net contacts, retains both endpoint anchors, and leaves the
whole candidate subject to the same independent native checks.
For a new off-angle external trace, cleanup also tries both shortest two-segment
horizontal/vertical and 45-degree alternatives. It preserves the endpoints,
net, layer and width, and screens each alternative against saved copper,
compiled rules and high-speed reference reservations. Existing and excluded
copper is never rewritten by this step; unsuccessful traces remain omitted.
The resulting batch still needs native DRC, connectivity, routing-style,
reference and electrical checks.
Conversely, if a newly proposed via overlaps a unique existing trace endpoint,
cleanup may center the via on that endpoint and recenter its proposed escapes.
It declines ambiguous or pad-touching cases and checks preservation of existing
contacts before the normal native clearance, connectivity and style checks.

Cleanup rejects nets that create extra isolated islands, temporarily defers
pruned nets that did not improve, and checks its time budget during local search.
Staircase findings identify the tracks in the affected directional runs rather
than the whole connected chain. Local repair can replace a connected defective
section while treating neighboring proposal copper as fixed contacts; native
DRC and connectivity checks still apply. A prune prepared before timeout gets
one final native check, with rollback if it worsens connectivity.
If a combined batch creates an attributable new native violation or an
ambiguous same-net overlap, cleanup can defer the implicated proposed nets and
retry a bounded subset. Unidentified errors still stop the check, and no
subset is retained until it passes the same independent validation.

Local repair samples 0.2, 0.1 and 0.05 mm grids within the same time and cell
limits. These change search resolution only; width, clearance and electrical
constraints do not shrink. A search or memory limit remains an unknown result.
When a narrow rejected section cannot be repaired, the search may also include
up to four adjacent proposed segments. It stops at pads, vias, branches or old
copper and rechecks the replacement against the same constraints.
It can prune dangling proposal branches through vias together using native direct
connectivity, stopping at existing copper, pads, junctions, or nets with zones.
The disposable candidate is rechecked and the prune is rolled back if its total
open count increases; final acceptance still checks prior connectivity per net.

Optional focused fast batches first use a fresh native DRC to select unfinished
nets in the requested classes and temporarily defer the others. This is a search
scope only: unexamined nets remain in the board-wide inventory and block completion.
The optional pinned KiCadRoutingTools adapter proposes ordinary-signal routes
directly on a private native-board copy. It uses equal layer costs without
per-layer direction preferences, retains input copper, disables rule relaxation
and submits its additions to the same independent filter as other external
proposals. An optional search-grid spacing does not change physical design rules.
Differential pairs and unqualified power or ground routes remain deferred outside
the explicit working-connectivity scope. That scope can enable geometric
power/ground proposals under the saved native rules; it does not supply missing
electrical ratings or remove their completion holds.
The optional full search runs KRT's recovery and finishing passes within the
requested time limit; quick search remains the default. Mirrored and rotated
recovery uses a fresh coordinate-dependent cache, leaving the original cache
and pinned tool checkout unchanged.
Before retaining or saving a proposal, the native-board guard rejects new via
drills intersecting pad copper on any enabled copper layer. Existing
intersections remain completion blockers; this check does not qualify a
manufacturer's via-in-pad process.
Proposal filtering also blocks new or worsened narrow-copper connection warnings
that KiCad attributes only to existing objects. Relocated markers may match an
existing warning only when the complete net's copper and pads are proven
unchanged. Exact existing findings remain recorded and final independent
validation is still mandatory.
The adapter sets a 10 mm-equivalent via penalty and a turn penalty. Its search
objective is an approximation: it does not encode the complete diagonal or
matched-channel symmetry costs, and a found route is not a global optimum.
Coordinated rework can use the same adapter after a private, bounded removal of
eligible signal copper. The whole transaction must reconnect all previously
connected copper and pass the independent checks before it can be retained.
For local cleanup, a wider safe repair window is tried promptly. Each grid
attempt first tries the original layer without a new via, then allows every
enabled copper layer at equal preference if needed and time remains. Either result still goes
through the same geometry, connectivity, clearance and native DRC checks.
The local search includes proposal vias as obstacles with the native hole
clearances and identifies existing vias by their actual centers. A first-feasible
repair does not prove the cheapest route. The obstacle model is reused for one
unchanged board, project-rule set, net and refill setting; changed inputs force
recompilation. If partial repair stalls, one shorter-section-first ordering is
tried within the same budget. Following a KRT strict-size refusal, an additive
ordinary-signal batch may discard all nets in a complete, unambiguous final size
report and submit the remaining copper to independent checks. Net numbers are
resolved using the pinned router's parser on the unchanged input. Without that
report the whole proposal is discarded. Coordinated rework may discard refused
replacement trees only when the complete report identifies requested target nets;
their original trees must be restored before independent combined validation.
An unexpected net stops the trial. Pair searches do not use partial salvage,
and no refused net is treated as routed.
Temporary aliases for deferred nets inherit exported cross-class clearance
rules, including relationships between two split classes. Native KiCad checks
remain necessary because a DSN export may omit other custom rules.
The optional `--search-pad-neckdown` setting lets fastroute or Freerouting
propose smaller pad exits. Saved width rules, including their package-local
scope, still apply to every generated segment; native DRC and independent
retention remain mandatory. This is a search proposal, not a current rating or
permission to narrow a power trunk. The default is off.
Saved package-local width and clearance exceptions can override Board Setup
defaults, as checked against native DRC inside and outside the package and on
inner layers. Explicit caller-supplied electrical floors remain hard constraints.
The resolver does not turn a local exception into a whole-net allowance.
External proposals use the same saved-width materializer as native search.
The external adapters permit up to eight routing passes within the existing
timeout, allocating one pass per 15 seconds of search budget.
It splits package/trunk regions and widens undersized pieces; it never thins a
proposal or edits existing copper. The resulting geometry still needs clearance,
angle and native validation. A 50,000-segment fragmentation limit bounds the
operation; exceeding it rejects the candidate. Working ground repair may add
checked joins after rebuilding a tree, while preserving every old ground track
and via and all other protected copper. Any added join triggers fresh native
validation; all prior connections must survive before retention.
Proposal cleanup may cross width transitions only with this saved-rule
materializer. It keeps pads, vias, branches and old contacts fixed, and checks
the rematerialized shortcut against clearance and native rules. Necessary
width transitions are distinguished from redundant same-width collinear joints.
The `--ground-contact-guard-mm` setting adjusts only the extra search margin
around actually filled ground near pads: 0.5 mm by default, bounded from 0 to
2 mm. Zero does not remove physical pads, clearance rules or USB/Ethernet
reference reservations. A candidate that breaks existing ground connectivity
is rejected. Both settings participate in the source-bound attempt identity.
On the temporary DSN, source-identified copper zones on signal layers are not
exported to the router as solid planes. This avoids assuming pads are connected
through an interrupted fill; native zones are retained and refilled during
validation. Other planes and keepouts are preserved, and unsupported plane forms
stop the trial. This improves the search model but does not establish a valid
route or reference return path.
If fastroute exceeds the external time limit, a structurally complete session
checkpoint can still be imported on the private board and subjected to the same
source, geometry, connectivity, electrical and native checks. A truncated or
missing session is rejected; a checkpoint is not an accepted route by itself.
Both focused and broad fast batches also defer identified protection and safety
nets, configured exclusions, and non-signal or unclassified nets.
The focused runner records failed deferrals and bounded-search exhaustion against
the exact candidate, inputs, tools, router and search settings. Repeating the
same bounded request reports `deferred_repeat` without running it again. Changed
inputs or an explicit retry permit a fresh attempt; no infeasibility is inferred.

The experimental coordinated-repair command selects a bounded set of nearby
unlocked copper on explicitly named signal classes. It excludes ground,
unclassified or power nets, user-excluded nets, classified USB/Ethernet nets,
and nets identified as protection or actuator-safety related from the available
footprint descriptions and net names. The same exclusions apply to the follow-up
search, and protected track and via geometry is compared before retention. This
classification is a conservative screen, not a complete electrical safety proof.
Trials rotate through eligible targets before revisiting alternate neighborhoods.
Each repair pass combines the remaining queue with a fresh inventory of eligible
unfinished nets; consuming the ordinary queue cannot hide later repair targets.
Nets that are now blocked are removed from older pending requests.
Within one batch, the checked, refilled baseline can be reused only while the
saved inputs, tools, policy, preview, blocked nets and search scope match. The
combined candidate and prior connectivity are still independently rechecked.
The command removes copper only on a private copy, tries the target
and affected neighbors together, and re-inventories the intermediate board so
that newly interrupted neighbors are included in a follow-up search. A candidate
can use Freerouting, fastroute or KiCadRoutingTools for either routing stage;
the Freerouting JAR is checked for changes throughout the private attempt.
An expired Freerouting search is unresolved, not proof that a route is impossible.
If the initial backend returns a known rejected, stalled-filter or no-safe-proposal
result, an explicitly selected different follow-up backend gets one attempt on
the same unchanged private input. No rejected copper is passed between engines.
Native violations naming only an existing zone or pad reject the whole candidate
when safe pruning is not possible. Unexpected execution failures, malformed
results and input mutations still stop the transaction. The actual attempted
backends are recorded in retained-result provenance.
An optional bounded sequence of follow-ups can continue from a changed
intermediate layout even when it has not yet reduced the original open count.
Identical copper stops the repeat, and intermediate native-error checks remain
in force; only the final independently verified improvement can be retained.
The final candidate
is retained only if it reduces opens and independently preserves previous
connectivity, protected items, native checks, routing style and sampled reference
copper. New tracks and vias on a reworked net cannot be narrower or smaller than
the corresponding selected copper. This is a conservative dimensional floor,
not verification of current, voltage drop, protection or impedance. Search
limits and failed repairs remain unresolved results. A repair that restores
identical copper without reducing opens tries each follow-up scope once per
batch sequence, then skips repeats of that scope; the same obstruction may
still be considered for a different target. Failed
local repairs may be deferred for the rest of that batch sequence, but the
current target and all of its affected neighbors remain eligible for its
coordinated retry. Later trial plans temporarily skip those failed nets until
the next batch sequence or an improved preview.

The optional fast-placement mode samples native poses for bottom-side signal
resistors. It can also test a 180-degree rotation around a symmetric protected
pad, including a ground pad, that remains at the same position and orientation
modulo 180 degrees; only the other, eligible signal pad is moved. Protected
tracks and vias are excluded from removal, and a user-excluded net cannot be
used as a protected anchor. Single-member footprint groups can remain intact;
shared, nested and locked groups are deferred. Same-net resistors are considered even when they
are far from the sampled checkpoint endpoints. This is a private geometry trial:
the current working-save guard still rejects changes to footprints touching
unqualified nets. Any moved nets and previously connected islands remain subject
to independent checks; neither a valid pose nor a clean DRC qualifies current,
protection or signal integrity.

The nonpublishing `pair-probe` can try fastroute or KRT paired routing only
after matching net classes, component endpoints and pin polarity. For fastroute
it subtracts export/import artifacts; for KRT it compares with the unchanged
native input. It checks extracted proposals with native DRC and reports geometry,
new via-in-pad overlaps and sampled-reference findings. It discards the trial; target
gaps, disabled skew tuning, impedance and electrical compatibility remain
unverified and cannot establish routing completion. Its per-pair summary
attributes opens and segments with incomplete sampled reference; these counts
do not certify the pair. `--pair-index` can test one complete discovered pair
while deferring all other pairs without changing their routing rules. Reference
checks still include all classified USB and Ethernet nets. A KRT timeout
discards the partial candidate and reports an unresolved search. An optional
run ID records diagnostic time without
counting the discarded copper as saved progress.

Checkpoint selection compares every retained access point using a bounded
nearest-pair search. Retained points are still a sample of the full copper.
The tree search first tries the configured number of nearest access pairs
(default 12). If none can extend that tree, it can try farther retained pairs
(default total 96), subject to the existing path-call and time limits. This is
still a finite search, not a proof that no route exists.
Existing configurations that set a primary sample size retain that hard limit
unless they explicitly set a larger fallback.

When a tree search has several starting islands, it shares the remaining
path-call and time budgets between those starts instead of allowing the first
one to consume every call or all the available time.
When several distinct net orders are available, each order reserves part of
the remaining batch time for alternatives. The final order may use the
remaining time. A time-limited order is not recorded as an impossible route.
When a grouped search reaches a limit on an independently attempted net, the
controller retries that net by itself at the same checkpoint before escalating.
Only an independently completed, unlimited failure can skip that retry; missing
limit evidence is treated as inconclusive.

At a via, the heading on the next layer starts afresh: changing direction
between layers is not a planar bend. Same-layer bends still obey the angle
rules, and two consecutive layer changes at the same point without intervening
copper cannot be used to bypass them.

Search and materialization share supported saved width/clearance rules, native
net-specific branch rules and explicit electrical floors. A net-class routing
preset cannot enlarge a deliberately specified branch minimum; broad unnamed
rules keep the conservative class default unless a local exception is proven.

External-router DSN exports use the same compiled whole-net width bounds, splitting
temporary classes while inheriting their via settings and cross-class clearances.
Local package exceptions cannot lower the exported trunk width; the imported
segments still pass the exact saved-rule materializer and independent native checks.
Runtime grid spacing and weighted-search greediness can be selected independently
of physical dimensions and the route-cost function. The same bounded controls
apply to initial searches, per-net overrides and temporary repair follow-ups.
Connected-neighbor renewal ranks native copper separation on shared layers;
overlapping bounding boxes on different layers do not imply an obstruction.
Fixed-placement copper repair honors the explicitly permitted working classes,
including power; it does not reuse the signal-only component-movement gate.
Mixed old track widths do not override scoped saved trunk and escape rules.
Independent electrical floors and existing via dimensions remain protected.
Endpoint alternatives share a starting island's budget. While alternatives
remain, a single attempt gets at most `search.endpoint_seconds` (default five
seconds); the parent time and path-call limits still apply. Timeout is an
unknown search outcome, never a cached proof that an access point is impossible.

The `pcb branch-widths` compiler reads terminal identities and normal-load targets
from `config/branch_widths.json`. Let $P$ be the physical pad vertices and $L$
the explicitly bounded load terminals in the native copper-contact graph. For
a cut track $e$, deleting its vertex must leave exactly one component containing
an unclassified terminal. Only the other components may receive a tap allowance:

$$
I_e=\sum_{p\in P_{\mathrm{downstream}}}I_p,
\qquad P_{\mathrm{downstream}}\subseteq L,
\qquad I_e\leq I_{\mathrm{tap,max}}.
$$

Unknown downstream terminals and zone-mediated sharing receive no allowance.
The contact graph includes net-assigned copper graphics. For an endpoint
junction, let $C_e$ be the contacts to the upstream component and let
$\pi_e(c)$ be the unique endpoint where contact $c$ meets track $e$.
A cut track has one proven upstream junction if all contacts share its endpoint:

$$
\pi_e(c)\text{ is defined for every }c\in C_e,
\qquad \left|\{\pi_e(c):c\in C_e\}\right|=1.
$$

Straight centerlines and supported convex pad shapes supply these port checks.
A second test accepts a single retained copper landing if a track enters that
landing at one endpoint and every secondary contact lies entirely inside it.
The compiler can check a connected group of explicit segments together. Its
removal must leave the upstream pads connected and isolate only bounded
terminals. All contacts from the group to the retained side must fit the same
landing. Contacts outside it require the general width or a fixed,
geometry-scoped rule. The LTC4367 VIN and VOUT controller pins
have documented small pin-current bounds in the maintained configuration;
these allowances never apply to the external MOSFET load path or capacitor surges.
The source graph, component identities and full terminal inventory of an entire
rail are checked before rule generation. The resulting tap rules match exact
existing geometry and expire when it changes; new-route search does not inherit
coordinate exceptions. Native checks compare the same private PCB before and
after the rule edit, exposing all width findings while rejecting unrelated new
defects or changed connectivity. Only the rule file is published atomically.
This is normal-current rule compilation, not fault-energy or thermal qualification.

The saved 3V3 distribution uses a 0.30 mm minimum with a 0.50 mm preferred width
for a 250 mA external-load allocation; timing distribution uses 0.40 / 0.50 mm
for the TLV758P 500 mA rated-output sizing case. Package escapes are scoped to
their documented terminals. Neither a regulator rating nor a fuse marking is
promoted into a proven fault-current limit.

The `pcb repair-widths` procedure changes only existing segment width fields.
Let $w_e^0$ be the saved width of segment $e$, $w_e^{\min}$ its native rule
minimum and $w_e^{\mathrm{pref}}$ its preferred configured width. Proposed edits
obey the following bounds; an obstructed edit is deferred rather than narrowed:

$$
w_e\geq\max(w_e^0,w_e^{\min}),\qquad
w_e^{\mathrm{trial}}=\max(w_e^0,w_e^{\min},w_e^{\mathrm{pref}}).
$$

The controller tests batches against the complete native DRC baseline, reduces
preferred widths to minima where necessary, then partitions interacting edits.
Accepted copper is retained through the remaining trials. Every saved candidate
preserves existing connectivity and protected items, adds no DRC findings and
does not degrade sampled USB/Ethernet reference copper. Deferred traces require
rerouting; a partial width repair is never reported as complete routing.
Every accepted width trial recompiles the branch proofs on its changed copper;
an old tap allowance that loses its proof blocks the trial.
Each proven tap's coordinate selector uses a 2 nm window to tolerate native
numeric conversion. It is emitted only when no other trace or arc on that
layer and net can match either endpoint order.
For zone-free nets, width-only increases cannot shrink their copper union.
Existing native neck warnings may therefore be matched by net, measured width,
severity and multiplicity when KiCad assigns them to another adjacent segment.
Nets with copper zones retain strict spatial comparison.

The `pcb repair-widths --prune-covered` mode instead removes a flagged segment
only after proving that its entire swept capsule is inside one retained,
same-net convex SMD pad on the actual layer. The existing circumscribed-polygon
containment check is reused. Every retained copper object and the independently
refilled zones must remain identical, as must all protected items and required
connections. This changes redundant track metadata, not physical copper width.
Native warning markers may be reassigned only with this unchanged-metal proof
and identical measured widths, severity and multiplicity.

During detour repair, native neck markers may also be reassigned within a
connected, unchanged local copper region. This comparison requires identical
reported measurements, severity and multiplicity, a native minimum no larger
than 0.50 mm, and a 1 mm exclusion halo around every changed copper item on
the relevant layers. Zone-mediated and unsupported arc nets remain strict.
Protected footprint geometry is checked before this comparison. The warnings
remain present in the native result; changed measurements and new necks are
rejected.

With `pcb repair-widths --reroute`, requests are generated from the current
native width findings rather than hand-selected pins. A bounded heading-aware
search replaces bounded, uniform-width series runs at their fixed external
contacts. Pads, vias, branching junctions, locks and groups stop run extension;
obsolete internal corners can move. It tries same-layer detours at the required width, preserving all fixed
component poses and silkscreen. USB/Ethernet signals are excluded from this
single-ended repair stage. All enabled layers have equal cost, and each candidate
uses one layer without intermediate transitions. No via is added or enlarged.
An endpoint on an unbranched SMD pad may move within that same fixed pad.
The native contact set must be identical after changing the landing.
An alternative layer is permitted only for a pure series run between two existing
through vias: neither end may acquire another current branch or a pad contact,
both used barrel paths must shorten, and nominal copper must be at least as thick.
The replacement is checked again for contacts with existing copper.

Let $j_0$ be the original layer's position in the physical stack, $j_a$ and $j_b$
the external contact layers at its two end vias, and $j$ a candidate layer.
These physical restrictions require

$$
j\in[\min(j_0,j_a),\max(j_0,j_a)]
\cap[\min(j_0,j_b),\max(j_0,j_b)],\qquad
j\notin\{j_a,j_b\},\qquad t_j\geq t_{j_0}.
$$

Here $t_j$ denotes the nominal saved copper thickness. Staying at $j_0$ remains
an option. No assumed via-plating rating is introduced: existing via geometry
and series current are retained, and each used barrel distance decreases.
The saved width profile splits new copper into short package escapes and wider
distribution pieces; search and materialization use the same dimensions.
Let $C$ denote the removed series run and $R$ the replacement pieces. For each
original piece $d$, $L_d^0$ and $w_d^0$ denote its length and width; for each
replacement piece $e$, $L_e$ and $w_e$ denote its length and width. The nominal
saved layer thicknesses are $t_d^0$ and $t_e$, respectively. The resistance
screen and source-width floor require

$$
\sum_{e\in R}\frac{L_e}{w_e t_e}\leq\sum_{d\in C}\frac{L_d^0}{w_d^0 t_d^0},
\qquad w_e\geq\max_{d\in C}w_d^0.
$$

That screen does not qualify the original current, fault or thermal design.
Each retained proposal must reduce native width findings without introducing
new undersized segments, other DRC findings, open connections, style defects,
lost branch proofs or degraded sampled high-speed reference copper. The saved
result is independently rechecked; an exhausted search remains unresolved.
The width-repair stage shares the main controller's geometric defect identities.
Replacing a track identifier or changing width does not turn the same old
staircase into a new defect. Existing defects still prevent routing completion;
new staircase geometry remains forbidden.

The separate `pcb repair-drc --keepouts` operation searches short high-speed
launch detours at fixed pads and vias with constant width and layer, a bounded
length change, and the saved pair gap. Physical rule areas, including those
inside footprints, forbid copper at their boundary; copper-to-copper and drill
clearances still apply to the actual features. The launch test uses native
filled ground polygons. If $T$ is the proposed swept trace, $P$ is one adjacent
ground polygon, and $V$ is an existing void inside the unchanged endpoint via's
saved clearance envelope, the local condition is $T\setminus V\subseteq P$.
The actual refilled board must pass that test and the independent native DRC,
contact, protected item and reference regression checks. This check does not
establish pair timing, impedance or manufacturing readiness.
Local reference reservations represent GND return copper. GND routing may
reinforce those reservations; foreign nets must avoid them. Physical PCB
keepouts and native copper/hole clearances remain mandatory for every net,
and the independent filled-reference comparison is still required.

The separate `pcb clean-silkscreen` stage uses the maintained presentation
configuration to align connector captions and bind displayed TP/fuse labels to
their actual footprints. Missing-footprint labels are removed without changing
schematic or PCB references. Board-edge artwork is clipped symmetrically where
configured, with matching silk-only local library updates. All nonsilkscreen
content is compared independently before publication. Display fields use KiCad's
native property order and explicit text strokes so native saves preserve them.

The `pcb repair-drc` stage corrects reported track-to-via contacts using exact
integer intersections of existing horizontal, vertical and 45-degree support
lines. Fixed opposite endpoints and every component pose remain unchanged.
An unused single-layer via may be removed only if native connectivity, full DRC,
branch proofs, protected content and sampled reference checks do not regress.
Single-layer vias with no pad, zone or interlayer connection do not split a route
chain for style checking; otherwise they could hide an existing staircase. Native
checks still distinguish these geometry repairs from electrical qualification.
Courtyard overlaps that require changing fixed component positions remain blockers.

KiCad 10.0.6 caps most error types at 199 reports. Width inventory runs further
native passes on disposable geometry-identical copies, placing already-reported
items in a temporary inventory group solely to check the remaining widths.
The original native rules supply the measured minima. Groups and inventory-only
overrides never reach the saved PCB or rule file. Merged width findings are used
by repair, saved-board verification and progress recording. Earlier capped
progress rows remain historical measurements, not revised estimates.

Runtime `--search-cells` and `--search-expansions` override only numerical search
caps, including per-net overrides and recovered requests. They are recorded in
the same checkpoint, participate in request identity and preserve the independent
aggregate memory ceiling. Increasing the cell budget can prevent automatic grid
coarsening from removing narrow legal passages; it changes no physical rule.

Search and materialization also share supported native
net classes, precedence and courtyard-local package escapes. A neck widens outside
its package; absolute Board Setup minima and verified electrical floors remain
independent of local allowances. New copper does not
inherit legacy group or exact-item exceptions. Unsupported expressions are reported.
The active routing configuration names all six enabled copper layers. Native
repair requests derive their available layers from the enabled saved stack and
reject explicitly requested disabled layers. Through vias have the same transition cost for any
layer pair. Equal preference does not erase the saved copper thicknesses, local
reference-copper reservations, or electrical and clearance requirements. Six-layer
native access and physical reference-layer adjacency have regression coverage.
Its default search also rejects staircases,
uses gentle turns, and applies the same diagonal-length cost as the board-wide
configuration. Explicit alternative settings produce geometric previews that
still require the independent native and style checks.
When zone refill is enabled, the planner recomputes zones on each temporary
board after the proposed placement and removals, then uses native copper
connectivity. These fills are not saved to the user's board. Every candidate
still requires an independent native refill and checks for broken existing
connections. Native validation compares all track errors and reported severities
against the baseline. When a schematic is present it also runs parity checks,
rejects newly introduced parity findings, and treats a failed or incomplete
parity check as an error rather than a clean result. Schematic inputs are part
of the validation snapshot. The planner can sample a nearby via position on a filled polygon
that already belongs to an existing copper island. A short direct connection
is checked against copper and via clearances before native validation.
Sampled sites with clear via access are searched before sites whose via is
obstructed. Obstacles and configured keepouts on every copper layer are
considered for a through via; harder sites remain search alternatives.
The search also prioritizes endpoint sites inside its current routing window
over sites outside the grid's usable envelope.
When temporarily removing copper for a repair, track ends and via positions
within a filled polygon are bound to that polygon's existing copper island.
The resulting restoration is still checked against a newly refilled board.
Sampling is finite and does not cover every possible plane attachment.
Complete reference-path verification remains unfinished.
The grid rejects impossible vertices, then checks each actual swept edge and
off-grid connection at its applicable dimensions. Topology bounds use exact
integer grid limits for their edge margins instead of allocating a point for
every cell; these limits do not alter the routing clearance.
The stackup reader validates the whole S-expression's syntax while parsing only
the top-level setup needed for copper thickness. Native DRC independently
checks the full candidate. See [KiCad 10 custom rules](https://docs.kicad.org/10.0/en/pcbnew/pcbnew.html#custom_design_rules).
Round pads with centered, compatible shapes use their circular envelope.
Centered ovals and rounded rectangles aligned to the board axes use a
conservative rounded envelope; unsupported shapes and rotations retain
bounding boxes. Clearance around a rectangular bound uses Euclidean,
rounded corners rather than a square expansion.
Trace capsules use a bounded scanline and the same exact distance test, so
long diagonals do not require scanning their entire bounding rectangle. Native
straight traces are divided into at most 2 mm capsules with a conservative
rounding allowance.
Pads with drills and existing vias include the saved hole and hole-to-hole
clearances. Non-plated holes also use the board's copper-edge clearance.
For a centered circular drill in a supported plated pad, the smallest guaranteed
copper annulus across represented layers is subtracted when converting the
drill-edge margin to a copper-edge margin. This covers circular, rectangular,
oval and rounded-rectangle pads. Offset, slotted and chamfered/custom pads keep
the conservative fallback; no saved hole geometry or native rule is changed.
These are conservative search envelopes and are still checked by native DRC.
Before grid search, first-feasible mode tests bounded, exact straight and
horizontal–45-degree–vertical candidates on one layer. The candidates retain
swept-copper clearance, endpoint-heading, segment-length and existing-tree
checks. Success is a truncated warm solution, not a global optimum; failure
falls back to the ordinary search.
Off-grid endpoints first try their surrounding grid vertices. If those accesses
do not produce a route, the search tries farther clear escapes across the
routing window, subject to the shared time budget. It enumerates disjoint grid
perimeters rather than repeatedly sorting the enclosed cells. Each expanded
ring tests at most 2,048 access pairs; reaching that cap leaves the result unknown.
For a ring with several access pairs, the router uses its ordinary geometric
lower bound instead of rebuilding an optional reverse-grid guide for each pair.
Single-pair and on-grid searches keep the configured guide. This affects which
candidate a bounded search finds, not its clearance, angle or electrical rules.
Before enumerating an off-grid access, the planner computes a lower bound on the
first octilinear stub leg. If all eight possible first legs at that length are
blocked even under minimum permitted width and clearance, that stub search stops early.
On-grid zero-length access remains available. This concerns the modeled stub
search, not proof that the physical board is unroutable.
Width and clearance checks consider courtyard intersections over the entire
emitted physical track, including portions outside the courtyard. KiCad's
general courtyard predicate checks either side of the board for tracks on all
enabled copper layers; the planner follows that behavior. It resolves supported
footprint-reference and library-name wildcards and stops with a clear error for
unsupported component-class selectors. Only courtyards referenced by those
rules enter the spatial index. The planner also considers stricter rules when a
courtyard's shape, a boundary intersection, or an existing obstacle's courtyard
membership is uncertain. A local narrowing is permitted only when the
applicable rule is established. Native DRC still evaluates the completed
candidate.
Within a multi-net batch, earlier proposed tracks are represented by short
centerline capsules that conservatively cover their actual widths. This avoids
blocking empty corners of a diagonal trace's bounding box; clearance and final
native checks remain in force.
Endpoint pairs receive bounded initial searches before a difficult pair can
consume the remaining expansion budget; limited pairs can be retried.
Each escape still passes the same swept-copper, clearance and angle
checks. A valid direct route that reaches the relaxed cost lower bound returns
without unnecessary grid search. The finite fallback can miss other legal
escapes and never proves a physical route impossible.
Endpoint heading constraints apply at the actual pad or copper anchor; each
grid-entry stub is joined to the grid path with permitted turns, and the
assembled route is checked again for angle, spacing and staircase rules.
For consecutive obstacle checks on the same track and width, a saved-rule
profile reuses its last courtyard-intersection calculation. The cache is
bounded to one track; obstacle-specific clearances and uncertain courtyards
remain independently evaluated.
If a path creates a staircase or invalid bend where it meets the existing
copper tree, the search can retry different endpoint headings within its
original limits. After three distinct context rejections, it also tries up to
three alternative searches that temporarily avoid an interior point on a
rejected path. Staircase errors identify added copper in the conflicting chain;
points on the candidate's conflicting copper are tried first. Each alternative uses the same clearance, angle, layer and
context checks; the temporary restriction is removed afterward. If the
detours fail, sampled alternative endpoint headings are tried before the
unrestricted search resumes within its budget. This
bounded diversification finds feasible candidates without proving optimality.

**Recovery.** Local failures escalate through wider pairs, finer pairs and a
board-wide detour. A relaxed-path diagnostic then identifies exact copper
intersecting sampled corridors and ranks removal sets by path and rework cost;
the diagnostic path is never accepted as copper. If it finds no witness, a
bounded local pad-to-via search can identify eligible copper in the escape
corridor, while retaining protected copper and keepouts. Endpoint clusters provide
additional hypotheses. Normal search must reroute the target and damaged
neighbors together; eligible existing target copper can also be removed and
repaired in the same transaction. Defaults allow 12 removed items, 3 repair nets and 12 repair
islands. Unqualified power, ground, protection and critical-pair copper is not
removed by the board-wide runner. The diagnostic's adjacent reference-copper
reservations use the USB/Ethernet net set, independently of the larger set of
protected copper that must remain in place.
Automatic order selection tries damaged neighbor nets before the new target,
leaving the target to route around their retained repair candidates. Other
orders remain available; explicitly requested orders keep their precedence.
After earlier nets are placed, the planner first searches a small window around
the next net's selected islands, then retains the wider search as a fallback.
Both remain bounded by the transaction budget. Within the scheduled bounds,
the checkpoint window expands as needed to leave room for the configured copper,
clearance and one effective grid step at the endpoints; physical design rules are
unchanged.
When every item on an unfinished target net is eligible and fits the configured
whole-net rip-up limit, recovery also considers replacing its complete existing
copper. The default limit is 32 items, independently configurable from the local
obstruction limit; zero disables it and 64 is the maximum. The search inventories
the entire target before applying its local-distance filter.
The removal is costed, and native connectivity, protected-copper, geometry, DRC
and reference comparisons still apply; this is a candidate, not a guarantee.
The external coordinated-repair command can optionally diagnose the actual
obstructing copper with bounded relaxed-path searches before selecting a removal
candidate. Those paths are never installed. The diagnostic excludes same-net
tracks from foreign-trace obstructions while still considering existing vias
when proposing a new hole. The replacement must restore all original required
connections and pass the same independent checks.
Local repair searches also model other proposed tracks and vias, and test new
paths against nearby same-net bends and staircases. Consecutive collinear
proposal tracks may be merged without changing their copper, provided the join
is not a pad, via, branch or contact with saved copper. The merged replacement
is checked before and after the full native validation.
An optional second router can work on an intermediate repair before it is
retained. Both router stages are recorded; the final combined result must
improve connectivity and pass the same protected-item and native checks.
Failed-plan history consumes earlier alternatives without applying the batch
ordinal a second time. Target order still rotates, and untried neighborhoods
remain available. The optional `--optimize` setting requests the backend's
post-route optimizer within its existing time limit before ordinary filtering.
Fixed-placement local repair can use `--ground-first` as well as whole-tree
renewal. Permitted ground additions are attempted after each intermediate
reroute and followed by an independent native check; no existing ground copper
or protected component is moved, and this does not establish current ratings.

If selected copper lies in an existing staircase, the recovery planner can also
offer adjoining eligible tracks as one bounded replacement candidate. It does
not follow through a pad, via, branch, locked item or excluded copper. For an
unambiguous uniform-width removed chain, local cleanup may instead straighten a
staircase before reuse. If direct shortcuts are blocked, a bounded local grid
search can try other endpoint headings and layers around that chain. A straight
continuation at an existing endpoint remains eligible, while right-angle bends
and retracing the existing segment are rejected. Changed geometry is checked
against surrounding copper,
connectivity, current style rules and native DRC; a blocked shortcut is rejected.
When the source board and placement are unchanged, exact same-width pieces of
the old path are distinguished from new pieces during the preliminary
clearance screen. Every piece is still checked against other proposed routes,
and the complete transaction must pass native DRC and connectivity checks.
An exact restoration may also retain an existing off-angle contact in a
temporary candidate; it does not make that contact compliant or permit a new
off-angle junction. A uniquely matching segment with identical saved properties
retains its existing DRC and style finding identity even if its UUID changes.
For a dangling-track warning, KiCad's connectivity model checks both ends;
their net, layer, width, and outgoing direction identify any still-dangling
end after a collinear extension. The warning and endpoint counts cannot increase,
and a new dangling endpoint is rejected even when KiCad displays only the
other end. Other changed or ambiguous copper is compared as new, and all
existing findings still block the final completion check.
Repair contacts include actual same-net pad, track and via copper, including
contacts away from a pad center or from the initial sampled sites.
When a new junction splits an existing track without moving components, a
native-confirmed dangling tail can be removed if no same-net pad, trace, via,
zone or proposed copper may depend on it. Full connectivity and native DRC
still check the candidate. Its removed length and track are charged as
rework before the sampled candidates are ranked in both the board-wide and
standalone checkpoint workflows.
For a reported signal dead end leading into an unambiguous interior T-junction,
the cleanup can shorten the trunk and adjacent branch and insert one 45-degree
corner. The independent acceptance check proves the old and new local geometry,
requires shorter copper, fewer warnings, no additional vias or opens, and still
checks connectivity, protected items, routing style, native DRC and sampled
reference copper. Ambiguous contacts, protected nets and locked or grouped
items are deferred.
For an existing staircase, the standalone cleanup can also replace a simple
eligible signal chain with a shorter octilinear chain between the same exact
endpoints. Independent nets are checked together and a failing batch is split.
An independent proof checks the original and replacement chains, dimensions,
non-track items and the reduction in staircase findings before native DRC,
connectivity, protected-item and sampled reference-copper checks can retain it.
An exact same-width collinear extension is merged with eligible existing copper
when the join has no pad, via or branch. Locked, grouped, nonstandard or
width-varying source copper is not silently changed; native validation remains
the authority for connectivity and clearance.
A legacy track within 0.05 mm of an axis or 45-degree direction may be offered as an
exact-angle replacement by moving one endpoint; all original required
connections, clearances and native checks must still pass. This bounded
projection is a candidate, not a change to fixed footprints or a proof that
other off-angle copper can be repaired.
When no components move and cleanup cannot repair an inherited staircase,
exactly unchanged removed copper may be a temporary restoration seed. Every
native validation independently rejects new style defects. An inherited
staircase still blocks final completion; restoration does not fix it.
Removing redundant copper does not force an unrelated open island to be routed:
if no surviving connection was split, the neighbor receives a self-anchor and
the closing check still verifies every pre-existing connection. A private
intermediate proposal may temporarily disconnect copper on the intermediate
board; it carries a separate unverified status and cannot be retained until
independent checks against the original board pass.
When no component moved, the exact removed traces and matching through-vias
can also seed the repair at their original widths and dimensions. They must
connect all selected islands, avoid new proposed copper and pass native
connectivity and DRC; the seed does not qualify current or signal integrity.
When other nets still need routing, alternatives to a valid seed use at most
one quarter of that net's remaining slice; if the seed conflicts with an
earlier proposal, the normal full-slice search remains available.
After the first attempt, repair alternatives are interleaved with distinct
other pending target nets using `recovery.defer_rounds` (two by default).
Repeated attempts on the same target do not count as different work.
The next queued round favors targets with fewer attempts on the current
preview; section priority breaks ties. Attempt counts persist across validated
improvements, resumes and code renewals; a new sweep resets them. A native
rejection can be rebuilt from the current board and policy after a code renewal,
without replaying old geometry or removals.
When two workers are available, ordinary retries and repair alternatives run
on separate temporary boards. If two ordinary searches both succeed and their
windows do not overlap, the coordinator tests their combined copper in one
native validation and can retain both improvements. If that combined candidate
fails or does not improve the preview, the coordinator validates the searches
individually. Competing repair alternatives are never merged, and an unselected
net is retried against the updated preview.
An equal-open, native-checked candidate can also be explored on a disposable
branch. The planner rebuilds work from that branch and tries up to two fresh
single-net follow-ups on the target or affected nets. It retains the branch
only if a follow-up reduces opens and the resulting board passes independent
native DRC, schematic parity, original-connection, protected-item, routing-style
and cumulative sampled-reference checks against the pre-branch preview.
Interruption, stale inputs, a failed check or no improvement leaves the prior
checkpoint intact. This bounded two-step search does not establish a symmetry
or cost improvement and does not qualify electrical behavior.
If a candidate changes the
preview, pending repair UUIDs and poses are discarded and one marker per target
is recompiled from the new board; stale physical edits are never replayed.

The same stage samples permitted bottom-SMD translations and right-angle
rotations, including nearby package pairs. It interleaves 0.5, 1 and 2 mm moves.
Component group membership alone does not prevent an authorized bottom move;
other members and the group record remain unchanged. All incident nets
participate; every surviving fragment of a connection broken
by a move is a mandatory repair. Moved pads retain their original identities.
The placement search permits up to 12 affected nets by default, independently
of the smaller limit on obstruction nets removed during copper rerouting. The
placement net limit is configurable up to 32; exceeding it skips that candidate
rather than omitting any incident net. Placement repair has its own default
limit of 24 copper items, configurable up to 64, separate from ordinary rip-up.
When removing an obstruction would expose a track end, the repair follows
eligible leaf traces to an anchor or surviving branch and includes them in the
same bounded, costed transaction. Locked or over-limit repairs are skipped.
The original placement remains the retained baseline. Movement costs 1 per
millimetre plus 1 per quarter-turn. Candidate poses are checked using native
courtyard polygons and moved-pad geometry. New courtyard overlaps are skipped;
foreign removable tracks and vias that a moved pad would hit are added to the
same rerouting transaction. Protected, locked or unsupported obstructions
reject the pose. Existing clearances and a component's internal pad geometry
are not treated as new collisions. The preliminary copper test is conservative
and does not replace rule-aware native DRC; it may exclude a pose that a local
exception would permit. Native checks still reject dangling tails,
clearance/courtyard violations and protected changes. This is bounded pose
sampling, not general board-wide placement or electrical qualification.

**Refinement.** A complete sampled tree is split into chains at pads, branches,
vias and existing contacts. The cleanup tests cheaper octilinear shortcuts while
holding these anchors, layers and vias fixed. It keeps a valid original candidate
when cleanup fails; finite beams may still discard alternatives. Staircases are
identified by local bend positions and directions, independent of UUIDs and
collinear splits. Branch junctions are distinguished from degree-two bends.
For an eligible unbranched proposed chain, the external adapter progressively
refines possible chamfer positions up to 128 divisions within its cleanup
budget. Each candidate still requires clearance screening and native validation;
an exhausted sampling budget does not prove that no route exists.

The optional `first_feasible` search stops once a valid warm-start route is
found instead of comparing every endpoint alternative. Truncated alternatives
are reported and the result is not an optimality proof. Both on-grid and
off-grid candidates are checked against the existing net tree during search,
including junction and staircase rules. If the compressed search state cannot
establish whether a different contextual path exists, the outcome is unknown,
not a proof of infeasibility. Smoothing retains the original path when its
proposed replacement fails the same contextual checks.
The current warm-start configuration uses a search weight of two to prioritize
speed; this can return a more expensive route and does not change the hard
constraints or replace candidate validation.
When many off-grid endpoint pairs are available, first-feasible mode allocates
deeper initial search to the nearest pairs instead of dividing the pilot budget
among every pair. The total expansion and wall-clock limits still apply. The
non-first-feasible mode retains the original broader sharing policy.

The configured base width is 0.20 mm; the saved net-class and local custom
rules, together with any explicit electrical width floor, determine the actual
width on each emitted track segment. For this saved board, ordinary core
signals use 0.20 mm and field signals use 0.25 mm outside saved local
exceptions. A design-rule width by itself does not establish current capacity
or qualify a power route.

Let $g(v)$ be the incurred search cost, $h_{\mathrm{relax}}(v)$ the optimistic
remaining grid cost after relaxing heading/bend/staircase rules, and $\alpha$
the search weight. The optional topology guide uses:

$$
f(v)=g(v)+\alpha h_{\mathrm{relax}}(v),\qquad \alpha\ge1.
$$

At $\alpha=1$ this is a lower bound within the modeled grid. The reverse-grid
calculation stops when it reaches the current start or hits its bounded work
limit. It uses exact settled costs and zero for unexplored cells, so an
unfinished calculation cannot falsely mark a path unreachable. Its relaxed
diagonals may pass between blocked grid vertices; the detailed search still
checks the whole swept edge. Larger weights favor candidate discovery and do
not prove an optimal route.

**Runtime and evidence.** Native work uses disposable headless processes, with
up to two search workers; it opens no editor. Spatial indexes and bounded caches
reduce repeated work. Multi-net searches share their remaining time among nets
still to be searched, so later nets can use time that earlier nets did not need.
An independent search is cached only if it did not hit a limit; a timed-out
empty search remains eligible for a later attempt.
An already-connected repair net is checked before nets needing new copper;
its empty route does not build a refinement grid. This avoids reserving an
equal search slice for a no-op. If refinement reaches its deadline, the
already-found candidate is retained for native validation.
The automatic
aggregate memory ceiling is the lesser of 6 GiB and half available RAM, with a
2 GiB launch guard and Windows commit-limit checks. An explicit
`--memory-gib 3` sets a 3 GiB ceiling. Memory failure retains the checkpoint as
`resource_blocked`; invalid or missing native reports never pass.
Obstacle-mask construction also checks the search deadline, including during
large mask fills. Native validation and exports take additional time.

Accepted batches, periodic runtime updates and final exit update the progress
PNG. Saved-history CSV measurements remain separate from private-preview JSON.
An export failure stops the run and retains its last committed checkpoint.
If a process exits without saving its final status, the status command detects
the released routing lock and reports the retained checkpoint as interrupted.
It recommends renewal after code changes or a fresh run after input changes.
Stale preview inputs hide the overlay. `review progress --preview-run RUN_ID`
revalidates an older retained preview before displaying it. Schematic parity is
checked but not plotted. Board PDFs/PNGs still show the saved PCB.

**Not complete.** The board-wide runner cannot publish. Coupled pair routing,
qualified branch-current limits, continuous reference verification, global
placement/negotiation and symmetry-driven board optimization remain unfinished.
The status is a private geometric preview, not electrical approval. Section 9
defines the remaining implementation and acceptance gates.

The standalone branch-current calculator requires capacity and resistance to
be marked as verified; a design target can specify a scenario or its allowable
voltage drop but cannot establish a physical rating. A matching source record
does not independently prove that the marked value or underlying model is
correct. Board-wide evidence compilation and electrical qualification remain
unfinished.




## 8. What the other tools optimize

### RePlAce: placement

In [RePlAce's paper](https://vlsicad.ucsd.edu/Publications/Journals/j126.pdf), $v$ is the vector of cell positions, $W(v)$ is a smoothed wirelength estimate, and $D_{\mathrm{global}}(v)$ and $D_{\mathrm{local}}(v)$ penalize crowding. The multiplier $\lambda$ balances wirelength against global density. Its placement objective is:

$$
\min_v\; W(v)+\lambda D_{\mathrm{global}}(v)+D_{\mathrm{local}}(v).
$$

The density model uses an electrostatic analogy, and Nesterov's method moves the cells. RePlAce also uses routing-congestion feedback to improve routability. It was designed for chip placement, so its density model and benchmarks do not directly capture PCB connector, mounting, high-current, and reference-plane constraints.

### Freerouting: routing and improvement

Freerouting's [published optimizer score](https://github.com/freerouting/freerouting/blob/master/docs/scoring.md) starts at 1000 and subtracts penalties for excess trace length, vias, and bends:

$$
S_{\mathrm{opt}}=\max\!\left(0,1000-\Delta L-\Delta V-\Delta B\right).
$$

Here the three $\Delta$ terms are weighted, normalized penalties relative to geometric reference values, not the raw length or counts. Its optimizer rejects candidates that increase opens or clearance violations before comparing the score. The maze router itself uses separate costs for directions, vias, and rip-up; this board score is not its path-search equation. A Manhattan reference is not a universal lower bound for a PCB that permits 45-degree routes and shared branches.

### What better means for AutoPCB

First satisfy the electrical, geometric, and protected-placement constraints. Then compare competing methods on the same saved inputs and resource budget using open connections, rule violations, track length, vias, bends, staircase windows, symmetry, runtime, and memory. Include high-speed return paths and actual current limits in the review. These comparisons and the more ambitious optimizer above still need implementation and benchmarking; neither the equations nor a clean DRC establish electrical safety or manufacturing readiness.

## 9. Completion plan and acceptance gates

This is the implementation plan, not a claim that the entire handoff is complete.
Keep `run.py` as the entry point and extend the existing modules. Run the phases
in dependency order; changes to search policy never weaken the independent gates.

| Priority and owner | Next implementation | Evidence needed to close the phase |
| --- | --- | --- |
| 1. Rules and native model: `core`, `rules`, `native`, `inventory` | One typed model of pads, islands, filled zones, exact holes/keepouts and courtyards; one unit/rule resolver for search and output. Cache by source and invalidate changed regions. | Round-trip fixtures for multilayer pads, net ties, zones and unsupported rules; no net silently omitted; generated copper agrees with native dimensions. |
| 2. Recovery: `obstructions`, `reroute`, `recovery`, `runner` | Extend current local witnesses into a conflict graph, congestion history and coordinated section repair. Permit temporary damage only inside disposable transactions; repair every affected net before acceptance. | Cases where alternate order and multiple removals are necessary; no repeated identical failed request; interruption retains the best validated preview. |
| 3. Placement: `relocation`, `workflow` | Replace the bounded pose sample with coarse-to-fine component-cluster search and courtyard legalization. Keep bypasses/protection close to their devices; reconsider every incident net. | Fixtures requiring several bottom moves; no top geometry/front silk changes; whole-transaction connectivity restored; movement and rework costs recorded. |
| 4. Signal and power constraints: `electrical`, `reference`, `backend` | Joint pair search with exact escapes, spacing/skew and reference corridors; branch-aware load scenarios, shared returns, via limits, voltage drop, ESD and decoupling constraints. | Evidence-backed limits and counterexamples for each gate. Unknown critical ratings block affected routes; independent nets continue. Reference checks use continuous copper geometry, not sample counts alone. |
| 5. Global planning and quality: `controller`, `scheduler`, `topology`, `symmetry` | Reserve power/pair corridors, then batch ordinary net trees. Revisit sections when they conflict. Add terminal, whole-net and matched-channel refinement using the lexicographic objective. | Same electrical connectivity and protected items; measured symmetry in equivalent local frames; fewer unnecessary vias/bends without harmful detours. |
| 6. Runtime: `resources`, `batch`, caches | Profile geometry construction, search, native checks and exports separately. Compare persistent workers with current isolated native processes; reuse safe immutable models, not live SWIG objects across processes. | Identical inputs within time/memory ceilings; peak aggregate commit measured; bounded cancellation; no GUI popups; activity at least every 60 seconds. |
| 7. Verification and publication: `verification`, `route_validation`, `review` | Independent closing checks, source/editor guards and atomic publication of the board and required current views. Export failures retain the last valid set. | Zero unintended opens, shorts, unresolved errors/warnings and exclusions added; full coverage/parity, electrical/reference gates and protection pass; matching PDF/PNG/gallery/progress. Only then enable publication. |
| 8. Maintainability: `core`, CLI, config, docs | Versioned typed schemas, single ownership of geometry/rules, meaningful errors, dependency isolation, lint/type checks and reproducible commands. Migrate historical schemas without replaying campaigns. | Compatibility fixtures, clean imports, tested failure paths and accurate README/PDF; no redundant framework or hand-edited per-net routing. |

### Why this architecture

The recommended approach is a greedy warm solution followed by negotiated,
transactional repair and local optimization. [Freerouting's architecture](https://github.com/freerouting/freerouting/blob/master/docs/architecture.md)
separates its headless model, routing, optimization and validation pipeline.
[PathFinder](https://janders.eecg.utoronto.ca/1387_2015/readings/pathfinder.pdf)
adds present and historical congestion costs so nets negotiate scarce resources.
For AutoPCB, adapt that idea to candidate corridors; never publish overlapping
copper. PathFinder's FPGA results do not establish PCB performance.

[OpenROAD global routing](https://openroad.readthedocs.io/en/latest/main/src/grt/README.html)
uses route guides and incremental work; its
[placement module](https://openroad.readthedocs.io/en/latest/main/src/gpl/README.html)
uses congestion feedback. My recommendation is to use coarse guides to choose
where detailed PCB search spends effort, with actual pad access, 45-degree
geometry and PCB constraints enforced afterward. These chip tools are design
references, not drop-in PCB engines.

For small difficult repair windows, evaluate an optional integer candidate-selection
model using [CP-SAT](https://developers.google.com/optimization/cp/cp_solver).
Select compatible paths and poses, retain lower bounds, and report its exact
status. An optimal result applies only to that finite model; a time-limited
unknown result proves neither impossibility nor optimality.
Electrical placement needs separate physical models: for example,
[TI's ESD layout guide](https://www.ti.com/lit/an/slva680a/slva680a.pdf)
explains why the protection path's impedance matters. Short route length alone
cannot qualify ESD performance.

### Benchmark and stop rules

Use the same source board, rules, fixed-item baseline, seeds and aggregate memory
budget for greedy-only, greedy-plus-refinement and optional Freerouting comparisons.
Measure at 30, 120 and 600 seconds: opens, native errors/warnings, length, vias,
collinear-merged bends, symmetry, wall time and peak memory. Include small known
solutions, congested repair fixtures and Jessie; report unsupported features and
all failures. Never compare a filtered subset against another tool's full board.

After three unproductive sweeps, escalate to an untried repair level. When the
bounded repair space is exhausted, retain the best candidate as stalled with
specific blockers. Completion requires every closing gate, including fresh
deliverables. This plan seeks measurable reliability and routing quality;
neither a heuristic nor a passing test suite guarantees a perfect board.
