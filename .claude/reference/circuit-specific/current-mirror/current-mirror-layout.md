# Current Mirror — Layout Pattern

## Recognizing it in a netlist
**The general case is one reference, many mirrors** — a single
diode-connected reference transistor commonly biases several independent
current-mirror legs at once (e.g. one bias branch feeding both a diff
pair's tail and an output stage). It is exactly one reference (A) and N
mirrors (B1..BN), not necessarily a single 1:1 pair:

- A is diode-connected: `gate(A) == drain(A)`
- Each `Bi` shares A's gate net: `gate(Bi) == gate(A)`
- Each `Bi` shares A's source net: `source(Bi) == source(A)`
- Each `Bi`'s drain differs from A's and from every other `Bi`'s: `drain(Bi)` is that leg's mirrored output

```
M_ref   VREF VREF VSS B  nfet ...   <- diode-connected reference
M_mirr1 VOUT1 VREF VSS B  nfet ...  <- output leg 1
M_mirr2 VOUT2 VREF VSS B  nfet ...  <- output leg 2 (same reference, fans out)
```

`detect_topology.py` scans every same-type device pair for this signature
and then **groups all mirrors that share one reference into a single
finding** (`reference=..., mirrors=[...], N output legs`) — it does not
report N unrelated 1:1 pairs. A `current_mirror()` call only models one
reference + one mirror transistor (see below), so a 1:N finding still
means N separate calls, one per leg, but they are N legs *of the same
bias branch*, not N independent mirrors — size all of them from the same
reference device's parameters.

## glayout call
Module: `src/glayout/cells/elementary/current_mirror/current_mirror.py`

```python
from glayout.cells.elementary.current_mirror.current_mirror import current_mirror

cm = current_mirror(
    pdk,
    numcols=3,             # number of interdigitized columns (see below)
    device='nfet',         # or 'pfet' -- one polarity per call
    with_dummy=True,
    with_substrate_tap=False,
    with_tie=True,
)
```
Ports: `current_mirror_interdigitized_netlist()` produces `VREF, VOUT, VSS, B`.

## Why it's laid out this way
- **Physically it is one interdigitized two-transistor structure**, not two
  separately-placed cells. `current_mirror()` builds both transistors as a
  single common-centroid finger array (reference and mirror fingers
  alternate ABAB across the array) so process gradients affect both
  devices identically -- this is what makes the mirror ratio accurate.
- Because it's one physical placement, **a coordinate/placement-map
  extraction tool (e.g. this repo's `extract_physical_info.py`) will
  record identical bounding boxes for both instance names** — there is
  only one GDS reference for the whole pair. Don't try to "fix" that by
  splitting the box; it reflects reality (see this repo's own
  `DCL_PMOS_S_*`/`DCL_NMOS_S_*` primitive cells, which are exactly this).
- `numcols` sets how many interdigitized columns are used. For a 1:1
  mirror this is typically the folded finger count of one device; for
  ratioed mirrors (e.g. 1:2), size the reference/mirror by adjusting
  finger count asymmetrically -- check `two_nfet_interdigitized`/
  `two_pfet_interdigitized` (called internally by `current_mirror()`) if
  you need an uneven ratio.
- `with_dummy=True` places dummy fingers on both outer edges of the
  interdigitized array -- required so the outermost *real* finger doesn't
  see a different local environment than interior fingers.
- `with_tie=True` wraps a bulk tie ring around the pair; keep this on
  unless the surrounding composite cell (e.g. an opamp's shared tap ring)
  already handles bulk connection at a higher level.

## Common mistakes
- Treating the reference and mirror as two independent single-device
  cells and placing them apart -- this defeats the whole point of the
  matched structure and reintroduces the gradient mismatch the
  interdigitization was meant to cancel.
- Missing the fan-out case: a single diode-connected reference feeding N
  mirror legs is *one bias branch*, not N unrelated mirrors. Draw N
  `current_mirror()` calls (one per reference/mirror leg, since the
  function only models a single reference + single mirror transistor),
  but size every leg from the *same* reference device's parameters --
  don't accidentally treat each leg as an independently-sized mirror.
- Conversely, don't force every mirror leg into one combined interdigitized
  array just because they share a reference -- `current_mirror()`'s
  common-centroid interdigitization is between *one* reference and *one*
  mirror; separate legs are separate physical structures that happen to
  share a bias reference, not one N-way interdigitized structure.

## Bipolar variant (NPN/PNP)

The exact same signature applies with BJT terms substituted in
(`base~gate`, `collector~drain`, `emitter~source`): a reference
diode-connected as `base(A)==collector(A)`, mirrored by one or more
devices sharing that base net and `A`'s emitter net, each with a distinct
collector as its mirrored output. `detect_topology.py` detects this
unchanged, since it only ever checks `kind` for *equality* between two
devices, never assumes it's specifically `nfet`/`pfet`.

```
Q_ref   VB1 VB1 GND  npn ...   <- diode-connected reference
Q_mirr  IOUT VB1 GND  npn ...  <- mirrors Q_ref's current to IOUT
```

**glayout support**: `src/glayout/primitives/bjt.py` has real, working
`npn()`/`pnp()` layout generators (emitter/base/collector regions, well,
tap ring, dummies -- same level of completeness as `nmos()`/`pmos()`).
**But there is no composite BJT current-mirror module** analogous to
`current_mirror()` -- you'd compose one from 2× `npn()`/`pnp()` calls with
matched, interdigitized placement (mirroring `current_mirror()`'s own
internal approach) rather than call a single ready-made function.

Also worth knowing before using these primitives: `npn()`/`pnp()`
currently have their netlist-generation code commented out
(`component.info['netlist']` is never set), unlike `nmos()`/`pmos()`/
`mimcap()` which all attach a netlist automatically -- so a BJT-based
layout built from these primitives won't be LVS-comparable out of the box
the way a MOSFET-based one is. Their docstrings are also literally
copy-pasted from the NMOS generator ("Generic NMOS generator") and
haven't been updated to describe the BJT case -- read the parameter list
itself (`active_area` instead of `width`/`length`, since a BJT is sized by
emitter area, not channel dimensions), not the docstring prose.

**Why bipolar current mirrors need more care than MOS ones**: a BJT's
base draws real DC current (unlike a MOSFET gate), so a plain 2-transistor
BJT mirror has a systematic ratio error proportional to `1/beta` --
this is *why* the Widlar and Wilson mirror topologies exist (adding a
beta-helper transistor to cancel most of the base-current error). If
you're building a precision BJT mirror rather than reproducing a simple
2-transistor structure, check whether the netlist actually uses one of
those topologies (a third transistor whose collector routes back to
supply the two base nodes) before assuming a plain mirror will do.
