---
name: circuit-decomposition
description:
  Read an analog design's top-level netlist and decompose it structurally --
  the `.subckt` hierarchy as a diagram, every circuit pattern registered in
  `pattern-table.md` (current mirror, diff pair, cascode, resistor ladder,
  ...) matched per hierarchy level, and the matched-parameter tie groups
  saying which devices must share one tunable `w`/`l` and which carry their
  ratio in `m`, plus the matched NETS those groups imply -- which wires are
  differential twins layout must route as mirror images, which are one
  shared node. One command scans; the conclusions land in one file,
  `<design_dir>/circuit_decomposition.yaml`. Reads the netlist only, never
  edits it, and reports device SHAPE, not circuit FUNCTION. Use at
  `schematic-agent`'s "circuit understanding" step, or any time a netlist
  must be understood before it is sized.
---

# Circuit Decomposition

**One design, one artifact**, holding conclusions only:
`<design_dir>/circuit_decomposition.yaml` — the `hierarchy` diagram,
`patterns`, `unmatched_devices`, `tie_groups`, `matched_nets`,
`open_questions`. `schematic-agent` and `schematic-sizing` read it to learn
what the circuit contains and **which devices share a tunable parameter**;
`placer` and `router` read it to learn **which nets must match**; it
carries what they act on and nothing else.

The evidence behind them — device tables, shared-net groups, affinity
cliques, raw detector output — is **printed** by Step 1, not stored:
persisting it buried the answers under hundreds of lines no consumer reads,
and the scan is cheap to re-run (`--scan-json PATH` keeps a copy for
auditing). `hierarchy` is machine-written; the rest is authored in
Steps 2–4, where empty means *not done yet*, never *none found*.

**Shape, not function.** This skill reports *that* XMN1/XMN2 are a diff
pair; whether that pair is the signal input or a cross-coupled load is
`../../agents/schematic-agent.md` #2's judgment call on top of this file.
**Never edits the netlist** — read-only at every step.

**Why YAML.** A diagram a human reads *and* device lists consumed by name:
block scalars carry the diagram verbatim, the lists stay parseable, comments
let an entry hold its caveat. Markdown makes every consumer re-parse tables
into device lists (a missed row is silent); JSON has no comments.

## Procedure

### Step 1 — scan

```
python .claude/skills/circuit-decomposition/script/build_decomposition.py <top>.sp \
    --out <design_dir>/circuit_decomposition.yaml
```

Runs all four structural passes in-process, **prints the per-block scan**
(device table, groups, detector findings, candidate patterns, net-match
candidates) for Steps 2–4 to work from, and writes the file with
`hierarchy` filled in and the rest stubbed. Per-block `.sp` slices go to a
temp dir deleted on exit (`--keep-work DIR` to inspect them). Re-running is
safe: it refuses to overwrite a file that already holds authored
`patterns`/`tie_groups`/`matched_nets`.

What the passes get right, and why it is not obvious:

- **Hierarchy.** An `X` line is an edge only if its model token names a
  `.subckt` in the deck — in PDKs that ship their primitives as subckts
  (sky130 among them) every primitive is *also* an `X` instance. `.include`/`.lib` are recorded, never followed: PDK primitives
  are the leaves where decomposition stops. A flat netlist is the normal
  single-level case, a real result.
- **Everything runs per block, never on the whole deck.** `.subckt`/`.ends`
  are invisible to `detect_topology.parse_devices()` and net names are
  block-local, so a whole-deck scan invents cross-block matches (verified: a
  diode-connected `XM1` in `blockA` and an unrelated `XM2` in `blockB`,
  both touching a local `nx`, report as one `current_mirror`).
- **Affinity grouping** narrows what Step 2 must consider: rails excluded,
  every other net weighted 1/(fanout−1), reported as shared-net groups (the
  only view that recovers a 1:N mirror *family*, whose legs are pairwise
  weak) and affinity cliques, each with its terminal signatures (`g-g`,
  `s-s`, `d-s`), the diode-connected devices, and `candidate_patterns`.
  **A group is a question, not an answer** — `s-s` alone cannot separate a
  diff pair from a cross-coupled pair. Its real payoff is passives: R and C
  reach no detector at all, so a resistor ladder or an `R0~XC0`
  compensation network first appears here.
