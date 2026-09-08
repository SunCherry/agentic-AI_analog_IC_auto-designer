#!/usr/bin/env python3
"""Per-design sizing runner for test_vco (cross-coupled LC VCO, sky130A).

HAND-WRITTEN, replacing generate_sizing_runner.py's output (kept beside this
file as run_sizing_vco_lc.py.generated-dead for the record). The generated
runner imports run_iteration() from
.claude/skills/schematic-sizing/script/run_sizing_iteration.py, and that engine
RAISES on this deck before it can measure anything -- three independent
failures, all verified by direct call, none of them fixable without editing a
mandatory user input:

  1. generate_op_probe.detect_hier_prefix() requires the top-level instance to
     be named literally `x<wrapper>` (`xvco_core`); this deck names it `xvco`.
     run_sizing_iteration.py calls it with no override, so the documented
     `--hier-prefix` escape hatch is unreachable from the loop.
  2. augment_testbench() splices op-probe lines after a bare `run` line.
     This deck has none: it is an OSCILLATOR deck, and `op` plus three
     `tran 2p 60n` are .control-native interactive commands that execute
     immediately. Adding a `run` would change what is simulated.
  3. find_write_out() demands `v(` immediately after the .out filename; this
     deck writes `write ./simout_vco_lc_pre.out vdiff v(voutp) v(voutn)`.

This runner is NOT a fork of the engine. It reuses the skill's own
edit_netlist.py to write values through structure_groups.json (so a tie group
cannot desynchronise), and it delegates all scoring to the design's own #4c
script. What it replaces is only the MEASUREMENT TRANSPORT: instead of splicing
probes into the deck and parsing a raw file, it runs the deck exactly as the
user wrote it and reads the four numbers the deck itself measured with
`meas tran`.

CONSEQUENCE, stated rather than hidden: there is NO per-device op-point data
this run. generate_op_probe.py is the only producer of it and it cannot run
here. Reasoning falls back to spec deltas plus the analytic tank model in
spec_analysis.log.

Usage:
  python run_sizing_vco_lc.py --iter 3 --set C0_W=10,C0_L=10,C1_W=10,C1_L=10
  python run_sizing_vco_lc.py --iter 3 --show          # just print current values
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DESIGN_DIR = os.path.dirname(HERE)
REPO = os.path.abspath(os.path.join(DESIGN_DIR, os.pardir, os.pardir))
sys.path.insert(0, os.path.join(REPO, ".claude", "skills", "schematic-sizing", "script"))

# ---------------------------------------------------------------- CONFIG
DESIGN        = "vco_lc"
TUNING_SP     = os.path.join(HERE, "vco_lc_tuning.sp")
GROUPS_JSON   = os.path.join(HERE, "structure_groups.json")
FILED_DECK    = os.path.join(DESIGN_DIR, "testbench", "vco_lc_pre.spice")
SCORE_SCRIPT  = os.path.join(DESIGN_DIR, "process_vco_lc_results.py")
REAL_SPEC     = os.path.join(DESIGN_DIR, "spec", "target_spec.json")
HARDER_SPEC   = os.path.join(HERE, "harder_target_spec.json")
RUN_DIR       = os.path.join(HERE, "run")
# UNRESOLVED #2 from generate_sizing_runner.py, resolved BY HAND as its report
# instructs. Its auto-detection failed because it looks for a MOS whose SOURCE
# sits on a candidate rail; this is an all-NMOS design whose load to VDD is
# passive (two spiral inductors and two MiM caps), so no MOS source touches
# VDD. The deck itself measures supply current directly as AVG(i(vdd)) and
# forms Power = |iavg| * v(VDD), so nothing here needs to recompute it.
SUPPLY_NETS   = ["VDD"]
VDD           = 1.8
# TG3 from circuit_decomposition.yaml: XC0 and XC1 must share one w/l.
# setup_sizing.py did NOT group them (it emitted C0_* and C1_* separately), so
# the tie is enforced HERE and checked on every call -- SKILL.md Step 1's
# "read on mismatch, not only on detection".
ENFORCED_TIES = [("C0_W", "C1_W"), ("C0_L", "C1_L")]
# sky130_fd_pr__cap_mim_m3_1 is a plain .subckt with NO binning, so nothing in
# the PDK will refuse an undrawable plate. This is the only guard there is.
MIM_MIN_UM    = 2.0


def load_values():
    from edit_netlist import read_values, load_groups
    groups, _fixed = load_groups(GROUPS_JSON)
    return read_values(open(TUNING_SP).read(), groups)


def write_values(sets):
    """Write through structure_groups.json so a tie group cannot desynchronise."""
    from edit_netlist import apply_values, load_groups, check_groups
    groups, fixed = load_groups(GROUPS_JSON)
    text = open(TUNING_SP).read()
    vals = dict(load_values())
    vals.update(sets)
    new_text, changes = apply_values(text, vals, groups, fixed)
    open(TUNING_SP, "w").write(new_text)
    desync = check_groups(new_text, groups)
    if desync:
        raise SystemExit("group desync after write: %s" % (desync,))
    return changes


def check_ties(vals):
    bad = []
    for a, b in ENFORCED_TIES:
        if abs(float(vals[a]) - float(vals[b])) > 1e-9:
            bad.append("%s=%s but %s=%s (TG3 requires them equal)" % (a, vals[a], b, vals[b]))
    for k in ("C0_W", "C0_L", "C1_W", "C1_L"):
        if float(vals[k]) < MIM_MIN_UM:
            bad.append("%s=%s um is below the %s um drawable-plate floor" % (k, vals[k], MIM_MIN_UM))
    return bad


def run_deck():
    """Run the filed deck VERBATIM against the tuning netlist.

    The only edit is the `.include` target -- the deck is a mandatory user
    input and is never otherwise modified, and testbench/ is never written to.
    Returns (rc, results_path, stdout_tail).
    """
    if os.path.isdir(RUN_DIR):
        shutil.rmtree(RUN_DIR)
    os.makedirs(RUN_DIR)
    deck = open(FILED_DECK).read()
    # LINE-ANCHORED and MULTILINE on purpose. A bare r'(\.include\s+")[^"]*"'
    # matches intake's own header COMMENT first -- that comment quotes the
    # include line it re-pointed -- and silently rewrites the comment while
    # leaving the real statement alone. Comment lines start with '*', so
    # requiring '.include' at the start of a line excludes them.
    deck, n = re.subn(r'(?m)^([ \t]*\.include\s+")[^"]*"',
                      r'\1../vco_lc_tuning.sp"', deck, count=1)
    if n != 1:
        raise SystemExit("could not re-point the deck's .include (found %d)" % n)
    deck_path = os.path.join(RUN_DIR, "vco_lc_pre.spice")
    open(deck_path, "w").write(deck)
    p = subprocess.run(["ngspice", "-b", "vco_lc_pre.spice"], cwd=RUN_DIR,
                       capture_output=True, text=True, timeout=900)
    log = os.path.join(RUN_DIR, "ngspice.log")
    open(log, "w").write(p.stdout + "\n---stderr---\n" + p.stderr)
    return p.returncode, os.path.join(RUN_DIR, "vco_lc_pre_results.txt"), p.stdout[-1500:]


def score(results_path, spec_path, label, json_out=None):
    cmd = [sys.executable, SCORE_SCRIPT, "--results", results_path, "--spec", spec_path]
    if json_out:
        cmd += ["--json", json_out]
    p = subprocess.run(cmd, capture_output=True, text=True)
    print("\n---------- scored against %s ----------" % label)
    print(p.stdout.rstrip())
    if p.stderr.strip():
        print(p.stderr.rstrip())
    return p.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iter", type=int, required=True)
    ap.add_argument("--set", dest="sets", default="",
                    help="comma-separated VAR=value, microns (e.g. C0_W=10,C0_L=10)")
    ap.add_argument("--show", action="store_true", help="print values and exit")
    args = ap.parse_args()

    before = dict(load_values())
    if args.show:
        print(json.dumps(before, indent=2))
        return 0

    sets = {}
    for tok in filter(None, (t.strip() for t in args.sets.split(","))):
        k, v = tok.split("=", 1)
        sets[k.strip()] = float(v)
    unknown = [k for k in sets if k not in before]
    if unknown:
        raise SystemExit("unknown variable(s): %s\nknown: %s"
                         % (", ".join(unknown), ", ".join(sorted(before))))
    if sets:
        write_values(sets)

    after = dict(load_values())
    bad = check_ties(after)
    if bad:
        write_values({k: before[k] for k in sets})     # roll back, leave no half-edit
        raise SystemExit("REFUSED (rolled back):\n  " + "\n  ".join(bad))

    changed = {k: (before[k], after[k]) for k in after if before[k] != after[k]}
    print("=" * 78)
    print("ITER %d -- %s" % (args.iter, DESIGN))
    print("Changed: " + (", ".join("%s %s -> %s" % (k, o, n) for k, (o, n) in sorted(changed.items()))
                         or "(nothing -- re-measuring current state)"))
    print("=" * 78)

    rc, results_path, tail = run_deck()
    if rc != 0 or not os.path.exists(results_path):
        print("ngspice rc=%d, results file %s" % (rc, "missing" if not os.path.exists(results_path) else "present"))
        print(tail)
        return 3

    dbg = os.path.join(HERE, "debug", "iter_%d" % args.iter)
    os.makedirs(dbg, exist_ok=True)
    shutil.copy(results_path, os.path.join(dbg, "vco_lc_pre_results.txt"))
    shutil.copy(TUNING_SP, os.path.join(dbg, "vco_lc_tuning.sp"))

    rc_real = score(results_path, REAL_SPEC, "target_spec.json (THE REAL SPEC -- must be met)",
                    os.path.join(dbg, "score_real.json"))
    rc_hard = score(results_path, HARDER_SPEC, "harder_target_spec.json (the aim)",
                    os.path.join(dbg, "score_harder.json"))

    print("\nREAL   exit=%d  (0=all met, 1=a key missed, 2=NO DATA / did not oscillate)" % rc_real)
    print("HARDER exit=%d" % rc_hard)
    print("NOTE: for a CEILING key the harder bar is LOOSER than the real bar "
          "(Power <=3.3 vs <=3.0 mW), so 'all harder met' does NOT imply "
          "'all real met'. The REAL spec governs.")
    print("op-point: NONE -- generate_op_probe.py cannot splice this deck (see module docstring).")
    return 0 if rc_real == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
