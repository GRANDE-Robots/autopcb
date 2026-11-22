# AutoPCB algorithm

<style>
body{font-size:10.3pt;line-height:1.26}
table{font-size:9.2pt;margin:0.06in 0 0.10in}
th,td{padding:4pt 5pt}
th:first-child{width:19%}
tbody tr:last-child td{background:#e4f1ee}
.math-display{margin:0.07in auto 0.09in}
.priority{margin:0.04in 0 0.13in;padding:9px 12px;border-left:4px solid #207268;background:#e4f1ee;color:#143d3a;font:10.2pt Arial,sans-serif;line-height:1.4}
.priority strong{display:block;margin-bottom:3px;font-size:11.8pt}
.workflow{display:flex;gap:7px;align-items:center;margin:0.07in 0 0.1in;break-inside:avoid;font:9pt Arial,sans-serif;color:#173f59}
.workflow div{flex:1;border:1px solid #b8cbd6;background:#eef5f6;padding:8px 3px;text-align:center}
.workflow div:last-child{border-color:#7ba8a1;background:#e4f1ee}
</style>

<div class="priority">
<strong>Open connections → Channel symmetry → Layout cost</strong>
Board-level ranking follows feasibility. Connectivity previews use measured open counts and native checks.
</div>

## Board score

$U$ counts opens, $S$ measures channel asymmetry, and $C$ is layout cost. $L$ and $L_{45}$ are total and diagonal trace length in mm. $V$ counts vias and $J$ counts bends after collinear merging. $D$ is the layer penalty, $M$ placement movement, $R$ copper rework, and $C_{\mathrm{silk}}$ silkscreen cost. Each $w$ weights its term.

$$
\operatorname{lex\,min}(U,S,C),\qquad
C=w_L L+w_{45}L_{45}+w_V V+w_J J+D+w_M M+R+w_I C_{\mathrm{silk}}.
$$

**Symmetry.** Match equivalent circuit roles and connections. Compare geometry, vias, bends, and topology in translated, rotated, or mirrored frames. Positional tolerance defaults to 0.05 mm. Record differences forced by fixed endpoints.

## Placement

Component $i$ has pose $x_i$, with position $p_i$, rotation $\theta_i$, and side $b_i$. For net $n$, its pin bounding box has width $W_n$, height $H_n$, and weight $w_n$. A joint placement model can combine these spans with estimated congestion $C_{\mathrm{cong}}$, spacing $C_{\mathrm{prox}}$, and movement. The current catalog fitter uses the area objective below.

$$
x_i=(p_i,\theta_i,b_i),\qquad
\widehat C_{\mathrm{place}}
=\sum_n w_n(W_n+H_n)+w_C C_{\mathrm{cong}}+w_P C_{\mathrm{prox}}+w_M M.
$$

**Recovery.** Route a blocked net with the pads of movable parts relaxed. By default that is every part not locked in KiCad; the placement policy can limit it to one side or to SMD parts. Move the parts that relaxed route crosses first, with poses that clear it before the rest, then the nearest parts. Each move is tried on a copy, affected nets are rerouted, and native checks decide.

**Flexible outline.** For new catalog boards, minimize rectangular area $A=WH$ within the approved width and height limits. Fit the original poses and try sliding authorized groups in both axis orders. Keep each group's geometry, the edge margin and corridor spacing. Equal areas favor less movement. Measure the finished outline on the native edge centerlines. Routing and verification then establish connectivity. Recovery on an existing board reroutes eligible moves within its fixed outline.

## Checkpoints and search

<div class="workflow">
<div>Saved board</div><span>→</span>
<div>Copper islands</div><span>→</span>
<div>Route or repair</div><span>→</span>
<div>Refill and check</div><span>→</span>
<div>Keep checkpoint</div>
</div>

$B_{\mathrm{saved}}$ is the source board. Checkpoint $q_t$ on board $B_t$ selects pin set $P_t$ and optional same-net copper. $k$ limits pins per checkpoint. Acceptance $A$ applies a validated route $r_t$.

$$
B_0=B_{\mathrm{saved}},\qquad |P_t|\le k,\qquad B_{t+1}=A(B_t,q_t,r_t).
$$

**Order.** For order $s$, $\widehat C(s)$ estimates final cost, $\widehat T(s)$ estimates runtime, and $\lambda_T$ weights time. The model ranks $\widehat C(s)+\lambda_T\widehat T(s)$. The scheduler uses net priority, islands, compatible windows, and bounded backtracking.

**Look-ahead.** In legal route set $\mathcal R_t$, route $r$ changes cost by $\Delta C_t(r)$ and leaves estimated work $\widehat C_{\mathrm{remain}}(r)$. The model favors $r_t^\star$.

$$
r_t^\star\approx \arg\min_{r\in\mathcal R_t}
\big[\Delta C_t(r)+\widehat C_{\mathrm{remain}}(r)\big].
$$

**Grid search.** State $v$ stores position, layer, and heading. A* ranks $g(v)+\eta h(v)$ using accumulated cost $g$, estimated remaining cost $h$, and heuristic weight $\eta$ (default 1).

<!-- pagebreak -->

# Copper checks and acceptance

## Geometry and rework

**Route rules.** Use headings in 45° increments and odd multiples of 45° at bends. Check branches as junctions. Enforce clearance, width, via access, and keepouts. Reject shorts and broken connections.

**Cost.** Default local cost is $L+L_{45}+10V+J$ plus configured layer and guide costs. For trace $e$, $d_e$ is the length removed or changed in position, layer, or width. For via $v$, $m_v$ is 1 when the via is removed or changes position, span, drill, or diameter, and 0 otherwise.

$$
R=\sum_e\mathbf 1[d_e>0]+\sum_e d_e+10\sum_v m_v .
$$

**Staircases.** After collinear merging, $\phi_i$ is heading $i$ along an unbranched same-layer chain. $N_{\mathrm{stair}}$ counts repeated diagonals separated by one cardinal run. $E_i$ is 1 inside a complete dogleg such as $0^\circ,45^\circ,90^\circ,45^\circ,0^\circ$ and 0 otherwise.

$$
N_{\mathrm{stair}}=\sum_i\mathbf 1[\phi_i\text{ diagonal},\ \phi_{i+1}\text{ cardinal},\
\phi_{i+2}=\phi_i](1-E_i).
$$

## Electrical limits

In a radial load case, $I_j$ is load current, $I_e$ branch current, and $\Delta V_p$ the DC drop along supply path $p$. Branch $e$ has length $L_e$, width $w_e$, thickness $t_e$, and copper resistivity $\rho$.

$$
I_e=\sum_{j\ \mathrm{downstream\ of}\ e}I_j,\qquad
\Delta V_p=\sum_{e\in p}I_e\,\frac{\rho L_e}{w_et_e}.
$$

**Power.** Size traces and vias for the load cases. Check clearance for voltage and fabrication limits.

**Pairs.** New catalog designs can seed two parallel lanes with 45° launches. Check separation, length difference, and reference coverage along the coupled run. Verify launch gaps, impedance, and stackup through the electrical review.

## Silkscreen

Label $i$ has anchor $a_i$ and footprint region $K_i$. Its distance $d_i$ is the shortest distance to any point $u$ in $K_i$. Ink areas $A_{c,i}$, $A_{h,i}$, and $A_{o,i}$ measure clipping, hiding, and overlap. The review cost $C_{\mathrm{silk}}$ uses weights $\alpha_i,\beta_i,\gamma_i,\delta_i$ in that order.

$$
d_i=\inf_{u\in K_i}\|a_i-u\|_2,\qquad
C_{\mathrm{silk}}=\sum_i(\alpha_i d_i+\beta_i A_{c,i}+\gamma_i A_{h,i}+\delta_i A_{o,i}).
$$

## Acceptance

| Step | Required evidence |
| --- | --- |
| Preview | Refill zones. Check native rules, protected geometry, and retained connectivity. At budget exhaustion, keep the best checked preview and report opens. |
| Apply | Connectivity runs require fewer opens. Dedicated cleanup modes can prove an equal-open improvement. Match saved inputs and policy, check the editor lock and keep a recoverable board copy. |
| **Checked export** | **Zero opens and clean DRC, ERC, and schematic parity.** Bind electrical review and reference findings to the exact sources. Track hardware and production approval separately. |

Resume with matching inputs. Renew after tool changes. Start a fresh preview after a source or policy change.
