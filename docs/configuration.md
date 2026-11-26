# AutoPCB configuration reference

This page lists every key a user can set in the two routing configuration files, with its type, unit, default and the code that reads it. Defaults are quoted from the code. Where the code gives no default the table says "no default". Where the code does not settle a point the text says "not determined from code". The page describes software behavior only. It says nothing about hardware or about whether a board can be built.

## Files

| File | Location | Written by | Read by |
| --- | --- | --- | --- |
| Routing defaults | `<workspace>/config/routing.defaults.json` | The user. The repository ships one at [`config/routing.defaults.json`](../config/routing.defaults.json). | [`policy.py`](../scripts/autopcb/policy.py), through [`cli.py`](../scripts/autopcb/cli.py) |
| Project routing policy | `<workspace>/config/projects/<project>/autopcb.json` | `configure-layout` and `create`, then the user | [`checkpoints.py`](../scripts/pcb/checkpoints.py) `read_config`, then the routing engine |

The workspace is the folder given with `--workspace`, or the current folder. `<project>` is the board file name without its suffix when the board sits at `results/projects/<name>/`. For any other board it is the sanitized name, a hyphen and 16 hexadecimal digits of a hash of the board path, for example `My-Board-v2-2edcae959b804711` ([`paths.py`](../scripts/core/paths.py)).

A policy is looked up at the path above first. When only a legacy copy exists, `autopcb/config/autopcb.json` beside the KiCad files if that folder exists, otherwise `config/autopcb.json` beside them, that copy is used. `route` stops with "Create and review the project routing policy before running layout." when neither file exists ([`layout.py`](../scripts/autopcb/layout.py)).

## Value rules

- Every distance is in millimetres. Keys that carry a unit say so in the name (`width_mm`). A distance may have at most 6 decimal places (1 nm). A value such as 0.1234567 fails with "Coordinates must be finite and have at most nanometre precision" ([`route_optimizer.py`](../scripts/pcb/route_optimizer.py)).
- Times are in seconds, currents in amperes (A), voltages in volts (V), resistances in ohms, resistivity in ohm millimetres, angles in degrees.
- Counts are JSON integers. A boolean is accepted only as `true` or `false`, never as 0 or 1. Where a number is required, `true` and `false` are refused.
- JSON `NaN` and `Infinity` are refused in a policy file. A UTF-8 byte order mark is tolerated.
- Layer names are KiCad copper layer names: `F.Cu`, `In1.Cu` to `In30.Cu`, `B.Cu`.
- The policy file uses `schema`. The defaults file uses `schema_version`. They are not interchangeable.

## routing.defaults.json

The file gives the starting dimensions that `configure-layout` and `create` write into a new policy. It does not affect an existing policy. When the file is missing the built-in values below apply, and no message is shown.

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `schema_version` | integer | none | no default, must be `1` when the file exists | Version marker. Any other value, a boolean or a missing key raises an error. |
| `search_margin_mm` | number, above 0 | mm | `6.0` | Written to the policy as `search_margin_mm`. |
| `step_mm` | number, above 0 | mm | `0.15` | Written to the policy as `route.step_mm`. |
| `width_mm` | number, above 0 | mm | `0.25` | Starting track width. Raised to the board's minimum track width. |
| `clearance_mm` | number, above 0 | mm | `0.25` | Starting clearance. Raised to the board's minimum clearance. |
| `via_size_mm` | number, above 0 | mm | `0.65` | Starting via diameter. Raised as described below. |
| `via_drill_mm` | number, above 0 | mm | `0.30` | Starting via drill. Raised to the board's minimum through-hole diameter. |

A defaults file may hold any subset of the six dimensions. Keys it omits keep the built-in value. Any other key, a string or boolean value, zero, a negative number or a non-finite number fails with "Expected version 1 routing defaults containing only geometry and search distances." or "Routing defaults must be positive, finite dimensions in millimeters." A file that is not a JSON object fails with "Routing defaults must be a JSON object." The command then reports `status` `blocked` and exits with status 2. All of this is in [`policy.py`](../scripts/autopcb/policy.py).

### Board minima

`configure-layout` also reads five board limits from the board's `.kicad_pro` file, under `board.design_settings.rules`. They are set in KiCad, not in AutoPCB. `create` does not read them.

| Key | Unit | Default when absent | Effect |
| --- | --- | --- | --- |
| `min_clearance` | mm | `0` | `route.clearance_mm` is the larger of the default and this value. |
| `min_track_width` | mm | `0` | `route.width_mm` is the larger of the default and this value. |
| `min_through_hole_diameter` | mm | `0` | `route.via.drill_mm` is the larger of the default and this value. |
| `min_via_annular_width` | mm | `0` | The annular width used below is the larger of `0.15` mm and this value. |
| `min_via_diameter` | mm | `0` | A lower bound on `route.via.size_mm`. |

`route.via.size_mm` is the largest of `via_size_mm`, the via drill plus twice the annular width, and `min_via_diameter`. The `0.15` mm annular floor is fixed in the code. `search_margin_mm` and `step_mm` are never changed by the board minima.

## Project policy: autopcb.json

### What the commands write

