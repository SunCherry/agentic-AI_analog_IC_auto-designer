# Pattern Look-Up Table

The registry `SKILL.md` Step 2 recognizes against. **A pattern exists for
this skill only if it has a row here** — if a structure in the netlist is
not in this table, it is reported under `unmatched_devices`, never
invented as a new pattern name. Adding a pattern means adding a row here
first (see "Registering a new pattern" at the bottom).

Every entry has four fields, and all four are load-bearing:

| Field | What it is |
|---|---|
| **Structure** | The netlist-level signature. Connectivity only — the test a device group must pass, stated so two people reading it reach the same verdict. |
| **Parameters** | Which device parameters govern this pattern's behavior, and what circuit quantity each one sets. This is what makes a recognized pattern actionable for `schematic-sizing`. |
| **Tie group** | Which parameters must be *one shared tunable* across the group, which carry a ratio, which are free. `SKILL.md` Step 3 emits these verbatim. |
| **Watch out** | The confusion this pattern is actually mistaken for, and the disambiguating test. |

**Tie-group vocabulary** (same words in the output file):

- `tied` — one shared tunable value across every device in the group. Changing it changes all of them together. Matching depends on it.
- `ratio_carrier` — the parameter that expresses the intended ratio between legs. Almost always `m`. **Frozen for sizing** (`m` and `nf` are layout levers, never templated — `../../agents/schematic-agent.md` Key Rules), so a ratio that `m` cannot express is a finding for the user, not something to absorb into `w`.
- `free` — tuned per device, independently.
- `none` — the pattern imposes no matching constraint at all; tying its devices would be wrong.

**Detector column** — `auto` means
`../../reference/detect_topology.py` matches it by that name
and you copy its finding; `manual` means you apply the Structure rule
yourself, against the devices the detector left unclassified plus every
R/C in the block (`parse_devices()` returns MOS and BJT **only**, so no
passive pattern can ever be auto-detected — a resistor ladder reaches
neither `findings` nor `unclassified`, and its absence is silent).

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

- **Structure.** Device `REF` is diode-connected (`gate == drain`). Each
  mirror leg `Mi` shares `REF`'s gate net *and* `REF`'s source net, with a
  distinct drain. One reference commonly fans out to several legs — that is
  **one** 1:N finding, not N separate 1:1 mirrors. BJT: base~gate,
  collector~drain, emitter~source; the signature is unchanged.
- **Parameters.** `l` sets output resistance (λ) and matching accuracy —
  long `l` for a bias mirror, and it must be identical across legs or the
  ratio drifts with Vds. Per-copy `w` sets current density / Vdsat and,
  with `l`, the overdrive the whole family runs at. `m` sets the leg ratio
  `I_i / I_ref = m_i / m_ref`. `nf` is layout-only (owned by
  `device-shaper`), never a ratio lever.
- **Tie group.** `tied: [w, l]` across reference + every leg;
  `ratio_carrier: m`; `free: none`.
  **Ratio conflict:** if the netlist as written gives the legs *different*
  `w` (not just different `m`), a single shared `w` cannot reproduce the
  intended ratios. Do not tie them and do not "compensate" with width —
  emit the group with `status: ratio_conflict`, list each leg's as-written
  `w`/`m` and the implied ratio, and say plainly that the ratio is not
  tunable because `m` is frozen. (This is not hypothetical: the
  `two_stage_rz` bias family runs XMN4 236µm / XMN3 102.6µm / XMN5 345µm
  off one gate net.)
- **Watch out.** Two devices sharing only a gate net are not a mirror
  unless one is diode-connected *and* the sources coincide — a shared gate
  with different sources is a bias-distribution net, not a mirror. Legs
  whose function differs wildly (a diff-pair tail vs. an output-stage
  load) are still one electrical family for matching, but say so in the
  finding's note: they are the same tie group and very different roles.

## `cascode_current_mirror` — cascode mirror

Three variants, one pattern ID; record which in `variant`.

