"""Add a real text label at every individual DEVICE's own drain/gate/source
(MOS), P/N (resistor), or top/bottom (cap) terminal onto an already-routed
`routed.gds` -- e.g. "XMN4_drain", not just the net-name label
`route_nets.py`'s own `render_gds()` already draws. That existing labeling
is net-granularity (one label per net) and macro-granularity (one label per
placed macro) -- see router/SKILL.md's "Each placed macro ALSO gets its own
instance-name label" section for why: this router never tracks where an
individual device sits inside its own macro's GDS, only
`generate_primitives.py`'s own per-device port extraction has that, and only
for the few nets it exposes near-edge ports for. This script closes that gap
for VISUALIZATION purposes (a human looking at the GDS asking "which of
these two transistors is M1?"), not for LVS pin promotion -- labels land on
a fixed generic annotation layer (`met5_label`, same layer/purpose the
existing instance-name labels already use), not the real per-terminal metal
`route_nets.py`'s own net labels are placed on.

How device ports are found: `generate_primitives.py` never wrote a full
per-device port list into manifest.json (its own `current_mirror_ports()`/
`diff_pair_ports()` only keep the few nets that clear the router's near-edge
margin). This script instead regenerates each macro's LIVE Component
in-process, the exact same way `generate_primitives.py`'s own `generate()`
does (same `build_device_table()`, `detect_topology.detect()`,
`gen_current_mirror()`/`gen_diff_pair()`/`build_leftover()` calls) --
guaranteed to reproduce identical geometry (this whole toolchain is only
deterministic with `gf.CONF.n_threads = 1`, set below, same as every other
script here) -- which restores the full, un-filtered `.ports` dict every
glayout primitive actually carries in memory (lost on GDS round-trip,
confirmed directly: `import_gds()` returns zero ports).

Per-device port-name convention, confirmed by directly inspecting each
generator's real `.ports` dict, not guessed:
  - `cells/current_mirror.py`: `fetA_<drain|gate|source|well>_<NSEW>` for
    the reference device (manifest `devices[0]`), `fetB<i>_<...>_<NSEW>`
    (i = 0..mirror_ratio-1) for the mirror-leg device (manifest
    `devices[1]`) -- `mirror_ratio` copies of the SAME circuit device, so
    every `fetB<i>` gets the SAME device-name label at its own physical
    location (real silicon, not a duplicate). `fetB_<...>` (no index) is
    just an alias to the LAST `fetB<i>` -- skipped here to avoid a
    duplicate label at an already-labeled point.
  - `cells/diff_pair.py`: common-centroid layout -- confirmed directly from
    `diff_pair()`'s own `add_ports()` calls: `tl_`/`br_` = device A
    (manifest `devices[0]`), `tr_`/`bl_` = device B (manifest `devices[1]`).
    Each again gets the SAME device-name label at both of its physical
    locations.
  - A standalone single-device macro (`single_nfet`/`single_pfet`/
    `current_mirror_leg`, built directly by `nmos()`/`pmos()`, no
    composite-cell prefix): `<drain|gate|source|well>_<NSEW>` un-prefixed --
    the whole macro IS that one device (`devices[0]`).
  - `single_res` (`build_resistor()`): `p_top_met_<NSEW>`/
    `n_top_met_<NSEW>` -- labeled `<name>_p`/`<name>_n`.
  - `single_cap` (`mimcap()`): `top_met_<NSEW>`/`bottom_met_<NSEW>` --
    labeled `<name>_top`/`<name>_bottom`.
One representative port per (device, terminal) is labeled (whichever of
E/N/W/S exists first) -- multi-finger devices expose many more raw
per-finger/per-contact ports (`multiplier_0_...`, `dummy_...`,
`array_row0_col0_...`) that all belong to the SAME merged terminal
electrically; labeling all of those would bury the GDS in redundant text,
so only the clean top-level merged-terminal port is used.

Usage:
    python script/label_device_ports.py <design_dir>
        [--manifest <design_dir>/primitives/manifest.json]
        [--placement <design_dir>/placement_pos.json]
        [--routed <design_dir>/routed.gds]
        [--out <design_dir>/routed.gds]  (default: overwrite --routed in place)
        [--no-dummy]

`--no-dummy` must match whatever `generate_primitives.py` was run with for
this design. Macros are regenerated in-process here (see above), so a
mismatch regenerates DIFFERENT geometry than the `routed.gds` being
labeled, and every label lands at a coordinate that does not correspond to
the drawn device.
"""
import argparse
import json
import re
import sys
from pathlib import Path

import gdstk

