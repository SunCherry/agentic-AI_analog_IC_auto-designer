#!/usr/bin/env python3
"""TEMPLATE -- the per-design simulation-results processing script.

`../../agents/schematic-agent.md`'s #4c copies this into `<design_dir>` as
`process_<design_name>_results.py`, fills in the CONFIG block below, and extends
it for any spec key outside the four this project already measures. One
copy per design case, self-contained inside that design's folder, so a
design's results can be re-checked long after the session that produced
them.

What it does that nothing else does: compares a design's OWN simulated
numbers against its OWN `target_spec.json`, in the project's units, in the
right direction per key. `compute_fidelity.py` next to this file answers a
different question -- pre-layout vs post-layout DRIFT, two `.out` files,
no target spec, no `Power`. Neither replaces the other.

Raw-file parsing is reused from `plot_sim_results.py` beside this file, not
reimplemented -- it owns the ngspice ASCII-raw format for this project.

Usage, once filled in:
  python process_<design_name>_results.py [--out-dir .] [--no-plot]

ADDING A SPEC KEY beyond Gain/UGBW/PM/Power: add its unit to UNITS, its
measurement to `measure()`, and -- if smaller is better -- get it into
CEILING_METRICS upstream in `../schematic-sizing/script/run_sizing_iteration.py`
rather than hardcoding it here, or the tuner and this script will disagree
about which direction the key passes in. A key with no measurement prints
NO DATA rather than being silently dropped; that is deliberate, and it is
the same hard stop #4a enforces against the testbench. The row also says
WHY -- see `no_data_reason()`: a key the testbench cannot measure, a raw
file that isn't where this script looked, and an analysis that ran without
producing the value are three different problems with three different
fixes, and only the first is #4a's.
"""
import argparse
import json
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- CONFIG
# schematic-agent fills these in per design. Everything below is generic.
DESIGN = "DESIGN_NAME"                      # e.g. "two_stage_rz"
# Every supply the deck drives, IN THE ORDER its `wrdata` line lists them,
# as (source name, its DC volts). A circuit may have several -- analog and
# digital rails, a split supply, a separate bias rail -- and Power is the
# sum over all of them, so a single-rail design is the one-entry case of
# this, not a different case.
#
# THE ORDER IS LOAD-BEARING. `wrdata` writes no column header, so the only
# thing tying column k to a source is the order of vectors in the deck's own
# `wrdata i(..) i(..)` line. Copy that order here. A mismatch pairs a
# current with the wrong voltage and produces a plausible wrong number, not
# an error -- so re-read the deck rather than trusting this default.
SUPPLIES = [("vdd", 1.8)]
# The single rail AC-side helpers refer to. Taken as the LARGEST voltage in
# SUPPLIES, not SUPPLIES[0][1]: entry order is fixed by the deck's `wrdata`
# vector order rather than by importance, so the first entry is not reliably
# the main rail -- a split supply may list its negative rail first, and a 0 V
# ground return is a supply the circuit draws through and gets an entry too.
# A VDD of 0.0 would be wrong in a way that reads as a plausible zero rather
# than as an error. Set it explicitly if this design's meaning of "the
# supply" is not the largest rail.
VDD = max((v for _n, v in SUPPLIES), default=0.0)
# The raw files land next to the DECK, not next to this script: on a design
# intake filed, the deck is run from <design_dir>/testbench/ (Step 1 rewrote
# its .include to ../netlist/..., which only resolves there) and its .control
# writes relative paths. An older flat design folder has no testbench/ and the
# deck ran in <design_dir> itself, so fall back to that rather than sending
# every key to NO DATA at once -- same two branches TARGET takes below.
# The BASENAMES are a guess and the names actually differ per deck -- the
# committed examples write `simout_rz_pre.out`, not `<design>_pre.out`. Read
# both off the testbench's own `write`/`wrdata` lines; a wrong name here is
# reported as NO DATA with the path it looked at, so check that first.
_RAW_DIR = os.path.join(_HERE, "testbench")
if not os.path.isdir(_RAW_DIR):
    _RAW_DIR = _HERE
AC_RAW = os.path.join(_RAW_DIR, DESIGN + "_pre.out")      # deck's `write` target
OP_RAW = os.path.join(_RAW_DIR, DESIGN + "_pre_op.txt")   # deck's `wrdata` target
# design-sheets-intake files the spec into spec/; older flat design folders
# keep it at the top level, so try that second rather than reporting no target.
TARGET = os.path.join(_HERE, "spec", "target_spec.json")
if not os.path.isfile(TARGET):
    TARGET = os.path.join(_HERE, "target_spec.json")
UNITS = {"Gain": "dB", "UGBW": "MHz", "PM": "deg", "Power": "mW"}
# ------------------------------------------------------------ END CONFIG


