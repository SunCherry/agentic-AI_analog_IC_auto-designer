# Device Shaper — Reference

Background for `SKILL.md`. Read a section when that step needs it; the
procedure itself is executable without this file.

## Which PDKs

Every coefficient the annotation uses — junction, sidewall and gate
coefficients, sheet resistance, the default diffusion length — is looked up by
`--pdk` from `PDK_TABLES` in
`../parasitic-estimation/script/estimate_parasitics.py`. The sweep, the
selection rule, the drop metric and the report never see a process name, so
nothing here is written around one process.

**"Supported" means "has a table", which is a data question.** Run `--help`:
`--pdk`'s choices are generated from the tables that exist, so the CLI is the
current answer rather than a list that goes stale. Which process a design
actually uses is not decided here either -- it is `"selected"` in
`../../reference/pdk_options.json`.

**A `--pdk` with no table is refused by name, up front** — before any
simulation — rather than estimated with another process's numbers. Two honest
responses: add the process's table (the sweep then works unchanged, with no
edit to the skill), or skip finger-count selection for this design and **say in
the report that `nf` was never measured**, since an unmeasured `nf` and a
chosen one look identical in the netlist.

## Working folder

```
<design_dir>/device_shaping/
  shaping_report.md             THE report -- results AND recommendation, one file
  <design>_final_shaped.sp      the hand-off netlist (top level, `_shaped` suffix)
  <sub-circuit>.sp              every design-local sub-netlist, ORIGINAL name kept
  shape_devices.json            which devices were swept, and the yaml entry each
                                one came from
  sweep/                        only with --save-artifacts
    nf_<N>/                     per swept finger count: annotated netlist, its
                                testbench, its .out
    result.json                 the full sweep + selection, machine-readable
```

**Everything this skill writes lands in `device_shaping/`**, including the
hand-off netlist. Nothing is written into `sizing/` — that folder belongs to
`../schematic-sizing/SKILL.md`, and mixing this skill's output into it is what
makes "which skill produced this file" unanswerable.
`default_shaping_dir()` finds the folder by walking up to the **design** folder
(the parent of `sizing`), falling back to the netlist's own directory when there
is no such ancestor: a netlist from outside a design folder still runs, it just
does not land in the tree.

**One report, not several.** `shaping_report.md` carries both the measurement
and the recommendation. `sweep/result.json` is raw data for a re-read, not a
second report — and `--nf-out` / `--stricter-target-json` (which write
`nf_recommendation.json` / `stricter_target_spec.json`) are **not part of this
flow**: they duplicate what the report already states, and a second file naming
a finger count is a second place for it to disagree.

**Naming.** Only the TOP-LEVEL netlist takes the `_shaped` suffix
(`<design>_final.sp` -> `<design>_final_shaped.sp`), derived from
whatever `sizing/` produced. Sub-circuits are copied in under their original
names, because every reference to them elsewhere already uses those names;
`.include` lines are rewritten to bare basenames so the flat folder resolves.
See SKILL.md Step 3.

## Why the Nf curve turns

The measured sweep quoted in `SKILL.md` — Nf 1/2/4/8 → 9.4% / 0.0% / 0.2% /
1.5% worst-case drop, optimal at 2 — turns because two effects trade against
each other. Going 1 → 2 halves each finger's diffusion area and with it the
junction capacitance, which is the whole benefit; past 2 the added diffusion
perimeter and gate resistance cost more than the remaining area saves.

The effect can be far larger than that example: an earlier run measured
37.5% / 27.4% / 495.3% / 463.1% across the same four values — folding two steps
past the optimum cost an order of magnitude. (Those figures predate the current
target-relative denominator; they are here for the shape, not the scale.)

## Why the stricter floor is shaped the way it is

**Additive headroom on top of target, not a division.**
`current_target * (1 + rel_drop)`: a floor key whose target is 44 showing an 8%
shortfall derives `44 * 1.08` = 47.52. It is also compared against the sizing
stage's own achieved value, and the stricter of the two wins — a spec that
cleared its target by a wide margin but still crossed the significance
threshold is a fragility signal on its own.

**Why every `CEILING_METRICS` key is RELAXED rather than held.**
`compute_stricter_targets()` takes the worst (largest) `effective_drop` among
the newly-flagged floor specs — worst case, not an average — and applies
`new_ceiling = ceiling * (1 + worst_effective_drop)`. Tightening every floor
while pinning the ceiling describes a bar that is harder on every axis at once
with no lever to offset it, and the ceiling is usually the one that closes off
"spend more bias current" — the very escape valve a tightened floor then demands
more of. It fires only when a floor was actually flagged.

