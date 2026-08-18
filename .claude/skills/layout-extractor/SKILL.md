---
name: layout-extractor
description: >-
  Extract an existing layout hierarchy -- top-level `.gds`, sub-module
  `.gds` files and primitive cells -- into `physical_map.json`: every
  device's placement box, rotation/mirror and primitive cell, plus each
  net's traced routing geometry. Needs both a layout and a netlist. A
  device naming no process primitive is kept and warned about, never
  dropped. Measures drawn bodies and lets the layout's w/l win over the
  netlist's, keeping both, then verifies by redrawing the map and diffing
  it against the original. Reads only. Use on any raw GDS with no generator
  script, or whenever an opaque layout must become data.
---
# Layout Extractor: GDS hierarchy -> coordinates + traces

## When to use this

Whenever a layout exists only as a bare `.gds` with no generator script: it
cannot be reproduced or revised as-is, and extraction turns it into data that
can be. Also for a floorplan reference, to recover coordinates for a redraw,
or to find the dummy/fill devices a redraw must preserve.

This skill **reads**. It does not redraw, place, or route -- see "Handing
off" for what to do with the map.

## Required inputs

A `<design_dir>` holding, **all at its top level**:

- the golden `.sp` netlist,
- the top-level `.gds` and every sub-module `.gds` it instantiates,
- a `primitives/` folder, one GDS per placed device instance. Hash-based
  generators name these `{TYPE}_{HASH}_{Xx_Yy}.python.gds` -- the hash is
  what ties a cell back to the parameters it was built from. (ALIGN-style
  layouts read devices from the main GDS and need no `primitives/`.)

Both netlist and layout are mandatory; stop and ask for whatever is
missing. Without `primitives/` nothing binds, and the map comes out hollow
rather than wrong-looking. The GDS is checked *first*, so a design with no
layout is told exactly that (`No .gds layout found`).

**Netlist discovery is non-recursive.** A netlist under
`<design_dir>/netlist/` (with a copy in `user_inputs/`) is not found and
the run fails. The error names every `.sp` it saw one level down, so read
it as *"copy the right one to the design root"*, never as "this design has
no netlist." (Searching `netlist/` is still open, and would need to prefer
it over the `user_inputs/` verbatim backup.)

## Extract

From the project root (all commands here assume it):

```
python .claude/skills/layout-extractor/script/extract_physical_info.py <design_dir>
```

Several design dirs may be passed at once. Both layout formats are handled:
hash-based (`{TYPE}_{HASH}_{Xx_Yy}_{TIMESTAMP}` cells, needs `primitives/`)
and ALIGN-style (`{instance}_{model}` with SREF hierarchy).

**Process-independent.** No PDK is hardcoded here. Devices are recognized by
the foundry primitive-library marker in their model names, and every process
fact -- that marker, the routing-stack and via layer numbers, the resistor
marker and label datatypes, the annotation layer -- is read at import from
the project guideline, `.claude/reference/pdk_options.json`, whose
`"selected"` key names the active PDK. Switching process is an edit to that
file (or `PDK_OPTION=<name>` for one run), never to this skill. A PDK whose
`layers` block is absent or unverified is refused by name rather than
silently extracting nothing.

Writes `<design_dir>/physical_map.json` -- keys `design`, `netlist`,
`layout` (provenance: the exact files this map came from, and what
verification re-reads), `format`, `pairs`, plus:

- **`devices`** -- per netlist instance: `instance`, `kind`
  (`nfet`/`pfet`/`cap`/`res`), `params`, connected `nets`,
  `primitive_cell`, `is_dummy`, the placement box
  `x0_um`/`y0_um`/`x1_um`/`y1_um` with `width_um`/`height_um`, and
  orientation `rotation_deg` + `mirror` (mirrored about X, then rotated).
  Orientation is emitted for both formats, `null` only where no placement
  resolved.
- **`nets`** -- per logical net: `name`, `source` (`label`, `matched`,
  `named_cell`, or `unmatched`), `endpoints`
  (`instance`/`terminal`/`terminal_index`), and `segments` -- the routing
  geometry, each a `layer` `[num, dtype]` plus a `points_um` polygon. This
  is what makes per-net routed wire length computable downstream. An
  `unmatched` net with zero segments is a real result, not a crash.