`configure-layout` runs `RoutingPolicy.compile` and writes the file with these sections only: `schema`, `max_pins`, `search_margin_mm`, `route`, `working_scope` and `exclude_nets`. For a two-layer board with the shipped defaults it writes:

```json
{
  "schema": 1,
  "max_pins": 3,
  "search_margin_mm": 6.0,
  "route": {
    "layers": ["F.Cu", "B.Cu"],
    "width_mm": 0.25,
    "clearance_mm": 0.25,
    "step_mm": 0.15,
    "via": {"size_mm": 0.65, "drill_mm": 0.3},
    "assume_zone_refill": true
  },
  "working_scope": {
    "kind": "connectivity",
    "classes": ["Default"],
    "manufacturing_approved": false
  },
  "exclude_nets": []
}
```

The command fails with "A routing policy already exists" when the target file exists. When only a legacy policy exists, the command copies that file's content unchanged and does not compile a new one.

`create` writes the same sections through the same compiler, with two differences. `exclude_nets` lists the two nets of every differential pair in the proposal, each with a leading `/` (for example `/USB_N`). `route.assume_zone_refill` is `true` only when the proposal has a pair layout and `false` otherwise.

Nothing writes `include_nets`, `priorities`, `net_overrides`, `search`, `max_nets`, `max_checkpoints`, `recovery`, `rework` or `zone_stitch`. Add them by hand. Hand edits survive because `configure-layout` never replaces an existing policy.

### Top-level keys

The keys below are the only ones `read_config` accepts. Any other key fails with "Invalid AutoPCB configuration or unexpected fields".

| Key | Type | Unit | Default | What it does | Read by |
| --- | --- | --- | --- | --- | --- |
| `schema` | integer | none | no default, must be `1` | Version marker. | [`checkpoints.py`](../scripts/pcb/checkpoints.py) |
| `max_pins` | integer, 1 to 20 | pins | no default, required. Written as `3`. | Most pins, as separate copper islands, that one checkpoint search joins. The board-wide router needs 2 to 20 and fails with "Routing checkpoints require a maximum of at least two islands" for 1. Only the checkpoint planner accepts 1. | [`checkpoints.py`](../scripts/pcb/checkpoints.py), [`controller.py`](../scripts/pcb/net_routing/controller.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py) |
| `search_margin_mm` | number above 0, or `null` | mm | no default. When absent the board-wide router uses `8.0`. The checkpoint planner then searches the whole board. Written as `6.0`. | Margin added around the pads and copper of each unfinished net to form its search window, clipped to the board outline. | [`scheduler.py`](../scripts/pcb/net_routing/scheduler.py), [`runner.py`](../scripts/pcb/net_routing/runner.py), [`prepare.py`](../scripts/pcb/net_routing/prepare.py), [`reference_repair.py`](../scripts/pcb/net_routing/reference_repair.py), [`checkpoints.py`](../scripts/pcb/checkpoints.py) |
| `include_nets` | array of strings | none | `[]` | Checkpoint planner only: when not empty, only these nets are planned, and a name absent from the board is an error. The board-wide router ignores it and reports `legacy_include_list_ignored`. Names are non-empty and unique. | [`checkpoints.py`](../scripts/pcb/checkpoints.py), [`controller.py`](../scripts/pcb/net_routing/controller.py) |
| `exclude_nets` | array of strings | none | `[]` | Nets the router must leave alone. They are not scheduled (blocked as "excluded by routing policy"). Rework does not remove copper on them. A part with a pad on one of them is not a candidate for a move. The checkpoint planner skips them. Names are non-empty and unique and must match KiCad net names exactly. They are not checked against the board. | [`controller.py`](../scripts/pcb/net_routing/controller.py), [`protection.py`](../scripts/pcb/net_routing/protection.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py), [`relocation.py`](../scripts/pcb/net_routing/relocation.py), [`checkpoints.py`](../scripts/pcb/checkpoints.py), [`runner.py`](../scripts/pcb/net_routing/runner.py), [`crowding.py`](../scripts/autopcb/crowding.py) |
| `priorities` | object, net name to integer | rank | `{}`. A net not listed has rank `100`. | Scheduling rank. Lower ranks are scheduled first, and the rounds of one rank are placed before those of the next. Values are integers, not booleans. | [`scheduler.py`](../scripts/pcb/net_routing/scheduler.py), [`controller.py`](../scripts/pcb/net_routing/controller.py), [`checkpoints.py`](../scripts/pcb/checkpoints.py) |
| `route` | object | see below | `{}` in the code. Written with the keys shown above. | Settings every net starts from. See the next section. | [`controller.py`](../scripts/pcb/net_routing/controller.py), [`backend.py`](../scripts/pcb/net_routing/backend.py) |
| `net_overrides` | object, net name to object | see below | `{}` | Per-net settings. Each value takes the same keys as `route`. Names are non-empty and not checked against the board. | [`controller.py`](../scripts/pcb/net_routing/controller.py), [`checkpoints.py`](../scripts/pcb/checkpoints.py), [`relocation.py`](../scripts/pcb/net_routing/relocation.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py) |
| `search` | object | see below | `{}`, meaning the defaults of every `search` key | Search limits. | [`model.py`](../scripts/pcb/net_routing/model.py) |
| `max_nets` | integer, 1 or more | nets | `1000` | Checkpoint planner only: the most nets it plans. The rest are listed as skipped. | [`checkpoints.py`](../scripts/pcb/checkpoints.py) |
| `max_checkpoints` | integer, 1 or more | checkpoints | `5000` | Checkpoint planner only: the most checkpoints it keeps. | [`checkpoints.py`](../scripts/pcb/checkpoints.py) |
| `recovery` | object | see below | `{}`, meaning the defaults of every `recovery` key | How a failed search escalates. | [`recovery.py`](../scripts/pcb/net_routing/recovery.py) |
| `rework` | object | see below | `{}`, meaning the defaults of every `rework` key | Prices of changes to existing copper and parts. | [`rework.py`](../scripts/pcb/net_routing/rework.py) |
| `zone_stitch` | object | see below | `{}` | Evidence for ground-island stitching. | [`zone_stitch.py`](../scripts/pcb/net_routing/zone_stitch.py) |
| `working_scope` | object | see below | `{}`, meaning no scope | Which net classes a run may change. | [`protection.py`](../scripts/pcb/net_routing/protection.py), [`fast_rework.py`](../scripts/pcb/net_routing/fast_rework.py) |
| `placement` | object | see the placement section | all parts may move | Which parts may move. | [`move_scope.py`](../scripts/pcb/move_scope.py) |

