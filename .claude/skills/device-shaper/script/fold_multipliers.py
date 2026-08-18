#!/usr/bin/env python3
"""Fold `m` into `nf` for STANDALONE MOS primitives -- layout hand-off only.

A device written with `m > 1` is `m` parallel copies, and a layout generator
draws each copy as its own row. For a device that gets drawn STANDALONE that
is a bad floorplan: measured on `example/test_miller_ota`, XMN5
(`w=43.12 nf=1 m=8`) came out of the placer as a 9.38 x 368.15um column, and
the whole floorplan's bbox was 132 x 430um at 18.4% utilization. The same
total width drawn as ONE row of `nf * m` fingers is compact instead.

THE TRANSFORM, and why `w` moves too
------------------------------------
    w -> w * m        nf -> nf * m        m -> 1

`m` is the only parameter that multiplies width: total = `w * m`, because
SPICE's `nf` SPLITS `w` into fingers and adds none (BSIM4 takes `Weff = W/NF`;
measured on sky130 -- w=40 nf=1 -> 1.686mA, w=40 nf=8 -> 1.695mA unchanged,
w=40 m=8 -> 13.486mA, exactly 8x). So dropping `m` to 1 without raising `w`
would DIVIDE the device's width by `m`. Scaling both keeps two things exact:

    total width    (w * m)  * 1      == w * m        unchanged
    finger extent  (w * m) / (nf * m) == w / nf      unchanged

Only the arrangement changes, which is the entire point.

WHY THIS WRITES A SEPARATE FILE AND NEVER EDITS IN PLACE
--------------------------------------------------------
The folded `w` is `m` times bigger, and routinely lands past the PDK's widest
model bin: on the design above, XMP3 folds to `w=240` against sky130's 100um
per-copy limit. Such a netlist matches no model card and does not simulate --
it would silently undo the model-bin fold that design-sheets-checker Step 2a
applied to make the design simulable in the first place. So the output here is
a GEOMETRY-ONLY artifact:

    <design>_final_shaped_primitives.sp

Never simulate it, never hand it to sizing, never let it replace
`<design>_final_shaped.sp`. It exists to tell a layout generator how to DRAW
the standalone devices. The two files describe the same circuit; only the
w/nf/m split differs, and `w * m` agrees device by device (verified below).

WHAT IS SKIPPED, AND WHY
------------------------
Devices that `circuit_decomposition.yaml` places inside a COMPOSED pattern --
a current_mirror, a differential_pair, anything grouping two or more MOS
devices -- are left exactly as written. Those are not drawn device by device:
the block generator derives its own geometry from the pattern (a mirror's leg
ratios come from each leg's total width against the reference; a pair splits
`nf * m` across its two halves for common centroid). Rewriting `m` underneath
it would either change a ratio the mirror is built on or break the pair's even
split. A single-device pattern (a lone common-source device, a self-biased
reference) IS drawn standalone, so it folds.

Usage:
  fold_multipliers.py <shaped.sp> [--decomposition circuit_decomposition.yaml]
                      [--out PATH] [--dry-run] [--json PATH]

Without --decomposition every MOS device is treated as standalone, and the run
says so -- correct only for a design with no composed blocks at all.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# A pattern grouping at least this many MOS devices is drawn as one composed
# block, so its members are skipped. Below it (a one-device pattern) the
# device is drawn standalone and folds like any ungrouped device.
BLOCK_MIN_DEVICES = 2

_W_RE = re.compile(r'\b(w)\s*=\s*([\d.eE+-]+)', re.I)
_NF_RE = re.compile(r'\bnf\s*=\s*([\d.eE+-]+)', re.I)
_M_RE = re.compile(r'\bm\s*=\s*([\d.eE+-]+)', re.I)


def is_mos_line(s: str) -> bool:
    t = s.strip()
    return bool(t) and t[:1].upper() in ("X", "M") and "fet" in t.lower()


def blocked_devices(yaml_path: Path):
    """Device names that live inside a composed (multi-device) pattern.

    Parsed with the same shallow scan the rest of this skill uses -- the file
    is generated, so its shape is known: `patterns:` holds `- id:` entries,
    each with a `devices:` list of `{ref: NAME, ...}` mappings.
    """
    blocked, patterns = {}, []
    cur_id = cur_pat = None
    refs = []
    in_patterns = False
    for raw in yaml_path.read_text().splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        if re.match(r'^\w+:', line) and not line.startswith(" "):
            in_patterns = line.startswith("patterns:")
            if not in_patterns and cur_id:
                patterns.append((cur_id, cur_pat, refs)); cur_id, refs = None, []
            continue
        if not in_patterns:
            continue
        m = re.match(r'^-\s*id:\s*(\S+)', stripped)
        if m:
            if cur_id:
                patterns.append((cur_id, cur_pat, refs))
            cur_id, cur_pat, refs = m.group(1), None, []
            continue
        m = re.match(r'^pattern:\s*(\S+)', stripped)
        if m and cur_id:
            cur_pat = m.group(1)
            continue
        for ref in re.findall(r'\{\s*ref:\s*([A-Za-z0-9_.]+)', stripped):
            refs.append(ref)
    if cur_id:
        patterns.append((cur_id, cur_pat, refs))

    for pid, pat, refs in patterns:
        mos = [r for r in refs if r.upper().startswith(("XM", "M"))]
        if len(mos) >= BLOCK_MIN_DEVICES:
            for r in mos:
                blocked[r] = f"{pat or '?'} '{pid}'"
    return blocked, patterns


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist", type=Path, help="the shaped hand-off netlist")
    ap.add_argument("--decomposition", type=Path, default=None,
                    help="circuit_decomposition.yaml -- names the composed blocks to skip")
    ap.add_argument("--out", type=Path, default=None,
                    help="output path (default: <stem>_primitives.sp beside the input)")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--json", type=Path, default=None, help="machine-readable decisions")
    args = ap.parse_args()

    if not args.netlist.is_file():
        sys.exit(f"no such netlist: {args.netlist}")

    blocked = {}
    if args.decomposition and args.decomposition.is_file():
        blocked, _ = blocked_devices(args.decomposition)
    elif args.decomposition:
        sys.exit(f"no such decomposition: {args.decomposition}")
    else:
        print("  NOTE: no --decomposition given -- treating EVERY MOS device as a "
              "standalone primitive. Correct only if this design composes no "
              "current_mirror/differential_pair blocks.")

    out_lines, rows = [], []
    for line in args.netlist.read_text().splitlines():
        if not is_mos_line(line):
            out_lines.append(line)
            continue
        name = line.split()[0]
        wm = _W_RE.search(line)
        if not wm:
            out_lines.append(line)
            continue
        w = float(wm.group(2))
        nfm, mm = _NF_RE.search(line), _M_RE.search(line)
        nf = float(nfm.group(1)) if nfm else 1.0
        m = float(mm.group(1)) if mm else 1.0
        total = w * m

        if name in blocked:
            rows.append(dict(device=name, action="skipped", reason=f"in {blocked[name]}",
                             w=w, nf=nf, m=m, total_w=total))
            out_lines.append(line)
            continue
        if m == 1:
            rows.append(dict(device=name, action="unchanged", reason="m already 1",
                             w=w, nf=nf, m=m, total_w=total))
            out_lines.append(line)
            continue

        new_w, new_nf = w * m, nf * m
        new_line = line[:wm.start()] + f"{wm.group(1)}={new_w:g}" + line[wm.end():]
        new_line = _NF_RE.sub(f"nf={new_nf:g}", new_line) if nfm else new_line
        new_line = _M_RE.sub("m=1", new_line) if mm else new_line
        assert abs(new_w * 1.0 - total) < 1e-9, f"{name}: total width not preserved"
        rows.append(dict(device=name, action="folded", reason="standalone primitive",
                         w=w, nf=nf, m=m, total_w=total,
                         new_w=new_w, new_nf=new_nf, new_m=1.0))
        out_lines.append(new_line)

    folded = [r for r in rows if r["action"] == "folded"]
    skipped = [r for r in rows if r["action"] == "skipped"]
    unchanged = [r for r in rows if r["action"] == "unchanged"]

    print(f"=== Fold m into nf (standalone primitives only): {args.netlist.name} ===")
    print(f"  {len(rows)} MOS device(s): {len(folded)} folded, {len(skipped)} in a "
          f"composed block (skipped), {len(unchanged)} already m=1\n")
    hdr = (f"  {'device':8}{'grouping':26}{'w':>9}{'nf':>5}{'m':>4}"
           f"{'->  w':>10}{'nf':>5}{'m':>3}{'total w*m':>12}")
    print(hdr); print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        grp = r["reason"] if r["action"] == "skipped" else \
              ("standalone" if r["action"] == "folded" else "standalone (m=1)")
        if r["action"] == "folded":
            print(f"  {r['device']:8}{grp:26}{r['w']:9.4g}{r['nf']:5.0f}{r['m']:4.0f}"
                  f"{r['new_w']:10.4g}{r['new_nf']:5.0f}{1:3d}{r['total_w']:12.2f}")
        else:
            print(f"  {r['device']:8}{grp:26}{r['w']:9.4g}{r['nf']:5.0f}{r['m']:4.0f}"
                  f"{'--':>10}{'--':>5}{'--':>3}{r['total_w']:12.2f}")
    print(f"\n  Total width is preserved device by device (w*m column is the "
          f"invariant, and every folded row keeps it).")
    print(f"  Finger extent w/nf is preserved too -- only the arrangement changes: "
          f"m rows of nf fingers -> 1 row of nf*m fingers.")

    if folded:
        print("\n  GEOMETRY ONLY -- do NOT simulate this output. Folding w by m "
              "routinely pushes a device past the PDK's widest model bin:")
        for r in folded:
            print(f"    {r['device']}: w {r['w']:g} -> {r['new_w']:g}um per copy")

    if args.dry_run:
        print("\n  --dry-run: nothing written.")
    else:
        out = args.out or args.netlist.with_name(args.netlist.stem + "_primitives.sp")
        banner = [
            f"* {out.name} -- GEOMETRY-ONLY derivative of {args.netlist.name}.",
            "* MODIFIED by fold_multipliers.py: for every STANDALONE MOS primitive,",
            "*   w -> w*m,  nf -> nf*m,  m -> 1.",
            "* Total width (w*m) and finger extent (w/nf) are unchanged per device;",
            "* only the arrangement changes (m rows of nf fingers -> one row of nf*m).",
            "* Devices inside a composed block (current_mirror, differential_pair, ...)",
            "* are left exactly as written -- their block generator derives its own",
            "* geometry from the pattern and must see the original w/nf/m.",
            "*",
            "* DO NOT SIMULATE THIS FILE and do not hand it to sizing. The folded w is",
            "* m times larger and routinely exceeds the PDK's widest model bin, which",
            "* is exactly what design-sheets-checker's model-bin fold exists to avoid.",
            f"* The simulable netlist remains {args.netlist.name}.",
        ]
        out.write_text("\n".join(banner + out_lines) + "\n")
        print(f"\n  wrote {out}")

    if args.json:
        args.json.write_text(json.dumps(
            {"netlist": str(args.netlist), "rows": rows,
             "folded": len(folded), "skipped": len(skipped),
             "unchanged": len(unchanged)}, indent=2) + "\n")
        print(f"  wrote {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