- **`dummy_check`** -- `netlist_dummy_devices` (self-shorted,
  drain=gate=source) and `unclaimed_primitive_cells` (cells no netlist
  device claimed). Both are *candidates* for deliberate dummy/fill that a
  redraw must **preserve, not drop** (well-proximity and
  length-of-diffusion matching). **"Unclaimed" does not mean "dummy"** --
  it equally signals a parse miss on a real device. Check whether the cell
  is actually placed before treating it as fill.

**Always reconcile the reported device count against the netlist's own
device lines.** A parse miss is silent: the run exits 0 reporting the short
count.

## Every device must be a real PDK primitive

Extraction binds netlist devices to layout cells, so every device should name
a primitive its process actually provides. One naming no model is
**abstract** -- an idealized element with no cell behind it, such as a
resistor given only a value.

**No abstract device is fatal.** Extraction warns and continues, and every one
is **kept in the map**, never dropped: dropping leaves the count short *and*
leaves the device's real placed cell surfacing as an "unclaimed" primitive,
which reads as dummy fill and invites a redraw to delete a real device. How
far each can be taken depends on what its SPICE prefix alone settles:

| Prefix | Kind | Outcome |
|---|---|---|
| `R` | `res` | binds to the `RES_` cell; body measured from the layout, so `w`/`l` survive with no model at all |
| `C` | `cap` | binds to the `CAP_` cell; placement box, rotation and mirror all resolve |
| `M`, `X` | `unknown` | **kept but unbound** -- the prefix says transistor, not nfet vs pfet, and polarity is what a primitive is matched on |
| `L`,`D`,`Q`,`J` | `unknown` | kept, unbound -- no primitive type for these here |

Polarity is never guessed -- fabricated coordinates for a real device are
worse than an honest gap. An unresolved device gets `kind: "unknown"`, no
`primitive_cell` and null coordinates, and verification reports `INCOMPLETE`
naming it, which is correct: its position genuinely is unknown.

Whenever a device's measured geometry disagrees with what the netlist
declared -- or the netlist never declared it -- both are kept and the run
prints the substitution. The layout's measurement is the physical truth, and the netlist's own numbers are preserved, never overwritten. 

Treat that as **the layout's word**, not as agreement. A mismatch is a finding,
not a fix: this skill never edits the `.sp`. Report it and **ask the user
whether to update the netlist to the layout's measured values** -- only they
can say which of the two is the intended design.

**An unbound device shows up twice**, once as itself and once as an unclaimed
primitive cell -- the device count still reconciles against the netlist, and
the `unclaimed_primitive_cells` entry is that device's real cell, not fill.
This is the one case where "unclaimed" has a known cause; check the warning
before reading it as a dummy.

## Resistors

**Reconciliation -- the layout wins.** Each resistor's drawn body is
measured and compared with what the netlist declared. Where they disagree,
or the netlist declared no geometry, the effective `w`/`l` come from the
layout, which is the physical truth about what was drawn; the netlist's own
numbers are preserved, never overwritten. Per resistor record:

- **`params`** -- effective values, plus `r` when the netlist gave one;
- **`netlist_params`** / **`layout_params`** -- each source's own values,
  the measurement carrying `body_layer`, `n_body_polygons`, `n_strips`,
  `n_links`, `w_um`, `l_um`, `squares_est`;
- **`param_source`** (`layout`/`netlist`) and **`param_reconciliation`**
  (`match`, `mismatch`, `no_netlist_geometry`, `no_layout_measurement`).

Every layout-sourced substitution is printed, so none is silent.

Two deliberate non-goals. **The placement box is not used as the device's
dimensions** -- it is a placement *site* including routing margin, so
comparing declared values against it would flag nearly every device; the
body is measured from the resistor-marker geometry inside the primitive cell
instead. **Geometry is not converted to ohms** -- `squares_est` applies no
corner correction and is a lower bound, and sheet resistance is PDK data
this tool does not carry, so `r` always comes from the netlist.

## Verify

A map is a claim about a layout. Check it before building on it:

```
python .claude/skills/layout-extractor/script/redraw_from_map.py <design_dir>
```

