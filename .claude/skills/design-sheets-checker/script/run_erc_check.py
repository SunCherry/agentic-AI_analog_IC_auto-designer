#!/usr/bin/env python3
"""Electrical Rule Check (ERC) on a flat SPICE netlist or testbench --
see ../SKILL.md for how this fits into the project's workflow.

This parses the WHOLE file passed to it (netlist OR testbench, `.subckt`
bodies and top-level statements alike), follows `.include` lines to
cross-reference a DUT's declared port count against how a testbench
actually instantiates it, and adds several checks a commercial ERC tool
(Cadence Pegasus/PVS, Mentor Calibre xRC, etc.) would run that a pure
netlist-connectivity check does not: conflicting drivers, duplicate
instance names, subcircuit port-count mismatches, missing ground
reference, and testbench-specific sanity (an analysis statement present,
a `.control` block that actually `run`s).

**Where this runs in the flow:** `../SKILL.md`'s **Step 2** -- 2a
(`--params-only`, device parameter VALUES) then 2b (the full
connectivity pass, which re-includes 2a). That is the authoritative
run, and it gates every later step. `../../../agents/schematic-agent.md`
also calls this script at its own #1, on the corrected files Step 2
hands back and on any file it is later given that is new or changed;
that is a confirmation pass, not this one. (It is NOT that agent's first
responsibility -- collecting the design's inputs is, at its #0.) See
`../SKILL.md`'s "Relationship to other skills".

**Not a replacement for a real schematic-capture/layout ERC tool** --
still purely text/connectivity-level, no device-physics rule deck
(spacing, well-proximity, latch-up, ESD). See "Backend extension point"
below for how a future commercial ERC tool could plug into this same
report/summary machinery without changing how callers use this script.

Three-tier severity (documented, not silently assumed):
  - HARD ERRORS (exit 1): unambiguous bugs -- zero-length shorts,
    duplicate instance names, subckt port-count mismatches, conflicting
    voltage-source drivers on the same non-ground net.
  - WARNINGS (exit 2): judgment-call flags, never silently proceed past
    without review -- floating nets, unused ports, no ground reference,
    no model/library source found, a testbench missing an analysis
    statement or a `.control` block with no `run`.
  - INFO (no exit-code impact): possible dummy/fill devices (terminal
    degeneracy), unresolved `.include` paths.

Every finding carries a concrete **fix suggestion** string, not just a
description of what's wrong -- see "Output" below.

Usage:
  python run_erc_check.py <netlist_or_testbench.sp> [--json out.json]
      [--backend builtin]
"""
import argparse
import json
import math
import os
import re
import sys

COMMENT_LINE_RE = re.compile(r"^\s*\*")
INLINE_COMMENT_RE = re.compile(r"\s*;.*$")

# device-line leading-letter -> (kind, min terminal count) -- SPICE's own
# element-prefix convention (the naming-convention skill that held the full
# derivation is not present in this repo); "min terminal count" is how many of the tokens
# after the instance name are treated as this device's OWN nets (a 4th
# MOS/BJT bulk/substrate terminal, and E/G/H/F's 2 extra controlling
# nets, are parsed but not required -- ERC here cares about the device's
# own current-carrying terminals, not its control inputs).
SOURCE_PREFIXES = {"V", "I", "E", "G", "H", "F"}
PASSIVE_PREFIXES = {"R": 2, "C": 2, "L": 2}
RAW_DEVICE_PREFIXES = {"M": 4, "Q": 4, "D": 2}
GROUND_ALIASES = {"0", "GND", "VSS!", "AGND", "DGND"}
# compared against an already-uppercased line (see the parsing loop below),
# so these must be uppercase too -- a lowercase/uppercase mismatch here
# silently made every one of these directives invisible to the parser
# until caught by testing against a real testbench (see ../SKILL.md)
ANALYSIS_DIRECTIVES = {".OP", ".AC", ".DC", ".TRAN", ".NOISE", ".PZ", ".SENS"}
MODEL_SOURCE_DIRECTIVES = {".LIB", ".INCLUDE", ".MODEL", ".INC"}


def _guideline_pdk(default="sky130A", allowed=None):
    """Active PDK name from `.claude/reference/pdk_options.json`.

    The project selects one PDK there; this is how a script picks that up
    instead of hardcoding a process. Falls back to `default` if the guideline
    cannot be read, or names a PDK this script has no table for, so a
    standalone run still works.
    """
    import os as _os, sys as _sys
    try:
        _ref = _os.path.abspath(_os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)),
            *([_os.pardir] * 3), "reference"))
        if _ref not in _sys.path:
            _sys.path.insert(0, _ref)
        from pdk_config import pdk as _pdk
        name = _pdk().name
    except Exception:
        return default
    return name if (allowed is None or name in allowed) else default


def _strip_comment(line):
    if COMMENT_LINE_RE.match(line):
        return ""
    return INLINE_COMMENT_RE.sub("", line)


def _join_continuations(lines):
    """SPICE `+`-prefixed continuation lines get folded onto the
    previous logical line before any further parsing."""
    out = []
    for raw in lines:
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("+") and out:
            out[-1] = out[-1] + " " + line.lstrip()[1:].strip()
        else:
            out.append(line)
    return out


def _tokenize(line):
    # ngspice tolerates optional parens/commas around a subckt call's net
    # list (`xtwo_stage_amp (Vip Vin VDD VSS vout) two_stage_amp`) -- strip
    # them before splitting, or e.g. "(Vip"/"vout)" get parsed as distinct,
    # wrong net names from the clean "Vip"/"vout" used elsewhere in the
    # same file, producing bogus floating-net findings.
    return line.replace("(", " ").replace(")", " ").replace(",", " ").split()


def _model_token(toks, index):
    """The model/subckt NAME a primitive device line references, or None
    when it carries a bare value instead (`C1 vout 0 10p`, `R1 a b 21k`)
    or a braced expression (`CL vout 0 {cload}`). Only the first token
    after the device's terminals is considered -- that is where SPICE
    puts the model name."""
    if index >= len(toks):
        return None
    tok = toks[index]
    if "=" in tok or tok.startswith("{") or tok.lower() == "params:":
        return None
    if _parse_spice_number(tok) is not None:
        return None
    return tok


