---
name: design-sheets-checker
description: >-
  Validation gate for a design not yet run through this project: reads the
  `design-sheets-intake` folder and validates it (never interviews or creates).
  Smoke-tests ngspice/Magic/netgen; checks the netlist in two stages
  (device-parameter validity, then electrical rule check); then structural
  check, output-node save fix-up, pre-layout sim, measurability, viability and
  spec-sanity gates, plus a one-off DRC/LVS pre-pass when a layout was supplied.
  Returns READY/BLOCKED/INCOMPLETE/TESTBENCH INCOMPLETE + WHICH FILES IT
  MODIFIED. Never resizes the netlist unasked (2a proposes) except the PDK
  model-bin fold; never authors a target_spec.json or testbench. Use after intake.
---
# Design Sheets Checker

**Paths.** Run from the project root. `<design_dir>` = `<design_root>/<project_name>/`.
Markdown links cite, not act: `../../reference/X` opens as `.claude/reference/X`.
**Two names:** `<project_name>` = folder; `<design_name>` = netlist `.sp` basename.

## Step 0 -- read the folder intake produced

This skill does not interview, collect, author or file anything; intake did.
Read paths off the folder, not intake's hand-off text. Every failure below is
`INCOMPLETE`: never author, derive or repair a missing input (the spec and
testbench carry decisions only the user can make).

Intake's **tracker** arrives with it -- Steps 2 and 4c continue it; on a
`TESTBENCH INCOMPLETE` lap the `testbench measurable` row is the only place the
`lap N of 3` count survives. If it didn't arrive, ask the agent that invoked you.

| Check | Missing or broken means |
|---|---|
| `netlist/`, `testbench/`, `spec/target_spec.json`, resolvable **PDK** on disk | intake didn't finish; name the gap |
| Every file readable; each sub-netlist in the closure included | a path that resolved during intake no longer does |
| `user_inputs/` present and non-empty | intake didn't back up |

## Step 0b -- the toolchain must actually launch (hard gate)

`python .claude/skills/design-sheets-checker/script/check_eda_tools.py [--magic-bin PATH] [--netgen-bin PATH] [--pdk-root PATH] [--pdk <pdk>]`

Launch ngspice/Magic/netgen (a tool can exist yet fail silently). **Exit 1
stops**: name the failing tool/path. Run once per design, before anything else.

## Step 1 -- confirm the folder, and that its includes resolve

| Folder | Your access |
|---|---|
| `user_inputs/` | read-only, always |
| `netlist/` | may rewrite (Step 2a, with approval) |
| `testbench/` | may rewrite (Step 4a) |
| `spec/`, `layout/` | read-only |

Nothing is moved, copied or deleted. Verify the testbench's `.include` resolves
(`../netlist/<design_name>.sp`). A loose `.sp`/`.spice` in the design root is
**reported**, not tidied. An include naming no existing file is `INCOMPLETE`.

## Step 2 -- netlist validity (two stages, in this order)

Both run `script/run_erc_check.py`. Read `<netlist>`/`<deck>` off the folder;
for a hierarchical `netlist/`, `<netlist>` is the **top-level** `.sp`.

### Step 2a -- device parameter validity

`python .claude/skills/design-sheets-checker/script/run_erc_check.py <netlist> --params-only`

Run against `netlist/`, never `user_inputs/`. Four findings, all **exit 1**:

- **`w`/`l` in SI metres** -- `w=6e-06` reads as microns, matches no bin (`w=6u` same).
- **Unsubstituted placeholder** -- `nf=ZZZ`, `w=XXX`.
- **Non-positive geometry** -- `w=0`, `l=-1`.
- **Per-copy `w` >= HALF the model's widest bin** -- rule `w_per_copy < 0.5 x wmax`.
  **Fold, never resize**; `m` is the lever, `nf` is not.

**Bin fold -- the one finding applied, not proposed:**
`python .claude/skills/schematic-sizing/script/fold_wide_devices.py netlist <netlist> [--dry-run]`