This rebuilds a GDS from what the map recorded -- net segments replayed on
their recorded layers, and **every device as a real instance of its recorded
primitive cell**, placed at its recorded origin, rotation and mirror, plus
placement-box outlines and labels on marker layer 236/0. It writes
`<design>_redrawn_from_map.gds`, re-parses it to prove it is real GDS, then
diffs it against the layout named in the map's `layout` key (so it cannot
drift onto a `_validation` copy or an old redraw). Pure stdlib: **no
glayout, no gdsfactory**, so it runs wherever extraction does.

The map stores *which* primitive cell each device is, not that cell's
internals, so device geometry is read back from where each format keeps it:
`primitives/` for hash-based designs, the main GDS's own cell definitions
for ALIGN (which has no separate file per device). Devices are emitted as
SREFs rather than flattened, so the top cell's polygons stay exactly the
routing -- flattening would mix each device's own internal routing-layer
geometry into the diff and report it as wiring the layout does not have.

Two diffs run. **Routing** compares top-level polygons per layer by count,
area, and exact **multiset**, so a dropped polygon cannot be hidden by a
duplicated one. **Device geometry** flattens both sides through their
placement transforms and compares the same way -- which is what proves a
device landed on the right coordinates in the right orientation, since a
wrong rotation leaves every count identical and shows up only as a geometry
difference. Reading it:

- **`VERIFIED`** -- no routing missing, none invented, no device geometry
  invented, every device placed.
- **excluded zero-area polygons** -- normal. Generators emit degenerate
  polygons (identical vertices); they carry no geometry and extraction
  rightly drops them.
- **"in the layout but not owned by the map"** -- context, not a failure.
  Layouts place taps, guard rings and fill that are not netlist devices. Only
  geometry *in the redraw but not the layout* fails the run, because that
  means the map put a device where the layout has none.
- **devices sharing one placed cell** -- where two netlist devices sit inside
  a single physical cell, the map gives both the same `primitive_cell` and
  the same box. The cell is instantiated once and the sharing is reported by
  name; placing it per device would double its geometry.
- **`INCOMPLETE`, devices lacking coordinates** -- usually a missing
  `primitives/`. Nothing binds, so net matching fails too and routing goes
  unrecorded. Fix the input; don't reinterpret the map. (Devices then fall
  back to outline boxes, and the run says so on one line.)

The routing reference set spans top-level polygons **and** `NET_<name>` cell
contents, since ALIGN layouts pre-group each net's wiring into such a cell
and have no top-level routing. Both formats verify end to end -- routing and
device geometry both reproduced exactly, every device placed.

## Handing off

Extraction ends here; `physical_map.json` is the deliverable. Whoever
consumes it should redraw from the map rather than **adopting the raw GDS
as-is** -- treat the input as a reference to match, not as a finished
artifact to carry forward.

**`redraw_layout.py` (in this skill's `script/`) runs**. It takes design directory *paths*, the same contract as the two
scripts above:

```
python .claude/skills/layout-extractor/script/redraw_layout.py <design_dir> [...]
```

On the 11-device example it emits a real 176KB GDS -- 11 cells, 2727
polygons, every instance labelled -- in `<design_dir>/<design>_redrawn.gds`.
This section previously said it could not run at all, on three counts that
have since gone: `gdsfactory` (7.7.0) and `glayout` are both importable, the
process module comes from the PDK guideline rather than a literal import, and
the hardcoded example-folder root it used to resolve designs against -- a
directory that never existed -- has been replaced by the path argument above.
Still confirm the `*_redrawn.gds` exists before calling a redraw done.

Two rough edges that are cosmetic, not blocking: it reports
`netlist: (none found)` unless the netlist is named after its directory, and
gdsfactory warns about unnamed cells.

**That script does not belong to this skill** -- it authors a GDS, while
this skill only reads; prefer relocating it over growing it here. When it
does run it generates devices from the map's `l`/`w`/`nf` and centres each in
its recorded box, but **does not replay routing** (`draw_routing()` is
disabled, since regenerated footprints are not pixel-exact so old polygons
may miss their ports) and **draws resistors as placeholder rectangles**
(glayout's `resistor()` is a PMOS pseudo-resistor). Both need drawing by
hand, using the map's placement as the floorplan reference.

**If part of the layout matches no glayout cell**, don't block: propose the
closest subcircuit's placement/routing *style* as a template (e.g.
`diff_pair`'s common-centroid pattern for a structurally similar matched
pair) and flag the substitution explicitly rather than approximating it
silently.
