# Sub-Circuit Layout Reference

## Elementary topologies (auto-detected by `script/detect_topology.py`)

| Topology | Netlist signature | glayout module | Ports | Bipolar (NPN/PNP)? |
|---|---|---|---|---|
| Current mirror | A diode-connected (`gate==drain`); B (or B1..BN) shares A's gate + source, distinct drain(s) | `cells/elementary/current_mirror/current_mirror.py` → `current_mirror()`, once per reference/mirror leg | VREF, VOUT, VSS, B | Yes — same signature (base~gate/collector~drain/emitter~source); no composite BJT module, compose from `npn()`/`pnp()` primitives |
| Differential pair | A, B same type, shared (non-rail) source, distinct gates, distinct drains | `cells/elementary/diff_pair/diff_pair.py` → `diff_pair()` | VP, VN, VDD1, VDD2, VTAIL, B | Yes — the "long-tailed pair", historically the original diff-amp topology; no composite BJT module |
| Transmission gate | 1 nfet + 1 pfet, shared drain, shared source, distinct gates | `cells/elementary/transmission_gate/transmission_gate.py` → `transmission_gate()` | VIN, VSS, VOUT, VCC, VGP, VGN | No — relies on a MOSFET gate's DC isolation; no bipolar equivalent |
| Flipped voltage follower | A, B same type; `drain(A)==gate(B)` and `source(A)==drain(B)` | `cells/elementary/FVF/fvf.py` → `flipped_voltage_follower()` | VIN, VBULK, VOUT, Ib | No — relies on a MOSFET gate drawing no DC current; `detect_topology.py`'s `is_fvf()` explicitly excludes BJT kinds |
| Quad pair (cross-coupled linearized diff pair) | Two diff-pair-like half-pairs on separate tails, same input/output net pairs, **inverted** gate→drain mapping between halves | no dedicated module — compose from 2× `diff_pair()` + manual cross-coupled drain routing | VIP, VIN, OUTP, OUTN, TAIL1, TAIL2, B | Yes — same linearization principle applies to BJT long-tailed pairs |
| Cascode current mirror (stacked-diode variant) | Two independently diode-connected devices stacked in series (reference), mirrored by a second stacked pair | no dedicated module — compose from 4× `nmos()`/`pmos()`, manually stacked | VB1, VB2, VSS, IOUT | Yes — compose from 4× `npn()`/`pnp()`; mind compounding base-current error at each stacked level |
| Cascode current mirror (FVF-biased / low-voltage variant) | Two FVF instances whose `Ib` nodes bias a stacked 2-level output branch | `cells/composite/low_voltage_cmirror/low_voltage_cmirror.py` → `low_voltage_cmirror()` | IBIAS1, IBIAS2, GND, IOUT1, IOUT2 | No — inherits the FVF's MOS-only restriction |

See `circuit-specific/<topology>/*.md` for the full layout rationale,
bipolar-variant details, and common mistakes for each.

## Composite cells (combinations of the above — not auto-detected)

These are built from the elementary cells above; `detect_topology.py` will
report their elementary sub-patterns individually (e.g. "diff pair" +
"two current mirrors"), but recognizing *which composite cell* that
combination corresponds to is a judgment call, not a pattern match. Use
this table to go from "I see a diff pair feeding a current-mirror load,
plus a bias branch" to the right composite module.

| Composite cell | Module | What it combines |
|---|---|---|
| Differential-to-single-ended converter | `cells/composite/differential_to_single_ended_converter/` | shared-gate PMOS load pair that converts a diff pair's two outputs into one single-ended node |
| Diff pair + current-mirror bias | `cells/composite/diffpair_cmirror_bias/diff_pair_cmirrorbias.py` → `diff_pair_ibias()` | a `diff_pair()` with its tail current sourced from a `current_mirror()` |
| Two-stage opamp (Miller-compensated) | `cells/composite/opamp/opamp.py`, `opamp_twostage.py` | diff pair + current-mirror load (stage 1) + gain stage + Miller cap/res (stage 2) — this is the pattern behind a reference design |
| FVF-based class-AB OTA | `cells/composite/fvf_based_ota/ota.py` → `super_class_AB_OTA()` | FVF-based bias + cascode current mirrors + output stage |

**Not actually a cascode mirror, despite the name**:
`cells/composite/stacked_current_mirror/stacked_current_mirror.py` →
`stacked_nfet_current_mirror()` returns two side-by-side single-level
`nmos()` components (a reference + a mirror), not a real series-stacked
cascode structure. It's used elsewhere (`diff_pair_stackedcmirror.py`) as
a diff pair's plain non-cascode tail-current source. See
`circuit-specific/cascode-current-mirror/cascode-current-mirror-layout.md`
for the real cascode-mirror modules (`low_voltage_cmirror()`, or compose
from primitives for the stacked-diode variant).

## Primitives underneath all of the above

| Device | Module |
|---|---|
| nfet / pfet | `primitives/fet.py` → `nmos()` / `pmos()` |
| npn / pnp | `primitives/bjt.py` → `npn()` / `pnp()` -- real, working layout generators (same completeness as `nmos()`/`pmos()`: emitter/base/collector regions, well, tap ring, dummies), but **their netlist generation is dead code** (`component.info['netlist']` assignment is commented out in both), so a BJT built from these isn't LVS-comparable out of the box the way `nmos()`/`pmos()`/`mimcap()` are. Their docstrings also both still read "Generic NMOS generator", copy-pasted and never updated. |
| MIM capacitor | `primitives/mimcap.py` → `mimcap()` |
| "Resistor" (diode-connected pfet model — **not** a physical poly/diff resistor) | `primitives/resistor.py` → `resistor()` |
| Guard ring / tap ring | `primitives/guardring.py` → `tapring()` |
| Via array / via stack | `primitives/via_gen.py` |

Note: this repo's example netlists use the selected PDK's generic resistor
(on sky130, `sky130_fd_pr__res_generic_l1`)
(a physical poly resistor) for their `R0`/`XR0` devices, which glayout has
**no generator for** — `primitives/resistor.py`'s `resistor()` builds a
diode-connected pfet instead, an electrically different device. Don't
route a real resistor detection to that function; see
`redraw_layout.py`'s placeholder-rectangle handling of `kind == "res"`
for how this repo currently works around the gap.

## Running the detector

```
python script/detect_topology.py path/to/design.sp
```