Fold every over-bin device in place, re-run 2a to confirm exit 0. Applied
because it changes nothing objectionable: same total width (`w x m`), topology
and nets; `nf` untouched. Copy count shifts the op point, so always re-simulate
(Steps 4/5). Report per device, Step 8.

**Correction loop -- every other finding is proposed, never applied:**

1. Report every finding; propose the exact change (old line above new line).
2. Ask via `AskUserQuestion`; say what it does *not* touch (topology, nets, `nf`/`m`).
3. Declined -> `BLOCKED`; accepted -> apply in place, `user_inputs/` untouched,
   header comment, **re-run and confirm exit 0** (never just "applied").

If the re-run still exits 1, stop -- the diagnosis was wrong.

| Finding | Proposal |
|---|---|
| Unit scale | Rescale every `w=`/`l=` by 1e6 -- with count + samples |
| Placeholder | **Never a number of your own**; use a sized sibling, else ask |
| Non-positive geometry | **No safe proposal exists** -- report and ask |
| Per-copy `w` past the bin | **Applied**, by the fold above |

If the netlist is clean, do nothing. Update the tracker with the count.

### Step 2b -- electrical rule check

`python .claude/skills/design-sheets-checker/script/run_erc_check.py <netlist>` and `... <deck>`

Run against the **corrected** netlist (full run re-includes 2a).

- **Exit 1** -- shorts, duplicate instance names, port-count mismatches,
  conflicting voltage-source drivers, element-prefix/model-card mismatches,
  everything from 2a. **Stop**; report each `fix` string.
- **Exit 2** -- floating/open nets, unused ports, no ground/model source, a
  deck with no analysis or `.control` that never `run`s. Report, proceed.
- **Exit 0** -- proceed.

**Element prefix vs model card.** A `.subckt`-only card with a primitive prefix
(`M0 … <model>`) fails as an empty netlist; a card with both `.model`+`.subckt`
used with `X` breaks netgen LVS as a phantom open. PDK off-disk = INFO.

## Step 3 -- structural check

`python .claude/skills/design-sheets-checker/script/check_design_sheets.py <design_dir> [--pdk <pdk>] [--netlist PATH]`

| Input | Checked |
|---|---|
| Netlist `*.sp` | exactly one **top-level** `.subckt`, an `.ends`, no behavioral source (`E`/`G`) |
| Testbench `*.spice` | at least one analysis + `.control`; `.include`s the netlist; names its `.subckt` |
| Layout `*.gds` | optional -- valid GDSII `HEADER`; INFO if absent |
| PDK | directory exists under `$PDK_ROOT` |
| Target spec | required, and must follow `spec_form_template.md` |

`user_inputs/` never scanned. Hierarchical designs are normal; only a genuine
top-level tie fails (`NETLIST: AMBIGUOUS`), settled with `--netlist`. `.ac`
is not required -- any of `.ac/.dc/.tran/.op/.noise/.disto/.pz/.sens/.tf/.four`
passes; a deck with none fails. **Exits:** `0` sane; `1` missing/ambiguous/
malformed input or a malformed GDS -- **stop**; `2` `target_spec.json` off-form
-- **hand back `INCOMPLETE`**.

#### The target spec must follow `spec_form_template.md`

`spec_form_template.md` is the normative form; one object per key, e.g.
`{"PM": {"Direction":"RANGE","Value":[50,70],"Units":"degree"}}`. `Direction`
is `FLOOR` (`sim >= Value`), `CEILING` (`sim <= Value`) or `RANGE`
(`Value[0] <= sim <= Value[1]`); `Value` matches it; `Units` non-empty (`"-"`
if dimensionless). All three mandatory. Non-conforming -> `INCOMPLETE`: **ask
the user** to correct it (naming every failing key) -- about form, never
supplying a target nobody set.

## Step 4 -- output-node save check + pre-layout sim

You **write** the output node's `write` line (4a); **check** every key's measurability (4c).

### Step 4a -- the output-node fix-up

Step 3 prints `OUTPUT NODE SAVE CHECK` with one mark:

- `+` found and saved -- nothing to do.
- `!` found, not saved -- add `write ./<design_name>_pre.out v(<node>)` to the
  deck Step 3 resolved, in place; declare it in Step 8.