- **Structure.**
  - `stacked_diode` (self-biased): reference branch is two *independently*
    diode-connected devices in series (bottom's drain = top's source, both
    `gate == drain`); the output branch is a second stacked pair whose
    bottom mirrors the reference bottom and whose top's gate ties to the
    reference top's drain — bias matched level-for-level.
  - `wide_swing`: same two-level stack, but the cascode gate bias comes
    from a *separate* bias device sized to hold the bottom device just at
    the edge of saturation, not from a diode stack in the signal branch.
    Recognize it by the cascode gate net originating outside the mirror's
    own branches.
  - `fvf_biased` (low-voltage): two FVF instances whose `Ib` nodes bias a
    stacked 2-level output branch instead of a diode reference stack.
- **Parameters.** Bottom-level `l` sets the mirror's own accuracy; the
  cascode (top) device's `l`/`w` set the *output resistance boost*
  (`gm_casc · ro_casc`) and cost headroom (`Vdsat_casc`) — that trade is
  the whole point of the pattern, so its `w` is a real design knob, not a
  matching artifact. `m` per branch sets the ratio, applied to **both**
  levels of a branch together.
- **Tie group.** **Two** groups, level-for-level, never one group of four:
  `tied: [w, l]` across all bottom devices, and a separate
  `tied: [w, l]` across all cascode devices. `ratio_carrier: m`, and a
  branch's two levels must carry the *same* `m` — flag it if they don't.
  Bottom and cascode levels are independent of each other.
- **Watch out.** Do not report the constituent bottom pair *also* as a
  plain `current_mirror` (the detector already consumes it; a manual pass
  must too). A two-device stack in the signal path with only one diode
  connection is a `cascode_stage`, not a cascode mirror.

## `differential_pair` — differential (long-tailed) pair

- **Structure.** Same-type devices A, B share a source net (the tail),
  with distinct gates and distinct drains. The shared source must **not**
  be a supply rail — two unrelated stages both sitting on VSS are not a
  pair. Tail is normally driven by a `current_mirror` leg.
- **Parameters.** `w` and `l` set `gm1` — which sets stage-1 gain *and*
  UGBW (`gm1 / (2π·Cc)` in a Miller-compensated amp), the most contended
  parameters in a typical OTA. `l` also sets input-referred flicker noise
  and offset (both improve with area `w·l`). `m` scales the whole pair's
  bias current and gm together.
- **Tie group.** `tied: [w, l]`, and **`m` and `nf` tied as well** — this
  is the one pattern where the ratio carrier is itself tied, because the
  two halves must be *identical*, not ratioed. `ratio_carrier: none`,
  `free: none`. Asymmetry here is offset, directly.
- **Watch out.** A `cross_coupled_pair` also shares a tail; the
  disambiguator is where the gates come from — a diff pair's gates are
  driven from outside the pair, a cross-coupled pair's gates come from the
  pair's own drains. A pair that is one half of a `quad_pair` must be
  reported as the quad, not as two diff pairs.

## `quad_pair` — cross-coupled linearized diff pair

- **Structure.** Two diff-pair-like half-pairs on *separate* tail nodes,
  driving the same two input nets and the same two output nets, with the
  gate→drain mapping **inverted** between the halves (each output sums one
  device from each half with opposite input polarity). That inversion is
  what cancels third-order distortion.
- **Parameters.** Same as `differential_pair` for gm/noise; the *ratio
  between* the two half-pairs' `w` (or `m`) sets the linearization point —
  a deliberate asymmetry between halves, if the design uses one.
- **Tie group.** `tied: [w, l]` across all four devices when the halves
  are equal-sized. If the netlist gives the two halves deliberately
  different `w`, emit **two** tie groups (one per half, each internally
  tied) and record the inter-half ratio as a design intent, not a defect.
- **Watch out.** Reporting it as two independent diff pairs is
  individually misleading — each half looks like a normal pair, and the
  cross-coupling (the entire reason the circuit exists) disappears.

## `cascode_stage` — single-branch cascode (CS + CG stacked)