### route and net_overrides

`route` and each `net_overrides` entry accept the same keys. The engine builds the settings of a net by merging them, `{**route, **net_overrides[net]}`. The merge is shallow, so an override of `via`, `weights`, `layer_penalties` or `electrical` replaces the whole object. A `via` override that sets only `size_mm` leaves the drill at its default, not at the value in `route`.

`read_config` checks only the key names and `route.layers`. The other values are checked when a net's settings are built, and an error is reported as a `blocked` result with its message and exit status 2. For example a via size of 0.3 mm with a drill of 0.4 mm reports "Via size must exceed its positive drill diameter".

The defaults below are those of [`backend.py`](../scripts/pcb/net_routing/backend.py) `Settings`, the class that builds a net's settings for routing. `configure-layout` writes `layers`, `width_mm`, `clearance_mm`, `step_mm`, `via` and `assume_zone_refill`, so the other defaults apply unless you add the key. Some other readers use a different fallback when a key is absent, and those are listed after the table.

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `bounds_mm` | array of 4 numbers `[x_min, y_min, x_max, y_max]` | mm | no default | Search window. The board-wide router replaces it with each net's scheduled window and rejects it in `net_overrides` ("Per-net bounds override the scheduled window"). The checkpoint planner keeps it as a fixed window. |
| `step_mm` | number above 0 | mm | `0.25` | Grid pitch of the search. When the window and layer count would exceed `max_cells`, the pitch is raised until the grid fits. |
| `layers` | array of distinct layer names | none | the copper layers the saved board defines, or `["F.Cu", "B.Cu"]` | Layers the search may use. Duplicates and unknown names are refused. |
| `width_mm` | number above 0 | mm | `0.2` | Track width. |
| `clearance_mm` | number above 0 | mm | `0.2` | Copper clearance. |
| `via` | object `{size_mm, drill_mm}` | mm | `{}` | Via dimensions, see the next table. |
| `weights` | object | cost | `{}` | Search cost weights, see the next table. |
| `symmetry_guide` | object | mm | none | Guide line for the `symmetry` weight, see the next table. |
| `assume_zone_refill` | boolean | none | `false` where the key is read as a fallback. Written as `true` by `configure-layout`. | Whether routing counts on zones being refilled afterwards, so zone copper gives access to a net. All nets of one run must use the same value, otherwise planning fails with "All nets must use the same zone-refill assumption". A `net_overrides` entry that differs from `route` therefore fails. |
| `max_expansions` | integer, 1 or more | search states | `30000` | Limit on search states expanded by one path search. |
| `max_cells` | integer, 1 or more | grid cells | `250000` | Limit on grid cells over all layers. A grid larger than this is refused. |
| `electrical` | object | see below | none | Supplied electrical limits for the net. Needs `current_model`. |
| `current_model` | string | none | none | The only accepted value is `whole_net_upper_bound`. It is required with `electrical` and refused without it ("current_model requires supplied electrical limits"). It also makes the track width a hard minimum. |
| `layer_penalties` | object, layer name to number | cost per mm of track | `{}` | Extra cost per millimetre of track on a layer. Values are finite and not negative. |
| `forbid_staircase` | boolean | none | `true` | Rejects diagonal, cardinal, diagonal staircases away from same-net pad copper. |
| `min_segment_length_mm` | number, 0 or more | mm | `0` | Shortest segment between bends. |
| `gentle_turns` | boolean | none | `true` | Also refuses 135 degree turns outside pad copper. 90 degree turns and reversals are always refused there. |
| `initial_directions` | array of headings | none | none, any heading | Headings allowed for the first segment. A heading is one of `E`, `SE`, `S`, `SW`, `W`, `NW`, `N`, `NE`. The list is not empty. |
| `final_directions` | array of headings | none | none, any heading | The same for the last segment. |
| `enforce_endpoint_spacing` | boolean | none | `false` | Applies `min_segment_length_mm` to the first and last segment too. |
| `first_feasible` | boolean | none | `false` | Prefers a feasible route found quickly over the cheapest one. |
| `topology_heuristic` | boolean | none | `false` | Guides the search with a relaxed multilayer cost bound. Escalated recovery stages turn it on. |
| `heuristic_weight` | number, 1 or more | none | `1.0` | Factor on the search estimate. Finite and at least 1. Escalated recovery stages raise it to at least `recovery.heuristic_weight`. |