- `?` no port matched, or several -- ask; once the user names the port you are
  in the `!` case (usually port-naming: `vref`/`clk`/`Q`).

Run the deck with `<design_dir>/testbench/` as cwd. Measure the registered
metrics: copy `.claude/reference/compute_fidelity.py` into
`<design_dir>/compute_spec.py` and read its registry. Uncovered keys have no
pre-layout number -- report "carried, not checked at this stage". Carry numbers
forward; Steps 5-7 reuse them.

### Step 4b -- classify what ngspice said

Run is against an **un-sized** netlist, so `W`/`L` diagnostics are expected.
- **Device-parameter class -- record, don't report**: one line,
  `device parameters (pre-sizing, not acted on): N`.
- **Testbench-setup class -- report, loop** (missing analysis, `.control` never
  `run`s, unresolved `.include`, `write` naming a nonexistent node).
- A device-parameter error so severe that **no raw file was produced** is
  suppressed but stated plainly -- "never ran" vs "ran and measured nothing".

### Step 4c -- is every spec key MEASURABLE? (the correction loop)

A deck can run cleanly yet not measure a spec key -- silent by construction.
Check **statically** (read `.control`). For every key, confirm the deck has
**both an analysis that produces it and a save that writes it out** -- derive
the pair from what the key measures; no lookup table. The odd key out is the
one most likely missing; `Power` is the standing example (needs `op` + a
supply-current save).

Record every failing key (which half is missing + the exact line), then **keep
going through Steps 5-7**. On `TESTBENCH INCOMPLETE`: the agent corrects the
deck, you re-run Steps 3-4. **Bounded at 3 corrections**; the count lives in
the tracker's `testbench measurable` row.

## Step 5 -- netlist viability gate

Runs before Step 7, needs no target. **Stop and notify if the netlist doesn't
work**: ngspice errored / no raw data, or any value NaN/Inf. Then judge per
registered metric whether the circuit did what it exists to do -- an unresolvable
value, pinned at the swept-range edge, or a sign/magnitude that says it isn't
operating. No layout work fixes any of this -- show the numbers, get direction.

## Step 7 -- spec sanity gate

**There is no Step 6** -- retired, so references to 7/7b/8 stay valid.
Compare Step 4's numbers against `spec/target_spec.json`; compare only the keys
Step 4 measured. Read each key's `Direction`/`Units` off the spec file.
**Convert units before comparing** (simulators report SI base units; spec files
write scaled ones). "Far from target" is judged by the gap's shape, respecting
direction: outside the physically meaningful range; off by a multiplicative
factor (~5x) for orders-of-magnitude quantities; off by many natural steps for
additive ones (dB/deg/V). Unmeasured keys are **carried, not checked**. Hard
gate: get direction before Step 8.

## Step 7b -- starting-layout DRC/LVS *(only if a layout was supplied)*

Skip entirely when `layout/` doesn't exist (the common case); say so. An
incomplete layout still runs. **Read intake's 6c counts first** -- LVS against
a knowingly short set reports devices intake already said were missing, not a
wrong GDS. **Load the whole hierarchy into Magic** (primitives + submodules +
top) or LVS reports phantom missing devices. Then DRC and LVS per
`layout-fixer.md`'s Steps 1-2, checks only (mind `.sp`->`.spice` and the
`R`-vs-`X` quirk). **Do
not fix it, do not fold it into the verdict** -- a dirty layout is not a
design-sheets defect. Report on its own Step 8 line; caller chooses: pre-pass,
or stop.

## Step 8 -- return a summary to the agent that invoked you

The caller continues on **your** output. Emit:

