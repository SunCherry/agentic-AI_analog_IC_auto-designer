#!/usr/bin/env python3
"""Build `physical_map.json` from a placer+router design, without extraction.

`extract_physical_info.py` recovers a physical map by READING a finished GDS,
and it only understands two cell-naming conventions (hash-based and
ALIGN-style). A layout this project generates itself -- `../../placer/SKILL.md`
then `../../router/SKILL.md` -- uses glayout cell names (`current_mirror_*`,
`via_stack_*`), which match neither. Pointed at such a design it emits a
structurally valid map in which every device has `null` position, `null`
rotation and `null` primitive_cell, every net reads `"source": "unmatched"`
with no routing, and it exits 0. Confirmed on a real routed design: 0/11
devices placed, 10/10 nets unmatched.

The information is not missing, though -- it never needed extracting, because
the tools that PLACED and ROUTED the design already wrote it down:

    placement_pos.json   x/y, the placed box, rotation, post-rotation w/h
    primitives/manifest.json   device -> macro, per-pin nets, kind, params
    routes.json          per-net wire polygons and via positions

So this script transcribes those into the same schema `extract_physical_info.py`
emits, rather than round-tripping through geometry recognition that is
guaranteed to fail. Same output contract, same consumers
(`redraw_from_map.py`, `../../placement-optimizer/script/generate_grid.py`,
`detect_placement_style.py`, `../../../agents/layout-fixer.md`).

**Layer names are translated through the PDK, not copied.** `routes.json`
names glayout *glayers*; `physical_map.json` records real (GDS layer,
datatype) pairs. These are offset in sky130 -- glayer `met3` is GDS (69, 20),
i.e. the process's met2 -- so the mapping goes through `pdk.get_glayer()`.
Copying the name across would mislabel every routed polygon by one layer.

Usage:
  python physical_map_from_placement.py <design_dir> [--netlist NAME]
      [--layout NAME] [--out PATH]

`<design_dir>` is the folder holding `placement_pos.json` and `primitives/`
(i.e. `<design>/layout`). Writes `<design_dir>/physical_map.json`.
"""
import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE.parent.parent
sys.path.insert(0, str(SKILLS_DIR.parent / "reference"))

# Terminal order used by physical_map's `endpoints[].terminal_index`, matching
# extract_physical_info.py's own ordering.
TERMINAL_INDEX = {"drain": 0, "gate": 1, "source": 2, "bulk": 3,
                  "plus": 0, "minus": 1}
PIN_KEYS = ("drain", "gate", "source", "bulk", "plus", "minus")


def load_pdk():
    """The active process's glayout PDK, for glayer -> (layer, datatype)."""
    from pdk_config import pdk as pdk_option
    cfg = pdk_option()
    os.environ.setdefault("PDK_ROOT", cfg.pdk_root or "")
    os.environ.setdefault("PDK", cfg.pdk_env)
    import gdsfactory as gf
    gf.CONF.n_threads = 1          # see ../../placer/SKILL.md Step 2
    import glayout
    return getattr(glayout, cfg.glayout_module), cfg


def closed(points):
    """physical_map polygons are closed rings; routes.json leaves them open."""
    pts = [list(p) for p in points]
    if pts and pts[0] != pts[-1]:
        pts.append(list(pts[0]))
    return pts


