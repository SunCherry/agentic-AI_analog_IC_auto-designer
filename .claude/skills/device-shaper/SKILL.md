---
name: device-shaper
description: >-
  Decide how every device is DRAWN -- unit width, finger count `nf`, copy count
  `m` -- from four geometric principles: build each tie group from one unit
  device, make that unit as large as the PDK's model bins allow, make one copy
  square, and make the array of copies square. Total width `w * m` is preserved;
  any residual is bounded and reported. Pure geometry, so it needs no parasitic
  table and runs on ANY PDK whose design rules can be read -- including sky130A,
  where the old Nf sweep refused to run. Then simulates before and after and
  states what the shape change cost. Runs ONCE after `schematic-sizing`, never
  loops back, never changes `W`/`L`.
---
# Device Shaper

**How should each device be drawn?** Sizing decides how much silicon a device
needs. This decides what shape that silicon takes: how wide one unit is, how
many fingers it is split into, and how many copies sit side by side.

- **Never changes `W`/`L` or total width** — `../schematic-sizing/SKILL.md` owns
  sizing. This is a *re-expression*: same total, different shape.
- **Runs once, no loop back**: sizing converges → this runs → hand-off to
  `../../agents/layout-agent.md`.
- **No verdict on the design.** It reports the geometry it chose and what that
  cost. Acting on a shortfall is the caller's call.

## The four principles

They are applied in this order, and the order is the whole design.

| # | Principle | Why |
|---|---|---|
| 1 | **Use a unit device** | Every member of a tie group is one physical device repeated. A mirror's legs differ only in how many copies they have — that is what makes them matchable, and it is the same rule `../circuit-decomposition/SKILL.md` already enforces on `w`/`l`. |
| 2 | **Maximize the unit** | Bigger unit → fewer copies → less perimeter, fewer junctions, less wiring between copies. Bounded above by the PDK's widest model bin. |
| 3 | **Square device** | One copy as tall as it is wide. A long thin device wastes area, stretches every net crossing it, and matches badly because edge effects dominate. |
| 4 | **Square array** | The copies tile into a block that should also be square, for the same reasons one device should be. |

**Across all four: total width `w * m` is preserved.** Where an exact
re-expression does not exist under the copy cap, the residual is bounded by
`--max-total-error` (default 2%) and **reported per device**, never absorbed
silently.

### The geometry

One copy alternates `nf` gate columns with `nf + 1` source/drain columns:

```
height = w_unit / nf + 2 * poly_extension
width  = nf * l + (nf + 1) * sd_column        sd_column = contact + 2 * enclosure
```

`sd_column` and `poly_extension` come from the PDK's own `get_grule()`, never
hardcoded. Squareness is scored as `|log(height / width)|`, so 2:1 and 1:2 cost
the same — neither orientation is better.

## Step 1 — shape

```
python .claude/skills/device-shaper/script/shape_devices.py \
    <design_dir>/sizing/<design>_final.sp \
    --decomposition <design_dir>/circuit_decomposition.yaml \
    --out <design_dir>/device_shaping/<design>_final_shaped.sp \
    --report <design_dir>/device_shaping/shaping_report.log \
    --json <design_dir>/device_shaping/shape_plan.json
```

`--decomposition` is **required and not optional**: the unit device is *per tie
group*, so without the circuit read there is nothing to build a unit for. A
netlist whose `circuit_decomposition.yaml` has no `tie_groups` stops here.

**Read the report before passing it on.** Three things in it are judgment, not
arithmetic:

| Line | What it means |
|---|---|
| `WORST TOTAL-WIDTH RESIDUAL` | 0.0000% means every device re-expressed exactly. Anything else is silicon that changed, and its electrical cost shows up in Step 2 |
| `<-- strip` on an array | that copy count has no square factorisation (a prime). Reported, never padded — a partly-filled row breaks the symmetry the array exists to provide. The fix is upstream: a different copy count means a different unit, which sizing chose |
| a device whose aspect is far from 1 | usually the min-finger-width floor refusing to fold something small. That is correct behaviour — see below |

### The min-finger-width floor, and why it is not optional

`--min-finger-width` (default **1.0 µm**) is a floor on `w_unit / nf`. Below it a
MOS stops behaving like a scaled version of itself: narrow-width effects shift
Vt and drive, and the device's current is no longer what its total width says.