SCRIPT_DIR = Path(__file__).resolve().parent
SKILLS_DIR = SCRIPT_DIR.parent.parent
# SKILLS_DIR is `.claude/skills`, so the repo root is two levels up, not one:
# `SKILLS_DIR.parent / "cells"` resolves to `.claude/cells`, which does not
# exist. That was latent rather than fatal only because the `cells/` modules
# are imported by `generate_primitives` (below), which sets up its own path --
# but anything importing from `cells/` directly here would have failed.
REPO = SKILLS_DIR.parent.parent
# `src/cells/` is where the generators actually live (`src/cells/primitives/`,
# `src/cells/blocks/`); `REPO/"cells"` does not exist. Legacy root location
# kept as a fallback -- same fix as
# ../../placer/script/generate_primitives.py's own CELLS_DIR.
CELLS_DIR = REPO / "src" / "cells"
if not CELLS_DIR.is_dir():
    CELLS_DIR = REPO / "cells"
sys.path.insert(0, str(SKILLS_DIR / "placer" / "script"))
sys.path.insert(0, str(SKILLS_DIR.parent / "reference"))
sys.path.insert(0, str(SKILLS_DIR / "schematic-sizing" / "script"))
sys.path.insert(0, str(CELLS_DIR))

import gdsfactory as gf
gf.CONF.n_threads = 1  # see module docstring / placer/SKILL.md Step 2 -- required for reproducibility

import generate_primitives as gp  # noqa: E402
import detect_topology as topo    # noqa: E402
from glayout.backend import Component  # noqa: E402

# The active process's glayout PDK, taken from `generate_primitives`'s own
# `PDK` (resolved by name from `.claude/reference/pdk_options.json`) rather
# than a hardcoded `from glayout.pdk.sky130_mapped import ...`. Reusing THAT
# object, not a fresh import, is what guarantees the labels are placed for
# the same process the macros below are regenerated with.
PDK = gp.PDK  # noqa: E402

# A real `*_label` glayer -- NOT the same thing route_nets.py uses for its own
# instance names. That script deliberately labels instances on a raw,
# extraction-inert (layer, datatype) so `port makeall` can never promote them
# and corrupt net identity; a `*_label` glayer is exactly the layer class that
# DOES get promoted. These labels sit over macro geometry rather than routed
# metal, but the distinction is not "inert" -- see ../SKILL.md's "Optional"
# section: run this on a copy if the GDS is about to be extracted.
INSTANCE_LABEL_LAYER = "met5_label"
LABEL_MAGNIFICATION = 3.0

CLEAN_PORT_RE = re.compile(r"^(?P<prefix>.*?)(?P<terminal>drain|gate|source|well)_(?P<side>[NSEW])$")
SIDE_ORDER = ["E", "N", "W", "S"]


def rebuild_macros(manifest, with_dummy=True):
    """Reproduces `generate_primitives.py`'s own `generate()` macro-building
    loop (same functions, same order) to recover each macro's LIVE Component
    -- see module docstring for why this is necessary (ports don't survive
    the GDS round-trip). Returns {macro_name: (kind, devices, component)}."""
    netlist_path = Path(manifest["netlist"])
    dev_by_name, topo_devices = gp.build_device_table(netlist_path)
    findings, unclassified = topo.detect(topo_devices)
    placed_devices = set()
    result = {}

    for finding in findings:
        topology = finding["topology"]
        all_mos_or_bjt_recognized = all(
            dev_by_name[d]["kind"] in gp.MOS_KINDS for d in finding["devices"]
        )
        if topology == "current_mirror" and all_mos_or_bjt_recognized:
            # gen_current_mirror() returns (macros, deferred, note): a leg
            # whose ratio the cell cannot express is NOT in any macro and
            # must fall through to the standalone pass below, so mark only
            # the devices the macros actually draw as placed.
            cm_macros, _deferred, _note = gp.gen_current_mirror(
                finding, dev_by_name, with_dummy=with_dummy)
            for m in cm_macros:
                result[m["name"]] = (m["kind"], m["devices"], m["component"])
            placed_devices.update(d for m in cm_macros for d in m["devices"])
        elif topology == "differential_pair" and all_mos_or_bjt_recognized:
            for m in gp.gen_diff_pair(finding, dev_by_name, with_dummy=with_dummy):
                result[m["name"]] = (m["kind"], m["devices"], m["component"])
            placed_devices.update(finding["devices"])

    leftover_names = [n for n in unclassified if n not in placed_devices]
    leftover_names += [n for n, d in dev_by_name.items()
                        if d["kind"] in ("cap", "res") and n not in placed_devices]
    for name in leftover_names:
        dev = dev_by_name[name]
        comp, _generation = gp.build_leftover(dev, with_dummy=with_dummy)
        if comp is not None:
            result[name] = (f"single_{dev['kind']}", [name], comp)
    return result