Readers: [`backend.py`](../scripts/pcb/net_routing/backend.py) for all keys, [`route_optimizer.py`](../scripts/pcb/route_optimizer.py) `GridRouter` for the value checks, [`workflow.py`](../scripts/pcb/net_routing/workflow.py) for `assume_zone_refill`, and [`route_batch.py`](../scripts/pcb/route_batch.py) for the list of accepted names (`SHARED_FIELDS`, plus `current_model`).

Effective dimensions: the width and clearance a segment uses are the largest of the policy value, the board's net class value and the board's minimum from KiCad Board Setup, with any saved custom rule applied on top ([`rules.py`](../scripts/pcb/net_routing/rules.py), [`routability.py`](../scripts/pcb/routability.py)). Lowering `width_mm` or `clearance_mm` below the board's own values therefore has no effect on those segments.

Fallbacks used when a key is absent:

- `step_mm`: `0.1` in [`obstructions.py`](../scripts/pcb/net_routing/obstructions.py) and [`reroute.py`](../scripts/pcb/net_routing/reroute.py).
- `width_mm`: `0.25` in [`obstructions.py`](../scripts/pcb/net_routing/obstructions.py).
- `layers`: `["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]` in [`controller.py`](../scripts/pcb/net_routing/controller.py), and all 32 copper layer names in [`checkpoints.py`](../scripts/pcb/checkpoints.py).
- `max_expansions`: `200000` in [`recovery.py`](../scripts/pcb/net_routing/recovery.py) and [`route_cli.py`](../scripts/pcb/route_cli.py).
- `max_cells`: `1000000` in [`route_cli.py`](../scripts/pcb/route_cli.py).

`via`:

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `size_mm` | number above 0 | mm | `0.6` | Via diameter. Must be larger than `drill_mm`. |
| `drill_mm` | number above 0 | mm | `0.3` | Via drill diameter. |

`weights`, from [`route_optimizer.py`](../scripts/pcb/route_optimizer.py) `Weights`. Every weight is finite and not negative, and `length` is above 0.

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `length` | number | cost per mm | `1.0` | Cost per millimetre of track. |
| `bend` | number | cost per bend | `1.0` | Cost per bend. |
| `via` | number | cost per via | `10.0` | Cost per new via. |
| `diagonal` | number | cost per mm | `1.0` | Extra cost per millimetre of diagonal track. |
| `symmetry` | number | cost per mm per mm | `0.0` | Cost per millimetre of track, scaled by its distance in mm from `symmetry_guide`. A non-zero value without a guide fails with "A symmetry weight requires a guide". |

`symmetry_guide`, from [`route_cli.py`](../scripts/pcb/route_cli.py):

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `points_mm` | array of `[x, y]` | mm | no default, required | Points of the guide line. |
| `mirror_axis` | `"x"` or `"y"` | none | none | Reflects the points about the line `x = mirror_at_mm` or `y = mirror_at_mm`. |
| `mirror_at_mm` | number | mm | none | Position of the mirror line. It is required with `mirror_axis` and refused without it. |

`electrical`, from [`route_constraints.py`](../scripts/pcb/route_constraints.py). Every key is required except `via`, and every number is finite and above 0. Each `basis` is a non-empty text naming the source of the value. The routing checks these limits as supplied. They are not derived by AutoPCB and a pass is not an approval.

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `current_a` | number | A | no default | Worst credible branch current. |
| `current_basis` | string | none | no default | Source of `current_a`. |
| `voltage_difference_v` | number | V | no default | Worst-case voltage difference to neighboring nets. |
| `voltage_basis` | string | none | no default | Source of that voltage. |
| `clearance.min_mm` | number | mm | no default | Smallest electrical clearance. The route clearance may not be below it. |
| `clearance.max_voltage_difference_v` | number | V | no default | Voltage the clearance is rated for. It must not be below `voltage_difference_v`. |
| `clearance.basis` | string | none | no default | Source of the clearance rating. |
| `voltage_drop.max_v` | number | V | no default | Largest voltage drop along the route. |
| `voltage_drop.copper_resistivity_ohm_mm` | number | ohm mm | no default | Copper resistivity used for the drop. |
| `voltage_drop.basis` | string | none | no default | Source of the drop budget. |
| `tracks` | object, layer name to object | none | no default, at least one layer | Track limit of each permitted layer. A layer without an entry is excluded. |
| `tracks.<layer>.min_width_mm` | number | mm | no default | Narrowest track that carries the rated current. |
| `tracks.<layer>.max_current_a` | number | A | no default | Current the track may carry. |
| `tracks.<layer>.copper_thickness_mm` | number | mm | no default | Copper thickness the rating assumes. It may not exceed the thickness in the saved stackup. |
| `tracks.<layer>.basis` | string | none | no default | Source of the rating. |
| `via` | object or `null` | none | none, no via permitted | Via limit, with the keys below. |
| `via.min_size_mm` | number | mm | no default | Smallest via diameter. Must exceed `min_drill_mm`. |
| `via.min_drill_mm` | number | mm | no default | Smallest drill. |
| `via.plating_thickness_mm` | number | mm | no default | Plating thickness the rating assumes. |
| `via.max_current_a` | number | A | no default | Current each via may carry. |
| `via.max_resistance_ohm` | number | ohm | no default | Resistance charged for each via. |
| `via.basis` | string | none | no default | Source of the via rating. |