**This is measured, not assumed.** On `example/test_miller_ota` the shaper
squared up `XMP4` — the 1.4 µm self-bias reference — into two 0.7 µm fingers.
That one device sets every branch current in the amplifier, and the fold cost
**38% of UGBW (16.78 → 10.43 MHz)** with total width preserved exactly.
Reverting `XMP4` alone recovered it to 16.11 MHz. Every other device's fingers
were 3.6 µm or wider and cost nothing.

So a small device is left deliberately un-square. **Principle 3 does not
outrank the device still working.**

### Principle 2 is a gate, not a tie-break

Worth knowing, because it is not what a literal reading of "total, then array,
then unit size" gives. Halving the unit doubles every copy count, and a bigger
copy count almost always factors more squarely — so array squareness can be
improved without limit by shrinking the unit. Measured on the same design's
nfet mirror: unit 47.2 µm (`m` = 5/2/6, arrays 1×5 / 1×2 / 2×3) *loses* to unit
7.87 µm (`m` = 30/12/36, arrays 5×6 / 3×4 / 6×6) — both exact on total width,
the second absurd.

So unit size is bucketed in halvings below the largest feasible unit and
compared **before** array shape: within one halving of the biggest unit
available, the squarest array wins. The full sort key is

```
(total-error bucket, unit-size bucket, array squareness, device squareness, residual)
```

## Step 2 — verify

```
python .claude/skills/device-shaper/script/verify_shaping.py \
    --testbench <design_dir>/testbench/<deck>.spice \
    --before <design_dir>/sizing/<design>_final.sp \
    --after  <design_dir>/device_shaping/<design>_final_shaped.sp \
    --out-dir <design_dir>/device_shaping/verify \
    --report <design_dir>/device_shaping/verify_report.log \
    --target-spec <design_dir>/spec/target_spec.json --vdd <supply>
```

The re-expression is width-preserving but **not exactly electrically neutral**,
for two reasons worth measuring rather than assuming: `nf` changes junction area
and perimeter (usually a small *improvement*, and the reason folding is worth
doing), and any total-width residual lands here.

**This is a measurement, not a gate.** It returns no verdict on the design. A
few percent is normal — on `test_miller_ota`: Gain +3.9%, UGBW −4.0%, PM −2.3%,
Power −4.7%, all four keys still passing. **A double-digit delta means look for
a small device that got folded**, and raise `--min-finger-width`.

State the before/after table in your report either way.

## Hand-off

| file | goes to |
|---|---|
| `device_shaping/<design>_final_shaped.sp` | `layout-agent` — **the frozen netlist**, and what `verify-agent` simulates, so LVS and the spec measurement stay on one netlist |
| `device_shaping/shaping_report.log` | the shape decisions and every residual |
| `device_shaping/verify_report.log` | what the shape change cost |

**There is no `_primitives.sp` any more.** The old skill wrote a second,
geometry-only netlist that folded `m` into `nf` for standalone devices and
deliberately did not simulate. `layout-agent` already records that it never uses
it: `build_fet()` honours `m` directly via `multipliers`. One netlist reaches
layout, and it simulates.

## What this skill does not do

- **Does not choose `W`/`L`.** Sizing's, and frozen by the time this runs.
- **Does not re-size, and never asks sizing to run again.** If Step 2 shows a
  key falling out of spec, report it — the decision is the caller's.
- **Does not lay anything out.** It chooses `w`/`nf`/`m` and the array shape it
  scored them against; `../placer/SKILL.md` decides where the copies actually go.
- **Does not touch passives.** A resistor's or capacitor's `w`/`l` *is* its
  value; reshaping it would change the circuit.
- **Does not need a parasitic coefficient table.** The previous version measured
  `nf` by simulating candidates against one, and on sky130A that table is empty
  by design (`parasitic-estimation`'s `PDK_TABLES = {}`, left so rather than
  filled with invented physics) — so the sweep refused to run by name and every
  device reached layout at `nf=1`, unmeasured and indistinguishable in the file
  from a chosen one. These rules are geometry, so `nf` is now always chosen, on
  every PDK.

## Files

`script/shape_devices.py` — the four principles, the search, the report ·
`script/verify_shaping.py` — before/after simulation and the spec delta.

External: `../circuit-decomposition/SKILL.md` (`tie_groups` — the unit device is
per group) · `../../reference/pdk_config.py` + `pdk_options.json` (the active
process) · glayout `get_grule()` (the two process numbers the geometry needs) ·
`../design-sheets-checker/script/run_erc_check.py` (`load_pdk_bin_widths` — the
model-bin ceiling on the unit).
