# Quad Pair (Cross-Coupled Linearized Differential Pair) — Layout Pattern

## What it is
A linearity-improvement variant of the differential pair: instead of one
diff pair carrying the full tail current, the pair is split into **two
half-pairs on separate tail branches**, and their drains are
**cross-coupled** — each output node sums one device from each half-pair,
but with *opposite* input polarity between the two half-pairs. Summing a
"normal" and a "flipped" copy at each output cancels the dominant
odd-order (mainly third-order) distortion term in the differential
transconductance while the linear (first-order) term from both halves
adds constructively. The two half-pairs are usually sized/biased
differently (different tail current and/or device width) — if they were
identical, the cross-coupled sum would just cancel to zero signal, not
partial-cancel the distortion.

## Recognizing it in a netlist
Two "diff-pair-like" half-pairs (each individually satisfies the plain
differential-pair signature — see `differential-pair-layout.md`) that:
- sit on **two different tail nodes** (not one shared tail — that would
  just be a single bigger diff pair)
- drive the **same two input nets** (`VIP`, `VIN`)
- output to the **same two output nets** (`OUTP`, `OUTN`)
- but with the gate→drain mapping **inverted** between the two half-pairs:
  half-pair 1's `VIP`-gated device drives `OUTP`, while half-pair 2's
  `VIP`-gated device drives `OUTN` (and symmetrically for `VIN`)

```
* half-pair 1 (tail1) -- "normal" polarity
M1A  OUTP  VIP  TAIL1  B   nfet ...
M1B  OUTN  VIN  TAIL1  B   nfet ...

* half-pair 2 (tail2) -- cross-coupled ("flipped") polarity
M2A  OUTN  VIP  TAIL2  B   nfet ...    <- same gate net VIP as M1A, but opposite output
M2B  OUTP  VIN  TAIL2  B   nfet ...    <- same gate net VIN as M1B, but opposite output
```

`detect_topology.py` finds this by first collecting every diff-pair-like
candidate pair, then checking every pair-of-candidates for this exact
inverted-mapping relationship. A match consumes both half-pairs into one
`quad_pair` finding (4 devices) instead of reporting two separate,
individually-misleading `differential_pair` findings.

## glayout call
**There is no dedicated composite cell for this in glayout** — unlike
current mirror / diff pair / transmission gate / FVF, which each map to
exactly one elementary cell, a quad pair has to be composed from existing
primitives:

```python
from glayout.cells.elementary.diff_pair.diff_pair import diff_pair

half_pair_1 = diff_pair(pdk, width=W1, fingers=F1, n_or_p_fet=True)
half_pair_2 = diff_pair(pdk, width=W2, fingers=F2, n_or_p_fet=True)
# then, at the parent/composite level:
#   - place half_pair_1 and half_pair_2 with a shared symmetry axis
#     (common-centroid *between* the two half-pairs, not just within each)
#   - route half_pair_1's "VP-side" drain and half_pair_2's "VN-side"
#     drain together to OUTP (and the complementary pair to OUTN) --
#     this is the actual cross-coupling, done at the routing level
#   - each half_pair keeps its own private tail node (do not short the
#     two tails together, or the whole point of separate-tail
#     linearization is lost)
```
If you're drawing this by hand instead of composing two `diff_pair()`
calls, treat each half-pair independently per `differential-pair-layout.md`,
then add the cross-coupling as a parent-level routing step.

## Why it's laid out this way
- **Common-centroid within *and* between the half-pairs.** Each
  `diff_pair()` call already gives common-centroid matching within its own
  half-pair. But the *distortion-cancellation* property additionally
  depends on the two half-pairs tracking each other (their relative
  sizing ratio is what sets the cancellation point), so place the two
  half-pairs symmetrically about a shared axis too — a gradient that
  shifts one half-pair's effective sizing relative to the other directly
  degrades the linearity improvement this whole structure exists for.
- **Keep the two tail nodes electrically separate** in the layout, not
  just in the schematic — an unintentional routing short between TAIL1
  and TAIL2 silently turns this back into one ordinary (non-linearized)
  diff pair.
- **The cross-coupling routing is the one part that isn't
  common-centroid-critical** — OUTP/OUTN are single nodes each already
  receiving contributions from both halves, so route them for minimal
  parasitic capacitance (they're the actual signal outputs) rather than
  for symmetry.

## Common mistakes
- Forgetting the cross-coupling and wiring both half-pairs "in parallel"
  (same polarity both times) — that's just a differential pair split into
  two parallel copies for more tail current, not a linearized quad. Check
  that the gate→drain mapping is actually inverted between the halves.
- Sizing the two half-pairs identically — with matched sizing and matched
  tail current, the cross-coupled sum trends toward zero differential
  output instead of a partially-linearized one; the deliberate asymmetry
  between the halves is what makes this work.
- Shorting the two tail nodes together to "simplify" the layout — this
  collapses the structure back to an ordinary diff pair and defeats the
  entire purpose.

## Bipolar variant (NPN/PNP)

The same cross-coupled-quad linearization principle applies to BJT
long-tailed pairs: two half-pairs on separate emitter (tail) branches,
same two base inputs, same two collector outputs, but with the
base→collector mapping inverted between the halves. `detect_topology.py`
finds this the same way as the MOS case (it pairs up
`differential_pair`-shaped candidates and checks for the inversion,
independent of whether the underlying devices are MOSFETs or BJTs).

As with the plain differential pair, there's no dedicated glayout module
for either the MOS or BJT quad pair -- compose from 2× hand-built diff
pairs (`diff_pair()` calls for MOS, or 2× `npn()`/`pnp()` pairs for
bipolar, per `differential-pair-layout.md`'s bipolar section) plus manual
cross-coupled routing at the parent level.