def parse_spice_text(text, base_dir=None, _seen_includes=None, lineno_offset=0):
    """Parses a flat OR `.subckt`-nested SPICE file into scopes:
    `{"top": {"devices": [...], "statements": [...]}, "subckts":
    {name: {"ports": [...], "devices": [...]}}}`. Follows `.include`
    (resolved relative to `base_dir`, the including file's own directory)
    so a testbench's `X0 ... two_stage_rz` can be checked against
    `two_stage_rz`'s own `.subckt` port list even though that definition
    lives in a separate file -- `unresolved_includes` lists any that
    couldn't be read (kept as a soft finding, never a crash).

    `_seen_includes` guards against an include cycle; not a concern for
    this project's own netlists but cheap to make safe regardless."""
    _seen_includes = _seen_includes if _seen_includes is not None else set()
    lines = _join_continuations(text.splitlines())

    top = {"devices": [], "model_directives": [], "analysis": [], "control_run": False,
           "has_control": False}
    subckts = {}
    unresolved_includes = []
    cur_subckt = None  # (name, ports, devices) while inside .subckt/.ends
    in_control = False  # inside .control/.endc -- ngspice interactive
    # commands live here (run/set/write/quit/plot/...), NOT SPICE device
    # lines -- without this, a bare "quit" line's leading "Q" gets
    # misparsed as a BJT instance (see ../SKILL.md's worked example)

    def target_scope():
        return subckts[cur_subckt]["devices"] if cur_subckt else top["devices"]

    for i, line in enumerate(lines):
        lineno = i + 1 + lineno_offset
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()

        if upper.startswith(".CONTROL"):
            top["has_control"] = True
            in_control = True
            continue
        if upper.startswith(".ENDC"):
            in_control = False
            continue
        if in_control:
            if upper == "RUN" or upper.startswith("RUN "):
                top["control_run"] = True
            continue  # every other .control-block line is an ngspice
            # interactive command, not a SPICE statement -- not ERC-relevant

        if upper.startswith(".SUBCKT"):
            toks = _tokenize(stripped)
            name = toks[1]
            ports = toks[2:]
            cur_subckt = name
            subckts[name] = {"ports": ports, "devices": [], "lineno": lineno}
            continue
        if upper.startswith(".ENDS"):
            cur_subckt = None
            continue
        if any(upper.startswith(d) for d in ANALYSIS_DIRECTIVES):
            top["analysis"].append(stripped)
            continue
        if upper.startswith(".INCLUDE") or upper.startswith(".INC "):
            top["model_directives"].append(stripped)
            m = re.search(r'["\']([^"\']+)["\']|(\S+)$', stripped)
            inc_path = (m.group(1) or m.group(2)) if m else None
            if inc_path and base_dir:
                resolved = os.path.join(base_dir, inc_path)
                if os.path.isfile(resolved) and resolved not in _seen_includes:
                    _seen_includes.add(resolved)
                    inc_text = open(resolved).read()
                    inc_result = parse_spice_text(
                        inc_text, base_dir=os.path.dirname(resolved),
                        _seen_includes=_seen_includes)
                    subckts.update(inc_result["subckts"])
                    unresolved_includes.extend(inc_result["unresolved_includes"])
                elif resolved not in _seen_includes:
                    unresolved_includes.append(inc_path)
            elif inc_path:
                unresolved_includes.append(inc_path)
            continue
        if any(upper.startswith(d) for d in MODEL_SOURCE_DIRECTIVES):
            top["model_directives"].append(stripped)
            continue
        if stripped.startswith("."):
            continue  # other directives (.param, .global, .end, ...) -- not ERC-relevant

        prefix = stripped[0].upper()
        toks = _tokenize(stripped)
        name = toks[0]

        if prefix == "X":
            # Xname net1 net2 ... subckt_name [params...] -- last
            # non-"key=value" token before any "params:"/"key=value"
            # tail is the referenced subckt's name.
            rest = toks[1:]
            tail_start = len(rest)
            for j, t in enumerate(rest):
                if "=" in t or t.lower() == "params:":
                    tail_start = j
                    break
            call_toks = rest[:tail_start]
            if len(call_toks) < 2:
                continue  # malformed X-line -- not enough tokens to have nets + a subckt name
            nets, subckt_name = call_toks[:-1], call_toks[-1]
            dev = {"name": name, "kind": "subckt_call", "nets": nets,
                   "subckt_name": subckt_name, "lineno": lineno, "raw_line": stripped}
            target_scope().append(dev)
        elif prefix in SOURCE_PREFIXES:
            # V/I: <name> out+ out- [dc/ac/tran spec...] -- 2 nets.
            # E/G (VCVS/VCCS): <name> out+ out- ctrl+ ctrl- gain -- 4 nets,
            # all real connectivity (this project's own testbenches use E
            # sources exactly this way for a differential stimulus --
            # missing the ctrl+/ctrl- nets here previously produced bogus
            # floating-net findings on nets only ever touched via an E
            # source's control terminals, see ../SKILL.md's worked example).
            # H/F (CCVS/CCCS): <name> out+ out- <Vcontrol_name> gain -- the
            # 3rd token is a CONTROLLING SOURCE'S NAME, not a net, so only
            # 2 real nets.
            n_terminals = 4 if prefix in ("E", "G") else 2
            nets = toks[1:1 + n_terminals]
            dev = {"name": name, "kind": "source", "source_type": prefix,
                   "nets": nets, "lineno": lineno, "raw_line": stripped}
            target_scope().append(dev)
        elif prefix in PASSIVE_PREFIXES:
            n = PASSIVE_PREFIXES[prefix]
            nets = toks[1:1 + n]
            dev = {"name": name, "kind": "passive", "nets": nets,
                   "model_name": _model_token(toks, 1 + n),
                   "lineno": lineno, "raw_line": stripped}
            target_scope().append(dev)
        elif prefix in RAW_DEVICE_PREFIXES:
            n = RAW_DEVICE_PREFIXES[prefix]
            nets = toks[1:1 + n]
            dev = {"name": name, "kind": "raw_device", "device_letter": prefix,
                   "nets": nets, "model_name": _model_token(toks, 1 + n),
                   "lineno": lineno, "raw_line": stripped}
            target_scope().append(dev)
        # anything else (unrecognized element prefix) is silently skipped --
        # don't hard-fail on syntax this parser hasn't anticipated.

    return {"top": top, "subckts": subckts, "unresolved_includes": unresolved_includes}


