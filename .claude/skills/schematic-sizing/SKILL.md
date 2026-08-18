---
name: schematic-sizing
description:
  Sizes a fixed-topology golden netlist to its target spec, any circuit
  class. Works out which devices set each spec from the netlist, circuit
  read and `spec_analysis.md`, then tunes `W`/`L` over real ngspice
  iterations -- reasoning from op-point evidence and a logged history,
  never searching. Judges a target unreachable and says so. Ends with a
  finalized `.sp` and a report of every spec against both the real and the
  harder bar. Runs after validation -- see "When to use this".
---
# Schematic Sizing

## When to use this

Five preconditions, and producing none of them is this skill's job:

1. netlist, testbench, `spec/target_spec.json` and the PDK all filed under
   `<design_dir>/`;
2. the design is validated -- ERC-clean, device parameters valid, and it
   simulates;
3. a confirmed circuit read exists in `circuit_decomposition.yaml`;
4. `spec_analysis.md` exists, with each key's direction and plausibility;
5. the testbench measures every spec key and takes the op-probe splice.

Then, and only then -- before any layout, DRC, LVS or PEX. **This is the last
stage that may change a device's `W`/`L`**: the netlist it produces is the one
frozen for layout.

## The workflow

```
   Step 1   setup_sizing.py           -> <d>_tuning.sp + structure_groups.json
   Step 1a  generate_sizing_runner.py -> run_sizing_<d>.py   (UNRESOLVED = STOP)
            compute_harder_target.py  -> harder_target_spec.json
            ASK THE USER for N iterations
   Step 2   THE TUNING LOOP -- you decide, the runner measures
            (Step 2a folds any w over the PDK max first)  <-- log it, then re-read
   Step 3   finalize_netlist.py       -> <d>_final.sp + sizing_report.md
```

**Three exits are stops, not laps**: `UNRESOLVED` from Step 1a, a spec key no
extractor can measure, or the budget spent -- each reports honestly rather than
pretending.

## Working folder

