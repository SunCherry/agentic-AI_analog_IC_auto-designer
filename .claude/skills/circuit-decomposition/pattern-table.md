# Pattern Look-Up Table

The registry `SKILL.md` Step 2 matches against. **A pattern exists only if it
has a row here** — anything else goes to `unmatched_devices`, never a name
invented inline. New pattern ⇒ new row first ("Registering", bottom).

Five fields per entry, all load-bearing:

| Field | What it is |
|---|---|
| **Structure** | The connectivity test, stated so two readers reach the same verdict. Never a name or an intention. |
| **Parameters** | Which device parameter sets which circuit quantity — what makes the pattern actionable for `schematic-sizing`. |
| **Tie group** | Which parameters are *one shared tunable*, which carry the ratio, which are free. Step 3 emits verbatim. |
| **Matched nets** | Which NETS carry a matching constraint. The tie group's other half — matched devices wired asymmetrically are not matched. Step 4 emits verbatim; read by layout, not by sizing. |
| **Watch out** | What this is actually mistaken for, and the disambiguating test. |

**Tie-group words:** `tied` — one shared tunable across the group ·
`ratio_carrier` — the parameter carrying the ratio, almost always `m`, and
**TUNABLE for sizing wherever a pattern names it** (one shared unit `w`/`l`
times a per-leg integer `m` — the same thing the layout draws). `m` stays
frozen on every device no pattern names a carrier for, and `nf` is always
frozen: it is `device-shaper`'s, never a ratio lever · `free` — per device ·
`none` — no constraint; tying would be wrong.

These fields are **read by `script/build_tunables.py`**, which emits
`tie_groups`, `tunable_parameters` and the `.sp.j2` template from them. A
row here is not documentation of a judgment call — it *is* the rule that
runs. Changing a row changes what sizing may move.

**Matched-net words:** `differential` — twins; mirror-image routing, same
layer/length/via count/neighbours · `complementary` — equal RC, opposite
phase (`clk`/`clkb`); no mirror symmetry · `common` — one net the group
shares; symmetric access and low IR drop along it · `branch` — one net per
leg of a *ratioed* family; twins only at 1:1, so always say which ·
`sensitive` — matched to nothing, but its own parasitics set a spec · `none`.

Step 1 prints `differential`/`complementary`/`branch`/`common` candidates for
**auto** patterns only, terminal by terminal (`script/match_nets.py`); manual
patterns get none, and `sensitive` is never auto-proposed.

**Detector column:** `auto` — `../../reference/detect_topology.py` matches it
by that name; copy the finding. `manual` — apply the Structure test yourself
to the detector's `unclassified` **plus every R/C in the block**:
`parse_devices()` returns MOS/BJT only, so a passive pattern reaches neither
`findings` nor `unclassified` and its absence is silent.

---

## Index

| ID | Pattern | Devices | Detector |
|---|---|---|---|
| `current_mirror` | Simple current mirror (1:N) | 2+ same-type MOS/BJT | auto |
| `cascode_current_mirror` | Cascode mirror (3 variants) | 4+ same-type MOS | auto |
| `differential_pair` | Differential (long-tailed) pair | 2 same-type MOS/BJT | auto |
| `quad_pair` | Cross-coupled linearized diff pair | 4 same-type MOS/BJT | auto |
| `cascode_stage` | Single-branch cascode (CS + CG stacked) | 2 same-type MOS | manual |
| `common_source_stage` | Common-source gain device | 1 MOS | manual |
| `common_gate_stage` | Common-gate / current buffer | 1 MOS | manual |
| `source_follower` | Common-drain buffer | 1 MOS | manual |
| `flipped_voltage_follower` | FVF | 2 same-type MOS | auto |
| `transmission_gate` | CMOS pass gate | 1 nfet + 1 pfet | auto |
| `cross_coupled_pair` | Latch / negative-resistance pair | 2 same-type MOS | manual |
| `push_pull_output` | Class-AB complementary output | 1 nfet + 1 pfet | manual |
| `diode_connected_load` | Diode-connected load device | 1 MOS | manual |
| `self_biased_reference` | Stacked diode bias leg (no external Ibias) | 2 complementary MOS | manual |
| `beta_multiplier_bias` | Constant-gm bias core | 4 MOS + 1 R | manual |
| `resistor_ladder` | Series resistor string / divider | 2+ R | manual |
| `rc_compensation_network` | Miller cap + nulling resistor | 1 C + 1 R | manual |
| `capacitor_bank` | Matched-ratio capacitor array | 2+ C | manual |

