#!/usr/bin/env python3
"""Independently verify a shortened layout against the one it came from.

`shorten_routes.py` reports on its own work. This script does not trust any of
that: it re-reads both GDS files and both maps from disk and asks four
questions the optimiser is in no position to answer about itself.

  1. **Does the output GDS agree with the output map?** Every net rectangle the
     map claims must be a real polygon in the top cell, and every routing-layer
     polygon in the top cell must be one the map claims. A stale polygon left
     behind by an incomplete edit is a short that nothing else would catch.
  2. **Is every net still one connected piece, and does it still hold every
     landing it started with?** The landings are recomputed from the ORIGINAL
     layout, not read out of the new map, so a landing quietly dropped during
     the rewrite shows up here.
  3. **Do two different nets touch?** Same-layer overlap between nets is a
     short. (Sub-minimum spacing is DRC's question, not this one's.)
  4. **Does the layout still extract to the same circuit?** Magic extracts both
     layouts and netgen compares them to each other. "Circuits match uniquely"
     is the real proof that shortening changed geometry and nothing else --
     and it is a comparison against the input layout, so it holds even where a
     layout-vs-schematic LVS needs setup this script has no business guessing.

Magic extraction is messy -- a `.ext` per cell, a copy of each GDS, a log per
run -- and none of it is the answer. It all goes to a scratch directory that is
deleted on the way out, and what survives is ONE file: the report. Pass
`--work-dir` to put the scratch somewhere and keep it instead.

Usage:
  python verify_shortening.py <original.gds> <shortened.gds>
      [--map-before <physical_map.json>] [--map-after <physical_map.json>]
      [--out <verification.txt>] [--work-dir <dir>] [--no-lvs]

Exit status is 0 only if every check that ran passed.
"""
import argparse
import os
import shutil
import subprocess
import tempfile
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shorten_routes import (  # noqa: E402
    Layout, bbox, boxes_touch, connected_groups, freeze_landings,
    load_pdk_layers, macro_world_polygons, pdk_option)
from primitives.fet import (  # noqa: E402 -- see shorten_routes' sys.path setup
    nodes_touching, partition_connected_metal)

MATCH_TOL = 0.02

EXTRACT_TCL = """gds read {gds}
load {top}
select top cell
port makeall
extract path .
extract all
ext2spice lvs
ext2spice -o {out}
quit -noprompt
"""


def magic_paths():
    cfg = pdk_option()
    root = os.environ.get("PDK_ROOT") or cfg.pdk_root
    magic = "/usr/local/bin/magic"
    if not os.path.exists(magic):
        magic = shutil.which("magic") or magic
    return magic, cfg.tool("magicrc"), root, cfg.tool("netgen_setup")


def extract(gds, top, work, name):
    """Magic extraction, verbatim from ../../../reference/environment.md's
    'Magic Extraction + netgen LVS' section."""
    magic, magicrc, root, _ = magic_paths()
    work.mkdir(parents=True, exist_ok=True)
    local = work / f"{name}.gds"
    shutil.copy(gds, local)
    spice = f"{name}_extracted.spice"
    (work / f"{name}.tcl").write_text(
        EXTRACT_TCL.format(gds=local.name, top=top, out=spice))
    env = dict(os.environ)
    env["PDK_ROOT"] = root
    r = subprocess.run([magic, "-dnull", "-noconsole", "-rcfile", magicrc,
                        f"{name}.tcl"], cwd=str(work), env=env,
                       capture_output=True, text=True, timeout=1200)
    (work / f"{name}_magic.log").write_text(r.stdout + "\n" + r.stderr)
    out = work / spice
    return out if out.exists() else None


def netgen_compare(a, b, top_a, top_b, work, log):
    _, _, root, setup = magic_paths()
    netgen = shutil.which("netgen") or str(Path.home() / ".local/bin/netgen")
    if not os.path.exists(netgen) and not shutil.which(netgen):
        log("  netgen not found -- skipping the extraction comparison")
        return None
    report = work / "layout_vs_layout.out"
    env = dict(os.environ)
    env["PDK_ROOT"] = root
    r = subprocess.run(
        [netgen, "-batch", "lvs", f"{a} {top_a}", f"{b} {top_b}", setup,
         str(report)], cwd=str(work), env=env, capture_output=True,
        text=True, timeout=1200)
    (work / "netgen.log").write_text(r.stdout + "\n" + r.stderr)
    text = report.read_text() if report.exists() else ""
    ok = "Circuits match uniquely" in text
    for line in text.splitlines()[-6:]:
        if line.strip():
            log("    " + line.rstrip())
    return ok


def macro_nodes(layout):
    """Every macro's metal split into electrically separate nodes, in world
    coordinates, keyed so the same node gets the same name in two different
    reads of the same layout."""
    parts, polys = {}, {}
    via_links = pdk_option().via_links
    cache = {}
    for ref in layout.macro_refs:
        pl = macro_world_polygons(ref, cache)
        polys[id(ref)] = pl
        parts[id(ref)] = partition_connected_metal(pl, via_links)
    sigs = {}
    for key, part in parts.items():
        by_node = {}
        for i, (lay, b) in enumerate(part["items"]):
            by_node.setdefault(part["root"][i], []).append((lay, b))
        sigs[key] = {node: min((l, tuple(round(v, 4) for v in bx))
                               for l, bx in boxes)
                     for node, boxes in by_node.items()}
    return parts, polys, sigs


