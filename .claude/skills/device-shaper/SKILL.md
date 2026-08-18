---
name: device-shaper
description: >-
  Choose the finger count (`nf`) for the MOS devices a design's parasitics
  actually depend on, and measure what that choice costs -- before any layout
  exists. Sweeps Nf only on the devices `circuit_decomposition.yaml` flags
  `severity: high`, simulates every candidate, and picks the smallest Nf past
  the point where folding stops helping; the curve is not monotonic, so this is
  a measurement, not a rule of thumb. Then levels every device's drawn extent so
  none reaches layout as a long thin sliver, and finally rewrites `m` into the
  finger count for STANDALONE primitives only -- never inside a current_mirror
  or differential_pair -- so they draw compact, into a separate geometry-only
  netlist that deliberately does not simulate. Writes one report and a `_shaped`
  hand-off netlist. Runs ONCE after `schematic-sizing`, never loops back, never
  changes W/L or total width, and needs a PDK with a parasitic coefficient
  table (`--pdk`).
---
# Device Shaper

**How many fingers should each device be drawn with, and does the sizing still
hold once that choice's parasitics are real?** `nf` is the one geometry
parameter no sizing loop touches; left unset it is whatever a default happens to be.

- **Never changes `W`/`L` or total width** — `../schematic-sizing/SKILL.md` owns sizing.
- **Runs once, no loop back**: sizing converges → this runs → hand-off to `layout-agent`.
- **No verdict on the sizing**, no re-sizing demand — only the chosen `nf` and its cost.
- **Only flagged devices are swept**; the rest keep the netlist's `nf`. Sweeping
  everything burns simulations and buries the result that matters.

**Preconditions**, all owned elsewhere (folder layout, PDK tables: `reference.md`):
`circuit_decomposition.yaml` **with `parasitic_sensitivity`** (schematic-agent
step 2c) · converged `sizing/<design>_final.sp` + its testbench · that
iteration's result JSON · the target spec, **the harder one if sizing used
one** · a PDK with a parasitic table (one without is **refused by name**).

## Step 1 — pick the devices to sweep

```
python .claude/skills/device-shaper/script/select_shape_devices.py \
    <design_dir>/circuit_decomposition.yaml <design_dir>/sizing/<design>_final.sp \
    -o <design_dir>/device_shaping/shape_devices.json
```

Recovers MOS names from every `severity: high` entry. **`ref` is prose, not a
device list** — a token enters only if it matches a real MOS instance, so nets,
passives and English words can't. **Only `ref` is scanned**; `why` names devices
as supporting argument and over-selects.

Act on both outputs:
- **`NOTE: ... names no MOS device`** — expected; a passive branch or bare node
  has nothing to fold. The sensitivity is real, so carry it to the report.
- **`WARNING: confirmed_by_user: false`** — the ranking is provisional. Say so.

`--severity high,medium` widens; `--devices` overrides the yaml when a `ref`'s
wording misses one. No `parasitic_sensitivity` section → the script **stops**;
that section is schematic-agent's, not yours to guess around.

## Step 2 — sweep the finger counts

```
python .claude/skills/device-shaper/script/sweep_fingers.py \
    <design_dir>/sizing/<design>_final.sp <design_dir>/testbench/<deck> \
    --ideal-specs <the converged sizing iteration's result JSON> \
    --pdk <the design's PDK> --fingers 1,2,4,8 --drop-threshold 0.05 \
    --parasitic-headroom 0 \
    --shape-devices <design_dir>/device_shaping/shape_devices.json \
    --target-spec <design_dir>/sizing/harder_target_spec.json \
    --work-dir <design_dir>/device_shaping/sweep --save-artifacts \
    --report-md <design_dir>/device_shaping/shaping_report.md
```

- **`--shape-devices` restricts the sweep**; every other MOS pins at its netlist
  `nf`. Omitting it sweeps everything — don't, unless nothing was flagged and you say so.
- **Target the spec sizing was tuned to**, else the shortfall math uses the wrong
  bar. **`--parasitic-headroom 0` with a harder target** — the script's 10pp
  buffer plays the same role and stacking double-counts (standalone default `0.10`).

**Selection rule.** Among Nf that simulate: prefer those meeting the target; in
that set (or all, if none qualify) find the best worst-case drop, then take the
**smallest Nf within 3pp** (`NF_IMPROVEMENT_TOLERANCE`) of it — beyond that,
folding buys <3pp and costs routing complexity.

**The curve is not monotonic**; the optimum is usually a middle value, and
folding past it can cost an order of magnitude more (`reference.md` for why).
Every Nf is really simulated, via a clamp-retry loop for devices folded past
their model bin — a large jump is a real circuit, not a crash. **Trust the sweep
over any assumption that more folding means less parasitic impact.**

