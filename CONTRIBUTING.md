# Contributing

Thanks for your interest in contributing! This project is a multi-agent
Claude Code workflow for analog IC design automation.

## Getting started

1. Install the prerequisites — see the **Installation** section of the
   [README](README.md).
2. Install the EDA toolchain (Magic, Netgen, ngspice) and a PDK (sky130A),
   and point `.claude/reference/pdk_options.json` at it.
3. Read [CLAUDE.md](CLAUDE.md) for the ground rules every agent follows.

## How the project is organized

- `.claude/agents/` — one Claude Code subagent per role
  (`schematic-agent`, `layout-agent`, `layout-fixer`, `verify-agent`).
- `.claude/skills/` — procedures the agents invoke (intake, checker,
  circuit-decomposition, schematic-sizing, device-shaper, placer, router,
  layout-extractor).
- `.claude/reference/` — PDK config (`pdk_options.json` / `pdk_config.py`),
  toolchain environment notes, and the fidelity metric.

## Making a change

- Run everything from the project root.
- Never hardcode a PDK or its layer numbers — read process facts through
  `.claude/reference/pdk_config.py`.
- Follow the metric and report formats in `.claude/reference/metrics.md`.
- Keep each agent's role file self-contained: a role describes only its own
  step and respects the gate chain (DRC + LVS before PEX).

## Reporting issues

Open an issue with:

- the design (or a minimal repro) and the three input files,
- the PDK and EDA tool versions,
- the agent/skill that failed and the relevant logs.

## License

By contributing, you agree your work is licensed under the MIT License (see
[LICENSE](LICENSE)).
