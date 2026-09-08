#!/usr/bin/env python3
"""Choose every device's drawn shape -- unit width, finger count, copy count --
from four geometric principles. No parasitic table, no simulation, no sweep.

THE FOUR PRINCIPLES, in the order they are applied:

  1. USE A UNIT DEVICE. Every member of a tie group is the same physical
     device repeated. A current mirror's legs differ only in how many copies
     they have; that is what makes them matchable, and it is the same rule
     `circuit-decomposition` already enforces on `w`/`l`.
  2. MAXIMIZE THE UNIT. Bigger unit -> fewer copies -> less perimeter, fewer
     junctions, less wiring between copies. Bounded above by the PDK's widest
     model bin.
  3. SQUARE DEVICE. One copy should be about as tall as it is wide. A long
     thin device wastes area, stretches every net that crosses it, and matches
     badly because its edge effects dominate.
  4. SQUARE ARRAY. The copies themselves tile into a block that should also be
     about square, for the same reasons one device should be.

And a constraint across all four: **the total width `w * m` is preserved**.
Re-shaping is a re-expression of the same silicon, not a resize -- `W`/`L` and
the drawn total belong to `../../schematic-sizing/SKILL.md`, which has already
finished. Where an exact re-expression does not exist, the residual is bounded
by `--max-total-error` and REPORTED, never absorbed silently.

SEARCH AND SCORING. For each group, candidate unit widths are every member's
total divided by every integer copy count (so an exact division is always in
the set). Each candidate is scored lexicographically:

    (total-error bucket, array squareness, -unit width, device squareness)

which is principles 1-4 read in the order the caller chose: keep the total,
then square the array, then make the unit as big as possible, then square the
device. Ties break toward the smaller residual.

WHY THIS REPLACED THE Nf SWEEP. The previous skill measured `nf` by simulating
every candidate against a PDK parasitic coefficient table. On `sky130A` that
table is empty by design (`parasitic-estimation`'s `PDK_TABLES = {}`, left so
deliberately rather than filled with invented physics), so the sweep refused to
run by name and every device reached layout at `nf=1` -- UNMEASURED, and
indistinguishable in the file from a chosen one. These four rules need no such
table: they are geometry, and they are the same geometry on any process whose
design rules can be read. `nf` is now always chosen, on every PDK.

Usage:
  python shape_devices.py <sized.sp> --decomposition <circuit_decomposition.yaml>
      [--pdk sky130A] [--pdk-root DIR] [--out <shaped.sp>] [--report <log>]
      [--json <plan.json>] [--max-total-error 0.02] [--max-copies 64]
      [--dry-run]

Reads the sized netlist; writes a NEW `_shaped.sp` and never edits its input.
"""
import argparse
import json
import math
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent.parent / "schematic-sizing" / "script"))
sys.path.insert(0, str(_HERE.parent.parent.parent / "reference"))
from netlist_devices import parse_devices  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: python3 -m pip install pyyaml")


# --------------------------------------------------------------------------
# Process geometry. Every number is READ from the PDK, never assumed -- see
# geometry_for(). The fallbacks exist only so the shaper still runs on a
# process glayout cannot answer for, and they are reported as assumed.
# --------------------------------------------------------------------------
FALLBACK_GEOM = {
    "sd_column_um": 0.33,   # source/drain column: contact + its two enclosures
    "poly_extension_um": 0.13,
    "source": "FALLBACK -- glayout could not be queried; numbers are sky130-like",
}


