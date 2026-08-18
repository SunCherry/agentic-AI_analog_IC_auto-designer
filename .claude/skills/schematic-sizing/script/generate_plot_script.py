#!/usr/bin/env python3
"""Generate this design's own plot script, only when a figure is wanted.

Run at `../SKILL.md`'s Step 3, and ONLY when the design actually needs a
figure -- `spec_analysis.md` calls for one, or the user asks. Plotting is
imperative, not a predefined step: this generator is the shared half, and it
writes

    <design_dir>/sizing/plot_<design_name>.py

a small, per-design script that bakes in this design's analysis, spec file,
title and output name, and imports the shared rendering engine
(`plot_results.py`) -- the same split as `generate_sizing_runner.py` and
`run_sizing_iteration.py`.

WHY GENERATE, rather than call `plot_results.py` with flags each time:

  * The facts are DERIVED, not remembered -- which analysis the deck runs,
    where `target_spec.json` lives, what to name the figure, what title to
    draw. Resolved once, in writing, beside the design it describes, so a
    later plot of the same design is one command, not a flag list that goes
    stale.
  * The generated file is the record of what was plotted with what title.
  * WHAT STAYS SHARED: the rendering. The generated script adds no drawing
    logic of its own, so a rendering fix reaches every design without
    regenerating anything.

The converged raw file is a RUNTIME argument, not a baked fact -- it depends
on which iteration converged, so it is passed when the script runs (see the
generated file's usage).

Usage:
  python generate_plot_script.py <design_dir> --netlist <design_dir>/netlist/<design_name>.sp
      [--analysis ac] [--title "..."] [--force]
"""

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

_TEMPLATE = '''#!/usr/bin/env python3
"""Plot script for {design_name} -- GENERATED, do not hand-edit.

Written by `generate_plot_script.py` from this design's own files. Bakes in
this design's analysis, spec file, title and output name; the shared rendering
engine is imported from `plot_results.py`, so a rendering fix reaches this
design without regenerating. The converged raw file (and an optional
`--compare` seed raw) are runtime arguments.

Usage:
  python {plot_name} <converged_raw.out> [--compare <seed_raw.out>] [--out <png>]
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

ANALYSIS = {analysis!r}
SPEC     = os.path.join(os.path.normpath(os.path.join(_HERE, "..")),
                        "spec", "target_spec.json")
OUT      = os.path.join(_HERE, {out_name!r})
TITLE    = {title!r}


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
'''


def render(design_name, analysis, out_name, title):
    return _TEMPLATE.format(
        design_name=design_name,
        plot_name="plot_%s.py" % design_name,
        analysis=analysis,
        out_name=out_name,
        title=title,
    )


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design_dir")
    ap.add_argument("--netlist", required=True,
                    help="the design's netlist (only its basename is used for "
                         "the script and figure names)")
    ap.add_argument("--analysis", default="ac",
                    help="which extractor interprets the raw file; 'ac' draws "
                         "a Bode pair, anything else the saved signal vs its "
                         "sweep. Defaults to 'ac'.")
    ap.add_argument("--title", default=None,
                    help="figure title. Defaults to '<design_name> -- converged'.")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing plot script")
    args = ap.parse_args()

    if not os.path.isfile(args.netlist):
        raise SystemExit("netlist does not exist: " + args.netlist)

    design_name = os.path.splitext(os.path.basename(args.netlist))[0]
    analysis = args.analysis
    out_name = "%s_%s.png" % (design_name, analysis)
    title = args.title or ("%s -- converged" % design_name)

    sizing_dir = os.path.join(args.design_dir, "sizing")
    os.makedirs(sizing_dir, exist_ok=True)
    plot_path = os.path.join(sizing_dir, "plot_%s.py" % design_name)

    print("=== plot facts for this design ===")
    print("  analysis      %s" % analysis)
    print("  spec          spec/target_spec.json (convention)")
    print("  output        %s" % out_name)
    print("  title         %r" % title)

    if os.path.isfile(plot_path) and not args.force:
        raise SystemExit(
            "\n%s already exists. This refuses to overwrite it -- pass --force "
            "once you have confirmed it carries no hand edits." % plot_path)

    with open(plot_path, "w") as f:
        f.write(render(design_name, analysis, out_name, title))
    os.chmod(plot_path, 0o755)
    print("\nWROTE  %s" % plot_path)
    print("Run it with the converged raw file:")
    print("  python %s <converged_raw.out> [--compare <seed_raw.out>]"
          % plot_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