def check_shorts(devices):
    errors = []
    for d in devices:
        nets = d.get("nets", [])
        if len(nets) == 2 and nets[0] == nets[1]:
            errors.append({
                "check": "short", "severity": "error",
                "message": f"line {d['lineno']}: {d['name']} ({d['kind']}) has "
                           f"both terminals on net '{nets[0]}' -- zero-length short.",
                "fix": f"Check the netlist line for {d['name']} -- one of its "
                       f"two terminal tokens should almost certainly reference a "
                       f"DIFFERENT net than '{nets[0]}'; verify against the "
                       f"schematic/design intent and correct the typo.",
            })
    return errors


def check_duplicate_names(all_devices):
    """Instance names are scoped PER `.subckt` in SPICE. Two different
    blocks may each legitimately declare an `M1`/`XM1`, and after
    `../../circuit-decomposition/SKILL.md` runs, several sibling
    `.subckt`s with parallel internal naming is the NORMAL shape of a
    netlist here -- as is a testbench whose `.include` pulls in a whole
    library of them. Keying on the bare name flagged all of those as hard
    errors; the key is `(scope, name)`."""
    errors = []
    seen = {}
    for d in all_devices:
        key = (d.get("scope"), d["name"].upper())
        seen.setdefault(key, []).append(d)
    for (scope, _name), group in seen.items():
        if len(group) > 1:
            lines = ", ".join(f"line {d['lineno']}" for d in group)
            where = f"inside .subckt {scope}" if scope else "at the top level"
            errors.append({
                "check": "duplicate_name", "severity": "error",
                "message": f"instance name '{group[0]['name']}' declared "
                           f"{len(group)} times {where} ({lines}) -- SPICE "
                           f"resolves duplicate instance names within one "
                           f"scope ambiguously (silently overwrites or errors "
                           f"depending on simulator).",
                "fix": f"Rename all but one occurrence of '{group[0]['name']}' "
                       f"{where} to a unique instance name (e.g. append an "
                       f"index). Note the same name in a DIFFERENT .subckt is "
                       f"fine and is not reported.",
            })
    return errors


def check_subckt_port_counts(subckts):
    """For every X-instance in every scope, if its referenced subckt's
    definition was found (in this file or a resolved `.include`),
    compare net count to declared port count -- a real, common bug this
    project's own workflow is exposed to (a schematic-sizing tuning
    iteration changes a device's tunable count but not `nf`/port wiring, or a
    testbench written against an older DUT port list)."""
    errors = []
    for scope_name, scope in subckts.items():
        for d in scope["devices"]:
            if d["kind"] != "subckt_call":
                continue
            target = subckts.get(d["subckt_name"])
            if target is None:
                continue  # not resolvable (external PDK subckt, or unresolved .include) -- skip, not an error
            expected = len(target["ports"])
            actual = len(d["nets"])
            if expected != actual:
                errors.append({
                    "check": "port_count_mismatch", "severity": "error",
                    "message": f"line {d['lineno']}: {d['name']} instantiates "
                               f"'{d['subckt_name']}' with {actual} net(s) "
                               f"({', '.join(d['nets'])}), but '{d['subckt_name']}' "
                               f"is declared with {expected} port(s) "
                               f"({', '.join(target['ports'])}).",
                    "fix": f"Add/remove net(s) on {d['name']}'s instantiation line "
                           f"to match '{d['subckt_name']}''s declared port list "
                           f"exactly, or fix the `.subckt {d['subckt_name']}` "
                           f"port list if the instantiation is actually correct.",
                })
    return errors


def check_conflicting_drivers(all_sources):
    """Two or more independent V-sources with a terminal on the same
    non-ground net -- a real short/conflict in hardware (E/G/H/F
    controlled sources are excluded: sharing a controlling/output net
    with another controlled source is a normal, intentional pattern in
    this project's own testbenches -- e.g. a differential pair's two E
    sources sharing a common-mode input net -- not a driver conflict)."""
    errors = []
    v_sources = [d for d in all_sources if d.get("source_type") == "V"]
    by_net = {}
    for d in v_sources:
        for net in d["nets"]:
            if net.upper() in GROUND_ALIASES:
                continue
            by_net.setdefault(net, []).append(d)
    for net, group in by_net.items():
        distinct_names = {d["name"] for d in group}
        if len(distinct_names) > 1:
            names = ", ".join(sorted(distinct_names))
            errors.append({
                "check": "conflicting_drivers", "severity": "error",
                "message": f"net '{net}' has {len(distinct_names)} independent "
                           f"voltage sources on it ({names}) -- conflicting "
                           f"drivers, a direct short unless the sources are "
                           f"guaranteed to always agree.",
                "fix": f"Confirm whether '{net}' is meant to have one driver -- "
                       f"if {names} are meant to be the SAME net, remove the "
                       f"redundant source; if they're meant to be different "
                       f"nets, rename one of the terminal references.",
            })
    return errors


def check_net_fanout(devices, ports):
    fanout = {}
    for d in devices:
        for net in d.get("nets", []):
            fanout[net] = fanout.get(net, 0) + 1
    port_set = set(ports)
    floating = sorted(net for net, n in fanout.items()
                       if n == 1 and net not in port_set and net.upper() not in GROUND_ALIASES)
    unused_ports = sorted(p for p in ports if fanout.get(p, 0) == 0)
    return floating, unused_ports