def geometry_for(pdk_name):
    """Finger-pitch geometry from the PDK's own design rules.

    A multi-finger MOS alternates gate columns (poly, width `l`) with
    source/drain columns (a contact plus its enclosures). So one copy is

        width  = nf * l + (nf + 1) * sd_column
        height = w / nf + 2 * poly_extension

    `sd_column` and `poly_extension` are the only process numbers this needs,
    and both come from `get_grule()`."""
    try:
        import importlib
        sys.path.insert(0, str(_HERE.parent.parent.parent / "reference"))
        import pdk_config
        mod = pdk_config.pdk(pdk_name).glayout_module
        gl = importlib.import_module(f"glayout.pdk.{mod}_mapped")
        p = getattr(gl, f"{mod}_mapped_pdk")
        con = p.get_grule("mcon")
        con_w = con.get("width") or con.get("min_width")
        enc_poly = (p.get_grule("mcon", "poly") or {}).get("min_enclosure")
        enc_diff = (p.get_grule("mcon", "active_diff") or {}).get("min_enclosure")
        ext = (p.get_grule("poly", "poly") or {}).get("extension")
        if None in (con_w, enc_poly, ext):
            raise ValueError("PDK did not answer for mcon/poly rules")
        return {
            "sd_column_um": round(con_w + 2 * enc_poly, 6),
            "poly_extension_um": float(ext),
            "contact_um": float(con_w),
            "mcon_poly_enclosure_um": float(enc_poly),
            "mcon_diff_enclosure_um": float(enc_diff) if enc_diff else None,
            "source": f"glayout get_grule() on {mod}_mapped",
        }
    except Exception as exc:  # pragma: no cover
        g = dict(FALLBACK_GEOM)
        g["source"] += f" ({type(exc).__name__}: {exc})"
        return g


def bin_widths(pdk_name, pdk_root=None):
    """`{model: widest per-copy w}` from the PDK's own model cards. Empty when
    unreadable -- an absent bound must never become a false constraint."""
    try:
        sys.path.insert(0, str(_HERE.parent.parent / "design-sheets-checker" / "script"))
        from run_erc_check import load_pdk_bin_widths
        return load_pdk_bin_widths(pdk_root=pdk_root, pdk=pdk_name) or {}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# Shape maths
# --------------------------------------------------------------------------
def device_box(w_unit, l, nf, geom):
    """(height, width) of ONE copy drawn with `nf` fingers, in um."""
    sd, ext = geom["sd_column_um"], geom["poly_extension_um"]
    height = w_unit / nf + 2 * ext
    width = nf * l + (nf + 1) * sd
    return height, width


def _aspect_penalty(height, width):
    """0 for a perfect square, growing with how far from square it is. Log so
    a 2:1 and a 1:2 cost the same -- neither orientation is better."""
    if height <= 0 or width <= 0:
        return float("inf")
    return abs(math.log(height / width))


# Below this per-finger width, a MOS stops behaving like a scaled version of
# itself: narrow-width effects shift Vt and drive, and the device's current is
# no longer what its total width says. Squareness must not buy that.
#
# MEASURED, not assumed. On test_miller_ota the shaper folded XMP4 -- the 1.4um
# self-bias reference -- into two 0.7um fingers to square it up. That one device
# sets every branch current in the amplifier, and the fold cost 38% of UGBW
# (16.78 -> 10.43 MHz) with total width preserved exactly. Reverting XMP4 alone
# recovered it to 16.11 MHz. Every other device's fingers were 3.6um or wider
# and cost nothing.
DEFAULT_MIN_FINGER_UM = 1.0


def best_fingers(w_unit, l, geom, nf_max=64, min_finger_um=DEFAULT_MIN_FINGER_UM):
    """The `nf` making one copy squarest (principle 3), subject to a floor on
    per-finger width. `nf=1` is always allowed: refusing to fold is never worse
    than the unfolded device it started as."""
    best, best_score = 1, _aspect_penalty(*device_box(w_unit, l, 1, geom))
    for nf in range(2, nf_max + 1):
        if w_unit / nf < min_finger_um:
            break                      # narrower still, for every larger nf
        score = _aspect_penalty(*device_box(w_unit, l, nf, geom))
        if score < best_score - 1e-12:
            best, best_score = nf, score
    return best, best_score


