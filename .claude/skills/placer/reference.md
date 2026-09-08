# Placer — Reference

Background for `SKILL.md`. Read a section when that step needs it; the
procedure itself is executable without this file. Everything here was measured
on a real design — none of it is advice.

## Step 2 — traps, each measured

**Do not remove `gf.CONF.n_threads = 1`.** glayout geometry construction is not
thread-safe at the default 8: identical input gave byte-different GDS, one
clean, one with 24 extra DRC violations. `PYTHONHASHSEED` does not fix it, and
a single-cell build is not a valid test.

**`src/cells/`, not `cells/`.** `CELLS_DIR` pointing at a nonexistent
`<repo>/cells` killed every run at `from primitives.fet import nmos`. Check that
path first if the import ever fails again.

**Total width is `w * m`; `nf` SPLITS `w` into fingers and adds none.** glayout
is the other way round — its `width` is the PER-FINGER extent and `fingers` adds
width — so a device is drawn `width=w/nf, fingers=nf*m`, giving
`(w/nf) * nf * m = w * m`. All three are honored: `m` -> `multipliers`; a pair
splits `nf * m` across its halves, which its common-centroid cell places twice;
a mirror draws its reference at `nf * m` fingers. `lvs_compare.sp` folds MOS to
`w * m`, so cell and netlist must agree or LVS cannot match.
**Do not "restore" `w * nf * m`** — that reading was tested and refuted against
this PDK's own models. BSIM4 derives `Weff = W/NF`, so `w` is total;
`gen_current_mirror`'s `total_w()` carries the three measured currents.

**Mirror ratio is integer N:1 only**, `round(total_w(leg)/total_w(ref))` clamped
`>=1`. A leg more than 5% off an integer multiple of the reference is not
drawable in the cell at its netlist width, so it is excluded from the composed
macro and falls through to a standalone primitive at its true width, with a
warning naming it and a `manual_composition` entry. If no leg is expressible the
whole mirror falls through and every device — reference included — is drawn
standalone. Correct the widths; the in-cell matching for those legs is lost,
which the cell could not have provided anyway. To keep it, make the reference
the design's UNIT device (then every leg is an integer multiple) or compose by
hand.

**It does not round and draw the wrong width** — doing so was a measured bug:
`example/test_miller_ota`'s `cm_nbias` (reference 236um, legs 102.6um = 0.435x
and 344.96um = 1.462x) emitted `mirror_ratio=[1, 1]` and drew all three legs at
236um, reaching LVS as a width mismatch on two devices.

**A diff pair is drawn twice from half `a`'s geometry.** Disagreeing halves warn
and `b` is drawn at `a`'s size — fix the netlist, asymmetry here is input
offset. Odd `nf * m` rounds up, also warned.

**A 3-terminal resistor subckt call is dropped.** The parser matches a 2-net
`R`-line, so it becomes `kind="other"` with no primitive and the floorplan will
not LVS. Workaround: a 2-net `R`-line in a derived copy, re-run through Step 1.

**The process comes from `.claude/reference/pdk_options.json`**, nothing is
hardcoded: the glayout PDK object is resolved by that file's `glayout_module`,
the metal stack from the PDK's own `valid_glayers`, and the resistor-recognition
marker from the poly glayer's GDS number paired with
`resistor_marker_datatype`. Retargeting is the one-word edit to `selected`
there. Two honest limits: a PDK with no `resistor_marker_datatype` gets **no
resistor primitive** (a marker on a guessed layer extracts as wiring, so it
refuses and says so), and only the active PDK has been run end-to-end — the
sky130 numbers here are measured, another process's are not.

**`import glayout` resolves to the external editable install** at
`~/Documents/work/external_ai/gLayout`; the script's `REPO/"src"` insert does
not shadow it, whatever its comment says.

## Step 2 — `manifest.json`, key by key

