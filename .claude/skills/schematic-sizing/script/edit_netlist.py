#!/usr/bin/env python3
"""Read and write a sizing netlist's tunable values, through the circuit read's
own tunable registry and Jinja2 template. A thin adapter over `tunables.py`.

WHAT CHANGED, AND WHY. This module used to own `structure_groups.json` -- its
own detection of which devices share a variable, written at Step 1 and read by
every step after. That was a SECOND answer to a question
`circuit_decomposition.yaml` already answers, produced by a different detector,
and the two drifted: on `test_miller_ota` the circuit read had the nfet mirror
as one group (`tied: [w, l]`, `ratio_carrier: m`) while the JSON gave
`XMN3`/`XMN4`/`XMN5` six independent variables. Sizing believed the JSON and
produced three different unit devices on one gate net -- not a mirror, and not
layout-able as one.

The registry and the template now come from `circuit-decomposition`, which
derives them from `pattern-table.md`'s own rules. **Tying is structural:**
devices that share a parameter share the `{{ VAR }}` in the template, so a
group is rewritten from one value on every render and cannot come apart.

WHAT IS UNCHANGED: **the netlist is still the state.** No value store sits
beside it. `read_values()` reports what the `.sp` says now; `apply_values()`
merges a change into that and re-renders.

Usage (standalone -- the loop calls these as functions):
  python edit_netlist.py <netlist.sp> --design-dir <design_dir>
  python edit_netlist.py <netlist.sp> --design-dir <d> --set MN1_W=45.2 --apply
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import tunables as _T  # noqa: E402
from tunables import (  # noqa: F401,E402  -- re-exported, same names as before
    TunablesError, fmt as _fmt, read_values, render, seeds,
)


def load_groups(where):
    """`(registry, template_path)` for a design.

    `where` is the design dir, or `circuit_decomposition.yaml` itself. The old
    signature returned `(groups, fixed)`; `fixed` is gone -- a mirror
    reference's `m` is no longer pinned by a side-car, it is simply a variable
    the template renders like any other."""
    return _T.load(where)


def check_groups(netlist_text, registry):
    """Variables whose members disagree. Empty for anything rendered from the
    template; non-empty means the `.sp` was hand-edited off it."""
    return _T.check_consistency(netlist_text, registry)


def apply_values(netlist_text, values, registry, template_path):
    """Write `{var: value}` into the netlist by RE-RENDERING the template.
    Returns `(new_text, changes)` with `changes` as `[(var, old, new)]`.

    Every member of a variable is rewritten from one value, always -- a caller
    cannot set one half of a matched pair, because it never addresses a device.
    Raises KeyError on an unknown variable, so a typo costs an error rather
    than an iteration that silently measured no change."""
    current = _T.read_values(netlist_text, registry)
    merged = dict(_T.seeds(registry))
    merged.update(current)

    changes = []
    for var, val in values.items():
        if var not in registry:
            raise KeyError(
                "no such tunable: %s -- known variables are %s (from "
                "circuit_decomposition.yaml's tunable_parameters)"
                % (var, ", ".join(sorted(registry))))
        old, new = merged.get(var), float(val)
        if old is None or abs(float(old) - new) > 1e-12:
            changes.append((var, old, new))
        merged[var] = new

    missing = [v for v in registry if v not in merged]
    if missing:
        raise TunablesError(
            "no value for %s -- the netlist does not carry them and they have "
            "no seed. Re-run circuit-decomposition." % ", ".join(sorted(missing)))
    return _T.render(template_path, merged, registry), changes


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist")
    ap.add_argument("--design-dir", required=True,
                    help="design dir holding circuit_decomposition.yaml")
    ap.add_argument("--set", action="append", default=[], metavar="VAR=VALUE")
    ap.add_argument("--apply", action="store_true", help="write the netlist back")
    args = ap.parse_args()

    registry, template = load_groups(args.design_dir)
    text = open(args.netlist).read()

    if not args.set:
        for var, val in sorted(read_values(text, registry).items()):
            print(f"  {var:12s} = {val:g}")
        for var, seen in check_groups(text, registry):
            print(f"  INCONSISTENT {var}: {seen}")
        return

    updates = {}
    for item in args.set:
        var, _, val = item.partition("=")
        updates[var.strip()] = float(val)
    new_text, changes = apply_values(text, updates, registry, template)
    for var, old, new in changes:
        print(f"  {var}: {old} -> {new}")
    if args.apply:
        open(args.netlist, "w").write(new_text)
        print(f"wrote {args.netlist}")
    else:
        print("(dry run -- pass --apply to write)")


if __name__ == "__main__":
    main()