def best_grid(m, height, width):
    """(rows, cols, penalty) tiling `m` copies as squarely as possible
    (principle 4). Only exact factorisations are considered: a partly-filled
    row breaks the symmetry an array exists to provide, so a prime `m` is
    honestly reported as a strip rather than padded into a lie."""
    best = (1, m, _aspect_penalty(height, m * width))
    for rows in range(1, int(math.isqrt(m)) + 1):
        if m % rows:
            continue
        cols = m // rows
        for r, c in ((rows, cols), (cols, rows)):
            pen = _aspect_penalty(r * height, c * width)
            if pen < best[2] - 1e-12:
                best = (r, c, pen)
    return best


# --------------------------------------------------------------------------
# Grouping
# --------------------------------------------------------------------------
def _num(text, default=None):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def device_facts(dev):
    p = {k.lower(): v for k, v in dev["params"].items()}
    w = _num(p.get("w"))
    l = _num(p.get("l"))
    m = _num(p.get("m"), 1.0) or 1.0
    nf = _num(p.get("nf"), 1.0) or 1.0
    return w, l, m, nf


def build_groups(devices, decomposition):
    """-> [{"id", "members": [name], "tied_m": bool}] covering every MOS.

    A tie group from the circuit read shares ONE unit (principle 1). A group
    whose `tied` list contains `m` (a differential pair) shares the copy count
    too -- its halves are identical, not ratioed. Every device no group claims
    is its own group of one."""
    mos = [d["name"] for d in devices if d["kind"] == "mos"]
    groups, claimed = [], set()
    for tg in (decomposition.get("tie_groups") or []):
        members = [d for d in tg.get("devices", []) if d in mos and d not in claimed]
        if not members:
            continue
        claimed.update(members)
        groups.append({
            "id": tg.get("id", "tg"),
            "pattern": tg.get("pattern", "?"),
            "members": members,
            "tied_m": "m" in (tg.get("tied") or []),
        })
    for name in mos:
        if name not in claimed:
            groups.append({"id": f"free_{name}", "pattern": "standalone",
                           "members": [name], "tied_m": False})
    return groups


def shape_group(group, facts, geom, w_max, max_total_error, max_copies,
                min_finger_um=DEFAULT_MIN_FINGER_UM):
    """The best (unit width, nf, per-member copy count) for one group.

    `facts` is {name: (w, l, m, nf)}. Returns a plan dict, or None when the
    group carries no usable width."""
    members = group["members"]
    totals = {n: facts[n][0] * facts[n][2] for n in members
              if facts[n][0] is not None}
    if not totals:
        return None
    # `l` is already shared inside a tie group (sizing tied it). If a group
    # somehow disagrees, the smallest is the safe one to draw against: it makes
    # the widest device box, so squareness is not overstated.
    l = min(facts[n][1] for n in members if facts[n][1] is not None)

    lo = 1e-3
    hi = w_max if w_max else max(totals.values())

    # Candidate units: every member's total over every copy count. An exact
    # division is therefore always in the set when one exists.
    candidates = set()
    for t in totals.values():
        for k in range(1, max_copies + 1):
            u = t / k
            if lo <= u <= hi:
                candidates.add(round(u, 9))
    if not candidates:
        return None

    scored = []
    for w_unit in sorted(candidates):
        nf, dev_pen = best_fingers(w_unit, l, geom,
                                   min_finger_um=min_finger_um)
        h, wd = device_box(w_unit, l, nf, geom)

        counts, errs, grids = {}, {}, {}
        ok = True
        for name, t in totals.items():
            m_i = max(1, int(round(t / w_unit)))
            if m_i > max_copies:
                ok = False
                break
            err = abs(m_i * w_unit - t) / t if t else 0.0
            if err > max_total_error:
                ok = False
                break
            counts[name], errs[name] = m_i, err
            grids[name] = best_grid(m_i, h, wd)
        if not ok:
            continue
        if group["tied_m"] and len(set(counts.values())) > 1:
            continue  # a tied-m group's members must land on one copy count

        max_err = max(errs.values())
        array_pen = sum(g[2] for g in grids.values()) / len(grids)
        scored.append({"w_unit": w_unit, "l": l, "nf": nf, "counts": counts,
                       "errors": errs, "grids": grids, "device_box": (h, wd),
                       "device_penalty": dev_pen, "array_penalty": array_pen,
                       "max_total_error": max_err, "totals_before": dict(totals)})

    if not scored:
        return None

    # PRINCIPLE 2 IS A GATE, NOT A TIE-BREAK -- and it has to be, which is worth
    # writing down because it is not what a literal reading of the priority
    # order gives. Applied literally as "total, then array, then unit size",
    # the search collapses: halving the unit doubles every copy count, and a
    # bigger copy count almost always factors more squarely, so array
    # squareness can be improved without limit by shrinking the unit. Measured
    # on test_miller_ota's nfet mirror: unit 47.2um (m=5/2/6, arrays 1x5/1x2/
    # 2x3) loses to unit 7.87um (m=30/12/36, arrays 5x6/3x4/6x6) -- both exact
    # on total width, the second absurd. So unit size is bucketed in halvings
    # below the largest feasible unit and compared BEFORE array shape: within
    # one halving of the biggest unit available, the squarest array wins.
    u_ref = max(c["w_unit"] for c in scored)

    def key(c):
        err_bucket = 0 if c["max_total_error"] < 1e-9 else \
            1 + int(c["max_total_error"] / 0.005)
        unit_bucket = int(math.log2(u_ref / c["w_unit"])) if c["w_unit"] > 0 else 99
        return (err_bucket, unit_bucket, round(c["array_penalty"], 6),
                round(c["device_penalty"], 6), c["max_total_error"])

    best = min(scored, key=key)
    best["unit_ceiling"] = u_ref
    best["candidates_considered"] = len(scored)
    return best