- **Structure.** Same-type devices A (bottom) and B (top) with
  `B.source == A.drain`. A's gate carries the signal; B's gate is a fixed
  bias (a net that is neither A's drain nor a signal node). Distinguishes
  from a cascode *mirror* by there being no mirrored second branch.
- **Parameters.** Bottom `w`/`l` set gm (gain, noise). Cascode `w`/`l` set
  the `gm·ro` boost and the headroom cost; the cascode's own gm barely
  affects gain, so `w` here is chosen for Vdsat, not transconductance.
- **Tie group.** `none` — the two devices are in series carrying the same
  current, but they do **not** need matched geometry, and tying them
  removes a real degree of freedom. Only tie a cascode device to *another
  branch's* cascode device (see `cascode_current_mirror`).
- **Watch out.** Two stacked devices where the top is diode-connected are
  a bias stack (`self_biased_reference` / cascode mirror reference), not a
  gain cascode.

## `common_source_stage` — common-source gain device

- **Structure.** One device: signal on the gate, source on a supply rail
  (or a degeneration element to a rail), drain at a high-impedance output
  node loaded by a current source or resistor. Typically arrives as
  `unclassified` from the detector.
- **Parameters.** `w`/`l` set `gm` and `ro` → this stage's gain
  `gm·(ro ∥ ro_load)`. In a Miller amp the second-stage CS device's gm
  also sets the RHP zero at `gm/Cc` — the zero `rc_compensation_network`'s
  resistor exists to cancel — so its `w` is coupled to phase margin, not
  just gain.
- **Tie group.** `none`. Free device; a prime sizing lever.
- **Watch out.** A CS device whose gate happens to sit on a bias net is
  really a current-source load — check whether the gate net is signal or
  bias before calling it a gain stage. Both may exist on one node (a CS
  gain device and its current-source load share the output net); they are
  two separate findings.

## `common_gate_stage` — common-gate / current buffer

- **Structure.** One device: gate on a fixed bias, signal injected at the
  **source**, output at the drain.
- **Parameters.** `w` sets input impedance `1/gm` (the spec this stage is
  usually chosen for); `l` sets output resistance.
- **Tie group.** `none`.
- **Watch out.** Structurally identical to the top device of a
  `cascode_stage`; the difference is whether the source node is a signal
  input (common-gate) or another device's drain (cascode). Report it as
  part of the cascode when a bottom device is stacked underneath.

## `source_follower` — common-drain buffer

- **Structure.** One device: signal on the gate, drain on a supply rail,
  output taken at the **source**, which is loaded by a current source (not
  a rail).
- **Parameters.** `w` sets `gm` → output impedance `1/gm` and the level
  shift `Vgs`; body effect on `Vt` moves that shift, so bulk connection
  matters to whether the spec is met.
- **Tie group.** `none`.
- **Watch out.** If the output node also feeds back into another device's
  gate that in turn drives this device's source, it is a
  `flipped_voltage_follower`, which behaves very differently.

## `flipped_voltage_follower` — FVF

- **Structure.** Same-type devices A, B where `A.drain == B.gate` **and**
  `A.source == B.drain` — the shunt feedback loop that defines an FVF.
  MOS-only by construction (relies on a gate drawing no DC current).
- **Parameters.** Input device `w` sets the loop gain and hence the
  (very low) output impedance `1/(gm1·gm2·ro)`; feedback device sizing
  sets the bias current and the loop's stability.
- **Tie group.** `none` — the two devices are deliberately asymmetric;
  tying them defeats the topology.
- **Watch out.** A pair of FVFs biasing a stacked output branch is one
  `cascode_current_mirror` (`fvf_biased`), not two FVFs.

## `transmission_gate` — CMOS pass gate

- **Structure.** One nfet + one pfet with coinciding drains **and**
  coinciding sources (they switch the same two nodes), gates distinct and
  complementary. MOS-only.
- **Parameters.** `w_n`/`w_p` set on-resistance and its flatness across
  the input range — the pfet is deliberately the wider of the two
  (roughly the mobility ratio, ~2–3×) so `Ron` stays flat; `l` at minimum
  for both, since channel length only adds `Ron` and charge injection.
- **Tie group.** `tied: [l]` only, across the n and p device.
  **`w` is explicitly NOT tied** — the n/p width ratio is the design
  intent of this pattern. Record the as-written `w_p/w_n` ratio in the
  finding.
- **Watch out.** Don't force it into the "matched pair ⇒ share W" habit
  that `differential_pair` and `current_mirror` establish; here that habit
  is wrong.

## `cross_coupled_pair` — latch / negative resistance

- **Structure.** Same-type devices A, B with `A.gate == B.drain` and
  `B.gate == A.drain`, sources shared. Presents `−2/gm` at the drains.
- **Parameters.** `w`/`l` set `gm` → the negative resistance magnitude
  (oscillator start-up condition, or latch regeneration time constant).
- **Tie group.** `tied: [w, l, m, nf]` — like a diff pair, the two halves
  must be identical; asymmetry here shows up as a static latch offset or
  as duty-cycle error.
- **Watch out.** Shares a tail like a diff pair. The disambiguator is gate
  origin: cross-coupled gates come from the pair's own drains.

## `push_pull_output` — class-AB complementary output stage

- **Structure.** One nfet + one pfet whose **drains coincide** at the
  output node, sources on opposite rails, gates driven by two *different*
  (usually bias-offset) nets. Contrast `transmission_gate`, where the
  sources also coincide.
- **Parameters.** `w_n`/`w_p` set drive strength per direction and the
  quiescent current; their ratio sets slew symmetry. `l` at/near minimum
  for drive.
- **Tie group.** `none` by default. If the design intends symmetric
  slew, the n/p `w` ratio is a derived constraint (`w_p ≈ (µn/µp)·w_n`) —
  record it as a note, not as a tie.
- **Watch out.** Reads as two unrelated single-device stages to any
  per-device scan; only the shared drain node reveals it.

## `diode_connected_load` — diode-connected load device

- **Structure.** One device with `gate == drain`, whose drain node is a
  *signal* node (not a bias net feeding other gates). If other devices'
  gates hang off that node, it is a `current_mirror` reference instead.
- **Parameters.** `w`/`l` set `1/gm`, which **is** the load resistance —
  so this device sets the stage's gain ratio `gm_in/gm_load`, a ratio that
  is process-insensitive by construction.
- **Tie group.** `none` standalone.
- **Watch out.** This is the single most common misread in the table: a
  diode-connected device is a mirror reference *only if something mirrors
  it*. Check for gate fan-out before choosing.

## `self_biased_reference` — stacked diode bias leg

- **Structure.** A diode-connected nfet and a diode-connected pfet in
  series between the rails (`gate == drain` on both, drains tied to the
  same net), with no external `Ibias` source anywhere in the netlist. That
  shared net is the bias node gating the rest of the design's mirrors.
- **Parameters.** The **smaller** device's `w`/`l` dominates: it sets the
  reference current for every mirror family downstream, and therefore
  total supply current and the Power spec. Very high sensitivity —
  a small device with outsized influence.
- **Tie group.** `none` (n and p are different device types). But record
  the downstream dependency explicitly: this leg's sizing propagates into
  every `current_mirror` group whose reference gate sits on this node.
- **Watch out.** Easy to dismiss as a small unimportant device. Also: it
  makes the bias PVT-dependent (no PTAT/bandgap reference), which is worth
  saying out loud when a spec has a supply- or temperature-sensitivity
  key.

## `beta_multiplier_bias` — constant-gm bias core

- **Structure.** Two cross-linked current mirrors (an nfet mirror and a
  pfet mirror, each providing the other's bias) with a **resistor
  degenerating the source** of one nfet leg, and a deliberate `m` ratio
  (classically 1:K, K≈4) between that leg and its reference.
- **Parameters.** `R` and the mirror ratio `K` set the bias current
  `I ≈ 2/(µCox·(W/L)·R²)·(1−1/√K)²`, i.e. `gm ≈ 2(√K−1)/R` — the design
  goal is a `gm` set by a resistor, not by process. The nfet pair's `w/l`
  is what `R` is trading against.
- **Tie group.** The two mirrors are ordinary `current_mirror` tie groups
  (`tied: [w, l]`, `ratio_carrier: m`), plus a **cross-group constraint**:
  the K ratio and `R` must move together, so record `R` as a co-dependent
  parameter of the nfet group. Never tie a degenerated leg's `w` to its
  reference without carrying the ratio.
- **Watch out.** Has a degenerate zero-current start-up state; check for a
  start-up device and report its absence as a finding.

## `resistor_ladder` — series resistor string / divider

- **Structure.** Three or more resistors chained head-to-tail (each one's
  second net is the next one's first, no other device on the interior taps
  except high-impedance gates), between two references. Two resistors in
  series with a single tap is the degenerate case — report it as
  `resistor_ladder` with `n_taps: 1` and note it reads as a plain divider.
- **Parameters.** Unit `w` and `l` set the unit resistance
  (`R = ρ_sheet·l/w`) and its matching (σ improves with `√(w·l)`); `w`
  additionally sets current density / self-heating limits. The **tap
  ratios** come from how many unit segments sit below each tap — never
  from per-segment `w`/`l` differences.
- **Tie group.** `tied: [w, l]` across **every** segment; `ratio_carrier:
  m` (or series segment count). A ladder whose segments have unequal `w`
  or `l` does not match and its tap voltages will not track over process
  and temperature — emit `status: ratio_conflict` and say so.
- **Watch out.** Invisible to `detect_topology.py` (no resistor parsing at
  all), so it will be silently missing unless the manual pass looks. Also:
  a resistor in a feedback path or a nulling resistor is not a ladder —
  interior taps must actually be used.

## `rc_compensation_network` — Miller cap + nulling resistor

- **Structure.** A capacitor bridging two high-impedance gain nodes (a
  stage's input and the following stage's output), in series with a small
  resistor. Recognize by the cap's two nets being the outputs of two
  consecutive gain stages — not by device name.
- **Parameters.** `Cc` (cap `w`·`l` × the PDK's fF/µm²) sets pole
  splitting and directly sets UGBW = `gm1/(2π·Cc)`; `R` sets the RHP-zero
  cancellation `R ≈ 1/gm2` (of the stage-2 gain device), which is what
  recovers phase margin. Both are geometry, so both are sizing knobs.
- **Tie group.** `none`, but with a hard **cross-pattern dependency**:
  `R` must track `1/gm2` of the `common_source_stage` it compensates, and
  `Cc` trades against the `differential_pair`'s `gm1`. Record both links —
  a sizing pass that moves `gm1` or `gm2` without revisiting this network
  will move UGBW and PM together and look inexplicable.
- **Watch out.** Structurally invisible (R and C both unparsed); this is
  the standing example of the passive blind spot. Also, this is the
  pattern that most often explains a phase-margin spec failure, so its
  absence from a report is expensive.

## `capacitor_bank` — matched-ratio capacitor array

- **Structure.** Two or more capacitors sharing one plate net (bottom or
  top), with the other plates going to distinct switch/tap nodes —
  a CDAC, a switched-capacitor gain network, or a binary-weighted array.
- **Parameters.** Unit `w`/`l` set unit capacitance and matching
  (σ improves with area); binary weights come from **unit count**, not
  from scaling one device's dimensions.
- **Tie group.** `tied: [w, l]` across all units; `ratio_carrier: m`
  (unit count). A "binary-weighted" bank implemented as one big cap with a
  scaled `w` will not match — flag it.
- **Watch out.** Same passive blind spot. Also do not sweep a single load
  or compensation cap into this pattern: a bank needs ≥2 caps whose
  ratio is the design intent.

---

## Registering a new pattern

1. Add a row to the Index and a full four-field section, in the same
   order and voice as the existing entries.
2. State the Structure as a connectivity test, not as a name or an
   intention. "The gates come from the pair's own drains" is a test;
   "used as a latch" is not.
3. If `detect_topology.py` cannot match it, say `manual` — do **not**
   claim auto-detection the detector does not implement. Adding real
   auto-detection means editing
   `../../reference/detect_topology.py`, which is a shared file and serves
   layout generation too; that is a
   deliberate cross-skill change, not a side effect of registering a
   pattern here.
4. Give the tie group a reason. A pattern whose Tie group is `none` is
   still worth registering — "these devices must NOT be tied" is exactly
   as useful to `schematic-sizing` as a tie.
