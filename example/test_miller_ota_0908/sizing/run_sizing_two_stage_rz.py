#!/usr/bin/env python3
"""Sizing runner for two_stage_rz -- GENERATED, do not hand-edit the CONFIG.

Written by `generate_sizing_runner.py` from this design's own netlist,
testbench and target_spec.json. Regenerate when any of those three change;
edit `measure()` by hand, that is what it is for.

The simulation engine is NOT copied here: `run_iteration()` is imported from
the shared `run_sizing_iteration.py`, so engine fixes reach this design
without regenerating. What lives here is what is true of THIS design.

Usage:
  python run_sizing_two_stage_rz.py --iter <n> [--set MN1_W=45.2,...] [--save-artifacts]
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
DESIGN_NAME      = 'two_stage_rz'
DESIGN_DIR       = os.path.normpath(os.path.join(_HERE, ".."))
NETLIST_BASENAME = 'two_stage_rz.sp'
TUNING_NETLIST   = os.path.join(_HERE, 'two_stage_rz_tuning.sp')   # read AND written
# The tunable registry + .sp.j2 template come from the circuit read, one
# directory up. There is no structure_groups.json any more -- see
# script/tunables.py for why two sources for one fact was the bug.
GROUPS           = DESIGN_DIR
TESTBENCH        = os.path.join(DESIGN_DIR, "testbench", 'two_stage_rz_pre.spice')
SPEC             = os.path.join(DESIGN_DIR, "spec", "target_spec.json")
PDK              = 'sky130A'
OUT_ROOT         = os.path.join(_HERE, "debug")  # only --save-artifacts writes here

# Which extractor reads this design's raw file (compute_fidelity.EXTRACTORS).
# Deck carries: ac, op
ANALYSIS         = 'ac'
# Supply net(s) the operating current is drawn through, and their volts.
# Detected from the deck's own non-zero DC sources, cross-checked against the
# netlist's device source nets. Power sums every device sourced on these.
SUPPLY_NETS      = ['VDD']
# PER-RAIL volts, each read from its own DC source. This is what power is
# weighted by -- NOT a single number: on a split-rail design, summing a 1.8V
# and a 3.3V rail at one voltage is wrong by however much they differ.
SUPPLY_VOLTS     = {'VDD': 1.8}
# The highest rail, kept for reference only (headroom sanity checks, reports).
# Deliberately NOT what power uses -- see SUPPLY_VOLTS above.
VDD              = 1.8
# Op-point attributes to probe. Device kinds in this netlist: cap=1, mos=9, res=1
ATTRS            = "gm,gds,id,vds,vgs,vdsat,cgg"

# Spec keys, split by whether anything measures them today.
KEYS_REACHABLE   = ['Gain', 'PM', 'Power', 'UGBW']
KEYS_NEED_HOOK   = []
# -------------------------------------------------------------- END CONFIG


def measure(result):
    """Per-design measurements no registered extractor produces.

    Return {spec_key: value} in the spec's OWN units; they are merged into
    `result["specs"]`, and `check_target()` picks them up by name with no
    METRIC_MAP entry needed. Anything you cannot measure, leave out -- it is
    then reported as `unmeasured` rather than scored as a missed target, which
    is the honest outcome and the one that does not burn the budget.

    `result` carries: specs, op_points, raw file paths, ngspice output.

    KEYS STILL NEEDING A MEASUREMENT HERE: none
    """
    extra = {}
    # Nothing to add: every spec key is produced by the registered
    # extractor or from op-point data. Leave this returning {} unless a
    # new key is added to target_spec.json.
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

    values = {}
    for pair in (args.set or "").split(","):
        k, _, v = pair.partition("=")
        if k.strip() and v:
            try:
                values[k.strip()] = float(v)
            except ValueError:
                raise SystemExit(f"--set {pair.strip()}: {v!r} is not a number "
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

    extra = measure(result) or {}
    if extra:
        result.setdefault("specs", {}).update(extra)
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