def plan(devices, decomposition, geom, bins, max_total_error, max_copies,
         min_finger_um=DEFAULT_MIN_FINGER_UM):
    facts = {d["name"]: device_facts(d) for d in devices}
    out = []
    for group in build_groups(devices, decomposition):
        models = {d["model"] for d in devices if d["name"] in group["members"]}
        w_max = min((bins.get(f"{m}__model") or bins.get(m) or 1e9)
                    for m in models) if bins else None
        if w_max and w_max >= 1e9:
            w_max = None
        res = shape_group(group, facts, geom, w_max, max_total_error,
                          max_copies, min_finger_um)
        if res:
            res.update({"id": group["id"], "pattern": group["pattern"],
                        "members": group["members"], "tied_m": group["tied_m"],
                        "w_max": w_max,
                        "before": {n: facts[n] for n in group["members"]}})
            out.append(res)
        else:
            out.append({"id": group["id"], "pattern": group["pattern"],
                        "members": group["members"], "unshaped": True,
                        "w_max": w_max,
                        "before": {n: facts[n] for n in group["members"]}})
    return out


# --------------------------------------------------------------------------
# Emit
# --------------------------------------------------------------------------
import re  # noqa: E402

PARAM_RE = re.compile(r"\b(w|l|m|nf)\s*=\s*(\S+)", re.IGNORECASE)


def _fmt(v):
    return f"{int(round(v))}" if abs(v - round(v)) < 1e-9 else f"{v:g}"


def apply_plan(netlist_text, groups):
    """Rewrite `w`, `m` and `nf` on every shaped device. `l` is never touched --
    it is channel length, and sizing owns it."""
    assign = {}
    for g in groups:
        if g.get("unshaped"):
            continue
        for name in g["members"]:
            if name in g["counts"]:
                assign[name] = {"w": g["w_unit"], "m": g["counts"][name],
                                "nf": g["nf"]}
    lines = netlist_text.splitlines(keepends=True)
    index = {d["name"].lower(): d["lineno"] - 1 for d in parse_devices(netlist_text)}
    for name, vals in assign.items():
        idx = index.get(name.lower())
        if idx is None:
            continue
        line = lines[idx]

        def sub(mo):
            key = mo.group(1).lower()
            return f"{mo.group(1)}={_fmt(vals[key])}" if key in vals else mo.group(0)

        new = PARAM_RE.sub(sub, line)
        for key in ("nf", "m"):          # add a token the line never carried
            if not re.search(rf"\b{key}\s*=", new, re.IGNORECASE):
                new = new.rstrip("\n") + f" {key}={_fmt(vals[key])}\n"
        lines[idx] = new
    return "".join(lines), assign


