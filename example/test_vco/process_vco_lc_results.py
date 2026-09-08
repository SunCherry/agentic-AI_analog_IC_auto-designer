#!/usr/bin/env python3
"""Per-design results processing for test_vco (cross-coupled LC VCO).

schematic-agent #4c. Derived from .claude/reference/process_results_template.py:
it keeps that template's CONTRACT -- compare THIS design's own simulated numbers
against THIS design's own target_spec.json, in the spec's units, in the right
direction per key, and print NO DATA with a REASON rather than silently dropping
a key -- but not its body, and the reason is structural, not stylistic:

  The template measures Gain/UGBW/PM by parsing an ngspice ASCII raw file from
  an `.ac` sweep (via plot_sim_results.py). THIS DESIGN IS AN OSCILLATOR. There
  is no .ac analysis and no AC raw file to parse; an oscillator has no stable
  small-signal operating point to sweep about. All four spec keys are computed
  by `meas tran` INSIDE the deck's own .control block, across three separate
  transients, and echoed to a text results file. The measurement therefore
  already exists when this script runs -- the job here is to READ it, convert
  units and judge direction, not to re-derive it from a waveform.

  For the same reason .claude/reference/compute_fidelity.py is NOT used and must
  not be: its EXTRACTORS registry holds only "ac", so it raises
  NotImplementedError on this design.

Usage:
  python process_vco_lc_results.py [--results PATH] [--spec PATH] [--json PATH]

Exit 0 = every key met. Exit 1 = at least one key missed. Exit 2 = a key had no
data (which is a testbench problem, not a design problem -- see no_data_reason).
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- CONFIG
DESIGN = "vco_lc"
SUPPLIES = [("vdd", 1.8)]
VDD = max((v for _n, v in SUPPLIES), default=0.0)

# The deck runs from <design_dir>/testbench/ (its .include is ../netlist/...,
# which only resolves there) and its .control writes with a "./" prefix, so both
# artifacts land in testbench/, not next to this script.
RESULTS_TXT = os.path.join(_HERE, "testbench", "vco_lc_pre_results.txt")
WAVEFORM_OUT = os.path.join(_HERE, "testbench", "simout_vco_lc_pre.out")
SPEC_JSON = os.path.join(_HERE, "spec", "target_spec.json")

# How each spec key is named in the results file, and the factor converting the
# deck's SI value into the spec file's unit. The deck reports pure SI (Hz, W)
# except OutputPower, which it reports already in dBm because dBm is not an SI
# scaling of anything -- it is a logarithmic ratio, so there is no factor.
#
# THIS TABLE IS THE WHOLE UNIT CONTRACT. A wrong factor here produces a
# plausible wrong verdict, never an error -- the standing failure mode the
# template warns about.
KEY_MAP = {
    "OscFreq":     {"field": "OscFreq",     "sim_unit": "Hz",  "to_spec_unit": 1e-9, "spec_unit": "GHz"},
    "TuningRange": {"field": "TuningRange", "sim_unit": "Hz",  "to_spec_unit": 1e-6, "spec_unit": "MHz"},
    "OutputPower": {"field": "OutputPower", "sim_unit": "dBm", "to_spec_unit": 1.0,  "spec_unit": "dBm"},
    "Power":       {"field": "Power",       "sim_unit": "W",   "to_spec_unit": 1e3,  "spec_unit": "mW"},
}
# Diagnostic fields the deck also writes. Not spec keys; printed for context
# because f_lo/f_hi are what TuningRange is the difference of, and Vamp_fund is
# what OutputPower is the dBm of -- a key that looks wrong is usually one of
# these two that is wrong.
EXTRA_FIELDS = ["f_lo", "f_hi", "Vamp_fund"]


def parse_results(path):
    """Read the deck's `echo`-written results file into {field: float}.

    Format is one `Name: value` per line after a title line. ngspice's `$&var`
    interpolation writes plain floats, sometimes in E-notation with a capital E.
    A field present but unparseable is returned as None so the caller can tell
    'the deck never wrote it' (absent) from 'the deck wrote garbage' (None) --
    different problems, per no_data_reason().
    """
    if not os.path.exists(path):
        return None
    out = {}
    for line in open(path):
        m = re.match(r"^\s*([A-Za-z_]\w*)\s*:\s*(\S+)\s*$", line)
        if not m:
            continue
        name, raw = m.group(1), m.group(2)
        try:
            out[name] = float(raw)
        except ValueError:
            out[name] = None
    return out


def no_data_reason(key, results, path):
    """Why a key has no number. Three different problems, three different fixes."""
    if results is None:
        return ("the results file does not exist at %s -- the deck never ran, or it "
                "ran from a different cwd (it must run WITH testbench/ as cwd)" % path)
    field = KEY_MAP[key]["field"]
    if field not in results:
        return ("the deck ran but never wrote `%s` -- this is a TESTBENCH gap "
                "(schematic-agent #4a), not a design failure" % field)
    if results[field] is None:
        return ("the deck wrote `%s` but the value did not parse as a number -- "
                "usually a `meas tran` that FAILED (no zero-crossing found), which "
                "on this design means THE CIRCUIT DID NOT OSCILLATE at these sizes. "
                "Treat that as an INVALID iteration, not a poor score." % field)
    return "unknown"


def judge(direction, value, target):
    """met?, and a human string for the bar. Directions per spec_form_template.md."""
    if direction == "FLOOR":
        return value >= target, ">= %g" % target
    if direction == "CEILING":
        return value <= target, "<= %g" % target
    if direction == "RANGE":
        lo, hi = target
        return lo <= value <= hi, "[%g, %g]" % (lo, hi)
    raise ValueError("unknown Direction %r -- expected FLOOR/CEILING/RANGE" % direction)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", default=RESULTS_TXT)
    ap.add_argument("--spec", default=SPEC_JSON)
    ap.add_argument("--json", help="also write the table as JSON here")
    args = ap.parse_args()

    spec = json.load(open(args.spec))
    results = parse_results(args.results)

    print("=" * 78)
    print("SPEC CHECK -- %s" % DESIGN)
    print("results : %s" % args.results)
    print("spec    : %s" % args.spec)
    print("=" * 78)
    print("%-13s %-9s %-14s %-14s %-6s" % ("KEY", "DIR", "TARGET", "MEASURED", "VERDICT"))
    print("-" * 78)

    rows, n_missed, n_nodata = [], 0, 0
    for key, entry in spec.items():
        direction = entry["Direction"]
        target = entry["Value"]
        unit = entry.get("Units", "-")
        if key not in KEY_MAP:
            print("%-13s %-9s %-14s %-14s %s" % (key, direction, target, "NO DATA",
                  "no KEY_MAP entry -- add one"))
            n_nodata += 1
            continue
        field = KEY_MAP[key]["field"]
        if results is None or field not in results or results[field] is None:
            print("%-13s %-9s %-14s %-14s %s" % (key, direction, target, "NO DATA", "--"))
            print("    REASON: %s" % no_data_reason(key, results, args.results))
            n_nodata += 1
            rows.append({"key": key, "measured": None,
                         "reason": no_data_reason(key, results, args.results)})
            continue
        sim_si = results[field]
        value = sim_si * KEY_MAP[key]["to_spec_unit"]
        met, bar = judge(direction, value, target)
        if not met:
            n_missed += 1
        print("%-13s %-9s %-14s %-14s %s" % (
            key, direction, "%s %s" % (bar, unit), "%.6g %s" % (value, unit),
            "MET" if met else "MISS"))
        rows.append({"key": key, "direction": direction, "target": target,
                     "units": unit, "sim_si": sim_si, "measured": value, "met": met})

    print("-" * 78)
    if results:
        extras = ["%s=%.6g" % (f, results[f]) for f in EXTRA_FIELDS
                  if f in results and results[f] is not None]
        if extras:
            print("context: " + "  ".join(extras))
        print("         (TuningRange = f_hi - f_lo ; OutputPower = 10*log10(Vamp_fund^2/(2*50)/1e-3))")
    print("waveform: %s%s" % (WAVEFORM_OUT,
          "" if os.path.exists(WAVEFORM_OUT) else "   [ABSENT]"))

    if n_nodata:
        print("\nVERDICT: %d key(s) with NO DATA -- resolve those before judging the design." % n_nodata)
        rc = 2
    elif n_missed:
        print("\nVERDICT: %d of %d key(s) MISSED." % (n_missed, len(rows)))
        rc = 1
    else:
        print("\nVERDICT: ALL %d KEYS MET." % len(rows))
        rc = 0

    if args.json:
        json.dump(rows, open(args.json, "w"), indent=2)
        print("wrote %s" % args.json)
    return rc


if __name__ == "__main__":
    sys.exit(main())