def check_ground_reference(top_devices):
    """Only meaningful for a runnable circuit -- a bare `.subckt`-only
    golden netlist (no top-level statements at all) legitimately has no
    ground reference of its own; its ports (including whatever it calls
    'VSS') are wired to a real ground by whoever instantiates it. Skipped
    entirely when `top_devices` is empty, so this only fires against a
    file that actually looks like a testbench."""
    if not top_devices:
        return []
    for d in top_devices:
        for net in d.get("nets", []):
            if net.upper() in GROUND_ALIASES:
                return []
    return [{
        "check": "no_ground_reference", "severity": "warning",
        "message": "no net named '0' (or a recognized ground alias: "
                   f"{', '.join(sorted(GROUND_ALIASES - {'0'}))}) appears as "
                   "any top-level device terminal in this file.",
        "fix": "SPICE requires a node named '0' as the voltage reference for "
               "the simulator to solve the circuit -- confirm the ground/"
               "reference net is actually wired, not just named something "
               "non-standard (e.g. 'GND' without also being net '0').",
    }]


def check_model_sources(top):
    """Same 'only meaningful for a runnable circuit' scoping as
    check_ground_reference() -- a bare golden netlist (`.subckt` body
    only) can legitimately reference PDK model names with no `.lib`/
    `.include` of its own, relying on whichever testbench includes it to
    supply the model library."""
    if not top["devices"]:
        return []
    if top["model_directives"]:
        return []
    return [{
        "check": "no_model_source", "severity": "warning",
        "message": "no `.lib`/`.include`/`.model` statement found anywhere "
                   "in this file.",
        "fix": "If this file uses foundry PDK devices (sky130A/gf180mcuD "
               "binned models), add the appropriate `.lib \"<path>\" <corner>` "
               "or `.include` line -- otherwise every X-instance referencing "
               "a PDK model will fail to simulate with an undefined-model "
               "error.",
    }]


def check_testbench_sanity(top):
    warnings = []
    if top["has_control"] and not top["analysis"]:
        warnings.append({
            "check": "no_analysis_statement", "severity": "warning",
            "message": "a `.control` block is present but no analysis "
                       "statement (.op/.ac/.dc/.tran/...) was found.",
            "fix": "Add the analysis this testbench is meant to run (e.g. "
                   "`.ac dec 10 1 10G` for an AC sweep) before the "
                   "`.control` block -- `run` inside `.control` has nothing "
                   "to execute without one.",
        })
    if top["has_control"] and not top["control_run"]:
        warnings.append({
            "check": "control_block_no_run", "severity": "warning",
            "message": "a `.control` block is present but contains no "
                       "`run` statement.",
            "fix": "Add `run` inside the `.control` block -- without it, "
                   "ngspice sets up the analysis but never actually "
                   "simulates it, and any `write`/`print` after it will "
                   "operate on empty/stale data.",
        })
    return warnings


def check_terminal_degeneracy(devices_by_scope):
    """Informational only, deliberately narrow: gate=drain / source=bulk
    diode-connect patterns are routine and NOT flagged; only drain=source
    (or 3+-way ties, this project's documented dummy/fill-device pattern)
    get a note."""
    notes = []
    for d in devices_by_scope:
        if d["kind"] != "raw_device" or d.get("device_letter") not in ("M", "Q"):
            continue
        nets = (d["nets"] + [None, None, None, None])[:4]
        near, far_gate, far, bulk = nets
        near_label, far_label = ("drain", "source") if d["device_letter"] == "M" else ("collector", "emitter")
        if near == far:
            notes.append({
                "check": "terminal_degeneracy", "severity": "info",
                "message": f"{d['name']}: {near_label}={far_label}={near} -- "
                           f"suspicious short, verify intentional.",
                "fix": f"Confirm {d['name']}'s {near_label} and {far_label} "
                       f"are meant to be the same net; if not, fix the "
                       f"terminal that's wrong.",
            })
        elif far_gate is not None and len({near, far_gate, far}) == 1:
            notes.append({
                "check": "terminal_degeneracy", "severity": "info",
                "message": f"{d['name']}: all three of {near_label}/gate-base/"
                           f"{far_label}={near} -- possible dummy/fill device, confirm.",
                "fix": f"If {d['name']} is an intentional dummy/fill device "
                       f"(LOD/well-proximity matching), no action needed -- "
                       f"otherwise check for a miswired terminal.",
            })
    return notes


# Smallest drawn dimension that can plausibly be a real geometry value once
# the netlist's own unit convention (MICRONS -- see check_device_parameters)
# is assumed. sky130's minimum drawn L is 0.15um and minimum W is 0.42um, so
# 0.1um sits below anything legal while staying far above the 1e-7..1e-5
# range an SI-metre value lands in. The gap between the two is wide enough
# that this never has to guess.
MIN_PLAUSIBLE_DRAWN_UM = 0.1

# Parameters whose value is a drawn geometry in microns. `r=`/`c=` are
# deliberately absent: those are resistance/capacitance, already unit-correct
# at any magnitude, and flagging a legitimate `r=21.0k` would be noise.
GEOMETRY_PARAMS = ("w", "l")

_PARAM_RE = re.compile(r'\b([a-zA-Z_]\w*)\s*=\s*([^\s=]+)')

# ngspice engineering-suffix scale factors. ORDER MATTERS: longest match
# wins, so "MEG" (1e6) must be tried before "M" (1e-3) and "MIL" before
# "M" -- getting that backwards turns `w=1.5meg` into 1.5e-3. Trailing
# alphabetic noise after the suffix is ignored the way ngspice ignores it
# ("1.8V", "6uF", "10kOhm").
_SPICE_SUFFIXES = (
    ("MEG", 1e6), ("MIL", 25.4e-6), ("T", 1e12), ("G", 1e9), ("K", 1e3),
    ("M", 1e-3), ("U", 1e-6), ("N", 1e-9), ("P", 1e-12), ("F", 1e-15),
)
_NUM_HEAD_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")


