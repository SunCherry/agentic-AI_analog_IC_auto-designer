#!/usr/bin/env python3
"""Generate a placement grid for a design -- a reference layout-agent uses
to decide where a new device can go (see ../SKILL.md's "Placement Grid").

The grid's extent is the design's existing placement bounding box
(computed from physical_map.json's device list), expanded by a margin
fraction on all four sides (default 10%) so there's room to place new
devices beyond the current footprint.

The grid's cell pitch (uniform in x and y) is the finest achievable
resolution across all metal layers: the minimum, over every metal
glayer, of (min_width + min_separation) -- queried from the PDK's own
design rules, not hardcoded, since this differs per PDK/process node.
E.g. if met1 has min_width=0.5um and min_separation=0.6um, and every
other metal layer's sum is larger, the grid pitch is 0.5+0.6=1.1um.

Each cell is classified:
  used    -- overlaps an existing device's bounding box.
  blocked -- adjacent to a used cell (a one-cell keepout halo -- since
             the grid pitch already encodes one layer's
             min_width+min_separation, a cell touching a used cell is
             too close to place a new device without violating that
             layer's spacing rule).
  free    -- safe to consider for new placement. Still verify the actual
             candidate device's real footprint fits before committing --
             this grid is a coarse placement reference, not a DRC
             guarantee.

Usage:
  python generate_grid.py <design_dir> [--pdk sky130] [--margin 0.10]

  e.g. python generate_grid.py <design_dir>

Reads <design_dir>/physical_map.json (device x0_um/y0_um/x1_um/y1_um,
written by extract_physical_info.py). Writes
<design_dir>/placement_grid.json.
"""
import argparse
import json
import math
import os
import sys

def _guideline_pdk(default="sky130A", allowed=None):
    """Active PDK name from `.claude/reference/pdk_options.json`.

    The project selects one PDK there; this is how a script picks that up
    instead of hardcoding a process. Falls back to `default` if the guideline
    cannot be read, or names a PDK this script has no table for, so a
    standalone run still works.
    """
    import os as _os, sys as _sys
    try:
        _ref = _os.path.abspath(_os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)),
            *([_os.pardir] * 3), "reference"))
        if _ref not in _sys.path:
            _sys.path.insert(0, _ref)
        from pdk_config import pdk as _pdk
        name = _pdk().name
    except Exception:
        return default
    return name if (allowed is None or name in allowed) else default


os.environ.setdefault("PDK_ROOT", os.path.expanduser("~/pdk/manual"))
os.environ.setdefault("PDK", _guideline_pdk())

from glayout import sky130, gf180

PDKS = {"sky130": sky130, "gf180": gf180}

ASCII_SYMBOLS = {"free": ".", "blocked": "x", "used": "#"}
MAX_ASCII_DIM = 200  # skip the ASCII render for grids bigger than this either way


def metal_glayers(pdk):
    return [g for g in pdk.valid_glayers
            if g.startswith("met") and not g.endswith(("_pin", "_label"))]


def finest_grid_pitch(pdk):
    """Return (pitch_um, layer_name, {layer: pitch_um for every metal layer}) --
    pitch = min_width + min_separation, minimized across all metal layers."""
    pitches = {}
    for g in metal_glayers(pdk):
        rule = pdk.get_grule(g)
        pitches[g] = rule["min_width"] + rule["min_separation"]
    if not pitches:
        raise RuntimeError("no metal glayers found on this PDK")
    finest_layer = min(pitches, key=pitches.get)
    return pitches[finest_layer], finest_layer, pitches


def load_device_boxes(design_dir):
    """Placed boxes from whichever file the design has.

    Two producers, one schema: an annealed placement carries each macro's
    `x0_um`/`y0_um`/`x1_um`/`y1_um` inside `placement_pos.json`'s own
    `positions` entries (it no longer writes a separate physical_map.json --
    the same numbers in two files could only drift), while an EXTRACTED
    layout still supplies `physical_map.json` with a `devices` list. Both are
    read here, placement_pos.json first, so neither producer has to know about
    the other."""
    pos_path = os.path.join(design_dir, "placement_pos.json")
    map_path = os.path.join(design_dir, "physical_map.json")
    boxes = []
    if os.path.isfile(pos_path):
        data = json.load(open(pos_path))
        entries = [dict(dev, instance=name)
                   for name, dev in (data.get("positions") or {}).items()]
    elif os.path.isfile(map_path):
        entries = json.load(open(map_path))["devices"]
    else:
        raise FileNotFoundError(
            f"no placement_pos.json (an annealed placement) and no "
            f"physical_map.json (an extracted layout) in {design_dir} -- "
            f"nothing to build a grid from"
        )
    for dev in entries:
        if all(k in dev for k in ("x0_um", "y0_um", "x1_um", "y1_um")):
            boxes.append({
                "instance": dev["instance"],
                "x0": dev["x0_um"], "y0": dev["y0_um"],
                "x1": dev["x1_um"], "y1": dev["y1_um"],
            })
    if not boxes:
        raise ValueError(
            f"no devices with placement coordinates in {design_dir} -- "
            f"nothing to build a grid from"
        )
    return boxes


def overall_bbox(boxes):
    x0 = min(b["x0"] for b in boxes)
    y0 = min(b["y0"] for b in boxes)
    x1 = max(b["x1"] for b in boxes)
    y1 = max(b["y1"] for b in boxes)
    return x0, y0, x1, y1


