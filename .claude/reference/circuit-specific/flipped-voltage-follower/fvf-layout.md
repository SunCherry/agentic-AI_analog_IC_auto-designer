# Flipped Voltage Follower (FVF) — Layout Pattern

## Recognizing it in a netlist
Two same-type MOSFETs forming a feedback loop, fet_1 (input) and fet_2 (feedback):
- `drain(fet_1) == gate(fet_2)` (fet_1's drain sets fet_2's gate -- the feedback node, often called `Ib`)
- `source(fet_1) == drain(fet_2)` (the follower's output node, `VOUT`)

```
M1  Ib   VIN  VOUT  VBULK  nfet ...   <- input fet
M2  VOUT Ib   VBULK VBULK  nfet ...   <- feedback fet
```

This is a tighter, less common signature than current mirror/diff pair --
if `detect_topology.py` reports an FVF match, double check it against the
netlist by hand before committing to the layout, since a false positive
here is more consequential (FVF has a very specific low-voltage-headroom
use case, not a generic building block).

## glayout call
Module: `src/glayout/cells/elementary/FVF/fvf.py`

```python
from glayout.cells.elementary.FVF.fvf import flipped_voltage_follower

fvf = flipped_voltage_follower(
    pdk,
    device_type="nmos",      # or "pmos" -- both fets share one polarity
    placement="horizontal",  # or "vertical"
    width=(6.6, 3.7),        # (input fet, feedback fet)
    length=(2.4, 2.0),       # (input fet, feedback fet)
    fingers=(1, 1),
    multipliers=(2, 2),
)
```
Ports: `fvf_netlist()` produces `VIN, VBULK, VOUT, Ib`.

## Why it's laid out this way
- Unlike current mirror/diff pair, the two fets in an FVF are **not**
  matched devices -- they play asymmetric roles (input transistor vs.
  feedback transistor) and are commonly sized differently (the default
  args above use different width/length for each), so there is no
  common-centroid requirement here either.
- `placement="horizontal"` vs `"vertical"` controls whether the two fets
  sit side by side or stacked -- pick based on which orientation makes the
  feedback-node (`Ib`) routing shortest in your surrounding layout, since
  that node is inside a tight local feedback loop and benefits from
  minimal parasitic capacitance/resistance.
- Both fets' dummies tie to `VBULK` -- this cell assumes a single shared
  bulk/well for both devices (consistent with them being the same
  polarity), unlike the transmission gate's per-device bulk rings.

## Common mistakes
- Assuming the two fets should be matched/interdigitated like a current
  mirror -- they shouldn't; sizing is intentionally asymmetric to the
  circuit's DC operating point.
- Ignoring the `Ib` feedback-node routing length -- since this loop sets
  the FVF's speed/stability, it's the one node in this cell most sensitive
  to added parasitic capacitance.

## No bipolar variant

Unlike current mirror/differential pair/cascode mirror/quad pair, this
pattern is MOS-specific -- `detect_topology.py`'s `is_fvf()` explicitly
excludes BJT kinds rather than just checking `kind` equality (see
`cascode-current-mirror-layout.md`'s bipolar section, which hits this
same restriction). The FVF's whole benefit is that a MOSFET gate draws no
DC current, so the feedback device's gate voltage is set purely by the
input device's drain with no loading; a BJT base draws real base current,
so the same feedback loop doesn't deliver the same low-voltage-headroom
benefit -- it becomes a different (and less clean) circuit, not a
bipolar FVF. If a BJT netlist has this exact connectivity shape,
`detect_topology.py` deliberately leaves it unclassified rather than
mislabeling it.
