# Transmission Gate — Layout Pattern

## Recognizing it in a netlist
One nfet + one pfet, D and S both wired in parallel between the same two nodes, gates complementary:
- `drain(nfet) == drain(pfet)` and `source(nfet) == source(pfet)` (they switch the same two nodes)
- `gate(nfet) != gate(pfet)` (complementary clock/enable signals)

```
Mn  VOUT VGN VIN VSS  nfet ...
Mp  VOUT VGP VIN VCC  pfet ...
```

## glayout call
Module: `src/glayout/cells/elementary/transmission_gate/transmission_gate.py`

```python
from glayout.cells.elementary.transmission_gate.transmission_gate import transmission_gate

tg = transmission_gate(
    pdk,
    width=(1, 1),        # (nmos, pmos)
    length=(None, None), # (nmos, pmos); None = min length
    fingers=(1, 1),      # (nmos, pmos)
    multipliers=(1, 1),  # (nmos, pmos)
)
```
Ports: `tg_netlist()` produces `VIN, VSS, VOUT, VCC, VGP, VGN`. All tuple
args are always `(nmos_value, pmos_value)`.

## Why it's laid out this way
- The nfet and pfet are placed side by side sharing the VIN/VOUT routing,
  since they're electrically in parallel across the same two nodes --
  there's no common-centroid requirement here (unlike current mirror/diff
  pair) because a transmission gate isn't a *matched* structure; the n and
  p devices are intentionally different sizes/types, not meant to track
  each other.
- Each fet's dummy fingers tie to that fet's own bulk ring (NMOS bulk =
  VSS, PMOS bulk = VCC) -- this is what the netlist's `DUM` mapping
  encodes, and it matters for LVS: get it backwards and the dummy nets
  won't match the extracted layout.
- Sizing `width`/`fingers` for the nmos and pmos independently is
  deliberate and typical -- pfets need roughly 2-3x the width of an nfet
  for balanced on-resistance across the input swing (electron vs. hole
  mobility), so don't assume `width=(w,w)` is "matched"; matched would
  actually mean intentionally *unequal* n/p widths tuned to equalize Ron.

## Common mistakes
- Sizing nmos and pmos identically expecting "matched" resistance -- see
  above, that produces an *unbalanced* switch, not a matched one.
- Treating this as a common-centroid pair and trying to interdigitate the
  n and p devices -- they're different device types, this doesn't apply.

## No bipolar variant

Unlike current mirror/differential pair/cascode mirror/quad pair, this
pattern is MOS-specific and intentionally isn't generalized to bipolar in
`detect_topology.py`. A transmission gate works because a MOSFET gate is
a DC-isolated control terminal, so pairing complementary threshold
devices gives a switch with no DC path through the control input and
(with matched sizing choices) roughly constant on-resistance across the
input swing. A BJT has no equivalent -- its base is a real current input,
not an isolated gate -- so there's no bipolar structure that plays the
same role. (Circuits that switch signals using BJTs, e.g. some ECL/current-
steering topologies, solve a related problem differently and aren't a
drop-in substitute for this pattern.)