def expand_with_margin(bbox, margin_frac):
    x0, y0, x1, y1 = bbox
    mx = (x1 - x0) * margin_frac
    my = (y1 - y0) * margin_frac
    return x0 - mx, y0 - my, x1 + mx, y1 + my


def _rects_overlap(a, b):
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    return not (ax1 <= bx0 or bx1 <= ax0 or ay1 <= by0 or by1 <= ay0)


def build_grid(boxes, grid_bbox, pitch):
    gx0, gy0, gx1, gy1 = grid_bbox
    n_cols = max(1, math.ceil((gx1 - gx0) / pitch))
    n_rows = max(1, math.ceil((gy1 - gy0) / pitch))

    # cells[row][col]; row 0 = bottom (min y), col 0 = left (min x)
    cells = [["free"] * n_cols for _ in range(n_rows)]

    def cell_rect(row, col):
        cx0 = gx0 + col * pitch
        cy0 = gy0 + row * pitch
        return cx0, cy0, cx0 + pitch, cy0 + pitch

    used_cells = set()
    for row in range(n_rows):
        for col in range(n_cols):
            rect = cell_rect(row, col)
            if any(_rects_overlap(rect, (b["x0"], b["y0"], b["x1"], b["y1"]))
                   for b in boxes):
                cells[row][col] = "used"
                used_cells.add((row, col))

    # one-cell keepout halo around every used cell -- see module docstring
    for (row, col) in used_cells:
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                r, c = row + dr, col + dc
                if 0 <= r < n_rows and 0 <= c < n_cols and cells[r][c] == "free":
                    cells[r][c] = "blocked"

    return cells, n_rows, n_cols


def print_ascii(cells):
    for row in reversed(cells):  # top of printout = max y
        print("".join(ASCII_SYMBOLS[c] for c in row))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design_dir")
    ap.add_argument("--pdk", choices=sorted(PDKS), default="sky130")
    ap.add_argument("--margin", type=float, default=0.10,
                     help="fraction of bbox size to pad on each side (default 0.10 = 10%%)")
    ap.add_argument("--no-save", action="store_true",
                     help="report the grid (counts, bbox, pitch, ASCII preview when small "
                          "enough) without writing placement_grid.json. For a caller that only "
                          "needs the legality verdict and does not want the artifact left "
                          "behind -- a re-run reproduces it from the same placement anyway")
    args = ap.parse_args()

    pdk = PDKS[args.pdk]
    try:
        boxes = load_device_boxes(args.design_dir)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(f"error: {e}")
    bbox = overall_bbox(boxes)
    grid_bbox = expand_with_margin(bbox, args.margin)
    pitch, finest_layer, all_pitches = finest_grid_pitch(pdk)

    cells, n_rows, n_cols = build_grid(boxes, grid_bbox, pitch)

    n_free = sum(row.count("free") for row in cells)
    n_blocked = sum(row.count("blocked") for row in cells)
    n_used = sum(row.count("used") for row in cells)

    print(f"=== Placement grid: {args.design_dir} ===")
    print(f"PDK: {args.pdk}  |  per-layer (min_width+min_separation) um: "
          f"{ {k: round(v, 3) for k, v in all_pitches.items()} }")
    print(f"Grid pitch: {pitch:.4f} um (from '{finest_layer}', the finest layer)")
    print(f"Placement bbox (devices only): "
          f"({bbox[0]:.2f}, {bbox[1]:.2f}) - ({bbox[2]:.2f}, {bbox[3]:.2f}) um")
    print(f"Grid bbox (+{args.margin * 100:.0f}% margin): "
          f"({grid_bbox[0]:.2f}, {grid_bbox[1]:.2f}) - "
          f"({grid_bbox[2]:.2f}, {grid_bbox[3]:.2f}) um")
    print(f"Grid: {n_rows} rows x {n_cols} cols "
          f"({n_free} free, {n_blocked} blocked, {n_used} used)")
    print()
    if n_rows <= MAX_ASCII_DIM and n_cols <= MAX_ASCII_DIM:
        print_ascii(cells)
    else:
        print(f"(grid too large for an ASCII preview -- "
              f"{n_rows}x{n_cols} exceeds {MAX_ASCII_DIM}x{MAX_ASCII_DIM}"
              + (")" if args.no_save else "; read placement_grid.json instead)"))

    if args.no_save:
        print("\n--no-save: placement_grid.json not written (the counts above are "
              "the legality verdict)")
        return

    out_path = os.path.join(args.design_dir, "placement_grid.json")
    with open(out_path, "w") as f:
        json.dump({
            "design_dir": args.design_dir,
            "pdk": args.pdk,
            "grid_pitch_um": pitch,
            "grid_pitch_source_layer": finest_layer,
            "per_layer_pitch_um": all_pitches,
            "margin_fraction": args.margin,
            "device_bbox_um": {"x0": bbox[0], "y0": bbox[1], "x1": bbox[2], "y1": bbox[3]},
            "grid_bbox_um": {"x0": grid_bbox[0], "y0": grid_bbox[1],
                              "x1": grid_bbox[2], "y1": grid_bbox[3]},
            "n_rows": n_rows,
            "n_cols": n_cols,
            "cells": cells,
        }, f, indent=2)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