def _parse_spice_number(val):
    """Numeric value of a SPICE parameter, or None if it isn't a number at
    all.

    `w=6u`, `l=150n` and `r=21k` are ordinary SPICE and must NOT be
    mistaken for unsubstituted template placeholders -- an earlier version
    called bare `float()` here, so every suffixed geometry in a netlist
    came back as a hard `unresolved_parameter` error. Note this makes
    `w=6u` resolve to 6e-06, i.e. the SAME metre-scale value as
    `w=6.000e-06`, so it is correctly caught by the unit-scale check below
    rather than waved through: under this project's micron convention both
    spellings mean the same wrong thing."""
    stripped = val.strip()
    m = _NUM_HEAD_RE.match(stripped)
    if not m:
        return None
    num = float(m.group(0))
    tail = stripped[m.end():].upper()
    for suffix, scale in _SPICE_SUFFIXES:
        if tail.startswith(suffix):
            return num * scale
    return num


def check_device_parameters(devices):
    """Validate that device parameter VALUES are usable, not just that the
    connectivity around them is sane.

    This catches the class of defect where a netlist parses, looks entirely
    reasonable, and cannot be simulated or built at all:

    - **A `w`/`l` written in SI metres.** `w=6.000e-06` is ordinary SPICE
      for 6um, but the sky130 ngspice models bin on MICRONS, so the value
      is read as 6e-06um and matches no bin. Confirmed directly against
      the PDK, not assumed: `l=0.15 w=3.6` runs and draws 231uA, while the
      same device as `l=0.15e-6 w=3.6e-6` dies with "could not find a valid
      modelname". That is a hard stop before simulation, which is why this
      is an error rather than a warning.
    - **An unsubstituted template placeholder** (`nf=ZZZ`, `w=XXX`). A
      sizing template that was never filled in still parses as a netlist.
    - **A non-positive geometry** (`w=0`, `l=-1`).

    Scoped to VALUES only -- whether a legal-looking geometry is a *good*
    choice is a sizing question (`../../schematic-sizing/`), not an ERC one.
    Reads each device's own `raw_line`, so no parser change is needed and
    M-lines, X-subckt-calls and C/R passives are all covered uniformly.
    """
    findings = []
    for d in devices:
        raw = d.get("raw_line") or ""
        name = d.get("name", "?")
        for key, val in _PARAM_RE.findall(raw):
            k = key.lower()
            if k in ("params",):
                continue
            num = _parse_spice_number(val)
            if num is None:
                # A non-numeric value is only a defect for parameters that
                # must be numbers. A model NAME or a `dc=1.8v`-style suffixed
                # source spec is not this check's business.
                if k in GEOMETRY_PARAMS or k in ("nf", "m"):
                    findings.append({
                        "check": "unresolved_parameter", "severity": "error",
                        "message": f"{name}: {key}={val} is not a number -- "
                                   f"looks like an unsubstituted template "
                                   f"placeholder. The netlist parses but "
                                   f"cannot be simulated or built.",
                        "fix": f"Substitute a real value for {key} on {name} "
                               f"(if this file is a sizing template, build "
                               f"from its filled-in output instead).",
                    })
                continue

            if k not in GEOMETRY_PARAMS:
                continue

            if num <= 0:
                findings.append({
                    "check": "invalid_geometry", "severity": "error",
                    "message": f"{name}: {key}={val} is not a positive length.",
                    "fix": f"Set {key} on {name} to a real drawn dimension "
                           f"in microns.",
                })
            elif num < MIN_PLAUSIBLE_DRAWN_UM:
                findings.append({
                    "check": "geometry_unit_scale", "severity": "error",
                    "message": f"{name}: {key}={val} is far below the "
                               f"{MIN_PLAUSIBLE_DRAWN_UM}um minimum plausible "
                               f"drawn dimension -- this is an SI-METRE value "
                               f"in a netlist whose models bin on MICRONS "
                               f"({key}={val} reads as {num:g}um, 1e6x too "
                               f"small; the intended dimension is almost "
                               f"certainly {num * 1e6:g}um). It will not "
                               f"resolve to a model bin.",
                    "fix": f"Rescale {name}'s {key} by 1e6 "
                           f"({key}={val} -> {key}={num * 1e6:g}). Check the "
                           f"whole netlist -- this is normally a "
                           f"file-wide convention, not one device.",
                })
    return findings


_CARD_RE = re.compile(r"^[ \t]*\.(subckt|model)[ \t]+(\S+)", re.IGNORECASE | re.MULTILINE)
_PDK_CARD_CACHE = {}


def load_pdk_model_cards(pdk_root=None, pdk="sky130A"):
    """Map every model/subckt name the PDK defines -> the set of CARD
    TYPES it is defined with ({"subckt"}, {"model"}, or both).

    Needed because the element prefix a device must be instantiated with
    is decided by the card type, not by the device's physical kind (see
    check_element_prefix_vs_model_card). Scanning this PDK's ~675 spice
    files takes ~0.3s and is cached per process, so the cost is paid at
    most once per run. Returns {} when the PDK isn't on disk -- the check
    then degrades to an INFO finding rather than failing."""
    pdk_root = pdk_root or os.environ.get("PDK_ROOT", os.path.expanduser("~/pdk/manual"))
    key = (pdk_root, pdk)
    if key in _PDK_CARD_CACHE:
        return _PDK_CARD_CACHE[key]
    base = os.path.join(pdk_root, pdk)
    cards = {}
    if os.path.isdir(base):
        import glob as _glob
        patterns = [os.path.join(base, "libs.ref", "*", "spice", "*.spice"),
                    os.path.join(base, "libs.tech", "ngspice", "*.spice")]
        for pattern in patterns:
            for f in _glob.glob(pattern):
                try:
                    text = open(f, errors="ignore").read()
                except OSError:
                    continue
                for kind, name in _CARD_RE.findall(text):
                    cards.setdefault(name, set()).add(kind.lower())
    _PDK_CARD_CACHE[key] = cards
    return cards


_PDK_BIN_CACHE = {}
_MODEL_BIN_RE = re.compile(r"^\s*\.model\s+(\S+?)\.\d+\s", re.IGNORECASE)
_WMAX_RE = re.compile(r"\bwmax\s*=\s*([0-9.eE+-]+)", re.IGNORECASE)


