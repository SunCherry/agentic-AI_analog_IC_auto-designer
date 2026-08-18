#!/usr/bin/env python3
"""Assemble `anneal_placement.py`'s abstract macro positions into one real
GDS, viewable and editable directly in KLayout -- a visual sanity check on
the annealed floorplan before Step 3's legalization / `layout-agent`'s
routing pass.

(KLayout opens GDSII (`.gds`) and OASIS, not a "`.gdf`" format -- there is
no such standard layout format, so this writes `.gds`, same as every other
GDS this skill and the rest of this project produce.)

For each macro `anneal_placement.py` placed, re-imports its own GDS
(written by `generate_primitives.py`, via `manifest.json`'s `gds` path)
with `glayout.backend.import_gds()` and moves its **center** to
`(x + w/2, y + h/2)` -- matching `anneal_placement.py`'s own overlap math,
which treats `(x, y)` as each macro's bounding-box lower-left corner
regardless of where that macro's own internal glayout origin happens to
sit (`ComponentReference.move(destination=...)` moves relative to the
ref's *current* center, so this reproduces the annealer's abstract
geometry exactly, independent of each macro's own internal bbox-to-origin
offset). Also adds a `met5_label` instance-name label at each macro's
center, same label-layer convention as
`agents/scripts/redraw_layout.py`'s `label_device()`.

A macro in `placement_pos.json`'s `excluded_no_geometry` list (no
generated primitive -- see `generate_primitives.py`'s `"generation":
"manual"` case) has nothing to render; it's reported, not silently
dropped from the printed summary.

Usage:
  python render_placement.py <manifest.json> <placement_pos.json>
      [--out <design_dir>/placement_visualization.gds]
"""
import argparse
import json
import os
from pathlib import Path

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


os.environ.setdefault("PDK_ROOT", str(Path.home() / "pdk" / "manual"))
os.environ.setdefault("PDK", _guideline_pdk())

from glayout import sky130                          # noqa: E402
from glayout.backend import Component, import_gds    # noqa: E402

LABEL_LAYER = "met5_label"
LABEL_MAGNIFICATION = 4.0

# See ../../../reference/environment.md's "Paths" section -- a bare
# `klayout <file.gds>` renders every layer in generic/arbitrary colors,
# which makes dense multi-finger devices visually blur into what looks
# like a solid block at a glance ("just a bounding box"). Real device
# geometry is present in the GDS either way (verified with
# klayout.lay.LayoutView headless renders) -- this is a *viewing* problem,
# not a generation one, and loading the real sky130 layer-properties file
# fixes it. Not hardcoded as a silent assumption: only recommended if it
# actually exists on this machine.
# Anchored on this file, not on an absolute home-relative path, so it keeps
# working wherever the repo is checked out: this file is
# <repo>/.claude/skills/placer/script/render_placement.py, so parents[4] is
# the repo root that holds the vendored `PDK/` checkout.
_REPO = Path(__file__).resolve().parents[4]


def _layer_properties_file():
    """The active PDK's KLayout `.lyp`, from `tools.klayout_lyp` in
    `.claude/reference/pdk_options.json` -- the project's own PDK guideline,
    which is where a process-specific path belongs (see ../../../CLAUDE.md).
    This used to be a hardcoded sky130 path, which quietly stayed sky130 no
    matter what `selected` named. Returns None when the active PDK declares
    no `.lyp` (gf180mcuD currently does not) or the file isn't on this
    machine -- the caller then prints the generic 'load one if you have it'
    hint rather than a path that doesn't exist."""
    try:
        from pdk_config import pdk as _pdk   # path inserted by _guideline_pdk()
        rel = _pdk().tool("klayout_lyp")
    except Exception:
        return None
    if not rel:
        return None
    cand = Path(rel)
    if not cand.is_absolute():
        cand = _REPO / cand          # repo-relative, as written in the guideline
    return cand if cand.exists() else None