**Why an unmeasurable ceiling is left alone.** Relaxing a bar nobody measured
would loosen the spec on no evidence, so any key `check_target()` reported as
`measurable: false` carries its original value through unchanged.

**The limitation, stated plainly.** The floor is derived from the target and
the annotated result, not from the sized value, so it does **not** model how
big a `W`/`L` change is actually needed to close the gap — it assumes closing
the same absolute shortfall suffices, when the annotated degradation itself
scales with device geometry, which is exactly what re-sizing changes. It is a
starting **aim**, not a sufficient number, and it under-states the required
move rather than over-stating it. Say so when reporting it.

**Finite by construction.** `target * (1 + effective_drop)` never blows up, so
a steep-but-real ask is reported as a large number rather than as impossible.
Genuine impossibility is checked separately: an annotated value collapsing to
`<= 0` — the circuit breaking under parasitics, not merely falling short —
yields `stricter_target: null` with a note, because no amount of `W`/`L` would
have fixed a topology or compensation problem.

## Why the original-spec cross-check is in the report

Clearing a harder floor guarantees clearing the original floor with margin, by
construction. **A ceiling is the opposite**: the harder-target step *raised*
it, so a design landing between the original and the relaxed value clears the
working ceiling this cycle used and would fail the original, tighter one. That
is a deliberate accepted tradeoff of sizing under a harder target — the relaxed
ceiling is the real budget — but it has to be visible rather than an unstated
assumption. The cross-check is informational and never gates.

## More things this skill does not do

Beyond the ones stated in `SKILL.md`:

- **Change `W`/`L`, or anything except `nf`.** `../schematic-sizing/SKILL.md`
  owns device sizing. When the sizing cannot survive parasitics this skill
  reports the shortfall it measured; it never tunes and never prescribes.
- **Measure a spec key outside the sweep's own metric set.** The sweep reads
  one AC raw file through `ac_metrics()` and carries the sizing result's ceiling
  metric forward unannotated. **As the script stands that set is `Gain`, `UGBW`,
  `PM` (from `ac_metrics()`) plus `Power` (carried forward)** — `ac_metrics()`
  returns a fixed `gain_dB, ugb_Hz, pm_deg` triple and `_TARGET_KEY` maps those
  four names, so despite `compute_fidelity.py` having a `METRICS` registry the
  sweep is not registry-driven and adding a key means editing
  `sweep_fingers.py`. Any other key in `target_spec.json` comes back from
  `check_target()` as unmeasured, pinning `all_met` to `False` at every finger
  count. See SKILL.md Step 1: that state reads exactly like a failed design.
- **Pick a per-device finger count.** The sweep chooses one global Nf for every
  MOS device. A design where different devices genuinely want different counts
  needs that decided by hand after reading the sweep's per-device clamping —
  not assumed away.
- **Measure a real layout.** Every number is a PDK-model *estimate* of
  parasitics on a netlist, not an extraction. `verify-agent`'s post-layout PEX is
  what confirms it.
- **Re-invoke the sizing stage, ask anyone else to, or judge the sizing at all.**
  Sizing ran before this skill and does not run again: a spec that falls short
  under annotated parasitics is a measurement reported in the hand-off, never a
  verdict and never routed back (`../../agents/schematic-agent.md`'s Protocol).

## Files, and the two result names

`script/sweep_fingers.py` is the whole procedure: the Nf sweep, the selection,
the drop/target measurements, the report and the `nf_recommendation.json`
write. (It also still computes a `needs_resizing` flag and a stricter-target
derivation, and exits non-zero on a shortfall -- vestigial, no longer part of
this skill's contract: read the numbers, ignore the verdict.) It imports four shared helpers rather than
keeping its own copies — the PDK coefficient tables, the AC metric reader, the
annotate-and-clamp-retry loop, and the FLOOR/CEILING target comparison. Its
module docstring names each import's path; read that if one fails to resolve.

**`ideal` and `annotated`** are the names used everywhere — in the JSON, the
report table, and the flag names:

| Name | What it is |
|---|---|
| `ideal` | the sizing stage's converged result — the netlist against the ideal schematic, no parasitics. Handed in via `--ideal-specs`; never re-derived here |
| `annotated` | that same netlist re-simulated with estimated parasitics on every device, at one finger count. One per swept Nf |

Reading a report is then one question: how far `annotated` sits from the
target, and which Nf puts it closest.

**An older `result.json` will use `round1`/`round2` for these**, along with a
`--round1-specs` flag that no longer exists. Same two quantities, previous
names; nothing reads those files back, so there is nothing to migrate.