### search

All keys are optional. An unknown key, a boolean, or a value outside the range fails with a message naming the key. From [`model.py`](../scripts/pcb/net_routing/model.py) `Limits`. `autopcb route --seconds N` replaces `seconds` for every batch.

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `tree_beam` | integer, 1 or more | trees | `4` | Partial trees kept per step. |
| `board_beam` | integer, 1 or more | plans | `4` | Partial board plans kept per step. |
| `net_candidates` | integer, 1 or more | candidates | `3` | Complete candidates kept per net. |
| `path_candidates` | integer, 1 or more | paths | `2` | Alternative paths kept per endpoint pair. |
| `connections_per_step` | integer, 1 or more | pairs | `12` | Endpoint pairs tried per tree. When set without `fallback_connections_per_step`, it also caps the fallback sample. |
| `fallback_connections_per_step` | integer, 1 or more | pairs | `96` | Wider endpoint sample used when the first yields no useful tree. |
| `sites_per_island` | integer, 1 or more | sites | `48` | Access sites kept per island. |
| `branch_sites` | integer, 1 or more | sites | `32` | Branch sites sampled on tree copper. |
| `order_trials` | integer, 1 or more | orders | `4` | Net orders tried. |
| `placement_trials` | integer, 1 or more | combinations | `8` | Explicit placement combinations allowed. |
| `path_calls` | integer, 1 or more | searches | `400` | Path searches allowed per batch. |
| `net_path_calls` | integer, 1 or more | searches | `120` | Path searches allowed per net. |
| `endpoint_seconds` | integer, 1 or more | s | `5` | Time share for one access point. It must be a whole number. |
| `cache_entries` | integer, 1 or more | entries | `128` | Path results cached. |
| `validation_candidates` | integer, 1 or more | candidates | `3` | Candidates carried to validation. |
| `smoothing_trials` | integer, 0 or more | trials | `256` | Shortcut trials per route. 0 turns smoothing off. |
| `seconds` | number above 0 | s | `120.0` | Search time limit. |
| `seed` | integer, 0 or more | none | `0` | Seed for generated net orders. |

### recovery

All keys are optional. An unknown key, a boolean where a number is needed, a non-integer where an integer is needed or a value outside the range fails with a message such as "Recovery supports at most 3 stages and 64 alternatives per stage". From [`recovery.py`](../scripts/pcb/net_routing/recovery.py) `RecoveryPolicy`. `autopcb route` without `--allow-placement` sets `placement_candidates` to 0 for the run, whatever the file says.

