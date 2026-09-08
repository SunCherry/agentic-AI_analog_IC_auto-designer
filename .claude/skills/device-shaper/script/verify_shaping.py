#!/usr/bin/env python3
"""Simulate the netlist before and after shaping, and report what the shape
change cost.

WHY THIS EXISTS. `shape_devices.py` is pure geometry: it re-expresses each
device as a unit repeated, preserving total width. That re-expression is very
nearly electrically neutral -- same `W`, same `L`, same total -- but it is not
exactly neutral, for two reasons worth measuring rather than assuming:

  * **finger count changes perimeter.** `nf` fingers share source/drain
    diffusions, so folding cuts junction area and perimeter, which moves Cdb
    and Csb. That is usually a small improvement, and it is the reason folding
    is worth doing at all.
  * **a total-width residual, where one exists.** An exact re-expression does
    not always exist under the copy-count cap; `shape_devices.py` bounds the
    residual and reports it, and here is where its electrical cost shows up.

So this does not gate anything and returns no verdict on the design. It states
the delta. Deciding what to do about it is the caller's.

Usage:
  python verify_shaping.py --testbench <deck.spice>
      --before <sized.sp> --after <shaped.sp> --out-dir <dir>
      [--target-spec <target_spec.json>] [--vdd 1.8] [--op-file NAME]
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent.parent / "reference"))
import compute_fidelity as CF  # noqa: E402

INCLUDE_RE = re.compile(r'^(\s*\.include\s+")([^"]+)(")', re.IGNORECASE | re.MULTILINE)
WRITE_RE = re.compile(r'(\bwrite\s+)(\S+)', re.IGNORECASE)
WRDATA_RE = re.compile(r'(\bwrdata\s+)(\S+)', re.IGNORECASE)


def build_deck(testbench: Path, netlist: Path, work: Path, tag: str) -> tuple:
    """The deck with its design `.include` re-pointed at `netlist` and its
    outputs redirected into `work`. The PDK `.lib` line is left alone."""
    text = testbench.read_text()
    raw_out = work / f"ac_{tag}.out"
    op_out = work / f"op_{tag}.txt"

    def fix_include(mo):
        target = mo.group(2)
        # Only the design's own netlist moves; a PDK .lib/.include stays put.
        if target.endswith((".sp", ".spice")) and "libs.tech" not in target:
            return f"{mo.group(1)}{netlist}{mo.group(3)}"
        return mo.group(0)

    text = INCLUDE_RE.sub(fix_include, text)

    # Only rewrite REAL commands. `write` and `wrdata` are ordinary English and
    # appear in these decks' own comments; substituting there corrupts the
    # commentary and, worse, hides which line actually produced the raw file.
    out_lines = []
    for line in text.split("\n"):
        if not line.lstrip().startswith("*"):
            line = WRITE_RE.sub(lambda m: f"{m.group(1)}{raw_out}", line)
            line = WRDATA_RE.sub(lambda m: f"{m.group(1)}{op_out}", line)
        out_lines.append(line)
    text = "\n".join(out_lines)
    deck = work / f"tb_{tag}.spice"
    deck.write_text(text)
    return deck, raw_out, op_out


def run(deck: Path, cwd: Path):
    proc = subprocess.run(["ngspice", "-b", str(deck)], cwd=str(cwd),
                          capture_output=True, text=True, timeout=900)
    return proc


def supply_power_mw(op_file: Path, vdd: float):
    """Power from the deck's own `wrdata` of the supply current. Returns None
    when the deck does not write one -- absent, not zero."""
    if not op_file.exists():
        return None
    try:
        vals = []
        for line in op_file.read_text().split("\n"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    vals.append(float(parts[-1]))
                except ValueError:
                    pass
        if not vals:
            return None
        return abs(vals[-1]) * vdd * 1e3
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--testbench", required=True)
    ap.add_argument("--before", required=True, help="the sized netlist")
    ap.add_argument("--after", required=True, help="the shaped netlist")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--target-spec", default=None)
    ap.add_argument("--vdd", type=float, default=None)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()

    work = Path(args.out_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    tb = Path(args.testbench).resolve()

    results = {}
    for tag, nl in (("before", args.before), ("after", args.after)):
        deck, raw, op = build_deck(tb, Path(nl).resolve(), work, tag)
        proc = run(deck, work)
        if not raw.exists():
            print(f"{tag}: ngspice produced no raw file", file=sys.stderr)
            print((proc.stderr or proc.stdout)[-1500:], file=sys.stderr)
            sys.exit(1)
        vals = dict(CF.EXTRACTORS["ac"](str(raw)))
        if args.vdd:
            p = supply_power_mw(op, args.vdd)
            if p is not None:
                vals["Power_mW"] = p
        results[tag] = vals

    keys = [k for k in results["before"] if k in results["after"]]
    lines = []
    A = lines.append
    A("device-shaper -- shape verification (geometry change, before vs after)")
    A("")
    A(f"  {'metric':12s}{'before':>16s}{'after':>16s}{'delta':>14s}{'rel':>10s}")
    for k in keys:
        b, a = results["before"][k], results["after"][k]
        d = a - b
        rel = (d / b * 100) if b else float("nan")
        A(f"  {k:12s}{b:16.5g}{a:16.5g}{d:14.5g}{rel:9.2f}%")

    if args.target_spec:
        spec = json.loads(Path(args.target_spec).read_text())
        A("")
        A("  against target_spec.json (the AFTER netlist is what layout gets)")
        alias = {"Gain": "Gain", "UGBW": "UGBW", "PM": "PM", "Power": "Power_mW"}
        for key, meta in spec.items():
            src = alias.get(key, key)
            if src not in results["after"]:
                A(f"  {key:12s}  not measured here")
                continue
            v = results["after"][src]
            if key == "UGBW" and v > 1e3:
                v = v / 1e6
            d, val = meta.get("Direction", "").upper(), meta.get("Value")
            if d == "FLOOR":
                ok = v >= val
            elif d == "CEILING":
                ok = v <= val
            elif d == "RANGE":
                ok = val[0] <= v <= val[1]
            else:
                ok = None
            A(f"  {key:12s}{v:16.5g}   bar={val}   "
              f"{'PASS' if ok else 'FAIL' if ok is not None else '?'}")

    A("")
    A("This is a MEASUREMENT, not a gate. A small delta is expected: folding")
    A("changes junction perimeter, and any total-width residual shows up here.")
    out = "\n".join(lines)
    print(out)
    if args.report:
        Path(args.report).write_text(out + "\n")


if __name__ == "__main__":
    main()