---

## `current_mirror` — simple current mirror (1:N)

- **Structure.** `REF` diode-connected (`gate == drain`); every leg shares
  REF's gate *and* source net, distinct drains. One REF fanning out to N legs
  is **one** 1:N finding, not N mirrors. BJT: b~g, c~d, e~s.
- **Parameters.** `l` → ro (λ) and matching accuracy; identical across legs
  or the ratio drifts with Vds. `w` → current density / Vdsat and the
  family's overdrive. `m` → leg ratio `I_i/I_ref = m_i/m_ref`. `nf` is
  layout-only (`device-shaper`), never a ratio lever.
- **Tie group.** `tied: [w, l]` across REF + legs; `ratio_carrier: m`;
  `free: none`. Legs written with different `w` are **not** a conflict: the
  group re-expresses as one shared *unit* `w`/`l` and a per-leg integer `m`,
  seeded so each leg's original TOTAL width is preserved to within one unit.
  (Real: `two_stage_rz` runs 236 / 102.6 / 345 µm off one gate net → unit
  w = 47.2, m = 5 / 2 / 7, i.e. 236 / 94.4 / 330.4.) `ratio_seed` records
  the residual per leg. Sizing then moves the shared `w`/`l` and each `m`.
  **Never give a mirror's legs independent widths** — three unit devices on
  one gate net is not a mirror, and no layout can match it.
- **Matched nets.** Gate and source nets `common`. The gate net is the one
  that bites: no DC current, so its resistance is invisible to simulation,
  yet each leg samples `Vgs` where *it* taps — one low-R spine with symmetric
  taps, never a daisy chain outward from REF. Leg drains `branch`.
- **Watch out.** A shared gate alone is not a mirror: one device must be
  diode-connected *and* the sources coincide, else it is a bias-distribution
  net. Legs of very different function (diff-pair tail vs. output load) are
  still one tie group — note the roles.

## `cascode_current_mirror` — cascode mirror

Three variants, one ID; record which in `variant`.

- **Structure.** `stacked_diode`: reference branch is two *independently*
  diode-connected devices in series (bottom drain = top source, both
  `gate == drain`); the output branch's bottom mirrors the reference bottom,
  its top's gate ties to the reference top's drain. · `wide_swing`: same
  stack, cascode gate bias from a *separate* device — recognize it by that
  gate net originating outside the mirror's branches. · `fvf_biased`: two
  FVFs whose `Ib` nodes bias a stacked output branch.
- **Parameters.** Bottom `l` → mirror accuracy. Cascode `w`/`l` → the ro
  boost (`gm_casc·ro_casc`) and headroom cost (`Vdsat_casc`) — that trade is
  the point of the pattern, so cascode `w` is a real knob. `m` per branch
  applies to **both** levels.
- **Tie group.** **Two** groups, level-for-level, never one of four:
  `tied: [w, l]` across bottoms, separately across cascodes.
  `ratio_carrier: m`; a branch's two levels must carry the same `m` — flag it
  if they don't. Levels are independent.
- **Matched nets.** Two `common` gate nets, one per level, same
  spine-not-chain rule. The per-branch internal node (bottom drain = cascode
  source) is `branch`, and `sensitive` in itself: its capacitance sets the
  non-dominant pole. Output drains `branch`.
- **Watch out.** Do not also report the bottom pair as a plain
  `current_mirror` (the detector consumes it; a manual pass must too). A
  signal-path stack with only one diode connection is a `cascode_stage`.

## `differential_pair` — differential (long-tailed) pair

- **Structure.** Same-type A, B share a source net (tail), distinct gates,
  distinct drains. The shared source must **not** be a rail — two unrelated
  stages on VSS are not a pair. Tail is normally a `current_mirror` leg.
- **Parameters.** `w`/`l` → `gm1` → stage-1 gain *and* UGBW `gm1/(2π·Cc)`;
  typically an OTA's most contended parameters. `l` also sets flicker noise
  and offset (both improve with `w·l`). `m` scales bias current and gm.
