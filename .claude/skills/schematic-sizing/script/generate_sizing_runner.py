#!/usr/bin/env python3
"""Generate this design's own sizing runner, by reading its actual inputs.

Run ONCE per design, at `../SKILL.md`'s Step 1a -- right after Step 1 has
written the tuning netlist and its structure-group map. It reads the
netlist, the testbench and `target_spec.json`, resolves every fact the sizing
loop would otherwise need re-supplied on each invocation, and writes

    <design_dir>/sizing/run_sizing_<design_name>.py

a small, readable, per-design runner that calls the shared simulation engine
(`run_sizing_iteration.py`) with those facts baked in.

WHY GENERATE, rather than pass flags to the shared script every time:

  * The facts are DERIVED, not remembered. Which nets are supplies, which
    analyses the deck carries, what the deck names its raw files, whether the
    netlist has MOS devices to probe at all -- all of that is readable from
    the design's own files, and a flag list retyped per invocation is a flag
    list that goes stale or gets forgotten. Here it is resolved once, in
    writing, next to the design it describes.
  * Some of it is CODE, not values. A spec key no registered extractor
    produces needs a real measurement function -- see `measure()` in the
    generated file. No argument can supply that.
  * The generated file is the record of what was assumed. When a later run
    disagrees with an earlier one, the diff is a file, not a shell history.

WHAT STAYS SHARED: the engine. The generated runner imports
`run_iteration()` and adds no simulation logic of its own, so a fix to the
engine reaches every design without regenerating anything. Regenerate only
when the design's own inputs change (a new testbench, a new spec key).

Usage:
  python generate_sizing_runner.py <design_dir> \\
      --netlist <design_dir>/netlist/<design_name>.sp \\
      --testbench <design_dir>/testbench/<deck>.spice \\
      --spec <design_dir>/spec/target_spec.json \\
      [--pdk sky130A] [--force]

Exits non-zero if a fact it cannot resolve would make the runner wrong --
it names what it could not determine rather than guessing a default.
"""

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from netlist_devices import parse_devices  # noqa: E402

# Metrics the registered extractors already produce, by analysis key. A spec
# key outside these needs either a new extractor in
# ../../../reference/compute_fidelity.py's EXTRACTORS or a measure() hook below.
PRODUCED_BY = {
    "ac": ("Gain", "UGBW", "PM"),
}
# Produced from op-point data rather than a raw-file extractor.
FROM_OP_POINT = ("Power",)

_ANALYSIS_RE = re.compile(r"^\s*\.?(ac|dc|tran|op|noise|disto|pz|sens|tf|four)\b",
                          re.MULTILINE | re.IGNORECASE)
_DCSRC_RE = re.compile(
    r"^(?P<name>[vV]\S*)\s+(?P<np>\S+)\s+(?P<nn>\S+)\s+.*?\bdc\s*=?\s*(?P<val>\S+)",
    re.MULTILINE)
_WRITE_RE = re.compile(r"^\s*write\s+(\S+)", re.MULTILINE | re.IGNORECASE)
_WRDATA_RE = re.compile(r"^\s*wrdata\s+(\S+)", re.MULTILINE | re.IGNORECASE)
_SUBCKT_RE = re.compile(r"^\.subckt\s+(\S+)", re.MULTILINE | re.IGNORECASE)


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


def _num(tok):
    """Best-effort SPICE number; None for a .param reference like `vcm`."""
    m = re.match(r"^([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)", tok)
    return float(m.group(1)) if m else None


