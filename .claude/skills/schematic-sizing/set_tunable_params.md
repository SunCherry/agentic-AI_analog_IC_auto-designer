# Setting tunable parameters, per structure

**The registry that used to live here has moved.** It is now
`../circuit-decomposition/pattern-table.md`'s **Tie group** field — one row per
pattern — and it is no longer a description of a judgment call. It is read by
`../circuit-decomposition/script/build_tunables.py`, which derives every tie
group and every variable from it. Changing a row there changes what sizing may
move.

This file is kept as the explanation of *why* it works that way, because the
failure it prevents is silent.

## Why this moved

Some structures cannot be sized one device at a time. Sizing one half of a
matched pair breaks the symmetry the design depends on; sizing a mirror's
branches independently loses the ratio. Both are silent defects, not errors.

The rules were written down twice — here in prose for an agent to apply, and
again as a detector inside `setup_sizing.py` writing `structure_groups.json`.
Two sources for one fact drift, and they did. On `test_miller_ota`:

| | said |
|---|---|
| `circuit_decomposition.yaml` | nfet mirror `XMN4`/`XMN3`/`XMN5` is one group, `tied: [w, l]`, `ratio_carrier: m` |
| `structure_groups.json` | six independent variables — `MN3_W`, `MN3_L`, `MN4_W`, `MN4_L`, `MN5_W`, `MN5_L` |

The loop believed the JSON and tuned the three legs to `w = 47.2 / 85 / 100 µm`
— three different unit devices on one gate net, which is not a mirror and
cannot be laid out as one. Nothing failed; the numbers met the spec. It was
only visible by reading the netlist.

So the rule moved into code, and the *grouping moved into the netlist itself*.

## How it works now

`circuit-decomposition` emits `<top>_tunable.sp.j2` — the netlist with each
tunable replaced by `{{ VAR }}`:

```
XMN3 net3 net7 VSS VSS ...nfet l={{ MN4_L }} w={{ MN4_W }} nf=1 m={{ MN3_M }}
XMN4 net7 net7 VSS VSS ...nfet l={{ MN4_L }} w={{ MN4_W }} nf=1 m={{ MN4_M }}
XMN5 vout net7 VSS VSS ...nfet l={{ MN4_L }} w={{ MN4_W }} nf=1 m={{ MN5_M }}
```

**Tying is now structural rather than clerical.** The three legs share the
literal placeholder, so one value writes all three on every render. There is no
per-device value to forget, and no second file to disagree with the first.

- **`m` is a real sizing variable wherever a pattern names it `ratio_carrier`**
  (`current_mirror`, `cascode_current_mirror`, `resistor_ladder`,
  `capacitor_bank`), and frozen everywhere else. Each variable carries
  `tunable_by: sizing | fold`.
- **`nf` is never templated** — `device-shaper` chooses it after sizing
  converges.
- **There is no "ratio conflict".** Legs written with different `w` do not
  defeat a shared `w`: one shared *unit* `w`/`l` times a per-leg integer `m`
  reproduces any ratio to within one unit, and is also how the mirror is drawn.

## What still needs your judgment

**The seed moves the bias point, and that is expected.** Re-expressing a family
on one unit device changes each leg's total width by up to one unit —
`ratio_seed` records exactly how much, and Step 1 prints
`<-- seed moved this leg` for anything past a few percent. On
`test_miller_ota`: `XMN3` 102.6 → 94.4 µm (−8.0%), `XMN5` 345 → 330.4 µm
(−4.2%). **Re-establish the family's bias** (`id` across reference and legs,
`vds` vs `vdsat`) before chasing any spec.

**Review the derived groups against the circuit read.** Detection is still a
heuristic over wiring: a mirror whose reference is not diode-connected is
missed, and two unrelated devices that share a rail can be over-grouped. A
structure the read names but the Step 1 report does not is exactly the case to
check.

**Seeding stays per-copy, never `w * m`.** A binned model is valid only up to
some `w_max` per copy; a total can land past it, and then no model card matches
and the iteration measures nothing. `setup_sizing.width_bounds()` reads each
bound from the PDK's model cards at the moment it checks, never cached.

## Adding a structure

1. Add a row to `../circuit-decomposition/pattern-table.md` — the pattern, its
   **Tie group** field, its **Matched nets** field.
2. Add its detector to `../../reference/detect_topology.py`.
3. Add its row to `TIE_RULES` in
   `../circuit-decomposition/script/build_tunables.py`. A pattern absent from
   that table gets `DEFAULT_RULE` — untied, its own free `w`/`l` — which is
   what the table's `none` rows mean, so a `none` pattern needs no row.

Do not add a detector here. There is one place these rules live.