- **Tie group.** `tied: [w, l]`, **plus `m` and `nf`** — the one pattern
  where the ratio carrier is itself tied, the halves being *identical*, not
  ratioed. `ratio_carrier: none`, `free: none`. Asymmetry is offset, directly.
- **Matched nets.** Two `differential` pairs — the **gate** nets (input) and
  the **drain** nets (output) — routed as mirror images about the pair's
  axis: a difference between twins is offset and CMRR loss that identical
  devices cannot cancel, since 20µm of extra met1 on one gate is series R and
  coupling C the other half never sees. **Tail** is `common`: both halves
  must reach it identically or the tail current splits before any device
  mismatch does it. In a fully differential stage carry the pairing outward
  through the load and compensation nets, not just the pair's own terminals.
- **Watch out.** A `cross_coupled_pair` also shares a tail; the disambiguator
  is gate origin — a diff pair's gates are driven from outside the pair. A
  pair that is half of a `quad_pair` is reported as the quad.

## `quad_pair` — cross-coupled linearized diff pair

- **Structure.** Two diff-pair-like halves on *separate* tails, driving the
  same two input and same two output nets, gate→drain mapping **inverted**
  between halves (each output sums one device from each half at opposite
  polarity). That inversion cancels third-order distortion.
- **Parameters.** As `differential_pair`; the *ratio between* the halves' `w`
  (or `m`) sets the linearization point, where the design uses one.
- **Tie group.** `tied: [w, l]` across all four when the halves are equal.
  Deliberately different halves ⇒ **two** groups, each internally tied, with
  the inter-half ratio recorded as intent, not a defect.
- **Matched nets.** The same two `differential` pairs as `differential_pair`,
  plus one it lacks: the two **tail** nets are separate nodes and themselves a
  `differential` pair. Matching them keeps the halves' bias equal, without
  which the distortion cancellation does not happen.
- **Watch out.** Reported as two diff pairs, each half looks normal and the
  cross-coupling — the reason the circuit exists — disappears.

## `cascode_stage` — single-branch cascode (CS + CG stacked)

- **Structure.** Same-type A (bottom), B (top), `B.source == A.drain`. A's
  gate carries signal; B's gate is a fixed bias (neither A's drain nor a
  signal node). No mirrored second branch — that is a cascode mirror.
- **Parameters.** Bottom `w`/`l` → gm (gain, noise). Cascode `w`/`l` → the
  `gm·ro` boost and headroom; its own gm barely affects gain, so its `w` is
  chosen for Vdsat.
- **Tie group.** `none` — same current, but no matched geometry needed and
  tying removes a real degree of freedom. Tie a cascode only to *another
  branch's* cascode (`cascode_current_mirror`).
- **Matched nets.** `none` — one branch, nothing to twin with. The internal
  node is `sensitive`: this pattern's non-dominant pole, so routing area on
  it is a phase-margin cost. Keep the cascode gate bias off aggressor routes.
- **Watch out.** A stack whose top is diode-connected is a bias stack
  (`self_biased_reference` / cascode mirror reference), not a gain cascode.

## `common_source_stage` — common-source gain device

- **Structure.** One device: signal on the gate, source on a rail (or a
  degeneration element to one), drain at a high-impedance output loaded by a
  current source or resistor. Usually arrives `unclassified`.
- **Parameters.** `w`/`l` → `gm`, `ro` → gain `gm·(ro ∥ ro_load)`. In a
  Miller amp the second stage's gm also sets the RHP zero `gm/Cc` that
  `rc_compensation_network` cancels, so its `w` is coupled to phase margin.
- **Tie group.** `none`. Free device; a prime sizing lever.
- **Matched nets.** `none` standalone. The **drain** is `sensitive`: high
  impedance, so its capacitance sets this stage's pole, and in a Miller amp
  it is one end of `Cc`. As half of a differential stage its drain is twinned
  on that stage's pair, not here.
- **Watch out.** A CS device whose gate sits on a bias net is a
  current-source load — check signal vs. bias first. Both can share one
  output node; they are two findings.

## `common_gate_stage` — common-gate / current buffer

- **Structure.** One device: gate on a fixed bias, signal injected at the
  **source**, output at the drain.
