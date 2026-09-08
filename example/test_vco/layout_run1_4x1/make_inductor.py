#!/usr/bin/env python3
"""Draw this design's two tank coils as ONE routable macro cell.

Why this script exists (all three reasons are flow gaps, not design choices):

1. `src/cells/primitives/inductor.py` is not a glayout primitive and no skill
   in the placer/router chain knows how to call it, so nothing upstream draws
   the coil at all.  `generate_primitives.py` saw `XL0 VDD voutp
   vco_spiral_ind` as kind="other" and emitted NO macro and NO device_index
   entry for it -- silently.
2. The generated spiral brings BOTH terminals out on GDS 72/20 (real met5).
   The router's stack is glayout met2..met5 == GDS 68..71 == real met1..met4,
   and `pdk_options.json` defines no met4->met5 via link, so a bare coil has
   NO terminal the router can reach.  This adds a real met4 (71/20) landing
   pad at each terminal, tied to the coil with a via4 (71/44) array built
   from the PDK's own via rules -- the same construct the generator already
   uses for its underpass.
     * terminal A (inner, net VDD): the met4 underpass IS terminal A already;
       it is only extended north to the lead tip so the landing point sits on
       the macro's edge instead of buried inside it.  No new via needed --
       the generator's own outer via array already ties underpass to lead.
     * terminal B (outer, net voutp/voutn): fresh met4 pad + via4 array under
       the lead tip at the macro's east edge.
3. The cell is named `vco_spiral_ind`, matching the netlist subckt, so
   netgen's `ignore class "-circuit1/2 vco_spiral_ind"` black-box lines apply
   to the same name on both sides of LVS.

Run:  python make_inductor.py [--out DIR]
"""
import argparse
import os
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "src", "cells", "primitives"))
sys.path.insert(0, os.path.join(REPO, ".claude", "reference"))

import gdstk                                    # noqa: E402
from pdk_config import pdk                      # noqa: E402
import inductor as ind_mod                      # noqa: E402

# The coil the frozen netlist's vco_spiral_ind pi-model was measured from.
TURNS, INNER, WIDTH, SPACE, SHAPE = 4.5, 100.0, 8.0, 2.0, "octagon"
CELL = "vco_spiral_ind"


def via_array(x0, y0, x1, y1, cut, space, enc, grid_um):
    """Cut rectangles filling [x0,x1]x[y0,y1] inset by `enc`, on `grid_um`."""
    def snap(v):
        return round(round(v / grid_um) * grid_um, 6)
    rects = []
    ax0, ay0, ax1, ay1 = x0 + enc, y0 + enc, x1 - enc, y1 - enc
    pitch = cut + space
    n_x = int((ax1 - ax0 + space) // pitch)
    n_y = int((ay1 - ay0 + space) // pitch)
    if n_x < 1 or n_y < 1:
        raise ValueError("landing pad too small for one via cut")
    span_x = n_x * pitch - space
    span_y = n_y * pitch - space
    bx = ax0 + ((ax1 - ax0) - span_x) / 2.0
    by = ay0 + ((ay1 - ay0) - span_y) / 2.0
    for i in range(n_x):
        for j in range(n_y):
            cx, cy = bx + i * pitch, by + j * pitch
            rects.append((snap(cx), snap(cy), snap(cx + cut), snap(cy + cut)))
    return rects


def build(out_gds):
    cfg = pdk()
    ind = cfg.require_inductor("drawing this design's tank coil")
    rules = ind["rules_um"]
    grid_um = ind["grid_nm"] / 1000.0
    L_COIL = tuple(ind["coil_layer"])
    L_UP = tuple(ind["underpass_layer"])
    L_VIA = tuple(ind["via_layer"])

    coil = ind_mod.spiral_inductor(n_turns=TURNS, inner_diameter=INNER,
                                   width=WIDTH, space=SPACE, shape=SHAPE,
                                   cell_name=CELL)
    cell = coil.cell

    def layer_bbox(layer):
        polys = [p for p in cell.polygons
                 if (p.layer, p.datatype) == layer]
        b = [p.bounding_box() for p in polys]
        return (min(q[0][0] for q in b), min(q[0][1] for q in b),
                max(q[1][0] for q in b), max(q[1][1] for q in b))

    tips = {lb.text: (lb.origin[0], lb.origin[1]) for lb in cell.labels}
    a_tip, b_tip = tips["A"], tips["B"]
    up = layer_bbox(L_UP)                       # the met4 underpass == net A
    hw = WIDTH / 2.0

    # --- terminal A: extend the underpass north to the lead tip -----------
    a_pad = (up[0], up[3], up[2], a_tip[1])
    cell.add(gdstk.rectangle((a_pad[0], a_pad[1]), (a_pad[2], a_pad[3]),
                             layer=L_UP[0], datatype=L_UP[1]))

    # --- terminal B: new met4 pad + via4 array under the outer lead tip ---
    coil_bb = layer_bbox(L_COIL)
    b_pad = (b_tip[0] - hw, b_tip[1] - 5.0, b_tip[0] + hw, b_tip[1])
    # keep the pad inside real drawn coil metal
    covered = gdstk.boolean(
        [gdstk.rectangle((b_pad[0], b_pad[1]), (b_pad[2], b_pad[3]))],
        [p for p in cell.polygons if (p.layer, p.datatype) == L_COIL],
        "not")
    if covered:
        raise SystemExit(
            f"terminal-B pad {b_pad} is not fully covered by coil metal "
            f"-- {len(covered)} uncovered region(s); shrink it")
    cell.add(gdstk.rectangle((b_pad[0], b_pad[1]), (b_pad[2], b_pad[3]),
                             layer=L_UP[0], datatype=L_UP[1]))
    for r in via_array(b_pad[0], b_pad[1], b_pad[2], b_pad[3],
                       rules["via_size"], rules["via_space"],
                       rules["via_enclosure_coil"], grid_um):
        cell.add(gdstk.rectangle((r[0], r[1]), (r[2], r[3]),
                                 layer=L_VIA[0], datatype=L_VIA[1]))

    coil.library.write_gds(out_gds)

    bb = cell.bounding_box()
    a_land = ((a_pad[0] + a_pad[2]) / 2.0, (a_pad[1] + a_pad[3]) / 2.0)
    b_land = ((b_pad[0] + b_pad[2]) / 2.0, (b_pad[1] + b_pad[3]) / 2.0)
    print(coil.report(freq_ghz=3.5))
    print(f"  cell name        : {CELL}")
    print(f"  cell bbox        : ({bb[0][0]:.3f},{bb[0][1]:.3f}) .. "
          f"({bb[1][0]:.3f},{bb[1][1]:.3f})  "
          f"{bb[1][0]-bb[0][0]:.3f} x {bb[1][1]-bb[0][1]:.3f} um")
    print(f"  A landing (met4) : {a_land[0]:.3f}, {a_land[1]:.3f}   "
          f"pad {a_pad}")
    print(f"  B landing (met4) : {b_land[0]:.3f}, {b_land[1]:.3f}   "
          f"pad {b_pad}")
    print(f"  wrote            : {out_gds}")
    return coil, bb, a_land, b_land


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "modules", f"{CELL}.gds"))
    ap.add_argument("--no-drc", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    build(a.out)
    if not a.no_drc:
        n, rules = ind_mod.run_drc(a.out, CELL)
        print(f"  Magic DRC        : {n} violation(s)"
              + ("" if not rules else "\n    " + "\n    ".join(rules)))
        if n:
            sys.exit(1)


if __name__ == "__main__":
    main()