def item_nodes(item, layout, parts, polys, sigs):
    """Which macro nodes a piece of routing lands on.

    This is what makes "is the net connected" a fair question after a bulk
    landing has been released: two pieces of top-level wire that both touch a
    device's tie ring ARE connected, through metal inside the macro that no
    rectangle test at the top level can see. Extraction knows that; this is how
    the structural check gets to know it too."""
    out = set()
    for ref in layout.macro_refs:
        pl = polys[id(ref)]
        for lay in item.layers():
            b = item.box_on(lay)
            if b is None or lay not in pl:
                continue
            if not any(boxes_touch(b, mb, 0.0) for mb in pl[lay]):
                continue
            for node in nodes_touching(parts[id(ref)], lay, b):
                out.add(sigs[id(ref)][node])
    return out


def net_boxes(layout):
    per = defaultdict(list)
    for name, net in layout.nets.items():
        for it in net.items:
            for lay in it.layers():
                b = it.box_on(lay)
                if b is not None:
                    per[lay].append((name, b))
    return per


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--map-before", default=None)
    ap.add_argument("--map-after", default=None)
    ap.add_argument("--out", default=None,
                    help="where the report goes "
                         "(default: verification.txt beside the shortened GDS)")
    ap.add_argument("--work-dir", default=None,
                    help="keep Magic's and netgen's scratch here; without it "
                         "they run in a temp directory that is deleted")
    ap.add_argument("--no-lvs", action="store_true")
    args = ap.parse_args()

    before_gds = Path(args.before).resolve()
    after_gds = Path(args.after).resolve()
    map_b = Path(args.map_before).resolve() if args.map_before else before_gds.parent / "physical_map.json"
    map_a = Path(args.map_after).resolve() if args.map_after else after_gds.parent / "physical_map.json"
    keep_work = bool(args.work_dir)
    work = (Path(args.work_dir).resolve() if keep_work
            else Path(tempfile.mkdtemp(prefix="route_optimizer_verify_")))
    work.mkdir(parents=True, exist_ok=True)
    report_path = (Path(args.out).resolve() if args.out
                   else after_gds.parent / "verification.txt")

    lines = []

    def log(m):
        print(m)
        lines.append(m)

    _, layer_table = load_pdk_layers()
    lb = Layout(before_gds, map_b, layer_table)
    la = Layout(after_gds, map_a, layer_table)
    freeze_landings(lb)
    freeze_landings(la)
    failures = []

    log(f"=== verify_shortening: {after_gds.name} vs {before_gds.name} ===")

    # 1. map vs GDS ---------------------------------------------------------
    claimed = defaultdict(list)
    for net in la.nets.values():
        for it in net.rects():
            claimed[it.layer].append(it.box)
    drawn = defaultdict(list)
    for p in la.top.polygons:
        key = (p.layer, p.datatype)
        if key in la.layer_table:
            drawn[key].append(bbox(p.points))
    missing = extra = 0
    for lay in set(claimed) | set(drawn):
        pool = list(drawn.get(lay, ()))
        for box in claimed.get(lay, ()):
            hit = None
            for k, d in enumerate(pool):
                if max(abs(d[i] - box[i]) for i in range(4)) <= MATCH_TOL:
                    hit = k
                    break
            if hit is None:
                missing += 1
            else:
                pool.pop(hit)
        extra += len(pool)
    if missing or extra:
        failures.append(
            f"map/GDS disagreement: {missing} map rectangle(s) not drawn, "
            f"{extra} drawn rectangle(s) the map does not claim "
            f"(a leftover polygon is a short nothing else looks for)")
    log(f"  [{'FAIL' if (missing or extra) else 'PASS'}] output map and output "
        f"GDS describe the same metal ({missing} missing, {extra} unclaimed)")

    # 2. connectivity and landings -----------------------------------------
    log("  partitioning macro metal into nodes ...")
    parts_a, polys_a, sigs_a = macro_nodes(la)
    parts_b, polys_b, sigs_b = macro_nodes(lb)
    bad_conn, lost, moved = [], [], 0
    for name, net in la.nets.items():
        if not net.items:
            continue
        # Two pieces of wire that land on one macro node are connected. Give
        # each item a spare "region" tag per node it touches so
        # connected_groups() -- which already unions by region -- sees it.
        groups = defaultdict(list)
        for it in net.items:
            for sig in item_nodes(it, la, parts_a, polys_a, sigs_a):
                groups[sig].append(it)
        tag = 10 ** 6
        saved = {id(it): it.region for it in net.items}
        for sig, members in groups.items():
            for it in members:
                if it.region is None:
                    it.region = tag
            tag += 1
        # An item on two nodes needs both unions; do the extra ones by hand.
        pieces = connected_groups(net.items)
        index = {id(it): gi for gi, g in enumerate(pieces) for it in g}
        merge = list(range(len(pieces)))

        def root(i):
            while merge[i] != i:
                merge[i] = merge[merge[i]]
                i = merge[i]
            return i

        for members in groups.values():
            ids = {root(index[id(it)]) for it in members}
            ids = sorted(ids)
            for other in ids[1:]:
                merge[other] = ids[0]
        for it in net.items:
            it.region = saved[id(it)]
        if len({root(i) for i in range(len(pieces))}) != 1:
            bad_conn.append(name)

        old_net = lb.nets.get(name)
        if old_net is None:
            continue
        reached = set()
        for it in net.items:
            reached |= item_nodes(it, la, parts_a, polys_a, sigs_a)
        for it in old_net.items:
            if not it.frozen:
                continue
            same = any(
                lay in set(new.layers()) and new.box_on(lay) is not None and
                max(abs(new.box_on(lay)[i] - it.box_on(lay)[i])
                    for i in range(4)) <= MATCH_TOL
                for new in net.items for lay in it.layers())
            if same:
                continue
            # The rectangle is gone -- but a landing is a connection to a
            # NODE, not a claim on one rectangle. `route-optimizer` deliberately
            # moves bulk landings along the tie ring they sit on, so the
            # question is whether the net still reaches that same node.
            nodes = item_nodes(it, lb, parts_b, polys_b, sigs_b)
            if nodes and nodes <= reached:
                moved += 1
                continue
            lost.append(f"{name} @ {it.layers()[0]} "
                        f"{tuple(round(v, 3) for v in it.box)}")
    if bad_conn:
        failures.append(f"net(s) split into more than one piece, counting "
                        f"macro-internal metal: {', '.join(bad_conn)}")
    if lost:
        failures.append(f"{len(lost)} pin connection(s) from the original "
                        f"layout are gone: {lost[:3]}")
    log(f"  [{'FAIL' if bad_conn else 'PASS'}] every net is one connected piece "
        f"(macro-internal nodes counted)")
    log(f"  [{'FAIL' if lost else 'PASS'}] every original pin connection is "
        f"still made ({len(lost)} lost, {moved} moved along their own node)")

    # 3. cross-net shorts ---------------------------------------------------
    shorts = []
    for lay, entries in net_boxes(la).items():
        for i in range(len(entries)):
            ni, bi = entries[i]
            for j in range(i + 1, len(entries)):
                nj, bj = entries[j]
                if ni == nj:
                    continue
                if (bi[0] < bj[2] - 1e-4 and bj[0] < bi[2] - 1e-4 and
                        bi[1] < bj[3] - 1e-4 and bj[1] < bi[3] - 1e-4):
                    shorts.append((lay, ni, nj))
    if shorts:
        failures.append(f"{len(shorts)} same-layer overlap(s) between different "
                        f"nets: {shorts[:3]}")
    log(f"  [{'FAIL' if shorts else 'PASS'}] no two nets overlap on one layer "
        f"({len(shorts)} found)")

    # 4. the layouts extract to the same circuit ---------------------------
    if args.no_lvs:
        log("  [SKIP] extraction comparison (--no-lvs)")
    else:
        log("  extracting both layouts with Magic (slow) ...")
        sb = extract(before_gds, lb.top_name, work / "before", "before")
        sa = extract(after_gds, la.top_name, work / "after", "after")
        if not sb or not sa:
            failures.append(
                "Magic extraction failed for one of the layouts -- re-run "
                "with --work-dir <dir> to keep the logs and see why")
            log("  [FAIL] extraction")
        else:
            shutil.copy(sb, work / "before_extracted.spice")
            shutil.copy(sa, work / "after_extracted.spice")
            ok = netgen_compare("before_extracted.spice",
                                "after_extracted.spice",
                                lb.top_name, la.top_name, work, log)
            if ok is False:
                failures.append(
                    "the shortened layout does NOT extract to the same circuit "
                    "as the original -- connectivity changed; re-run with "
                    "--work-dir <dir> to keep netgen's full report")
            log(f"  [{'PASS' if ok else ('SKIP' if ok is None else 'FAIL')}] "
                f"the two layouts extract to the same circuit")

    # totals ----------------------------------------------------------------
    wb = sum(n.wirelength() for n in lb.nets.values())
    wa = sum(n.wirelength() for n in la.nets.values())
    vb = sum(len(n.vias()) for n in lb.nets.values())
    va = sum(len(n.vias()) for n in la.nets.values())
    log("")
    log(f"  wirelength {wb:.2f} -> {wa:.2f} um ({wa - wb:+.2f}, "
        f"{100.0 * (wa - wb) / wb if wb else 0:+.1f}%), vias {vb} -> {va}")
    log("")
    if failures:
        log("VERDICT: FAIL")
        for f in failures:
            log("  - " + f)
    else:
        log("VERDICT: PASS -- shorter wire, same circuit. DRC is "
            "shorten_routes.py's gate; LVS against the schematic remains "
            "layout-fixer's.")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n")
    if not keep_work:
        shutil.rmtree(work, ignore_errors=True)
    print(f"\nreport: {report_path}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
