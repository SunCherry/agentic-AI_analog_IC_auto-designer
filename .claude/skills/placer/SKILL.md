---
name: placer
description:
  Generate an empty-canvas starting layout for any analog circuit from its
  frozen netlist -- create the design's `layout/` folder, flatten the netlist
  into one ERC-clean netlist proven equivalent to the original, build a real
  glayout module for each pattern `circuit_decomposition.yaml` names (current
  mirror, differential pair, ...) plus a standalone primitive for every
  ungrouped device, derive the mirror pairs a differential circuit needs from
  that same read, then place everything with simulated annealing (HPWL +
  overlap + symmetry + clearance + area + canvas aspect), grid-check,
  DRC-check and visualize. Use when a design has a frozen netlist and no layout at all --
  no GDS, no generator script, no reference floorplan to adapt.
---
# Placer: flatten -> modules + primitives -> SA placement -> DRC

**Empty canvas only.** If a reference layout or generator script exists, adapt
it instead. Produces a checked floorplan -- one flat netlist, real geometry per
module and device, real macro positions, a viewable GDS, a DRC verdict. It does
not route (Step 3's wire term is a coarse routability proxy) and never touches
the netlist.

**`reference.md` holds the measured background** -- every trap, weight
derivation and worked number. This file is executable without it; read a section
when a step points there.

## Inputs
| Input | Required | Use |
|---|---|---|
| Frozen netlist (`.sp`) | **yes** | terminals and `w`/`l`/`nf`/`m` per device. Decomposed or flat -- Step 1 flattens either |
| `<design_dir>/circuit_decomposition.yaml` | optional | **the only source of pattern grouping**; found in the design dir, the netlist's dir or its parent; `--decomposition PATH` overrides |

**No topology detection happens here.** The yaml is user-confirmed and gives
each device a `role` (the mirror reference is read, not guessed). Without it
every device becomes a standalone primitive (as `--per-device` does) and the run
says so -- a real floorplan with no mirror/pair matching.

## Step 0 -- layout folder
```
mkdir -p <design_dir>/layout/primitives <design_dir>/layout/modules
```
`layout/` holds the flat netlist, then `placement_pos.json`,
`placing_summary.txt`, `placement_visualization.gds`. `modules/` gets one GDS
per pattern-composed macro, `primitives/` one per standalone device plus
`manifest.json` / `manifest.md` / `lvs_compare.sp`.

## Step 1 -- flatten, ERC, prove equivalent
```
python .claude/skills/placer/script/flatten_netlist.py <netlist.sp>
    [--layout-dir <design_dir>/layout] [--out NAME] [--top-subckt NAME]
    [--no-equivalence] [--json report.json]
```
Writes `<layout_dir>/<stem>_flat.sp`, **the only netlist Steps 2+ read**, then
gates it: exit `0` clean, `2` ERC warnings or skipped equivalence, `1` ERC hard
errors or an equivalence mismatch. **On `1`, do not run Step 2.** `--layout-dir`
defaults to `<design_dir>/layout` (`netlist/`, `sizing/`, `device_shaping/` are
stage folders).

1. **Flatten** -- `.subckt` instances expanded one level, internal nets mapped to
   top-level names. Those blocks are usually `.include`d, which a device-line
   parser never opens: without this every device inside a subckt is dropped
   (measured: 7 parsed where the circuit has 11). An already-flat netlist is
   copied verbatim; device names survive unless a collision forces an
   `<instance>.<device>` prefix, so the yaml's `ref`s still match. Nested
   subckts are reported (`nested_unexpanded`) and left alone.
2. **ERC** on the **flat** file -- flattening is where names collide, so
   checking upstream of it checks the wrong file.
3. **Equivalence** -- netgen LVS, flat vs original. Not optional: nothing else
   would notice a wrong pin mapping. `--no-equivalence`, missing netgen or a
   missing PDK setup all report `skipped` (exit 2), never a pass.

**Fixing ERC findings.** The golden netlist is frozen; the flat file is a derived
copy and equivalence bounds a legal fix. Repairable here: element-prefix mismatch
(`X`-call vs `R`-primitive on a resistor carrying both cards), `w`/`l` in SI
metres, unsubstituted placeholders, a duplicate name flattening produced.
**Escalate, don't patch**, anything touching connectivity, device count or
sizing (real opens, port-count mismatch, a device over its model's widest bin):
those are schematic defects, and patching the flat copy breaks equivalence with
the netlist LVS runs against.

**Output**: `<stem>_flat.sp` plus `equivalence/netgen_equivalence.out`. ERC
findings and the verdict are terminal output; `--json PATH` writes the summary
machine-readably.

## Step 2 -- generate modules and primitives
```
python .claude/skills/placer/script/generate_primitives.py <layout>/<stem>_flat.sp \
    --out-dir <design_dir>/layout/primitives \
    --modules-dir <design_dir>/layout/modules \
    [--decomposition <design_dir>/circuit_decomposition.yaml]
    [--no-dummy] [--per-device] [--split-mirror-legs] [--top-subckt NAME]
    [--no-subckt-expand]
```
**Pass Step 1's flat netlist by path** (re-flattening it is a no-op). Handed a
directory, `find_netlist()` tries only `<dir>/<dirname>_final.sp` then
`<dir>/<dirname>.sp` -- it raises, or silently picks an unsized template.
`--no-subckt-expand` turns off the re-flatten; it changes device and net names
only, so it matters when a name the yaml `ref`s must survive verbatim. Preview
grouping first:
`python .claude/skills/placer/script/decomposition_patterns.py <design_dir>/circuit_decomposition.yaml`.

| Pattern | Built as | Folder |
|---|---|---|
| `current_mirror` | **one** `src/cells/blocks/current_mirror.py::current_mirror(pdk, mirror_ratio=[r1, ...])` covering reference **and every leg** -- inside one cell the legs share gate bias and bulk by construction, not via the router. `--split-mirror-legs` restores per-leg macros | `modules/` |
| `differential_pair` | one `src/cells/blocks/diff_pair.py::diff_pair(...)` | `modules/` |
| `capacitor_bank`, exactly 2 caps | one `src/cells/blocks/diff_cap.py::diff_cap(pdk, size, multipliers, arrangement)` -- two matched MiM caps on ONE shared bottom plate, so **three** terminals. `arrangement` trades area against gradient immunity. **Not wired in**; terminals and both arrangements: `reference.md` | `modules/` |
| a differential inductor pair | one `src/cells/blocks/diff_ind.py::diff_ind(n_turns, inner_diameter, separation, arrangement, tie)` -- two identical `primitives/inductor.py` coils, optionally centre-tapped. **`arrangement` is electrical, not cosmetic** -- it flips the sign of the coupling. **Not wired in**; terminals, the `L_diff` formulas and how to choose: `reference.md` | `modules/` |
| any other pattern, or a mirror/pair whose yaml device COUNT the generator can't take | **not auto-composed**: each device falls through to a standalone primitive, the pattern is listed in `manual_composition` with a warning. Drawn but unmatched -- a placeholder, not a finished module | `primitives/` |
| every leftover device (unmatched, all caps/res) | standalone primitive via `src/cells/primitives/fet.py`'s `nmos`/`pmos`, `mimcap.py`'s `mimcap`, or `build_resistor()` (real poly + the PDK's resistor marker, contacted to met1). A **BJT gets no primitive**: `generation: "manual"`, null `w`/`h`/`gds`, excluded from Step 3 (`excluded_no_geometry`) | `primitives/` |

**`diff_cap` and `diff_ind` build real geometry but nothing triggers them yet**
-- both need registry edits first (`reference.md`). Until then, hand-compose and
fold into the manifest. Step 3a still finds such twins without a pattern, so the
symmetry constraint works even while composition does not.

A macro composed by hand must be **folded into the manifest** or Step 3 ignores
it: its own `macros` entry (`name`/`devices`/`w`/`h`/`gds` + real `ports`), the
standalone entries for those devices removed, each device's `macro` field in
`device_index` repointed at it.

**Traps -- read `reference.md` before editing this step or explaining its
output.** In brief: never remove `gf.CONF.n_threads = 1`; total width is
`w * m` and `nf` splits it (do **not** "restore" `w * nf * m`); mirror ratios are
integer N:1 and a non-integer leg is excluded rather than mis-drawn; a diff pair
is drawn twice from half `a`; a 3-terminal resistor subckt call is silently
dropped, so **check resistor count against the manifest**; the process comes
wholly from `pdk_options.json`.

**Warnings to read, not just the exit code**: an unsigned circuit read
(`confirmed_by_user` not true); an empty `patterns` block (judgment steps have not
run -- never *none found*); a pattern naming a device the netlist lacks; a device
in neither `patterns` nor `unmatched_devices`; a mirror with no `role: reference`;
a device claimed by two macros, which would draw it twice.

**Output**: `manifest.json` (+ readable `manifest.md`) and `lvs_compare.sp` --
**the file LVS compares against, not the golden netlist**. Key-by-key schema and
the reason for the `w * m` fold: `reference.md`.

## Step 3 -- anneal the placement

### 3a -- derive the symmetry groups
**Run before annealing, always.** Cheap, and on a single-ended circuit it
correctly emits nothing.
```
python .claude/skills/placer/script/derive_sym_groups.py \
    <design_dir>/layout/primitives/manifest.json
    [--decomposition circuit_decomposition.yaml]  # default: walks up from the manifest
    [--axis X]                                    # default: a shared FREE axis
```
Writes `layout/sym_groups.json` (the `[macroA, macroB, axis]` triples 3b takes)
plus `sym_groups.txt`, naming every pair derived, every group dropped, and why.
**Read that report** -- it is how you check the circuit read agreed with you, not
just that a file appeared.

1. **Matched tie groups.** A `tie_groups` entry naming exactly two devices, with
   `tied`/`also_must_match` set and `ratio_carrier: none`, means they must be
   IDENTICAL -- a mirror pair. A ratioed group (`ratio_carrier: m`, or
   `status: ratio_conflict`) is NOT: its devices are deliberately different sizes.
   Only literal `none` passes; a prose carrier like `m (nominal only ... intent is
   1:1)` is not evidence enough to assert a hard geometric constraint.
2. **Differential-net propagation.** Nets come from `circuit_decomposition.yaml`'s
   `matched_nets` (`kind: differential` only -- `common` is one shared node,
   `branch` a ratioed family, neither a mirror axis). Where that section is empty
   they are re-derived from the pairs above, compared TERMINAL BY TERMINAL,
   because a cross-coupled pair's two devices touch the same two nets just
   swapped, so a set difference finds nothing. Either way, two so-far-unpaired
   macros of the same kind and footprint, one on each side of such a net pair, are
   twins. The report names which source it used.

   **This is the only thing that catches a pair of tank inductors**, or any twin
   passive `pattern-table.md` has no row for -- they arrive via
   `unmatched_devices` with no tie group at all.

Devices sharing ONE macro are dropped, not paired: a mirror or diff pair composed
into a single cell is already matched inside it.

**The axis is `null` by default -- a shared free axis**, one midline the annealer
positions itself. `--axis X` means guessing a coordinate before anything is
placed; what matters is that the halves mirror each other, not where.

### 3b -- anneal
```
python .claude/skills/placer/script/anneal_placement.py \
    <design_dir>/layout/primitives/manifest.json --iters <N> \
    [--t0 50.0] [--accept-threshold 0.02] [--stage-iters N]
    [--w-wire 1.0] [--w-ov 50.0] [--w-sym 10.0] [--w-density 5.0] [--w-area 0.01]
    [--w-aspect 10.0] [--max-aspect 16:9]
    [--sym-groups groups.json] [--min-metal-spacing UM] [--single-phase]
    [--joint-refine-iters 0] [--seed 1] [--no-render]
    [--out ...] [--summary-out ...]
```
**There is no `--physical-map-out`** (the placed box lives in
`placement_pos.json`); passing it is an `unrecognized arguments` error.

**`--iters` is required and has no default.** Read it from
`design_constraints.json` and **say which value you passed**:
```
python .claude/reference/design_constraints.py <design_dir> --key placer_anneal_iters
```
(20000 when the file is silent.) It is an upper bound, not a runtime -- the run
stops at plateau. Never pick it silently: the old default of 20 quietly produced a
barely-perturbed random placement that FAILs overlap.

Other values shown are real defaults; output paths default to the manifest's
**grandparent**, i.e. `layout/`. Cost = HPWL + overlap + symmetry + density
clearance + area + canvas aspect, exponential cooling, displace 70% / swap 15% /
rotate 15%. Where the weights come from: `reference.md`.

- **HPWL** from `device_index` at **macro granularity** -- coarse on purpose,
  port-exact routing comes later. Supply rails excluded via `supply_rail_names`.
  **Select seeds on routed wire, not this number** (`reference.md`).
- **Density**: `min_distance = 10 * min_metal_spacing * num_nets`, the denser
  macro's requirement per pair. **Soft** -- PASS/FAIL, no nonzero exit.
- **Canvas aspect**: the bbox's long side may not exceed `--max-aspect` times its
  short side. **Pass no flag normally** -- the script reads `max_canvas_aspect`
  from `design_constraints.json` itself (walking up from the manifest; `16:9` if
  unset) and prints which source won. Charged as the long side's overrun in
  microns, so it is on HPWL's scale; zero for anything squarer than the limit.
  **Soft** -- PASS/OVER, never part of the verdict or the exit code. One macro
  longer than the limit allows makes the ratio unreachable at any weight -- check
  the biggest macro before turning knobs.
- **Orientation** is `rotation_deg` (0/90/180/270 CCW, **authoritative**) plus
  `mirrored`, a reflection about the macro's own vertical world line. `rotated`
  survives only as a footprint hint (true iff w/h are swapped) and **cannot tell
  0 from 180 or 90 from 270 -- never orient geometry from it**. **Rotate first,
  then reflect, in all three consumers** -- `anneal_placement.py`,
  `render_placement.py`, and the router (its own `local_to_world()`, its obstacle
  map, its GDS writer). Reversing that order silently becomes a horizontal
  mirror; why, and why 180/270 matter at all: `reference.md`. A
  `placement_pos.json` predating `mirrored` still loads (absent = False).

A tiny budget does not converge and FAILs overlap; large values are safe because
stopping is by acceptance ratio (a stage of `max(50, 20*movable)` below
`--accept-threshold`), not move count. A FAIL means more iterations, different
weights or manual legalization -- never "close enough". `--seed` is what to vary.

**Two-phase is the default**: modules place first as the skeleton, single devices
around them (charged full cost, unable to move them). `--single-phase` restores
joint annealing; `--joint-refine-iters N` adds an all-movable pass, off because it
can undo the hierarchy.

**`--sym-groups` -- pass Step 3a's `sym_groups.json`.** Without it symmetry is
inactive and the summary says so (`symmetry: N/A`), which on a differential
circuit is a gap, not a pass. It is enforced two ways, because a soft term alone
loses to HPWL:

- **Orientation is structural**, not bought: a rotate or mirror move carries both
  members, and **a pair holds the same rotation and OPPOSITE reflection**, seeded
  that way so the reflected arrangement is reachable at all.
- **Port symmetry is measured**, matched by **local coordinate, not net name**.
- **Position is the `--w-sym` penalty** on centroids, and **gated**: `symmetry` is
  PASS/FAIL alongside overlap and clearance, inside the `overall` verdict, with a
  per-pair `dx`/`dy`/orientation table in `placing_summary.txt`. It does *not*
  take canvas aspect's exemption -- halves that do not mirror have mismatched
  parasitics, a correctness defect.

**Symmetry needs a bigger `--iters`** -- start around **100k** when groups are
active, and **a symmetry FAIL is usually budget, not impossibility: re-run longer
before touching weights.** Raising `--w-sym` is counterproductive and
non-monotonic. Symmetry also costs wire, and that is the correct trade -- do not
"fix" it by dropping the groups. Numbers for all three: `reference.md`.

**Output** in `layout/`: `placement_pos.json` and `placement_visualization.gds`.
Each `positions` entry carries
`{x, y, rotation_deg, rotated, mirrored, w, h, num_nets, min_distance_um, ports}`
-- `w`/`h` already post-rotation, `ports` in world coords under Step 5's centre
convention -- **plus the placed box `x0_um`/`y0_um`/`x1_um`/`y1_um`, which is what
Step 4a reads**. `placing_summary.txt` is written **every run, PASS or FAIL**:
gates with residuals, HPWL stats, bbox/utilization, cost breakdown by % share, 10
worst nets, 8 tightest pairs, per-tier placement, seed/weights. Two sections turn
a FAIL into an action -- tightest pairs says by how much (-0.03um is a couple of
iterations, -4um is a floorplan problem), and the % shares say what is actually
being optimized (raising `--w-sym` at 0.0% share changes nothing). Utilization is
not the clearance penalty: low utilization is fine, that penalty buys routing
channels.

## Step 4 -- grid legality check (4a), then DRC (4b)

### 4a -- check legality on the routing grid
Only if Step 3 reported nonzero overlap (it exits nonzero then).
```
python .claude/reference/generate_grid.py <design_dir>/layout --no-save
```
**This REPORTS; it does not legalize.** It reads each macro's placed box out of
`placement_pos.json`, builds the routing grid at the PDK's finest layer pitch, and
prints free/blocked/used counts plus an ASCII preview. It contains no move step
and **never writes `placement_pos.json`** -- with `--no-save` it writes nothing,
without it only `placement_grid.json`. Resolve a nonzero overlap one of two ways,
both yours:
- **Re-run Step 3** with more `--iters`, another `--seed`, or higher
  `--w-density` -- the normal fix, and the only one that re-optimizes.
- **Move the macro by hand** -- edit its `x`/`y` *and* its `x0_um`..`y1_um` box in
  `placement_pos.json` (they must stay consistent; Step 5 and the router read
  different ones). Reserve this for a single stubborn macro.

Either way **re-render before re-checking** -- the grid reads positions, DRC reads
the GDS, and a stale GDS reports the old placement's result.

### 4b -- DRC on the rendered GDS
```
python .claude/skills/router/script/run_drc.py <design_dir>/layout/placement_visualization.gds \
    --work-dir <design_dir>/layout/drc_work_placement
```
**Pass `--work-dir`.** It defaults to `<gds's dir>/drc_work`, which every later
DRC on a GDS in `layout/` shares -- the router's own pass would overwrite this
run's `drc.log`/`drc_violations.json`, and keeping this one separate is what makes
the pre-routing-vs-routed comparison possible later.

`DRC_TOTAL == 0` with no `RULE:` lines -- done. Otherwise **classify before
reacting** (precedents for both classes: `reference.md`):
- **Placement-fixable** (spacing *between* macros): re-run Step 3 with higher
  `--w-density`, another `--seed`, more `--iters`; re-render, re-check. **Bound to
  ~3 attempts** -- surviving that means it isn't placement-fixable.
- **Intrinsic to a cell**: **Step 3 will never fix it** -- the annealer moves
  macros, it never regenerates geometry. Fix Step 2.
- Still dirty after the budget: report real counts and categories. **Never hand
  off a dirty GDS as clean.**

A committed `placement_visualization.gds` can be **stale** -- re-render before
believing a re-check.

## Step 5 -- visualize
```
python .claude/skills/placer/script/render_placement.py \
    <design_dir>/layout/primitives/manifest.json \
    <design_dir>/layout/placement_pos.json [--out ...]
```
Runs automatically at the end of Step 3 (that GDS is what 4b checks); run it
directly after `--no-render`, or here on a final post-legalization placement.
Re-imports each macro's GDS, moves its **center** to `(x + w/2, y + h/2)`, applies
rotation then mirror, labels it; `excluded_no_geometry` macros are reported as
skipped, never dropped. **Open with the PDK's layer colors** -- bare, a dense
multi-finger device looks like a solid block; the script prints the exact
`klayout -l <file.lyp> ...` command. Sanity-check artifact only: no routing, no
ports wired.

## Hand-off
`layout/placement_pos.json` is the floorplan the final layout starts from; routing
and labelling are **`../router/SKILL.md`**'s, driven by
`../../agents/layout-agent.md`:
```
placer (here) -> router -> layout-fixer (DRC + LVS gates) -> verify-agent
```
**Point the router at `<design_dir>/layout`, not `<design_dir>`** -- that is where
`placement_pos.json` and `primitives/manifest.json` live, and it is the one
argument that differs between the two skills (this one takes the design **root**
and creates `layout/` under it). The easiest mistake in the chain.

Route only a placement whose gates passed here (Step 3's
overlap/clearance/symmetry, Step 4's grid + DRC). The router cannot recover a
crowded placement -- raising `--w-density` and re-running Step 3 is the fix, and
no routing knob substitutes for it.

## Files in this skill
- `reference.md` -- traps, weight derivations, `manifest.json` schema, worked
  numbers. Read a section when a step points there.
- `script/flatten_netlist.py` (Step 1) + `script/subckt_macros.py`, the flattener
  it uses (standalone: `--print-flat`).
- `script/generate_primitives.py` (Step 2) + `script/decomposition_patterns.py`,
  its yaml reader and the only source of grouping (runnable standalone).
- `script/derive_sym_groups.py` (Step 3a) -- `circuit_decomposition.yaml`
  (`tie_groups` + `matched_nets`) -> `sym_groups.json`; writes no geometry.
- `script/anneal_placement.py` (Step 3b), `script/render_placement.py` (Step 5).

External: Step 1 shells out to an ERC checker and netgen, Step 2 imports the
shared `netlist_devices.py` parser and the cell generators in `src/cells/`, and
Step 4's two commands are the executables shown inline. Everything else is in
`script/`.