def load_pdk_bin_widths(pdk_root=None, pdk="sky130A"):
    """Map a PDK model base name -> the largest per-device `w` any of its
    bins accepts, IN MICRONS.

    A binned BSIM4 model is a family of `.model <base>.<n>` cards, each
    valid over an `lmin..lmax` x `wmin..wmax` window. A geometry outside
    every window resolves to no card at all. The widest `wmax` in the
    family is therefore the hard ceiling on one device's width -- exceed
    it and the device has no model whatever its length.

    Widths are converted to microns here because that is the unit this
    project's netlists are written in (see check_device_parameters), while
    the model cards state metres. Returns {} when the PDK isn't on disk,
    so the check degrades to silence rather than to a false alarm."""
    pdk_root = pdk_root or os.environ.get("PDK_ROOT", os.path.expanduser("~/pdk/manual"))
    key = (pdk_root, pdk)
    if key in _PDK_BIN_CACHE:
        return _PDK_BIN_CACHE[key]
    base_dir = os.path.join(pdk_root, pdk)
    bins = {}
    if os.path.isdir(base_dir):
        import glob as _glob
        for f in _glob.glob(os.path.join(base_dir, "libs.ref", "*", "spice", "*.spice")):
            try:
                text = open(f, errors="ignore").read()
            except OSError:
                continue
            if "wmax" not in text:
                continue
            current = None
            for line in text.splitlines():
                m = _MODEL_BIN_RE.match(line)
                if m:
                    current = m.group(1).lower()
                    continue
                if current:
                    wm = _WMAX_RE.search(line)
                    if wm:
                        try:
                            w_um = float(wm.group(1)) * 1e6
                        except ValueError:
                            continue
                        bins[current] = max(bins.get(current, 0.0), w_um)
    _PDK_BIN_CACHE[key] = bins
    return bins


def check_model_bin_width(devices, bins):
    """A device's PER-COPY `w` must fall inside some bin of the model it
    names. This is a CONSTRAINT the PDK imposes on the search space, not a
    judgement about the design -- so it is reported as a bound that was
    exceeded, with the folding that satisfies it, rather than as a bad
    value.

    Why it needs its own check: nothing else sees it. The netlist parses,
    every connectivity rule passes, and the failure appears only at
    ngspice as `could not find a valid modelname` -- which
    `../SKILL.md`'s Step 4b classifies as pre-sizing device-parameter
    noise to record rather than report. The run then produces no data at
    all and Step 5's viability gate calls the netlist non-functional. The
    real cause (one width over a bin ceiling) is named nowhere in that
    chain, and has to be rediscovered by sweeping widths against the PDK
    by hand.

    **`m` is the lever, not `nf`.** The sky130 device subckt passes `w`
    through to the model per instance and `nf` does not divide it, so
    `w=320 nf=4` still has no bin while `w=40 m=8` has one and carries the
    same 320um total. Verified by simulation, both directions.

    A model whose bins this PDK does not define is skipped in silence --
    local `.subckt`s and generic models are not this check's business."""
    findings = []
    if not bins:
        return findings
    for d in devices:
        model = d.get("model_name") or (d.get("subckt_name")
                                        if d["kind"] == "subckt_call" else None)
        if not model:
            continue
        wmax = bins.get(f"{model}__model".lower()) or bins.get(model.lower())
        if not wmax:
            continue
        raw = d.get("raw_line") or ""
        w = None
        for key, val in _PARAM_RE.findall(raw):
            if key.lower() == "w":
                w = _parse_spice_number(val)
        if w is None or w <= wmax:
            continue
        need_m = int(math.ceil(w / wmax))
        findings.append({
            "check": "model_bin_width", "severity": "error",
            "message": (f"{d.get('name', '?')}: w={w:g} exceeds the widest "
                        f"bin of {model} (wmax={wmax:g}um PER COPY), so this "
                        f"device resolves to no model card and nothing "
                        f"simulates. This is a PDK bound on the search "
                        f"space, not a bad value -- the total width is a "
                        f"design choice, its division into copies is not."),
            "fix": (f"Split the same total across copies: w={w / need_m:g} "
                    f"m={need_m} on {d.get('name', '?')} -- total {w:g}um, "
                    f"unchanged. `nf` does NOT satisfy this bound: the model "
                    f"bins on per-copy w and nf does not divide it "
                    f"(w=320 nf=4 has no bin; w=40 m=8 does)."),
        })
    return findings