- **Net-match candidates.** For every auto-detected pattern, the group's
  devices compared terminal by terminal: a terminal they all agree on is a
  common node, one they disagree on names a matched net set
  (`script/match_nets.py`). Printed as `[net-match: <pattern>]` lines under
  each block, and Step 4 authors from them. Terminal by terminal, never as
  net sets — a cross-coupled pair's two devices touch the *same* two nets
  with drain and gate swapped, so a set difference finds nothing at all.
  Supply rails are excluded: two sources on VDD and VSS are not twins.

### Step 2 — match against the look-up table

Read `pattern-table.md` fully, then per block in Step 1's printed scan,
working from its `candidate_patterns`:

1. **Auto rows** — copy `detector.findings` through; they are signature
   matches, not guesses.
2. **Manual rows** — the detector parses **MOS/BJT only**, so every R and C
   reaches neither `findings` nor `unclassified`: the scan looks complete
   with all transistors accounted for while a whole compensation network is
   missing, silently. Apply each `manual` row's Structure test to the
   `unclassified` devices *and* every R/C in the printed device table.
3. **Disambiguate** with each row's "Watch out" — patterns share signatures
   and differ on one test (diff pair vs. cross-coupled pair: gate origin).
4. **A pattern exists only if the table registers it.** Anything else goes
   to `unmatched_devices`; never invent a pattern name inline.

Fill `patterns` per the schema: block, devices, `instance_paths` from
`hierarchy.blocks` (a block instantiated twice is **two physical copies** of
every pattern in it), what matched it, `confidence: certain` (signature) or
`likely` (judgment, with its reason), and each device's `w`/`l`/`m`/`nf`
copied at full precision from the printed table — never fabricated.
**Every device appears exactly once**, in a pattern or in
`unmatched_devices`; reconcile against the scan before finishing.

### Step 3 — tie groups and tunables (DERIVED — review, do not author)

**You do not write this step; `build_tunables.py` already did, when Step 1
ran.** It applies `pattern-table.md`'s own **Tie group** field to the
detector's findings and emits three things:

| Output | What it is |
|---|---|
| `tie_groups` | per pattern: `tied`, `ratio_carrier`, `free`, and for a ratio group the `unit_device` + `ratio_seed` |
| `tunable_parameters` | the variable registry: `name`, `param`, `members`, `seed`, `kind` (`continuous` for `w`/`l`, `integer` for `m`) |
| `<top>_tunable.sp.j2` | the netlist with every tunable replaced by `{{ VAR }}` — what `schematic-sizing` renders |

**This used to be authored by hand, and that is exactly how it went wrong.**
On `two_stage_rz` the nfet mirror (REF `XMN4`, legs `XMN3`/`XMN5`) was
authored `ratio_conflict` with three independent free widths; sizing then
tuned them to w = 47.2 / 85 / 100 µm — three different unit devices in one
mirror, which is not a mirror. The table had said `tied: [w, l]`,
`ratio_carrier: m` the whole time. Rules that live in code cannot be
talked out of.

**`m` is a real sizing variable wherever a pattern names it
`ratio_carrier`** (`current_mirror`, `cascode_current_mirror`,
`resistor_ladder`, `capacitor_bank`), and frozen everywhere else. `nf` is
never templated — `device-shaper` owns it.

**There is no ratio-conflict rule any more, because there is no conflict.**
Legs written with different `w` do not defeat a shared `w`: one shared
*unit* `w`/`l` times a per-leg integer `m` reproduces any ratio to within
one unit, and is also how the mirror is laid out. `ratio_seed` records each
leg's as-written total, its seeded `m`, and the total that seeds — read it,
and say in your report if a leg's seeded total moved more than a few
percent, since that shifts the starting bias point.

**What you DO still author here:** cross-pattern dependencies, which are
circuit judgment and not derivable from the signature —
`rc_compensation_network`'s `R` tracks `1/gm` of the `common_source_stage`
it compensates, and `Cc` trades against the pair's `gm`. Append them to the
relevant group as a `cross_pattern_dependency` string.

### Step 4 — matched nets (the layout half of a tie group)

A tie group makes two devices identical. It does not make them *matched*:
a differential pair drawn common-centroid still has an offset if one gate
arrives on 40µm of met2 and the other on 9µm of met1, because the twins
then see different series R and different coupling C. **The nets are a
constraint in the same way the devices are**, and layout is the only stage
that can honour it — so this is where it gets written down, while the
netlist is still in hand.

Emit `matched_nets` from each pattern's **Matched nets** field in
`pattern-table.md`, in that file's vocabulary (`differential`,
`complementary`, `common`, `branch`, `sensitive`, `none`), working from the
`[net-match: ...]` candidates Step 1 printed:

1. **Auto patterns** — the candidate lines already name the terminal, the
   nets and the kind. Confirm the kind against the pattern's table row and
   copy them through. The scan cannot decide one thing for you: a `branch`
   set is twins **only if the legs are 1:1**, so check the group's `m`
   before promoting `branch` to `differential`, and say which you chose.
2. **Manual patterns** — no detector, so no candidates: a resistor
   ladder's taps, a cap bank's plates, an RC network's two gain nodes are
   authored by hand from the printed device table. This is the same blind
   spot as Step 2's, and it costs more here, because the passive patterns
   are the ones whose *whole* constraint is parasitic.
3. **`sensitive` is never auto-proposed.** A high-impedance gain node
   matches nothing, so no terminal comparison can find it; it comes from
   the pattern row, and it is what tells layout which single net not to
   run under the digital bus.
4. Carry a pair **outward** past the pattern that named it. A diff pair's
   drain twins are also its load's drains and its compensation branch's
   nets; a pairing that stops at the pattern boundary asks layout to match
   half a signal path.

Each entry: `id`, `nets` (in group order — a mirror's reference first),
`kind`, `pattern` and `devices` it came from, `terminal`, and `why` — the
mismatch that appears if it is ignored, stated as a circuit effect
(offset, CMRR, weight error), not as "for symmetry". Where a group's
devices are deliberately asymmetric (`flipped_voltage_follower`,
`cascode_stage`), emit `kind: none` explicitly rather than omitting the
group: a missing entry reads as *not looked at*.

### Step 5 — report

The tree; one line per pattern (`[pattern] block :: devices — confidence`);
the derived tie groups, each ratio group's `unit_device` and any leg whose
`ratio_seed` moved its total width by more than a few percent (that shifts
the starting bias point, and sizing should know); the matched nets,
`differential` first; anything unmatched; the path to the `.sp.j2`.

## Files and references

- `pattern-table.md` — the registry Step 2 matches against, and the source
  of both the **Tie group** field Step 3 emits and the **Matched nets**
  field Step 4 emits. A skill reference, not generated.
- **`output-schema.yaml` does not exist.** Several places here and in
  `script/build_decomposition.py` cite it as the per-key documentation of the
  output file; it was never written. Until it is, this SKILL.md's own
  description of `circuit_decomposition.yaml` is the whole schema — treat a
  pointer to `output-schema.yaml` as pointing at that gap, not at content you
  failed to find.
- `script/build_decomposition.py` — Step 1's driver. It composes
  `script/scan_hierarchy.py` (hierarchy + slices), `script/group_devices.py`
  (affinity grouping), `../../reference/detect_topology.py`
  (signature matching) and `script/match_nets.py` (net-match candidates);
  each runs standalone to debug one pass, and each module docstring carries
  that pass's full rationale.
- `script/match_nets.py` — Step 4's candidate generator. Compares a
  detected group's devices terminal by terminal: agreement is a common
  node, disagreement is a matched net set. `NET_KIND` there maps pattern ID
  → net kind, and a pattern absent from it yields common nodes only —
  correct for a deliberately asymmetric pattern (FVF), wrong for anything
  else, so a new `auto` pattern gets a row there as well as in
  `pattern-table.md`.
- `script/decompose_netlist.py` — **optional, different job**: physically
  rewrites a flat netlist, pulling diff-pair/mirror groups into real
  `.subckt` blocks (always to `--out-dir`; the input is untouched). No step
  above calls it — analysis needs no rewrite — but **do not delete it**: it
  is the only producer of the decomposed form `../placer/script/subckt_macros.py`
  reads (a path `generate_primitives.py` takes by default), and its pin
  naming is what `cells/diff_pair.py`'s `diff_pair_params_from_subckt()`
  parses.
- `../../reference/topology_glayout_map.md` — topology → glayout module/ports.
- `../../agents/schematic-agent.md` — the caller, #2; `../schematic-sizing/SKILL.md`
  — the tie groups' consumer; `../../../CLAUDE.md` — Key Rules, netlist freeze.
- `../placer/script/derive_sym_groups.py` — `matched_nets`' downstream
  consumer. It seeds its differential-net map from this file's
  `differential` entries and falls back to re-deriving them from
  `tie_groups` when the section is empty, so an unauthored Step 4 degrades
  rather than breaks — but only the authored form carries the pairs no tie
  group implies (a passive twin, a pairing carried outward past its
  pattern).