HEADER = """\
* SHAPED by device-shaper: unit device, maximal unit, square device, square array.
* `w` is the UNIT width and `m` the copy count -- total width w*m is preserved
* (residual per device in shaping_report.log). `l` is untouched: sizing owns it.
* Every member of a tie group shares one unit and differs only in `m`.
"""


def write_report(path, groups, geom, netlist, pdk_name, max_total_error,
                 assign, min_finger_um=DEFAULT_MIN_FINGER_UM):
    L = []
    A = L.append
    A("=" * 78)
    A("shaping_report.log -- device-shaper")
    A(f"netlist : {netlist}")
    A(f"PDK     : {pdk_name}")
    A("=" * 78)
    A("")
    A("THE FOUR PRINCIPLES, applied in this order")
    A("  1 unit device      every member of a tie group is one device repeated")
    A("  2 maximal unit     as big as the PDK's widest model bin allows")
    A("  3 square device    one copy as tall as it is wide")
    A("  4 square array     the copies tile into a square block")
    A("  constraint         total width w*m preserved; residual bounded by "
      f"--max-total-error {max_total_error:.3%} and reported below")
    A("")
    A("PROCESS GEOMETRY (one copy = nf gate columns + nf+1 source/drain columns)")
    A(f"  source              {geom['source']}")
    A(f"  sd_column           {geom['sd_column_um']:.4f} um")
    A(f"  poly_extension      {geom['poly_extension_um']:.4f} um")
    A(f"  min finger width    {min_finger_um:.3f} um (floor on w_unit/nf)")
    A("  height = w_unit/nf + 2*poly_extension")
    A("  width  = nf*l + (nf+1)*sd_column")
    A("")

    worst = 0.0
    for g in groups:
        A("-" * 78)
        if g.get("unshaped"):
            A(f"{g['id']}  [{g['pattern']}]  {g['members']}")
            A("  NOT SHAPED -- no usable width, or no candidate met "
              "--max-total-error. Left exactly as sizing wrote it.")
            continue
        h, wd = g["device_box"]
        A(f"{g['id']}  [{g['pattern']}]  {g['members']}"
          + ("   (tied m -- identical halves)" if g["tied_m"] else ""))
        A(f"  UNIT   w={g['w_unit']:.4f} um  l={g['l']:g} um  nf={g['nf']}"
          + (f"   (PDK bin max {g['w_max']:g} um)" if g.get("w_max") else ""))
        A(f"  DEVICE {h:.3f} x {wd:.3f} um   aspect {h / wd:.3f}"
          f"   (1.000 = square, penalty {g['device_penalty']:.4f})")
        A(f"  {'member':10s}{'total was':>12s}{'total now':>12s}{'err':>9s}"
          f"{'m':>5s}{'array':>9s}{'aspect':>9s}")
        for name in g["members"]:
            if name not in g["counts"]:
                continue
            m_i = g["counts"][name]
            was = g["totals_before"][name]
            now = m_i * g["w_unit"]
            r, c, pen = g["grids"][name]
            asp = (r * h) / (c * wd)
            worst = max(worst, g["errors"][name])
            flag = "  <-- strip" if min(r, c) == 1 and m_i > 2 else ""
            A(f"  {name:10s}{was:12.4f}{now:12.4f}{g['errors'][name]:9.3%}"
              f"{m_i:5d}{f'{r}x{c}':>9s}{asp:9.3f}{flag}")
    A("-" * 78)
    A("")
    A(f"WORST TOTAL-WIDTH RESIDUAL: {worst:.4%}"
      + ("  (every device exact)" if worst < 1e-9 else ""))
    A(f"DEVICES RESHAPED: {len(assign)}")
    A("")
    A("A `<-- strip` array is a copy count with no square factorisation (a "
      "prime, usually).")
    A("It is reported rather than padded: a partly-filled row breaks the "
      "symmetry the array exists")
    A("to provide. If it matters, the fix is upstream -- a copy count with "
      "factors, which means a")
    A("different unit width, which sizing chose.")
    A("")
    A("NOT MEASURED HERE: what any of this costs electrically. These are "
      "geometric rules, and the")
    A("re-expression is width-preserving, but finger count changes junction "
      "and gate perimeter.")
    A("Run verify_shaping.py to simulate before/after and get the spec delta.")
    Path(path).write_text("\n".join(L) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist")
    ap.add_argument("--decomposition", required=True,
                    help="circuit_decomposition.yaml -- supplies the tie groups")
    ap.add_argument("--pdk", default=None)
    ap.add_argument("--pdk-root", default=None)
    ap.add_argument("--out", default=None, help="default: <stem>_shaped.sp beside --report")
    ap.add_argument("--report", default=None)
    ap.add_argument("--json", default=None)
    ap.add_argument("--max-total-error", type=float, default=0.02,
                    help="hard cap on any device's total-width residual (default 2%%)")
    ap.add_argument("--max-copies", type=int, default=64)
    ap.add_argument("--min-finger-width", type=float, default=DEFAULT_MIN_FINGER_UM,
                    metavar="UM",
                    help="floor on per-finger width; below it narrow-width effects "
                         "change the device's drive (default %(default)s um -- see "
                         "DEFAULT_MIN_FINGER_UM for the measurement behind it)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.pdk:
        sys.path.insert(0, str(_HERE.parent.parent.parent / "reference"))
        import pdk_config
        args.pdk = pdk_config.pdk().pdk_env

    netlist = Path(args.netlist).resolve()
    text = netlist.read_text()
    devices = parse_devices(text)
    decomposition = yaml.safe_load(Path(args.decomposition).read_text()) or {}
    if not decomposition.get("tie_groups"):
        sys.exit(f"{args.decomposition} carries no `tie_groups`. The unit device "
                 f"is per tie group, so this skill cannot run without the circuit "
                 f"read -- run circuit-decomposition first.")

    geom = geometry_for(args.pdk)
    bins = bin_widths(args.pdk, args.pdk_root)
    groups = plan(devices, decomposition, geom, bins,
                  args.max_total_error, args.max_copies,
                  args.min_finger_width)

    new_text, assign = apply_plan(text, groups)

    out = Path(args.out) if args.out else netlist.with_name(
        netlist.stem + "_shaped.sp")
    report = Path(args.report) if args.report else out.with_name("shaping_report.log")

    for g in groups:
        if g.get("unshaped"):
            print(f"  {g['id']:28s} NOT SHAPED  {g['members']}")
            continue
        h, wd = g["device_box"]
        print(f"  {g['id']:28s} unit w={g['w_unit']:.3f} l={g['l']:g} nf={g['nf']} "
              f"dev {h:.2f}x{wd:.2f} (asp {h / wd:.2f})  m="
              + ",".join(f"{n}:{g['counts'][n]}" for n in g['members'] if n in g['counts']))

    if args.dry_run:
        print("\n(dry run -- nothing written)")
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(HEADER + new_text)
    write_report(report, groups, geom, netlist, args.pdk,
                 args.max_total_error, assign, args.min_finger_width)
    print(f"\nwrote {out}")
    print(f"wrote {report}")
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(
            {"pdk": args.pdk, "geometry": geom,
             "groups": [{k: v for k, v in g.items() if k != "key"} for g in groups]},
            indent=2, default=str))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
