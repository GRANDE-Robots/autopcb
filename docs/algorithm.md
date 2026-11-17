# AutoPCB algorithm

<style>body{font-size:10pt;line-height:1.27}table{font-size:8.8pt}</style>

## 1. Board and placement model

The workflow is **requirements → native KiCad design → private layout preview → independent verification → export**. An existing KiCad design enters at layout or verification. Rules, hierarchy, libraries, and design evidence travel with each private job.

Let $B_t$ be the copper and footprint state after $t$ accepted checkpoints. A checkpoint $q_t$ selects pins and optional existing copper on the same net. $P(q_t)$ is its pin set, and $k$ is its pin limit. A route $r_t$ adds, replaces, or removes permitted copper. Let $A$ denote acceptance of a validated candidate.

$$
|P(q_t)|\le k,\qquad B_{t+1}=A(B_t,q_t,r_t),\qquad B_0=B_{\mathrm{saved}}.
$$

A large net can span several checkpoints. Each accepted batch preserves previous required connections. Related nets may have to be repaired together when they compete for the same space.

### Board objective

Let $U$ count unintended opens and $S$ measure symmetry error across equivalent channels. Let $L$ be track length, $L_{45}$ diagonal length, $V$ vias, and $J$ bends after collinear merging. Let $D$ be a layer penalty, $M$ placement movement, $R$ copper rework, and $C_{\mathrm{silk}}$ the silkscreen cost. Weights specify the cost of each layout feature.

$$
\operatorname{lex\,min}\ (U,S,C),\qquad
C=w_L L+w_{45}L_{45}+w_V V+w_J J+w_D D+w_M M+R+w_I C_{\mathrm{silk}}.
$$

Compare $U$ first, then $S$, then $C$. The legal candidate set is defined separately by the hard constraints. A smaller score cannot override a clearance, electrical, or protection failure. Symmetry compares role-equivalent routes in translated, rotated, or mirrored local frames. Measure separation, via positions, bends, and topology with an explicit tolerance. Positional comparisons default to 0.05 mm. Record differences forced by fixed endpoints. The current preview engine does bounded local optimization. A full board-wide symmetry solve remains outside its coverage.

### Placement estimate

For component $i$, $x_i=(p_i,\theta_i,\ell_i)$ gives its position, rotation, and side. Protected items keep their saved pose. For net $n$, the half-perimeter wirelength $\operatorname{HPWL}_n(x)$ is the width plus height of its pin bounding box.

$$
\widehat C_{\mathrm{place}}(x)=
\sum_n w_n\operatorname{HPWL}_n(x)
+w_C C_{\mathrm{congestion}}+w_P C_{\mathrm{proximity}}+w_M M.
$$

Congestion estimates available routing space. The proximity term covers requirements such as bypass placement. This estimate ranks placements before routing. It cannot certify clearance or routed length, especially with 45° tracks. The current engine tries bounded moves for eligible components during recovery and routes affected nets on the trial board.

<div style="display:flex;gap:8px;align-items:center;margin:22px 0 0;break-inside:avoid;font:9.5pt Arial,sans-serif;color:#173f59">
<div style="flex:1;border:1px solid #b8cbd6;background:#eef5f6;padding:11px;text-align:center"><strong>Saved board</strong><br>Copper islands</div><span>→</span>
<div style="flex:1;border:1px solid #b8cbd6;background:#eef5f6;padding:11px;text-align:center"><strong>Private candidate</strong><br>Route or repair</div><span>→</span>
<div style="flex:1;border:1px solid #b8cbd6;background:#eef5f6;padding:11px;text-align:center"><strong>Native checks</strong><br>Refill and validate</div><span>→</span>
<div style="flex:1;border:1px solid #7ba8a1;background:#e6f2ef;padding:11px;text-align:center"><strong>Accepted board</strong><br>Next checkpoint</div>
</div>

<!-- pagebreak -->

# 2. Routing and rework

### Checkpoints and path search

Let $s=(q_0,\ldots,q_{H-1})$ be a candidate order over $H$ checkpoints. The estimated layout cost and search time are $\widehat C(s)$ and $\widehat T(s)$, with time weight $\lambda_T$. The outer search favors a small $\widehat C(s)+\lambda_T\widehat T(s)$. The preview engine schedules from policy and copper islands, with alternative orders and bounded backtracking.

For a checkpoint, let $\Delta C(B_t,r)$ be the immediate cost change and $\widehat V(B)$ an estimate of the effort to route the rest of board $B$. A look-ahead criterion is

$$
r_t^*\approx\arg\min_{r\in\mathcal R(B_t,q_t)}
\big[\Delta C(B_t,r)+\widehat V(A(B_t,q_t,r))\big].
$$

