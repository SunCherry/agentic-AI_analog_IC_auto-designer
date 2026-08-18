#!/usr/bin/env python3
"""Write device-shaper's hand-off netlist into `<design_dir>/device_shaping/`.

Naming rule, and it applies to the TOP-LEVEL netlist only:

    <design_dir>/sizing/two_stage_rz_final.sp
      -> <design_dir>/device_shaping/two_stage_rz_final_shaped.sp

Sub-circuit netlists pulled in by `.include` are **not renamed**. They are
copied into that same `device_shaping/` folder under their ORIGINAL basename,
modified in place if they happen to hold a swept device. Only the top-level
file carries the `_shaped` marker, so "which netlist is the hand-off" has one
answer, while a sub-circuit keeps the identity every other file already refers
to it by.

Because every design-local file lands in one flat folder, each design-local
`.include` is rewritten to a BARE BASENAME -- it resolves as a sibling from the
new location. An include that points OUTSIDE the design tree (PDK models,
corner libs) is left pointing where it pointed, but a RELATIVE one is made
absolute first: the netlist has moved to a different directory, and a relative
PDK path that resolved from `sizing/` would silently fail to resolve from
`device_shaping/`.

This writes the chosen `nf` and nothing else. It does NOT carry the sweep's
parasitic annotation (`ad`/`as`/`pd`/`ps`/`nrd`/`nrs`, the series `Rg`) --
that annotation exists to MEASURE a finger count, and baking it into the
hand-off would hand layout a netlist whose parasitics are estimated twice.
`W`/`L`/`m` and every other parameter are passed through untouched.
"""
import argparse
import json
import os
import re
import shutil
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "parasitic-estimation", "script"))

from estimate_parasitics import _MOS_RE  # noqa: E402

SHAPED_SUFFIX = "_shaped"

# `.include "f"` / `.inc f` / `.lib f corner` -- group(1) is the directive,
# group(2) the (optionally quoted) path, group(3) whatever trails it.
_INCLUDE_RE = re.compile(
    r'^(\s*\.(?:include|inc|lib)\s+)(["\']?)([^"\'\s]+)(["\']?)(.*)$',
    re.IGNORECASE)
_NF_RE = re.compile(r'\bnf\s*=\s*\d+', re.IGNORECASE)


def shaped_name(netlist_path):
    """`two_stage_rz_final.sp` -> `two_stage_rz_final_shaped.sp`."""
    base = os.path.basename(netlist_path)
    stem, ext = os.path.splitext(base)
    if stem.endswith(SHAPED_SUFFIX):
        return base  # already marked; don't stack suffixes on a re-run
    return f"{stem}{SHAPED_SUFFIX}{ext}"


def _set_nf(line, nf):
    """Return `line` with `nf=<nf>`, whether or not it already had one."""
    if _NF_RE.search(line):
        return _NF_RE.sub(f"nf={nf}", line, count=1)
    stripped = line.rstrip("\n")
    newline = line[len(stripped):]
    return f"{stripped} nf={nf}{newline}"


def _is_design_local(resolved, design_dir):
    """True if `resolved` sits inside the design tree (a sub-circuit to copy),
    False for a PDK/model/corner include that belongs where it is."""
    try:
        return os.path.commonpath([os.path.abspath(resolved),
                                   os.path.abspath(design_dir)]) == os.path.abspath(design_dir)
    except ValueError:      # different drives on Windows
        return False


