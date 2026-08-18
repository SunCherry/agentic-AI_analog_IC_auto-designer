# Cascode Current Mirror — Layout Pattern

## What it is
A current mirror where each branch (reference and output) is **two
transistors stacked in series** rather than one, adding a cascode device
on top of the plain mirror transistor to raise output impedance. The two
branches must be biased level-for-level: the bottom devices share one
gate bias, the top (cascode) devices share a second, different gate bias.
There are two common ways to generate those two gate biases:

1. **Self-biased, stacked-diode reference** — the reference branch is
   itself two *independently* diode-connected devices in series. Simple,
   but costs two stacked `Vgs` drops of headroom.
2. **FVF-biased (low-voltage) reference** — the two gate biases come from
   two Flipped Voltage Followers instead of a literal diode stack, which
   trades some complexity for significantly better output headroom (see
   `flipped-voltage-follower/fvf-layout.md` for the FVF pattern itself).

## Recognizing it in a netlist

### Variant 1: stacked-diode (self-biased)
Four same-type devices: `ref_bot`/`ref_top` (reference branch, stacked)
and `mirror_bot`/`mirror_top` (output branch, stacked):
- `ref_bot` diode-connected: `gate(ref_bot) == drain(ref_bot)` (call this node `VB1`)
- `ref_top` stacked on `ref_bot` and *also* independently diode-connected:
  `source(ref_top) == drain(ref_bot)` and `gate(ref_top) == drain(ref_top)` (call this node `VB2`)
- `mirror_bot` mirrors `ref_bot`: `gate(mirror_bot) == VB1`, `source(mirror_bot) == source(ref_bot)`
- `mirror_top` stacked on `mirror_bot` and mirrors `ref_top`'s cascode bias:
  `source(mirror_top) == drain(mirror_bot)`, `gate(mirror_top) == VB2`, `drain(mirror_top)` is the distinct output node

```
M_RB  VB1 VB1 GND  GND  nfet ...        <- reference bottom, diode-connected
M_RT  VB2 VB2 VB1  GND  nfet ...        <- reference cascode, stacked + diode-connected
M_MB  MID VB1 GND  GND  nfet ...        <- mirror bottom
M_MT  IOUT VB2 MID GND  nfet ...        <- mirror cascode, stacked
```

### Variant 2: FVF-biased (low-voltage)
Two FVF instances (each individually matching `is_fvf`'s signature — see
`flipped-voltage-follower/fvf-layout.md`) whose `Ib` feedback nodes bias a
stacked 2-level output branch instead of a stacked-diode reference:
- `bias_fvf` (fet1, fet2): `drain(fet1) == gate(fet2)` — call this node `IBIAS1`
- `cascode_fvf` (fet3, fet4): `drain(fet3) == gate(fet4)` — call this node `IBIAS2`, distinct from `IBIAS1`
- `mirror_bot`: `gate(mirror_bot) == IBIAS1`
- `mirror_top`: `source(mirror_top) == drain(mirror_bot)`, `gate(mirror_top) == IBIAS2`

`detect_topology.py` finds both variants and reports the full device group
as one `cascode_current_mirror` finding (not as a separate plain
current-mirror finding for just the bottom pair, and not as separate FVF
findings for the two bias branches).

## glayout call

**Variant 2 (FVF-biased) has a real, ready module:**
`src/glayout/cells/composite/low_voltage_cmirror/low_voltage_cmirror.py`

```python
from glayout.cells.composite.low_voltage_cmirror.low_voltage_cmirror import low_voltage_cmirror

lvcm = low_voltage_cmirror(
    pdk,
    width=(4.15, 1.42),      # (main fet width, bias_fvf's smaller feedback-fet width)
    length=2,
    fingers=(2, 1),
    multipliers=(1, 1),
)
```
This builds 2 FVFs (`bias_fvf`, `cascode_fvf`) + 8 nfets forming **two**
output branches (`IOUT1`, `IOUT2`) — it's a 2-output-leg mirror by
construction, ports `IBIAS1, IBIAS2, GND, IOUT1, IOUT2`.

**Variant 1 (stacked-diode) has no dedicated module.** Despite its name,
`cells/composite/stacked_current_mirror/stacked_current_mirror.py`'s
`stacked_nfet_current_mirror()` does **not** build a real series-stacked
cascode — it returns two side-by-side single-level `nmos()` components
(used elsewhere, in `diff_pair_stackedcmirror.py`, as a diff pair's plain
non-cascode tail-current source). If you need variant 1, compose it
directly from primitives instead:

```python
from glayout.primitives.fet import nmos

ref_bot   = nmos(pdk, width=W, length=L, fingers=F)
ref_top   = nmos(pdk, width=W, length=L, fingers=F)
mirror_bot = nmos(pdk, width=W, length=L, fingers=F)
mirror_top = nmos(pdk, width=W, length=L, fingers=F)
# stack ref_top on ref_bot and mirror_top on mirror_bot (drain-to-source
# routing), diode-connect ref_bot and ref_top independently, then route
# ref_bot's/ref_top's gate nets across to mirror_bot's/mirror_top's gates
```
Given the extra manual routing this requires and that it costs more
headroom for no layout benefit over variant 2, **prefer `low_voltage_cmirror()`
(variant 2) unless the design specifically calls for the stacked-diode
reference** (e.g. matching an existing schematic that already uses it).

## Why it's laid out this way
- **Both stacked pairs need to track each other, not just each pair
  internally.** The bottom devices (`ref_bot`/`mirror_bot`) need standard
  current-mirror matching (see `current-mirror-layout.md`), and
  *separately* the top/cascode devices (`ref_top`/`mirror_top`) need the
  same matching at their level. Placing the two stacks with a shared
  symmetry axis (not just common-centroid within each level) keeps a
  process gradient from unbalancing one level relative to the other.
- **The FVF-based variant's headroom advantage is a direct layout
  consequence, not just a schematic trick**: a stacked-diode reference
  physically burns `Vgs(ref_bot) + Vgs(ref_top)` of headroom before the
  output branch even starts; the FVF's feedback loop delivers a lower
  cascode gate voltage without that stacked `Vgs` cost, which is why
  `low_voltage_cmirror()` is the better default (see `glayout call` above).
- **The stacking order matters for LVS, not just DRC.** `mirror_top`'s
  source must land exactly on `mirror_bot`'s drain — if a layout
  accidentally routes the cascode device's source to a *different* node
  than the bottom device's drain (e.g. through an intervening via stack
  that isn't actually the same net), LVS will report a net-count mismatch
  that's easy to misdiagnose as a mirror error when it's really a stacking
  connectivity error.

## Common mistakes
- Reaching for `stacked_current_mirror.py`'s `stacked_nfet_current_mirror()`
  expecting real series cascoding — it doesn't do that (see above); it's a
  plain single-level mirror-pair builder.
- Building the stacked-diode reference (variant 1) when there's no
  specific reason not to use `low_voltage_cmirror()` (variant 2) — variant
  1 costs headroom and manual routing for no benefit unless you're
  matching an existing design that already committed to it.
- Forgetting that the two gate-bias levels (`VB1`/`VB2` or
  `IBIAS1`/`IBIAS2`) must each independently satisfy the matching
  requirements of a plain current mirror at their level — treating the
  whole 4-device (or 6-device, FVF variant) stack as "one big matched
  group" instead of two separately-matched levels.

## Bipolar variant (NPN/PNP)

Only the **stacked-diode** variant generalizes to bipolar --
`detect_topology.py`'s stacked-diode cascode detector applies unchanged to
BJTs (base~gate, collector~drain, emitter~source), since it's built
directly on the kind-agnostic `is_current_mirror()` check:

```
Q_RB  VB1 VB1 GND   npn ...       <- reference bottom, diode-connected
Q_RT  VB2 VB2 VB1   npn ...       <- reference cascode, stacked + diode-connected
Q_MB  MID VB1 GND   npn ...       <- mirror bottom
Q_MT  IOUT VB2 MID  npn ...       <- mirror cascode, stacked
```

The **FVF-biased variant does not generalize** -- `is_fvf()` is
explicitly restricted to MOS kinds (see `fvf-layout.md`), so a BJT
structure with FVF-shaped connectivity is intentionally left unclassified
rather than mislabeled as an FVF-based cascode. There's a loosely
analogous bipolar technique (using an extra buffering transistor to
shift the cascode bias down without a full stacked-`V_BE` cost), but it's
a different circuit, not a drop-in bipolar substitute for the MOS FVF
cascode, so it isn't auto-detected here.

Same headroom-and-error caveat as the plain bipolar current mirror
applies with extra force here: each diode-connected reference level
carries its own base-current error, and errors from the two stacked
levels don't cancel each other -- if you need a precision bipolar cascode
mirror, check whether the netlist actually implements a beta-helper
(Widlar/Wilson-style) correction at each level before assuming a plain
4-transistor stack will hold the mirror ratio accurately.

No dedicated glayout module exists for either bipolar cascode variant --
compose the stacked-diode form from 4× `npn()`/`pnp()` primitives (see
`src/glayout/primitives/bjt.py`), same composition caveats (no attached
netlist, docstrings copy-pasted from the MOS generator) as noted in
`current-mirror-layout.md`'s bipolar section.