def render(manifest, placement, out_path: Path):
    gds_by_name = {m["name"]: m.get("gds") for m in manifest["macros"]}
    top = Component(name="placement_visualization")

    placed, skipped = [], []
    for name, pos in placement["positions"].items():
        gds_path = gds_by_name.get(name)
        if not gds_path or not Path(gds_path).exists():
            skipped.append(name)
            continue
        comp = import_gds(gds_path)
        ref = top << comp
        # pos["w"]/["h"] are already the EFFECTIVE (post-rotation) placed
        # dims anneal_placement.py wrote -- so this target center is where
        # the macro's center belongs regardless of orientation (rotating a
        # rectangle 90 degrees about its own center doesn't move the
        # center). Move first, then rotate about that same fixed point --
        # order doesn't matter for the end result, but this order keeps
        # the rotation call simple (center is already known).
        #
        # `origin=` must be the ref's OWN bbox centre, not left at its
        # default. `ComponentReference.move(destination=D)` defaults to
        # `origin=(0,0)`, which makes it a pure translate putting the
        # macro's glayout ORIGIN at D -- not its centre. For a macro whose
        # content is symmetric about its origin the two coincide and the
        # difference is invisible, which is why this went unnoticed: every
        # standalone device here is symmetric. The composites are not.
        # Measured on the reference design, the rendered macro sat displaced
        # from the box the annealer had reserved for it by exactly its
        # bbox-to-origin offset -- 6.040um for current_mirror_XMN4_XMN3_XMN5
        # (annealed x 146.543..193.893, drawn x 152.583..199.933), 0.435um
        # for diff_pair_XMN1_XMN2, 1.010um for current_mirror_XMP1_XMP2.
        # That silently invalidates the whole annealed floorplan: the
        # overlap-free, density-checked arrangement is not the one drawn,
        # and every real port lands somewhere the router does not expect.
        cx = pos["x"] + pos["w"] / 2
        cy = pos["y"] + pos["h"] / 2
        (rbx0, rby0), (rbx1, rby1) = ref.bbox
        ref.move(origin=((float(rbx0) + float(rbx1)) / 2,
                         (float(rby0) + float(rby1)) / 2),
                 destination=(cx, cy))
        if pos.get("rotated"):
            ref.rotate(90, center=(cx, cy))
        top.add_label(name, position=(cx, cy), layer=sky130.get_glayer(LABEL_LAYER),
                       magnification=LABEL_MAGNIFICATION)
        placed.append(name)

    for name in placement.get("excluded_no_geometry", []):
        if name not in placed and name not in skipped:
            skipped.append(name)

    top.write_gds(str(out_path))

    print(f"=== Rendered placement visualization: {out_path} ===")
    print(f"  placed: {len(placed)} macro(s): {', '.join(placed)}")
    if skipped:
        print(f"  NOT rendered (no generated geometry): {len(skipped)} macro(s): {', '.join(skipped)}")
    print(f"  overlap check already reported by anneal_placement.py "
          f"({'PASS' if placement.get('overlap_ok') else 'FAIL'})")
    lyp = _layer_properties_file()
    if lyp:
        print(f"  open in KLayout WITH real PDK layer colors (recommended -- a bare "
              f"`klayout {out_path}` shows flat/generic colors that make dense finger "
              f"structure look like a solid block):")
        print(f"    klayout -l \"{lyp}\" {out_path}")
    else:
        print(f"  open in KLayout to inspect/edit -- for readable layer colors, load this PDK's "
              f"layer-properties (.lyp) file if you have one (Layers panel > Load Layer "
              f"Properties, or `klayout -l <file.lyp> {out_path}`); without one, real device "
              f"geometry is still present but renders in flat/generic colors -- see "
              f"../../../reference/environment.md's \"Paths\" section.")
    return out_path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", help="manifest.json from generate_primitives.py")
    parser.add_argument("placement", help="placement_pos.json from anneal_placement.py")
    parser.add_argument("--out", default=None, help="default: <placement's dir>/placement_visualization.gds")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    placement_path = Path(args.placement).resolve()
    manifest = json.loads(manifest_path.read_text())
    placement = json.loads(placement_path.read_text())

    out_path = Path(args.out).resolve() if args.out else placement_path.parent / "placement_visualization.gds"
    render(manifest, placement, out_path)


if __name__ == "__main__":
    main()