def process(netlist_path, out_dir, nf_map, design_dir, is_top=True,
            _seen=None, _actions=None):
    """Write `netlist_path` into `out_dir`, recursing through its includes.

    Returns the list of actions taken (one dict per file written).
    """
    _seen = _seen if _seen is not None else set()
    _actions = _actions if _actions is not None else []

    src = os.path.abspath(netlist_path)
    if src in _seen:
        return _actions
    _seen.add(src)

    out_name = shaped_name(src) if is_top else os.path.basename(src)
    out_path = os.path.join(out_dir, out_name)
    src_dir = os.path.dirname(src)

    with open(src) as f:
        lines = f.readlines()

    applied, includes = {}, []
    out_lines = []
    for line in lines:
        inc = _INCLUDE_RE.match(line)
        if inc:
            head, q1, path, q2, tail = inc.groups()
            resolved = path if os.path.isabs(path) else os.path.join(src_dir, path)
            if os.path.isfile(resolved) and _is_design_local(resolved, design_dir):
                # a design-local sub-circuit: copy it in flat, refer to it bare
                includes.append(resolved)
                new_path = os.path.basename(resolved)
                kind = "design-local"
            else:
                # PDK / model / corner: keep the target, make it survive the move
                new_path = os.path.abspath(resolved) if not os.path.isabs(path) else path
                kind = "external"
            out_lines.append(f"{head}{q1}{new_path}{q2}{tail}\n")
            _actions.append({"file": out_name, "include": path,
                             "rewritten_to": new_path, "kind": kind})
            continue

        m = _MOS_RE.match(line.strip())
        if m and m.group("name") in nf_map:
            name = m.group("name")
            line = _set_nf(line, nf_map[name])
            applied[name] = nf_map[name]
        out_lines.append(line)

    os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        f.writelines(out_lines)

    _actions.append({"file": out_name, "source": src, "top": is_top,
                     "nf_applied": applied, "renamed": out_name != os.path.basename(src)})

    for inc_src in includes:
        process(inc_src, out_dir, nf_map, design_dir, is_top=False,
                _seen=_seen, _actions=_actions)

    return _actions


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist", help="the sized TOP-LEVEL netlist, "
                                    "<design_dir>/sizing/<design>_final.sp")
    ap.add_argument("--nf", default=None,
                    help="comma-separated name=nf, e.g. XMN1=4,XMN2=4")
    ap.add_argument("--nf-json", default=None,
                    help="JSON with a per-device nf map: either {name: nf} or "
                         "an object carrying one under `per_device_nf`/`nf_map`")
    ap.add_argument("--out-dir", default=None,
                    help="default: <design_dir>/device_shaping")
    ap.add_argument("--design-dir", default=None,
                    help="design root, for telling a sub-circuit include from a "
                         "PDK one (default: the netlist's parent's parent)")
    ap.add_argument("--json", default=None, help="write the action log here")
    args = ap.parse_args()

    netlist = os.path.abspath(args.netlist)
    if not os.path.isfile(netlist):
        sys.exit(f"error: no such netlist: {netlist}")

    design_dir = os.path.abspath(args.design_dir) if args.design_dir \
        else os.path.dirname(os.path.dirname(netlist))
    out_dir = os.path.abspath(args.out_dir) if args.out_dir \
        else os.path.join(design_dir, "device_shaping")

    nf_map = {}
    if args.nf_json:
        with open(args.nf_json) as f:
            doc = json.load(f)
        for key in ("per_device_nf", "nf_map", "shaped_nf"):
            if isinstance(doc.get(key), dict):
                doc = doc[key]
                break
        nf_map.update({k: int(v) for k, v in doc.items() if isinstance(v, (int, str))})
    if args.nf:
        for pair in args.nf.split(","):
            name, _, val = pair.partition("=")
            if name.strip() and val.strip():
                nf_map[name.strip()] = int(val)
    if not nf_map:
        sys.exit("error: no finger counts given -- pass --nf and/or --nf-json")

    actions = process(netlist, out_dir, nf_map, design_dir, is_top=True)

    written = [a for a in actions if "source" in a]
    top = next(a for a in written if a["top"])
    print(f"Shaped hand-off: {os.path.join(out_dir, top['file'])}")
    for a in written:
        tag = "TOP (renamed)" if a["top"] else "sub-circuit (name kept)"
        applied = ", ".join(f"{k}=nf{v}" for k, v in a["nf_applied"].items()) or "-"
        print(f"  {a['file']:<40} {tag:<24} nf applied: {applied}")
    rewrites = [a for a in actions if "include" in a]
    for r in rewrites:
        print(f"  include [{r['kind']}] {r['include']} -> {r['rewritten_to']}")

    unplaced = set(nf_map) - {k for a in written for k in a["nf_applied"]}
    if unplaced:
        print(f"  WARNING: never found in any netlist file: {sorted(unplaced)}")

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump({"out_dir": out_dir, "top": top["file"], "actions": actions,
                       "nf_map": nf_map, "unplaced": sorted(unplaced)}, f, indent=2)
        print(f"  -> {args.json}")

    if unplaced:
        sys.exit(1)


if __name__ == "__main__":
    main()
