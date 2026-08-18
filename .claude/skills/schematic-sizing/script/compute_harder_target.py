#!/usr/bin/env python3
"""Compute a "harder" target_spec.json for the tuning loop to actually tune
against -- every value scaled by `(1 + margin)` (default 10 percentage
points) -- see ../SKILL.md's "Harder target".

Net parasitics are highly layout-dependent and cannot be predicted before a
layout exists, and this loop tunes against the ideal schematic, where they do
not exist at all. A netlist that lands exactly on its target here has nothing
left to lose to them. So the loop deliberately over-shoots from its very first
iteration, building the margin in up front rather than discovering the
shortfall once a layout is drawn.

**The same multiplication works uniformly for BOTH floor and ceiling
metrics, no branching needed** -- because of how each direction's own
comparison already works (see `CEILING_METRICS` in
run_sizing_iteration.py): a HIGHER floor-metric target (Gain, UGBW, PM)
is STRICTER (the loop must clear a bigger number); a HIGHER ceiling-
metric target (Power) is LOOSER (the loop has more room under a bigger
ceiling). Scaling every value up by the same factor therefore makes
every floor spec harder and every ceiling spec easier, automatically,
without this script needing to know or care which is which -- it's a
direct, intentional consequence of `(1 + margin)` always being > 1, not
a coincidence.

Concretely, at the default 10% margin: a floor key at `40` becomes `44`
(stricter -- the loop must now clear 44, not 40), and a ceiling key at
`0.5813` becomes `0.63943` (relaxed -- ~0.639 to work with, not 0.5813).
Relaxing the ceiling is the point, not a side effect: it is the headroom the
loop spends buying the harder floor margin.

Usage:
  python compute_harder_target.py target_spec.json -o harder_target_spec.json [--margin 0.10]
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from run_sizing_iteration import parse_spec_entry  # noqa: E402

DEFAULT_MARGIN = 0.10


def compute_harder_target(target_spec, margin=DEFAULT_MARGIN):
    """Returns a new spec, same keys and SAME FORM as the one handed in,
    with every bar moved by `margin`.

    - **FLOOR / CEILING** -- the value is scaled by `(1 + margin)`. One
      uniform multiplication does both directions: a higher floor is
      stricter, a higher ceiling is more allowance. See this module's own
      docstring for why that asymmetry is deliberate.
    - **RANGE** -- scaling makes no sense on a two-sided spec (it would
      slide the whole interval), so each bound moves INWARD by `margin` of
      the interval's own width: `[50, 70]` at 10% becomes `[52, 68]`. That
      is what "stricter" means for a range -- a narrower interval.

    A structured entry comes back structured, keeping its `Direction` and
    `Units`; a flat numeric entry comes back flat. The harder spec is read
    by the same `check_target()` as the original, so it has to be the same
    shape."""
    out = {}
    for key, entry in target_spec.items():
        direction, value, _units = parse_spec_entry(key, entry)
        if direction == "RANGE":
            lo, hi = value
            # EACH bound moves inward by `margin` of the interval's width --
            # not half of it. `[50, 70]` at 10% gives `[52, 68]`.
            #
            # The `/2` this used to carry read the rule as "narrow the interval
            # BY margin" (total width x 0.9), which halved the margin against
            # what this module's docstring and ../SKILL.md both state, and against
            # the FLOOR/CEILING case beside it, where the bar itself moves by
            # `margin x value` rather than half of it.
            step = (hi - lo) * margin
            new_value = [lo + step, hi - step]
            if new_value[0] >= new_value[1]:
                raise SystemExit(
                    f"{key}: a {margin:.0%} margin collapses the range "
                    f"{value} to {new_value} -- no interval left to tune "
                    f"into. Lower --margin, or widen the spec.")
        else:
            new_value = value * (1 + margin)
        if isinstance(entry, dict):
            out[key] = dict(entry)
            out[key]["Value"] = new_value
        else:
            out[key] = new_value
    return out


def format_report(target_spec, harder, margin):
    lines = [f"=== harder target_spec (margin={margin:.0%}) ===", ""]
    _WHY = {
        "CEILING": "relaxed (ceiling -- more allowance)",
        "FLOOR": "stricter (floor -- higher bar)",
        "RANGE": "stricter (range -- narrower interval)",
    }
    for key in harder:
        direction, was, _u = parse_spec_entry(key, target_spec[key])
        _d, now, _u2 = parse_spec_entry(key, harder[key])
        fmt = (lambda v: f"[{v[0]:.4g}, {v[1]:.4g}]") if direction == "RANGE" \
            else (lambda v: f"{v:.4g}")
        lines.append(f"{key}: {fmt(was)} -> {fmt(now)}  ({_WHY[direction]})")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target_spec")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN)
    args = ap.parse_args()

    target_spec = json.load(open(args.target_spec))
    harder = compute_harder_target(target_spec, margin=args.margin)
    print(format_report(target_spec, harder, args.margin))

    with open(args.output, "w") as f:
        json.dump(harder, f, indent=2)
    print(f"\nSaved: {args.output}")


if __name__ == "__main__":
    main()