def check_element_prefix_vs_model_card(devices, cards):
    """A device's SPICE element prefix must match how the PDK DEFINES the
    model it names -- a `.subckt` card can only be instantiated with `X`,
    a `.model` card only with its primitive letter. Names are correct in
    both failure modes, so nothing else in this checker notices.

    Both directions are real, expensive bugs this project has already hit:

    - **`.subckt`-only card instantiated as a primitive.** Every sky130
      MOSFET and MiM cap (`sky130_fd_pr__nfet_01v8`,
      `sky130_fd_pr__cap_mim_m3_1`, ...) is a `.subckt`, so `M0 d g s b
      sky130_fd_pr__nfet_01v8 ...` dies with "could not find a valid
      modelname" and NOTHING simulates. Found by running this skill's own
      workflow against a real design, where the netlist passed every
      other check and then produced an empty netlist at ngspice.
    - **A card with BOTH a `.model` and a `.subckt` instantiated as `X`.**
      `sky130_fd_pr__res_generic_po` is the documented case: it simulates
      fine as `X`, then netgen reports both terminals on synthetic
      `dummy_N` nets and LVS fails as a phantom open circuit. See
      `../../../reference/environment.md`'s "resistor element-prefix syntax
      must match" -- it has cost multiple wasted iterations, which is why
      it is caught here at intake rather than at the LVS gate.

    Models the PDK doesn't define at all are skipped silently: a netlist's
    own local `.subckt`s, and generic model names, are not this check's
    business."""
    errors = []
    for d in devices:
        model = d.get("model_name") or (d.get("subckt_name")
                                        if d["kind"] == "subckt_call" else None)
        if not model:
            continue
        types = cards.get(model)
        if not types:
            continue
        prefix = "X" if d["kind"] == "subckt_call" else d["name"][0].upper()
        if types == {"subckt"} and prefix != "X":
            errors.append({
                "check": "element_prefix_mismatch", "severity": "error",
                "message": f"line {d['lineno']}: {d['name']} instantiates "
                           f"'{model}' with the '{prefix}' primitive prefix, but "
                           f"the PDK defines '{model}' ONLY as a `.subckt` -- "
                           f"ngspice will fail with \"could not find a valid "
                           f"modelname\" and run no simulation at all.",
                "fix": f"Rename the instance to 'X{d['name']}' (keep the same "
                       f"terminal order and parameters) so it is a subcircuit "
                       f"call. This is normally a file-wide convention -- check "
                       f"every device in the netlist, not just {d['name']}.",
            })
        elif prefix == "X" and "model" in types:
            if "subckt" in types:
                errors.append({
                    "check": "element_prefix_mismatch", "severity": "error",
                    "message": f"line {d['lineno']}: {d['name']} instantiates "
                               f"'{model}' as a subcircuit call, but the PDK "
                               f"defines '{model}' with BOTH a `.model` and a "
                               f"`.subckt` card. It will simulate, then netgen "
                               f"will report both terminals on synthetic "
                               f"'dummy_N' nets and LVS will fail as a phantom "
                               f"open circuit.",
                    "fix": f"Rename the instance to "
                           f"'{d['name'][1:] or d['name']}' so it uses the "
                           f"primitive prefix Magic's ext2spice also emits "
                           f"(see ../../../reference/environment.md, \"resistor "
                           f"element-prefix syntax must match\").",
                })
            else:
                errors.append({
                    "check": "element_prefix_mismatch", "severity": "error",
                    "message": f"line {d['lineno']}: {d['name']} instantiates "
                               f"'{model}' as a subcircuit call, but the PDK "
                               f"defines '{model}' ONLY as a `.model` card -- "
                               f"there is no subcircuit of that name to call.",
                    "fix": f"Instantiate '{model}' with its primitive element "
                           f"prefix instead of 'X' (e.g. 'M...'/'R...'/'C...' "
                           f"as the device kind requires).",
                })
    return errors


def run_erc(path, backend="builtin", params_only=False,
            pdk_root=None, pdk="sky130A"):
    """Entry point. `backend="builtin"` (default) runs every check in
    this module. See "Backend extension point" below for how a future
    commercial ERC tool integrates without changing this function's
    return shape.

    `params_only=True` runs just `check_device_parameters()` and skips
    every connectivity check -- ../SKILL.md's Step 2a gate. The return
    shape is identical either way, so every caller (print_report, --json,
    the exit code) works unchanged."""
    if backend != "builtin":
        return run_backend_erc(path, backend)

    text = open(path).read()
    base_dir = os.path.dirname(os.path.abspath(path))
    parsed = parse_spice_text(text, base_dir=base_dir)
    top, subckts = parsed["top"], parsed["subckts"]

    # Tag each device with the scope it was declared in before flattening --
    # check_duplicate_names() needs it, since instance names are per-.subckt.
    all_devices = list(top["devices"])
    for d in all_devices:
        d["scope"] = None
    for scope_name, s in subckts.items():
        for d in s["devices"]:
            d["scope"] = scope_name
        all_devices.extend(s["devices"])
    all_sources = [d for d in all_devices if d.get("kind") == "source"]

    if params_only:
        return {
            "path": path,
            "subckts": {name: {"ports": s["ports"]} for name, s in subckts.items()},
            "device_counts": _device_counts(all_devices),
            "findings": (check_device_parameters(all_devices)
                         + check_model_bin_width(
                             all_devices, load_pdk_bin_widths(pdk_root, pdk))),
        }

    findings = []
    findings += check_shorts(all_devices)
    findings += check_duplicate_names(all_devices)
    findings += check_subckt_port_counts(subckts)
    findings += check_conflicting_drivers(all_sources)
    findings += check_ground_reference(top["devices"])
    findings += check_model_sources(top)
    findings += check_testbench_sanity(top)
    findings += check_terminal_degeneracy(all_devices)
    findings += check_device_parameters(all_devices)

    # 2a's PDK-bound check runs here too: ../SKILL.md promises the full
    # pass re-includes every 2a check, so omitting it would let a netlist
    # that 2a hard-stops come back clean from 2b alone.
    findings += check_model_bin_width(
        all_devices, load_pdk_bin_widths(pdk_root=pdk_root, pdk=pdk))

    cards = load_pdk_model_cards(pdk_root=pdk_root, pdk=pdk)
    if cards:
        findings += check_element_prefix_vs_model_card(all_devices, cards)
    else:
        findings.append({
            "check": "pdk_not_scanned", "severity": "info",
            "message": f"PDK '{pdk}' was not found under "
                       f"{pdk_root or os.environ.get('PDK_ROOT', '~/pdk/manual')} "
                       f"-- the element-prefix-vs-model-card check was skipped.",
            "fix": "Set PDK_ROOT (or pass --pdk-root/--pdk) if you want device "
                   "lines cross-checked against how the PDK actually defines "
                   "each model -- see ../../../reference/environment.md.",
        })

    for scope_name, scope in subckts.items():
        floating, unused_ports = check_net_fanout(scope["devices"], scope["ports"])
        for net in floating:
            findings.append({
                "check": "floating_net", "severity": "warning",
                "message": f"in .subckt {scope_name}: net '{net}' is touched "
                           f"by only one device terminal -- floating/open.",
                "fix": f"Trace net '{net}' in .subckt {scope_name} -- either "
                       f"it's missing a second connection, or it's an "
                       f"intentional test point (confirm, don't assume).",
            })
        for port in unused_ports:
            findings.append({
                "check": "unused_port", "severity": "warning",
                "message": f".subckt {scope_name} declares port '{port}' but "
                           f"it's never referenced inside the body.",
                "fix": f"Remove '{port}' from {scope_name}'s port list if it's "
                       f"truly unused, or check whether a device line inside "
                       f"the body should be connected to it.",
            })
    # top-level floating-net check (a testbench's own top-level statements,
    # not inside any .subckt) -- no declared "ports" at top level, so
    # every fanout==1 net is a candidate
    top_floating, _ = check_net_fanout(top["devices"], [])
    for net in top_floating:
        findings.append({
            "check": "floating_net", "severity": "warning",
            "message": f"top-level net '{net}' is touched by only one "
                       f"device terminal -- floating/open.",
            "fix": f"Trace net '{net}' -- either it's missing a second "
                   f"connection, or it's an intentional test point "
                   f"(confirm, don't assume).",
        })

    for inc in parsed["unresolved_includes"]:
        findings.append({
            "check": "unresolved_include", "severity": "info",
            "message": f"`.include \"{inc}\"` could not be resolved relative "
                       f"to {base_dir} -- any subckt it defines was not "
                       f"available for port-count cross-checking.",
            "fix": f"If '{inc}' is a relative path, confirm it's correct "
                   f"relative to this file's own directory; if it's an "
                   f"absolute/PDK path, this is expected and not an error.",
        })

    return {
        "path": path,
        "subckts": {name: {"ports": s["ports"]} for name, s in subckts.items()},
        "device_counts": _device_counts(all_devices),
        "findings": findings,
    }


