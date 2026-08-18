#!/usr/bin/env python3
"""Redraw a layout from <design_dir>/physical_map.json using glayout.

physical_map.json records, per device instance, its kind (nfet/pfet/cap/res),
sizing params (from the netlist: l, w, nf), and a placement bounding box
(x0/y0/x1/y1, in microns). It also records, per net, the exact routing
polygons ("segments": layer + points_um) that were extracted from the
original layout.

This script:
  1. Generates each device with glayout's own primitive cells (nmos/pmos/
     mimcap), sized from physical_map's recorded params, and centers each
     one within its recorded placement bounding box.
  2. Redraws every net's routing verbatim: physical_map's segments are
     already absolute-coordinate polygons, so they're replayed as-is.
  3. Writes <design_dir>/<design>_redrawn.gds.

Caveats (see printed warnings per design):
  - The recorded device bounding boxes are placement *sites* (they include
    routing margin), not tight component footprints -- e.g. a recorded
    24x24um mimcap's site is 28.38x31.5um. glayout's generated device won't
    exactly fill that box, and the original generation flags (with_tie,
    with_dummy, guard rings, ...) aren't recoverable from physical_map, so
    device footprints are best-effort, not pixel-exact. Routing IS
    pixel-exact, since it's replayed directly from recorded polygons.
  - "res" devices have no matching glayout primitive: glayout's own
    resistor() builds a diode-connected pfet, not the physical
    diffusion/poly resistor these designs actually use. Resistors are
    drawn as a plain placeholder rectangle spanning their recorded box.

Takes design directory PATHS, one or more -- the same contract as this
skill's extract_physical_info.py and redraw_from_map.py, so any design
anywhere can be redrawn, not only one sitting under a fixed example/ root.

Usage (from the repo root):
  python redraw_layout.py example/miller_ota
  python redraw_layout.py example/miller_ota example/ota_ff
  python redraw_layout.py --no-dummy example/miller_ota   # no dummy fingers
"""
import os
import sys
import json
import argparse
from pathlib import Path

HERE = Path(__file__).resolve().parent

# The active PDK comes from the project-wide guideline, never from a literal
# here -- see .claude/reference/pdk_options.json ("selected").
_REPO_ROOT = HERE.parents[3]
sys.path.insert(0, str(_REPO_ROOT / ".claude" / "reference"))
from pdk_config import pdk                                   # noqa: E402

PDK = pdk()
os.environ.setdefault("PDK_ROOT", PDK.pdk_root)
os.environ.setdefault("PDK", PDK.pdk_env)

import importlib                                             # noqa: E402
from glayout.backend import Component                        # noqa: E402
from glayout.primitives.fet import nmos, pmos                # noqa: E402
from glayout.primitives.mimcap import mimcap                 # noqa: E402

# glayout exposes one module per process ('sky130', 'gf180'); which one is a
# PDK fact, so it is looked up by name rather than imported literally.
try:
    PDK_MODULE = getattr(importlib.import_module("glayout"), PDK.glayout_module)
except AttributeError:
    raise SystemExit(
        f"glayout has no '{PDK.glayout_module}' module, which "
        f"{PDK.name} selects in .claude/reference/pdk_options.json")

M_PER_UM = 1e-6


def build_device(dev: dict, with_dummy: bool = True) -> Component | None:
    kind = dev["kind"]
    params = dev["params"]
    if kind == "nfet":
        return nmos(
            PDK_MODULE,
            width=params["w"] / M_PER_UM,
            length=params["l"] / M_PER_UM,
            fingers=params.get("nf", 1),
            multipliers=1,
            with_tie=False,
            with_dummy=with_dummy,
            with_substrate_tap=False,
            with_dnwell=False,
        )
    if kind == "pfet":
        return pmos(
            PDK_MODULE,
            width=params["w"] / M_PER_UM,
            length=params["l"] / M_PER_UM,
            fingers=params.get("nf", 1),
            multipliers=1,
            with_tie=False,
            with_dummy=with_dummy,
            with_substrate_tap=False,
            dnwell=False,
        )
    if kind == "cap":
        return mimcap(PDK_MODULE, size=(params["w"] / M_PER_UM, params["l"] / M_PER_UM))
    return None  # "res": no matching glayout primitive, handled by caller


# A glayer the generated geometry never uses, so labels render in a colour
# distinct from the device/routing layers. Which layer that is depends on the
# process, so it comes from the guideline's layers.label_glayer.
LABEL_LAYER = PDK.label_glayer
LABEL_MAGNIFICATION = 4.0   # bigger than the default (1.0) glyph size