def device_terminal_points(kind, devices, comp):
    """(label_text, port_name) pairs for every (device, terminal) this
    macro's live Component actually has a clean, un-prefixed-clutter port
    for -- see module docstring for the exact per-kind prefix convention."""
    ports = set(comp.ports.keys())
    out = []

    def pick(prefix, terminal_names, dev_name, label_suffix_map=None):
        for term in terminal_names:
            for side in SIDE_ORDER:
                pname = f"{prefix}{term}_{side}"
                if pname in ports:
                    suffix = (label_suffix_map or {}).get(term, term)
                    out.append((f"{dev_name}_{suffix}", pname))
                    break

    if kind == "current_mirror":
        a_name, b_name = devices
        pick("fetA_", ("drain", "gate", "source", "well"), a_name)
        i = 0
        while any(p.startswith(f"fetB{i}_") for p in ports):
            pick(f"fetB{i}_", ("drain", "gate", "source", "well"), b_name)
            i += 1
    elif kind == "current_mirror_leg":
        pick("", ("drain", "gate", "source", "well"), devices[0])
    elif kind == "differential_pair":
        a_name, b_name = devices
        pick("tl_", ("drain", "gate", "source", "well"), a_name)
        pick("br_", ("drain", "gate", "source", "well"), a_name)
        pick("tr_", ("drain", "gate", "source", "well"), b_name)
        pick("bl_", ("drain", "gate", "source", "well"), b_name)
    elif kind in ("single_nfet", "single_pfet"):
        pick("", ("drain", "gate", "source", "well"), devices[0])
    elif kind == "single_res":
        for prefix, suffix in (("p_top_met", "p"), ("n_top_met", "n")):
            for side in SIDE_ORDER:
                pname = f"{prefix}_{side}"
                if pname in ports:
                    out.append((f"{devices[0]}_{suffix}", pname))
                    break
    elif kind == "single_cap":
        for prefix, suffix in (("top_met", "top"), ("bottom_met", "bottom")):
            for side in SIDE_ORDER:
                pname = f"{prefix}_{side}"
                if pname in ports:
                    out.append((f"{devices[0]}_{suffix}", pname))
                    break
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("design_dir")
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--placement", default=None)
    parser.add_argument("--routed", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--no-dummy", dest="with_dummy", action="store_false", default=True)
    args = parser.parse_args()

    design_dir = Path(args.design_dir).resolve()
    manifest_path = Path(args.manifest) if args.manifest else design_dir / "primitives" / "manifest.json"
    placement_path = Path(args.placement) if args.placement else design_dir / "placement_pos.json"
    routed_path = Path(args.routed) if args.routed else design_dir / "routed.gds"
    out_path = Path(args.out) if args.out else routed_path

    manifest = json.loads(manifest_path.read_text())
    placement = json.loads(placement_path.read_text())["positions"]

    print(f"=== Rebuilding {len(manifest['macros'])} macro(s) live (for real per-device ports) ===")
    live = rebuild_macros(manifest, with_dummy=args.with_dummy)

    labels_top = Component(name="device_port_labels")
    n_labels = 0
    n_macros_labeled = 0
    for mname, p in placement.items():
        if mname not in live:
            continue
        kind, devices, comp = live[mname]
        pairs = device_terminal_points(kind, devices, comp)
        if not pairs:
            continue
        n_macros_labeled += 1
        ref = labels_top << comp
        cx, cy = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
        ref.move(destination=(cx, cy))
        if p.get("rotated"):
            ref.rotate(90, center=(cx, cy))
        for text, pname in pairs:
            pt = ref.ports[pname]
            labels_top.add_label(text, position=pt.center, layer=PDK.get_glayer(INSTANCE_LABEL_LAYER),
                                  magnification=LABEL_MAGNIFICATION)
            n_labels += 1
    print(f"  {n_labels} device-terminal label(s) across {n_macros_labeled} macro(s)")

    tmp_gds = design_dir / "_device_port_labels_tmp.gds"
    labels_top.write_gds(str(tmp_gds))

    lib_routed = gdstk.read_gds(str(routed_path))
    lib_labels = gdstk.read_gds(str(tmp_gds))
    routed_cell = next(c for c in lib_routed.cells if c.name == "routed")
    labels_cell = next(c for c in lib_labels.cells if c.name == "device_port_labels")
    for lab in labels_cell.labels:
        routed_cell.add(gdstk.Label(lab.text, lab.origin, layer=lab.layer, texttype=lab.texttype,
                                     magnification=lab.magnification))
    lib_routed.write_gds(str(out_path))
    tmp_gds.unlink()
    print(f"  wrote {out_path} ({n_labels} new device-terminal labels merged into the existing routed geometry)")


if __name__ == "__main__":
    main()
