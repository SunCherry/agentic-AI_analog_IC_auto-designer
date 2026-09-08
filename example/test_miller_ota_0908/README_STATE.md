# test_miller_ota_0908 — what this design dir is

**`mechanism_demo/` is a MECHANISM VALIDATION for the tie-group / `.sp.j2`
change, not a converged design.** Do not read its numbers as a sizing result.
A real `schematic-agent` run was started afterwards and writes to `sizing/`;
that one is the design. The two are deliberately kept apart.

It exists to prove one thing end to end, on the real toolchain: that a current
mirror now reaches sizing as **one shared unit device plus a per-leg integer
`m`**, and stays that way through every step that writes the netlist.

## What was actually run

| Step | Command | Result |
|---|---|---|
| circuit read | `build_decomposition.py` | `tie_groups` + `tunable_parameters` DERIVED, `two_stage_rz_tunable.sp.j2` rendered |
| seed | `setup_sizing.py --design-dir .` | tuning `.sp` rendered at the registry's seeds; simulates clean in ngspice |
| runner | `generate_sizing_runner.py` | all 4 spec keys reachable, no hook needed |
| iterations | `mechanism_demo/run_sizing_two_stage_rz.py --iter 0..3` | real ngspice runs, a handful of hand-picked moves |
| fold | `fold_wide_devices.py --apply` | forced past the 100 µm bin, folded through the template |
| finalize | `finalize_netlist.py` | `two_stage_rz_final.sp` promoted |

The `--set` moves were chosen to exercise the machinery, **not** by the
judgment loop `schematic-agent` runs. No `book_keeper.log`, no
`sizing_report.log`, no convergence claim. `circuit_decomposition.yaml` still
has `confirmed_by_user: false` and there is no `spec_analysis.log`.

## What it demonstrates

- `MN4_W` / `MN4_L` are **one variable each across `XMN4`/`XMN3`/`XMN5`** —
  setting `MN4_W=60` moved all three legs, and the fold's `w 140 -> 70` moved
  all three with every `m` doubled (5/3/7 -> 10/6/14), preserving both the
  totals and the 5:3:7 ratio.
- `MN3_M` moves `XMN3` alone — the ratio is still tunable.
- `MP3_M` is **refused** by the loop (`tunable_by: fold`), so `m` outside a
  ratio carrier is enforced-frozen rather than merely documented.
- The seed shift is reported, not hidden: `XMN3` 102.6 -> 94.4 µm (-8.0%) and
  `XMN5` 345 -> 330.4 µm (-4.2%) when the family is re-expressed on one unit
  device, which drops UGBW 11.46 -> 9.43 MHz at the seed. Expected, and the
  reason Step 1 prints `<-- seed moved this leg`.

## The before-picture

`designs/test_miller_ota_0907/` is the run made under the OLD behaviour, kept
deliberately. Its mirror finished at `w = 47.2 / 85 / 100 µm` on one gate net —
three different unit devices, met spec, and is not a mirror.

## To turn this into a real design

Run `schematic-agent` on it from the front door (intake -> checker -> circuit
read confirmation -> spec analysis -> testbench audit -> sizing -> shaping).
Nothing here substitutes for that.