- **Parameters.** `w` → input impedance `1/gm` (the spec this stage is chosen
  for); `l` → ro.
- **Tie group.** `none`.
- **Matched nets.** `none`. The **source** is `sensitive`, opposite to a CS
  drain: low-impedance signal input, so series routing R adds directly to the
  `1/gm` bought here.
- **Watch out.** Structurally identical to a `cascode_stage`'s top device;
  the difference is whether the source is a signal input or another device's
  drain. With a bottom device stacked underneath, report the cascode.

## `source_follower` — common-drain buffer

- **Structure.** One device: signal on the gate, drain on a rail, output at
  the **source**, loaded by a current source (not a rail).
- **Parameters.** `w` → `gm` → output impedance `1/gm` and the level shift
  `Vgs`; body effect moves `Vt` and hence that shift, so the bulk connection
  matters to the spec.
- **Tie group.** `none`.
- **Matched nets.** `none`. The **source** (output) carries the load current
  — size it for IR drop, not matching.
- **Watch out.** If the output feeds a gate that in turn drives this device's
  source, it is a `flipped_voltage_follower`, which behaves very differently.

## `flipped_voltage_follower` — FVF

- **Structure.** Same-type A, B with `A.drain == B.gate` **and**
  `A.source == B.drain` — the shunt feedback loop. MOS-only (relies on a gate
  drawing no DC current).
- **Parameters.** Input `w` → loop gain, hence output impedance
  `1/(gm1·gm2·ro)`; feedback device sizing sets bias current and loop
  stability.
- **Tie group.** `none` — deliberately asymmetric; tying defeats the topology.
- **Matched nets.** `none`, same reason. The feedback node
  (`A.drain == B.gate`) is `sensitive` — capacitance there moves stability,
  not just bandwidth.
- **Watch out.** Two FVFs biasing a stacked output branch are one
  `cascode_current_mirror` (`fvf_biased`), not two FVFs.

## `transmission_gate` — CMOS pass gate

- **Structure.** One nfet + one pfet with coinciding drains **and**
  coinciding sources (they switch the same two nodes), gates distinct and
  complementary. MOS-only.
- **Parameters.** `w_n`/`w_p` → on-resistance and its flatness across the
  input range; the pfet is deliberately wider (~the mobility ratio, 2–3×).
  `l` minimum for both — length only adds `Ron` and charge injection.
- **Tie group.** `tied: [l]` only. **`w` is explicitly NOT tied** — the n/p
  width ratio is this pattern's intent; record the as-written `w_p/w_n`.
- **Matched nets.** The two **gate** nets are `complementary` (`clk`,
  `clkb`): match their RC, since charge injection cancels only if both gates
  switch at the same instant with the same edge. Source and drain are
  `common` by construction.
- **Watch out.** Don't apply the "matched pair ⇒ share W" habit
  `differential_pair` and `current_mirror` establish; here it is wrong.

## `cross_coupled_pair` — latch / negative resistance

- **Structure.** Same-type A, B with `A.gate == B.drain` and
  `B.gate == A.drain`, sources shared. Presents `−2/gm` at the drains.
- **Parameters.** `w`/`l` → `gm` → negative-resistance magnitude (start-up
  condition, latch regeneration time constant).
- **Tie group.** `tied: [w, l, m, nf]` — like a diff pair the halves must be
  identical; asymmetry is a static latch offset or duty-cycle error.
- **Matched nets.** One `differential` pair appearing at **two** terminals:
  each drain is the other's gate, so {`A.drain`,`B.drain`} and
  {`A.gate`,`B.gate`} are the same two nets, swapped. Mirror-image routing;
  sources `common`. (Hence terminal-by-terminal comparison: as *sets* the two
  devices touch an identical pair of nets and nothing appears to differ.)
- **Watch out.** Shares a tail like a diff pair; the disambiguator is gate
  origin — cross-coupled gates come from the pair's own drains.

## `push_pull_output` — class-AB complementary output stage

- **Structure.** One nfet + one pfet whose **drains coincide** at the output,
  sources on opposite rails, gates on two *different* (usually bias-offset)
  nets. Contrast `transmission_gate`, where the sources also coincide.