def _find_plot_helper(start):
    """`plot_sim_results.py` owns the ngspice ASCII raw parser.

    It lives in the shared reference folder (`.claude/reference/`), which is
    where it is looked for FIRST -- the canonical copy, independent of any
    design folder. The parent-directory walk below is a fallback for an older
    layout that kept it beside the designs; it is deliberately second, so
    deleting a design tree cannot break every per-design results script.
    """
    for up in range(0, 7):
        cand = os.path.normpath(os.path.join(start, *([".."] * up),
                                             ".claude", "reference"))
        if os.path.isfile(os.path.join(cand, "plot_sim_results.py")):
            return cand
    d = start
    for _ in range(6):
        if os.path.isfile(os.path.join(d, "plot_sim_results.py")):
            return d
        d = os.path.dirname(d)
    raise SystemExit(
        "could not locate plot_sim_results.py -- expected it at "
        ".claude/reference/plot_sim_results.py, reachable from " + start)


sys.path.insert(0, _find_plot_helper(_HERE))
from plot_sim_results import parse_ngspice_ascii_raw  # noqa: E402

# CEILING_METRICS is imported, never restated -- schematic-sizing owns the
# FLOOR/CEILING convention, and a local copy would drift from the tuner.
_SIZING = os.path.join(_HERE, "..", "..", ".claude", "skills",
                       "schematic-sizing", "script")
if not os.path.isdir(_SIZING):        # design folders sit at varying depths
    for _up in range(1, 6):
        _c = os.path.join(_HERE, *([".."] * _up),
                          ".claude", "skills", "schematic-sizing", "script")
        if os.path.isdir(_c):
            _SIZING = _c
            break
try:
    sys.path.insert(0, os.path.abspath(_SIZING))
    from run_sizing_iteration import CEILING_METRICS, parse_spec_entry  # noqa: E402
except Exception:
    CEILING_METRICS = {"Power"}

    def parse_spec_entry(key, entry):
        """Fallback for a design folder that cannot see the sizing scripts.
        Same contract as the real one: returns (direction, value, units), reads
        `Direction` off a structured entry, and falls back to CEILING_METRICS
        only for the older flat form."""
        if isinstance(entry, dict):
            d = str(entry.get("Direction", "")).strip().upper()
            v = entry.get("Value")
            if d == "RANGE":
                v = (float(v[0]), float(v[1]))
            elif isinstance(v, (int, float)):
                v = float(v)
            return d, v, (str(entry["Units"]) if entry.get("Units") else None)
        return ("CEILING" if key in CEILING_METRICS else "FLOOR"), float(entry), None

    print("WARNING: could not import CEILING_METRICS from schematic-sizing; "
          "falling back to {'Power'}. Check the direction of every key below.")


def ac_metrics(path):
    """DC gain (dB), unity-gain bandwidth (Hz), phase margin (deg).

    Interpolates the 0 dB crossing in LOG frequency rather than taking the
    nearest sample, so UGBW and PM don't quantise onto the sweep grid.
    Returns (gain, None, None) when the sweep never crosses 0 dB -- that is
    a testbench/netlist problem to report, not a number to invent.
    """
    _, _names, cols, is_complex = parse_ngspice_ascii_raw(path)
    if not is_complex:
        raise SystemExit(path + ": expected a complex AC raw file")
    freq = cols[0].real
    h = cols[-1]
    mag_db = 20 * np.log10(np.abs(h))
    phase = np.angle(h, deg=True)
    gain_db = float(mag_db[0])
    ugb_hz = pm_deg = None
    below = np.where(mag_db < 0)[0]
    if len(below) and below[0] > 0:
        i = below[0]
        f0, f1 = np.log10(freq[i - 1]), np.log10(freq[i])
        m0, m1 = mag_db[i - 1], mag_db[i]
        t = m0 / (m0 - m1)
        ugb_hz = float(10 ** (f0 + t * (f1 - f0)))
        pm_deg = float(180.0 + (phase[i - 1] + t * (phase[i] - phase[i - 1])))
    return gain_db, ugb_hz, pm_deg


def op_power_mw(path, supplies):
    """Total supply power, summed over EVERY supply the deck saves.

    `wrdata` writes an x-column before each vector, so a row of n saved
    currents is `x i1 x i2 ... x in` and the currents sit at the odd
    indices. Taking the last column instead -- which this did while only one
    rail was ever saved -- silently reports the LAST supply's power as the
    whole design's the moment a second rail is added.

    Each current reads NEGATIVE: SPICE measures a source's branch current
    flowing INTO its + terminal, so a supply that is delivering reads
    negative by convention, not by fault. Take |i| per rail before summing,
    or two rails of opposite sign cancel into a plausible, wrong, small
    number.

    `supplies` is CONFIG's ordered [(name, volts)], matching the deck's
    `wrdata` vector order, which by schematic-agent's #4a carries SUPPLY
    sources only -- no stimulus. A trailing extra column is tolerated rather
    than trusted (a hand-edited deck can carry one); fewer is a config error
    worth raising,
    since it means a rail the deck saved is missing from the sum.
    """
    with open(path) as f:
        row = [ln for ln in f.read().splitlines() if ln.strip()][-1]
    cols = row.split()
    currents = [float(c) for c in cols[1::2]]
    if len(currents) < len(supplies):
        raise SystemExit(
            f"{path}: {len(currents)} current column(s) but CONFIG lists "
            f"{len(supplies)} supplies {[s[0] for s in supplies]} -- the "
            f"deck's `wrdata` line and SUPPLIES disagree. Fix one; do not "
            f"report a partial sum as the design's power.")
    return sum(abs(i) * v for i, (_, v) in zip(currents, supplies)) * 1000.0


