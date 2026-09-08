# Special Tracing Patterns

The registry of routing shapes worth **more than the shortest wire**.

`shorten_routes.py` optimises wirelength, then vias, then turns -- net by net,
each in isolation. That is right for most of a layout and wrong wherever the
spec is set not by how long a wire is but by how *equal* two wires are: 20um of
extra metal on one half of a matched pair is series R and coupling C the other
half never sees, and shortening the two nets independently has no term that
knows they are a pair.

**A pattern exists only if it has a row here.** Three rules bind every entry:

- **Routing only.** An entry may ask for different wire and nothing else --
  never a device moved, rotated, mirrored or resized. Where it needs symmetric
  placement it *tests* the placement it was given and declines.
- **Any circuit.** No entry tests for a device kind. Twins are twins whether
  the devices behind them are MOS, bipolar or passive; that judgment was made
  upstream, with the netlist in hand, and is read from `matched_nets`. Name
  terminals, never a net name from one design.
- **Any process.** No layer name, no rule value. Width, pitch, spacing and via
  geometry come from the active PDK and from the input layout's own metal.

## Status

`shorten_routes.py` implements no entry below. `script/river_route.py`
implements and tests entry 1's *geometry*; the rest -- searching the trunk,
drawing it, gating it -- is specification.

- **`specified`** -- construction, inputs, acceptance and failure modes stated;
  implementable without further design.
- **`registered`** -- constraint and reason stated, construction not. Do not
  implement one of these from its summary line.

Registering "no special treatment, and here is why" is worth doing: entry 4
exists to stop a future reader equal-lengthing a ratioed family.

## Where the pass runs

```
prune -> bulk relax -> rip-up/re-route -> SPECIAL PATTERNS -> emit -> DRC revert loop
```

After `optimize()`, before `emit()`: matching is applied to the *shortened*
routes, because the shortener has no term that preserves matching and would
simply undo it. A pattern instance is accepted or reverted **whole** -- half a
matched pair is not a partial success.

## Inputs beyond the skill's two

Which nets are twins is a circuit fact no geometry recovers, so this pass reads
a third file -- a real scope change to declare in `../SKILL.md`.

| Input | Used for | If missing |
|---|---|---|
| `circuit_decomposition.yaml`, the `matched_nets` section keyed by `kind` | which nets, and what they are to each other | the pass is **skipped**. Never guess twins from net names: a `p`/`n` suffix is a convention, not evidence |
| `physical_map.json` `nets[].endpoints[]` | pairing landings by `{instance, terminal}` | the pattern cannot pair its landings and does not apply |
| `sym_groups.json` (optional) | corroborating which devices are twins | the transform is fitted to the landings, which is the authority anyway |

The map's top-level `pairs` key is written `[]` by
`../../layout-extractor/script/physical_map_from_placement.py` and is empty in
every map in this repo -- it is not a source of twins.

## The list

| # | ID | Shape it draws | `kind` | Status |
|---|---|---|---|---|
| 1 | `river_bundle` | two nets down one corridor at constant pitch, split off one trunk | `differential` | **specified** |
| 2 | `balanced_spine` | one net as a wide low-R trunk, taps at equal distance along it | `common` | registered |
| 3 | `equal_length_pair` | two nets of equal length, layers and vias, spaced apart not bundled | `complementary` | registered |
| 4 | `independent_routes` | nothing -- each net routed on its own, deliberately | `branch` | registered |
| 5 | `quiet_corridor` | one net kept short and clear of every other pattern's metal | `sensitive` | registered |

---

## 1. `river_bundle` -- river routing

**Status: specified.** River routing carries a bundle along one corridor at
constant spacing, so every wire in it sees the same geometry -- equal R, equal
C and equal via count bought at once, because the wires are drawn from one path
rather than measured against each other.

**Trigger.** A `matched_nets` entry with `kind: differential` and exactly two
nets, both present in the map with >= 2 frozen terminal clusters, whose
landings pair 1:1. Three or more nets, or landings that do not pair: reported
and skipped, never approximated.

**Constraint.** After the rebuild, for the pair (A, B): length difference
`<= --match-tol`; equal corner count; equal via count per layer pair; identical
layer sequence. *Total* length, not per-segment -- equal totals on the same
layers with the same vias is equal series R and equal ground C to the accuracy
anything downstream of this can claim.

**Construction.** `script/river_route.py` holds the geometry;
`ConnectGrid.astar()` finds the trunk and `path_to_items()` draws the result.