- **Parameters.** `w_n`/`w_p` → drive strength per direction and quiescent
  current; their ratio sets slew symmetry. `l` at/near minimum.
- **Tie group.** `none` by default. For symmetric slew the n/p ratio
  (`w_p ≈ (µn/µp)·w_n`) is a derived constraint — a note, not a tie.
- **Matched nets.** **Drain** `common` — shared by construction, and wide
  enough for the output current is the real constraint. The two **gate**
  drive nets are `complementary`: different bias, matched RC, or n and p turn
  on at different times and the quiescent current is not the one the class-AB
  bias set.
- **Watch out.** Reads as two unrelated single-device stages to any
  per-device scan; only the shared drain reveals it.

## `diode_connected_load` — diode-connected load device

- **Structure.** One device, `gate == drain`, whose drain is a *signal* node,
  not a bias net feeding other gates. If other gates hang off it, it is a
  `current_mirror` reference instead.
- **Parameters.** `w`/`l` → `1/gm`, which **is** the load resistance — it
  sets the stage's gain ratio `gm_in/gm_load`, process-insensitive by
  construction.
- **Tie group.** `none` standalone.
- **Matched nets.** `none` standalone. If it is a mirror reference after all,
  its gate net is that family's `common` bias spine.
- **Watch out.** The most common misread here: a diode-connected device is a
  mirror reference *only if something mirrors it*. Check gate fan-out first.

## `self_biased_reference` — stacked diode bias leg

- **Structure.** A diode-connected nfet and pfet in series between the rails
  (`gate == drain` on both, drains tied to one net), no external `Ibias`
  anywhere in the netlist. That shared net gates the design's mirrors.
- **Parameters.** The **smaller** device's `w`/`l` dominates: it sets the
  reference current for every downstream mirror family, hence supply current
  and the Power spec. A small device with outsized influence.
- **Tie group.** `none` (different device types). Record the downstream
  dependency: this leg's sizing propagates into every `current_mirror` group
  whose reference gate sits on this node.
- **Matched nets.** The shared **bias node** is `common`, and the
  widest-reaching net of its kind — every mirror family gates off it, so IR
  drop along it is a systematic current error in all of them at once.
- **Watch out.** Easy to dismiss as a small unimportant device. It also makes
  the bias PVT-dependent (no PTAT/bandgap), worth saying when a spec has a
  supply- or temperature-sensitivity key.

## `beta_multiplier_bias` — constant-gm bias core

- **Structure.** Two cross-linked current mirrors (nfet and pfet, each
  biasing the other) with a **resistor degenerating the source** of one nfet
  leg, and a deliberate `m` ratio (classically 1:K, K≈4) between that leg and
  its reference.
- **Parameters.** `R` and K set `I ≈ 2/(µCox·(W/L)·R²)·(1−1/√K)²`, i.e.
  `gm ≈ 2(√K−1)/R` — a `gm` set by a resistor, not by process. The nfet
  pair's `w/l` is what `R` trades against.
- **Tie group.** Two ordinary `current_mirror` groups plus a **cross-group
  constraint**: K and `R` move together, so record `R` as a co-dependent
  parameter of the nfet group. Never tie a degenerated leg's `w` to its
  reference without carrying the ratio.
- **Matched nets.** Each mirror contributes its `common` gate net; the two
  branch nets between them are `branch` at 1:K, not twins. The **degeneration
  node** is `sensitive`: `R` sets `gm` directly, so series routing R lands
  straight on the bias current — connect where that R is smallest, not where
  routing is shortest.
- **Watch out.** Has a degenerate zero-current start-up state; check for a
  start-up device and report its absence as a finding.

## `resistor_ladder` — series resistor string / divider

- **Structure.** Three or more resistors chained head-to-tail (each one's
  second net is the next one's first; nothing but high-impedance gates on the
  interior taps), between two references. Two resistors with a single tap is
  the degenerate case — report it with `n_taps: 1` as a plain divider.
- **Parameters.** Unit `w`/`l` → unit resistance (`R = ρ_sheet·l/w`) and
  matching (σ improves with `√(w·l)`); `w` also sets current density /
  self-heating. **Tap ratios come from segment counts**, never from
  per-segment `w`/`l` differences.
