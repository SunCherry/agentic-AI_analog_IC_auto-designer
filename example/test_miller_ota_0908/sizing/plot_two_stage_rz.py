#!/usr/bin/env python3
"""Plot script for two_stage_rz -- GENERATED, do not hand-edit.

Written by `generate_plot_script.py` from this design's own files. Bakes in
this design's analysis, spec file, title and output name; the shared rendering
engine is imported from `plot_results.py`, so a rendering fix reaches this
design without regenerating. The converged raw file (and an optional
`--compare` seed raw) are runtime arguments.

Usage:
  python plot_two_stage_rz.py <converged_raw.out> [--compare <seed_raw.out>] [--out <png>]
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# The shared rendering engine, found by walking up to the skills tree.
for _up in range(0, 7):
    _c = os.path.normpath(os.path.join(
        _HERE, *([".."] * _up), ".claude", "skills", "schematic-sizing", "script"))
    if os.path.isfile(os.path.join(_c, "plot_results.py")):
        sys.path.insert(0, _c)
        break
else:
    raise SystemExit("cannot find plot_results.py -- is this file still "
                     "inside the project tree?")
from plot_results import plot_ac, plot_generic, _spec_keys  # noqa: E402

ANALYSIS = 'ac'
SPEC     = os.path.join(os.path.normpath(os.path.join(_HERE, "..")),
                        "spec", "target_spec.json")
OUT      = os.path.join(_HERE, 'two_stage_rz_ac.png')
TITLE    = 'two_stage_rz -- converged'


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("raw", help="the converged ngspice ASCII raw file")
    ap.add_argument("--compare", default=None,
                    help="a second raw file to overlay (e.g. the seed iteration)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    paths = [args.raw] + ([args.compare] if args.compare else [])
    labels = [os.path.basename(p) for p in paths]
    spec = _spec_keys(SPEC)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    if ANALYSIS == "ac":
        out = plot_ac(paths, labels, args.out, spec, TITLE)
    else:
        out = plot_generic(paths, labels, args.out, TITLE)
    print("Wrote " + out)


if __name__ == "__main__":
    main()