| Key | Type | Unit | Default | Allowed | What it does | Read by |
| --- | --- | --- | --- | --- | --- | --- |
| `early_rework` | boolean | none | `false` | `true` or `false` | After a net's first failed stage-0 search, also queues a copper and part-move repair for that net, so the repair does not wait for the widening stages to finish. Also keeps early rework requests when a saved queue is renewed. | [`runner.py`](../scripts/pcb/net_routing/runner.py), [`recovery.py`](../scripts/pcb/net_routing/recovery.py) |
| `stages` | integer | stages | `3` | 0 to 3 | Widening stages before copper repair, in order: wider pairs, finer pairs, board detour. | [`recovery.py`](../scripts/pcb/net_routing/recovery.py), [`runner.py`](../scripts/pcb/net_routing/runner.py) |
| `alternatives_per_stage` | integer | endpoint pairs | `3` | 1 to 64 | Checkpoint choices tried per stage. | [`runner.py`](../scripts/pcb/net_routing/runner.py) |
| `defer_rounds` | integer | rounds | `2` | 0 or more | How far down the queue a failed net is deferred. | [`runner.py`](../scripts/pcb/net_routing/runner.py) |
| `wide_margin_mm` | number | mm | `12.0` | above 0 | Margin added around a net for wider windows. | [`recovery.py`](../scripts/pcb/net_routing/recovery.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py), [`relocation.py`](../scripts/pcb/net_routing/relocation.py), [`obstructions.py`](../scripts/pcb/net_routing/obstructions.py) |
| `max_cells` | integer | grid cells | `1000000` | 1 or more | Grid cell limit of escalated searches. | [`recovery.py`](../scripts/pcb/net_routing/recovery.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py) |
| `max_expansions` | integer | search states | `800000` | 1 or more | Expansion limit of escalated searches. | [`recovery.py`](../scripts/pcb/net_routing/recovery.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py) |
| `path_candidates` | integer | paths | `3` | 1 or more | Alternative paths per endpoint pair in escalated searches. | [`recovery.py`](../scripts/pcb/net_routing/recovery.py) |
| `heuristic_weight` | number | none | `2.0` | 1 or more | Weight on the heuristic in escalated searches. | [`recovery.py`](../scripts/pcb/net_routing/recovery.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py) |
| `rip_up_candidates` | integer | candidates | `3` | 0 to 16 | Copper replacement candidates. 0 turns copper replacement off. | [`reroute.py`](../scripts/pcb/net_routing/reroute.py), [`runner.py`](../scripts/pcb/net_routing/runner.py) |
| `rip_up_items` | integer | copper items | `12` | 1 to 64 | Copper items one replacement may remove. | [`reroute.py`](../scripts/pcb/net_routing/reroute.py) |
| `whole_net_rip_up_items` | integer | copper items | `32` | 0 to 64 | Copper items of a whole-net replacement. | [`reroute.py`](../scripts/pcb/net_routing/reroute.py) |
| `rip_up_nets` | integer | nets | `3` | 1 to 8 | Nets one replacement may touch. | [`reroute.py`](../scripts/pcb/net_routing/reroute.py) |
| `repair_islands` | integer | islands | `12` | 2 to 20 | Copper islands a repair may reconnect. | [`reroute.py`](../scripts/pcb/net_routing/reroute.py), [`relocation.py`](../scripts/pcb/net_routing/relocation.py) |
| `rip_up_radius_mm` | number | mm | `1.5` | above 0 | Radius around an endpoint searched for removable copper. | [`reroute.py`](../scripts/pcb/net_routing/reroute.py), [`relocation.py`](../scripts/pcb/net_routing/relocation.py) |
| `obstruction_trials` | integer | searches | `4` | 0 to 16 | Obstruction diagnosis searches. | [`obstructions.py`](../scripts/pcb/net_routing/obstructions.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py), [`relocation.py`](../scripts/pcb/net_routing/relocation.py) |
| `obstruction_seconds` | number | s | `10.0` | above 0 | Time limit of one diagnosis. | [`obstructions.py`](../scripts/pcb/net_routing/obstructions.py) |
| `placement_candidates` | integer | candidates | `6` | 0 to 32 | Part-move candidates. 0 turns part moves off. | [`relocation.py`](../scripts/pcb/net_routing/relocation.py), [`reroute.py`](../scripts/pcb/net_routing/reroute.py), [`runner.py`](../scripts/pcb/net_routing/runner.py) |
| `placement_nets` | integer | nets | `12` | 1 to 32 | Nets one part move may affect. | [`relocation.py`](../scripts/pcb/net_routing/relocation.py) |
| `placement_items` | integer | copper items | `24` | 1 to 64 | Copper items one part move may remove. | [`relocation.py`](../scripts/pcb/net_routing/relocation.py) |

### rework

All keys are optional. Each is a finite number that is not negative. An unknown key or a bad value fails with "Unknown rework weights" or "Rework weights must be finite and nonnegative". From [`rework.py`](../scripts/pcb/net_routing/rework.py) `ReworkWeights`, read by [`reroute.py`](../scripts/pcb/net_routing/reroute.py), [`relocation.py`](../scripts/pcb/net_routing/relocation.py), [`workflow.py`](../scripts/pcb/net_routing/workflow.py) and [`checkpoints.py`](../scripts/pcb/checkpoints.py). The weights price a change. They are a preference and not an electrical qualification.

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `track` | number | cost per changed track item | `1.0` | Cost of each track item changed. |
| `length_mm` | number | cost per mm | `1.0` | Cost per millimetre of changed copper. |
| `via` | number | cost per via | `10.0` | Cost of each via changed. |
| `placement_mm` | number | cost per mm | `1.0` | Cost per millimetre a part moves. |
| `quarter_turn` | number | cost per 90 degrees | `1.0` | Cost per 90 degrees a part turns. |

### working_scope

`working_scope` limits a run to chosen KiCad net classes. Without it, [`protection.py`](../scripts/pcb/net_routing/protection.py) protects power, monitoring and actuator-safety nets that it recognizes by part and net names, and routes only nets in the `SIGNAL` class or a class ending in `_SIGNAL`. With a non-empty `working_scope`, every net whose class is not listed is protected, and so are nets named `unconnected-*` and nets in the `USB_DATA` and `ETH_DATA` classes. Class names are compared without regard to case. An empty object `{}` means no scope.

When the object is not empty, all three keys are required and checked.

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `kind` | string | none | no default | Must be `"connectivity"`. |
| `classes` | array of strings | none | no default | Net class names the run may change. Not empty, each name non-empty and unique. |
| `manufacturing_approved` | boolean | none | no default | Must be `false`. Any other value fails with "Connectivity scope requires named classes and an explicit manufacturing hold". |

Also read by [`fast_rework.py`](../scripts/pcb/net_routing/fast_rework.py), which needs a `connectivity` scope for ground-island repair and raises "Ground-first repair requires an explicit working-connectivity scope" without one.