# Which raw file carries which key. This exists so the table can tell three
# different failures apart, because they print identically otherwise and have
# nothing in common but the words "no data":
#   - the testbench genuinely cannot measure the key  (#4a's hard stop)
#   - the run produced no file, or wrote it somewhere else  (a sim/CWD problem)
#   - the analysis ran and the key has no value in it  (e.g. no 0 dB crossing)
KEY_SOURCE = {"Gain": "AC", "UGBW": "AC", "PM": "AC", "Power": "OP"}
_RAW_FOR = {"AC": AC_RAW, "OP": OP_RAW}


def no_data_reason(key):
    """Why `key` has no number -- the actionable half of a NO DATA row."""
    which = KEY_SOURCE.get(key)
    if which is None:
        return ("this script has no measurement for the key -- add it to "
                "measure() and UNITS (schematic-agent #4c)")
    raw = _RAW_FOR[which]
    if not os.path.isfile(raw):
        return ("no simulation output at %s -- did the run fail, or write "
                "somewhere else?" % os.path.relpath(raw, _HERE))
    return ("the analysis ran but yielded no value for this key -- e.g. no "
            "0 dB crossing inside the swept band")


def measure():
    """Every key this design's testbench was built to make measurable.
    A key absent here prints NO DATA below."""
    sim = {}
    if os.path.isfile(AC_RAW):
        g, u, p = ac_metrics(AC_RAW)
        sim["Gain"] = g
        # ac_metrics returns Hz; target_spec.json's UGBW is in MHz. Convert
        # before comparing -- backwards makes a healthy circuit look six
        # orders of magnitude off, and the other way makes anything pass.
        sim["UGBW"] = None if u is None else u / 1e6
        sim["PM"] = p
    if os.path.isfile(OP_RAW):
        sim["Power"] = op_power_mw(OP_RAW, SUPPLIES)
    return sim


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", default=_HERE)
    ap.add_argument("--no-plot", action="store_true")
    args = ap.parse_args()

    with open(TARGET) as f:
        target = json.load(f)
    sim = measure()

    print("=== %s: simulated vs target ===\n" % DESIGN)
    print("  %-8s %12s %12s  %-8s %s"
          % ("spec", "target", "sim", "unit", "verdict"))
    all_met = True
    for key, entry in target.items():
        # Direction and unit come from the spec file when it states them
        # (design-sheets-checker's spec_form_template.md); a flat numeric entry
        # predates that form and falls back to CEILING_METRICS / UNITS.
        direction, tval, units = parse_spec_entry(key, entry)
        sval = sim.get(key)
        unit = units or UNITS.get(key, "")
        shown = ("[%.4g, %.4g]" % tuple(tval)) if direction == "RANGE" else "%.4g" % tval
        if sval is None:
            print("  %-8s %12s %12s  %-8s NO DATA -- %s"
                  % (key, shown, "-", unit, no_data_reason(key)))
            all_met = False
            continue
        ceiling = direction == "CEILING"
        if direction == "RANGE":
            met = tval[0] <= sval <= tval[1]
            need = "within"
        else:
            met = sval <= tval if ceiling else sval >= tval
            need = "<=" if ceiling else ">="
        all_met = all_met and met
        print("  %-8s %12s %12.4g  %-8s %s (need sim %s target)"
              % (key, shown, sval, unit, "MET" if met else "NOT MET", need))
    print("\n" + ("ALL SPECS MET" if all_met else "NOT all specs met"))

    if not args.no_plot and os.path.isfile(AC_RAW):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        _, _names, cols, _ = parse_ngspice_ascii_raw(AC_RAW)
        freq, h = cols[0].real, cols[-1]
        fig, ax = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
        ax[0].semilogx(freq, 20 * np.log10(np.abs(h)))
        ax[0].axhline(0, color="k", lw=0.6, alpha=0.5)
        ax[0].set_ylabel("Magnitude (dB)")
        ax[0].grid(True, which="both", alpha=0.3)
        ax[1].semilogx(freq, np.angle(h, deg=True))
        ax[1].set_ylabel("Phase (deg)")
        ax[1].set_xlabel("frequency (Hz)")
        ax[1].grid(True, which="both", alpha=0.3)
        if sim.get("UGBW"):
            for a in ax:
                a.axvline(sim["UGBW"] * 1e6, color="r", ls="--", lw=0.8)
            ax[0].set_title("UGBW = %.3f MHz, PM = %.1f deg"
                            % (sim["UGBW"], sim.get("PM") or float("nan")))
        fig.suptitle("%s pre-layout AC response" % DESIGN)
        png = os.path.join(args.out_dir, DESIGN + "_pre_ac.png")
        fig.tight_layout()
        fig.savefig(png, dpi=130)
        print("saved plot: " + png)


if __name__ == "__main__":
    main()