def analyze(netlist_path, testbench_path, spec_path):
    """Everything the runner needs, read off the design's own files."""
    nl = open(netlist_path).read()
    tb = open(testbench_path).read()
    spec = json.load(open(spec_path))

    facts = {"unresolved": [], "notes": []}
    facts["netlist_basename"] = os.path.basename(netlist_path)
    facts["testbench_basename"] = os.path.basename(testbench_path)
    facts["spec_keys"] = sorted(spec)

    # --- devices: is there anything to probe, and anything to tune? --------
    devs = parse_devices(nl)
    kinds = {}
    for d in devs:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    facts["device_kinds"] = kinds
    facts["has_mos"] = kinds.get("mos", 0) > 0
    if not facts["has_mos"]:
        facts["notes"].append(
            "no MOS devices: generate_op_probe.py raises on this netlist, so "
            "there is no per-device op-point data to reason from and no "
            "op-point route to a power-like key. The loop works from spec "
            "deltas alone.")

    # --- analyses the deck actually carries --------------------------------
    present = sorted({m.lower() for m in _ANALYSIS_RE.findall(tb)})
    facts["analyses_in_deck"] = present
    extractable = [a for a in present if a in PRODUCED_BY]
    if extractable:
        facts["analysis"] = extractable[0]
        if len(extractable) > 1:
            facts["notes"].append(
                "deck carries more than one extractable analysis (%s); chose "
                "%r -- change `ANALYSIS` if that is the wrong one."
                % (", ".join(extractable), facts["analysis"]))
    else:
        facts["analysis"] = None
        facts["unresolved"].append(
            "no analysis in this deck has a registered extractor (deck has: %s; "
            "registered: %s). Register one in compute_fidelity.EXTRACTORS, or "
            "write measure() in the generated runner."
            % (", ".join(present) or "none", ", ".join(sorted(PRODUCED_BY))))

    # --- supply nets: DC sources in the deck, cross-checked against the
    #     netlist's own device source nets ---------------------------------
    dc_rails = {}
    for m in _DCSRC_RE.finditer(tb):
        val = _num(m.group("val"))
        if val is None or val == 0.0:
            continue                      # a 0 V return or a .param stimulus
        for net in (m.group("np"), m.group("nn")):
            if net not in ("0", "gnd", "GND"):
                dc_rails[net] = val
    src_nets = {d["nets"][2].upper() for d in devs
                if d["kind"] == "mos" and len(d["nets"]) >= 4}
    supplies = {n: v for n, v in dc_rails.items() if n.upper() in src_nets}
    facts["supply_nets"] = sorted(supplies)
    facts["supply_volts"] = supplies
    facts["vdd"] = max(supplies.values()) if supplies else None
    if not supplies:
        facts["unresolved"].append(
            "could not identify a supply net: the deck's non-zero DC sources "
            "drive %s, and no MOS device sources sit on any of them. Power "
            "cannot be computed -- set SUPPLY_NETS by hand."
            % (sorted(dc_rails) or "nothing"))
    elif len(supplies) > 1:
        facts["notes"].append(
            "multi-rail design: power sums %s. Confirm that is every rail the "
            "circuit draws through." % ", ".join(f"{n}={v}" for n, v in
                                                  sorted(supplies.items())))

    # --- raw filenames the deck writes ------------------------------------
    writes = [w.lstrip("./") for w in _WRITE_RE.findall(tb)]
    wrdatas = [w.lstrip("./") for w in _WRDATA_RE.findall(tb)]
    facts["write_files"] = writes
    facts["wrdata_files"] = wrdatas
    if not writes:
        facts["unresolved"].append(
            "the deck's .control block has no `write` line, so no raw file is "
            "produced for an extractor to read -- the testbench owns "
            "adding it).")

    # --- the wrapper shape the op-probe splice needs -----------------------
    subckts = _SUBCKT_RE.findall(tb)
    facts["tb_wrapper"] = subckts[0] if subckts else None
    if not subckts:
        facts["notes"].append(
            "no .subckt wrapper in the deck: detect_hier_prefix() will raise, "
            "so pass HIER_PREFIX explicitly.")

    # --- which spec keys are actually reachable ----------------------------
    reachable = set(PRODUCED_BY.get(facts["analysis"] or "", ()))
    if facts["has_mos"] and supplies:
        reachable |= set(FROM_OP_POINT)
    facts["keys_reachable"] = sorted(k for k in spec if k in reachable)
    facts["keys_need_hook"] = sorted(k for k in spec if k not in reachable)
    if facts["keys_need_hook"]:
        facts["notes"].append(
            "no extractor produces %s -- check_target() will list them under "
            "`unmeasured` (never as missed) until measure() returns them."
            % ", ".join(facts["keys_need_hook"]))
    return facts