### zone_stitch

`zone_stitch` holds evidence for ground-island stitching. The only key is `proofs`. Any other key fails with "Invalid zone-stitch settings or unexpected fields".

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `proofs` | array of objects | none | `[]` | One entry per ground pad. Read by `StitchAllowances.proofs_from_policy` in [`zone_stitch.py`](../scripts/pcb/net_routing/zone_stitch.py), which raises "Invalid zone-stitch branch evidence" for a malformed entry. Any `evidence.source` file named inside is hashed into the run inputs, as a path relative to the project folder ([`runner.py`](../scripts/pcb/net_routing/runner.py)). |

Each entry of `proofs` has these keys. All are required except `island_items` and `board_sha256`, which default to an empty list and an empty string. An empty `board_sha256` never matches a board, so the entry is skipped.

| Key | Type | Unit | What it does |
| --- | --- | --- | --- |
| `pad_id` | string | none | UUID of the ground pad. It appears once per file. |
| `dimensions` | object | nm | Integers `track`, `clearance`, `via`, `drill` and `hole_clearance` in KiCad internal units, 1 nm each. They must equal the board's values. |
| `max_length` | integer above 0 | nm | Longest stitch the proof covers. |
| `branches` | array | none | Exactly two branches. `track` goes from `pad` to `via` with kind `track`, and `via` goes from `via` to `plane` with kind `via`. Each has `identifier`, `parent`, `child`, `kind`, `capacity` and `resistance`. |
| `scenarios` | array | none | Must include the names `normal`, `transient` and `fault`. Each has `name`, `loads` and `max_drop`. Each `loads` entry has one `node`, which must be `plane`, and a `quantity`. |
| `island_items` | array of strings | none | UUIDs of every item in the island. It must contain `pad_id`. |
| `board_sha256` | string | none | SHA-256, 64 lowercase hexadecimal digits, of the board the proof was made for. |

A quantity is `{"value", "unit", "evidence"}` with a finite value that is not negative. The unit is `A` for `capacity` and for loads, `ohm` for `resistance` and `V` for `max_drop`. `evidence` has `source` (a file path relative to the project folder), `sha256` of that file, `location` and `status`, which is `verified` or `design_target`. A capacity or resistance needs status `verified` ([`electrical.py`](../scripts/pcb/net_routing/electrical.py)).

In this checkout the assessment never sets `electrically_verified`, and `zone_stitch.py` accepts a proof only when it is set. A proof is therefore read, validated and hashed, but it does not yet produce a stitch allowance. A ground pad in a `working_scope` that includes the net class of `GND` gets a fixed allowance of 2.4 mm without a proof.

### placement

`placement` selects which footprints routing may move. Its three keys are read by `MoveScope.from_config` ([`move_scope.py`](../scripts/pcb/move_scope.py)). Any other key fails with "placement accepts only sides, through_hole and locked". A side that stays fixed also keeps its silkscreen, so a part that prints on that side stays fixed.

| Key | Type | Unit | Default | What it does |
| --- | --- | --- | --- | --- |
| `sides` | array of `"top"` and `"bottom"`, no repeats | none | `["bottom", "top"]` | Sides whose parts may move. |
| `through_hole` | boolean | none | `true` | When `false`, parts with through-hole pads stay fixed. |
| `locked` | boolean | none | `true` | When `false`, parts locked in KiCad stay fixed. The default lets locked parts move. |

Limits in this checkout:

- `read_config` accepts `placement` as a top-level key and validates it with `MoveScope.from_config`. Earlier versions left it out of the accepted keys, so a policy that set it was rejected as "Invalid AutoPCB configuration or unexpected fields".
- `autopcb route` keeps every part fixed unless `--allow-placement` is given. It passes `--keep-placement` and sets `recovery.placement_candidates` to 0.

## Keys for compatibility or internal use

| Key | Status |
| --- | --- |
| `include_nets` | Legacy for the board-wide router, which ignores it. Used by the checkpoint planner (`run.py pcb checkpoints`). |
| `max_nets`, `max_checkpoints` | Used only by the checkpoint planner. |
| `route.bounds_mm` | Used as a fixed window by the checkpoint planner. Replaced by the scheduled window in the board-wide router. |
| `placement` | Accepted by `read_config`. It is read by the engine and by `CrowdedVariant`. |
| `_ground_anchor_candidates`, `_protected_anchor_candidates` | Internal. `fast_rework.py` sets them on an in-memory copy of the policy for ground-island repair. `read_config` rejects them in a file. |
| `schema` | A version marker. Only `1` is accepted. |

Keys the engine builds from the policy, such as `shared`, `nets`, `max_islands`, `checkpoint_offset`, `checkpoint_margin_mm`, `recovery_stage`, `recovery_bounds_mm`, `rip_up`, `movable` and `orders`, belong to the net-planner request in [`workflow.py`](../scripts/pcb/net_routing/workflow.py) and are not policy keys. The `shared` object of that request takes the same keys as `route`.

## Validation

Every consumer reads the policy through `read_config` in [`checkpoints.py`](../scripts/pcb/checkpoints.py). It does the following, in this order:

