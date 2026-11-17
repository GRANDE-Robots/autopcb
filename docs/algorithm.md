# AutoPCB algorithm

<style>
body{font-size:10pt;line-height:1.23}
h3{margin:0.13in 0 0.05in}
table{font-size:8.9pt;margin:0.07in 0 0.10in}
th,td{padding:3pt 4.6pt}
th:first-child{width:22%}
.math-display{margin:0.06in auto 0.08in}
.workflow{display:flex;gap:7px;align-items:center;margin:0.02in 0 0.17in;break-inside:avoid;font:9pt Arial,sans-serif;color:#173f59}
.workflow div{flex:1;border:1px solid #b8cbd6;background:#eef5f6;padding:8px 3px;text-align:center}
.workflow div:last-child{border-color:#7ba8a1;background:#e6f2ef}
</style>

<div class="workflow">
<div><strong>Snapshot</strong><br>KiCad sources</div><span>→</span>
<div><strong>Plan</strong><br>Copper islands</div><span>→</span>
<div><strong>Preview</strong><br>Route or repair</div><span>→</span>
<div><strong>Verify</strong><br>Refill and check</div><span>→</span>
<div><strong>Accept</strong><br>Next checkpoint</div>
</div>

## Board objective

Let $U$ count open connections and $S$ measure asymmetry between equivalent channels. Let $C$ be layout cost. Rank candidates by $U$, then $S$, then $C$. Clearance, electrical limits, and protected geometry constrain which candidates are eligible.

Let $L$ be trace length in millimeters, $L_{45}$ its diagonal portion, $V$ the number of vias, and $J$ the bend count after merging collinear runs. Let $D$ be the layer penalty, $M$ placement movement, $R$ copper rework, and $C_{\mathrm{silk}}$ silkscreen cost. Each $w$ sets the weight of its term.

$$
\operatorname{lex\,min}(U,S,C),\qquad
C=w_L L+w_{45}L_{45}+w_V V+w_J J+D+w_M M+R+w_I C_{\mathrm{silk}}.
$$

Compare channels by circuit role and connectivity in translated, rotated, or mirrored local frames. The default positional tolerance is 0.05 mm. Via alignment, bend patterns, and topology also contribute. Record differences forced by fixed endpoints.

## Placement

For component $i$, its pose $x_i$ comprises position $p_i$, rotation $\theta_i$, and board side $b_i$. For net $n$, let $W_n$ and $H_n$ be the width and height of its pin bounding box and $w_n$ its importance. Let $C_{\mathrm{cong}}$ estimate routing congestion and $C_{\mathrm{prox}}$ penalize departures from placement spacing targets. The placement score is $\widehat C_{\mathrm{place}}$.

$$
x_i=(p_i,\theta_i,b_i),\qquad
\widehat C_{\mathrm{place}}
=\sum_n w_n(W_n+H_n)+w_C C_{\mathrm{cong}}+w_P C_{\mathrm{prox}}+w_M M.
$$

$W_n+H_n$ is the net's half-perimeter wirelength. Use it to rank placements before routing. Recovery tries bounded moves of eligible components and reroutes their affected nets.

## Checkpoints and search

Let $B_{\mathrm{saved}}$ be the starting board. At step $t$, $B_t$ is the current board, $q_t$ selects pins and optional same-net copper, $P_t$ is its pin set, and $k$ is the pin limit. The acceptance operation $A$ applies a validated route $r_t$.

$$
B_0=B_{\mathrm{saved}},\qquad |P_t|\le k,\qquad B_{t+1}=A(B_t,q_t,r_t).
$$

For order $s$, $\widehat C(s)$ estimates final cost, $\widehat T(s)$ estimates search time, and $\lambda_T$ weights time. The order model ranks $\widehat C(s)+\lambda_T\widehat T(s)$. The implemented scheduler uses net priority, copper islands, and compatible windows, with bounded backtracking.

For route $r$ in the checkpoint's legal route set $\mathcal R_t$, let $\Delta C_t(r)$ be immediate cost change and $\widehat C_{\mathrm{remain}}(r)$ the estimated cost to finish. The look-ahead model favors route $r_t^\star$.

$$
r_t^\star\approx \arg\min_{r\in\mathcal R_t}
\big[\Delta C_t(r)+\widehat C_{\mathrm{remain}}(r)\big].
$$

A grid state $v$ tracks position, layer, and arrival heading. Weighted A* ranks it by $g(v)+\eta h(v)$, where $g$ is the accumulated cost, $h$ estimates the remaining cost, and $\eta$ is the heuristic weight, normally 1. Exhausted search retains the best validated candidate and reports the remaining opens.

<!-- pagebreak -->

# Routing checks and acceptance

## Geometry and rework

Default local cost is $L+L_{45}+10V+J$, plus configured layer and guide costs. For replaced trace $e$, let $d_e$ be the length no longer reproduced at the same position, layer, and width. Let $m_v$ be 1 if via $v$ changes position, span, drill, or diameter, and 0 otherwise.

$$
R=\sum_e \mathbf 1[d_e>0]+\sum_e d_e+10\sum_v m_v .
$$

Each track heading is a multiple of 45°. At a true bend, the change in heading is an odd multiple of 45°. Branches and pad contacts are checked as junctions. Check each candidate for copper and drill clearance, width, keepouts, via access, shorts, and preserved connections.

After collinear merging, let $\phi_i$ be run $i$'s heading in eighth-turns on an unbranched same-layer chain. Let $N_{\mathrm{stair}}$ count windows that repeat a diagonal heading across one cardinal run. Set the exception $E_i=1$ when that window lies inside a complete ordinary dogleg, such as $0^\circ,45^\circ,90^\circ,45^\circ,0^\circ$. Otherwise $E_i=0$.

$$
N_{\mathrm{stair}}=\sum_i \mathbf 1[\phi_i\text{ diagonal},\ \phi_{i+1}\text{ cardinal},
\phi_{i+2}=\phi_i](1-E_i).
$$

## Electrical and silkscreen checks

For a radial load scenario, $I_j$ is load $j$'s current and $I_e$ is the current in branch $e$. For branch length $L_e$, width $w_e$, copper thickness $t_e$, and copper resistivity $\rho$, the DC voltage drop $\Delta V_p$ along supply path $p$ is

$$
I_e=\sum_{j\ \mathrm{downstream\ of}\ e} I_j,\qquad
\Delta V_p=\sum_{e\in p} I_e\,\frac{\rho L_e}{w_e t_e}.
$$

Check width, vias, and voltage clearance against the actual load cases and fabrication limits. Qualify differential-pair width, gap, impedance, and return paths on the actual stackup. The working router preserves deferred pair copper.

For label $i$, let $a_i$ be its anchor and $K_i$ its footprint region. Let $u$ range over $K_i$. The nearest distance is $d_i=\inf_{u\in K_i}\|a_i-u\|_2$. Let $A_{c,i}$, $A_{h,i}$, and $A_{o,i}$ be ink area clipped by fabrication, hidden by components, and overlapping other labels. The weights $\alpha_i,\beta_i,\gamma_i,\delta_i$ penalize distance and the three areas in that order.

$$
C_{\mathrm{silk}}=\sum_i\big(\alpha_i d_i+\beta_i A_{c,i}
+\gamma_i A_{h,i}+\delta_i A_{o,i}\big).
$$

## Acceptance

| Step | Required check |
| --- | --- |
| Preview | Refill zones and independently check protected objects, native rules, and retained connectivity. |
| Apply | Recheck saved inputs, effective policy, and editor state. Keep a recoverable board copy. |
| Checked export | Require passing DRC, ERC, schematic parity, and connectivity plus evidence for electrical and reference-copper checks. Track hardware validation and production approval separately. |

Resume with matching inputs. Renew after a tool change to revalidate the candidate and rebuild the queue. Make a fresh preview after a source or policy change.
