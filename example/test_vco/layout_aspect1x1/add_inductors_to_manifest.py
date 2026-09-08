#!/usr/bin/env python3
"""Fold this design's two tank coils into `primitives/manifest.json` as
first-class placeable macros.

Why this script exists (a flow gap, not a design choice)
-------------------------------------------------------
`generate_primitives.py` classifies `XL0 VDD voutp vco_spiral_ind` as
`kind="other"`: `netlist_devices.parse_devices()` only recognises
MODEL-NAMED devices, and a subckt call whose subckt body holds only
value-form `L`/`R`/`C` cards parses as zero devices.  It therefore emits
NO `macros` entry and NO `device_index` entry for either coil -- and it
does so **silently, exit 0**.  A placer run on that manifest floorplans
the 47x47um transistor core and never learns that two 196 x 213um coils
(41,748 um^2 each, 18.5x the whole rest of the design EACH) exist.

`../.claude/skills/placer/SKILL.md` Step 2 states the remedy verbatim:
"A macro composed by hand later must be folded into the manifest or Step 3
ignores it: its own `macros` entry (name/devices/w/h/gds + real `ports`),
the standalone entries for those devices removed, each device's `macro`
field in `device_index` repointed at it."  That is exactly what this does.
There were no standalone entries to remove -- the coils were absent
entirely, which is the more dangerous failure of the two.

Coordinates
-----------
Everything is read from `modules/vco_spiral_ind.gds` itself (written by
`make_inductor.py`), never hardcoded from a printout:

  * `w`/`h`          -- the cell's real bbox, 196.000 x 213.220 um.
  * `bbox_center_offset` -- the bbox centre in the cell's own frame,
    (0.000, 62.610).  The cell origin is NOT its bbox centre, and both
    `render_placement.py` and `route_nets.py::local_to_world()` place a
    macro by moving its BBOX CENTRE, so every port below is stored
    RELATIVE TO THAT CENTRE, matching `generate_primitives.py`'s
    convention.  Storing raw cell coordinates instead would put every
    landing 62.6um from the metal.
  * `ports`          -- the two met4 landing pads `make_inductor.py` adds.
    Found geometrically: the (71,20) rectangles that are NOT the long
    underpass spine, i.e. the pad at each labelled lead tip.  Reported on
    glayer **`met5`**, because glayout's stack is shifted one down in
    sky130 -- glayout `met5` resolves to GDS (71,20), the real met4.  That
    is the router's TOP routing layer, so both coil terminals are
    reachable without any met4->met5 via link (the PDK defines none).
    `width` is each pad's SHORTEST dimension (5.0um), the honest bar for
    `route_nets.py::filter_landable()`.
  * `keepout_um`     -- 0.4um, the PDK's own
    `inductor.rules_um.underpass_wide_metal_space` (met4.3).  The coil's
    met4 underpass is 8um wide, i.e. wide metal, so the general 0.3um
    met4 spacing the router would otherwise use is the wrong rule.

Net mapping (from the frozen netlist, `XL0 VDD voutp` / `XL1 VDD voutn`):
terminal A -> VDD on both coils; terminal B -> voutp (XL0) / voutn (XL1).
`device_index` uses the same two-terminal schema `generate_primitives.py`
gives a MiM cap: drain = first node, source = second, gate/bulk null.

Both macros reference the SAME GDS file.  That is deliberate: one drawn
cell, two placements -- identical coils are what a differential tank wants,
and it keeps the two instances matched by construction.

Idempotent: re-running replaces the XL0/XL1 entries rather than duplicating.

Run:  python add_inductors_to_manifest.py [--check-only]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, ".claude", "reference"))

import gdstk                                    # noqa: E402
from pdk_config import pdk                      # noqa: E402

CELL = "vco_spiral_ind"
COILS = [("XL0", "VDD", "voutp"), ("XL1", "VDD", "voutn")]
PORT_GLAYER = "met5"          # glayout name; resolves to GDS (71,20) = real met4


def coil_geometry(gds_path, up_layer):
    """(w, h, cx, cy, {label: (px, py, pad_w)}) read from the drawn cell."""
    lib = gdstk.read_gds(gds_path)
    cell = next(c for c in lib.cells if c.name == CELL)
    (x0, y0), (x1, y1) = cell.bounding_box()
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

    pads = []
    for p in cell.polygons:
        if (p.layer, p.datatype) != up_layer:
            continue
        (a0, b0), (a1, b1) = p.bounding_box()
        pads.append((a0, b0, a1, b1))
    if not pads:
        raise SystemExit(f"{gds_path}: no {up_layer} geometry -- the coil has no "
                         f"router-reachable landing pad; re-run make_inductor.py")

    # The landing pad nearest each labelled lead tip. The underpass spine is
    # also on this layer, so pick by distance to the label rather than by size.
    out = {}
    for lb in cell.labels:
        lx, ly = lb.origin[0], lb.origin[1]
        best = min(pads, key=lambda r: (max(r[0] - lx, lx - r[2], 0.0) ** 2
                                        + max(r[1] - ly, ly - r[3], 0.0) ** 2))
        px, py = (best[0] + best[2]) / 2.0, (best[1] + best[3]) / 2.0
        out[lb.text] = (px, py, min(best[2] - best[0], best[3] - best[1]))
    return (x1 - x0), (y1 - y0), cx, cy, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default=os.path.join(HERE, "primitives", "manifest.json"))
    ap.add_argument("--gds", default=os.path.join(HERE, "modules", f"{CELL}.gds"))
    ap.add_argument("--check-only", action="store_true")
    a = ap.parse_args()

    cfg = pdk()
    ind = cfg.require_inductor("folding this design's tank coils into the manifest")
    up_layer = tuple(ind["underpass_layer"])
    keepout = float(ind["rules_um"]["underpass_wide_metal_space"])

    w, h, cx, cy, pads = coil_geometry(a.gds, up_layer)
    print(f"{CELL}: {w:.3f} x {h:.3f} um   bbox centre ({cx:.3f}, {cy:.3f})")
    for t, (px, py, pw) in sorted(pads.items()):
        print(f"  terminal {t}: pad centre ({px:.3f}, {py:.3f}) cell frame "
              f"-> ({px - cx:.3f}, {py - cy:.3f}) local, min width {pw:.3f} um")

    man = json.load(open(a.manifest))
    names = {m["name"] for m in man["macros"]}
    new_macros = []
    for inst, net_a, net_b in COILS:
        pa, pb = pads["A"], pads["B"]
        new_macros.append({
            "name": inst,
            "kind": "inductor",
            "devices": [inst],
            "generation": "manual",
            "constraints": {"matched_with": [o for o, _, _ in COILS if o != inst]},
            "glayout_call": ("make_inductor.py -> src/cells/primitives/inductor.py"
                             "::spiral_inductor(n_turns=4.5, inner_diameter=100, "
                             "width=8, space=2, shape='octagon')"),
            "subckt_instance": CELL,
            "w": round(w, 6), "h": round(h, 6),
            "gds": os.path.abspath(a.gds),
            "ports": {
                net_a: [{"x": round(pa[0] - cx, 6), "y": round(pa[1] - cy, 6),
                         "layer": PORT_GLAYER, "width": round(pa[2], 6), "pin": "A"}],
                net_b: [{"x": round(pb[0] - cx, 6), "y": round(pb[1] - cy, 6),
                         "layer": PORT_GLAYER, "width": round(pb[2], 6), "pin": "B"}],
            },
            "bbox_center_offset": [round(cx, 6), round(cy, 6)],
            "keepout_um": keepout,
        })

    if a.check_only:
        missing = [n for n, _, _ in COILS if n not in names]
        print(f"\nmanifest has {len(man['macros'])} macro(s); "
              f"coils missing: {missing or 'none'}")
        return

    man["macros"] = [m for m in man["macros"] if m["name"] not in {n for n, _, _ in COILS}]
    man["macros"].extend(new_macros)
    for inst, net_a, net_b in COILS:
        man["device_index"][inst] = {
            "drain": net_a, "gate": None, "source": net_b, "bulk": None,
            "kind": "ind", "macro": inst,
        }
    json.dump(man, open(a.manifest, "w"), indent=1)

    md = os.path.join(os.path.dirname(a.manifest), "manifest.md")
    lines = [l for l in open(md).read().splitlines()
             if not any(l.startswith(f"- **{n}**") for n, _, _ in COILS)]
    at = max(i for i, l in enumerate(lines) if l.startswith("- **")) + 1
    lines[at:at] = [f"- **{m['name']}** ({m['kind']}, {m['generation']}): "
                    f"devices={m['devices']}, size={m['w']}x{m['h']}, gds={m['gds']}"
                    for m in new_macros]
    open(md, "w").write("\n".join(lines) + "\n")

    print(f"\nwrote {a.manifest} -- {len(man['macros'])} macros, "
          f"{len(man['device_index'])} devices")


if __name__ == "__main__":
    main()
