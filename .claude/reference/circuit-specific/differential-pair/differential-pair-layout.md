# Differential Pair — Layout Pattern

## Recognizing it in a netlist
Two same-type MOSFETs A, B:
- Sources tied together at a shared (non-supply) tail node: `source(A) == source(B)`
- Gates are distinct (the two differential inputs): `gate(A) != gate(B)`
- Drains are distinct (the two differential outputs): `drain(A) != drain(B)`

```
M_L  VDD1 VP VTAIL B  nfet ...
M_R  VDD2 VN VTAIL B  nfet ...
```

**Important**: a shared *supply rail* (VDD/VSS/GND) is not a diff pair's
tail — that's just two unrelated single-device stages both tied to a
rail. `detect_topology.py` excludes recognized rail names from this check
for exactly that reason; if your netlist uses a non-standard rail name,
you'll need to check the match by hand.

## glayout call
Module: `src/glayout/cells/elementary/diff_pair/diff_pair.py`

```python
from glayout.cells.elementary.diff_pair.diff_pair import diff_pair

dp = diff_pair(
    pdk,
    width=3,
    fingers=4,           # must be >= 2
    n_or_p_fet=True,     # True = nfet pair, False = pfet pair
    substrate_tap=True,
)
```
Ports: `diff_pair_netlist()` produces `VP, VN, VDD1, VDD2, VTAIL, B`.

## Why it's laid out this way
- **Common-centroid, not side-by-side.** `diff_pair()` places the two
  transistors in two rows using an AB/BA interleaved placement (four
  device references total: two half-copies of the left device, two of the
  right), so a linear gradient across the pair affects both halves
  equally. This is why the reference netlist (`diff_pair_netlist`) models
  the layout as four device instances even though the schematic only has
  two -- LVS is comparing against the *actual* common-centroid structure,
  not an idealized two-device schematic.
- Sources are shorted at the tail internally by the cell; the caller only
  needs to provide the external tail connection.
- `dummy`/`dum_net`: dummy fingers surround the array on the same
  common-centroid-edge-effect reasoning as the current mirror. The
  dummies' net gets absorbed differently depending on PDK (`B` on sky130,
  a separate `dum` net on gf180) -- see the code comment in
  `diff_pair_netlist()` if you're hand-writing a reference netlist for LVS.

## Common mistakes
- Instantiating the two transistors as independent `nmos()`/`pmos()` calls
  placed manually side by side, instead of using `diff_pair()` -- this
  loses the common-centroid interleaving and reintroduces offset from
  process gradients, which is the entire reason a diff pair gets special
  layout treatment instead of just being "two matched transistors."
- Using `diff_pair()` for a pair that shares a *rail* instead of a private
  tail node -- that's not actually a differential pair, don't force it.

## Bipolar variant (NPN/PNP): the long-tailed pair

This is, historically, the *original* differential amplifier topology --
the BJT long-tailed pair predates the MOS differential pair. Same
signature with BJT terms substituted (`base~gate`, `collector~drain`,
`emitter~source`): two same-type BJTs sharing a non-rail emitter (tail),
distinct bases (differential inputs), distinct collectors (differential
outputs). `detect_topology.py` detects this unchanged.

```
Q_L  VOUT1 VP VTAIL  npn ...
Q_R  VOUT2 VN VTAIL  npn ...
```

**glayout support**: as with the current mirror, `npn()`/`pnp()` in
`src/glayout/primitives/bjt.py` are real, working device generators, but
there is **no composite BJT diff-pair module**. Compose one from 2×
`npn()`/`pnp()` with matched, interdigitized placement -- the same
common-centroid reasoning as the MOS `diff_pair()` applies identically
here (a BJT pair's offset is just as sensitive to process/thermal
gradients across the pair as a MOSFET pair's is), it just isn't wired up
as a single ready-made function yet.

**One extra consideration beyond the MOS case**: matched BJT pairs are
also sensitive to *thermal* gradients specifically (not just process
gradients) in a way that's more pronounced than for MOSFETs, because
`V_BE` mismatch translates directly and linearly into input-referred
offset. If the pair sits near a self-heating power device elsewhere in
the layout, common-centroid placement alone may not be enough -- consider
orientation relative to the heat source too.
