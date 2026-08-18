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
| `layout-agent` | downstream | a GDS + `physical_map.json` from a reference layout (`layout-extractor`) or an empty canvas (`placer` → `router`); drives `layout-fixer` to a clean result | `layout-extractor`, `placer`, `router` |
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
| `design-sheets-intake` | front-door intake: interviews for the three files + PDK, resolves the netlist's `.include` closure, lays down `<design_root>/<project_name>/` |
| `design-sheets-checker` | validation gate: EDA tool health, netlist ERC, structural checks, pre-layout sim, measurability, spec sanity, optional DRC/LVS pre-pass |
| `circuit-decomposition` | scans the netlist into `.subckt` hierarchy, matched patterns, and `w`/`l` tie groups → `circuit_decomposition.yaml` |
| `schematic-sizing` | tunes device `W`/`L` against `target_spec.json` (pre-layout) |
| `device-shaper` | chooses each device's finger count `nf` under estimated parasitics |
| `placer` | empty-canvas layout: recognizes subcircuits, generates glayout primitives, simulated-annealing placement |
| `router` | grid-based A* router with negotiated congestion over a placement |
| `layout-extractor` | recovers a `physical_map.json` (placement + traced routing) from a reference GDS |

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
verify-agent     →  PEX + pre/post AC sims + fidelity metric  (terminal)
```

The gates are hard: `verify-agent` refuses to measure a layout
`layout-fixer` hasn't passed. If a **reference layout** exists,
`layout-agent` skips `placer`/`router` and extracts the map from it
instead — but the reference is not assumed clean, and still goes through
`layout-fixer`'s gates.

## Key rules

- **Never edit the target `.sp` netlist once it has entered the layout
  stage.** Only layout geometry (device placement, routing paths) may
  change. Before that point, while `schematic-agent` is sizing `W`/`L`
  against `target_spec.json`, the netlist is exactly what's being edited;
  that stage ends, and the freeze begins, once it is handed to layout.
- **DRC and LVS must *both* be clean** for a layout before its
  PEX/simulation numbers are trusted or reported.
- **All parasitic/metric calculations MUST be done in Python** (reuse
  `.claude/reference/compute_fidelity.py`) — never estimate numbers by
  inspection.
- **Always print the full pre-/post-layout comparison table**
  (`.claude/reference/metrics.md` format) when `verify-agent` measures a
  layout.
- **DRC/LVS fix-retries** have their own small sub-budget (5 tries each)
  inside `layout-fixer`; they do not otherwise change the flow.
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
- **PDK selection**: `.claude/reference/pdk_options.json` (`"selected"`
  key) + `.claude/reference/pdk_config.py` loader; never hardcode a
  process.
