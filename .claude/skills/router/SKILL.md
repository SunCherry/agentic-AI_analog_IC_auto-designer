---
name: router
description:
  Grid-based A* router with PathFinder-style negotiated congestion for a
  placement `../placer/SKILL.md` already produced. Reads macro positions from
  `placement_pos.json` and net connectivity from `primitives/manifest.json`,
  routes every net on a real (x, y, layer) grid with via cost, per-layer H/V
  preference, a soft crosstalk proximity penalty and negotiated-congestion
  history, writes `routes.json` plus a real routed GDS, and finishes with a
  real Magic DRC pass. Invoked by `../../agents/layout-agent.md` once
  `../placer/SKILL.md` has produced a placement whose overlap/clearance gates
  and Step 4 checks are clean; its output then goes to `layout-fixer` for the
  DRC/LVS gates.
---
# Router: grid-based A* + negotiated congestion

Routes **any** analog layout, any circuit class, on **any** configured PDK.
Two files in, routed GDS out; every process fact (layer stack, pitch, widths,
spacings, via footprints) is queried from the active PDK at run time.

## When to use this

| Input (both required) | Carries |
|---|---|
| `<design_dir>/placement_pos.json` | each macro's position, rotation, placed box, world-space port coords |
| `<design_dir>/primitives/manifest.json` | **the netlist side**: `device_index` (which macro/pin on which net), `supply_rail_names`, `top_pins`, `min_metal_spacing_um`, per-macro `ports`/`keepout_um`, macro GDS paths |

Positions alone carry no connectivity; the manifest alone says nothing about
where anything sits. `../placer/SKILL.md` Steps 2 and 3 produce them
(`<design_dir>` is `<design>/layout`), but any producer that writes these two
files honestly can drive this router.