1. **Pair the landings, fit the transform.** Cluster `A@D1.t` pairs with
   `B@D2.t` -- the same terminal `t` on twin devices `D1`, `D2`
   (`matched_nets[].devices`). `fit_transform()` on the paired centres returns
   mirror, translate, or **None**, and None is a real answer: the placement is
   not symmetric, so the pattern declines. Fit on *all* paired clusters. Which
   terminal `t` is, and what device carries it, this pass never asks.
2. **Route the trunk once**, with clearance grown by `pitch/2` -- the grid
   already offsets by clearance + half wire width, so that leaves room for both
   images. `--bundle-pitch` defaults to the PDK min separation plus one wire
   width.
3. **Split.** `plan()` returns both tracks: mirror mode reflects the trunk,
   translate mode offsets it by +/- pitch/2. Then one fan-out stub per end --
   search A's, *derive* B's through the transform; if the derived stub fails
   gate 1, search it instead and accept only if its `(length, vias, corners)`
   equals A's.

**The trunk is a template and is never emitted.** Two distinct nets cannot
share metal -- that is a short, and the notch gate would not even catch it,
since same-net metal is not an obstacle.

**The two modes are not interchangeable.** Offsetting is *not*
equal-by-construction. Two tracks offset from one trunk differ by

```
dL = 2 * pitch * T        T = (left turns) - (right turns) along the trunk
```

so they are equal only when the trunk's turns balance (`T = 0`). A single net
corner -- an L around a macro, the commonest corridor shape there is --
mismatches the pair by `2 * pitch`, the same order as the mismatch the pattern
was invoked to remove. A reflection is an isometry and carries no such term:
**mirror mode is exact whatever the trunk does.** So prefer mirror mode; in
translate mode require `T = 0`, and otherwise reroute the trunk or decline.
Never report a pair matched on the strength of the construction alone --
`plan()` returns `residual_um`, and the gate is on that number.
`river_route.py --self-test` checks both laws over randomised trunks.

One further refusal is geometric: two same-handed turns around a leg shorter
than the offset make the offset polyline **fold**, and the two tracks cross --
a short between the twins. `split()` raises rather than draw it. A single
perpendicular jog, however short, is fine: both tracks step across together and
stay one pitch apart.

**Acceptance.** Gates 1-3 -- exact legality, no same-net notch, spans every
landing -- stand unchanged and are checked per net. Gate 4 is *replaced*,
because matching costs wire and a rule forbidding any increase forbids the
pattern:

1. the pair's mismatch before the pass must exceed `--match-tol`. **An already
   matched pair is left alone** -- perturbing clean geometry for no gain is how
   a safe rewrite breaks DRC;
2. both nets satisfy the Constraint above;
3. the pair's combined length grows by at most `--match-budget` (default 0.15
   of the shortened pair). A bundle detouring half the die buys series R on
   both halves, not matching.

Failing any of the three reverts the instance and records the mismatch, so the
number stays visible. Then the DRC revert loop, with the instance as the unit:
a new violation on either net reverts **both**.

**Watch out.**
- **The bundle couples the twins.** A long parallel run is a real capacitor
  between the two nets: usually a good trade on a pair's inputs, but on a
  high-impedance node it is load on a pole something upstream sized around. A
  `sensitive` entry (5) naming either net is the signal -- raise
  `--bundle-pitch`, or decline and say so.
- **One net claimed by two entries.** Any matched pair loaded by a second
  matched group leaves a net that is half a `differential` pair *and* a shared
  node. The mirror rule and the spine rule differ, and first-come is not an
  answer: report the collision, and let the tighter constraint win only if a
  human said so in the read's `open_questions`.
- **`differential` promoted from `branch`.** The promotion is valid only at
  1:1; if a multiplier or a device size ratios the legs, the entry should have
  stayed `branch` and equal-length routing is the wrong constraint applied
  confidently. Read the entry's `why` before honouring it.

---

## 2. `balanced_spine` -- registered

- **Trigger.** `kind: common`: one net every member of a matched group lands on
  -- a shared tail, a bias spine.
- **Constraint.** Symmetric *access*, not a twin: each member's landing at an
  equal resistive distance from whatever drives the spine. It prevents the
  daisy chain -- out to the first member, on to the second, on to the third --
  where each samples the shared node after a different IR drop, so the quantity
  that node exists to hold equal arrives different at every landing. A
  schematic simulation never shows it: there the spine has no resistance.