def build(design_dir, netlist_name=None, layout_name=None):
    dd = Path(design_dir).resolve()
    pos_path = dd / "placement_pos.json"
    man_path = dd / "primitives" / "manifest.json"
    for p in (pos_path, man_path):
        if not p.exists():
            raise SystemExit(f"missing {p} -- this script needs a placer/router "
                             f"design dir (the one holding placement_pos.json "
                             f"and primitives/manifest.json)")
    placement = json.loads(pos_path.read_text())
    manifest = json.loads(man_path.read_text())
    positions = placement.get("positions", {})
    device_index = manifest.get("device_index", {})

    routes_path = dd / "routes.json"
    routes = json.loads(routes_path.read_text()) if routes_path.exists() else None
    if routes is None:
        print("  note: no routes.json -- nets will carry endpoints but no "
              "routing geometry (run the router first for a complete map)")

    pdk, cfg = load_pdk()
    macro_by_name = {m["name"]: m for m in manifest.get("macros", [])}

    # -- devices -----------------------------------------------------------
    # A device's box is its MACRO's box: a composed macro (current mirror,
    # diff pair) places several devices in one cell, and the placer records
    # geometry per macro, not per device. Recorded honestly as such rather
    # than inventing a per-device sub-box the placement never determined.
    devices = []
    for inst, entry in sorted(device_index.items()):
        macro = entry.get("macro")
        pos = positions.get(macro)
        mac = macro_by_name.get(macro, {})
        nets = [entry[k] for k in PIN_KEYS if entry.get(k)]
        rec = {
            "instance": inst,
            "kind": entry.get("kind"),
            "params": {k: v for k, v in entry.items()
                       if k in ("w", "l", "nf", "m")} or None,
            "nets": list(dict.fromkeys(nets)),
            "primitive_cell": macro,
            "macro_shared_by": sorted(d for d, e in device_index.items()
                                      if e.get("macro") == macro),
            "width_um": pos.get("w") if pos else mac.get("w"),
            "height_um": pos.get("h") if pos else mac.get("h"),
            "is_dummy": False,
            # The placer tracks rotation as a bool (90 deg swaps w/h; 180/270
            # give the same box) -- see anneal_placement.py. Report what was
            # actually recorded, not a precise angle it never chose.
            "rotation_deg": (90 if pos.get("rotated") else 0) if pos else None,
            "mirror": False,
            "geometry_source": "placement_pos.json",
        }
        if pos:
            rec.update({k: pos[k] for k in ("x0_um", "y0_um", "x1_um", "y1_um")
                        if k in pos})
        devices.append(rec)

    # -- nets --------------------------------------------------------------
    endpoints = {}
    for inst, entry in sorted(device_index.items()):
        for pin in PIN_KEYS:
            net = entry.get(pin)
            if not net:
                continue
            endpoints.setdefault(net, []).append({
                "instance": inst, "terminal": pin,
                "terminal_index": TERMINAL_INDEX.get(pin, 0)})

    route_map = (routes or {}).get("routes", {})
    nets = []
    for net in sorted(endpoints):
        segments, vias = [], []
        for seg in route_map.get(net, []):
            if seg.get("kind") == "wire":
                segments.append({"layer": list(pdk.get_glayer(seg["layer"])),
                                 "glayer": seg["layer"],
                                 "points_um": closed(seg["points_um"])})
            elif seg.get("kind") == "via":
                vias.append({"x_um": seg["x_um"], "y_um": seg["y_um"],
                             "from_layer": list(pdk.get_glayer(seg["from_layer"])),
                             "to_layer": list(pdk.get_glayer(seg["to_layer"])),
                             "from_glayer": seg["from_layer"],
                             "to_glayer": seg["to_layer"]})
        nets.append({
            "name": net,
            "source": "router" if net in route_map else "unrouted",
            "endpoints": endpoints[net],
            "segments": segments,
            "vias": vias,
        })

    return {
        "design": manifest.get("design") or dd.parent.name,
        "netlist": netlist_name or manifest.get("netlist"),
        "layout": layout_name or ("routed.gds" if (dd / "routed.gds").exists()
                                  else "placement_visualization.gds"),
        "format": "placer_router",
        "pdk": cfg.name,
        "pairs": [],
        "devices": devices,
        "nets": nets,
        "dummy_check": {"netlist_dummies": [], "unclaimed_primitive_cells": []},
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design_dir")
    ap.add_argument("--netlist", default=None)
    ap.add_argument("--layout", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    payload = build(args.design_dir, args.netlist, args.layout)
    out = Path(args.out) if args.out else Path(args.design_dir) / "physical_map.json"
    out.write_text(json.dumps(payload, indent=2))

    placed = sum(1 for d in payload["devices"] if d.get("x0_um") is not None)
    routed = sum(1 for n in payload["nets"] if n["segments"])
    print(f"  devices : {placed}/{len(payload['devices'])} with a real placed box")
    print(f"  nets    : {routed}/{len(payload['nets'])} with routing geometry "
          f"({sum(len(n['segments']) for n in payload['nets'])} polygons, "
          f"{sum(len(n['vias']) for n in payload['nets'])} vias)")
    print(f"  wrote   : {out}")
    # Same non-degeneracy bar layout-fixer is told to assert before shipping.
    if not placed:
        sys.exit("ERROR: no device got a placed box -- refusing to call this a "
                 "usable physical map")


if __name__ == "__main__":
    main()