**Route a placement that already passed its own gates** (placer Step 3's
overlap gate, Step 4's grid + DRC checks). **Nothing here legalizes a
placement** -- Step 4a (`generate_grid.py`) only *reports* legality, never
moves a macro, never writes `placement_pos.json`. Fix overlap back in Step 3
(more `--iters`, another `--seed`, higher `--w-density`) or by hand.

This is **global+detailed routing at macro-terminal granularity** -- real,
DRC-plausible paths between macros. Whether that is *port-exact* is not a
blanket yes or no, and the summary's **landing points** line is what settles
it per design: a pin that landed on a real glayout port is already on real
drawn metal, and only `pin_point()` box-edge fallbacks stop short of it (by
`keepout_margin`). So a run with zero fallbacks needs no port-exact pass at
all; a run with N fallbacks needs one for **those N nets**, via
`../routing-handler/SKILL.md`, before LVS sign-off. Read the number rather
than assuming either extreme.

## Run it
```
python script/route_nets.py <design_dir> [--layers met2,met3,met4,met5]
    [--track-multiplier 1] [--via-cost 10] [--sensitive-via-multiplier 3]
    [--wrong-direction-penalty 1.5] [--proximity-radius 2]
    [--proximity-weight 0.5] [--history-step 1.0] [--ripup-iters 5]
    [--over-macro-cost 0.4] [--sensitive-over-macro-multiplier 3]
    [--shared-cell-penalty 400] [--via-pad-penalty 40] [--no-feedthrough]
    [--out <design_dir>/routes.json] [--gds <design_dir>/routed.gds]
    [--summary-out <design_dir>/routing_summary.txt] [--no-gds]
```
Every value shown is that flag's real default; all three outputs default
beside `placement_pos.json`. `--no-gds` skips the GDS render (the slow part);
use it for cost sweeps, then render once for the setting you keep.

- **`--layers` names glayout glayers, not GDS numbers**, and must be given
  **low-to-high up the stack** -- vias are priced between consecutive entries
  and H/V alternates by index, so out-of-order entries would route on a grid
  that doesn't exist. The list is validated against the PDK's own metal stack
  before anything runs (`validate_routing_layers()`): unknown, repeated,
  out-of-order or fewer-than-two layers stop the run with the stack printed.
  The default suits any deep-enough stack; on a shallower process this is
  what tells you so, instead of a traceback from inside glayout.
- **A glayer name is NOT the process's own layer name, and in sky130 they are
  offset by one.** glayout `met2` is physically met1, `met3` is met2, `met4`
  is met3, `met5` is met4 -- and glayout `met1` is `li1`, local interconnect,
  not metal at all. Everything this skill prints (`routing_summary.txt`,
  `routes.json`, the obstacle map) is in **glayer** names; everything Magic
  DRC reports is in **process** names. So a `met2.2` "Metal2 spacing"
  violation is about what the summary calls met3. Resolve the name through
  the PDK (`pdk.glayers[g]`, `pdk.get_glayer(g)`) before matching a DRC rule
  to a routed layer -- assuming the two namespaces agree mis-aims the fix by
  exactly one layer.
- **The process comes from `.claude/reference/pdk_options.json`, not a flag.**
  All three scripts resolve it through `../../reference/pdk_config.py` (glayout
  `MappedPDK` via `glayout_module`, `PDK_ROOT`/`PDK`, `run_drc.py`'s magicrc),
  exactly as `generate_primitives.py` does upstream -- so both halves of the
  placer -> router hand-off draw for the same process. Retargeting is the
  one-word edit to `selected`. Only the active PDK has been run end-to-end.
- **Do not remove `gf.CONF.n_threads = 1`** (set before importing glayout).
  Geometry generation is not thread-safe at gdsfactory's default 8 --
  byte-different GDS and different DRC counts from identical invocations.

## The six cost terms
All knobs are echoed into `routing_summary.txt`'s reproducibility section, so
a run's costs are recoverable from its own summary.

| Term | Flags | What it costs |
|---|---|---|
| Via | `--via-cost`, `--sensitive-via-multiplier` | **Not flat per layer change** -- scales that layer *pair's* real `via_stack` footprint in um. Via pads are strongly non-uniform up a stack; a flat cost made every transition look equally attractive |
| Layer preference | `--wrong-direction-penalty` | Layers alternate H/V by index in `--layers` (first H, second V, ...); moving against the preferred axis costs this multiplier |
| Feed-through | `--over-macro-cost`, `--sensitive-over-macro-multiplier` | Per step crossing a macro on a layer that macro leaves empty. Legal, but it couples into the device below, so it is priced rather than free. `--no-feedthrough` forbids it outright |
| Present congestion | `--shared-cell-penalty`, `--via-pad-penalty` | A cell another net already holds THIS pass -- `metal` (a real SHORT) and `halo` (its min-spacing ring). The other half of PathFinder: without it `history` reacts a pass late and shorts oscillate forever |
| Proximity | `--proximity-radius`, `--proximity-weight` | Once a net routes, nearby cells cost extra for every OTHER net -- crosstalk control beyond the DRC-minimum spacing already in the pitch |
| Congestion history | `--history-step`, `--ripup-iters` | Real PathFinder (Ebeling/McMurchie): route all nets at capacity 1, bump history on shared cells, re-route. Reports `converged`/`overflow_cells`/`failed_nets`; on non-convergence reports the best result found, never a false success |

**Sensitive nets** are auto-detected: any net touching a pin `device_index`
marks as a transistor gate (same rule as `../../agents/layout-agent.md`). The
multipliers are a *bias, not a guarantee* -- a long net still takes vias when
the detour costs more. The summary reports the split; if you need a hard
guarantee, this is the wrong lever.

## Honest scope (read before trusting a result)

**Endpoints.** Each pin prefers a REAL glayout port near its macro's bbox edge
(`world_port_map()`, from `generate_primitives.py`'s `near_edge_ports()`),
else falls back to `pin_point()`'s macro boundary point facing the net's other
terminals -- the same macro-granularity abstraction the placer's HPWL uses.
**Only `pin_point()` fallbacks stop short of real drawn metal** (by
`keepout_margin`); touching metal was never claimed for that approximation.
Supporting machinery, each added to close a real DRC-confirmed defect:
`filter_landable()` (rejects a port narrower than the smallest via that could
land on it), `to_grid_outward()` (a padded point can't snap back inside the
keepout), `off_grid_layer_vias` (explicit via down to a port below the first
routing layer), `landing_stubs` (ONE padded rectangle to the pin's exact
coordinate -- two thin legs leave a DRC sliver), `stagger_pin_point()`
(separates nets falling back on the same macro edge), `corridor_to_edge()`
(strip from an inside-the-box port to the edge; it is *shared* grid state, so
another net can route through it -- verify with DRC).

**Obstacles are per LAYER** (`build_layer_obstacles()`), rasterized from each
macro's own GDS: a layer the macro fills is blocked, one it leaves empty is
open, so a net may cross above or below a device on a layer that device
doesn't use. Padded by `max(keepout_margin, min_separation) + wire_width/2` --
a wire has width, so a "free lane" on the box edge line would half-overlap the
macro. Blocking
every layer at once (`--no-feedthrough`) is self-defeating: with identical
obstacles everywhere a via can never shorten a path, so upper layers go
unused.

**Grid pitch** is the smallest legal on the COARSEST routing layer
(`base_track_pitch()`) -- taking it from the finest yields tracks illegal on
the coarse layers. **Don't coarsen much past `--track-multiplier 1`**: a macro
gap narrower than one cell makes nets genuinely unroutable. If routing won't
converge with everything else clean, raise `--w-density` in
`anneal_placement.py` -- that clearance guarantee is load-bearing for
routability.

**Keepout margin** = `KEEPOUT_MARGIN_MULT * min_metal_spacing_um`. Not
cosmetic: with zero margin, a wire landing DRC-close to a macro's tap ring
made extraction merge a supply net into the device-substrate plane -- a real
short caught by a real LVS run. `2.0` closed off most nets' channels on a real
placement; **`1.0` is the smallest confirmed to still route cleanly**. Too
tight? Raise `--w-density`, not this.

**One grid state, committed after every action** (`GridState`, each LEG
committed as it completes, so state is never stale mid-net). `metal` and
`halo` are tracked separately because a short and a spacing violation differ
in kind. Both are **costs, never hard blocks** -- a block is an irrevocable
first-come claim, and with fixed net order one early net's large via pad
walled later nets out of corridors entirely.

**Conflicts are reported at the precision they're known**, and only exact ones
gate. **`path_on_path_shorts` must be 0** (two nets sharing a path cell is a
real short). `conservative_pad_cells`/`spacing_cells` are whole-cell
rasterizations of sub-um geometry -- upper bounds, reported and left to DRC;
gating on them fails designs whose real geometry is correct. Stub-on-stub
overlaps present *before* routing are excluded from per-pass counts (no ripup
can move them) and raised once as a port-spacing warning.

**Geometry details that cause confusing DRC errors.** Wire bends emit an
explicit corner square -- runs are padded perpendicular only, so an L's outer
corner would otherwise notch by half the wire width, which Magic reports as a
*width* error on thick layers and a *spacing* error on thin ones. Wire width
per layer is `min(WIRE_WIDTH_MARGIN=1.5 x min_width, pitch - min_separation -
slack)`: the margin is there because exact-minimum wires trip Magic's strict
`< min_width` check after GDS round-trip, and **the pitch cap routinely
binds** on the coarse upper layers, so those come out narrower than 1.5x --
read the real per-layer widths off the summary's "by layer" section, don't
compute them. If a layer can't fit even `min_width` at the shared pitch the
run stops and names it.

### KNOWN OPEN GAP -- not fixed
Magic's hierarchical DRC derives an "obstructed" tile class for a layer
wherever a sibling cell reference's *bounding box* overlaps it, regardless of
what that sibling draws -- spacing rules then fire against the bbox, not the
polygons. `off_grid_layer_vias`/`landing_stubs` drop a `via_stack()` (a
referenced Component, not flattened) at a primitive's exact port, exactly the
shape that triggers this when the port sits near the primitive's own metal.
Confirmed on a real design: spacing violations with no genuine polygon overlap
at `gdstk` level. Fixing it means drawing router-inserted vias as flattened
polygons or relocating them to a clear center -- both need a live Magic DRC
pass before any "fixed" claim. The mechanism, not any count, is the finding.

## Output
- **`routes.json`** -- per net, `{"kind":"wire","layer":,"points_um":[...]}` /
  `{"kind":"via","x_um":,"y_um":,"from_layer":,"to_layer":}` segments, plus
  `status`, `sensitive_nets`, `total_wire_um`, `total_vias`, `sensitive_vias`.
- **`routed.gds`** -- placed/rotated macros + wire polygons + real `via_stack`
  geometry + **a net-name label per routed net** on its own `<layer>_label`
  (without these an LVS `port makeall` promotes zero top-level ports).
  Single-macro nets get a label on a real near-edge port (`single_pin_nets`).
  Macro **instance** names go on a deliberately extraction-inert raw layer
  (`INSTANCE_LABEL_LAYER`) so they can never be promoted as ports and corrupt
  net identity -- **do not "tidy" that onto a real `*_label` glayer.** Open
  with the PDK's layer-properties file (`pdk_options.json`'s
  `tools.klayout_lyp`); bare, a dense macro reads as a solid block.
- **`routing_summary.txt`** -- written **every run, PASS or FAIL**; a summary
  that only appears on success is the one you can't diff against the run that
  regressed. `routes.json` has the geometry but unaggregated and unranked;
  this is what answers "is this routing any good, and if not, where".

Sections: quality gates (nets routed, **path-on-path shorts**, congestion
PASS/FAIL, failed nets, conservative cells) · wire length (total, vias,
mean/max per net, **feed-through um and %**) · by layer (direction, width,
wirelength share, fraction blocked) · vias by transition (with real pad size)
· 10 worst nets · macro obstacle map · **landing points** · routing grid ·
reproducibility. Three earn their place: **path-on-path shorts** is the gate
(not total congestion -- DRC is the authority); **feed-through** is geometric,
so ~0 on a design with no open layer is information, not a bug; **landing
points** splits real ports from `pin_point()` fallbacks, and a high fallback
count is the best predictor LVS will find unconnected nets.

Two scope limits: multi-pin nets route as a **chain** (each pin A*s to the
nearest cell already in the net's tree -- not a Steiner solver), and **supply
rails route as ordinary nets**, one A* trunk per rail at macro granularity --
not a ring/strap power grid.

**Re-measure rather than trusting any number quoted here.** Every figure
describes one design on one process at one moment.

## Last step -- DRC with Magic
```
python script/run_drc.py <design_dir>/routed.gds
    [--top <GDS filename stem>] [--work-dir <gds's dir>/drc_work]
```
`route_nets.py`'s own "Congestion check: PASS" verifies only capacity-1 cell
sharing and macro-box avoidance -- **not** a DRC guarantee. `run_drc.py`
reuses `../../reference/environment.md`'s validated Magic Tcl pattern verbatim
(`drc on` + `expand` before `drc check` + `drc catchup`, `DRC_TOTAL`
cross-checked against `drc listall why`), the same pattern
`../../agents/layout-fixer.md` uses. If that pattern changes, change it there.

**`--top` defaults to the GDS filename stem** -- every routed GDS here has top
cell `routed`, so a copy saved under another name needs `--top routed`.
Magic's `load` silently CREATES a missing cell and DRCs an empty one, a false
PASS; the script checks up front and refuses instead.

**Outputs, besides the printed verdict**: `<work-dir>/drc.log` (the raw Magic
log) and `<work-dir>/drc_violations.json` -- **every** violation box per rule,
`{rule: [[llx,lly,urx,ury], ...]}`, already converted from Magic internal
units to um. The terminal prints only the first few per rule; take
coordinates from the JSON rather than hand-writing Tcl or grepping the log.
That file is what `../../agents/layout-fixer.md` attributes violations to
nets with.

**`--work-dir` defaults to `<gds's dir>/drc_work`, which two runs share.**
Both `routed.gds` and `placement_visualization.gds` live in the same
directory, so the categorization run below **overwrites the log and
`drc_violations.json` of the run you are categorizing**. Give each run its
own `--work-dir` whenever you intend to keep both.

**Reading the verdict.** `drc listall why` on the *expanded top cell* carries
it. Errors counted only in `via_stack` SUBCELLS are out-of-context (their
completing geometry lives in the parent; flattening settles it), but the
per-cell counts stay printed because they catch real child-cell violations
`DRC_TOTAL` alone reports as 0. Read "clean" as **"no violation survives
expansion"**, not "nothing is reported anywhere".

> **A congestion PASS is not a DRC PASS.** Last measured on this project's
> reference placement, routing converged cleanly -- every net routed, 0
> path-on-path shorts -- and DRC still **FAILED** with several metal-spacing
> violations in the expanded top cell. The same DRC on that design's
> pre-routing GDS was clean, so those were **router-introduced, not
> inherited**, and not the `via_stack` artifact. Expect this on your design;
> never hand off a dirty GDS as clean.

**Categorizing what's left.** Run the SAME DRC on the pre-routing
`placement_visualization.gds` (macros only, no wires) -- **with its own
`--work-dir`**, per the warning above, or it clobbers the routed run's
artifacts. A matching violation set is inherited from placement/generation,
not routing. This is how a real
inherited gap (well/latch-up rules from primitives generated without substrate
taps) was separated from a router-caused one. Routing adds real geometry, so
it is not automatically innocent -- confirm, don't assume, and report
violations honestly categorized either way.

## Verification discipline
Never trust "PASS" from the congestion check alone -- development caught three
real bugs an "all nets routed" summary did not surface (a tuple-shape mismatch
that silently defeated ALL obstacle avoidance, the edge-line free-lane bug,
bare-minimum wire-width violations). Independently re-open `routes.json` (or
`routed.gds` via `gdstk`) and check (a) no wire segment's bbox overlaps a
macro outside its own net, (b) no two DIFFERENT nets' segments overlap on the
SAME layer (different-layer crossings at one (x,y) are expected) -- **and run
the DRC step**, which is stronger than either check.

## Optional -- label individual device terminals
```
python script/label_device_ports.py <design_dir> [--manifest ...]
    [--placement ...] [--routed ...] [--out ...] [--no-dummy]
```
`render_gds()`'s labels are net- and macro-granularity, so a composite macro
reads as one name with nothing naming the devices inside it. This adds
per-terminal labels (`<device>_drain`, `<device>_gate`, ...). It regenerates
each macro's LIVE Component in-process (a glayout Component's `.ports` do not
survive a GDS round-trip), so **`--no-dummy` must match whatever
`generate_primitives.py` built the design with** -- mismatched, every label
lands at a wrong coordinate and nothing looks wrong.

**Visualization only, but NOT on the inert layer**: unlike `route_nets.py`'s
instance labels these land on a real `*_label` glayer, the same layer class
LVS pin promotion reads. Run it on a copy if you are about to extract.
Overwrites `routed.gds` unless `--out`.

## Files
`SKILL.md` · `script/route_nets.py` (the router) ·
`script/label_device_ports.py` (optional labeling) · `script/run_drc.py` (DRC).
External: `../../reference/pdk_config.py` + `pdk_options.json` (the active
process -- the only place a process name or path is written down) ·
`../../reference/environment.md` (the Magic DRC Tcl pattern) ·
`../placer/SKILL.md` (upstream input) ·
`../../reference/generate_grid.py` (reused `metal_glayers()`) ·
`../../agents/layout-agent.md` (sensitive-net language; port-exact routing) ·
`../routing-handler/SKILL.md` (the port-exact decision tree).