_TEMPLATE = '''#!/usr/bin/env python3
"""Sizing runner for {design_name} -- GENERATED, do not hand-edit the CONFIG.

Written by `generate_sizing_runner.py` from this design's own netlist,
testbench and target_spec.json. Regenerate when any of those three change;
edit `measure()` by hand, that is what it is for.

The simulation engine is NOT copied here: `run_iteration()` is imported from
the shared `run_sizing_iteration.py`, so engine fixes reach this design
without regenerating. What lives here is what is true of THIS design.

Usage:
  python {runner_name} --iter <n> [--set MN1_W=45.2,...] [--save-artifacts]
      [--target-spec <path>] [--json <path>]
"""

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# The shared engine, found by walking up to the skills tree.
for _up in range(0, 7):
    _c = os.path.normpath(os.path.join(
        _HERE, *([".."] * _up), ".claude", "skills", "schematic-sizing", "script"))
    if os.path.isfile(os.path.join(_c, "run_sizing_iteration.py")):
        sys.path.insert(0, _c)
        break
else:
    raise SystemExit("cannot find run_sizing_iteration.py -- is this file still "
                     "inside the project tree?")
from run_sizing_iteration import run_iteration  # noqa: E402

# ------------------------------------------------------------------ CONFIG
# Every value below was READ from this design's files, not assumed.
DESIGN_NAME      = {design_name!r}
DESIGN_DIR       = os.path.normpath(os.path.join(_HERE, ".."))
NETLIST_BASENAME = {netlist_basename!r}
TUNING_NETLIST   = os.path.join(_HERE, {tuning_name!r})   # read AND written
GROUPS           = os.path.join(_HERE, "structure_groups.json")
TESTBENCH        = os.path.join(DESIGN_DIR, "testbench", {testbench_basename!r})
SPEC             = os.path.join(DESIGN_DIR, "spec", "target_spec.json")
PDK              = {pdk!r}
OUT_ROOT         = os.path.join(_HERE, "debug")  # only --save-artifacts writes here

# Which extractor reads this design's raw file (compute_fidelity.EXTRACTORS).
# Deck carries: {analyses_in_deck}
ANALYSIS         = {analysis!r}
# Supply net(s) the operating current is drawn through, and their volts.
# Detected from the deck's own non-zero DC sources, cross-checked against the
# netlist's device source nets. Power sums every device sourced on these.
SUPPLY_NETS      = {supply_nets!r}
# PER-RAIL volts, each read from its own DC source. This is what power is
# weighted by -- NOT a single number: on a split-rail design, summing a 1.8V
# and a 3.3V rail at one voltage is wrong by however much they differ.
SUPPLY_VOLTS     = {supply_volts!r}
# The highest rail, kept for reference only (headroom sanity checks, reports).
# Deliberately NOT what power uses -- see SUPPLY_VOLTS above.
VDD              = {vdd!r}
# Op-point attributes to probe. Device kinds in this netlist: {device_kinds}
ATTRS            = "gm,gds,id,vds,vgs,vdsat,cgg"

# Spec keys, split by whether anything measures them today.
KEYS_REACHABLE   = {keys_reachable!r}
KEYS_NEED_HOOK   = {keys_need_hook!r}
# -------------------------------------------------------------- END CONFIG


def measure(result):
    """Per-design measurements no registered extractor produces.

    Return {{spec_key: value}} in the spec's OWN units; they are merged into
    `result["specs"]`, and `check_target()` picks them up by name with no
    METRIC_MAP entry needed. Anything you cannot measure, leave out -- it is
    then reported as `unmeasured` rather than scored as a missed target, which
    is the honest outcome and the one that does not burn the budget.

    `result` carries: specs, op_points, raw file paths, ngspice output.

    KEYS STILL NEEDING A MEASUREMENT HERE: {keys_need_hook_text}
    """
    extra = {{}}
{hook_body}
    return extra


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iter", required=True)
    ap.add_argument("--set", default=None, help="comma-separated NAME=VAL overrides")
    ap.add_argument("--target-spec", default=None,
                     help="default: this design's harder_target_spec.json if it "
                          "exists, else spec/target_spec.json")
    ap.add_argument("--save-artifacts", action="store_true")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    values = {{}}
    for pair in (args.set or "").split(","):
        k, _, v = pair.partition("=")
        if k.strip() and v:
            try:
                values[k.strip()] = float(v)
            except ValueError:
                raise SystemExit(f"--set {{pair.strip()}}: {{v!r}} is not a number "
                                 f"(geometry is in microns; write 45, not 45u)")

    target_path = args.target_spec
    if target_path is None:
        harder = os.path.join(_HERE, "harder_target_spec.json")
        target_path = harder if os.path.isfile(harder) else SPEC
    target_spec = json.load(open(target_path))

    result = run_iteration(
        DESIGN_DIR, TUNING_NETLIST, TESTBENCH, NETLIST_BASENAME, values, GROUPS,
        PDK, args.iter, ATTRS.split(","), target_spec=target_spec,
        out_root=OUT_ROOT, save_artifacts=args.save_artifacts,
        supply_nets=tuple(SUPPLY_NETS) or ("VDD",),
        supply_volts=SUPPLY_VOLTS or None,
        analysis=ANALYSIS or "ac")

    extra = measure(result) or {{}}
    if extra:
        result.setdefault("specs", {{}}).update(extra)
        result["measure_hook"] = sorted(extra)
        # Re-score with the hook's keys included.
        from run_sizing_iteration import check_target
        result["target"] = check_target(result["specs"], target_spec)

    result["target_spec_used"] = target_path
    print(json.dumps(result, indent=2))
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
'''