- **Construction (unspecified).** Here the trunk is **real metal** -- one net,
  one spine, real taps -- which makes it a different construction from entry 1
  despite the shared word. Width is a free variable the other entries lack: a
  spine may be drawn wider than minimum to cut R.
- **Watch out.** A `common` net is often a supply or near-supply rail whose
  landings the bulk-relaxation pass may already have released. Both passes must
  agree on what the landings *are*; a spine measured to landings the other has
  since moved measures nothing.

## 3. `equal_length_pair` -- registered

- **Trigger.** `kind: complementary`: two nets carrying one signal and its
  inverse.
- **Constraint.** Equal RC, **no mirror symmetry** -- the nets are opposite in
  phase, not geometric twins. Entry 1's equal-length half applies, its
  transform half does not, which makes this the cheaper pattern: no fit, no
  placement symmetry required.
- **Watch out.** Bundling these *does* couple a signal to its own inverse --
  the opposite of entry 1's benign case. Spacing, not parallelism.

## 4. `independent_routes` -- registered, and the answer is "leave it alone"

- **Trigger.** `kind: branch`: one net per leg of a ratioed family.
- **Constraint.** **None, unless the legs are 1:1.** A ratioed family's legs
  carry current in the ratio sizing chose, so their nets are not twins: what
  each wants is a wire width for its own current, a different pass and not this
  skill's. The circuit read promotes a genuinely 1:1 family to `differential`,
  and that promotion is the only route into entry 1.
- **Watch out.** This row exists because "matched nets" reads as "make them
  match". Equal-lengthing a 1:3 family costs wire on the short leg for no
  electrical return, and buys the long leg nothing.

## 5. `quiet_corridor` -- registered

- **Trigger.** `kind: sensitive`: matched to nothing, but its own parasitics
  set a spec -- a high-impedance node, a node whose capacitance places a pole.
- **Constraint.** Minimum area and minimum coupling, which the shortener
  already optimises -- so the useful addition is *negative*: no other pattern's
  bundle alongside it, no released ring handing it a detour. Priority, not
  geometry.
- **Watch out.** `sensitive` is never auto-proposed by
  `../../circuit-decomposition/script/match_nets.py` -- a human wrote it into
  the circuit read by hand. Its absence means nobody looked.

---

## What lands in `../SKILL.md` when entry 1 is implemented

- **Inputs**: `circuit_decomposition.yaml`, third and optional, and the "no
  netlist is read" line qualified -- the circuit *read* is read, the netlist
  still is not.
- **Flags**: `--decomposition <path>`, `--no-special-patterns`,
  `--patterns <ids>`, `--match-tol 0.05`, `--match-budget 0.15`,
  `--bundle-pitch <um>`.
- **Gates**: gate 4 replaced per instance by the Acceptance rule; 1-3 unchanged.
- **Outputs**: no new file -- a `special_patterns` block in
  `shortening_report.json` and a section in `shortening_summary.txt` (per
  instance: mode, before/after mismatch, length paid, verdict). The seven-file
  rule is not up for negotiation.
- **Honest scope**: symmetrically placed pairs only; mirror mode exact,
  translate mode needs a balanced trunk; matching bought with wirelength up to
  a budget; a pair the placement cannot support is reported, not approximated.

## Registering a new pattern

1. Add a list row and a full section in the same voice. **Name it for the metal
   it draws** -- the "shape it draws" column has to be fillable, and a name
   like `differential_pair` describes devices this file never touches.
2. Check it is routing at all, and circuit- and process-agnostic. If it needs a
   device moved, or cannot be written without a device family, a layer or a
   rule value, it does not belong here -- report it against the placement, or
   generalise it.
3. State the Trigger as a test against `matched_nets` plus the map. "The clock
   nets" is not a trigger; `kind: complementary` with two nets pairing 1:1 by
   twin device is.
4. State the Constraint as something measurable **after** the geometry exists.
   What cannot be checked cannot be gated, and an ungated pattern is an
   assumption.
5. **Prove the construction or measure it.** Entry 1's "equal by construction"
   was wrong by `2 * pitch` per net turn until it was computed. A geometric
   claim in this file needs a check in `river_route.py --self-test`.
6. Say which gate it replaces (never 1-3), what it costs, and cap that.
7. Register the "no constraint" answer too, with its reason. See entry 4.
