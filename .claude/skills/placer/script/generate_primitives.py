#!/usr/bin/env python3
"""Generate real glayout primitives for every subcircuit/device in a frozen
netlist, and a manifest describing them -- the input `anneal_placement.py`
needs for top-level simulated-annealing placement.

Two inputs, one mandatory:
  - the frozen netlist, parsed by `schematic-sizing/script/netlist_devices.py`
    for every device's terminals and `w`/`l`/`nf`/`m` (see
    `build_device_table`);
  - `<design_dir>/circuit_decomposition.yaml`, the user-confirmed circuit
    read, which is the ONLY source of pattern grouping -- read via
    `decomposition_patterns.py`. This script runs no structural detection of
    its own; without the yaml every device becomes a standalone primitive
    and it says so loudly.

Generation policy, per pattern:
  - `current_mirror`: ONE call, covering the reference and every mirror
    leg, to this project's own `cells/blocks/current_mirror.py::current_mirror()`
    (imported as `build_current_mirror` -- NOT glayout's own
    elementary-cell composite; see the `CELLS_DIR` sys.path override right
    after the license-y import block below). That generator takes
    `mirror_ratio` as a per-branch LIST, so an N-leg mirror is a single
    cell holding the reference plus all N legs, and the macro's `devices`
    is `[reference, leg1, leg2, ...]`. Keeping the legs together is the
    point of a current mirror: inside one cell they share the reference's
    gate bias and bulk by construction (matched, abutted, one guard ring)
    rather than depending on the router to reconnect them. `--split-mirror-legs`
    restores the older one-macro-per-leg behavior -- see
    `gen_current_mirror()`'s own docstring for why that split existed
    (glayout's elementary cell could only draw a single A/B pair) and why
    it is no longer needed.
  - `differential_pair`: one call to `cells/blocks/diff_pair.py::diff_pair()`
    (imported as `build_diff_pair`, same override).
  - Any other pattern the circuit read names (cascode mirror, FVF, quad
    pair, transmission gate, an RC compensation network, or a BJT
    mirror/pair -- no dedicated module for any of these) is NOT
    auto-composed into one matched macro in this pass. Each constituent
    device instead gets its own standalone primitive (same as an ordinary
    leftover device), and the pattern is recorded in the manifest's
    `manual_composition` list so those standalone primitives can be replaced
    with a properly hand-composed, matching-aware macro later, before this
    becomes a final layout. Reported, never a silent gap.
  - Every device no pattern claimed gets a standalone primitive:
    `cells/primitives/fet.py`'s own `nmos`/`pmos`, that same directory's
    `mimcap`, or for `res` a real poly resistor
    device (`build_resistor()` -- hand-drawn from raw poly + the PDK's
    own resistor-recognition marker, since no primitive draws
    one; glayout's own `resistor()` is a diode-connected pfet, not a
    physical resistor -- see that function's docstring for the exact
    layer stack and how it was verified against real Magic extraction,
    not assumed). BJT leftovers still have no primitive available at all
    (`generation: "manual"`).

Current-mirror ratio, no longer approximated: glayout's own `current_mirror()`
is a 1:1 interdigitized pair (`numcols` folds BOTH devices by the same
factor -- see `src/glayout/placement/two_transistor_interdigitized.py`'s
`two_nfet_interdigitized()` docstring), which used to force a netlist ratio
(differing `m` between reference and mirror leg) into a `round(leg_m /
ref_m)` best-effort column-folding approximation. `cells/blocks/current_mirror.py`'s
own `current_mirror()` takes a `mirror_ratio` instead: that many *unit-sized*
copies of the mirror device in a row beside a single reference unit -- a
literal N:1 ratio. `numcols = round(leg_m / ref_m)` is still how this script
derives the number to pass in (netlist `m` is still the only ratio signal
available), but it's now honored exactly, not approximated further. See
`../SKILL.md`'s Step 1 "Now wired in" note for the real, checked
consequences of this generator swap (near-edge port availability per
macro kind, and a real DRC scaling gap found at mirror_ratio>1 + fingers>1
+ substrate_tap=True).

Usage:
  python generate_primitives.py <netlist.sp> [--out-dir DIR]
      [--decomposition circuit_decomposition.yaml] [--no-dummy] [--per-device]

  If given a directory instead, looks for `<dirname>_final.sp` then
  `<dirname>.sp` inside it. Default `--out-dir` is
  `<design_dir>/primitives/`.

`--per-device`: ignore the circuit read's patterns entirely
-- every MOS/cap/res device becomes its OWN standalone macro (the exact
same `build_leftover()` path an ordinary leftover device already takes,
just applied to EVERY device, not only the ones no finding claimed), so
`anneal_placement.py` places individual `nmos()`/`pmos()`/`mimcap()`/
`build_resistor()` primitives directly rather than pre-grouped
`current_mirror()`/`diff_pair()` macros. No `current_mirror`/
`differential_pair`/`manual_composition` entries at all in this mode --
`manifest.json`'s `macros` list is flat, one entry per device, each with
its own independent `constraints: {}` (no `centroid_match`/`symmetry`
hint -- those only make sense at the subcircuit level this mode
deliberately skips). BJT devices still get `generation: "manual"` (no
glayout primitive covers them, same as ever -- see `build_leftover()`).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE.parent.parent
REPO = SKILLS_DIR.parent.parent
# Must come before `from glayout import ...` below, and take priority (index
# 0) over site-packages: `glayout` is also pip-installed as an editable
# package pointing at a DIFFERENT, external clone
# (~/Documents/work/external_ai/gLayout) that does not carry this repo's own
# glayout fixes (e.g. current_mirror.py/diff_pair.py's `_add_edge_breakout`)
# -- confirmed by a real import-resolution check this session, not assumed.
# Same convention already used by the committed layout scripts.
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(SKILLS_DIR / "schematic-sizing" / "script"))
sys.path.insert(0, str(SKILLS_DIR.parent / "reference"))
# Session override: use the hand-built cells/ generators (edge-wrapped
# primitives.fet nmos/pmos/mimcap, and cells/blocks/current_mirror.py +
# cells/blocks/diff_pair.py's own current_mirror()/diff_pair()) instead of
# glayout's own composites. Two path entries are needed: `cells/` for the
# `primitives` package (it has an __init__.py), and `cells/blocks/` for the
# composite modules, which live there and are NOT a package.
# `src/cells/`, not `cells/`: the generators live under `src/` (see
# `src/cells/primitives/fet.py`, `src/cells/blocks/current_mirror.py`).
# Pointing this at `REPO/"cells"` -- a path that does not exist -- made every
# run of this script die at `from primitives.fet import nmos, pmos` with
# ModuleNotFoundError, since nothing else on sys.path exposes a top-level
# `primitives` package. The legacy location is kept as a fallback so a
# checkout that still has `cells/` at the root keeps working.
CELLS_DIR = REPO / "src" / "cells"
if not CELLS_DIR.is_dir():
    CELLS_DIR = REPO / "cells"
BLOCKS_DIR = CELLS_DIR / "blocks"
sys.path.insert(0, str(CELLS_DIR))
sys.path.insert(0, str(BLOCKS_DIR))

import netlist_devices as devparse      # noqa: E402
import subckt_macros                    # noqa: E402 -- decomposed-netlist reader (see generate()'s subckt branch)
import decomposition_patterns           # noqa: E402 -- circuit_decomposition.yaml reader, the ONLY source of pattern grouping
from pdk_config import pdk as pdk_option  # noqa: E402 -- the project's active-PDK accessor


def metal_glayers(pdk):
    """This PDK's real routing-metal glayers, asked of the PDK rather than
    listed here -- so a process with a different stack depth needs no edit."""
    return [g for g in pdk.valid_glayers
            if g.startswith("met") and not g.endswith(("_pin", "_label"))]


# The active PDK, from `.claude/reference/pdk_options.json` -- the project
# selects one process there and nothing here hardcodes it (../../../CLAUDE.md).
PDK_CFG = pdk_option()
os.environ.setdefault("PDK_ROOT", PDK_CFG.pdk_root or str(Path.home() / "pdk" / "manual"))
os.environ.setdefault("PDK", PDK_CFG.pdk_env)

import gdsfactory as gf
# Must come before any `glayout`/gdsfactory Component is built. Real,
# confirmed root cause of a severe reproducibility bug: gdsfactory
# defaults to `n_threads=8` (parallel geometry construction), and this
# whole toolchain's cell construction (glayout's current_mirror()/
# diff_pair() in particular) is NOT thread-safe against it -- confirmed
# directly by running this exact script twice, same netlist, same flags,
# and getting BYTE-DIFFERENT `current_mirror_XMN4_XMN3.gds` output (one
# run: 19 Magic DRC met1.2 violations; the other: the same 19 plus 24 new
# LU.2 violations, from literally identical source). Forcing single-
# threaded construction here made two independent full re-runs of this
# script produce bit-identical GDS across every generated primitive
# (verified via direct gdstk polygon diff, not assumed) -- an earlier,
# weaker attempt at this fix (pinning `PYTHONHASHSEED` alone) looked like
# it worked in some tests but did NOT reliably hold up under further
# testing; this does. See `../SKILL.md`'s Step 1 and
# `../../router/SKILL.md`'s "KNOWN OPEN GAP" section for the fuller
# investigation. Slower (no parallelism) but correctness beats speed for
# a primitive-generation step that runs once per design, not in a loop.
gf.CONF.n_threads = 1

import glayout                                                  # noqa: E402
from glayout.backend import Component                          # noqa: E402
from glayout.primitives.via_gen import via_stack                # noqa: E402
from glayout.util.comp_utils import evaluate_bbox               # noqa: E402
# Session override (see CELLS_DIR note above): single/leftover devices come
# from cells/primitives/fet.py's own nmos/pmos/mimcap (not glayout's), and
# the current_mirror/differential_pair subcircuits come from this project's
# own cells/blocks/current_mirror.py + cells/blocks/diff_pair.py, not glayout's elementary
# cells module. Aliased so the rest of this file's call sites don't need to
# change beyond the actual signature differences (handled at each call site).
from primitives.fet import nmos, pmos                            # noqa: E402
from primitives.mimcap import mimcap                             # noqa: E402
from current_mirror import current_mirror as build_current_mirror  # noqa: E402
from diff_pair import diff_pair as build_diff_pair                # noqa: E402
# Instance-name annotation (text-only on met5_label, LVS/DRC-neutral by
# construction -- see either function's own docstring). Both modules export
# the same function name, so alias per cell type. Without these the
# generated macros carry NO per-device names at all: `render_placement.py`
# labels each placed block with its MACRO name, which for a composite says
# "current_mirror_XMN4_XMN3_XMN5" over the whole cell and never identifies
# which physical transistor inside it is XMN4 vs XMN3 vs XMN5.
from current_mirror import add_instance_labels as label_cm_instances  # noqa: E402
from diff_pair import add_instance_labels as label_dp_instances       # noqa: E402

# The glayout MappedPDK for the selected process (`sky130`, `gf180`,
# `ihp130`), resolved by NAME from pdk_options.json's `glayout_module` rather
# than imported as `from glayout import sky130`. Everything below draws
# through this object's own `get_glayer()`/`get_grule()`, so retargeting the
# process is the one-word edit to that file the project's PDK rule promises.
try:
    PDK = getattr(glayout, PDK_CFG.glayout_module)
except AttributeError:
    raise SystemExit(
        f"glayout has no PDK module named {PDK_CFG.glayout_module!r} (from "
        f"pdk_options.json's 'glayout_module' for {PDK_CFG.name}). Available: "
        f"{[n for n in dir(glayout) if n in ('sky130', 'gf180', 'ihp130')]}")

# (layer, datatype) of the resistor-recognition marker a drawn poly resistor
# needs -- the poly glayer's own GDS layer number paired with this PDK's
# `resistor_marker_datatype` from pdk_options.json. sky130 spells it
# `calma POLYRES 66 13` in its Magic tech file, which is where the 13 comes
# from; the 66 is asked of the PDK, never typed here. `None` for a PDK whose
# marker datatype is not filled in -- `build_resistor()` then refuses to draw
# a resistor rather than marking it on a guessed layer (see there).
def _resistor_marker_layer():
    dt = PDK_CFG.resistor_marker_datatype
    if dt is None:
        return None
    return (int(PDK.get_glayer("poly")[0]), int(dt))


RESISTOR_MARKER_LAYER = _resistor_marker_layer()

# Lowest routing metal this process has -- what a poly resistor's end
# contacts land on, and what "met1" used to be hardcoded as.
LOWEST_METAL = metal_glayers(PDK)[0]

MOS_KINDS = {"nfet", "pfet"}
BJT_KINDS = {"npn", "pnp"}
# Nets that are a supply/bias tie rather than a signal net. Carried into
# manifest.json and read by `anneal_placement.py`, which excludes them from
# HPWL: a rail touches nearly every macro, so counting it would drag the
# whole floorplan toward one centroid. Held here rather than imported,
# because `detect_topology.py` -- where this set used to live -- is no
# longer a dependency of this skill (see `decomposition_patterns.py`).
SUPPLY_RAIL_NAMES = {"vdd", "vss", "gnd", "vcc", "vee", "avdd", "avss", "dvdd", "dvss",
                     "vbulk", "vnb", "vpb"}
# Macro kinds that are a COMPOSED SUBCIRCUIT rather than a single device --
# built from a pattern the circuit read named, via `cells/blocks/`. Their GDS
# goes to `--modules-dir` when one is given (../SKILL.md's Step 0 layout).
# `current_mirror_leg` is deliberately absent: `--split-mirror-legs` emits it
# as a plain standalone fet, so it belongs with the primitives.
COMPOSITE_MACRO_KINDS = {"current_mirror", "differential_pair"}
NO_AUTO_MODULE_TOPOLOGIES = {
    "cascode_current_mirror", "flipped_voltage_follower",
    "quad_pair", "transmission_gate",
}

# Real routing-metal glayers, queried from the PDK (not a listed met1..met5)
# so port extraction below can turn a glayout Port's raw (layer, datatype)
# tuple back into a glayer NAME a downstream router can use. Ports on any
# other layer (poly, active, well/tap markers) are not something a router can
# land a wire on -- filtered out below.
ROUTING_METAL_BY_TUPLE = {tuple(PDK.get_glayer(name)): name
                           for name in metal_glayers(PDK)}

# How close (um) a real glayout Port must sit to its OWN component's bbox
# edge to be trusted as a router landing point (see near_edge_ports()'s
# docstring for why this matters, not just an arbitrary tolerance).
PORT_EDGE_MARGIN_UM = 1.5


def tag_pin(points, pin):
    """Stamp each extracted port with the TERMINAL it came from.

    `../../router/script/route_nets.py` lands ONE point per (net, macro).
    That is right when a macro's ports for a net are all the same piece of
    metal (a tap ring's compass quad), and wrong when they are not: a
    diode-connected device has its gate and drain on ONE netlist node but
    on two physically separate pieces of metal, and a current mirror gives
    each branch its own source stub. Landing one of them leaves the rest
    floating -- measured on the reference design, that is exactly what left
    XMP4's drain and both mirrors' source stubs unconnected in extraction
    (4 of the 7 nets LVS could not match). With this tag the router can
    land one point per (net, macro, TERMINAL) instead."""
    out = []
    for p in points:
        q = dict(p)
        q["pin"] = pin
        out.append(q)
    return out


def near_edge_ports(comp, pattern):
    """Real glayout Ports on `comp` whose name matches `pattern`, kept ONLY
    if they sit within PORT_EDGE_MARGIN_UM of comp's own bbox edge.

    Why the margin, not every matching port: a device's raw `drain_S`/
    `gate_S`/`source_S`-style ports (see build_fet()) sit deep INSIDE the
    macro's own bounding box -- verified this session by direct inspection
    (nmos bbox extends to +-4.88/+-3.635um, but drain/gate/source ports sit
    at +-0.4/+-1.5um, ~2-3um inside). `../../router/script/route_nets.py`
    treats every macro's interior as fully blocked (no feed-through -- see
    its own module docstring); if this function handed such a point to the
    router as a landing cell, the router would need to cut an ad hoc
    corridor from deep inside the macro out to its edge, risking a real
    short across whatever real metal (e.g. the substrate/well tap ring)
    happens to lie in between -- not a hypothetical, confirmed by direct
    inspection of real port coordinates during this session's own
    development. Ports glayout ALREADY routes close to the edge (composite
    diff_pair() ports built from real internal `c_route()` wires -- see
    that module's PLUSgateroute_*/drain_route*_con_* ports -- and every
    with_substrate_tap=True tie/welltie/substrate_tap ring's own
    `_top_met_` contacts, which sit right at the guard ring's outer edge by
    construction) DO land within this margin, verified the same way. Ports
    that don't clear the margin are simply omitted here -- callers fall
    back to `../../router/script/route_nets.py`'s existing (safe, if
    imprecise) box-edge approximation for that net, same as before this
    function existed."""
    (x0, y0), (x1, y1) = comp.bbox
    seen = set()
    result = []
    for name, port in comp.ports.items():
        if not re.match(pattern, name):
            continue
        layer = ROUTING_METAL_BY_TUPLE.get(tuple(port.layer))
        if layer is None:
            continue
        x, y = float(port.center[0]), float(port.center[1])
        if min(x - x0, x1 - x, y - y0, y1 - y) > PORT_EDGE_MARGIN_UM:
            continue
        key = (round(x, 4), round(y, 4), layer)
        if key in seen:
            continue
        seen.add(key)
        result.append({"x": x, "y": y, "layer": layer, "width": float(port.width)})
    return result


def fet_ports(comp, dev):
    """Real near-edge (gate/drain/source) landing points for a standalone
    build_fet() Component -- `dev`'s drain/gate/source net names, mapped to
    whichever of that pin's 4 compass-duplicate ports (see build_fet()'s
    own port dump: `{pin}_{N,S,E,W}`, all the same physical location, just
    4 orientation copies) clears near_edge_ports()'s margin. In practice
    the raw gate/drain/source ports never clear the margin either
    (confirmed by inspection -- see near_edge_ports()'s docstring), so
    these nets fall back to ../../router/script/route_nets.py's box-edge
    approximation same as before this function existed -- this function
    mainly matters for macro kinds where a net's OTHER macro does have a
    usable port (e.g. a diff_pair()'s drain landing on this fet's own
    boundary point still benefits from the diff_pair side being real).

    Deliberately NOT extracting `dev['bulk']` via the `tie_*` ring here
    (an earlier version of this function did): build_fet() draws BOTH a
    `with_tie=True` ring (real, polarity-matched device bulk tap) AND a
    SEPARATE, larger `with_substrate_tap=True` ring OUTSIDE it (see
    build_fet()'s own docstring) -- any corridor from the inner tie ring
    out to the macro's bbox edge has to physically cross that outer ring.
    Confirmed by a real LVS regression this session's own development hit:
    with the tie-ring corridor enabled, `VDD`'s extracted net absorbed ALL
    5 nfet substrate/bulk pins (`nfet/4=5`) alongside the real pfet bulks
    (`pfet/4=4`) -- a genuine short between VDD and the chip's shared
    p-substrate plane, not just a DRC nuisance. Excluding bulk here avoids
    that corridor entirely; VDD/VSS distribution still gets real
    multi-macro connectivity from `main()`'s `include_supply=True` (via
    `device_index`'s ordinary drain/gate/source pins), just landing on the
    same box-edge approximation every other net without a real port
    uses -- safe, if less precise."""
    pts = {}
    # `bulk` is back, via the `bulk_bo_*` stub. The note above describes why
    # it used to be excluded -- the only bulk access was the INNER tie ring,
    # and a corridor from there to the macro edge had to cross the outer
    # substrate ring, shorting VDD to the shared substrate. That is no longer
    # how bulk leaves this cell: ../../../../cells/primitives/fet.py's
    # `_stretch_terminals_to_edge()` runs the tie ring's own south bar out to
    # the macro's OUTERMOST edge, after every ring, so there is nothing left
    # to cross. Without it the nwell tie is simply never connected and both
    # standalone pfets extract with their bulk on the substrate instead of
    # VDD -- measured: `pfet/4 = 2` on VSS where the golden has 4 on VDD.
    for pin in ("gate", "drain", "source", "bulk"):
        net = dev.get(pin)
        if not net:
            continue
        if pin == "bulk":
            found = near_edge_ports(comp, r"^bulk_bo_[NSEW]$")
            if found:
                pts.setdefault(net, []).extend(tag_pin(found, "bulk"))
            continue
        # Prefer a real edge BREAKOUT port when the generator made one
        # (`cells/primitives/fet.py`'s `_stretch_terminals_to_edge()`, which
        # BOTH nmos() and pmos() now run by default): those are the terminal
        # run out to the macro's own outline -- drain west, gate and source
        # east -- which is exactly the near-edge landing point this function
        # otherwise fails to find. Fall back to the raw compass-duplicate
        # ports for any device without them, where near_edge_ports() will
        # still reject them for sitting too deep inside -- same box-edge
        # fallback as before.
        found = near_edge_ports(comp, rf"^multiplier_\d+_{pin}_bo_[NSEW]$")
        if not found:
            found = near_edge_ports(comp, rf"^{pin}_[NSEW]$")
        if found:
            pts.setdefault(net, []).extend(tag_pin(found, pin))
    return pts


def res_ports(comp, dev):
    """Real near-edge P/N landing points for a standalone build_resistor()
    Component -- dev's `drain`/`source` fields hold the resistor's P/N
    terminal net names (build_device_table()'s res-merge convention:
    `nets[0]` -> `drain`, `nets[1]` -> `source`: `dev_by_name`'s field
    names are MOS-shaped but reused for every device kind). Maps onto build_resistor()'s own
    `p_top_met_*`/`n_top_met_*` via_stack ports (met1, real and near-edge
    by construction -- `contact_tab/2` in from the poly's own drawn edge,
    see that function's docstring), same real-port pattern `fet_ports()`
    uses for gate/drain/source. **Real, confirmed gap this fixes**: before
    this function existed, `main()`'s port-assignment switch had no case
    for `single_res` at all, so every standalone resistor macro (e.g.
    the reference design's `R0`) got `"ports": {}` despite
    `build_resistor()` drawing real top/bottom terminal ports -- routing
    fell back entirely to `../../router/script/route_nets.py`'s
    `pin_point()` box-edge approximation, which picks whichever side faces
    the net's other macro (frequently left/right for a resistor placed
    beside its neighbors) instead of the resistor's actual fixed top/
    bottom terminals -- a real, visible placement/routing mismatch, not
    just an approximation nicety.

    Collapses the `N`/`E`/`S`/`W` compass duplicates `near_edge_ports()`
    returns (4 copies of the SAME physical via_stack pad, per its own
    docstring) down to their averaged centroid, instead of handing back 4
    near-duplicate EDGE points. **Real bug found and fixed, not
    hypothetical**: `../../router/script/route_nets.py` lands its own
    fresh `via_stack(met1, met2)` at whichever ONE compass point got
    picked -- an edge point of the resistor's own already-drawn via, not
    its center (`top_met_N`/`top_met_S` sit at `(0, +-0.165)`, offset from
    the pad's real `(0, 0)` local center) -- so the router's new via and
    the resistor's own pre-existing via ended up offset from each other by
    that same ~0.165um, close enough to overlap but not coincide: 10 real
    Magic DRC `met1.2` spacing violations on the reference design's R0.
    Averaging the compass quad recovers the true via center (confirmed
    exactly: net5's 4 raw points averaged to `y=-11.2`, matching
    `build_resistor()`'s own `p_ref.move(destination=(0, -hl +
    contact_tab/2))` construction) -- landing the router's new via exactly
    on top of the existing one, not beside it. **Confirmed by re-running
    DRC after this fix**: those 10 instances dropped to 0."""
    pts = {}
    for pin, prefix in (("drain", "p"), ("source", "n")):
        net = dev.get(pin)
        if net:
            found = near_edge_ports(comp, rf"^{prefix}_top_met_[NSEW]$")
            if found:
                cx = sum(p["x"] for p in found) / len(found)
                cy = sum(p["y"] for p in found) / len(found)
                pts.setdefault(net, []).append(
                    {"x": cx, "y": cy, "layer": found[0]["layer"],
                     "width": found[0]["width"], "pin": prefix})
    return pts


def cap_ports(comp, dev):
    """Real near-edge landing points for a standalone `mimcap()` Component,
    one net per PLATE.

    **Real, confirmed gap this fixes**, the same shape as the one
    `res_ports()` closed: `main()`'s port-assignment switch had no
    `single_cap` case, so every capacitor macro got `"ports": {}` -- and
    unlike a fet, both of a cap's terminals then fall back to
    `../../router/script/route_nets.py`'s box-edge approximation, which
    picks the side facing the other macro *independently per net*. Nothing
    in that fallback knows the two nets belong to opposite plates, so both
    can be driven at the same face. Measured on the reference design:
    Magic extracted `XC0` with ONE pin where the golden netlist has two
    (`XC0 net8 vout`), i.e. the compensation cap sat with a terminal
    floating and LVS could not match it.

    `mimcap()` names its two plates `bottom_met_*` (capmetbottom, met4 on
    sky130) and `top_met_*` (capmettop, met5) -- see that function's own
    docstring. Mapping the device's two nets to the two DIFFERENT prefixes
    is the whole point: mapping both to one plate would short the cap.
    `dev`'s `drain`/`source` fields hold them in netlist order
    (build_device_table()'s res/cap-merge convention: `nets[0]` -> `drain`,
    `nets[1]` -> `source`). The order is NOT free, despite a MiM cap being
    electrically symmetric: netgen's sky130A_setup.tcl does not permute
    `cap_mim_m3_1`'s terminals, so mapping `nets[0]` to the bottom plate
    produced a real LVS device mismatch (`cap_mim_m3_1/2 = 1` on net8 where
    the golden has `/1 = 1`). `nets[0]` therefore goes to the TOP plate,
    which is what extraction calls terminal 1.

    It targets `mimcap()`'s breakout stubs (`bottom_bo_W`/`top_bo_E`), NOT
    the bare plate edges. Measured, not preferred: the bottom plate extends
    only 0.14um past capmet, so a router landing its own via on that edge
    straddles the cap -- 40 real Magic violations (capm.5/via3.4,
    capm.8/via2.4, capm.2b "MiM cap bottom plate spacing < 1.2um", subcell
    abut, via.1a width) on this design's XC0. The stubs sit 2.34um clear of
    capmet by construction; falls back to the plate edges only for a cap
    built with `edge_breakout=False`, where the caller has opted out."""
    pts = {}
    for pin, bo, plate in (("drain", "top_bo_E", "top_met"),
                            ("source", "bottom_bo_W", "bottom_met")):
        net = dev.get(pin)
        if not net:
            continue
        found = near_edge_ports(comp, rf"^{bo}$") or near_edge_ports(comp, rf"^{plate}_[NSEW]$")
        if found:
            pts.setdefault(net, []).extend(tag_pin(found, plate))
    return pts


def current_mirror_ports(comp, dev_by_name, ref_name, *leg_names):
    """`../../../../cells/blocks/current_mirror.py` now stretches its routing-relevant
    ports out to the tap ring's own edge (`_stretch_ports_to_ring()` there)
    -- every DRAIN to the ring's top edge, every SOURCE to its bottom edge,
    all on met3 -- so the reference's and every branch's drain and source
    have real `*_bo_*` ports sitting exactly ON the macro boundary,
    measured at 0.000um inset, comfortably inside near_edge_ports()'s
    margin.

    This closes a gap this function used to document as open: before those
    breakouts existed, ZERO gate/drain ports on a real mirror_ratio=1
    instance cleared the margin (checked directly), and only the tap ring's
    own `tap_*_top_met_*` ports qualified (0.13-0.34um out), so gate (VREF)
    and drain (VOUT) fell back to ../../router/script/route_nets.py's
    box-edge approximation. The ring anchor is still used for the source/
    bulk net -- the ring is tied to that rail by construction (see
    current_mirror.py's own netlist, where B maps to VSS/VDD rather than a
    separate bulk node).

    The reference's drain and gate are the SAME node (A is diode-connected),
    so both map to the one `drain_ref_bo_N` port; if the netlist happens to
    name them differently, they are still electrically one net."""
    a = dev_by_name[ref_name]
    pts = {}

    def add(net, pin, pattern):
        """`pin` is the logical TERMINAL, deliberately not the port name.
        Two names can be one piece of metal (this cell's gate is tied to
        drain_ref by its own diode bar), and the router draws a wire
        between terminals it is told are distinct -- see tag_pin()."""
        if not net:
            return
        found = near_edge_ports(comp, pattern)
        if found:
            pts.setdefault(net, []).extend(tag_pin(found, pin))

    add(a.get("drain"), "drain_ref", r"^drain_ref_bo_N$")
    add(a.get("gate"), "drain_ref", r"^drain_ref_bo_N$")
    add(a.get("source"), "source_ref", r"^source_ref_bo_S$")
    # leg_names are in branch order, matching current_mirror()'s own
    # 1-indexed drain_out{b}/source_out{b} naming. Drains break out through
    # the ring's top edge, sources through its bottom edge.
    for i, leg in enumerate(leg_names, start=1):
        leg_dev = dev_by_name.get(leg) or {}
        add(leg_dev.get("drain"), f"drain_out{i}", rf"^drain_out{i}_bo_N$")
        add(leg_dev.get("source"), f"source_out{i}", rf"^source_out{i}_bo_S$")
    add(a.get("source"), "tap_ring", r"^tap_[NSEW]_top_met_[NSEW]$")
    return pts


def diff_pair_ports(comp, dev_by_name, a_name, b_name):
    """cells/blocks/diff_pair.py is a relabeled copy of glayout's own diff_pair.py
    (same internal routing, no `_add_edge_breakout()` fix) -- so unlike
    current_mirror_ports() above, checked directly this session against a
    real instance: its OWN internal C-route connector ports genuinely do
    clear near_edge_ports()'s margin for gate and drain (they were already
    routed out toward the gate bars/drain crossovers at the top/bottom of
    the cell for other reasons), just under different, non-`_breakout`
    names:
      - `PLUSgateroute_{E,W}_con_{N,S}` -- the 'A'/PLUS half's gate net
        (0.0-0.79um from the bbox edge, both sides, checked)
      - `MINUSgateroute_{E,W}_con_{N,S}` -- the 'B'/MINUS half's gate net
        (0.0-0.79um, both sides)
      - `drain_routeTL_BR_con_S` -- ties to a_topl's (the 'A' device's) own
        drain (0.0um -- exactly on the bbox edge)
      - `drain_routeTR_BL_con_S` -- ties to b_topr's ('B') drain (0.57um)
    Source has NO qualifying near-edge port here either (closest real
    point ~2.42um out, confirmed) -- same box-edge fallback as
    current_mirror_ports()'s gate/drain nets, not a new gap. Device 'a' is
    diff_pair.py's own internal 'A'/PLUS half, 'b' is 'B'/MINUS (glayout's
    construction order, not something this script chooses) --
    gen_diff_pair()'s `a_name`/`b_name` ordering is used consistently for
    gate AND drain so the two stay self-matched."""
    a, b = dev_by_name[a_name], dev_by_name[b_name]
    pts = {}

    def add(net, pin, *names):
        """All `names` are ends of ONE piece of metal, so they share a
        single `pin` tag. Tagging them separately made the router treat the
        two ends of one gate c_route as two terminals and wire between
        them, across the macro -- which shorted Vin to Vip (measured on
        the reference design). See tag_pin()."""
        if not net:
            return
        for name in names:
            found = near_edge_ports(comp, rf"^{name}$")
            if found:
                pts.setdefault(net, []).extend(tag_pin(found, pin))

    add(a.get("gate"), "gate_a", "PLUSgateroute_E_con_S", "PLUSgateroute_W_con_S")
    add(b.get("gate"), "gate_b", "MINUSgateroute_E_con_N", "MINUSgateroute_W_con_N")
    add(a.get("drain"), "drain_a", "drain_routeTL_BR_con_S")
    add(b.get("drain"), "drain_b", "drain_routeTR_BL_con_S")
    # The shared source (tail) -- see cells/blocks/diff_pair.py's VTAIL_bo_S block.
    add(a.get("source"), "source_tail", "VTAIL_bo_S")
    return pts


def min_metal_spacing_um(pdk):
    """Finest real metal min_separation across the PDK's own metal glayers
    -- queried, not hardcoded, same convention as
    ../../placement-optimizer/script/generate_grid.py's finest_grid_pitch()
    (reuses its metal_glayers() filter directly so "what counts as a metal
    layer" can't drift between the two scripts). Computed once here, where
    PDK access is already loaded for device generation, and carried into
    manifest.json so anneal_placement.py's density penalty doesn't need
    its own glayout import just for this one number."""
    seps = {g: pdk.get_grule(g)["min_separation"] for g in metal_glayers(pdk)}
    finest = min(seps, key=seps.get)
    return seps[finest], finest


def _f(val, default):
    if val is None or val == "":
        return default
    try:
        return float(val)
    except ValueError:
        print(f"  warning: could not parse numeric value {val!r}, using default {default}")
        return default


_LVS_W_RE = re.compile(r"\b([wW])\s*=\s*([0-9.eE+-]+)")
_LVS_NF_RE = re.compile(r"\bnf\s*=\s*([0-9.eE+-]+)")
_LVS_M_RE = re.compile(r"\bm\s*=\s*([0-9.eE+-]+)")


def write_lvs_compare_netlist(source_path: Path, out_path: Path) -> Path:
    """Write a comparison copy of `source_path` with each MOS device's `w`
    folded to its TOTAL width (`w * m`) and `nf`/`m` normalized to 1.

    Why this is needed, from the PDK's own netgen setup rather than guessed:
    `sky130A_setup.tcl`'s MOS loop ends with

        property "-circuit1 $dev" delete as ad ps pd mult sa sb sd nf ...

    -- it DELETES `nf` and `mult` on both sides and then compares `w`
    directly. Magic extracts a folded device as one instance per finger,
    which netgen merges back with `parallel {w add}` into a single device of
    the TOTAL width; the schematic supplies the PER-FINGER `w`. For any
    `nf > 1` the two can therefore never agree. Measured on
    the reference design: every device with `nf > 1` reported a width
    delta of exactly `nf` (320 vs 20, 228 vs 11.4, 342 vs 11.4, 120 vs 15)
    while `XMN4` at `nf=1` matched exactly -- the signature of a units
    mismatch, not a layout error.

    That noise is not harmless: it masks REAL sizing errors. The diff pair
    was being drawn at twice its netlist width (`common_centroid_ab_ba`
    places each device twice) and it was invisible here, because every
    device already showed a delta. Its true signature was a ratio of
    2 x nf rather than nf.

    Rewriting the comparison netlist rather than patching
    `sky130A_setup.tcl`: that file is PDK-owned and shared by every design
    on this machine, while this is a derived, inspectable artifact and the
    golden netlist stays frozen (see ../../../CLAUDE.md's "never edit the
    target .sp" rule -- this writes a COPY, next to `flattened.sp`)."""
    out = []
    for line in source_path.read_text().splitlines():
        stripped = line.strip()
        # Only device instance lines carry w/nf/m; leave .subckt/.ends/
        # comments and any non-MOS device (res/cap have no nf) untouched.
        if stripped[:1].upper() in ("X", "M") and "fet" in stripped:
            wm = _LVS_W_RE.search(line)
            if wm:
                nf = float(_LVS_NF_RE.search(line).group(1)) if _LVS_NF_RE.search(line) else 1.0
                mm = float(_LVS_M_RE.search(line).group(1)) if _LVS_M_RE.search(line) else 1.0
                # Total width is `w * m`; `nf` splits `w` into fingers and adds
                # no width (measured -- see build_current_mirror()'s total_w()).
                total = float(wm.group(2)) * max(1.0, mm)
                line = line[:wm.start()] + f"{wm.group(1)}={total:g}" + line[wm.end():]
                line = _LVS_NF_RE.sub("nf=1", line)
                line = _LVS_M_RE.sub("m=1", line)
        out.append(line)
    out_path.write_text("\n".join(out) + "\n")
    return out_path


def find_netlist(design_dir: Path) -> Path:
    name = design_dir.name
    final = design_dir / f"{name}_final.sp"
    if final.exists():
        return final
    bare = design_dir / f"{name}.sp"
    if bare.exists():
        return bare
    raise FileNotFoundError(f"no {name}_final.sp or {name}.sp in {design_dir}")


def _flavor(model: str, kind: str) -> str:
    """`nfet`/`pfet`/`npn`/`pnp` from a model name. Kept substring-based and
    PDK-neutral: processes spell it `nfet_01v8`, `nmos_3p3`, `nch`, so match
    on the polarity token rather than a per-PDK model list."""
    m = (model or "").lower()
    if kind == "bjt":
        return "pnp" if "pnp" in m else "npn"
    if "pfet" in m or "pmos" in m or "pch" in m:
        return "pfet"
    return "nfet"


def build_device_table(netlist_path: Path):
    """Returns `dev_by_name`, covering every MOS/BJT/cap/res device with its
    terminals and geometry merged into one entry.

    Terminals come straight off each device line's net list --
    `netlist_devices.parse_devices()` returns `nets` in SPICE order, which
    for a MOS is drain/gate/source[/bulk] and for a BJT is
    collector/base/emitter. This used to be `detect_topology.parse_devices()`
    instead; that module was the placer's structural detector and is no
    longer a dependency (`decomposition_patterns.py` supplies the grouping),
    so its one remaining job -- splitting a device line into terminals -- is
    done here from the parser this file already uses for `w`/`l`/`nf`/`m`.
    One parser, one answer, no two-source merge to drift."""
    text = netlist_path.read_text()
    dev_by_name = {}
    for d in devparse.parse_devices(text):
        kind, params, nets = d["kind"], d["params"], d["nets"]
        if kind in ("mos", "bjt"):
            dev_by_name[d["name"]] = {
                "name": d["name"], "kind": _flavor(d.get("model"), kind),
                "drain": nets[0] if nets else None,
                "gate": nets[1] if len(nets) > 1 else None,
                "source": nets[2] if len(nets) > 2 else None,
                "bulk": nets[3] if len(nets) > 3 else None,
                "w": _f(params.get("w"), 1.0), "l": _f(params.get("l"), 0.15),
                "nf": _f(params.get("nf"), 1.0), "m": _f(params.get("m"), 1.0),
            }
        elif kind in ("cap", "res"):
            dev_by_name[d["name"]] = {
                "name": d["name"], "kind": kind,
                "drain": nets[0] if nets else None,
                "gate": None,
                "source": nets[1] if len(nets) > 1 else None,
                "bulk": None,
                "w": _f(params.get("w"), 1.0), "l": _f(params.get("l"), 1.0),
                "nf": 1.0, "m": 1.0,
            }
    return dev_by_name


def build_fet(dev, with_dummy=True):
    """with_tie=True, with_substrate_tap=True: every standalone fet gets
    its own tap ring. Originally False/False -- real Magic DRC on this
    project's own designs (../SKILL.md's Step 3b worked example,
    the reference design) found this produced real, reproducible
    `nwell.4`/`LU.2`/`LU.3` violations (missing/too-far substrate taps),
    confirmed intrinsic to generation (identical before/after routing,
    identical across independent runs) -- not a hypothetical concern.

    `multipliers` carries the netlist's `m`. This was hardcoded to 1, which
    silently DROPPED `m` from every standalone device: a real, measured
    error, not a hypothetical -- on this repo's own `designs/test_miller_ota`,
    `XMP3` (`w=80 nf=1 m=3`, i.e. 240um total) came out drawn as a single
    80um device, a third of its netlist width, while
    `write_lvs_compare_netlist()` was simultaneously folding that same
    device to `w=240` for the LVS comparison -- so the two could never
    match. `m` is parallel copies of the whole device and glayout's
    `multipliers` is exactly that (a multiplier is a row of fingers, and
    rows >1 are c_routed source/drain/gate to each other inside
    `cells/primitives/fet.py`'s own multiplier array, so the copies are
    genuinely one parallel device, not m floating ones)."""
    nf = max(1, int(round(dev["nf"])))
    mult = max(1, int(round(dev["m"] or 1.0)))
    # SPICE `w` is the device's TOTAL width and `nf` SPLITS it into fingers;
    # glayout's `width` is the PER-FINGER extent and its `fingers` ADDS width
    # ("introduces additional fingers of width=width" -- primitives/fet.py).
    # The two conventions are inverted, so the per-finger extent is w/nf and
    # the drawn total comes out (w/nf) * nf * m = w * m, which is the netlist's.
    # See total_w() for the measurement that settles which convention sky130
    # actually uses.
    per_finger_w = dev["w"] / nf
    if dev["kind"] == "nfet":
        return nmos(PDK, width=per_finger_w, length=dev["l"], fingers=nf,
                    multipliers=mult, with_tie=True, with_dummy=with_dummy,
                    with_substrate_tap=True, with_dnwell=False)
    if dev["kind"] == "pfet":
        # `with_substrate_tap=True` is kept for PFET even though
        # `../../../../cells/primitives/fet.py`'s pmos() now defaults it off.
        # **Measured, not precautionary**: dropping it here made every
        # standalone PFET DRC-clean in isolation but broke the ASSEMBLED
        # layout -- the reference design went from 0 to 32 violations
        # (nwell/subcell-abut/hvi.5), and re-annealing with far more
        # clearance made it worse, 116 violations including 11 of
        # "P-diff distance to N-tap must be < 15.0um (LU.3)". That ring is
        # the p-substrate tap LU.3 needs nearby; spreading macros apart
        # moves every p-diffusion FURTHER from the nearest tap. `dummy` is
        # left at pmos()'s own (now False) default -- only the ring is
        # forced back. pmos() breaks its terminals out to whichever ring
        # ends up outermost, so the near-edge ports survive this.
        return pmos(PDK, width=per_finger_w, length=dev["l"], fingers=nf,
                    multipliers=mult, with_tie=True,
                    with_substrate_tap=True, dnwell=False)
    return None


RESISTOR_CONTACT_TAB = 0.4  # um of poly at each end reserved for the
                            # poly-met1 contact -- see build_resistor()


def build_resistor(width, length, contact_tab=RESISTOR_CONTACT_TAB):
    """A REAL poly resistor device -- the PDK's poly glayer marked with its
    resistor-recognition marker (`RESISTOR_MARKER_LAYER`, derived from
    pdk_options.json -- see there), contacted to the lowest routing metal at
    each end via via_stack(). Returns `None` when the active PDK has no
    marker datatype recorded: a poly rectangle with the marker on a guessed
    layer would extract as wiring, not as a resistor, and LVS would then fail
    for a reason nothing in the report names.

    Not a placeholder: verified by real Magic extraction during this
    session's own development (not assumed) -- an early version marked the
    POLYRES region all the way to the poly's own edges and Magic rejected
    it ("Illegal overlap between poly and npolyres (types do not connect)"
    -- the marker must stop short of the contact, leaving a plain-poly
    landing tab at each end for the via, per `contact_tab`). Extracted
    resistor length comes out `contact_tab` shorter on EACH end than the
    drawn poly (confirmed empirically: drawn 22.8um -> extracted l=22.0um
    at contact_tab=0.4, exactly `2*contact_tab` less) -- this function
    draws `length + 2*contact_tab` of poly so the CALLER's `length`
    argument matches the real extracted `l=` value, not the drawn one.

    Ports: `p_top_met_*`/`n_top_met_*` (met1, from each via_stack) at the
    two terminals -- named to match glayout's own resistor_netlist()
    convention (P/N), for a future port-exact routing pass; this script's
    own macro-boundary-granularity placement doesn't need them today."""
    if RESISTOR_MARKER_LAYER is None:
        print(f"  warning: {PDK_CFG.name} has no 'resistor_marker_datatype' in "
              f"pdk_options.json, so no resistor-recognition marker layer is known "
              f"-- this resistor gets NO primitive (generation='manual'). Fill that "
              f"key in from the PDK's own tech file to enable it.")
        return None
    comp = Component()
    hw = max(width, 0.01) / 2
    drawn_length = max(length, 0.01) + 2 * contact_tab
    hl = drawn_length / 2
    comp.add_polygon([(-hw, -hl), (hw, -hl), (hw, hl), (-hw, hl)], layer=PDK.get_glayer("poly"))
    marker_hl = hl - contact_tab
    comp.add_polygon([(-hw, -marker_hl), (hw, -marker_hl), (hw, marker_hl), (-hw, marker_hl)],
                      layer=RESISTOR_MARKER_LAYER)
    p_ref = comp << via_stack(PDK, "poly", LOWEST_METAL, centered=True)
    p_ref.move(destination=(0, -hl + contact_tab / 2))
    comp.add_ports(p_ref.ports, prefix="p_")
    n_ref = comp << via_stack(PDK, "poly", LOWEST_METAL, centered=True)
    n_ref.move(destination=(0, hl - contact_tab / 2))
    comp.add_ports(n_ref.ports, prefix="n_")
    return comp


def build_leftover(dev, with_dummy=True):
    """Returns (component_or_None, generation_tag)."""
    kind = dev["kind"]
    if kind in MOS_KINDS:
        return build_fet(dev, with_dummy=with_dummy), "auto"
    if kind == "cap":
        return mimcap(PDK, size=(max(dev["w"], 0.01), max(dev["l"], 0.01))), "auto"
    if kind == "res":
        comp = build_resistor(dev["w"], dev["l"])
        return (comp, "auto") if comp is not None else (None, "manual")
    # BJTs, unrecognized subckt calls: no primitive available at all.
    return None, "manual"


def gen_current_mirror(finding, dev_by_name, with_dummy=True, split_legs=False):
    """One reference + N mirror legs, drawn as ONE macro.

    `cells/blocks/current_mirror.py`'s own `current_mirror()` takes `mirror_ratio`
    as a LIST -- one entry per output branch, each that many unit-sized
    copies of the mirror device beside the single reference unit -- so an
    N-leg mirror is one call with `mirror_ratio=[r1, r2, ...]` and comes
    out as one cell holding the reference and every leg. That is what this
    function emits: a single `current_mirror` macro whose `devices` list is
    `[reference, leg1, leg2, ...]`.

    `split_legs=True` restores the older behavior (leg 1 drawn with the
    reference, every additional leg emitted as its own standalone
    `current_mirror_leg` macro tagged `shares_reference_with`). That split
    existed because glayout's own elementary `current_mirror()` drew
    exactly one A/B pair, so calling it once per leg would have fabricated
    N copies of the reference's silicon -- physically wrong, since there is
    one reference device, not N. With this project's own multi-branch
    generator that workaround is no longer needed, and keeping the legs in
    one cell is strictly better for a current mirror: the legs share the
    reference's gate bias and bulk by construction inside the cell
    (matched, abutted, one guard ring) instead of depending on the router
    to reconnect them afterwards. Kept as a flag only as an escape hatch if
    a particular sizing ever makes the multi-branch cell problematic.

    Returns `(macros, deferred_device_names, note)`. `deferred` is
    non-empty when a leg's ratio to the reference is fractional: the cell
    can only draw integer multiples of the reference's total width, so such
    a leg is left OUT of the macro and must be drawn standalone by the
    caller at its netlist width (see the RATIO_TOL block below for the
    measured failure this replaced). When no leg is expressible, `macros`
    is empty and the whole finding is handed back."""
    ref_name = finding["devices"][0]
    ref = dev_by_name[ref_name]
    device_flavor = "nfet" if ref["kind"] == "nfet" else "pfet"
    macros = []
    legs = finding["devices"][1:]
    first_leg_name = legs[0]
    first_leg = dev_by_name[first_leg_name]
    ref_m = ref["m"] or 1.0

    def total_w(d):
        """A device's real total channel width, `w * m` -- `nf` is NOT a factor.

        `m` replicates whole devices, so it multiplies width. `nf` only
        SPLITS the device's `w` into that many fingers and adds none.

        This previously returned `w * nf * m`, reasoning that
        sky130_fd_pr__nfet_01v8.pm3.spice passes `w` and `nf` straight to a
        BSIM4 device "where W is the width PER FINGER". The pass-through is
        real (that file's line 40), but the inference is wrong: BSIM4 derives
        its per-finger width as `Weff = W/NF`, so W is the TOTAL.

        Settled by direct measurement rather than by reading, one nfet_01v8
        at Vgs=Vds=0.9 in this PDK's own `tt` corner:
            w=40 nf=1 m=1  ->  1.686 mA
            w=40 nf=8 m=1  ->  1.695 mA   (unchanged: nf adds no width)
            w=40 nf=1 m=8  -> 13.486 mA   (exactly 8x: m does)
        Reproduce before changing this back."""
        return (d["w"] or 0.0) * max(1.0, d["m"] or 1.0)

    ref_total = total_w(ref) or 1.0

    def leg_ratio(leg_name):
        """This leg's unit count relative to the reference, from real total
        WIDTH -- not from `m` alone, which is what this used to read.

        A netlist may put a mirror's ratio in `m` OR in `nf`, and reading
        only `m` silently loses the other. Confirmed on
        the reference design: with the ratio carried by `nf`
        (XMN4 nf=1, XMN3 nf=20, XMN5 nf=30) every `m` is 1, so this
        returned [1, 1] and the mirror was drawn with no ratio at all --
        all three legs at the reference's finger count, i.e. XMN3 20x too
        small and XMN5 30x. Comparing total width covers both conventions
        and any mixture of them.

        `mirror_ratio` still can only express N:1 -- N unit copies beside a
        single reference unit -- so a leg SMALLER than its reference still
        clamps to 1 and the drawn cell is then not the netlist's ratio.
        That is why the reference should be the design's UNIT device."""
        return max(1, int(round(total_w(dev_by_name[leg_name]) / ref_total)))

    numcols = leg_ratio(first_leg_name)
    # The reference UNIT is the reference device's own TOTAL width
    # (`w * m`), drawn as `nf * m` fingers of `width=ref['w']/ref['nf']` --
    # `leg_ratio()` above already measures every leg against `ref_total`, so
    # the unit the ratios are relative to has to be that same total. Reading
    # `ref["nf"]` alone (what this used to do) dropped `m` from the absolute
    # scale of the whole macro: measured on `designs/test_miller_ota`, XMN4
    # (`w=47.2 nf=1 m=5`, 236um total) was drawn as one 47.2um finger, so
    # every device in the mirror came out 5x too small even though the
    # ratios between them were right.
    nf = max(1, int(round((ref["nf"] or 1.0) * (ref["m"] or 1.0))))
    # `mirror_ratio` can only express an INTEGER number of reference units
    # per leg, so a leg whose real ratio is fractional (or below 1, which
    # clamps) is NOT DRAWABLE in this cell at its netlist width.
    #
    # This used to draw it anyway at the rounded ratio and print a warning.
    # That is the one failure mode the whole `total_w`/`leg_ratio` history
    # above exists to prevent, reintroduced at the last step: measured on
    # `example/test_miller_ota`, `cm_nbias` (XMN4 236um ref, XMN3 102.6um =
    # 0.435x, XMN5 344.96um = 1.462x) emitted `mirror_ratio=[1, 1]` and drew
    # all three legs at 236um -- XMN3 2.30x too wide, XMN5 0.68x too narrow.
    # A warning does not stop that reaching LVS as a device-width mismatch
    # on two devices, and it is a real circuit error (mis-ratioed tail and
    # stage-2 load), not a cosmetic one. No `width`/`fingers` choice fixes
    # it: the cell draws the reference as exactly one unit, so every leg is
    # forced to an integer multiple of the reference's own total width.
    #
    # So an inexpressible leg is now EXCLUDED from the composed cell and
    # deferred to a standalone primitive at its true width -- the same
    # fall-through an unsupported topology already takes (see the caller's
    # `manual_composition` branch). Correct widths, at the cost of the
    # in-cell matching the cell cannot provide for that leg anyway.
    RATIO_TOL = 0.05

    def expressible(leg_name):
        want = total_w(dev_by_name[leg_name]) / ref_total
        return abs(leg_ratio(leg_name) - want) <= RATIO_TOL * max(want, 1e-9)

    cell_legs = [n for n in ([first_leg_name] if split_legs else legs) if expressible(n)]
    inexpressible = [n for n in ([first_leg_name] if split_legs else legs)
                     if n not in cell_legs]
    for leg_name in inexpressible:
        want = total_w(dev_by_name[leg_name]) / ref_total
        print(f"  warning: mirror leg {leg_name} wants {want:.3f}x the reference's total "
              f"width ({total_w(dev_by_name[leg_name]):.3f}um vs {ref_total:.3f}um), which "
              f"mirror_ratio cannot express (integer reference units only) -- EXCLUDED from "
              f"the composed cell and drawn as a standalone primitive at its true width. "
              f"Matching for this leg is lost; to recover it, make the reference the "
              f"design's UNIT device or compose this mirror by hand.")
    if not cell_legs:
        # Nothing left to mirror against the reference -- a one-device
        # "current mirror" cell would be a standalone primitive wearing a
        # macro's name, and would still be drawn at the wrong scale for the
        # legs. Hand the WHOLE finding back for standalone treatment.
        note = (f"every leg has a fractional ratio to reference {ref_name} "
                f"({', '.join(f'{n}={total_w(dev_by_name[n]) / ref_total:.3f}x' for n in legs)}) "
                f"-- mirror_ratio takes integer reference units only, so no part of this "
                f"mirror is composable; all {len(finding['devices'])} devices drawn standalone "
                f"at their netlist widths")
        print(f"  warning: current_mirror {ref_name}+{'+'.join(legs)} not composed -- {note}")
        return [], [ref_name, *legs], note
    # with_substrate_tap: current_mirror()'s own default is False --
    # originally forced True unconditionally here for the same real,
    # confirmed nwell.4/LU.2/LU.3 DRC gap as build_fet()'s (see that
    # function's docstring), not a hypothetical -- but that finding was
    # never re-checked per device flavor. Real, confirmed re-check this
    # session: for NFET, `with_tie=True`'s own ring (current_mirror.py's
    # `tie_ref`, always sdlayer=`tap_layer`="p+s/d" for nfet -- the
    # LOCAL pwell tie) is ALREADY the same p+s/d layer/purpose as
    # `with_substrate_tap`'s ring (always hardcoded "p+s/d" regardless of
    # device flavor, current_mirror.py's own `subtap_ring`) -- for nfet
    # the second ring is redundant, not a second real requirement.
    # Verified directly, isolated standalone-primitive Magic DRC, both
    # with the pre-breakout-fix AND current current_mirror.py: an nfet
    # current_mirror with `with_substrate_tap=False` is bit-for-bit as
    # clean as one with it True (0 violations either way) -- and it
    # shrinks the macro noticeably (half-extents 10.2x33.1um vs
    # 12.15x40.8um for the numcols=2/width=58.8 config a real reference
    # design's own current mirror used -- the "outer tapring is distant
    # from the core devices" a user directly noticed). For PFET, the tie
    # ring is n+s/d (nwell tie) -- genuinely different from the p+s/d
    # substrate ring, so `with_substrate_tap=True` is kept there,
    # unverified-but-conservative per the original historical finding
    # (not re-litigated here). with_dummy is threaded through from
    # generate_primitives.py's own --no-dummy flag -- previously ignored
    # here (this call never passed it at all, so `--no-dummy` silently
    # had no effect on current_mirror()/diff_pair() macros, only on
    # standalone leftover devices -- a real gap, not by design).
    want_substrate_tap = True
    # cells/blocks/current_mirror.py's own current_mirror(): `mirror_ratio` unit-B
    # copies (A stays a single reference unit) instead of glayout's own
    # `numcols` (which folds BOTH A and B by the same factor) -- a more
    # literal match for "N-leg mirror" than the interdigitized numcols
    # scheme this script's docstring caveats above. Param names differ from
    # glayout's composite: `substrate_tap`/`dummy`, not `with_substrate_tap`/
    # `with_dummy`.
    # Every EXPRESSIBLE leg in ONE cell (default): mirror_ratio is a
    # per-branch list. `cell_legs` is `legs` minus anything the ratio check
    # above deferred, so the macro's `devices` list always describes what
    # the cell actually draws.
    mirror_ratio = numcols if split_legs else [leg_ratio(n) for n in cell_legs]
    grouped_devices = [ref_name, *cell_legs]
    # `width` is glayout's PER-FINGER extent, so the reference's own `nf`
    # divides it (see build_fet()); `fingers=nf` above is ref.nf*ref.m, giving
    # a drawn total of (ref.w/ref.nf) * ref.nf * ref.m = ref.w * ref.m.
    ref_per_finger_w = ref["w"] / max(1, int(round(ref["nf"] or 1.0)))
    comp = build_current_mirror(PDK, mirror_ratio=mirror_ratio, device=device_flavor,
                                 width=ref_per_finger_w, length=ref["l"], fingers=nf,
                                 substrate_tap=want_substrate_tap, dummy=with_dummy)
    # Real netlist instance names onto the physical devices. A branch drawn
    # with ratio > 1 is several parallel units of the SAME circuit device,
    # so every unit gets that device's name at its own location.
    comp = label_cm_instances(comp, PDK, ref_name=ref_name, branch_names=cell_legs)
    primary_name = "current_mirror_" + "_".join(grouped_devices)
    macros.append({
        "name": primary_name,
        "kind": "current_mirror",
        "devices": grouped_devices,
        "component": comp,
        "generation": "auto",
        "constraints": {"centroid_match": True},
        "reference_device": ref_name,
        "glayout_call": (f"current_mirror(pdk, mirror_ratio={mirror_ratio}, "
                          f"device='{device_flavor}', width={ref_per_finger_w:g}, "
                          f"length={ref['l']}, fingers={nf}, "
                          f"substrate_tap={want_substrate_tap}, "
                          f"dummy={with_dummy})  # cells/blocks/current_mirror.py"),
    })
    if split_legs:
        # legs[1:] get their own macros here, so they are never in
        # `inexpressible` (which only ever considers `first_leg_name` in
        # this mode) and are not deferred to the caller.
        for leg_name in legs[1:]:
            leg = dev_by_name[leg_name]
            leg_comp = build_fet(leg, with_dummy=with_dummy)
            macros.append({
                "name": f"current_mirror_leg_{leg_name}",
                "kind": "current_mirror_leg",
                "devices": [leg_name],
                "component": leg_comp,
                "generation": "auto",
                "constraints": {"centroid_match": True},
                "glayout_call": (f"nmos/pmos(pdk, width={leg['w']}, length={leg['l']}, "
                                  f"fingers={max(1, int(round(leg['nf'])))}, "
                                  f"multipliers={max(1, int(round(leg['m'] or 1.0)))}) -- extra mirror leg, "
                                  f"physical reference is drawn once in {primary_name!r}; route this "
                                  f"device's gate/source to that macro's reference nets, don't redraw "
                                  f"the reference"),
                "shares_reference_with": primary_name,
            })
    # (macros, deferred_device_names, note). `deferred` is what the caller
    # must draw standalone -- non-empty only when the ratio check above
    # excluded a leg; `note` is the manual_composition detail for it.
    return macros, inexpressible, (
        (f"leg(s) {', '.join(inexpressible)} have a fractional ratio to reference "
         f"{ref_name} and are drawn standalone at their netlist width; "
         f"{primary_name} holds the reference and the composable leg(s)")
        if inexpressible else None)


def gen_diff_pair(finding, dev_by_name, with_dummy=True):
    a_name, b_name = finding["devices"]
    a = dev_by_name[a_name]
    b = dev_by_name[b_name]
    n_or_p = a["kind"] == "nfet"
    # One call draws BOTH halves from `a`'s geometry, so a pair whose two
    # halves are not identical in the netlist gets drawn as two copies of
    # `a` -- silently sizing `b` to `a`. A diff pair's halves are supposed to
    # be identical (asymmetry here is input offset), so this is a netlist
    # finding, not something to absorb.
    for field in ("w", "l", "nf", "m"):
        if abs((a[field] or 0.0) - (b[field] or 0.0)) > 1e-9:
            print(f"  warning: diff pair halves disagree on {field} "
                  f"({a_name}={a[field]}, {b_name}={b[field]}); both halves are drawn from "
                  f"{a_name}'s geometry, so {b_name} is NOT drawn at its netlist size")
    # HALF the netlist's finger count per call. `common_centroid_ab_ba()`
    # places each device TWICE (the A/B B/A halves the comment below
    # describes), and `fingers` is per placed unit -- so passing the full
    # finger count builds each transistor at 2x that, i.e. twice the width
    # the netlist asks for. Confirmed by extraction on the reference design:
    # XMN1 (`w=20 nf=16`, so 320um) came out of Magic at w=640.
    #
    # The count to split is `nf * m`, not `nf`. Both carry parallel copies of
    # the same channel width and a netlist may use either (or both) -- this
    # read `nf` alone, so a pair written with its size in `m` was drawn at
    # 1/m of its width: measured on `designs/test_miller_ota`, XMN1/XMN2
    # (`w=40 nf=1 m=8`, 320um each) drew as 2 fingers = 80um, a quarter of
    # the netlist's device, against an `lvs_compare.sp` asking for w=320.
    want_nf = max(1, int(round((a["nf"] or 1.0) * (a["m"] or 1.0))))
    fingers = max(1, want_nf // 2)
    # glayout's `width` is the PER-FINGER extent while SPICE's `w` is the
    # total that `nf` splits (see build_fet()), so divide by this device's own
    # `nf`. Each half then draws width * fingers * 2 = (w/nf) * (nf*m) = w*m,
    # the netlist's total width for one device of the pair.
    a_per_finger_w = a["w"] / max(1, int(round(a["nf"] or 1.0)))
    a_total_w = a["w"] * max(1.0, a["m"] or 1.0)
    if want_nf % 2:
        # Odd nf cannot split evenly across the two halves. Say so rather
        # than silently rounding the device's width.
        print(f"  warning: {a_name}/{b_name} nf={want_nf} is odd; common-centroid needs an "
              f"even split, drawing {2 * fingers} fingers per device "
              f"({2 * fingers * a_per_finger_w:.3f}um vs the netlist's "
              f"{a_total_w:.3f}um)")
    # cells/blocks/diff_pair.py's own diff_pair() -- same param names as glayout's
    # composite (`dummy`, not `with_dummy`), so no signature remapping
    # needed here beyond the import swap.
    comp = build_diff_pair(PDK, width=a_per_finger_w, fingers=fingers, length=a["l"],
                            n_or_p_fet=n_or_p, dummy=with_dummy)
    # Common-centroid AB/BA: each device is split into two diagonally
    # opposite halves, so each name is placed twice (see that function).
    comp = label_dp_instances(comp, PDK, name_a=a_name, name_b=b_name)
    return [{
        "name": f"diff_pair_{a_name}_{b_name}",
        "kind": "differential_pair",
        "devices": [a_name, b_name],
        "component": comp,
        "generation": "auto",
        "constraints": {"symmetry": True, "self_symmetric": True},
        "glayout_call": (f"diff_pair(pdk, width={a_per_finger_w:g}, fingers={fingers}, "
                          f"length={a['l']}, n_or_p_fet={n_or_p}, dummy={with_dummy})  "
                          f"# cells/blocks/diff_pair.py"),
    }]


def generate(netlist_path: Path, out_dir: Path, with_dummy=True, per_device=False,
             use_subckt_macros=True, top_subckt=None, split_mirror_legs=False,
             decomposition=None, modules_dir=None):
    """`out_dir` holds the standalone per-device primitives, the manifest and
    the derived netlists. `modules_dir`, when given, holds the GDS of every
    macro built from a PATTERN the circuit read named (a current mirror,
    a differential pair) -- ../SKILL.md's Step 0 folder layout, where the
    two kinds of cell are different things: a module is a composed
    subcircuit whose internal matching is the point, a primitive is one
    device. Defaults to `out_dir`, so an older single-folder run is
    unchanged. Manifest paths are absolute either way, so every downstream
    consumer finds a macro's GDS wherever it was written."""
    out_dir.mkdir(parents=True, exist_ok=True)
    modules_dir = Path(modules_dir).resolve() if modules_dir else out_dir
    modules_dir.mkdir(parents=True, exist_ok=True)

    # A DECOMPOSED netlist (../../circuit-decomposition/SKILL.md) already
    # states its macro grouping structurally, as real `.subckt` blocks --
    # read it rather than throwing it away and re-detecting. `flatten()`
    # returns None for an ordinary flat netlist, in which case everything
    # below is unchanged. The flattened text is written out as a real
    # artifact (`primitives/flattened.sp`) instead of being transformed
    # only in memory, so what the parsers below actually consumed is
    # inspectable -- and it is LVS-equivalent to the original (confirmed
    # with netgen against the reference design's flat original, not
    # assumed: same 11 devices / 10 nets, "Circuits match uniquely").
    # Flattening is independent of GROUPING: `--per-device` means "don't
    # group devices into macros", not "don't read the subckts". Confirmed
    # as a real bug when these were conflated -- `--per-device` on
    # the reference design's decomposed netlist generated only 4
    # primitives (the top-level leftovers) and silently dropped the 7
    # devices inside the three subckts, instead of the 11 it should place.
    flat = None
    if use_subckt_macros:
        flat = subckt_macros.flatten(netlist_path, top_subckt=top_subckt)
    source_netlist_path = netlist_path
    if flat is not None:
        source_netlist_path = out_dir / "flattened.sp"
        source_netlist_path.write_text(flat.flat_text())
        print(f"=== Decomposed netlist: {len(flat.groups)} subckt instance(s), "
              f"top={flat.top_subckt!r} ===")
        for g in flat.groups:
            print(f"  [{g['instance']}] {g['subckt']}: {g['devices']}")
        if flat.nested:
            print(f"  warning: nested subckt instance(s) inside {flat.nested} left unexpanded")
        print(f"  wrote {source_netlist_path} (flattened, top-level net names)")

    # The top-level `.subckt`'s own pin list. Carried into manifest.json so
    # ../../router/script/route_nets.py knows which net names are real
    # top-level PINS and which are internal -- Magic's `port makeall`
    # promotes every label it finds, so labelling internal nets on a
    # conductive `<layer>_label` made the layout declare 10 top-level ports
    # where the golden declares 5, and LVS reported "Top level cell failed
    # pin matching" even with the netlists otherwise equivalent.
    top_pins = []
    for line in source_netlist_path.read_text().splitlines():
        m = re.match(r"\s*\.subckt\s+(\S+)\s+(.*)", line, re.I)
        if m:
            top_pins = m.group(2).split()
            break

    lvs_path = write_lvs_compare_netlist(source_netlist_path, out_dir / "lvs_compare.sp")
    print(f"  wrote {lvs_path} (w folded to w*m -- compare LVS against THIS, "
          f"see write_lvs_compare_netlist())")

    dev_by_name = build_device_table(source_netlist_path)

    macros = []
    manual_composition = []
    subckt_instance_by_macro = {}

    # Pattern grouping comes from `circuit_decomposition.yaml` and nowhere
    # else -- no structural re-detection (see decomposition_patterns.py's
    # module docstring). Missing file, unauthored `patterns`, or
    # `--per-device`: every device becomes its own standalone primitive.
    findings = []
    if not per_device:
        yaml_path = (Path(decomposition).resolve() if decomposition
                     else decomposition_patterns.find_yaml(netlist_path, netlist_path.parent))
        if yaml_path is None:
            print(f"  WARNING: no {decomposition_patterns.YAML_NAME} found for this netlist. "
                  f"Pattern grouping has no source, so EVERY device is generated as a "
                  f"standalone primitive -- a real floorplan, but with no current-mirror or "
                  f"differential-pair matching. Run ../../circuit-decomposition/SKILL.md "
                  f"first (or pass --decomposition PATH) if matching matters.")
        else:
            findings, unmatched, warnings = decomposition_patterns.load(yaml_path, dev_by_name)
            print(f"=== Circuit read: {yaml_path} ===")
            for f in findings:
                print(f"  [{f['topology']}] {f['pattern_id']}: {f['devices']} -> "
                      f"{decomposition_patterns.buildable_label(f['topology'], f['devices'])}")
            print(f"  ungrouped -> standalone primitives: {unmatched}")
            for w in warnings:
                print(f"  WARNING: {w}")

    if not findings:
        # Real netlist order (dev_by_name's own insertion order, i.e. the
        # netlist's own device-line order) -- no grouping logic left to
        # impose any other order.
        leftover_names = list(dev_by_name.keys())
    else:
        # Provenance only: which subckt instance a pattern's devices came
        # out of on a decomposed netlist, for `manifest.json`. The grouping
        # itself is the yaml's, so a subckt whose name disagrees with the
        # circuit read changes nothing here.
        instance_by_device = {}
        if flat is not None:
            for g in flat.groups:
                for d in g["devices"]:
                    instance_by_device[d] = g["instance"]

        findings_with_source = [
            (f, next((instance_by_device[d] for d in f["devices"]
                      if d in instance_by_device), None))
            for f in findings
        ]
        unclassified = unmatched
        placed_devices = set()

        for finding, subckt_instance in findings_with_source:
            topology = finding["topology"]
            all_mos_or_bjt_recognized = all(
                dev_by_name[d]["kind"] in MOS_KINDS for d in finding["devices"]
            )
            # A buildable topology whose device COUNT the generator can't
            # take (see decomposition_patterns.MACRO_DEVICE_COUNTS -- both
            # generators index `devices` positionally) takes the same
            # fall-through path as an unsupported topology: standalone
            # primitives plus a manual-composition entry, with the warning
            # `decomposition_patterns.load()` already raised. It used to
            # crash the run instead.
            buildable = all_mos_or_bjt_recognized and not decomposition_patterns.macro_arity_error(
                topology, finding["devices"])
            if topology == "current_mirror" and buildable:
                new_macros, deferred, cm_note = gen_current_mirror(
                    finding, dev_by_name, with_dummy=with_dummy,
                    split_legs=split_mirror_legs)
                macros.extend(new_macros)
                # Only what a macro actually DRAWS counts as placed; a leg
                # the mirror could not express at its netlist width falls
                # through to a standalone primitive below, exactly like an
                # unsupported topology's devices.
                placed_devices.update(d for m in new_macros for d in m["devices"])
                if deferred:
                    manual_composition.append({
                        "topology": topology, "devices": deferred,
                        "detail": cm_note, "doc": finding["doc"],
                    })
                for m in new_macros:
                    subckt_instance_by_macro[m["name"]] = subckt_instance
            elif topology == "differential_pair" and buildable:
                new_macros = gen_diff_pair(finding, dev_by_name, with_dummy=with_dummy)
                macros.extend(new_macros)
                placed_devices.update(finding["devices"])
                for m in new_macros:
                    subckt_instance_by_macro[m["name"]] = subckt_instance
            else:
                # No dedicated glayout module (composite topology, or a BJT
                # current_mirror/differential_pair) -- fall through to
                # per-device standalone primitives below, flagged manual.
                manual_composition.append({
                    "topology": topology, "devices": finding["devices"],
                    "detail": finding["detail"], "doc": finding["doc"],
                })

        leftover_names = [n for n in unclassified if n not in placed_devices]
        leftover_names += [n for n, d in dev_by_name.items()
                            if d["kind"] in ("cap", "res") and n not in placed_devices]
        manual_device_names = {d for m in manual_composition for d in m["devices"]}
        leftover_names += [n for n in manual_device_names if n not in placed_devices and n not in leftover_names]
        # Order-preserving dedupe. Necessary, not defensive: `unclassified`
        # is every device no pattern claimed, passives included, so the
        # cap/res line above re-adds each of them. Without this, a design's
        # `R0`/`XC0` each produced two identical macros.
        leftover_names = list(dict.fromkeys(leftover_names))

    manual_device_names = {d for m in manual_composition for d in m["devices"]}
    for name in leftover_names:
        dev = dev_by_name[name]
        comp, generation = build_leftover(dev, with_dummy=with_dummy)
        entry = {
            "name": name, "kind": f"single_{dev['kind']}", "devices": [name],
            "component": comp, "generation": generation, "constraints": {},
            "glayout_call": None,
        }
        if name in manual_device_names:
            entry["topology_hint"] = next(
                m["topology"] for m in manual_composition if name in m["devices"]
            )
        macros.append(entry)

    manifest_macros = []
    md_lines = ["# primitives manifest", ""]
    for m in macros:
        comp = m.pop("component")
        # Provenance, only when this run actually read a decomposed netlist:
        # which top-level `.subckt` instance this macro came from. Macro
        # NAMES stay device-derived (`current_mirror_XMN4_XMN3`) in both
        # modes deliberately -- a decomposed run and a flat run of the same
        # circuit then produce directly comparable manifests instead of
        # differing only in naming.
        if m["name"] in subckt_instance_by_macro:
            m["subckt_instance"] = subckt_instance_by_macro[m["name"]]
        if comp is None:
            print(f"  skipping {m['name']}: no glayout primitive available (kind={m['kind']}) -- manual only")
            m["w"] = None
            m["h"] = None
            m["gds"] = None
            m["ports"] = {}
        else:
            w, h = evaluate_bbox(comp)
            # A pattern-composed macro is a MODULE (modules_dir); everything
            # else is one device (out_dir). Same manifest, different folder --
            # see generate()'s own docstring.
            gds_dir = modules_dir if m["kind"] in COMPOSITE_MACRO_KINDS else out_dir
            gds_path = gds_dir / f"{m['name']}.gds"
            top = Component(name=m["name"])
            top << comp
            # Single-device macros are deliberately NOT labelled here.
            # `render_placement.py` already writes each placed macro's NAME
            # at its center, and for a one-device macro that name IS the
            # device name -- adding it here too put two identical labels at
            # the identical coordinate in the assembled
            # `placement_visualization.gds` (confirmed: `R0`, `XC0`, `XMP3`,
            # `XMP4` each appeared twice). Composite macros need the
            # per-device pass in gen_current_mirror()/gen_diff_pair()
            # because their macro name ("current_mirror_XMN4_XMN3_XMN5")
            # names the block, never the individual transistors inside it.
            top.write_gds(str(gds_path))
            m["w"] = float(w)
            m["h"] = float(h)
            m["gds"] = str(gds_path)
            # Real near-edge port coordinates (LOCAL frame, i.e. before
            # ../placer/script/anneal_placement.py's placement translate/
            # rotate) for ../../router/script/route_nets.py to land wires
            # on instead of blindly guessing a box-edge point -- see
            # near_edge_ports()'s docstring for why only SOME nets get
            # real ports here (device-pin nets on standalone/current_mirror
            # fets still fall back to the box-edge approximation; only
            # substrate/well ties and diff_pair's own already-routed
            # gate/drain/source stubs clear the safety margin).
            if m["kind"] in ("single_nfet", "single_pfet", "current_mirror_leg"):
                m["ports"] = fet_ports(comp, dev_by_name[m["devices"][0]])
            elif m["kind"] == "single_res":
                m["ports"] = res_ports(comp, dev_by_name[m["devices"][0]])
            elif m["kind"] == "single_cap":
                m["ports"] = cap_ports(comp, dev_by_name[m["devices"][0]])
            elif m["kind"] == "current_mirror":
                m["ports"] = current_mirror_ports(comp, dev_by_name, *m["devices"])
            elif m["kind"] == "differential_pair":
                m["ports"] = diff_pair_ports(comp, dev_by_name, m["devices"][0], m["devices"][1])
            else:
                m["ports"] = {}
            # Re-express every port relative to the macro's own BBOX CENTER,
            # not its glayout origin.
            #
            # `../../router/script/route_nets.py`'s `world_port_map()` maps a
            # local port to world as an offset from the placed macro's centre,
            # `(x + w/2, y + h/2)` -- i.e. it assumes local (0,0) IS the bbox
            # centre. For a macro whose content is symmetric about its origin
            # that holds, and every standalone device here is. It does NOT
            # hold for the composites: measured on the reference design,
            # current_mirror_XMP1_XMP2's bbox centre sits at (-1.010, 0.030),
            # diff_pair_XMN1_XMN2's at (0.000, 0.435), and
            # current_mirror_XMN4_XMN3_XMN5's at (6.040, 0.030) -- that last
            # one because the mirror's row grows to the RIGHT from the
            # reference device at its origin, so the more output legs it has
            # the further off-centre it gets. Left uncorrected the router
            # lands wires up to 6um away from the metal they are supposed to
            # reach, which silently defeats the whole point of these ports.
            # Symptom to recognise: ports reported at a NEGATIVE inset from
            # the macro's own w/h box.
            #
            # `render_placement.py` already places by centre, so correcting
            # here (rather than in the router) leaves one convention --
            # "local port coords are relative to the bbox centre" -- shared by
            # both consumers.
            (cbx0, cby0), (cbx1, cby1) = comp.bbox
            ox, oy = (float(cbx0) + float(cbx1)) / 2, (float(cby0) + float(cby1)) / 2
            if (abs(ox) > 1e-9 or abs(oy) > 1e-9) and m["ports"]:
                for pts in m["ports"].values():
                    for pt in pts:
                        pt["x"] = round(pt["x"] - ox, 6)
                        pt["y"] = round(pt["y"] - oy, 6)
            m["bbox_center_offset"] = [round(ox, 6), round(oy, 6)]
            # Extra routing keepout this macro needs BEYOND the general
            # metal minimum, for whatever routes around it later. A MiM cap's
            # plates are wide metal carrying their own capmet spacing rule, an
            # order above the ~0.14um general metal minimum -- measured:
            # routing met4 alongside XC0 gave 6 real "MiM cap bottom plate
            # spacing < 1.2um" violations even with every landing point clear
            # of the cap. Queried from the PDK rather than hardcoded, and
            # per-macro rather than global so other macros don't pay area for
            # it. A PDK with no `capmet` rule simply gets no keepout field.
            if m["kind"] == "single_cap":
                try:
                    m["keepout_um"] = float(PDK.get_grule("capmet")["min_separation"])
                except Exception:
                    print(f"  note: {PDK_CFG.name} exposes no 'capmet' spacing rule -- "
                          f"{m['name']} carries no extra keepout")
        manifest_macros.append(m)
        md_lines.append(
            f"- **{m['name']}** ({m['kind']}, {m['generation']}): devices={m['devices']}, "
            f"size={m['w']}x{m['h']}, gds={m['gds']}"
        )

    device_index = {
        name: {"drain": d["drain"], "gate": d["gate"], "source": d["source"], "kind": d["kind"]}
        for name, d in dev_by_name.items()
    }
    for m in manifest_macros:
        for dname in m["devices"]:
            if dname in device_index:
                device_index[dname]["macro"] = m["name"]

    spacing_um, spacing_layer = min_metal_spacing_um(PDK)
    manifest = {
        "design": netlist_path.stem,
        "netlist": str(netlist_path),
        "macros": manifest_macros,
        "primitives_dir": str(out_dir),
        "modules_dir": str(modules_dir),
        "device_index": device_index,
        "manual_composition": manual_composition,
        "supply_rail_names": sorted(SUPPLY_RAIL_NAMES),
        "top_pins": top_pins,
        "min_metal_spacing_um": spacing_um,
        "min_metal_spacing_layer": spacing_layer,
    }
    if flat is not None:
        manifest["decomposed"] = {
            "top_subckt": flat.top_subckt,
            "flattened_netlist": str(source_netlist_path),
            "groups": [{k: v for k, v in g.items() if k != "finding"} for g in flat.groups],
            "nested_unexpanded": flat.nested,
        }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    md_lines.append("")
    if manual_composition:
        md_lines.append("## Manual composition needed (no dedicated glayout module)")
        for mc in manual_composition:
            md_lines.append(f"- [{mc['topology']}] {mc['devices']}: {mc['detail']} -- see {mc['doc']}")
    (out_dir / "manifest.md").write_text("\n".join(md_lines) + "\n")

    n_modules = sum(1 for m in manifest_macros if m["kind"] in COMPOSITE_MACRO_KINDS)
    print(f"\n=== Generated {len(manifest_macros)} macros from {netlist_path.name} "
          f"({n_modules} module(s) from patterns, "
          f"{len(manifest_macros) - n_modules} standalone primitive(s)) ===")
    if modules_dir != out_dir:
        print(f"  modules -> {modules_dir}")
        print(f"  primitives -> {out_dir}")
    n_auto = sum(1 for m in manifest_macros if m["generation"] == "auto")
    n_placeholder = sum(1 for m in manifest_macros if m["generation"] == "placeholder")
    n_manual = sum(1 for m in manifest_macros if m["generation"] == "manual")
    print(f"  auto={n_auto} placeholder={n_placeholder} manual(no primitive)={n_manual}")
    print(f"  min metal spacing: {spacing_um:.4f}um (from {spacing_layer!r}) -- for "
          f"anneal_placement.py's density penalty")
    if manual_composition:
        print(f"  {len(manual_composition)} topology finding(s) need manual composition -- see manifest.md")
    print(f"  wrote {out_dir}/manifest.json, {out_dir}/manifest.md")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("design_dir_or_netlist", help="design directory or a .sp netlist path")
    parser.add_argument("--out-dir", default=None,
                         help="where standalone per-device primitives, the manifest and the "
                              "derived netlists go. Default: <design_dir>/primitives/")
    parser.add_argument("--modules-dir", default=None,
                         help="where PATTERN-composed macros (current mirrors, differential "
                              "pairs -- cells/blocks/) go. Default: the same folder as "
                              "--out-dir. ../SKILL.md's Step 2 passes "
                              "<design_dir>/layout/modules")
    parser.add_argument("--no-dummy", dest="with_dummy", action="store_false", default=True)
    parser.add_argument("--decomposition", default=None,
                         help="path to circuit_decomposition.yaml, the source of pattern "
                              "grouping. Default: look for it in the design dir, the netlist's "
                              "own dir, then its parent. Without one, every device is generated "
                              "as a standalone primitive")
    parser.add_argument("--no-subckt-expand", dest="use_subckt_macros", action="store_false", default=True,
                         help="do not expand a decomposed netlist's .subckt instances. Only "
                              "affects flattening (device names and net names); pattern grouping "
                              "comes from --decomposition either way")
    parser.add_argument("--top-subckt", default=None,
                         help="which .subckt is the top-level circuit, if a decomposed netlist "
                              "defines more than one non-instantiated candidate")
    parser.add_argument("--split-mirror-legs", action="store_true", default=False,
                         help="draw only the first mirror leg with the reference and emit every "
                              "additional leg as its own standalone macro (the older behavior); "
                              "by default an N-leg mirror is ONE multi-branch current_mirror() cell")
    parser.add_argument("--per-device", action="store_true", default=False,
                         help="ignore the circuit read's patterns -- every device becomes its own "
                              "standalone primitive (see module docstring)")
    args = parser.parse_args()

    input_path = Path(args.design_dir_or_netlist).resolve()
    if input_path.is_dir():
        netlist_path = find_netlist(input_path)
        design_dir = input_path
    else:
        netlist_path = input_path
        design_dir = input_path.parent

    out_dir = Path(args.out_dir).resolve() if args.out_dir else design_dir / "primitives"
    decomposition = args.decomposition
    if decomposition is None:
        decomposition = decomposition_patterns.find_yaml(netlist_path, design_dir)
    generate(netlist_path, out_dir, with_dummy=args.with_dummy, per_device=args.per_device,
             use_subckt_macros=args.use_subckt_macros, top_subckt=args.top_subckt,
             split_mirror_legs=args.split_mirror_legs, decomposition=decomposition,
             modules_dir=args.modules_dir)


if __name__ == "__main__":
    main()