- **Tie group.** `tied: [w, l]` across **every** segment; `ratio_carrier: m`
  (the segment count). Unequal *drawn* segments do not match and the taps
  will not track over process and temperature — which is precisely why the
  group re-expresses as one shared unit segment repeated `m` times, exactly
  as a mirror does. Unit `w`/`l` shared, integer `m` per tap.
- **Matched nets.** Taps are not twins — they sit at different potentials by
  design. What must match is the *wiring per segment*: identical interconnect
  between every adjacent pair, so each unit's parasitic R adds equally. Taps
  driving gates are `sensitive` — nothing drives them low-impedance, so any
  coupling is a direct voltage error.
- **Watch out.** Invisible to `detect_topology.py`, so silently missing
  unless the manual pass looks. A feedback or nulling resistor is not a
  ladder — interior taps must actually be used.

## `rc_compensation_network` — Miller cap + nulling resistor

- **Structure.** A capacitor bridging two high-impedance gain nodes (a
  stage's input and the next stage's output) in series with a small resistor.
  Recognize by the cap's two nets being two consecutive gain stages' outputs
  — not by device name.
- **Parameters.** `Cc` (cap `w`·`l` × the PDK's fF/µm²) sets pole splitting
  and UGBW `gm1/(2π·Cc)`; `R` sets the RHP-zero cancellation `R ≈ 1/gm2` of
  the stage-2 gain device. Both are geometry, so both are sizing knobs.
- **Tie group.** `none`, with a hard **cross-pattern dependency**: `R` tracks
  `1/gm2` of the `common_source_stage` it compensates, `Cc` trades against
  the `differential_pair`'s `gm1`. Record both — moving `gm1`/`gm2` without
  revisiting this network moves UGBW and PM together and looks inexplicable.
- **Matched nets.** Both nets `sensitive`, and that is the pattern's whole
  story: any capacitance the routing adds to either high-impedance node lands
  directly on pole splitting and phase margin, so keep the branch short and
  away from rails and switching nets. In a **fully differential** amp the two
  compensation branches' nets are `differential` twins — asymmetry reads as
  common-mode-dependent phase margin.
- **Watch out.** Structurally invisible (R and C both unparsed) — the
  standing example of the passive blind spot, and the pattern that most often
  explains a phase-margin failure, so its absence is expensive.

## `capacitor_bank` — matched-ratio capacitor array

- **Structure.** Two or more capacitors sharing one plate net, other plates
  on distinct switch/tap nodes — a CDAC, a switched-capacitor gain network, a
  binary-weighted array.
- **Parameters.** Unit `w`/`l` → unit capacitance and matching (σ improves
  with area); binary weights come from **unit count**, not from scaling one
  device.
- **Tie group.** `tied: [w, l]` across all units; `ratio_carrier: m` (unit
  count). A "binary-weighted" bank built as one big cap with scaled `w` will
  not match — flag it.
- **Matched nets.** Shared **plate** `common`. The per-unit **tap** nets
  carry the real constraint: each unit's routing capacitance adds to that
  unit's capacitance, so unequal routing is a weight error in the same units
  as unequal cap area — the LSB's routing must be a scaled copy of the MSB's.
- **Watch out.** Same passive blind spot. Do not sweep a single load or
  compensation cap in here: a bank needs ≥2 caps whose ratio is the intent.

---

## Registering a new pattern

1. Add an Index row and a full five-field section, in the same order and
   voice as the existing entries.
2. State the Structure as a connectivity test. "The gates come from the
   pair's own drains" is a test; "used as a latch" is not.
3. Say `manual` unless `detect_topology.py` really matches it. Real
   auto-detection means editing `../../reference/detect_topology.py`, a
   shared file that also serves layout generation — a deliberate cross-skill
   change, not a side effect of registering a pattern.
4. Give the tie group a reason. `none` is worth registering: "these must NOT
   be tied" is as useful to `schematic-sizing` as a tie.
5. Fill Matched nets even when it is `none` — layout reads this field, and a
   blank one is indistinguishable from "nobody looked". Name the terminal a
   net hangs off, never a net name from one design. For an `auto` pattern,
   check `script/match_nets.py`'s `NET_KIND` (keyed by pattern ID) agrees; a
   pattern missing there yields common nodes only, which is right for a
   deliberately asymmetric pattern and wrong for anything else.