1. Parses the file as JSON and refuses `NaN` and `Infinity`.
2. Refuses any top-level key outside the list above, and any unknown key inside `working_scope`, `zone_stitch`, `route` and each `net_overrides` entry.
3. Requires `schema` to be `1` and `max_pins` to be an integer from 1 to 20.
4. Checks `working_scope`, `include_nets`, `exclude_nets`, `priorities`, `route.layers` and `search_margin_mm`.
5. Builds `search`, `recovery`, `rework` and `placement` through their own readers, which check the keys, types and ranges listed in the tables.
6. Checks `max_nets` and `max_checkpoints`.
7. Hashes the file again and fails with "AutoPCB configuration changed while being read" if it changed during the read.

An invalid policy stops the command before any search. The command prints a JSON report with `status` `blocked` and the message in `error`, and exits with status 2. No board is changed.

`read_config` does not check the numeric values of `route`, `net_overrides` or `zone_stitch` entries. Those fail later, when a net's settings are built, with the message of the check that failed, again as a `blocked` result.

The policy's SHA-256 is bound into every run record. After a change to the policy, `route --resume` fails with a message that begins "Routing inputs or policy changed" and tells you to start a new run from the saved board. `apply` also refuses a preview made under a different policy.

## Examples

The two policy examples pass `read_config`. They were also run through the work-plan builder, the net-planner request check and the grid-router checks, using placeholder net names. The first example is a net-planner request, not a policy, and it passes the net-planner request check. The examples show syntax, not values to copy. Take the numbers from the board's own design rules.

### Narrowing which parts may move

`autopcb route` keeps every part fixed. The router may move parts only with `--allow-placement`, and then `placement` narrows the choice. The first example puts the key in a net-planner configuration, which runs with `run.py pcb plan-nets --config FILE`. The same `placement` object is also accepted in the project policy. It lets only unlocked, bottom-side, surface-mount parts move, and lists the explicit poses to try for one part.

```json
{
  "board_snapshot": "pcb",
  "shared": {
    "bounds_mm": [0, 0, 60, 40],
    "step_mm": 0.25,
    "layers": ["F.Cu", "B.Cu"],
    "width_mm": 0.2,
    "clearance_mm": 0.2,
    "via": {"size_mm": 0.6, "drill_mm": 0.3}
  },
  "nets": [{"name": "SIGNAL"}],
  "movable": [
    {"reference": "R1", "positions_mm": [[10, 12]], "rotations_deg": [0, 90]}
  ],
  "placement": {"sides": ["bottom"], "through_hole": false, "locked": false}
}
```

`movable` lists the poses to try, and only parts the `placement` scope allows are considered. Every net on a movable part must be listed in `nets`. The `sides` value `["bottom"]` keeps the top silkscreen and top parts fixed. `through_hole: false` keeps through-hole parts fixed. `locked: false` keeps parts locked in KiCad fixed. Omit a key to keep its default.

### Tightening clearance for a fine-pitch board

Lower `width_mm` and `clearance_mm`, use a finer grid, raise `max_cells` so the finer grid fits the window, and give the vias a smaller size. The `net_overrides` entry keeps one supply net wide. The override replaces only the keys it names, so `/VDD` still uses the clearance, grid and vias of `route`.

```json
{
  "schema": 1,
  "max_pins": 3,
  "search_margin_mm": 6.0,
  "route": {
    "layers": ["F.Cu", "B.Cu"],
    "width_mm": 0.15,
    "clearance_mm": 0.15,
    "step_mm": 0.05,
    "max_cells": 1000000,
    "via": {"size_mm": 0.45, "drill_mm": 0.2},
    "assume_zone_refill": true
  },
  "net_overrides": {"/VDD": {"width_mm": 0.4}},
  "working_scope": {
    "kind": "connectivity",
    "classes": ["Default"],
    "manufacturing_approved": false
  },
  "exclude_nets": []
}
```

A lower value takes effect only when the board's own values allow it. The width and clearance of a segment are the largest of these values, the board's net class values and its Board Setup minimums, so set the same limits in KiCad. A finer `step_mm` multiplies the grid cells, and the step is raised again whenever a window would exceed `max_cells`.

### Excluding a net

List the net by its exact KiCad name. Root-sheet nets keep their leading `/`, as in the policies that `create` writes for the USB reference design.

```json
{
  "schema": 1,
  "max_pins": 3,
  "search_margin_mm": 6.0,
  "route": {
    "layers": ["F.Cu", "B.Cu"],
    "width_mm": 0.25,
    "clearance_mm": 0.25,
    "step_mm": 0.15,
    "via": {"size_mm": 0.65, "drill_mm": 0.3},
    "assume_zone_refill": true
  },
  "working_scope": {
    "kind": "connectivity",
    "classes": ["Default"],
    "manufacturing_approved": false
  },
  "exclude_nets": ["/USB_N"]
}
```

The work plan reports `/USB_N` as blocked with "excluded by routing policy". Rework does not remove its copper, and a part with a pad on it is not a candidate for a move. A name that does not exist on the board is not an error. It simply matches nothing.