Everything lands under `<design_dir>/sizing/`, never outside the user's tree
(`run_sizing_iteration.py`'s temp dirs are deleted on exit).

```
<design_dir>/sizing/
  run_sizing_<d>.py        the runner (Step 1a) -- resolved facts in CONFIG,
                           plus a hand-written measure() hook
  harder_target_spec.json  the bar Step 2 tunes against
  <d>_tuning.sp            THE LOOP'S STATE -- real W/L; every --set writes
                           straight into it (Step 2a folds m= here too)
  structure_groups.json    which instances share a variable / carry a tunable M
  book_keeper.md           THE RECORD -- budget N at its head, one entry per iteration
  sizing_report.md         what the loop achieved, and the margin left
  <d>_final.sp             THE HAND-OFF, promoted at Step 3 -- TOP LEVEL ONLY
  <sub>.sp                 each tuned sub-circuit, under its ORIGINAL name
  <d>_<analysis>.png       the converged response, if a figure was wanted
  debug/iter_<n>/          only on --save-artifacts
```

**Naming is a contract.** Every tuned netlist -- top level and sub-circuits --
lands in this one folder, and exactly one is renamed: **the top level gains
`_final`** (`<d>_final.sp` is the name downstream resolves by, not optional and
not spellable another way); **every sub-circuit keeps its original filename**,
no suffix at all. The asymmetry marks which file is the hand-off, and leaving
sub-circuit names alone means the top level's `.include` lines resolve inside
`sizing/` exactly as they did in `netlist/`, with no rewriting.

**Nothing persists by itself.** An iteration runs in a temp dir and hands back a
result; **you** turn it into a `book_keeper.md` entry, and an iteration never
logged is a measurement that no longer exists. `--save-artifacts` (or `--json
<path>`) keeps an iteration's rendered files for debugging; `_final.sp` comes
from `finalize_netlist.py` at Step 3.

**Two logs, no overlap.** `book_keeper.md` is the per-iteration tuning record.
`<design_dir>/operations.log` is the design's file-level record across the whole
schematic stage: one row `by sizing` per artifact as it is first written --
never per iteration.

## Hierarchical designs

A top-level netlist may nest -- further `.subckt` blocks in the same file, or
separate `.sp` files pulled in by `.include`, nesting again. Do not assume flat.
**A device that sets a spec key is wherever the topology puts it**, possibly two
levels down inside an `ota_core`, so read `circuit_decomposition.yaml`'s
hierarchy diagram and establish which level each tunable lives at before the
first iteration.

- **Instance names are unique only within their level.** Two sub-circuits may
  each hold an `XM1`. Qualify by path (`ota_core.MN1_W`, not a bare `MN1_W`)
  whenever a design has more than one level, so a `--set` cannot land on the
  wrong device.
- **A sub-circuit instantiated twice is sized once** -- editing its `.subckt`
  body changes every instance, which is the intent for a matched block and a
  trap when two were meant to differ. If they must differ, that is two `.subckt`
  definitions and a finding for the user, never something to fix by editing the
  shared body and hoping.
- **Tie groups can cross levels.** `circuit_decomposition.yaml`'s tie groups,
  not file boundaries, say what moves together.

> **Implementation gap -- read before running a nested design.** `script/` does
> not implement this. `netlist_devices.py` scopes itself to "a flat,
> single-`.subckt` golden netlist": `subckt_header()` takes the *first*
> `.subckt` it matches as the only one, and `parse_devices()` returns every
> level's devices in one flat list with no level label. `setup_sizing.py` then
> keys them by bare lowercased name (`by_name = {d["name"].lower(): d}`), so **a
> name repeated across two levels silently overwrites the first** -- one device
> vanishes from the tunable set with no error. Nothing follows an `.include`, so
> sub-circuits in separate files are not seen at all. Until this is fixed,
> **report a hierarchical design as unsupported** rather than run it flat and
> call it sized; the naming contract above is what its fix must satisfy, not a
> description of current behaviour.

## Step 1: tunable parameters and the working netlist

```
python script/setup_sizing.py <netlist.sp>
    -o <design_dir>/sizing/<design_name>_tuning.sp
    --groups-out <design_dir>/sizing/structure_groups.json
    --pdk <pdk>
```

Two files out, one holding values: the tuning `.sp` the loop edits directly (no
template to render, no value store that can disagree with it), and
`structure_groups.json` for the one thing a `.sp` cannot say -- which instances
move together.

**The script templates every drawn size; which ones you move is a circuit
question.** What belongs in a `--set` comes from `circuit_decomposition.yaml`
and `spec_analysis.md`'s expression per key; a device analysis never implicates
is a variable to leave alone. **Know which parameters you expect to matter, and
why, before the first iteration** -- a loop that moves everything moves nothing
in particular. Review the printed tuned/skipped list against that reading.

**Microns everywhere from Step 1 on** -- tuning netlist, PDK bound and every
`--set` share one unit (`--set <dev>_W=45` is 45um). Step 3 renders bare microns
whatever the golden netlist used.

**Three boundaries the template enforces, whatever the circuit:**

- **Drawn size only -- `W` and `L`**, including a resistor or cap sized by
  `w`/`l` on its own line. One exception: a detected current-mirror BRANCH's `m`
  (a mirror's *ratio* is a real lever; an independent branch `w` is not).
- **`nf` is never templated, on any device** -- finger count is a layout choice
  and nothing here measures one. Every device reaches Step 3 with the `nf` its
  golden netlist wrote.
- **Never an ideal source** (bias, probe, stimulus, supply) -- no drawn geometry.

**Structure-specific parameterization.** A matched pair sized on one side breaks
a symmetry the design depends on; a mirror with independently sized branches
loses its ratio. `setup_sizing.py` shapes those for you (shared `W`/`L` across a
group, a ratio variable per mirror branch), and **the rules for reading and
reviewing each structure live in `set_tunable_params.md`** -- read the section
for each structure this design has, before the first iteration. **Read on
mismatch, not only on detection**: a structure the circuit read names but the
script's report does *not* is exactly the case those rules exist for. Mirror
families are claimed first; only the devices left over are considered for
matched grouping. A new structure is a new row there, not a special case here.

## Step 1a: this design's own runner

Once per design, immediately after Step 1:

```
python .claude/skills/schematic-sizing/script/generate_sizing_runner.py <design_dir>
    --netlist <design_dir>/netlist/<design_name>.sp
    --testbench <design_dir>/testbench/<deck>.spice
    --spec <design_dir>/spec/target_spec.json --pdk <pdk>
```

It derives every fact the loop needs from the design's own files -- supply nets
and voltages, the deck's analysis and raw filenames, the `.subckt` wrapper,
device kinds, which keys anything can measure -- into the runner's CONFIG, so
none is retyped per iteration. **Read its printed report**; two outcomes need you:

- **`UNRESOLVED`** (exit 2) -- a fact it refused to guess, commonly a deck whose
  analysis has no registered extractor. Fix it before iterating.
- **`measure()` TODOs** -- spec keys no extractor produces. Per-design code no
  argument can supply: fill them in and they merge into `specs` and score by
  name. Until then they report `unmeasured`, never as missed.

**The engine is not copied** -- the runner imports `run_iteration()` from the
shared script, so an engine fix reaches every design without regenerating. It
refuses to overwrite an existing runner without `--force`, because your
`measure()` edits live in it.

## The two bars

Each key's `Direction` is a field in `target_spec.json` (validated upstream), so
nothing here classifies a key on its own. A legacy flat spec declares nothing,
so `CEILING_METRICS` supplies the direction and everything else defaults to
FLOOR; where that is wrong, `spec_analysis.md`'s `CEILING*` marking is the
override.

| `Direction` | met when | what the harder target does |
|---|---|---|
| `FLOOR` | `sim >= Value` | `x (1 + margin)` -- a higher bar |
| `CEILING` | `sim <= Value` | `x (1 + margin)` -- more allowance, on purpose |
| `RANGE` | `Value[0] <= sim <= Value[1]` | each bound moves inward by `margin` of the width |

```
python script/compute_harder_target.py <design_dir>/spec/target_spec.json \
    -o <design_dir>/sizing/harder_target_spec.json   # --margin, default 10%
```

**Why aim past the spec.** This loop tunes against the ideal schematic, and the
netlist has to survive layout parasitics nobody can predict before a layout
exists. A netlist landing exactly on its target has nothing left to lose, so aim
harder from the first iteration.

| Bar | Status |
|---|---|
| `target_spec.json` (the REAL spec) | **must be met.** A key short of it is a genuine shortfall, whatever the budget did |
| `harder_target_spec.json` (real + margin) | **aim for it.** The parasitic buffer layout will spend; meeting every key here is the early-stop |

**The goal: meet every key of `target_spec.json` at minimum, and make a real
effort at `harder_target_spec.json` on top.** Aiming at the harder bar is not
optional effort -- it is how a netlist arrives at layout with anything left to
lose -- but falling short of it while clearing the real spec is a *qualified
success*, not a failure.

**Score and report every key against BOTH files, always.** A run reporting only
the harder bar cannot distinguish a design that cleared its real spec from one
that cleared neither, and those call for different decisions from the caller.

## Loop budget

**Ask the user how many tuning iterations this run may spend, and do not start
without an answer.** There is no default. Record it as **N iterations** at the
head of `book_keeper.md` before the first iteration.

**Nothing enforces N** -- no script reads it or stops on it. It is a convention
you honour by counting your own entries; a loop that assumes something will stop
it does not stop. Converging early is the normal good outcome: the budget is a
ceiling, not a quota. If the user asks for a budget you can already see is too
small, say so once, then honour the number they give.

## Step 2: the tuning loop

**Every iteration is a design decision; the runner only measures it.** Nothing
searches for values. No numerical step knows *why* a spec is short -- that one
device's `gm/gds` has run out at this `L`, that the stage before is loading the
one you are looking at.

**One iteration, in order:**

1. **Which key is short, and what carries it** -- `spec_analysis.md`'s
   expression and the instances it names, not a guess from the device list.
2. **What has already been tried** -- the recent `book_keeper.md` entries, not
   just the last one. This is how you avoid repeating a direction that already
   failed, and how you spot oscillation: a spec bouncing around the same range
   without progress means change *which* parameter you move, not push harder on
   the same one.
3. **Where the limit actually is** -- last iteration's op-point data; confirm
   the device you mean to move is the one that is limiting.
4. **Decide, and state the mechanism you expect** -- which parameter, which
   direction, what should move. Then run Step 2a's fold check and measure.
5. **Log what happened against what you predicted.** A missed prediction is the
   most useful entry; a lucky improvement you cannot explain teaches nothing.

```
python <design_dir>/sizing/run_sizing_<design_name>.py --iter <n>
    [--set <dev>_W=45.2,<dev>_L=0.2] [--save-artifacts]
    [--json <design_dir>/sizing/debug/iter_<n>/result_iter<n>.json]
```

**That is the whole invocation** -- netlist, testbench, params, PDK, supply
nets, analysis, attrs and out-root all live in the runner's CONFIG from Step 1a,
and `--target-spec` defaults to `harder_target_spec.json` when one exists. The
shared `script/run_sizing_iteration.py` still runs directly with the same facts
as flags; the runner exists so none has to be retyped.

Each call returns whatever the analysis's registered extractor measures
(`compute_fidelity.EXTRACTORS`, selected by `--analysis`; `ac` yields gain,
UGBW, phase margin) **plus every MOS device's
`gm`/`gds`/`id`/`vds`/`vgs`/`vdsat`/`cgg`** at that operating point. Use the
op-point data for the *reasoning* behind the next change, not just pass/fail:

- `gm/id` -- a device's inversion level; read it against this PDK's gm/Id
  characterization to judge whether a `W`/`L` change will move `gm` as expected.
- `vdsat` vs `vds` -- headroom. A device near `vdsat == vds` is nearly out of
  saturation; widening restores headroom without necessarily fixing the spec.
- `gm/gds` (intrinsic gain) -- what to check when a **small-signal gain** key is
  short: is the shortfall the device's own `gm/gds`, or loading (which shows up
  as the *speed* keys moving)? If `gm/gds` is already at what the devices give
  at this `L` and supply, **the target may be out of reach -- say so rather than
  spending the rest of the budget.**
- **Which quantity to read for which key is `spec_analysis.md`'s answer, not
  this list's** -- these bullets interpret a device's numbers, they do not map
  spec keys to devices.

**The op-point data is MOS-only, and the tuning is not.** `generate_op_probe.py`
probes every MOS and raises if a netlist has none, so a design whose critical
devices are BJTs, resistors or caps still gets sized and measured -- but with no
per-device evidence to reason from, falling back to spec deltas. Say so rather
than let an empty `op_points` read as "nothing to see".

### Stop conditions -- two, and only two

1. **Every key of `harder_target_spec.json` reports `met: true`** (`sim >=
   target` per FLOOR, `sim <= target` per CEILING, `lower <= sim <= upper` per
   RANGE). Convergence: stop, do not spend budget gilding a met spec.
2. **N iterations reached.** Stop and report.

**Clearing the real spec early does NOT stop the loop** -- while budget remains,
keep aiming at the harder bar, since that margin is exactly what layout
parasitics will eat. At stop, which bar was cleared decides the verdict:

| At stop | Verdict |
|---|---|
| every harder key met | **CONVERGED** -- the intended outcome |
| every real key met, one or more harder keys short | **MET, MARGIN SHORT** -- a qualified success. Name each key that fell inside the margin and by how much; it is the one most likely to fail post-layout |
| any real key short | **SHORTFALL** -- the gap per key against the REAL spec, and whether `spec_analysis.md` predicted it |

Report the best iteration honestly in all three cases. **Never a false success
-- and never call a run that met the real spec a failure because it missed the
margin.**

## Step 2a: fold a MOS wider than the PDK allows

**Runs inside Step 2's loop, before every runner call.** Above the maximum width
the PDK allows for the model, fold -- `m` up, `w` down, total width unchanged;
at or under it, leave the proposal exactly as written, using the fewest copies
the limit demands. The limit is the PDK's own, read from its model cards at the
moment of the check, not a preference: past the widest card a device matches no
card and the simulation measures nothing. `check_param_bounds` refuses exactly
that width, so this step resolves the refusal instead of walking into it.

```
python .claude/skills/schematic-sizing/script/fold_wide_devices.py tuning
    <design_dir>/sizing/<design_name>_tuning.sp
    --groups <design_dir>/sizing/structure_groups.json
    --pdk <pdk> --apply
```

Without `--apply` it reports and writes nothing; with it, it rewrites the tuning
netlist's `w=` and every member's `m=` together and prints the `book_keeper.md`
line. **Never fold by hand.**

- **The fold moves the operating point** -- the same total width in more,
  narrower copies lands on different model cards and draws different current. It
  happens *before* the run, so the numbers reported are the folded device's.
- **A variable with no recorded limit is reported, not passed** -- an absent
  limit is missing information (unreadable model cards), not permission.
- **A shared `W` folds as one unit** -- one factor on every member's `m`, so a
  matched group's symmetry and a mirror's ratio survive.

**The same operation runs once before the loop, against a stricter ceiling**:
`fold_wide_devices.py netlist` folds the golden netlist to *half* the model bin,
so a device enters the loop with room to be tuned up before it needs a fold.

## Logging: `book_keeper.md`

**One entry per iteration, and the only place a result survives** -- the
simulation ran in a temp dir and is gone, and the tuning netlist holds only the
CURRENT values, never how they got there. It opens with the run header (budget N
and the target file in force), then one section per iteration, **diff-based**
because a design can have dozens of tunables and only what moved is worth
re-reading:

| Line | Carries |
|---|---|
| **Changed** | every parameter that moved, old -> new, and the fold if Step 2a fired |
| **Specs** | each key's measured value and its delta from the previous iteration |
| **Target** | met / not met per key, the number judged against, and which target file |
| **Op-point** | the per-device numbers your reasoning used |
| **Reasoning** | *why* you made this change, and what you expected it to do |
| **Next** | what to look at if this did not land |

"Reasoning" and "Next" are what make a re-read useful, and the first to get
dropped under budget pressure. **The converged entry is the exception -- log the
FULL parameter set, the achieved performance against BOTH target files, and the
op point**: it is the only record of what produced the passing numbers.

## Step 3: final netlist

**A promotion, not a render.** The tuning netlist already holds the converged
`W`/`L`, so this step copies it to the hand-off name and adds what a hand-off
needs: rounding, a provenance header, the width breakdown.

```
python script/finalize_netlist.py <design_dir>/sizing/<design_name>_tuning.sp
    --groups <design_dir>/sizing/structure_groups.json
    -o <design_dir>/sizing/<design_name>_final.sp
```

Downstream resolves the hand-off by that exact filename, so `<d>_final.sp` is a
contract. **Check the converged `book_keeper.md` entry against the tuning
netlist first** -- a later exploratory iteration may have moved it past the
values that converged. It refuses to promote a netlist whose groups have
desynchronised (one half of a pair edited without the other). Log the render in
`operations.log`, `by sizing`.

- **Pass no `--nf`** -- every device keeps the finger count the netlist carries.
  `--nf N` overwrites `nf=` on every MOS line and belongs to a caller that has
  measured one.
- **Rounding is last**: every tunable to 2 decimal places (`ROUND_NDIGITS`),
  except a mirror branch's `_M`, which rounds to the nearest **integer** floored
  at 1 -- a count of parallel devices.
- **On a hierarchical design, promote the whole set and rename only the top**
  (see "Working folder" -> Naming). The hand-off is `<d>_final.sp` *plus* the
  sub-circuit files it includes; shipping the top file alone does not close, as
  its `.include` lines have nothing to resolve against. Log every promoted file
  in `operations.log`, and name the full set in `sizing_report.md` so layout
  knows what it received.
- **Width breakdown for layout**: one header row per MOS instance --
  per-multiplier, per-finger and true total width. `w` is the width of ONE of
  `m` copies and is Nf-invariant (`nf` splits, never adds), so `w x nf` would
  overstate the device; the block keeps anyone from redoing that arithmetic wrong.

**Plot the result when `spec_analysis.md` calls for one** -- a response-shaped
spec (gain and bandwidth, a stability margin, a filter corner) is easier to
check as a curve with its targets drawn on than as numbers:

```
python script/generate_plot_script.py <design_dir>
    --netlist <design_dir>/netlist/<design_name>.sp --analysis <the deck's analysis>
python <design_dir>/sizing/plot_<design_name>.py <converged raw> [--compare <seed raw>]
```

Same generator/engine split as Step 1a's runner, and spec-driven rather than
circuit-specific: every key the run can locate is marked with its target beside
the achieved value, and a key nothing produces is left unmarked rather than
guessed at. Reference the PNG from `sizing_report.md`.

## Sizing report

**Always save it** -- on convergence, budget exhaustion or an explicit stop --
to `<design_dir>/sizing/sizing_report.md`. It covers the tuning loop only and
every number in it is ideal-schematic: **no parasitic figure belongs here** (this
skill has annotated and estimated nothing). One row per key:

| Column | Is |
|---|---|
| **Metric** | the spec key, with its unit |
| **Real target** | the bar from `target_spec.json` -- the requirement |
| **Harder target** | the bar from `harder_target_spec.json` -- the aim |
| **Achieved** | what the converged netlist measured, no parasitics |
| **Meets real?** | floor `sim >= target`, ceiling `sim <= target`, range `lower <= sim <= upper` |
| **Meets harder?** | the same test against the harder bar |
| **Margin** | room left over the REAL bar -- everything downstream spends from this |

**Both target columns are mandatory.** The caller decides whether to re-size
from this table, and cannot tell `MET, MARGIN SHORT` from `SHORTFALL` if only
one bar is printed. The margin column is what matters next: a key clearing its
real target by a hair is the one most likely to fail once real parasitics exist.

Also in prose: **the verdict in the three-way form** (`CONVERGED` / `MET, MARGIN
SHORT` / `SHORTFALL`) and, when it is not `CONVERGED`, which keys fell short of
which bar and by how much; which iteration converged (or a pointer to
`book_keeper.md`); the margin the harder target added; where `<d>_final.sp`'s
`nf` came from (sizing measures none); which budget stopped the run, how many
iterations remained, and the best iteration achieved; and the figure, if one was
produced.

**Say what a further run would need**: whether more iterations would close the
remaining gap, which parameters you would move next and why, roughly how many
iterations that would take -- or that you judge the target unreachable on this
topology, with the op-point evidence for it. The caller uses exactly this to
decide whether to re-invoke, so an absent judgement forces it to re-derive one
you already had.

## The hand-off

What leaves this skill: `<d>_final.sp`, `sizing_report.md`, `book_keeper.md`,
the plot if one was made, and `debug/iter_<n>/` if it was asked for.

**From here the netlist is frozen** -- once it reaches layout, only layout
geometry changes, never a device's `W`/`L`. Whether anything else runs between
this skill and that point is the caller's decision; nothing here assumes a next
stage or leaves a value for one to fill in.

## Scope and boundaries

**A netlist-and-simulator skill, nothing more** -- it starts from valid files
and an understood circuit (the preconditions in "When to use this") and ends at
the hand-off netlist plus its report. No placement, routing, DRC, LVS or PEX.

- **No feasibility gate here -- the question was already asked.**
  `spec_analysis.md` carries a plausibility column per key and flags
  over-constrained specs. Read it before reading budget exhaustion as a sizing
  failure: a key it flagged was predicted to fail, so the target is what to
  revisit; a key it did not flag needs its gap reported honestly.
- **Measures only what a registered extractor produces**, through the keyed
  registry in `../../reference/compute_fidelity.py` (`EXTRACTORS`; `--analysis`
  selects the entry, `ac` by default, not the only one it reads). A key nothing
  produces is `unmeasured`, never scored as a miss. A key no extractor covers
  needs one **registered** -- a registry entry, not a fork; a transient key
  (oscillation frequency, settling time, jitter) is untrackable until then and
  should already have been raised in `spec_analysis.md`. A raw file the
  extractor cannot read is reported, not fatal: the op-point data still returns.
- **No automated optimizer** -- parameter moves are your reasoning over the
  simulated specs and op-point data, never a gradient or genetic search.
- **No finger count, no parasitics** -- `nf` carries through untouched and every
  number is ideal-schematic; the harder target is this skill's whole answer to
  parasitics it cannot see.
- **Does not size resistor/cap values directly** -- these netlists express R/C
  sizing as `W`/`L` on the device line, so tuning those is the lever.
- **Hierarchy is in scope; the current scripts are not** -- see "Hierarchical
  designs" for exactly where that breaks. Report a nested design as unsupported,
  never run it flat and report it as sized.

**Shared vs per-design.** The **engine** -- rendering, running ngspice,
op-point parsing, target scoring -- lives in `script/` and is shared, so a fix
reaches every design at once. The **facts and measurements** -- supply nets,
analysis, raw filenames, `measure()` -- live in
`<design_dir>/sizing/run_sizing_<d>.py`, generated per design by Step 1a. A
design that cannot run without editing a file in `script/` means a missing flag
or a missing extractor: report it as one, never make a local copy of the engine.

## Files in this skill

Each file's behaviour is defined by the step that owns it; this is an index.

| File | What it is | Defined in |
|---|---|---|
| `set_tunable_params.md` | the structure registry (which devices share a variable); a new topology is a new row there | its own file |
| `script/setup_sizing.py` | the tuning `.sp` + `structure_groups.json`, with mirror-family and matched-group detection | Step 1 |
| `script/generate_sizing_runner.py` | writes this design's own runner | Step 1a |
| `script/compute_harder_target.py` | the harder target spec | "The two bars" |
| `script/run_sizing_iteration.py` | the per-iteration measurement engine the runner imports | Step 2 |
| `script/fold_wide_devices.py` | `tuning` = the fold against the PDK max; `netlist` = the pre-sizing fold to half the bin | Step 2a |
| `script/finalize_netlist.py` | promotes the tuning netlist to the hand-off | Step 3 |
| `script/generate_plot_script.py` / `script/plot_results.py` | per-design plot script + the shared rendering engine it imports | Step 3 |
| `script/edit_netlist.py` | reads/writes tunable values through the groups; `check_groups()` catches a desynced pair | "Working folder" |
| `script/generate_op_probe.py` | the op-point probe lines spliced into the deck | Step 2 |
| `script/netlist_devices.py` | the shared device-line parser every script here imports | -- |
