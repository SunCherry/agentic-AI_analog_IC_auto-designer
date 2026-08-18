#!/usr/bin/env python3
"""Level the drawn extent of every MOS device in a shaped netlist.

The rule this implements, and nothing more: after Step 2 has chosen `nf` for
the FLAGGED devices, look at EVERY MOS device -- swept and pinned alike --
and fold any whose drawn extent is an outlier, so no single device comes out
of layout as a long thin sliver next to a floorplan of compact ones.

WHAT "LENGTH" MEANS HERE, because the netlist uses `l` for something else.
`l` is the CHANNEL length and folding cannot change it. What folding changes
is the device's drawn extent along the width axis:

    extent = w / nf          (the per-finger width)

`w` is the total width of ONE of the device's `m` copies and is nf-invariant
-- `nf` splits that width into fingers, it never adds any -- so raising `nf`
shrinks the extent exactly proportionally and leaves total width `w * m`,
the topology, and every electrical parameter alone. That invariance is what
makes this operation safe to apply after sizing has frozen W/L.

THE OUTLIER TEST is against the MEAN extent over all MOS devices, computed
ONCE from the pre-leveling netlist. A device is flagged when

    extent > factor * mean_extent        (factor default 3.0)

and folded to the `nf` whose extent `w / nf` lands NEAREST the mean, searched
rather than divided (see `_best_nf`: `round(w / mean)` answers a different
question and is measurably worse). Ties go to the smaller `nf`.

The mean is deliberately NOT recomputed as devices are folded -- a moving
target would let the order of processing change the answer, and would chase
its own tail as each fold pulls the mean down.

FOLDING ONLY EVER INCREASES `nf`. Un-folding a device to raise its extent
toward the mean would undo a finger count Step 2 measured, so the floor is
always the netlist's current value.

MATCHED DEVICES FOLD TOGETHER. A differential pair whose halves get
different finger counts is no longer a matched pair, and a mirror whose legs
do is no longer a ratio. When `--tie-groups` is given, a flag on any member
folds every member to the same `nf`.

PASSIVES ARE REPORTED, NEVER FOLDED. A resistor or capacitor has no `nf`;
its `l` is a real electrical parameter and changing it changes the component
value. They appear in the table for context and are never rewritten.

Usage:
  python level_device_lengths.py <shaped_netlist.sp>
      [--factor 3.0] [--tie-groups <circuit_decomposition.yaml>]
      [--powers-of-two] [--min-finger-width UM] [--apply] [--json PATH]

Without `--apply` it reports and writes nothing.
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "parasitic-estimation", "script"))

from estimate_parasitics import _MOS_RE  # noqa: E402

DEFAULT_FACTOR = 3.0
_PASSIVE_RE = re.compile(
    r"^(?P<name>[RCXrcx]\S*)\s+(?P<rest>.*\b(?:res|cap)\S*\s+.*)$", re.IGNORECASE
)


def _param(params, key):
    m = re.search(rf"\b{key}\s*=\s*([0-9.eE+-]+)", params, re.IGNORECASE)
    return float(m.group(1)) if m else None


def parse_mos(netlist_path):
    """[{name, w, l, nf, m, extent, lineno}] for every MOS line."""
    out = []
    with open(netlist_path) as f:
        for lineno, line in enumerate(f, 1):
            m = _MOS_RE.match(line.strip())
            if not m:
                continue
            params = m.group("params")
            w = _param(params, "w")
            if w is None:
                continue
            nf = int(_param(params, "nf") or 1)
            out.append({
                "name": m.group("name"),
                "w": w,
                "l": _param(params, "l"),
                "nf": nf,
                "m": int(_param(params, "m") or 1),
                "extent": w / nf,
                "lineno": lineno,
            })
    return out


def parse_passives(netlist_path):
    """[{name, l, w}] for resistor/cap lines -- reported, never folded."""
    out = []
    with open(netlist_path) as f:
        for line in f:
            s = line.strip()
            if _MOS_RE.match(s):
                continue
            m = _PASSIVE_RE.match(s)
            if not m:
                continue
            out.append({
                "name": m.group("name"),
                "l": _param(s, "l"),
                "w": _param(s, "w"),
            })
    return out


def load_tie_groups(yaml_path):
    """[[names...]] -- device sets that must share one nf."""
    try:
        import yaml
    except ImportError:
        print("  WARNING: pyyaml not available -- tie groups NOT enforced", file=sys.stderr)
        return []
    data = yaml.safe_load(open(yaml_path).read()) or {}
    groups = []
    for g in data.get("tie_groups") or []:
        names = g.get("devices") if isinstance(g, dict) else None
        if names and len(names) > 1:
            groups.append([str(n) for n in names])
    return groups


NF_SEARCH_MAX = 64


def _candidates(nf_floor, powers_of_two):
    """Finger counts to consider, from the device's current nf upward."""
    if powers_of_two:
        vals, p = [], 1
        while p <= NF_SEARCH_MAX:
            if p >= nf_floor:
                vals.append(p)
            p *= 2
        return vals or [nf_floor]
    return list(range(nf_floor, NF_SEARCH_MAX + 1))


def _best_nf(w, nf_floor, mean_extent, powers_of_two, min_finger_width):
    """The nf whose extent w/nf lands NEAREST the mean.

    Not `round(w / mean_extent)`: that picks the nf whose *ratio* rounds
    nearest, which is a different question and gives a worse answer whenever
    the ratio sits just under .5. For w=47.2 against a 32.5um mean it yields
    nf=1 (extent 47.2, off by 14.7) where nf=2 gives 23.6, off by only 8.9.
    Ties go to the SMALLER nf -- folding costs routing complexity, so buy no
    more of it than the distance actually improves.
    """
    best = nf_floor
    best_err = abs(w / nf_floor - mean_extent)
    for nf in _candidates(nf_floor, powers_of_two):
        if min_finger_width and w / nf < min_finger_width:
            continue
        err = abs(w / nf - mean_extent)
        if err < best_err - 1e-12:
            best, best_err = nf, err
    return best


