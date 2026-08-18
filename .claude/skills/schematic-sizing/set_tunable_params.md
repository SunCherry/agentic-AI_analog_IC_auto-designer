# Setting tunable parameters, per structure

**Input: a partial netlist** -- the device lines of ONE structure and nothing
else, every device involved included. **Output: which tunable variables that
structure gets, and what each one starts at.**

Some structures cannot be sized one device at a time. Sizing one half of
a matched pair breaks the symmetry the design depends on; sizing a mirror's
branches independently loses the ratio. Both are silent defects, not errors, so
each structure gets a stated rule below, and `setup_sizing.py` records it in
`structure_groups.json` -- the map `edit_netlist.py` writes values through, so a
group moves as one by construction rather than by discipline.

**This is a registry.** Each structure is one row in the table plus one section.
To add a structure, add both -- see "Adding a structure".

## The registry

| # | Structure | Recognized by | Shared variables | Per-device variable | Never tunable |
|---|---|---|---|---|---|
| 1 | **current mirror** | one device with `drain == gate` (the reference), plus every other device sharing its `(model, gate, source, bulk)` | `<ref>_W`, `<ref>_L` -- reference and every branch | `<branch>_M` per branch -- the mirror ratio | reference `m` (forced to literal `1`), `nf` |
| 2 | **matched group** (differential pair, and any set that must move together) | same `model`, same `source` **and** `bulk`, identical original `w`/`l`/`m`/`nf` | `<first>_W`, `<first>_L` -- every member | -- | `m`, `nf` (both keep their original, already-identical value) |

**Starting values:**

| Structure | Variable | Seeded from |
|---|---|---|
| current mirror | `<ref>_W` | the reference's own **per-copy** `w`, never `w * m` |
| current mirror | `<ref>_L` | the reference's own `l` |
| current mirror | `<branch>_M` | `max(1, round(branch_w * branch_m / (ref_w * ref_m)))` -- the golden netlist's own branch-to-reference total-width ratio |
| matched group | `<first>_W` / `<first>_L` | the group's own (identical) `w` / `l` |

**Two rules that hold across every structure:**

- **Detection order is table order.** Structure 1 claims its devices first; only
  the devices left over are considered for structure 2. A device belongs to one
  structure.
- **Variables are named after one member** -- the reference for a mirror, the
  first line encountered for a matched group -- with `_W` / `_L` / `_M`
  suffixed to the instance name minus a leading `X` (`XMN1` -> `MN1_W`).

## 1. Current mirror

Partial netlist in (reference first here, but line order does not matter):

```
XMN4 net7 net7 VSS VSS sky130_fd_pr__nfet_01v8 l=0.15 w=47.2 nf=1 m=5
XMN3 net3 net7 VSS VSS sky130_fd_pr__nfet_01v8 l=0.15 w=34.2 nf=1 m=3
XMN5 vout net7 VSS VSS sky130_fd_pr__nfet_01v8 l=0.15 w=43.1 nf=1 m=8
```

Variables out: `MN4_W`, `MN4_L` (all three devices), `MN3_M`, `MN5_M`. The
reference's `m` goes in the group file's `fixed` block, pinned to 1.

- **Detection is topological, not value-based.** `drain == gate` identifies the
  reference. It deliberately does **not** require the family's `w`/`l`/`m` to
  match, because a real mirror often expresses its ratio through
  independently-sized devices -- as the example above does. A value-matching rule
  would never group those three.
- **One shared `W`/`L` makes every finger in the family the same physical unit
  device**, which is the standard layout technique for mirror matching. What a
  real design varies branch-to-branch is the **ratio**, so that -- and not an
  independently sized `w` -- is the branch variable.
- **The reference's `m` is forced to `1`**: it is the one-copy unit the rest of
  the family counts in.
- **Seed `<ref>_W` from per-copy `w`, never `w * m`.** A binned model is valid
  only up to some `w_max` per copy; the total can land past it, and then no model
  card matches and the iteration measures nothing. Per-copy is valid by
  construction -- the netlist already used it. Each variable's own `w_max` is
  read from the PDK's model cards at the moment it is checked
  (`setup_sizing.width_bounds()`), never cached.
- **So the starting point does not reproduce the original bias currents.** A
  smaller unit shifts the family's equilibrium, and a downstream device can land
  in triode. **Expected, not a bug** -- re-establish the family's bias (`id`
  across reference and branches, `vds` vs `vdsat`) before chasing any spec.
- **A gate node with zero, or more than one, diode-connected member is
  ambiguous** and is left alone -- it falls through to structure 2.
- `--no-auto-mirror` disables detection entirely.

## 2. Matched group

Partial netlist in:

```
XMN1 net4 Vin net3 VSS sky130_fd_pr__nfet_01v8 l=0.15 w=40 nf=1 m=8
XMN2 net5 Vip net3 VSS sky130_fd_pr__nfet_01v8 l=0.15 w=40 nf=1 m=8
```

Variables out: `MN1_W`, `MN1_L` (both devices). No per-device variable.

- **The differential pair is the canonical case, not the only one** -- a
  PTAT/CTAT branch pair, a comparator's latch halves, the legs of a delay cell.
  Which symmetry a design depends on is a circuit question; breaking any of them
  mid-loop is the same defect.
- **`gate` and `drain` are deliberately NOT required to match** -- those are
  exactly the terminals that differ between a pair's two halves. Only
  `source`/`bulk` (the shared tail or rail node) must match.
- **One shared variable is structural prevention**, not a discipline to
  remember: `--set MN1_W=45` writes both halves, because it addresses the
  variable rather than a device. The failure it prevents is real -- sizing one
  half without the other, caught only by re-synchronizing by hand every
  iteration. `edit_netlist.check_groups()` reports a pair that has drifted apart
  anyway, which a hand edit can still do.
- `--no-auto-group` falls back to one variable per instance. **If you use it,
  merge the members by hand in `structure_groups.json`** -- put them in one
  variable's `members` list -- or that failure is back.

## Reviewing what the script decided

**Every detection is a heuristic over wiring and values, so review the printed
report before the first iteration.** `setup_sizing.py` lists every family
and group it found, with each branch's starting `M`. Cross-check it against the
confirmed circuit read in `<design_dir>/circuit_decomposition.yaml`.

- **Read this file on mismatch, not only on detection.** A structure the circuit
  read names but the report does *not* is exactly the case to check.
- Structure 1 can miss a mirror whose reference is not diode-connected.
  Structure 2 can **over-group** two unrelated devices that coincidentally share
  size and rail connections, or **under-group** a topology it does not recognize.

## Interaction with the fold

A structure folds as **one unit**: a single factor multiplies every member's `m`
-- reference and branches together, both halves of a pair together -- so totals
and the mirror ratio survive. Folding one member and not its partner is the
symmetry break the shared variable exists to prevent. See `SKILL.md`'s Step 2a.

## Adding a structure

1. Add a row to **The registry**, and a row per new variable to **Starting
   values**. State what is never tunable.
2. Add a numbered section: one partial netlist in, the variables out, then
   bullets for the detection rule and the seeding rule.
3. Place the row in detection order -- earlier rows claim their devices first.
4. Implement the detector in `script/setup_sizing.py` alongside
   `_find_mirror_families()` / `_build_groups()`, have it emit the group into
   `structure_groups.json`, and have it print what it found so the review step
   above still works.