def render(facts, design_name, pdk, tuning_name):
    need = facts["keys_need_hook"]
    if need:
        hook = "\n".join(
            "    # TODO {k}: measure it from result['raw_paths'] / "
            "result['op_points'] and set extra[{k!r}].".format(k=k) for k in need)
    else:
        hook = ("    # Nothing to add: every spec key is produced by the "
                "registered\n    # extractor or from op-point data. Leave this "
                "returning {} unless a\n    # new key is added to "
                "target_spec.json.")
    return _TEMPLATE.format(
        design_name=design_name,
        runner_name="run_sizing_%s.py" % design_name,
        netlist_basename=facts["netlist_basename"],
        testbench_basename=facts["testbench_basename"],
        tuning_name=tuning_name,
        pdk=pdk,
        analysis=facts["analysis"],
        analyses_in_deck=", ".join(facts["analyses_in_deck"]) or "none",
        supply_nets=facts["supply_nets"],
        supply_volts=facts["supply_volts"],
        vdd=facts["vdd"],
        device_kinds=", ".join(f"{k}={v}" for k, v in sorted(facts["device_kinds"].items())) or "none",
        keys_reachable=facts["keys_reachable"],
        keys_need_hook=facts["keys_need_hook"],
        keys_need_hook_text=", ".join(need) if need else "none",
        hook_body=hook,
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design_dir")
    ap.add_argument("--netlist", required=True)
    ap.add_argument("--testbench", required=True)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--pdk", default=_guideline_pdk())
    ap.add_argument("--force", action="store_true",
                     help="overwrite an existing runner (its measure() edits "
                          "are LOST -- copy them out first)")
    args = ap.parse_args()

    for p in (args.netlist, args.testbench, args.spec):
        if not os.path.isfile(p):
            raise SystemExit("input does not exist: " + p)

    design_name = os.path.splitext(os.path.basename(args.netlist))[0]
    facts = analyze(args.netlist, args.testbench, args.spec)

    sizing_dir = os.path.join(args.design_dir, "sizing")
    os.makedirs(sizing_dir, exist_ok=True)
    runner = os.path.join(sizing_dir, "run_sizing_%s.py" % design_name)
    tuning_name = "%s_tuning.sp" % design_name

    print("=== resolved from this design's own files ===")
    for k in ("netlist_basename", "testbench_basename", "device_kinds",
              "analyses_in_deck", "analysis", "supply_nets", "supply_volts",
              "vdd", "write_files", "wrdata_files", "tb_wrapper",
              "spec_keys", "keys_reachable", "keys_need_hook"):
        print(f"  {k:20} {facts[k]}")
    for n in facts["notes"]:
        print("  NOTE  " + n)
    for u in facts["unresolved"]:
        print("  UNRESOLVED  " + u)

    if os.path.isfile(runner) and not args.force:
        raise SystemExit(
            "\n%s already exists. It may carry hand-written measure() code, so "
            "this refuses to overwrite it -- pass --force once you have copied "
            "that out." % runner)

    with open(runner, "w") as f:
        f.write(render(facts, design_name, args.pdk, tuning_name))
    os.chmod(runner, 0o755)
    print("\nWROTE  %s" % runner)

    if facts["unresolved"]:
        print("\n%d fact(s) UNRESOLVED -- the runner is written but its CONFIG "
              "is incomplete. Fix those before iterating; a guessed default is "
              "what produces a plausible wrong number."
              % len(facts["unresolved"]))
        return 2
    if facts["keys_need_hook"]:
        print("\nmeasure() has TODOs for: %s. Until they return a value those "
              "keys report `unmeasured`, never as missed."
              % ", ".join(facts["keys_need_hook"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
