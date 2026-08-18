#!/usr/bin/env python3
"""Promote the loop's working netlist to the hand-off -- see ../SKILL.md's
"Step 3: Final netlist".

**This is a promotion, not a render.** The tuning netlist already holds real
`W`/`L` values, because the loop wrote them there; nothing is assembled from a
template and a value file. What this step adds is the three things a hand-off
needs and a working file does not:

  1. every tunable rounded to `ROUND_NDIGITS` (2) decimal places -- the loop
     lands on whatever precision its last `--set` specified, which is not a
     layout-practical number. `_M` is a count of parallel unit devices, so it
     rounds to the nearest integer, floored at 1.
  2. a header recording what it came from and the `nf` disposition, so the file
     is traceable back to the run.
  3. a WIDTH BREAKDOWN comment block, per MOS instance.

**`nf` is carried through, not chosen.** With no `--nf`, every device keeps the
finger count the netlist already carries -- the sizing loop does not tune finger
count, and promoting it unchanged is what that means at the output. `--nf N`
overwrites `nf=` on EVERY MOS line with one value, and belongs to a caller that
has actually measured one.

**Why the width breakdown is spelled out.** `w=` here is the TOTAL width of ONE
of the device's `m` parallel copies, and is Nf-invariant -- `nf` splits that
width into fingers, it never adds any. A bare `w`+`m`+`nf` triple invites a
real, physically-wrong misreading (multiplying `w` by `nf` on top), so the
header states the per-multiplier, per-finger and true total width outright.

Usage:
  python finalize_netlist.py <design>_tuning.sp -o <design>_final.sp
      --groups structure_groups.json  [--nf N]
"""
import argparse
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from netlist_devices import parse_devices  # noqa: E402
from edit_netlist import (load_groups, read_values, apply_values,  # noqa: E402
                          check_groups)

ROUND_NDIGITS = 2


def round_tunables(values, ndigits=ROUND_NDIGITS):
    """Round every tunable to `ndigits` decimals -- except an `_M`, which is a
    physical count of parallel unit devices and rounds to the nearest integer,
    floored at 1 (a zero- or negative-copy device is meaningless)."""
    return {k: (max(1, round(float(v))) if k.endswith("_M")
                else round(float(v), ndigits))
            for k, v in values.items()}


def inject_nf(netlist_text, nf):
    """Add/overwrite an explicit nf=<value> on every MOS device line."""
    lines = netlist_text.splitlines(keepends=True)
    for d in parse_devices(netlist_text):
        if d["kind"] != "mos":
            continue
        idx = d["lineno"] - 1
        line = lines[idx]
        if any(k.lower() == "nf" for k in d["params"]):
            lines[idx] = re.sub(r'(?i)\bnf\s*=\s*\S+', f"nf={nf:g}", line, count=1)
        else:
            lines[idx] = line.rstrip("\n") + f" nf={nf:g}\n"
    return "".join(lines)


def compute_width_breakdown(final_text):
    """{instance: {width_per_multiplier_um, m, nf, width_per_finger_um,
    total_width_um}} from the FINAL netlist text. `total_width_um` = `w * m`,
    Nf-invariant."""
    out = {}
    for d in parse_devices(final_text):
        if d["kind"] != "mos":
            continue
        params = {k.lower(): v for k, v in d["params"].items()}
        if "w" not in params:
            continue
        w = float(params["w"])
        m = float(params.get("m", 1.0))
        nf = float(params.get("nf", 1.0))
        out[d["name"]] = {
            "width_per_multiplier_um": w, "m": m, "nf": nf,
            "width_per_finger_um": round(w / nf, ROUND_NDIGITS),
            "total_width_um": round(w * m, ROUND_NDIGITS),
        }
    return out


def _header(source_name, nf_used, breakdown):
    lines = [
        "* Auto-generated FINAL netlist -- the sizing hand-off.",
        f"*   promoted from: {source_name}",
        "*   finger count:  " + ("as the netlist carries it (not chosen here)"
                                 if nf_used is None
                                 else f"nf={nf_used:g} (explicit --nf)"),
        f"*   tunables rounded to {ROUND_NDIGITS} decimal places"
        " (_M to the nearest integer)",
        "* Do not hand-edit -- re-promote from the tuning netlist instead.",
        "*",
        "* WIDTH BREAKDOWN -- `w=` is the total width of ONE of the device's `m`",
        "* copies and is Nf-invariant: `nf` splits that width into fingers, it",
        "* never adds any. Do NOT multiply w by nf.",
        f"*   {'device':<8} {'w/copy':>9} {'m':>4} {'nf':>4} {'w/finger':>9} {'TOTAL w':>10}",
    ]
    for name, b in breakdown.items():
        lines.append(f"*   {name:<8} {b['width_per_multiplier_um']:>9g} "
                     f"{b['m']:>4g} {b['nf']:>4g} {b['width_per_finger_um']:>9g} "
                     f"{b['total_width_um']:>10g}")
    return "\n".join(lines) + "\n"


def finalize_netlist(tuning_path, groups_path, nf=None):
    """Return `(final_text, nf_used, rounded_values)`."""
    groups, fixed = load_groups(groups_path)
    text = open(tuning_path).read()

    desync = check_groups(text, groups)
    if desync:
        raise ValueError(
            "the tuning netlist has desynchronised groups, so it is not the "
            "design the variables describe: " +
            "; ".join(f"{v} ({', '.join(f'{k}={x:g}' for k, x in seen.items())})"
                      for v, seen in desync))

    rounded = round_tunables(read_values(text, groups))
    text, _ = apply_values(text, rounded, groups, fixed)
    if nf is not None:
        text = inject_nf(text, nf)
    breakdown = compute_width_breakdown(text)
    return _header(os.path.basename(tuning_path), nf, breakdown) + text, nf, rounded


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tuning_netlist", help="<design_name>_tuning.sp")
    ap.add_argument("--groups", required=True, help="structure_groups.json")
    ap.add_argument("-o", "--out", required=True,
                    help="<design_name>_final.sp -- the hand-off")
    ap.add_argument("--nf", type=int, default=None,
                    help="overwrite nf= on EVERY MOS line with this value. Omit "
                         "to carry each device's own nf through unchanged -- "
                         "what sizing does, since it never tunes finger count")
    args = ap.parse_args()

    try:
        final_text, nf_used, rounded = finalize_netlist(
            args.tuning_netlist, args.groups, nf=args.nf)
    except (ValueError, KeyError) as e:
        sys.exit(f"error: {e}")

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(final_text)
    print(f"Wrote {args.out} "
          + (f"(nf={nf_used})" if nf_used is not None
             else "(nf carried through from the netlist)"))
    print(f"Rounded {len(rounded)} tunables to {ROUND_NDIGITS} decimal places; "
          f"width breakdown written into the header.")


if __name__ == "__main__":
    main()