$\mathcal R(B_t,q_t)$ contains the legal routes for that checkpoint. A grid search state includes position, copper layer, and arrival heading. A* ranks states using $g+h$, where $g$ is the incurred cost and $h$ is a remaining-distance estimate. Width, clearance, via spans, and available pad exits constrain the moves. Budgets bound grid expansion and runtime. Failed attempts can widen the search window or revisit neighboring copper.

Let $L,L_{45}$ be in millimeters. The default local cost is $L+L_{45}+10V+J$, plus configured layer and guide costs. Let $d_e$ be the length of track $e$ no longer reproduced at the same position, layer, and width. Let $h_v$ be 1 when via $v$ changes position, span, drill, or copper diameter, and 0 otherwise. These terms give the default rework penalty.

$$
R=\sum_e\mathbf 1[d_e>0]+\sum_e d_e+10\sum_v h_v.
$$

Collinear splits or changed UUIDs on equivalent copper leave $R$ at zero.

### Geometry and connectivity

| Constraint | Test |
| --- | --- |
| Net connectivity | Join every selected terminal and same-net anchor. Preserve previously connected terminal pairs. |
| Isolation | Different nets' copper stays separated, except in modeled net-tie regions. |
| Directions | Let $\theta$ be track heading. Require $\theta=m\pi/4$ for integer $m$. After merging collinear runs, every actual bend changes direction by an odd multiple of 45°. Branches and pad contacts have separate topology. |
| Physical limits | Check copper and drill separation, keepouts, width, via size and span, and geometry preserved by the profile. |

A staircase is counted on an unbranched same-layer chain after collinear merging. Let $d_i$ be heading $i$ in eighth-turns. A window of diagonal–cardinal–same diagonal counts once. Its dogleg exception $E_i=1$ applies when the complete five-run pattern is $(a,d,c,d,a)$ with perpendicular cardinal headings $a,c$ and a chamfer $d$ one eighth-turn from each.

$$
N_{\mathrm{stair}}=\sum_i
\mathbf 1[d_i\text{ diagonal},\ d_{i+1}\text{ cardinal},\ d_{i+2}=d_i]\,(1-E_i).
$$

An exhausted search leaves the affected connections unresolved. Its best validated candidate remains available for inspection.

<!-- pagebreak -->

# 3. Engineering checks and publication

### Electrical limits

For a radial load scenario, $I_j$ is load $j$'s current and $I_e$ is branch $e$'s aggregate downstream current. Let $\rho$ be copper resistivity and $L_e,w_e,t_e$ the length, width, and thickness of that branch. A uniform-conductor DC estimate for the drop along path $p$ is

$$
I_e=\sum_{j\ \mathrm{downstream\ of}\ e} I_j,\qquad
\Delta V_p=\sum_{e\in p} I_e\,\frac{\rho L_e}{w_e t_e}.
$$

Width and via limits also depend on temperature rise, fabrication tolerances, and inrush or fault conditions. Check clearance against the voltage difference and the applicable fabrication and insulation requirements. Differential pairs require checked widths and gaps on the actual stackup and a continuous return path through transitions. The preview engine protects deferred pairs. Current budgets, impedance, and physical validation need evidence for the exact design.

### Silkscreen

For label $i$, let $a_i$ be its anchor and $K_i$ its footprint region. Their distance is $d_i=\inf_{u\in K_i}\|a_i-u\|_2$. Let $a_{\mathrm{clip},i}$, $a_{\mathrm{hidden},i}$, and $a_{\mathrm{overlap},i}$ be clipped ink, ink hidden by components, and ink overlapping other labels. Use the actual ink and mask openings in the loss terms. With weights for those four effects,

$$
C_{\mathrm{silk}}=\sum_i
\left(\alpha_i d_i+\beta_i a_{\mathrm{clip},i}+
\gamma_i a_{\mathrm{hidden},i}+\delta_i a_{\mathrm{overlap},i}\right).
$$

This model guides separate silkscreen review. It respects fixed labels and fabrication clearances. The layout preview does not solve a global glyph-area optimization.

### Acceptance gates

| Gate | Evidence |
| --- | --- |
| Saved sources | Native files, dependencies, and effective policy still match the preview. KiCad editor state permits a guarded write. |
| Protected geometry | The schematic, outline, stackup, protected footprints and silkscreen match the baseline. |
| Native verification | Refill zones. Check opens, DRC, ERC, schematic parity, and retained connections independently. |
| Checked export | Require passing native checks and source-bound electrical evidence, including reference copper when applicable. Record hardware validation and production approval separately. |

Apply rechecks the candidate and keeps a recoverable board copy. Resume against matching inputs. Renew after tool changes to revalidate a retained candidate and rebuild the queue. A change to saved inputs or policy requires a fresh run.

Keep requirements and engineering inputs in `data/`. Store policies and campaigns in `config/`. Native projects, routing checkpoints, verification reports, and manufacturing packages belong in `results/`. See the README for command syntax.