```
DESIGN SHEETS CHECK -- <project_name>                   VERDICT: READY

FILES MODIFIED (work from these, NOT what you sent)
  netlist    CORRECTED  netlist/<design_name>.sp  -- 22 w/l rescaled SI->microns (approved; re-run exit 0)
  testbench  CORRECTED  testbench/<deck>.spice    -- `write` line added for v(<node>)
MANDATORY INPUTS  netlist OK · testbench OK · target spec OK (4 keys) · PDK OK
CHECKS  0b toolchain · 1 folder · 2a parameters · 2b ERC · 3 structural (name netlist+deck)
        4a output save FIXED/PASS/ASKED · 4b sim RAN · 4c measurable (lap N of 3)
        5 viability · 7 spec sanity (keys compared + carried) · 7b layout SKIP/FAIL
ALSO FOUND (omit only when the verdict is the sole finding)
LAYOUT PATH  generate from scratch (or supplied GDS path) · CARRY FORWARD pre-layout numbers
```

**`FILES MODIFIED`** names the two things this skill rewrites (2a's rescale after
approval, 4a's `write`); the `.include` re-point is intake's. State it even when nothing changed.

**VERDICT is one of four, never softened:**

| Verdict | Meaning |
|---|---|
| `READY` | every gate passed; the design can go to layout |
| `BLOCKED` | a hard gate failed (2a/2b exit 1, Step 3 exit 1, Step 5), or the user declined 2a's proposal / the approved edit didn't clear the re-run |
| `INCOMPLETE` | the folder was short: missing input, `user_inputs/` absent/empty, unresolvable include, or `target_spec.json` missing/unparseable/off-form |
| `TESTBENCH INCOMPLETE` | 4c found an unmeasurable key -- a lap, not an ending; name every key, which half is missing, the exact line |

`TESTBENCH INCOMPLETE` never coexists with `READY`. **Precedence:**
`INCOMPLETE > BLOCKED > TESTBENCH INCOMPLETE > READY`, and report the losing
findings anyway (`ALSO FOUND`) -- a `BLOCKED` netlist with unmeasurable keys
should be fixed in one lap. Print the tracker with every row settled.

## ERC reference

Three checks (`no_ground_reference`, `no_model_source`, the testbench-sanity
pair) gate on `top["devices"]` being non-empty, so a bare netlist reports clean;
`.include` is followed, unresolvable = INFO. ERC checks text and connectivity
only -- no layout/device physics (spacing, antenna, latch-up, ESD);
`no_model_source` is a presence check, not name resolution.

## What this skill does NOT check

- **DRC/LVS** (7b is a one-off pre-pass; the full gate is `layout-fixer`'s).
- **Netlist naming conventions** -- structure and values, not names.
- **Whether a model NAME exists** in the PDK -- 2b validates defined models'
  prefixes; a name defined nowhere skips in silence. Values are checked in 2a.
- **Whether the GDS is this netlist** at Step 3 (GDSII parse only); 7b's LVS answers it.
- **`layout/` completeness** -- intake's 6c counts that; extraction is
  `../layout-extractor/SKILL.md`'s job, not this checker's.
- **Anything beyond the spec's form** -- reachability is schematic-agent's #3;
  whether the numbers are *right* is the user's.

## Files in this skill

- `spec_form_template.md` -- the normative `target_spec.json` form.
- `script/check_eda_tools.py` -- toolchain smoke test (0b).
- `script/run_erc_check.py` -- parameter validity + ERC (2a/2b).
- `script/check_design_sheets.py` -- structural checker (Step 3).
- The **bin fold** is no longer this skill's script -- 2a runs
  `../schematic-sizing/script/fold_wide_devices.py netlist` (reads this skill's
  `run_erc_check.load_pdk_bin_widths()`).

## Relationship to other skills

- **`../design-sheets-intake/`** produces this skill's whole input: questions
  about *what the inputs are* belong there; judgments about *whether they are
  sound* belong here. The one question here is Step 3's spec-**form** correction.
- **schematic-agent** re-runs `run_erc_check.py` at its #1 as confirmation, not
  a substitute for this gate.
- **Nobody authors testbenches; two things correct them** -- 4a adds a missing
  `write`; everything else is `TESTBENCH INCOMPLETE`, corrected by
  schematic-agent's #4a.
- **`../schematic-sizing/`** assumes Step 2 passed; it splices op-probes per
  iteration (structural requirements schematic-agent's #4b checks).
- **Absorbed the former `sanity-checker` skill** -- tool smoke-test is 0b,
  starting-layout DRC/LVS is 7b.