**Swept / Pinned / Clamped differ**: swept = Step 1's flagged; pinned = the rest,
held on purpose; clamped = the retry loop had to *reduce* one. **Only clamped is
a problem.**

**If no Nf meets the target**, selection returns the best by drop alone —
**informational, not a pass**. **Check `unmeasured` first**: the sweep
re-simulates only the AC metrics its reader extracts and carries the ceiling
metric forward unchanged, so any key outside that set returns unmeasured and
pins `all_met=False` at every Nf — **which reads exactly like a failed design**.
Which keys: `reference.md`. Name them.

**Two independent measurements, both reported, neither a gate:**

| Question | How |
|---|---|
| Is the drop significant? | default **5%**, as `(target − annotated) / target` — **not** from the un-annotated result |
| Does it still meet the target? | independently, via `check_target()` |

They disagree, and that is the information: a spec can miss the target while
dropping under 5%, or drop past 5% and still clear it. Say which is which.

**State the shortfall** (spec, ideal, annotated, `rel_drop`, met or not) and stop
there — don't derive a stricter target, tell `schematic-sizing` to re-run, or
call it a failed cycle; that call is the caller's. **A non-zero exit signals a
shortfall, not a failed run.**

## Step 3 — write the shaped netlist

```
python .claude/skills/device-shaper/script/write_shaped_netlist.py \
    <design_dir>/sizing/<design>_final.sp \
    --nf <name>=<nf>,<name>=<nf> --out-dir <design_dir>/device_shaping
```

**The finger count reaches the design solely through this netlist.** Pass `nf`
for swept devices only — pinned ones already carry theirs (`--nf-json <sweep
result.json>` reads a map instead).

| input | lands in `device_shaping/` as | renamed |
|---|---|---|
| `sizing/<design>_final.sp` | `<design>_final_shaped.sp` | yes — the suffix marks the hand-off |
| any `.include`d sub-circuit | same basename | no — existing references use it |

One flat folder, so design-local `.include`s become **bare basenames** resolving
as siblings (full-depth recursion). An include pointing **outside** the design
tree keeps its target, but a **relative** one is made absolute first — it
resolved from `sizing/` and would silently fail from `device_shaping/`.

Writes the chosen `nf` and nothing else — **not** the sweep's parasitic
annotation (`ad`/`as`/`pd`/`ps`/`nrd`/`nrs`, series `Rg`), which exists to
*measure* a finger count and would have layout count it twice. `W`/`L`/`m` pass
through. A `--nf` name in no file → `WARNING: never found`, non-zero exit.

## Step 4 — level device extents (every device, not just the flagged)

An unflagged device keeps whatever `nf` the netlist carried; drawn as a long thin
sliver it costs area, stretches every net crossing it, and is invisible upstream.

```
python .claude/skills/device-shaper/script/level_device_lengths.py \
    <design_dir>/device_shaping/<design>_final_shaped.sp \
    --tie-groups <design_dir>/circuit_decomposition.yaml \
    [--factor 3.0] [--powers-of-two] [--min-finger-width UM] [--apply]
```

**"Length" is drawn extent, not `l`** — `l` is channel length, unchangeable by
folding, and on a passive `l` *is* the value:

```
extent = w / nf          (per-finger width)
```

`nf` splits `w` and never adds any, so raising it shrinks extent proportionally
while **total width `w * m`, topology and every electrical parameter stay put** —
which is why this is safe once `W`/`L` are frozen.

**The rule.** Mean extent over all MOS devices → flag every device above
`--factor` × mean (default **3.0**) → fold each to the `nf` landing **nearest
the mean**.

- **Nearest, searched — not `round(w / mean)`**, which is measurably worse
  (`w=47.2`, mean 32.5: it gives `nf=1`, off by 14.7; `nf=2` is off by 8.9).
  Ties → smaller `nf`.
- **Mean computed ONCE**, before any folding — otherwise processing order changes
  the answer and each fold chases the mean down.
- **Folding only increases `nf`** — current `nf` is the floor, so Step 2's
  measurement is never undone.
- **Matched devices fold together** (`--tie-groups`): a flag on any member folds
  the group. Without it a diff pair's halves fold apart and it is no longer
  matched; the script says so when absent.
- **Passives reported, never folded** — no `nf`, and changing `l` changes the value.

**This can override a *measured* `nf` with a geometric one**: **re-simulate after
`--apply`** and report swept-then-leveled devices separately, with both numbers.
A never-swept device has no measured `nf` to override — and no measurement of
what its new one costs. Say that too.

Without `--apply` nothing is written. **No outlier is the normal outcome, not a
skipped step** — report the mean, threshold and sorted table anyway.

## Step 5 — fold `m` into `nf` for standalone primitives (layout hand-off)

