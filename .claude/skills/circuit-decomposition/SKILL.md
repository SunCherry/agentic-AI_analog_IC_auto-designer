---
name: circuit-decomposition
description:
  Read an analog design's top-level netlist and decompose it structurally --
  the `.subckt` hierarchy as a diagram, every circuit pattern registered in
  `pattern-table.md` (current mirror, diff pair, cascode, resistor ladder,
  ...) matched per hierarchy level, and the matched-parameter tie groups
  saying which devices must share one tunable `w`/`l` and which carry their
  ratio in `m`. One command scans; the conclusions land in one file,
  `<design_dir>/circuit_decomposition.yaml`. Reads the netlist only, never
  edits it, and reports device SHAPE, not circuit FUNCTION. Use at
  `schematic-agent`'s "circuit understanding" step, or any time a netlist
  must be understood before it is sized.
---

# Circuit Decomposition

**One design, one artifact**, holding conclusions only:
`<design_dir>/circuit_decomposition.yaml` — the `hierarchy` diagram,
`patterns`, `unmatched_devices`, `tie_groups`, `open_questions`.
`schematic-agent` and `schematic-sizing` read it to learn what the circuit
contains and **which devices share a tunable parameter**; it carries what
they act on and nothing else.

The evidence behind them — device tables, shared-net groups, affinity
cliques, raw detector output — is **printed** by Step 1, not stored:
persisting it buried the answers under hundreds of lines no consumer reads,
and the scan is cheap to re-run (`--scan-json PATH` keeps a copy for
auditing). `hierarchy` is machine-written; the rest is authored in
Steps 2–3, where empty means *not done yet*, never *none found*.

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

Runs all three structural passes in-process, **prints the per-block scan**
(device table, groups, detector findings, candidate patterns) for Step 2 to
work from, and writes the file with `hierarchy` filled in and the rest
stubbed. Per-block `.sp` slices go to a temp dir deleted on exit
(`--keep-work DIR` to inspect them). Re-running is safe: it refuses to
overwrite a file that already holds authored `patterns`/`tie_groups`.

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

### Step 3 — tie groups (matched parameters)

Emit each pattern's tie group from its **Tie group** field in
`pattern-table.md`, in that file's vocabulary: `tied` (one shared tunable —
a diff pair ties `w, l` *and* `m, nf`, its halves being identical rather
than ratioed; a mirror ties `w, l` with the ratio in `m`), `ratio_carrier`
(**`m`/`nf` are frozen for sizing** — layout levers, never templated),
`free`/`none` (cascode, CS stage, FVF — tying them removes a real degree of
freedom, and "must NOT be tied" is worth writing down).

**Ratio-conflict rule.** Before emitting `tied: [w, l]`, compare the group's
as-written `w`/`l`. If they already differ, a shared tunable cannot
reproduce the intended ratios: do not tie, do not rescale — emit
`status: ratio_conflict` with each device's values and carry it into
`open_questions`. Silently tying a deliberately-ratioed mirror family moves
the bias point of every leg it touches. Record cross-pattern dependencies
too: `rc_compensation_network`'s `R` tracks `1/gm` of the
`common_source_stage` it compensates, `Cc` trades against the pair's `gm`.

### Step 4 — report

The tree; one line per pattern (`[pattern] block :: devices — confidence`);
the tie groups, any `ratio_conflict` first; anything unmatched; the path.

## Files and references

- `pattern-table.md` — the registry Step 2 matches against. A skill
  reference, not generated.
- **`output-schema.yaml` does not exist.** Several places here and in
  `script/build_decomposition.py` cite it as the per-key documentation of the
  output file; it was never written. Until it is, this SKILL.md's own
  description of `circuit_decomposition.yaml` is the whole schema — treat a
  pointer to `output-schema.yaml` as pointing at that gap, not at content you
  failed to find.
- `script/build_decomposition.py` — Step 1's driver. It composes
  `script/scan_hierarchy.py` (hierarchy + slices), `script/group_devices.py`
  (affinity grouping) and `../../reference/detect_topology.py`
  (signature matching); each runs standalone to debug one pass, and each
  module docstring carries that pass's full rationale.
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