Per macro: `name`/`kind`/`devices`/`w`/`h`/`gds`/`generation`/`constraints`/
`glayout_call`; real near-edge `ports` (net -> landing points, in **local coords
relative to the bbox center**, with `bbox_center_offset`); `keepout_um` on a MiM
cap.

Top level: `primitives_dir`/`modules_dir`, `device_index`, `supply_rail_names`,
`manual_composition`, `min_metal_spacing_um`, and `top_pins` — the top
`.subckt`'s pins, without which Magic's `port makeall` promotes internal labels
and LVS fails pin matching.

Modules carry real ports on every routing-relevant net and standalone fets ride
`*_bo_*` stubs; only a portless net falls back to the router's box-edge
approximation. `manifest.md` is the same list, readable.

**`lvs_compare.sp` is what LVS compares against, not the golden netlist**: `w`
folded to `w * m` with `nf`/`m` set to 1, because the PDK's netgen setup deletes
`nf`/`mult` while Magic merges a folded device back to total width — otherwise
every `nf > 1` device reports a width delta of exactly `nf`, masking real errors.

## Step 3b — where the weights come from

**`--w-aspect 10.0`** is measured (full sweep in the script's docstring). At 1.0
the area term outbids it and the ratio is not held; at 3 the box lands 0.01um
over; 10 passes; 30 buys nothing further. Roughly 10% HPWL for a box that fits
the shape asked for.

**`--w-area 0.01`** is deliberate: a bbox is 10^4 um^2, so `1.0` would swamp
HPWL.

**Two-phase**, measured on 8 macros at `--iters 20000 --w-density 60
--w-area 0.0005`: 35% smaller bbox at the same cost/HPWL.

**`--per-device`** is coarser, not a superset: more macros means more nets per
boundary, so routing needs more room — one design went 7 -> 11 macros and needed
`--w-density 200`, not 60.

**`--w-density`**: at the default 5 on an inductor design the only clearance
violation was coil-to-coil (1.48um against the met5 1.60um minimum), because
area was 61% of total cost and clearance 0.0%. 30 closes it.

## Step 3b — why orientation needs all four turns, and a reflection

180/270 give an identical footprint and were once omitted for exactly that
reason. They matter now because ports are in the symmetry cost and a mirror pair
shares one rotation: an orientation that faces both twins' terminals away from
the cluster makes every net pay the detour twice — ~358um per tank net on an LC
VCO whose coil brings its terminal out 4um from one edge.

**Rotate then reflect, never the other way.** `mirror_x . rot90 ==
rot90 . mirror_y`, so reflecting first silently becomes a *horizontal* mirror on
any rotated macro. Measured: reflect-then-rotate left that VCO's two tank ports
at the same offset from their own centres — translated copies, 175.78um from
where a mirror puts them — with the reflection flags already correct.

Matching centroids and orientation alone is not symmetry either. For two
instances of one cell it puts both of their ports on the same side, which left
the same design's two tank nets' own lower bounds 110um apart: an imbalance no
router can fix, because it is in the floorplan.

**Port symmetry is matched by local coordinate, not net name.** A differential
pair does not share the net name on its mirrored terminal (`voutp` vs `voutn`),
so name-matching skips the one port the constraint exists to protect and reports
a clean pass beside a 175um error.

## Step 3b — the symmetry budget, and what symmetry costs

The groups plus the four rotations enlarge the search space, and `--iters` is an
upper bound with an acceptance-ratio stop, so an under-budgeted run does not run
long — it stops early at a bad local optimum and FAILs the gate. Measured, seed
1: at 20000 the gate FAILs with an 84um centroid offset; at 120000 it PASSes at
0.01um with HPWL 815.64. Raising the budget is cheap when it is not needed.

Raising `--w-sym` instead is counterproductive and non-monotonic: at 100 and 500
HPWL explodes (751.91 -> 2233.91 -> 2436.54 on one seed) and another seed flips
from PASS to FAIL going 10 -> 500. Symmetry's cost share is ~0%, so the knob has
no useful range. **A symmetry FAIL is budget, not impossibility.**

**Symmetry costs wire, and that is the correct trade.** Same design, 8 macros,
`--iters 20000 --seed 26 --single-phase --w-density 30 --min-metal-spacing 0.16
--max-aspect 4:1`: without groups, macro-centre HPWL 435.70um but the varactor
pair lands rot-90/rot-0 and clearance FAILs; with the four derived groups, HPWL
831.82um, every pair mirrored to within 0.02um on one shared axis, and
overlap/clearance/symmetry/aspect all PASS. Do not "fix" that increase by
dropping the groups.

## Step 3b — seed selection

Select on **routed** wire, not the annealer's HPWL. Its HPWL is macro-centre and
drops supply nets, and a macro's port can sit 145um off-centre. Measured across
five all-gates-PASS candidates: the best-HPWL seed routed 48% worse than the
best-routed one, and the annealer's own best candidate ranked 11th of 130 by
real port-to-port distance.

## Step 4b — a clean baseline is worth keeping

Precedent for the placement-fixable class: `--w-density` 5 -> 30 fixed
unroutable nets. Precedent for the intrinsic class: 29 well/latch-up violations,
identical before and after routing, traced to primitives built
`with_substrate_tap=False, with_tie=False` — a Step 2 decision, which Step 3
could never have fixed because the annealer moves macros and never regenerates
their geometry.

## Step 2 — `diff_cap` and `diff_ind`, and what wiring them in takes

**`diff_cap(pdk, size, multipliers, arrangement)`** — two matched MiM caps
sharing ONE bottom plate, so **three** terminals: `CM` (the shielded shared
plate, tied to a low-impedance node) plus `CA`/`CB`. `common_centroid` lays the
units A B B A so both capacitors' centroids land on the same point and a linear
process gradient shifts them equally — the ratio survives it; it needs an even
`multipliers`, since each capacitor is split into halves. `side_by_side` is
smaller and simpler and still mirror-symmetric, but the two centroids sit a block
apart, so a gradient makes one cap larger. Prefer common-centroid whenever the
two caps' RATIO matters.

**`diff_ind(n_turns, inner_diameter, separation, arrangement, tie)`** — two
identical `primitives/inductor.py` coils drawn once and placed twice. Four
terminals `AP/BP/AN/BN`, or three with `tie="center_tap"` (`AP`/`AN`/`CT`), which
is what a differential LC tank wants and a single coil cannot give.

`arrangement` is an electrical choice, not a cosmetic one. `mirror` reflects the
second coil, which reverses its handedness: under differential drive the two
moments end up parallel, they oppose, and `L_diff = 2(L - M)`. `parallel` keeps
both wound the same way, the moments are antiparallel, the coupling aids, and
`L_diff = 2(L + M)` — more inductance from the same two coils and the same area.
Neither is universally right, so the module computes M by Neumann integration
over the two real conduction paths and reports `k` and `L_diff` for the placement
it actually drew. Pick from those numbers.

**Wiring either one in** takes three edits: add the pattern to
`decomposition_patterns.py`'s `MACRO_TOPOLOGIES`, add its arity to
`MACRO_DEVICE_COUNTS` (`(2, 2)` for `capacitor_bank` — a 3+ bank must keep
falling through), and add a `gen_*()` branch in `generate_primitives.py`.
`diff_ind` additionally needs a `pattern-table.md` row *first*: nothing
inductor-bearing is registered, so coils reach the manifest via
`unmatched_devices` and there is nothing for a table row to match on.

Worth the wiring: two *separate* mirrored coils sit terminal-to-terminal, and on
one LC VCO that more than doubled the direct `voutp`-`voutn` coupling cap
(1.55 -> 3.67 fF). Entering differentially at 2x, that alone cost the design its
`TuningRange` floor post-layout. One symmetric cell makes that coupling part of
the device instead of a parasitic between two of them.