def run_backend_erc(path, backend):
    """**Backend extension point** -- not implemented yet, by design (see
    ../SKILL.md's "Backend extension point"). A future commercial ERC
    tool (Cadence Pegasus/PVS, Mentor Calibre xRC, etc.) plugs in here:
    implement a `_run_<backend>_erc(path)` function that shells out to
    the real tool, parses its native report, and returns a dict with the
    SAME shape `run_erc()` returns (`path`, `subckts`, `device_counts`,
    `findings` -- each finding a dict with `check`/`severity`/`message`/
    `fix`), then dispatch to it here by `backend` name. Every downstream
    consumer of `run_erc()`'s return value (print_report(), --json,
    severity-based exit code) keeps working unchanged."""
    raise NotImplementedError(
        f"ERC backend {backend!r} is not implemented -- only 'builtin' "
        f"(this module's own checks) exists today. See run_backend_erc()'s "
        f"own docstring for the integration contract a future commercial "
        f"ERC tool would need to satisfy.")


def _device_counts(devices):
    counts = {}
    for d in devices:
        key = d["kind"]
        counts[key] = counts.get(key, 0) + 1
    return counts


_SEVERITY_MARK = {"error": "x", "warning": "!", "info": "?"}


def print_report(result):
    print(f"=== ERC-runner: {result['path']} ===\n")
    if result["subckts"]:
        print("Subcircuits found:")
        for name, s in result["subckts"].items():
            print(f"  .subckt {name} ({len(s['ports'])} ports: {', '.join(s['ports'])})")
        print()
    print(f"Device counts: {result['device_counts']}\n")

    by_severity = {"error": [], "warning": [], "info": []}
    for f in result["findings"]:
        by_severity[f["severity"]].append(f)

    if by_severity["error"]:
        print(f"HARD ERRORS ({len(by_severity['error'])}, must fix):")
        for f in by_severity["error"]:
            print(f"  {_SEVERITY_MARK['error']} [{f['check']}] {f['message']}")
            print(f"      fix: {f['fix']}")
        print()
    else:
        print("HARD ERRORS: none\n")

    if by_severity["warning"]:
        print(f"WARNINGS ({len(by_severity['warning'])}, review before proceeding):")
        for f in by_severity["warning"]:
            print(f"  {_SEVERITY_MARK['warning']} [{f['check']}] {f['message']}")
            print(f"      fix: {f['fix']}")
        print()

    if by_severity["info"]:
        print(f"INFO ({len(by_severity['info'])}, not auto-flagged as wrong):")
        for f in by_severity["info"]:
            print(f"  {_SEVERITY_MARK['info']} [{f['check']}] {f['message']}")
            print(f"      fix: {f['fix']}")
        print()

    n_err, n_warn = len(by_severity["error"]), len(by_severity["warning"])
    if n_err:
        print(f"Result: FAIL -- {n_err} hard error(s) must be fixed before proceeding.")
    elif n_warn:
        print(f"Result: PASS, with {n_warn} warning(s) -- review before proceeding.")
    else:
        print("Result: PASS -- no ERC issues found.")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="netlist or testbench .sp/.spice file")
    ap.add_argument("--backend", default="builtin",
                     help="ERC backend to use (default: builtin -- this "
                          "module's own checks; see run_backend_erc() for "
                          "the future-commercial-tool extension point)")
    ap.add_argument("--json", default=None)
    ap.add_argument("--pdk", default=_guideline_pdk(),
                     help="PDK whose model/subckt cards device lines are "
                          "cross-checked against (default: sky130A)")
    ap.add_argument("--pdk-root", default=None,
                     help="PDK root (default: $PDK_ROOT, else ~/pdk/manual). "
                          "If the PDK isn't there the element-prefix check is "
                          "skipped with an INFO finding, not a failure.")
    ap.add_argument("--params-only", action="store_true",
                     help="run ONLY the device-parameter-value checks "
                          "(unit scale, unsubstituted placeholders, "
                          "non-positive geometry) and skip every "
                          "connectivity check. This is ../SKILL.md's Step 2a "
                          "gate: a netlist whose w/l are in the wrong unit "
                          "cannot simulate at all, so it is worth failing on "
                          "before spending a full ERC pass -- and the "
                          "connectivity findings would be noise until the "
                          "geometry is fixed. The full run (no flag) "
                          "includes these checks too.")
    args = ap.parse_args()

    result = run_erc(args.path, backend=args.backend,
                     params_only=args.params_only,
                     pdk_root=args.pdk_root, pdk=args.pdk)
    print_report(result)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nSaved: {args.json}")

    n_err = sum(1 for f in result["findings"] if f["severity"] == "error")
    n_warn = sum(1 for f in result["findings"] if f["severity"] == "warning")
    if n_err:
        sys.exit(1)
    if n_warn:
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
