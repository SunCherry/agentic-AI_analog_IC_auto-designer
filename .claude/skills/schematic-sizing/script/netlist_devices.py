#!/usr/bin/env python3
"""Shared netlist device-line parser for this skill's scripts
(setup_sizing.py, generate_op_probe.py) -- one parser, not two
slightly-different regexes drifting apart.

Scope (documented, not silently assumed): parses a flat, single-.subckt
golden netlist using this project's own conventions --

  - MOS: 3-4-terminal `X<name>`/`M<name>` line, model name containing
    "fet" (matches sky130A's `sky130_fd_pr__{n,p}fet_01v8` and
    gf180mcuD's `nfet_03v3`/`pfet_03v3` alike).
  - Resistor: 2-terminal `R<name>`/`X<name>` line, model name containing
    "res".
  - Cap: 2-terminal `C<name>`/`X<name>` line, model name containing "cap".
  - BJT: 3-4-terminal `Q<name>`/`X<name>` line, model name containing
    "npn"/"pnp".
  - Ideal source: 2-terminal `V<name>`/`I<name>`/`E<name>`/`G<name>`/
    `H<name>`/`F<name>` line, no model token -- e.g. `ibias`, `iprobe`,
    the differential AC stimulus. Never a tuning target (see
    `../SKILL.md`'s "Identify tunable parameters").
  - Anything else that's an `X<name>` subcircuit call is returned as
    kind="other" (nets = every token but the last, taken as the subckt
    name) -- not tuned, not ERC-checked beyond basic net connectivity,
    since this parser has no way to know an arbitrary subckt's terminal
    semantics.
"""
import re

_MOS_RE = re.compile(
    r'^(?P<name>[MX]\S+)\s+'
    r'(?P<nets>(?:\S+\s+){3,4})'
    r'(?P<model>\S*fet\S*)\s+'
    r'(?P<params>.*)$',
    re.IGNORECASE,
)
_RES_RE = re.compile(
    r'^(?P<name>[RX]\S+)\s+'
    r'(?P<nets>(?:\S+\s+){2})'
    r'(?P<model>\S*res\S*)\s+'
    r'(?P<params>.*)$',
    re.IGNORECASE,
)
_CAP_RE = re.compile(
    r'^(?P<name>[CX]\S+)\s+'
    r'(?P<nets>(?:\S+\s+){2})'
    r'(?P<model>\S*cap\S*)\s+'
    r'(?P<params>.*)$',
    re.IGNORECASE,
)
_BJT_RE = re.compile(
    r'^(?P<name>[QX]\S+)\s+'
    r'(?P<nets>(?:\S+\s+){3,4})'
    r'(?P<model>\S*(?:npn|pnp)\S*)\s*'
    r'(?P<params>.*)$',
    re.IGNORECASE,
)
_SOURCE_RE = re.compile(
    r'^(?P<name>[VIEGHF]\S+)\s+(?P<n1>\S+)\s+(?P<n2>\S+)\s+(?P<rest>.*)$',
    re.IGNORECASE,
)
_SUBCKT_CALL_RE = re.compile(r'^(?P<name>X\S+)\s+(?P<rest>.+)$', re.IGNORECASE)
_PARAM_RE = re.compile(r'(\w+)\s*=\s*([^\s]+)')

_DIRECTIVE_PREFIXES = ('.subckt', '.ends', '.model', '.param', '.lib',
                        '.include', '.global', '.end', '*')


def _is_directive_or_comment(stripped):
    if not stripped:
        return True
    low = stripped.lower()
    return any(low.startswith(p) for p in _DIRECTIVE_PREFIXES)


def _mk(m, kind, lineno, stripped):
    return {
        "name": m.group('name'),
        "kind": kind,
        "nets": m.group('nets').split(),
        "model": m.group('model'),
        "params": dict(_PARAM_RE.findall(m.group('params'))),
        "lineno": lineno,
        "raw_line": stripped,
    }


def parse_devices(text):
    """Return a list of device dicts: {name, kind, nets (list), model,
    params (dict), lineno, raw_line}. kind in {"mos","res","cap","bjt",
    "source","other"}. Skips directives/comments/blank lines."""
    devices = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if _is_directive_or_comment(stripped):
            continue

        m = _MOS_RE.match(stripped)
        if m:
            devices.append(_mk(m, "mos", lineno, stripped))
            continue
        m = _RES_RE.match(stripped)
        if m:
            devices.append(_mk(m, "res", lineno, stripped))
            continue
        m = _CAP_RE.match(stripped)
        if m:
            devices.append(_mk(m, "cap", lineno, stripped))
            continue
        m = _BJT_RE.match(stripped)
        if m:
            devices.append(_mk(m, "bjt", lineno, stripped))
            continue
        m = _SOURCE_RE.match(stripped)
        if m:
            devices.append({
                "name": m.group('name'), "kind": "source",
                "nets": [m.group('n1'), m.group('n2')],
                "model": None, "params": {}, "lineno": lineno,
                "raw_line": stripped,
            })
            continue
        m = _SUBCKT_CALL_RE.match(stripped)
        if m:
            toks = m.group('rest').split()
            if len(toks) >= 2:
                devices.append({
                    "name": m.group('name'), "kind": "other",
                    "nets": toks[:-1], "model": toks[-1], "params": {},
                    "lineno": lineno, "raw_line": stripped,
                })
    return devices


def subckt_header(text):
    """Return (subckt_name, port_list) for the netlist's single .subckt,
    or (None, [])."""
    m = re.search(r'^\.subckt\s+(\S+)\s+(.+)$', text, re.MULTILINE | re.IGNORECASE)
    if not m:
        return None, []
    return m.group(1), m.group(2).split()
