---
name: placer
description:
  Generate an empty-canvas starting layout for any analog circuit from its
  frozen netlist -- create the design's `layout/` folder, flatten the netlist
  into one ERC-clean netlist proven equivalent to the original, build a real
  glayout module for each pattern `circuit_decomposition.yaml` names (current
  mirror, differential pair, ...) plus a standalone primitive for every
  ungrouped device, then place everything with simulated annealing (HPWL +
  overlap + clearance + area), grid-check, DRC-check and visualize. Use when a
  design has a frozen netlist and no layout at all -- no GDS, no generator
  script, no reference floorplan to adapt.
---
# Placer: flatten -> modules + primitives -> SA placement -> DRC

**Empty canvas only.** If a reference layout or generator script exists, adapt
it instead. Produces a checked floorplan -- one flat netlist, real geometry
per module and device, real macro positions, a viewable GDS, a DRC verdict. It
does not route (Step 3's wire term is a coarse routability proxy) and never
touches the netlist.

## Inputs
| Input | Required | Use |
|---|---|---|
| Frozen netlist (`.sp`) | **yes** | terminals and `w`/`l`/`nf`/`m` per device. Decomposed or flat -- Step 1 flattens either |
| `<design_dir>/circuit_decomposition.yaml` | optional | **the only source of pattern grouping**; found in the design dir, the netlist's dir or its parent, `--decomposition PATH` overrides |

**No topology detection happens here.** The yaml is user-confirmed and gives
each device a `role` (the mirror reference is read, not guessed). Without it
every device becomes a standalone primitive (as `--per-device` does) and the
run says so -- a real floorplan with no mirror/pair matching.

## Step 0 -- layout folder
```
mkdir -p <design_dir>/layout/primitives <design_dir>/layout/modules
```
- `layout/` -- the flat netlist, then `placement_pos.json`,
  `placing_summary.txt`, `placement_visualization.gds`. Four artifacts, each
  consumed by a later step; every report is terminal output.
- `layout/modules/` -- one GDS per pattern-composed macro (internal matching
  is the point).
- `layout/primitives/` -- one GDS per standalone device, plus
  `manifest.json`/`manifest.md`/`lvs_compare.sp`.

## Step 1 -- flatten, ERC, prove equivalent
```
python .claude/skills/placer/script/flatten_netlist.py <netlist.sp>
    [--layout-dir <design_dir>/layout] [--out NAME] [--top-subckt NAME]
    [--no-equivalence] [--json report.json]
```
Writes `<layout_dir>/<stem>_flat.sp`, **the only netlist Steps 2+ read**, then
gates it: exit `0` clean, `2` ERC warnings or skipped equivalence, `1` ERC
hard errors or an equivalence mismatch. **On `1`, do not run Step 2.**
`--layout-dir` defaults to `<design_dir>/layout` (`netlist/`, `sizing/`,
`device_shaping/` are treated as stage folders).

1. **Flatten** -- `.subckt` instances expanded one level, internal nets mapped
   to top-level names. Those blocks are usually `.include`d, which a
   device-line parser never opens: without this every device inside a subckt
   is dropped (measured: 7 parsed where the circuit has 11). An already-flat
   netlist is copied verbatim. Device names survive unless a real collision
   forces an `<instance>.<device>` prefix, so the yaml's `ref`s still match.
   Nested subckts are reported (`nested_unexpanded`), left alone, and keep
   local net names. Flattening assigns no topology.
2. **ERC** on the **flat** file, not the original -- flattening is where names
   collide, so checking upstream of it checks the wrong file.
3. **Equivalence** -- netgen LVS, flat vs original (copied to `.spice`, which
   netgen requires, relative `.include`s rewritten absolute). Not optional:
   nothing else would notice a wrong pin mapping, and every later step would
   be built on the wrong circuit. `--no-equivalence`, missing netgen or a
   missing PDK setup all report `skipped` (exit 2), never a pass.

**Fixing ERC findings.** The golden netlist is frozen; the flat file is a
derived copy and equivalence bounds a legal fix. Repairable here:
element-prefix mismatch (`X`-call vs `R`-primitive on a resistor with both
cards), `w`/`l` in SI metres, unsubstituted placeholders, a duplicate name
flattening produced. **Escalate, don't patch**, anything touching
connectivity, device count or sizing (real opens, port-count mismatch, a
device over its model's widest bin): those are schematic defects, and fixing
them in the flat copy would break equivalence with the netlist LVS runs
against.

**Output**: `<stem>_flat.sp` plus `equivalence/netgen_equivalence.out`. The
ERC findings and the verdict are terminal output, not files -- read them
there; `--json PATH` writes the whole summary machine-readably if something
needs to consume it.

## Step 2 -- generate modules and primitives
```
python .claude/skills/placer/script/generate_primitives.py <layout>/<stem>_flat.sp \
    --out-dir <design_dir>/layout/primitives \
    --modules-dir <design_dir>/layout/modules \
    [--decomposition <design_dir>/circuit_decomposition.yaml]
    [--no-dummy] [--per-device] [--split-mirror-legs] [--top-subckt NAME]
    [--no-subckt-expand]
```
`--no-subckt-expand` turns off the re-flatten. It changes device and net names
only -- pattern grouping still comes from `--decomposition` either way -- so it
matters when a name the yaml `ref`s must survive verbatim.
**Pass Step 1's flat netlist by path** (re-flattening it is a no-op). Handed a
directory, `find_netlist()` tries only `<dir>/<dirname>_final.sp` then
`<dir>/<dirname>.sp` -- it raises, or silently picks an unsized template.
Preview grouping first with
`python .claude/skills/placer/script/decomposition_patterns.py <design_dir>/circuit_decomposition.yaml`.

| Pattern | Built as | Folder |
|---|---|---|
| `current_mirror` | **one** `src/cells/blocks/current_mirror.py::current_mirror(pdk, mirror_ratio=[r1, ...])` covering reference **and every leg** -- inside one cell the legs share gate bias and bulk by construction, not via the router. `--split-mirror-legs` restores per-leg macros | `modules/` |
| `differential_pair` | one `src/cells/blocks/diff_pair.py::diff_pair(...)` | `modules/` |
| any other pattern, or a mirror/pair whose yaml device COUNT the generator can't take | **not auto-composed**: each device falls through to a standalone primitive, the pattern is listed in `manual_composition` with a warning. Drawn but unmatched -- a placeholder, not a finished module | `primitives/` |
| every leftover device (unmatched, all caps/res) | standalone primitive via `src/cells/primitives/fet.py`'s `nmos`/`pmos`, `mimcap.py`'s `mimcap`, or `build_resistor()` (real poly + the PDK's resistor marker, contacted to met1). A **BJT gets no primitive**: `generation: "manual"`, null `w`/`h`/`gds`, excluded from Step 3 (`excluded_no_geometry`) | `primitives/` |

A macro composed by hand later must be **folded into the manifest** or Step 3
ignores it: its own `macros` entry (`name`/`devices`/`w`/`h`/`gds` + real
`ports`), the standalone entries for those devices removed, each device's
`macro` field in `device_index` repointed at it.

### Traps, each measured
- **Do not remove `gf.CONF.n_threads = 1`.** glayout geometry construction is
  not thread-safe at the default 8: identical input gave byte-different GDS,
  one clean, one with 24 extra DRC violations. `PYTHONHASHSEED` does not fix
  it, and a single-cell build is not a valid test of it.
- **`src/cells/`, not `cells/`** -- `CELLS_DIR` pointing at a nonexistent
  `<repo>/cells` killed every run at `from primitives.fet import nmos`. Check
  that path first if the import ever fails again.
- **Total width is `w * m`; `nf` SPLITS `w` into fingers and adds none.**
  glayout is the other way round (its `width` is the PER-FINGER extent and
  `fingers` adds width), so a device is drawn `width=w/nf, fingers=nf*m`,
  giving `(w/nf) * nf * m = w * m`. All three are honored (`m` ->
  `multipliers`; a pair splits `nf * m` across its halves, which its
  common-centroid cell then places twice; a mirror draws its reference at
  `nf * m` fingers). `lvs_compare.sp` folds MOS to `w * m`, so cell and
  netlist must agree or LVS cannot match. **Do not "restore" `w * nf * m`**
  -- that reading was tested and refuted against this PDK's own models
  (`generate_primitives.py::gen_current_mirror`'s `total_w()` carries the
  three measured currents; BSIM4 derives `Weff = W/NF`, so `w` is total).
- **Mirror ratio is integer N:1 only**: `round(total_w(leg)/total_w(ref))`,
  clamped >=1. A leg more than 5% off an integer multiple of the reference
  is **not drawable in the cell at its netlist width**, so it is EXCLUDED
  from the composed macro and falls through to a standalone primitive at its
  true width, with a warning naming it and a `manual_composition` entry. If
  no leg is expressible, the whole mirror falls through and every device --
  reference included -- is drawn standalone. Correct widths; the in-cell
  matching for those legs is lost, which the cell could not have provided
  anyway. To keep the matching, make the reference the design's UNIT device
  (then every leg is an integer multiple of it) or compose the mirror by
  hand. **This does not round and draw the wrong width** -- doing so was a
  measured bug: `example/test_miller_ota`'s `cm_nbias` (ref 236um, legs
  102.6um=0.435x and 344.96um=1.462x) emitted `mirror_ratio=[1, 1]` and drew
  all three legs at 236um, reaching LVS as a width mismatch on two devices.
- **A diff pair is drawn twice from half `a`'s geometry**; disagreeing halves
  warn and `b` is drawn at `a`'s size (fix the netlist -- asymmetry here is
  input offset). Odd `nf * m` rounds up, also warned.
- **A 3-terminal resistor subckt call is dropped** (the parser matches a 2-net
  `R`-line, so it becomes `kind="other"` with no primitive and the floorplan
  will not LVS). Check resistor count against the manifest; workaround is a
  2-net `R`-line in a derived copy, re-run through Step 1.
- **The process comes from `.claude/reference/pdk_options.json`**, nothing is
  hardcoded: the glayout PDK object is resolved by that file's
  `glayout_module`, the metal stack from the PDK's own `valid_glayers`, and
  the resistor-recognition marker from the poly glayer's GDS number paired
  with `resistor_marker_datatype`. Retargeting is the one-word edit to
  `selected` there. Two honest limits: a PDK with no
  `resistor_marker_datatype` gets **no resistor primitive** (a marker on a
  guessed layer extracts as wiring, so it refuses and says so), and only the
  active PDK has been run end-to-end -- the sky130 numbers below are measured,
  another process's are not.
- `import glayout` resolves to the external editable install at
  `~/Documents/work/external_ai/gLayout`; the script's `REPO/"src"` insert
  does not shadow it, whatever its comment says.

**Warnings to read, not just the exit code**: an unsigned circuit read
(`confirmed_by_user` not true); an empty `patterns` block (judgment steps have
not run -- never *none found*); a pattern naming a device the netlist lacks; a
device in neither `patterns` nor `unmatched_devices`; a mirror with no
`role: reference`; a device claimed by two macros, which would draw it twice.

**`--per-device`** ignores patterns -- every device its own primitive, no
modules. Coarser, not a superset: more macros means more nets per boundary, so
routing needs more room (one design went 7 -> 11 macros and needed
`--w-density 200`, not 60).

**Output**, besides the GDS: `manifest.json` -- per macro name/kind/devices/
`w`/`h`/gds/generation/`constraints`/`glayout_call`, real near-edge `ports`
(net -> landing points, **local coords relative to the bbox center**, with
`bbox_center_offset`), `keepout_um` on a MiM cap; plus `primitives_dir`/
`modules_dir`, `device_index`, `supply_rail_names`, `top_pins` (the top
`.subckt`'s pins -- otherwise Magic's `port makeall` promotes internal labels
and LVS fails pin matching), `manual_composition`, `min_metal_spacing_um`.
Modules carry real ports on every routing-relevant net and standalone fets
ride `*_bo_*` stubs; only a portless net falls back to the router's box-edge
approximation. `manifest.md` is the same list, readable. **`lvs_compare.sp` is
what LVS compares against, not the golden netlist**: `w` folded to `w * m`
with `nf`/`m` set to 1, because the PDK's netgen setup deletes `nf`/`mult`
while Magic merges a folded device back to total width -- otherwise every
`nf > 1` device reports a width delta of exactly `nf`, masking real errors.

## Step 3 -- anneal the placement
```
python .claude/skills/placer/script/anneal_placement.py \
    <design_dir>/layout/primitives/manifest.json --iters <N> \
    [--t0 50.0] [--accept-threshold 0.02] [--stage-iters N]
    [--w-wire 1.0] [--w-ov 50.0] [--w-sym 10.0] [--w-density 5.0] [--w-area 0.01]
    [--sym-groups groups.json] [--min-metal-spacing UM] [--single-phase]
    [--joint-refine-iters 0] [--seed 1] [--no-render]
    [--out ...] [--summary-out ...]
```
**There is no `--physical-map-out`** -- it was removed when the placed box moved
into `placement_pos.json` (see "Output" below). Passing it is an
`unrecognized arguments` error, not a no-op.
**`--iters` is required and has no default -- ASK THE USER for the maximum
iteration count before running this step**, offering 20000 as a starting
suggestion and saying what it buys (it is an upper bound, not a runtime: the
run stops itself at plateau). Never pick the budget silently; the old default
of 20 quietly produced a barely-perturbed random placement that FAILs overlap,
and the script now refuses to run without the flag rather than repeat that.

Other values shown are real defaults; output paths need no flags (they default
to the manifest's **grandparent**, i.e. `layout/`). Cost = HPWL + overlap +
symmetry + density clearance + area, exponential cooling, displace 70% / swap
15% / rotate 15%:

- **HPWL** from `device_index` at **macro granularity** -- coarse on purpose,
  port-exact routing comes later. Supply rails excluded via
  `supply_rail_names`.
- **Density**: `min_distance = 10 * min_metal_spacing * num_nets`, the denser
  macro's requirement per pair. **Soft** -- PASS/FAIL, no nonzero exit.
- **Area**: `--w-area 0.01` is deliberate; a bbox is 10^4 um^2, so `1.0` would
  swamp HPWL.
- **Rotate** swaps placed w/h, tracked as a bool (180/270 give the same box);
  Step 5 applies real rotation to real geometry.

A small budget (a few dozen moves) does not converge and FAILs overlap; large
values are safe because stopping is by acceptance ratio (a stage of
`max(50, 20*movable)` falling below `--accept-threshold`), not move count. A
FAIL means more iterations, different weights or manual legalization -- never
"close enough". `--seed` is what to vary when it won't converge.

**Two-phase is the default**: modules place first as the skeleton, single
devices place around them (charged full cost, unable to move them). One
measurement (8 macros, `--iters 20000 --w-density 60 --w-area 0.0005`): 35%
smaller bbox at the same cost/HPWL. `--single-phase` restores joint annealing;
`--joint-refine-iters N` adds an all-movable pass, off because it can undo the
hierarchy. `--sym-groups` (`[macroA, macroB, axis_x]` triples) is **not**
auto-inferred -- a circuit-understanding call; without it symmetry is inactive.

**Output** in `layout/`: `placement_pos.json` and
`placement_visualization.gds`. Each `positions` entry carries
`{x, y, rotated, w, h, num_nets, min_distance_um, ports}` -- `w`/`h` already
post-rotation, `ports` in world coords under Step 5's centre convention, so a
router lands on the metal actually drawn -- **plus the placed box
`x0_um`/`y0_um`/`x1_um`/`y1_um`, which is what Step 4a's grid check reads.**
That box used to be a second file (`physical_map.json`) holding the same
numbers; it is merged in here, since two copies could only drift.
`placing_summary.txt` is written **every run, PASS or FAIL**: gates with
residuals, HPWL stats, bbox/utilization, cost breakdown by % share, 10 worst
nets, 8 tightest pairs, per-tier placement, seed/weights. Two sections turn a
FAIL into an action -- tightest pairs says by how much (-0.03um is a couple of
iterations away, -4um is a floorplan problem), and the % shares say what is
actually being optimized (raising `--w-sym` at 0.0% share changes nothing).
Utilization is not the clearance penalty: low utilization is fine, that
penalty deliberately buys routing channels.

Measured on `designs/test_miller_ota` (3 modules + 4 primitives, weights
above): overlap PASS, clearance PASS, 251x103um bbox at 24.8% utilization,
grid check and Magic DRC clean on those artifacts.

## Step 4 -- grid legality check (4a), then DRC (4b)

### 4a -- check legality on the routing grid
Only if Step 3 reported nonzero overlap (it exits nonzero then).
```
python .claude/reference/generate_grid.py <design_dir>/layout --no-save
```
**This REPORTS; it does not legalize.** It reads each macro's placed box
(`x0_um`/`y0_um`/`x1_um`/`y1_um`) straight out of `placement_pos.json`, builds
the routing grid at the PDK's finest layer pitch, and prints free/blocked/used
cell counts plus an ASCII preview. It contains no nudge or move step, and it
**never writes `placement_pos.json`** -- with `--no-save` it writes nothing at
all, and without it the only new file is `placement_grid.json`. No macro moves
because you ran it. Treat its counts as the legality verdict, nothing more.

So a nonzero overlap is resolved one of two ways, both yours:
- **Re-run Step 3** with more `--iters`, a different `--seed`, or higher
  `--w-density` -- the normal fix, and the only one that re-optimizes.
- **Move the macro by hand** -- edit its `x`/`y` *and* its `x0_um`..`y1_um` box
  in `placement_pos.json` (they must stay consistent; Step 5 and the router
  read different ones), then re-render with Step 5 and re-check. Reserve this
  for a single stubborn macro that annealing keeps re-placing badly.

Either way **re-render before re-checking** -- the grid reads positions, DRC
reads the GDS, and a stale GDS will happily report the old placement's result.

### 4b -- DRC on the rendered GDS
```
python .claude/skills/router/script/run_drc.py <design_dir>/layout/placement_visualization.gds \
    --work-dir <design_dir>/layout/drc_work_placement
```
**Pass `--work-dir`.** It defaults to `<gds's dir>/drc_work`, which every
later DRC on a GDS in `layout/` shares -- so the router's own DRC pass would
overwrite this run's `drc.log`/`drc_violations.json`. Keeping this result
under its own name is what makes the pre-routing-vs-routed comparison
(`../router/SKILL.md`'s "Categorizing what's left") possible later.

`DRC_TOTAL == 0` with no `RULE:` lines -- done. Otherwise **classify before
reacting**:
- **Placement-fixable** (spacing *between* macros): re-run Step 3 with higher
  `--w-density`, another `--seed`, more `--iters`; re-render, re-check.
  Precedent: `--w-density` 5 -> 30 fixed unroutable nets. **Bound to ~3
  attempts** -- surviving that means it isn't placement-fixable.
- **Intrinsic to a cell**: **Step 3 will never fix it -- the annealer moves
  macros, it never regenerates their geometry.** Worked example: 29
  well/latch-up violations, identical before and after routing, traced to
  primitives built `with_substrate_tap=False, with_tie=False` -- a Step 2
  decision (now fixed; if missing-tap rules reappear, fix Step 2).
- Still dirty after the budget: report real counts/categories. **Never hand
  off a dirty GDS as clean.**

A committed `placement_visualization.gds` can be **stale** -- re-render before
believing a re-check; and a clean result depends on that design's tuned
weights, not the defaults.

## Step 5 -- visualize
```
python .claude/skills/placer/script/render_placement.py \
    <design_dir>/layout/primitives/manifest.json \
    <design_dir>/layout/placement_pos.json [--out ...]
```
Runs automatically at the end of Step 3 (that GDS is what 4b checks); run it
directly after `--no-render`, or here on the final post-legalization
placement. Re-imports each macro's GDS from either folder, moves its
**center** to `(x + w/2, y + h/2)`, labels it; `excluded_no_geometry` macros
are reported as skipped, never dropped. **Open with the PDK's layer colors** --
bare, a dense multi-finger device looks like a solid block; the script prints
the exact `klayout -l <file.lyp> ...` command. Sanity-check artifact only: no
routing, no ports wired.

## Hand-off -- what runs next

`layout/placement_pos.json` is the floorplan the final layout starts from:
positions, orientations and real world-space port coordinates. Final routing
and instance/net labelling are outside this skill; the successor is
**`../router/SKILL.md`**, driven by `../../agents/layout-agent.md`:

```
placer (here) -> router -> layout-fixer (DRC + LVS gates) -> verify-agent
```

**Point the router at `<design_dir>/layout`, not `<design_dir>`** -- that is
where `placement_pos.json` and `primitives/manifest.json` live, and it is the
one argument that differs between the two skills: this skill takes the design
**root** and creates `layout/` under it, the router takes that `layout/`
folder itself. Passing the wrong one is the single easiest mistake to make in
this chain.

Route only a placement whose gates passed here (Step 3's overlap/clearance,
Step 4's grid + DRC). The router cannot recover a crowded placement -- raising
`--w-density` and re-running Step 3 is the fix, and no routing knob substitutes
for it.

## Files in this skill
- `script/flatten_netlist.py` (Step 1) + `script/subckt_macros.py`, the
  flattener it uses (standalone: `--print-flat`).
- `script/generate_primitives.py` (Step 2) +
  `script/decomposition_patterns.py`, its yaml reader and the only source of
  grouping (runnable standalone).
- `script/anneal_placement.py` (Step 3), `script/render_placement.py` (Step 5).

External to the skill's own scripts: Step 1 shells out to an ERC checker and
netgen, Step 2 imports the shared `netlist_devices.py` parser and the cell
generators in `src/cells/`, and Step 4's two commands are the executables
shown inline there. Everything else is in `script/`.
