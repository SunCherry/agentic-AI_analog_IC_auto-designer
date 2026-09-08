# Agentic AI Analog IC Auto-Designer — Project Instructions

This project is an **agentic AI program for analog schematic and layout
design automation**. Given three mandatory user files — a **netlist**, a
**testbench**, and a **target spec** (`target_spec.json`) — it drives a
team of cooperating agents that size the circuit against the spec, then
build and verify a physical layout. The deliverables are a **well-tuned
netlist** (sized `W`/`L`, chosen `nf`) and a **DRC-clean, LVS-matched
`.gds`** whose post-layout simulation is measured against that netlist's
pre-layout result.

The flow is **specialized for the PDKs listed in
`.claude/reference/pdk_options.json`**. The PDK is a *project-wide setting*:
the `"selected"` key names the one active process, and every script, skill,
and agent reads process facts (model prefix, GDS layer numbers, tool paths,
glayout module) from there via `.claude/reference/pdk_config.py`. Never
hardcode `sky130`, `gf180`, or their layer numbers anywhere.

These instructions apply to any agent working in this project — an
orchestrating Claude Code session, or a spawned subagent taking on one of
the four roles below.

## Two stages

The work is one pipeline with two distinct halves:

1. **Upstream — sizing.** `schematic-agent` owns the netlist through its
   analog schematic stage: it files and gates the inputs, understands the
   circuit, confirms the testbench can measure every spec key, then sizes
   device `W`/`L` (via `schematic-sizing`) and picks finger count `nf` for MOS devices (via
   `device-shaper`) until every spec key is met at minimum power. It hands
   layout a **frozen netlist**.
2. **Downstream — automatic layout design.** With that frozen netlist and
   a starting layout, `layout-agent` builds (or extracts) the layout,
   `layout-fixer` drives it to DRC-clean and LVS-matched, and
   `verify-agent` runs parasitic extraction and post-layout simulation to
   confirm the result. **Once a netlist enters this stage it is never
   modified — only layout geometry.**

## The four agents

Each role is a native Claude Code subagent definition in
`.claude/agents/`, and each owns its own step. Spawn them via the Agent
tool's `subagent_type`, never by hand-pasting role text.

| Agent | Stage | Owns | Key skills it invokes |
|---|---|---|---|
| `schematic-agent` | upstream | input intake → validity gate → circuit read → spec analysis → testbench audit → `W`/`L` sizing → `nf` shaping → frozen-netlist hand-off | `design-sheets-intake`, `design-sheets-checker`, `circuit-decomposition`, `schematic-sizing`, `device-shaper` |
| `layout-agent` | downstream | a GDS + `physical_map.json` from a reference layout (`layout-extractor`) or an empty canvas (`placer` → `router`); stops at the DRC/LVS gate with a hand-off payload, then shortens the routing once the gate is cleared | `layout-extractor`, `placer`, `router`, `route-optimizer` |
| `layout-fixer` | downstream | **both** physical-verification gates — DRC and LVS — iterating until the GDS is DRC-clean *and* LVS-matched | Magic DRC + netgen LVS flows (`.claude/skills/router/script/run_drc.py`) |
| `verify-agent` | downstream | Magic PEX + pre-/post-layout AC simulation + the fidelity metric (terminal step; stops at the numbers) | `.claude/reference/compute_fidelity.py` |

`schematic-agent` is the front door — nothing runs before it, and a design
missing any of the three mandatory files stops at intake (the netlist,
testbench, and `target_spec.json` are always user-authored; no agent
authors them).

## Skill stack

Skills are procedures the agents invoke with the `Skill` tool. Each lives
under `.claude/skills/` with its own `SKILL.md`; an agent reads that for
its gates but never hand-executes it.