def plan(devices, factor, tie_groups, powers_of_two, min_finger_width):
    """Decide each device's new nf. Returns (mean_extent, [decisions])."""
    mean_extent = sum(d["extent"] for d in devices) / len(devices)
    threshold = factor * mean_extent
    by_name = {d["name"]: d for d in devices}

    proposed = {}
    for d in devices:
        if d["extent"] <= threshold:
            continue
        proposed[d["name"]] = _best_nf(d["w"], d["nf"], mean_extent,
                                       powers_of_two, min_finger_width)

    # A flag on any tie-group member folds the whole group to one nf.
    for group in tie_groups:
        members = [n for n in group if n in by_name]
        hit = [n for n in members if n in proposed]
        if not hit:
            continue
        nf_group = max(proposed.get(n, by_name[n]["nf"]) for n in members)
        for n in members:
            proposed[n] = nf_group

    decisions = []
    for d in sorted(devices, key=lambda x: -x["extent"]):
        nf_new = proposed.get(d["name"], d["nf"])
        decisions.append({
            **d,
            "flagged": d["extent"] > threshold,
            "nf_new": nf_new,
            "extent_new": d["w"] / nf_new,
            "changed": nf_new != d["nf"],
            "tied_in": next((g for g in tie_groups if d["name"] in g), None),
        })
    return mean_extent, decisions


def apply_to_netlist(netlist_path, decisions):
    changes = {d["lineno"]: d for d in decisions if d["changed"]}
    if not changes:
        return 0
    with open(netlist_path) as f:
        lines = f.readlines()
    for lineno, d in changes.items():
        line = lines[lineno - 1]
        if re.search(r"\bnf\s*=", line, re.IGNORECASE):
            lines[lineno - 1] = re.sub(
                r"\b(nf\s*=\s*)\d+", rf"\g<1>{d['nf_new']}", line, count=1,
                flags=re.IGNORECASE)
        else:
            lines[lineno - 1] = line.rstrip("\n") + f" nf={d['nf_new']}\n"
    with open(netlist_path, "w") as f:
        f.writelines(lines)
    return len(changes)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist", help="the shaped netlist from Step 3")
    ap.add_argument("--factor", type=float, default=DEFAULT_FACTOR,
                    help=f"flag extent > factor * mean (default {DEFAULT_FACTOR})")
    ap.add_argument("--tie-groups", default=None,
                    help="circuit_decomposition.yaml -- matched devices fold together")
    ap.add_argument("--powers-of-two", action="store_true",
                    help="round nf up to a power of two")
    ap.add_argument("--min-finger-width", type=float, default=None,
                    help="never fold a finger narrower than this (um)")
    ap.add_argument("--apply", action="store_true",
                    help="rewrite the netlist; without it, report only")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    devices = parse_mos(args.netlist)
    if not devices:
        sys.exit(f"error: no MOS device lines found in {args.netlist}")

    tie_groups = load_tie_groups(args.tie_groups) if args.tie_groups else []
    mean_extent, decisions = plan(devices, args.factor, tie_groups,
                                  args.powers_of_two, args.min_finger_width)

    print(f"=== Device extent leveling: {os.path.basename(args.netlist)} ===")
    print(f"  extent = w/nf (per-finger width); mean over {len(devices)} MOS "
          f"device(s) = {mean_extent:.3f} um")
    print(f"  flag threshold = {args.factor} x mean = "
          f"{args.factor * mean_extent:.3f} um")
    if tie_groups:
        print(f"  tie groups enforced: {len(tie_groups)}")
    else:
        print("  tie groups: NONE given -- matched devices may fold apart "
              "(pass --tie-groups)")
    print()
    print(f"  {'device':<10} {'w':>9} {'nf':>4} {'extent':>9}  ->  "
          f"{'nf':>4} {'extent':>9}   note")
    for d in decisions:
        note = ""
        if d["flagged"]:
            note = f"FLAGGED ({d['extent'] / mean_extent:.1f}x mean)"
        elif d["changed"]:
            note = "folded with its tie group"
        print(f"  {d['name']:<10} {d['w']:>9.3f} {d['nf']:>4} "
              f"{d['extent']:>9.3f}  ->  {d['nf_new']:>4} "
              f"{d['extent_new']:>9.3f}   {note}")

    passives = parse_passives(args.netlist)
    if passives:
        print("\n  passives (no nf lever -- reported, never folded):")
        for p in passives:
            print(f"    {p['name']:<10} l={p['l']} w={p['w']}")

    changed = [d for d in decisions if d["changed"]]
    print()
    if not changed:
        print("  No device exceeds the threshold -- nothing to fold.")
    elif args.apply:
        n = apply_to_netlist(args.netlist, decisions)
        print(f"  APPLIED: rewrote nf on {n} device(s) in {args.netlist}")
        print("  Total width w*m is unchanged on every one of them.")
        print("  RE-SIMULATE: a folded device's parasitics were not measured "
              "by Step 2's sweep.")
    else:
        print(f"  {len(changed)} device(s) would be folded. Re-run with "
              f"--apply to write them.")

    if args.json:
        with open(args.json, "w") as f:
            json.dump({"netlist": args.netlist, "mean_extent_um": mean_extent,
                       "factor": args.factor, "devices": decisions}, f, indent=2)
        print(f"  wrote {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