Last thing done to geometry. `m > 1` is `m` parallel copies and a generator draws
each as its own row — fine inside a composed block, bad standalone. Measured on
`example/test_miller_ota`: XMN5 (`w=43.12 nf=1 m=8`) drew as a **9.38 x 368.15um
column**, in a floorplan of bbox 132 x 430um at 18.4% utilization.

```
python .claude/skills/device-shaper/script/fold_multipliers.py \
    <design_dir>/device_shaping/<design>_final_shaped.sp \
    --decomposition <design_dir>/circuit_decomposition.yaml \
    [--out PATH] [--dry-run] [--json <design_dir>/device_shaping/multiplier_fold.json]
```

```
w -> w * m        nf -> nf * m        m -> 1
```

**Why `w` moves too.** `m` is the only parameter that multiplies width (total =
`w * m`); `nf` SPLITS `w` and adds none, since BSIM4 takes `Weff = W/NF`.
Measured on sky130: `w=40 nf=1` → 1.686mA, `w=40 nf=8` → 1.695mA **unchanged**,
`w=40 m=8` → 13.486mA, exactly 8x. **So `m=1` without raising `w` divides the
device's width by `m`** — the one mistake to avoid. Scaling both holds **total
width `w*m`** and **finger extent `w/nf`** exactly; only the arrangement changes,
`m` rows of `nf` fingers → **one** row of `nf*m`. Verify both invariants per
device and report them.

**Writes `<design>_final_shaped_primitives.sp`, never in place.** The folded `w`
is `m`× larger and routinely passes the PDK's widest model bin (`XMP3` → `w=240`
vs sky130's 100um), so it **matches no model card and does not simulate** — it
would silently undo `design-sheets-checker` Step 2a's model-bin fold. **Geometry
only**: never simulate it, never hand it to sizing, never let it replace
`<design>_final_shaped.sp`, the simulable hand-off.

**Composed blocks are skipped, not as an optimization.** A device inside a
`current_mirror`, `differential_pair` or any pattern grouping ≥2 MOS devices
isn't drawn device by device — a mirror takes each leg's ratio from its total
width against the reference, a pair splits `nf * m` across halves for common
centroid — so rewriting `m` changes a ratio the block is built on. A
**one-device** pattern (lone common-source, self-biased reference) does fold.
Without `--decomposition` everything counts as standalone: correct only with no
composed blocks, and the run says so.

Report per device: folded, skipped (naming the block), or already `m=1`.

## Step 6 — the report

**One file: `<design_dir>/device_shaping/shaping_report.md`**, written by Step
2's `--report-md`, carrying results *and* recommendation. **Don't pass
`--nf-out` or `--stricter-target-json`** — each writes another file naming a
finger count, another place to disagree with the report. `sweep/result.json` is
raw data, not a report.

**`--report-md` REWRITES, it does not merge**, destroying the hand-written
sections. **Write it LAST**, or drop the flag on re-runs; if lost, rebuild from
`sweep/result.json`, not memory, and say so.

Scripted: per-Nf table, selection `reasoning`, swept/pinned lists, per-spec
ideal-vs-annotated with `rel_drop`. **Add by hand:**

- **Performance drop analysis** — the script ranks *which* specs dropped, not
  *why*. Per flagged spec, 2–4 sentences: how much, which named instances carry
  the parasitic, and why that node is more sensitive than a similar one elsewhere
  (dominant pole, zero, matched ratio, startup margin) — grounded in op-point
  data and the circuit read. **Never leave the placeholder in.**
- **High-severity entries with no MOS device** (Step 1's NOTE) + the specs they
  threaten: the parasitics this skill had no lever for.
- **The hand-off path**, and that it now carries a chosen `nf`.
- **`confirmed_by_user: false`**, if Step 1 warned.
- **Step 4's leveling** — mean, threshold, sorted table, which folded.
  Distinguish: swept and left alone · swept then **overridden** (its measured
  `nf` no longer stands — give re-simulated numbers) · never swept but folded
  (geometric, cost unmeasured).
- **Step 5's fold** — which folded, which skipped and their block, both
  invariants held. State that `_primitives.sp` is **geometry-only** and won't
  simulate while `_shaped.sp` stays the simulable hand-off; mixing them up gives
  a netlist with no model card.

## Files

- `script/select_shape_devices.py` — Step 1. Importable: `mos_devices(netlist)` → `{name: nf}`.
- `script/sweep_fingers.py` — Step 2; its docstring names the shared helpers.
- `script/write_shaped_netlist.py` — Step 3.
- `script/level_device_lengths.py` — Step 4. `--json` dumps per-device decisions.
- `script/fold_multipliers.py` — Step 5. `--dry-run` reports; `--decomposition` names the blocks to skip.
- `reference.md` — PDK tables, folder layout, floor/ceiling derivations, `ideal`/`annotated`, non-goals.