| Skill | What it does |
|---|---|
| `design-sheets-intake` | front-door intake: interviews for the three files + PDK, resolves the netlist's `.include` closure, lays down `<design_root>/<project_name>/`, and collects the run constraints (loop budgets + canvas aspect) into `design_constraints.json` |
| `design-sheets-checker` | validation gate: EDA tool health, netlist ERC, structural checks, pre-layout sim, measurability, spec sanity, optional DRC/LVS pre-pass |
| `circuit-decomposition` | scans the netlist into `.subckt` hierarchy and matched patterns, then DERIVES the tie groups, the tunable-variable registry and a `<top>_tunable.sp.j2` netlist template from `pattern-table.md`'s own rules → `circuit_decomposition.yaml` + the `.j2` |
| `schematic-sizing` | tunes device `W`/`L` (and a ratio carrier's `m`) against `target_spec.json` (pre-layout) by rendering that `.j2` — it decides no groupings of its own |
| `device-shaper` | decides how each device is DRAWN -- unit width, finger count `nf`, copy count `m` -- from four geometric principles (one unit device per tie group; maximal unit; square device; square array), preserving total width `w * m`. Needs no parasitic table, so it runs on any PDK |
| `placer` | empty-canvas layout: recognizes subcircuits, generates glayout primitives, simulated-annealing placement |
| `router` | grid-based A* router with negotiated congestion over a placement |
| `layout-extractor` | recovers a `physical_map.json` (placement + traced routing) from a reference GDS |
| `route-optimizer` | rebuilds the wiring of a DRC-clean layout as a minimum-length tree over its frozen pin landings, re-runs DRC and reverts any net a new violation lands on |

## Gate chain & workflow

The pipeline is strictly ordered, and each agent owns exactly one step:

```
schematic-agent  →  (sized, frozen netlist)
   │
   ▼
layout-agent     →  GDS + physical_map.json  (reference extraction OR placer+router)
   │
   ▼
layout-fixer     →  DRC-clean  AND  LVS-matched
   │
   ▼
route-optimizer  →  shorter, straighter routing  (gates itself on DRC)
   │
   ▼
layout-fixer     →  RE-GATE: geometry changed, both gates open again
   │
   ▼
verify-agent     →  PEX + pre/post AC sims + fidelity metric  (terminal)
```

**The orchestrator spawns every agent in that chain, including both
`layout-fixer` passes.** No agent in it can spawn another: a subagent cannot
spawn subagents, and only the orchestrating session holds the Agent tool. So
`layout-agent` stops at the DRC/LVS gate and returns a hand-off payload (GDS
path + top cell name + the LVS golden to compare against); the orchestrator
spawns `layout-fixer` with it and relays the report back to `layout-agent`.
Same again after `route-optimizer`. An agent that stops at a gate it cannot
cross itself is behaving correctly — do not read it as a failure, and never
ask it to "just spawn" the next one.

The gates are hard: `verify-agent` refuses to measure a layout
`layout-fixer` hasn't passed. If a **reference layout** exists,
`layout-agent` skips `placer`/`router` and extracts the map from it
instead — but the reference is not assumed clean, and still goes through
`layout-fixer`'s gates.

## Key rules

- **Tie groups and tunable parameters are DERIVED, never hand-authored.**
  `circuit-decomposition` emits them from `pattern-table.md`'s own "Tie group"
  rules, together with `<top>_tunable.sp.j2` -- the netlist with every tunable
  as `{{ VAR }}`. Devices that must share a parameter share the placeholder, so
  a tie group is rewritten from one value on every render and cannot come
  apart. `schematic-sizing` renders that template and runs no detector of its
  own; `structure_groups.json` is gone, because two sources for one fact
  drifted and put three independent widths on one current mirror.
  **Never give a mirror's legs independent `w`** -- one shared unit `w`/`l`
  with a per-leg integer `m` IS the mirror rule, and is how it gets laid out.
- **`m` is a sizing lever only where a pattern names it `ratio_carrier`**
  (`current_mirror`, `cascode_current_mirror`, `resistor_ladder`,
  `capacitor_bank`). Elsewhere it is templated but frozen for sizing, writable
  only by the PDK-bin fold. **`nf` is never a sizing lever** -- `device-shaper`
  owns it.
- **Never edit the target `.sp` netlist once it has entered the layout
  stage.** Only layout geometry (device placement, routing paths) may
  change. Before that point, while `schematic-agent` is sizing `W`/`L`
  against `target_spec.json`, the netlist is exactly what's being edited;
  that stage ends, and the freeze begins, once it is handed to layout.
- **DRC and LVS must *both* be clean** for a layout before its
  PEX/simulation numbers are trusted or reported. `route-optimizer` runs
  *after* that verdict and invalidates it — its output goes back through
  `layout-fixer` before `verify-agent` sees it, and if the re-gate fails the
  pre-shortening layout ships.
- **All parasitic/metric calculations MUST be done in Python** (reuse
  `.claude/reference/compute_fidelity.py`) — never estimate numbers by
  inspection.
- **Always print the full pre-/post-layout comparison table**
  (`.claude/reference/metrics.md` format) when `verify-agent` measures a
  layout.
- **DRC/LVS fix-retries** have their own small sub-budget inside
  `layout-fixer` (`layout_fix_iterations`, default 5 tries each); they do not
  otherwise change the flow.
- **Run constraints are asked once, at intake, and read from a file.**
  `design-sheets-intake` writes `<design_dir>/design_constraints.json`
  (sizing iterations, layout fix rounds, placer anneal iters, max canvas
  aspect ratio); every consumer reads it through
  `.claude/reference/design_constraints.py` --
  `python .claude/reference/design_constraints.py <design_dir> --key <key>`.
  Never re-ask a constraint that file answers, and never hardcode one: a new
  constraint is a new row in that script's `CONSTRAINT_KEYS` plus a row in
  the intake skill's Step 7 table. A value named in an agent's spawning
  prompt overrides the file, and is declared when it does.
- **Artifacts are written under `runs/<design>/`**; never overwrite a
  previous run's files. `<design>` is the design directory's name, not the
  netlist's top `.subckt` — the two routinely differ.

Orchestration rules (spawn order, gates, budgets) are the "Key rules"
above plus the individual role files; there is no separate master
orchestration file in this repo.

## How to use the stack

1. Read the role file for the step you're on, plus the "Key rules" above
   for the constraints (gates, budgets, artifact layout).
2. Spawn each agent via the Agent tool's `subagent_type` (the
   `.claude/agents/<role>.md` frontmatter and body load automatically).
3. Follow `.claude/reference/metrics.md` verbatim for the fidelity metric
   formula, default tolerances, and report-table format — do not invent
   your own.
4. Follow each role file as a procedure to execute, not as reference
   material — run the steps in order and respect the gates.

## Environment

- **Tool assignment** (`eda_tool_config.json`): DRC = Magic, LVS = Netgen,
  PEX = Magic, Simulator = ngspice.
- **Toolchain paths and known quirks**: read
  `.claude/reference/environment.md` before running any
  magic/netgen/ngspice command (Magic DRC batch-mode gotchas, the netgen
  resistor R-vs-X syntax quirk, `PDK_ROOT` requirements, etc.).
- **Run constraints**: `<design_dir>/design_constraints.json` +
  `.claude/reference/design_constraints.py` loader; ask for a value only when
  the loader reports it `UNRESOLVED`.
- **PDK selection**: `.claude/reference/pdk_options.json` (`"selected"`
  key) + `.claude/reference/pdk_config.py` loader; never hardcode a
  process.