def label_device(top: Component, dev: dict) -> None:
    cx = (dev["x0_um"] + dev["x1_um"]) / 2
    cy = (dev["y0_um"] + dev["y1_um"]) / 2
    top.add_label(
        dev["instance"], position=(cx, cy),
        layer=PDK_MODULE.get_glayer(LABEL_LAYER), magnification=LABEL_MAGNIFICATION,
    )


def place_centered(top: Component, device: Component, dev: dict) -> None:
    cx = (dev["x0_um"] + dev["x1_um"]) / 2
    cy = (dev["y0_um"] + dev["y1_um"]) / 2
    ref = top << device
    ref.move(destination=(cx, cy))
    label_device(top, dev)


def draw_resistor_placeholder(top: Component, dev: dict) -> None:
    x0, y0, x1, y1 = dev["x0_um"], dev["y0_um"], dev["x1_um"], dev["y1_um"]
    points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    top.add_polygon(points, layer=PDK_MODULE.get_glayer("met2"))
    label_device(top, dev)


def find_netlist(design_dir: Path) -> Path | None:
    design_name = design_dir.name
    numbered = sorted(design_dir.glob(f"{design_name}_[0-9]*.sp"))
    if numbered:
        return numbered[-1]
    bare = design_dir / f"{design_name}.sp"
    if bare.exists():
        return bare
    return None


def draw_routing(top: Component, physical_map: dict) -> int:
    count = 0
    for net in physical_map["nets"]:
        for seg in net["segments"]:
            top.add_polygon(seg["points_um"], layer=tuple(seg["layer"]))
            count += 1
    return count


def redraw_design(design_dir: Path, with_dummy: bool = True) -> dict:
    design_dir = design_dir.resolve()
    map_path = design_dir / "physical_map.json"
    if not map_path.exists():
        raise FileNotFoundError(f"no physical_map.json in {design_dir}")
    physical_map = json.loads(map_path.read_text())

    design_name = physical_map["design"]
    netlist_path = find_netlist(design_dir)
    print(f"\n=== Redrawing {design_name} ({design_dir.name}) ===")
    print(f"  netlist: {netlist_path.name if netlist_path else '(none found)'}")

    top = Component(name=f"{design_name}_redrawn")

    counts = {"nfet": 0, "pfet": 0, "cap": 0, "res_placeholder": 0}
    for dev in physical_map["devices"]:
        kind = dev["kind"]
        if kind == "res":
            draw_resistor_placeholder(top, dev)
            counts["res_placeholder"] += 1
            continue
        device = build_device(dev, with_dummy=with_dummy)
        if device is None:
            print(f"  warning: unsupported device kind {kind!r} for {dev['instance']}, skipping")
            continue
        place_centered(top, device, dev)
        counts[kind] += 1

    # num_segments = draw_routing(top, physical_map)

    out_path = design_dir / f"{design_name}_redrawn.gds"
    top.write_gds(str(out_path))

    print(f"  devices: {counts['nfet']} nfet, {counts['pfet']} pfet, "
          f"{counts['cap']} cap, {counts['res_placeholder']} res (placeholder)")
    # print(f"  routing: {num_segments} polygons redrawn from {len(physical_map['nets'])} nets")
    print(f"  wrote {out_path}")

    return {
        "design": design_name,
        "gds_path": str(out_path),
        "netlist_path": str(netlist_path) if netlist_path else None,
        "counts": counts,
        # "num_segments": num_segments,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "design_dirs", nargs="+", metavar="design_dir",
        help="one or more design directories, each holding a physical_map.json",
    )
    parser.add_argument(
        "--dummy", dest="with_dummy", action=argparse.BooleanOptionalAction, default=True,
        help="add dummy fingers to nmos/pmos devices (default: on; disable with --no-dummy)",
    )
    args = parser.parse_args()

    # Real paths, the same contract as extract_physical_info.py and
    # redraw_from_map.py. This used to take a bare folder *name* resolved
    # under a hardcoded example/ root, which could not reach a design
    # anywhere else and pointed at a directory that does not exist.
    design_dirs = [Path(d).resolve() for d in args.design_dirs]
    missing = [str(d) for d in design_dirs if not d.is_dir()]
    if missing:
        sys.exit("no such design directory: " + ", ".join(missing))

    results = []
    for design_dir in design_dirs:
        try:
            results.append(redraw_design(design_dir, with_dummy=args.with_dummy))
        except Exception as e:
            print(f"\n=== {design_dir.name}: ERROR: {e} ===")
            results.append({"design": design_dir.name, "error": str(e)})

    print("\n=== SUMMARY ===")
    for r in results:
        if "error" in r:
            print(f"{r['design']:30s} error: {r['error']}")
        else:
            netlist_name = Path(r["netlist_path"]).name if r["netlist_path"] else "(none found)"
            print(f"{r['design']:30s} netlist={netlist_name:25s} -> {r['gds_path']}")


if __name__ == "__main__":
    main()