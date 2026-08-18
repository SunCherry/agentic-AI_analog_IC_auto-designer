#!/usr/bin/env python3
"""Read and write a sizing netlist's tunable values IN PLACE, through the
structure groups -- the replacement for the old `.j2` + `params_values.json`
pair. See ../SKILL.md's "Working folder".

**The netlist is the state.** There is no separate value store to drift out of
sync with it: `read_values()` reports what the `.sp` currently says, and
`apply_values()` writes back into the same file. A value that is in the netlist
is the value that simulates, always.

**Groups are what the netlist alone cannot say.** A plain `.sp` gives every
device its own `w=` token, so nothing stops one half of a matched pair being
sized without the other -- the exact symmetry break
`../set_tunable_params.md` exists to prevent. `structure_groups.json` records
which instances share a variable; setting that variable writes every member,
by construction. Written once at Step 1, and read by every step after it.

Group file shape (values live in the netlist, never here):

    {
      "groups": {
        "MP1_W": {"param": "w", "members": ["XMP1", "XMP2"]},
        "MP2_M": {"param": "m", "members": ["XMP2"]}
      },
      "fixed": {"XMN4": {"m": 1}}
    }

`fixed` is what must NOT move -- a mirror reference's `m`, pinned to the unit
it counts in.

Usage (standalone -- the loop calls these as functions):
  python edit_netlist.py <netlist.sp> --groups structure_groups.json
  python edit_netlist.py <netlist.sp> --groups g.json --set MN1_W=45.2 --apply
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from netlist_devices import parse_devices  # noqa: E402

sys.path.insert(0, os.path.join(_HERE, "..", "..", "parasitic-estimation", "script"))
from estimate_parasitics import _um_val  # noqa: E402


def load_groups(path):
    """`(groups, fixed)` from a structure_groups.json."""
    d = json.load(open(path))
    return d.get("groups", {}), d.get("fixed", {})


def _token_re(key):
    return re.compile(rf'(?i)(\b{re.escape(key)}\s*=\s*)(\S+)')


def _fmt(v):
    """A netlist reads `4`, not `4.0`; `m` is a count and must stay whole."""
    return f"{int(round(v))}" if abs(v - round(v)) < 1e-9 else f"{v:g}"


def read_values(netlist_text, groups):
    """`{var: value}` read out of the netlist itself, in MICRONS for `w`/`l`
    and as a plain count for `m`.

    A group's value is its FIRST member's -- members are kept identical by
    `apply_values()`, so any of them answers. A member that disagrees is
    reported by `check_groups()`, not silently averaged here."""
    by_name = {d["name"].lower(): d for d in parse_devices(netlist_text)}
    out = {}
    for var, spec in groups.items():
        param = spec["param"].lower()
        for inst in spec["members"]:
            d = by_name.get(inst.lower())
            if not d:
                continue
            raw = next((v for k, v in d["params"].items() if k.lower() == param), None)
            if raw is None:
                continue
            out[var] = float(raw) if param == "m" else _um_val(raw)
            break
    return out


def check_groups(netlist_text, groups):
    """Every group whose members do NOT already agree, as
    `[(var, {instance: value})]`. Empty when the netlist is consistent.

    Run after anything edits the netlist by hand: a desynchronised pair is the
    defect groups exist to prevent, and it is invisible in the `.sp` itself."""
    by_name = {d["name"].lower(): d for d in parse_devices(netlist_text)}
    bad = []
    for var, spec in groups.items():
        param = spec["param"].lower()
        seen = {}
        for inst in spec["members"]:
            d = by_name.get(inst.lower())
            if not d:
                continue
            raw = next((v for k, v in d["params"].items() if k.lower() == param), None)
            if raw is None:
                continue
            seen[inst] = float(raw) if param == "m" else _um_val(raw)
        if len(set(round(v, 9) for v in seen.values())) > 1:
            bad.append((var, seen))
    return bad


def apply_values(netlist_text, values, groups, fixed=None):
    """Write `{var: value}` into the netlist. Returns `(new_text, changes)`.

    **Every member of a group is written, always.** That is the whole point:
    a caller cannot set one half of a pair, because it never addresses a
    device -- it addresses the variable the pair shares.

    Raises KeyError on a variable no group defines, so a typo costs an error
    rather than an iteration that silently measured no change."""
    unknown = [v for v in values if v not in groups]
    if unknown:
        raise KeyError(
            "no such tunable: %s -- known variables are %s (see "
            "structure_groups.json)" % (", ".join(sorted(unknown)),
                                        ", ".join(sorted(groups))))
    lines = netlist_text.splitlines(keepends=True)
    index = {d["name"].lower(): d["lineno"] - 1 for d in parse_devices(netlist_text)}
    changes = []
    for var, val in values.items():
        spec = groups[var]
        param = spec["param"].lower()
        for inst in spec["members"]:
            idx = index.get(inst.lower())
            if idx is None:
                continue
            line = lines[idx]
            new_line, n = _token_re(param).subn(
                lambda m: f"{m.group(1)}{_fmt(val)}", line, count=1)
            if n == 0:      # the device carries no such token yet -- add it
                new_line = line.rstrip("\n") + f" {param}={_fmt(val)}\n"
            if new_line != line:
                lines[idx] = new_line
                changes.append(f"{inst}.{param} -> {_fmt(val)}")
    text = "".join(lines)
    if fixed:
        text = _reassert_fixed(text, fixed)
    return text, changes


def _reassert_fixed(netlist_text, fixed):
    """Pin every `fixed` token back to its stated value -- a mirror
    reference's `m` is the unit the family counts in, and a fold or a hand
    edit that moved it would rescale the whole family silently."""
    lines = netlist_text.splitlines(keepends=True)
    index = {d["name"].lower(): d["lineno"] - 1 for d in parse_devices(netlist_text)}
    for inst, params in fixed.items():
        idx = index.get(inst.lower())
        if idx is None:
            continue
        for param, val in params.items():
            line = lines[idx]
            new_line, n = _token_re(param).subn(
                lambda m: f"{m.group(1)}{_fmt(float(val))}", line, count=1)
            lines[idx] = new_line if n else (
                line.rstrip("\n") + f" {param}={_fmt(float(val))}\n")
    return "".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist")
    ap.add_argument("--groups", required=True)
    ap.add_argument("--set", default=None, help="comma-separated VAR=VALUE")
    ap.add_argument("--apply", action="store_true",
                    help="write the netlist back (default: report only)")
    args = ap.parse_args()

    groups, fixed = load_groups(args.groups)
    text = open(args.netlist).read()

    bad = check_groups(text, groups)
    if bad:
        print("DESYNCHRONISED groups -- members of one variable disagree:")
        for var, seen in bad:
            print(f"  {var}: " + ", ".join(f"{k}={v:g}" for k, v in seen.items()))
        print("  Set the variable to re-synchronise them.\n")

    if not args.set:
        print("current values (read from the netlist):")
        for var, val in sorted(read_values(text, groups).items()):
            print(f"  {var:12} {val:g}   <- {', '.join(groups[var]['members'])}")
        return 0

    values = {}
    for pair in args.set.split(","):
        k, _, v = pair.partition("=")
        if k and v:
            try:
                values[k.strip()] = float(v)
            except ValueError:
                raise SystemExit(f"--set {pair}: {v!r} is not a number "
                                 f"(units are microns; write 45, not 45u)")
    try:
        new_text, changes = apply_values(text, values, groups, fixed)
    except KeyError as e:
        raise SystemExit(str(e).strip('"'))
    for c in changes:
        print("  " + c)
    if args.apply:
        open(args.netlist, "w").write(new_text)
        print(f"\nwrote {args.netlist}")
    else:
        print("\nreport only -- pass --apply to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
