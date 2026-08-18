#!/usr/bin/env python3
"""Grid-based A* router with PathFinder-style negotiated congestion, over a
placement already produced by `../../placer/SKILL.md` (reads
`<design_dir>/placement_pos.json` for macro positions -- the same file
`../../placement-optimizer/script/generate_grid.py`'s "Placement Grid" READS
to report grid legality; that script legalizes nothing and never writes this
file -- plus `<design_dir>/primitives/manifest.json` for net connectivity, since
`placement_pos.json`/`physical_map.json` alone are positions only, no
netlist information).

Core engine: A* on a 3D (x, y, layer) grid -- lateral moves within a layer,
"via" moves between adjacent layers at the same (x, y). Real PDK design
rules drive the grid (pitch, layer set), not hardcoded numbers, same
convention as `generate_grid.py`. Six edge-cost terms, each with its own
flag (`../SKILL.md`'s "The cost terms" documents the same six):

  - **via cost**: `--via-cost` scales each layer pair's REAL `via_stack`
    footprint (see via_footprints()), multiplied by
    `--sensitive-via-multiplier` for nets flagged sensitive (any device
    pin on the net is a MOS `gate` -- auto-detected from
    `manifest.json`'s `device_index`, the same "gate nodes are sensitive"
    language `../../../agents/layout-agent.md`'s geometry-advice
    section already uses -- not a separate ad hoc rule). The footprints
    are wildly non-uniform in sky130 (0.43 / 1.50 / 0.38um going up the
    stack, because `via3` needs a 0.65um met4 enclosure), so a flat cost
    made every transition look equally attractive.
  - **present congestion**: a cell another net's path already occupies in
    THIS pass costs `--shared-cell-penalty`. This is the other half of
    PathFinder and it was missing; `history` alone only reacts to a
    conflict one pass late, which left real cross-net shorts oscillating
    indefinitely instead of resolving.
  - **over-macro cost**: `--over-macro-cost` per grid step spent over a
    macro on a layer that macro leaves empty -- feed-through is legal but
    couples into the device below, so it must be bought deliberately.
  - **layer preference**: each routing layer is tagged H or V by
    alternating index (met2=H, met3=V, met4=H, ... -- the standard
    ASIC/analog alternating-direction convention, not invented here); a
    lateral move against a layer's preferred axis costs
    `--wrong-direction-penalty` times as much as one along it.
  - **proximity penalty**: after a net routes, every cell within
    `--proximity-radius` of its path gets a soft, falling-off extra cost
    for every OTHER net's search (not its own) -- a crosstalk-control
    lever beyond the hard DRC minimum spacing the grid pitch already
    encodes.
  - **congestion history**: real PathFinder (Ebeling/McMurchie) negotiated
    congestion -- route every net once (capacity 1 per cell), and if a
    cell is used by more than one net, bump its `history` cost and
    re-route everything; repeat up to `--ripup-iters` times. Converges
    when no cell is shared, or reports the best (fewest-overflow) result
    found within budget -- never silently claims convergence it didn't
    reach.

**Honest scope, stated plainly, not glossed over**:
  - Routing endpoints prefer a REAL glayout port when
    `generate_primitives.py` found one close enough to a macro's own bbox
    edge to trust (`world_port_map()`, sourced from that script's
    near_edge_ports() -- substrate/well tie rings, and diff_pair()'s own
    already-routed gate/drain/source stubs) -- otherwise falling back to
    the macro's own boundary point facing the net's other terminals
    (`pin_point()`), same macro-granularity abstraction
    `../../placer/script/anneal_placement.py`'s HPWL already uses, for the
    same reason (a full glayout-port-to-port map for every net is a much
    larger, per-cell-specific undertaking; see that script's own
    "deliberate scope" note). Even with real ports, this is not a claim
    every net now lands port-exact -- a human/layout-agent pass is still
    the real fallback for any net neither mechanism anchors correctly.
  - Multi-pin nets (>2 macros) are routed as a chain: each additional
    macro's pin connects via its own A* search to the NEAREST cell already
    in that net's growing tree (a standard, real simplification -- not
    a full Steiner-tree solver).
  - Supply rail nets (`manifest.json`'s `supply_rail_names`) are now
    routed like any other net (see `build_nets(..., include_supply=True)`
    in `main()`) -- connectivity comes from `manifest.json`'s
    `device_index` same as any signal net, landing points prefer each
    macro's real substrate/well tie ring when `world_port_map()` has one.
    This is still real macro-granularity rail distribution, not a proper
    ring/strap power grid -- one A*-routed trunk per rail net, no
    dedicated wide-rail geometry.
  - Macro obstacles are PER LAYER, read from each macro's own GDS (see
    build_layer_obstacles()): a layer the macro genuinely fills is blocked
    across its whole box, a layer it leaves empty is open. Feed-through
    over a device on met4/met5, and under a MiM cap on met2/met3, is
    therefore allowed. This replaced blocking every layer at once, which
    was conservative but had a structural consequence: with identical
    obstacles on every layer a via could never shorten a path, only pay
    for a direction change, so the upper layers were useless for the one
    thing upper layers exist for. Measured A/B on
    the reference decomposed netlist, same pitch/layers/costs:
    603.7um/18 vias blocked vs 567.7um/14 vias open.
  - The routing grid pitch is `base_track_pitch() * --track-multiplier`,
    where the base is the smallest pitch legal on the COARSEST routing
    layer (0.72um in sky130, set by met4/met5's 0.3/0.4 rules). It used to
    be taken from the FINEST layer, which is not a legal met4 track at
    all. **Confirmed by real testing, not just reasoned about**: a
    `--track-multiplier` that coarsens the pitch much past this closes
    real channels -- at an absolute 1.12um pitch, a gap between two macros
    could end up narrower than one grid cell, making nets genuinely
    unroutable (obstacle avoidance was working correctly; there just
    wasn't a large enough gap left to discretize into a usable free cell).

Usage:
  python route_nets.py <design_dir> [--layers met2,met3,met4,met5]
      [--track-multiplier 1] [--via-cost 10] [--sensitive-via-multiplier 3]
      [--wrong-direction-penalty 1.5] [--proximity-radius 2]
      [--proximity-weight 0.5] [--history-step 1.0] [--ripup-iters 5]
      [--over-macro-cost 0.4] [--sensitive-over-macro-multiplier 3]
      [--shared-cell-penalty 400] [--via-pad-penalty 40] [--no-feedthrough]
      [--out <design_dir>/routes.json] [--gds <design_dir>/routed.gds]
      [--summary-out <design_dir>/routing_summary.txt] [--no-gds]
"""
import argparse
import datetime
import time
import heapq
import json
import math
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE.parent.parent
REPO = SKILLS_DIR.parent.parent
# Must come before `from glayout import ...` below and take priority (index
# 0) over site-packages -- see ../../placer/script/generate_primitives.py's
# identical comment: `glayout` is also pip-installed as an editable package
# pointing at a different, external clone that doesn't carry this repo's own
# glayout fixes.
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(SKILLS_DIR.parent / "reference"))

from generate_grid import metal_glayers  # noqa: E402 -- reuse "what's a metal layer" filter

sys.path.insert(0, str(SKILLS_DIR.parent / "reference"))
from pdk_config import pdk as pdk_option  # noqa: E402 -- the project's active-PDK accessor

# The active process, from `.claude/reference/pdk_options.json`. Same accessor
# `../../placer/script/generate_primitives.py` uses, so both halves of the
# placer->router hand-off are drawing for the SAME process by construction.
PDK_CFG = pdk_option()

# Set before importing glayout: both it and magic read these from the env.
os.environ.setdefault("PDK_ROOT", PDK_CFG.pdk_root or str(Path.home() / "pdk" / "manual"))
os.environ.setdefault("PDK", PDK_CFG.pdk_env)

import gdstk
import numpy as np
import gdsfactory as gf
# Same real, confirmed fix as ../../placer/script/generate_primitives.py's
# identical line -- see its comment for the full investigation:
# gdsfactory's default `n_threads=8` makes glayout's own Component
# construction (via_stack() here) non-deterministic across runs.
gf.CONF.n_threads = 1

import glayout                                                   # noqa: E402
from glayout.backend import Component, import_gds               # noqa: E402
from glayout.primitives.via_gen import via_stack                 # noqa: E402

# The glayout MappedPDK for the selected process (`sky130`, `gf180`,
# `ihp130`), resolved by NAME from pdk_options.json's `glayout_module` rather
# than imported as `from glayout import sky130`. Every grid pitch, wire width,
# via footprint and drawn layer below is asked of THIS object's own
# `get_glayer()`/`get_grule()`, so retargeting the process is the one-word
# edit to that file the project's PDK rule promises. Same resolution as
# `../../placer/script/generate_primitives.py`'s identical block.
try:
    PDK = getattr(glayout, PDK_CFG.glayout_module)
except AttributeError:
    raise SystemExit(
        f"glayout has no PDK module named {PDK_CFG.glayout_module!r} (from "
        f"pdk_options.json's 'glayout_module' for {PDK_CFG.name}). Available: "
        f"{[n for n in dir(glayout) if n in ('sky130', 'gf180', 'ihp130')]}")

MAX_GRID_CELLS = 2_000_000


# ---------------------------------------------------------------------------
# Loading + net/macro data
# ---------------------------------------------------------------------------

def load_design(design_dir: Path):
    manifest = json.loads((design_dir / "primitives" / "manifest.json").read_text())
    placement = json.loads((design_dir / "placement_pos.json").read_text())
    return manifest, placement


def build_nets(manifest, placement, include_supply=False):
    """Returns (net_to_macros, net_kinds): net -> sorted [macro names], and
    net -> set of pin kinds ('drain'/'gate'/'source') seen on it. Only nets
    touching >=2 distinct macros that actually have real placed geometry
    (excludes manifest devices whose macro was excluded for lack of
    generated geometry -- see generate_primitives.py's "manual" case).

    `include_supply=False` (default) matches this router's original scope
    (rail distribution excluded -- see module docstring). `main()` calls
    this twice: once as-is for signal nets, once with include_supply=True
    to ALSO get real device-pin connectivity for VDD/VSS-style nets, which
    combines with generate_primitives.py's own near-edge bulk/tie ports
    (see world_port_map()) to actually distribute the rail -- a device-pin
    connection point alone (e.g. a pfet's own `source_*`) is almost always
    too deep inside its macro to trust as a landing point (see
    generate_primitives.py's near_edge_ports() docstring), so supply-net
    connectivity here mostly ends up anchored by the bulk/tie ports
    instead; this function only decides WHICH macros are on the net, not
    WHERE on each macro to land (that's build_net_landing_points())."""
    supply = {n.lower() for n in manifest.get("supply_rail_names", [])}
    placed = set(placement["positions"])
    net_macros, net_kinds = {}, {}
    for dev in manifest["device_index"].values():
        macro = dev.get("macro")
        if macro is None or macro not in placed:
            continue
        for pin in ("drain", "gate", "source"):
            net = dev.get(pin)
            if not net:
                continue
            if net.lower() in supply and not include_supply:
                continue
            net_macros.setdefault(net, set()).add(macro)
            net_kinds.setdefault(net, set()).add(pin)
    net_macros = {n: sorted(m) for n, m in net_macros.items() if len(m) >= 2}
    net_kinds = {n: k for n, k in net_kinds.items() if n in net_macros}
    return net_macros, net_kinds


def local_to_world(lx, ly, cx, cy, rotated):
    """Same transform render_gds() applies to each macro's own GDS -- move
    the macro's BBOX CENTRE to (cx,cy), then rotate about that same point --
    reused here bit-for-bit so a real port's world coordinate lands exactly
    where that macro's actual drawn geometry lands, not a
    separately-reasoned-about approximation.

    `lx, ly` are therefore expected relative to the macro's own bbox centre,
    which is how `../../placer/script/generate_primitives.py` records them.
    An earlier version of both sides used `ref.move(destination=(cx,cy))`
    with its default `origin=(0,0)` -- a pure translate of the macro's
    ORIGIN, not its centre -- and this docstring asserted that as the
    contract. For an origin-symmetric macro the two agree; for the
    composites they do not, and every one of their ports came out up to
    6.04um from the real metal.

    gdsfactory's rotate() is confirmed CCW-positive by direct test during
    this session's own development (not assumed) -- CCW-90 about (cx,cy):
    (x,y) -> (cx - (y-cy), cy + (x-cx))."""
    wx, wy = lx + cx, ly + cy
    if not rotated:
        return wx, wy
    dx, dy = wx - cx, wy - cy
    return cx - dy, cy + dx


def world_port_map(manifest, placement):
    """net -> {macro_name: [(x_um, y_um, layer_name, width_um), ...]} in WORLD
    coords, built from each macro's manifest.json `ports` (LOCAL,
    pre-placement -- see generate_primitives.py's near_edge_ports()).
    Empty for any macro generate_primitives.py didn't extract real ports
    for (older manifests, or macro kinds near_edge_ports() doesn't cover --
    see that function's docstring) -- callers must tolerate a net/macro
    combination having no entry here and fall back to pin_point().

    `width_um` is the port's own real drawn metal width (None for older
    manifests generated before this field existed) -- callers use it to
    reject candidates too narrow to safely land a via (see
    min_via_footprint_um()/filter_landable())."""
    placed = placement["positions"]
    result = {}
    for m in manifest["macros"]:
        name = m["name"]
        p = placed.get(name)
        if p is None:
            continue
        ports = m.get("ports") or {}
        if not ports:
            continue
        cx, cy = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
        rotated = bool(p.get("rotated"))
        for net, pts in ports.items():
            for pt in pts:
                wx, wy = local_to_world(pt["x"], pt["y"], cx, cy, rotated)
                result.setdefault(net, {}).setdefault(name, []).append(
                    (wx, wy, pt["layer"], pt.get("width"), pt.get("pin")))
    return result


def min_via_footprint_um(pdk, routing_layers, layer_name):
    """The narrowest real via_stack() footprint (um) that could connect a
    port on `layer_name` onto the routing grid. If `layer_name` is already
    one of `routing_layers`, the SMALLEST of the vias to its immediate
    neighbors WITHIN that list (a router hopping to the nearer neighbor is
    the best case, so that's the bar a candidate port's own metal width
    must clear). If `layer_name` is BELOW `routing_layers[0]` (e.g. a real
    met1 device port -- `build_resistor()`'s `p_top_met_*`/`n_top_met_*`,
    since routing defaults to met2+ -- see main()'s `off_grid_layer_via`
    handling for why landing there still works), the via that would
    actually get drawn is straight up to `routing_layers[0]`, so that's
    what's checked instead. `via_stack()`'s own footprint already bakes in
    the PDK's real via-width + enclosure rule (queried, not guessed) --
    see filter_landable()'s docstring for the real DRC failure this was
    added to catch (a 0.29um-wide diff_pair drain spine, too narrow for
    the 0.43um met2/met3 via_stack that would need to land on it --
    confirmed via real Magic DRC on the reference design, not
    hypothetical)."""
    if layer_name not in routing_layers:
        try:
            vs = via_stack(pdk, layer_name, routing_layers[0], centered=True)
        except Exception:
            return math.inf  # no valid direct via -- filter_landable() must reject this candidate
        (x0, y0), (x1, y1) = vs.bbox
        return max(x1 - x0, y1 - y0)
    idx = routing_layers.index(layer_name)
    neighbors = [routing_layers[i] for i in (idx - 1, idx + 1) if 0 <= i < len(routing_layers)]
    if not neighbors:
        return None
    footprints = []
    for other in neighbors:
        vs = via_stack(pdk, layer_name, other, centered=True)
        (x0, y0), (x1, y1) = vs.bbox
        footprints.append(max(x1 - x0, y1 - y0))
    return min(footprints)


def via_footprints(pdk, routing_layers):
    """Real `via_stack()` footprint (um) for every ADJACENT routing-layer
    pair, keyed by the lower layer's index.

    These are not close to uniform, and that is the single most important
    fact about routing on the upper layers in sky130:

        met2 -> met3   0.43um     (via2, met3 enclosure 0.085)
        met3 -> met4   1.50um     (via3, met4 enclosure 0.65  <-- !)
        met4 -> met5   0.38um     (via4)

    `via3`'s 0.65um met4 enclosure is a real PDK rule (queried above, not
    a glayout artifact), and it makes the met3->met4 transition cost 3.5x
    the area of either neighbour. The correct topology that falls out of
    this is the classic one: climb to met4 ONCE, do the long haul on
    met4/met5 (whose mutual transition is the cheapest in the stack, so
    the top two layers work as a proper H/V pair), and come back down
    once -- rather than hopping up and down opportunistically.

    A flat per-via cost cannot express that. `main()` turns these into
    per-pair costs so the search sees the real asymmetry.

    Returns `{lower_index: {"overall": um, lower_index: um, upper_index: um}}`
    -- the overall bbox AND the pad on each of the two layers separately,
    because those differ sharply and conflating them is expensive. The
    met3->met4 via is 1.50um of MET4 but only 0.58um of MET3; reserving
    1.50um on both (the first version here) over-reserved met3 by 2.6x and
    reported ~80-140 phantom via-pad overflow cells per pass on
    the reference decomposed netlist."""
    info = {}
    tmp = Path(tempfile.gettempdir())
    for i in range(len(routing_layers) - 1):
        lo, hi = routing_layers[i], routing_layers[i + 1]
        vs = via_stack(pdk, lo, hi, centered=True)
        (x0, y0), (x1, y1) = vs.bbox
        entry = {"overall": max(float(x1 - x0), float(y1 - y0))}
        # Per-layer extents need the real polygons, which means a GDS
        # round-trip -- gdsfactory's Component bbox is the union only.
        path = tmp / f"_via_{lo}_{hi}.gds"
        vs.write_gds(str(path))
        top = gdstk.read_gds(str(path)).top_level()[0]
        widest = {}
        for p in top.get_polygons(depth=None):
            bb = p.bounding_box()
            widest[(p.layer, p.datatype)] = max(
                widest.get((p.layer, p.datatype), 0.0),
                max(bb[1][0] - bb[0][0], bb[1][1] - bb[0][1]))
        path.unlink(missing_ok=True)
        entry[i] = widest.get(tuple(pdk.get_glayer(lo)), entry["overall"])
        entry[i + 1] = widest.get(tuple(pdk.get_glayer(hi)), entry["overall"])
        info[i] = entry
    return info


def group_by_pin(candidates):
    """Split one macro's candidates for a net by TERMINAL, preserving order.

    A port carries `pin` only if the manifest was generated with terminal
    tagging (see ../placer/script/generate_primitives.py's `tag_pin()`);
    an older manifest leaves it None, and everything then falls into one
    group -- i.e. exactly the previous one-landing-per-macro behavior, so
    this can only add connections, never drop them."""
    groups = {}
    for c in candidates:
        groups.setdefault(c[4], []).append(c)
    return groups


def filter_landable(candidates, pdk, routing_layers, wire_widths=None):
    """Drop any (wx, wy, layer_name, width_um) candidate whose real metal
    is too narrow for the via that would actually be drawn ON it.

    **Only a port on a layer OUTSIDE `routing_layers` gets a via at its own
    coordinate** -- that's `main()`'s `off_grid_layer_vias`, the hop from
    e.g. a met1 device port up to the first routing layer. For such a port
    the via's real footprint is the bar, and it is a real one: Magic DRC
    found 6 violations ("abut/overlap between subcells", met1.2, met2.2,
    via width) at 2 diff_pair_XMN1_XMN2 ports on the reference design whose
    0.29um drain-rail metal was narrower than the 0.43um via that landed
    there -- reproduced across router tuning, with a placement-only control
    run confirming it wasn't inherited from placement.

    A port already ON a routing layer gets NO via at its coordinate: the
    router simply starts that net's path on that layer, and every path via
    is emitted at a grid cell (`path_to_segments()`), never at the port.
    Requiring a via footprint there rejected ports for a via that is never
    drawn -- measured on this same design after the macro generators grew
    real edge ports: ALL 24 met2/met3 edge ports (0.29-0.33um wide) were
    rejected against a 0.43um bar, leaving only the met1 tap rings, so
    every signal net silently fell back to the box-edge approximation and
    the wires stopped short of the real ports. For those the bar is the
    routing wire's own width, which is what has to physically fit.

    A candidate with `width_um=None` (older manifest, no width recorded) is
    kept, as before."""
    kept = []
    for wx, wy, layer_name, width_um, pin in candidates:
        if width_um is None:
            kept.append((wx, wy, layer_name, width_um, pin))
            continue
        if layer_name in routing_layers:
            need = (wire_widths or {}).get(layer_name)
        else:
            need = min_via_footprint_um(pdk, routing_layers, layer_name)
        if need is None or width_um >= need:
            kept.append((wx, wy, layer_name, width_um, pin))
    return kept


def macro_box(name, placement, margin=0.0):
    """`margin` (um) pads the box outward on all 4 sides -- see
    KEEPOUT_MARGIN_UM's docstring for why this exists. Default 0.0 keeps
    every OTHER caller (macro_center, the obstacle-free-cell-vs-macro
    checks that predate this margin) working against the true box, only
    build_layer_obstacles()/pin_point()/corridor_to_edge() opt into padding."""
    p = placement["positions"][name]
    return p["x"] - margin, p["y"] - margin, p["x"] + p["w"] + margin, p["y"] + p["h"] + margin


def macro_center(name, placement):
    x0, y0, x1, y1 = macro_box(name, placement)
    return (x0 + x1) / 2, (y0 + y1) / 2


def pin_point(name, other_names, placement, margin=0.0):
    """Exact box-boundary point on `name`'s macro facing the centroid of
    `other_names` -- a real ray-from-center-to-box-edge intersection, not
    a guess. `margin`: see KEEPOUT_MARGIN_UM -- lands on the PADDED edge
    (macro_box(..., margin)) so this approximate point comes out already
    clear of build_layer_obstacles()'s own padded blocking, no corridor needed.

    Also returns (`axis`, `sign`): which coordinate (`"x"`/`"y"`) actually
    sits ON the padded edge, and which direction is further OUT from the
    macro along it (-1/+1) -- see to_grid_outward()'s docstring for why
    the caller needs this instead of a plain to_grid()."""
    x0, y0, x1, y1 = macro_box(name, placement, margin=margin)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    ocx = sum(macro_center(o, placement)[0] for o in other_names) / len(other_names)
    ocy = sum(macro_center(o, placement)[1] for o in other_names) / len(other_names)
    dx, dy = ocx - cx, ocy - cy
    half_w, half_h = (x1 - x0) / 2, (y1 - y0) / 2
    if dx == 0 and dy == 0:
        return x1, cy, "x", 1
    tx = half_w / abs(dx) if dx != 0 else math.inf
    ty = half_h / abs(dy) if dy != 0 else math.inf
    t = min(tx, ty)
    px = min(max(cx + dx * t, x0), x1)
    py = min(max(cy + dy * t, y0), y1)
    axis, sign = ("x", -1 if dx < 0 else 1) if tx <= ty else ("y", -1 if dy < 0 else 1)
    return px, py, axis, sign


def stagger_pin_point(name, other_names, placement, margin, used_edge_offsets, step):
    """Same as `pin_point()`, but nudges the result along the macro's own
    edge (the axis `pin_point()` did NOT critically constrain) when
    another net's fallback point on the SAME macro already claimed a
    position too close to this one. `used_edge_offsets[name]` collects
    every offset already claimed on that macro's edge across ALL nets
    processed so far -- `pin_point()` itself has no such memory, computing
    each net's target independently.

    **Real bug found and fixed, not hypothetical**: with
    `../placer/script/generate_primitives.py --per-device` (every device
    its own macro, no subcircuit grouping), a raw device's gate/drain/
    source/bulk are 3-4 SEPARATE external nets instead of one composite
    cell's already-internally-routed pins -- several distinct nets
    commonly fall back to `pin_point()` on the SAME macro at once. Their
    independently-computed targets can land on nearly the same point:
    confirmed via Magic DRC on the reference design's `--per-device`
    routing -- `XMP3`'s `VDD` and `net5` connections landed stubs only
    ~0.055um apart, a real different-net `met1.2` spacing violation (14
    instances), not a false positive. Fixed by tracking claimed edge
    positions per macro and pushing a colliding new one out by `step`
    (alternating direction, growing each further attempt) until clear,
    then clamping back within the macro's own true (unpadded) box extent
    on that axis so a heavily-contested macro edge still produces a
    point ON the macro, never past its corner. **Confirmed by re-running
    DRC after this fix**: those 14 instances dropped to 0.

    **Second real bug found and fixed, not hypothetical**: `used_edge_
    offsets` only ever recorded OTHER `pin_point()` fallback targets on
    this macro -- it had no memory of a REAL near-edge port (from
    `world_ports`/`world_port_map()`) another net already landed on the
    SAME macro. A fallback net's target is computed with no awareness of
    real ports at all, so it can -- and on the reference design's
    `diff_pair_XMN1_XMN2` genuinely did -- land its stub directly on top
    of a different net's real port: `net3` (the diff pair's tail, no
    qualifying real port, `pin_point()` fallback) landed on the same spot
    as `net4`'s real `drain_routeTL_BR_con_S` port, a real polygon-level
    short between two distinct circuit nets (confirmed via a direct
    segment-overlap check on `routes.json`, reproduced across three
    different placement/track-multiplier/ripup-iters combinations -- this
    was a genuine capacity bottleneck the negotiated-congestion ripup loop
    could never resolve by more iterations alone, since nothing ever told
    it that cell was permanently occupied by real device metal). Fixed by
    `main()` now pre-seeding `used_edge_offsets[macro]` with every real
    port landing point already chosen for another net on that same macro
    (see `main()`, right before this function's own loop), and by this
    function comparing full 2D distance to every claimed point (previously
    only the single non-critical-axis coordinate was compared, implicitly
    assuming every claimed point sat on the same edge/axis -- not true for
    a real port, which can sit anywhere on the macro's boundary)."""
    px, py, axis, sign = pin_point(name, other_names, placement, margin=margin)
    claimed = used_edge_offsets.setdefault(name, [])

    def point_at(coord):
        return (coord, py) if axis == "y" else (px, coord)

    coord = px if axis == "y" else py
    n = 0
    while any((point_at(coord)[0] - cx) ** 2 + (point_at(coord)[1] - cy) ** 2 < step ** 2
              for cx, cy in claimed):
        n += 1
        coord = (px if axis == "y" else py) + (1 if n % 2 else -1) * ((n + 1) // 2) * step
    x0, y0, x1, y1 = macro_box(name, placement, margin=0.0)
    if axis == "y":
        px = min(max(coord, x0), x1)
    else:
        py = min(max(coord, y0), y1)
    claimed.append((px, py))
    return px, py, axis, sign


# ---------------------------------------------------------------------------
# Grid
# ---------------------------------------------------------------------------

WIRE_WIDTH_MARGIN = 1.5     # see layer_wire_widths()
MIN_WIDTH_SLACK_UM = 0.02   # see base_track_pitch()
TRACK_GAP_SLACK_UM = 0.01   # see layer_wire_widths()


def validate_routing_layers(routing_layers, pdk):
    """Reject a `--layers` list this PDK cannot route on, before anything
    else runs.

    Three real failure modes, none of which announced itself before:

      - An unknown glayer surfaced ~2000 lines later as a raw
        `ValueError: get_grule, met9 not valid glayer` out of glayout,
        with no hint that `--layers` was the cause. This also bites the
        DEFAULT on any process whose metal stack is shallower than the
        default list, which is exactly the "works on any configured PDK"
        case this skill claims.
      - A list given out of stack order silently produced nonsense: the
        via model prices `routing_layers[i] -> routing_layers[i+1]` as an
        ADJACENT pair, and layer_dirs() assigns H/V by list index, so both
        are wrong the moment index order stops matching stack order.
      - A repeated layer quietly created a "via" from a layer to itself.

    `metal_glayers()` is asked what the metal stack IS (the same filter
    `../../placement-optimizer/script/generate_grid.py` uses), so nothing
    process-specific is written down here."""
    available = metal_glayers(pdk)
    unknown = [g for g in routing_layers if g not in available]
    if unknown:
        raise SystemExit(
            f"--layers names {unknown}, which {pdk.name} has no routing metal for.\n"
            f"  this PDK's metal stack: {','.join(available)}")
    if len(set(routing_layers)) != len(routing_layers):
        raise SystemExit(f"--layers repeats a layer: {','.join(routing_layers)}")
    if len(routing_layers) < 2:
        raise SystemExit(
            "--layers needs at least 2 layers: with one layer there is no via to "
            "take and no H/V alternation, so nothing can cross anything else.")
    order = [available.index(g) for g in routing_layers]
    if order != sorted(order):
        raise SystemExit(
            f"--layers must be given low-to-high up the stack, not "
            f"{','.join(routing_layers)}.\n"
            f"  vias are priced between CONSECUTIVE entries and H/V alternates by "
            f"index, so out-of-order entries route on a grid that does not exist.\n"
            f"  this PDK's stack order: {','.join(available)}")
    return available


def base_track_pitch(pdk, routing_layers):
    """The smallest track pitch legal on EVERY routing layer.

    One grid, shared by all layers (a via is a move between two layers at
    the SAME (ix, iy), so the layers must line up), which means the pitch
    is set by the COARSEST layer, not the finest. This function used to
    take `min(...)` -- met2's 0.28um -- and apply it to met4/met5 as well.
    That is not a legal met4 track: sky130 met4/met5 are `min_width=0.3,
    min_separation=0.4`, so two met4 wires on adjacent 0.56um tracks
    (0.45um wide at the time) left a 0.11um gap against a 0.4um rule -- a
    4x violation. It never fired only because met4 carried 22.9% of the
    wire on this project's own test design and no two met4 tracks ever
    happened to land adjacent; it would have fired immediately once the
    upper layers started carrying real traffic, which is exactly what the
    per-layer obstacle work below is for.

    `MIN_WIDTH_SLACK_UM` keeps the wire strictly WIDER than bare
    `min_width` even on the layer that sets the pitch -- drawing at
    exactly min_width trips Magic's strict "< min_width" check on
    float/GDS-quantization round-trip (see main()'s wire_widths note for
    the original finding)."""
    return max(pdk.get_grule(g)["min_width"] + MIN_WIDTH_SLACK_UM
               + pdk.get_grule(g)["min_separation"] for g in routing_layers)


def layer_wire_widths(pdk, routing_layers, pitch):
    """Routed wire width per layer: `WIRE_WIDTH_MARGIN` x min_width where
    the shared pitch can afford it, otherwise the widest wire that still
    leaves a legal gap between two adjacent tracks on that layer.

    Both bounds matter. The margin is why met2/met3 are drawn at 0.21um
    rather than a bare 0.14um (see main()'s own note on the Magic
    min-width round-trip). The pitch cap is why met4/met5 come out at
    0.31um instead of 1.5 x 0.3 = 0.45um: at 0.45um two adjacent met4
    tracks would be 0.4um apart edge-to-edge only if the pitch were
    0.85um, and paying 0.85um of pitch on EVERY layer to widen the two
    that carry the least traffic is a bad trade -- the whole grid gets
    coarser, and coarse grids are what made nets unroutable the last time
    this was tuned (see --track-multiplier's help)."""
    widths = {}
    for g in routing_layers:
        r = pdk.get_grule(g)
        cap = pitch - r["min_separation"] - TRACK_GAP_SLACK_UM
        w = min(r["min_width"] * WIRE_WIDTH_MARGIN, cap)
        if w < r["min_width"]:
            raise ValueError(
                f"routing layer {g} needs min_width={r['min_width']}um + "
                f"min_separation={r['min_separation']}um but the shared grid pitch is only "
                f"{pitch:.4f}um -- raise --track-multiplier or drop {g} from --layers")
        widths[g] = w
    return widths


def compute_grid(placement, pdk, routing_layers, track_multiplier):
    xs0 = [p["x"] for p in placement["positions"].values()]
    ys0 = [p["y"] for p in placement["positions"].values()]
    xs1 = [p["x"] + p["w"] for p in placement["positions"].values()]
    ys1 = [p["y"] + p["h"] for p in placement["positions"].values()]
    x0, y0, x1, y1 = min(xs0), min(ys0), max(xs1), max(ys1)
    margin = max(0.15 * max(x1 - x0, y1 - y0), 5.0)
    x0, y0, x1, y1 = x0 - margin, y0 - margin, x1 + margin, y1 + margin

    base = base_track_pitch(pdk, routing_layers)
    pitch = base * track_multiplier
    n_cols = max(1, int(math.ceil((x1 - x0) / pitch)))
    n_rows = max(1, int(math.ceil((y1 - y0) / pitch)))
    guard = 0
    while n_cols * n_rows * len(routing_layers) > MAX_GRID_CELLS and guard < 6:
        pitch *= 1.5
        n_cols = max(1, int(math.ceil((x1 - x0) / pitch)))
        n_rows = max(1, int(math.ceil((y1 - y0) / pitch)))
        guard += 1
        print(f"  warning: grid too large for {len(routing_layers)} layers -- "
              f"coarsening pitch to {pitch:.4f}um for tractability (not a DRC value)")
    return {"x0": x0, "y0": y0, "pitch": pitch, "n_cols": n_cols, "n_rows": n_rows,
            "base_pitch": base}


def to_grid(x, y, grid):
    return (round((x - grid["x0"]) / grid["pitch"]), round((y - grid["y0"]) / grid["pitch"]))


def to_grid_outward(x, y, axis, sign, grid):
    """Same as `to_grid()`, except the coordinate along `axis` rounds AWAY
    from the macro (floor if `sign<0`, ceil if `sign>0`) instead of to the
    nearest grid line. Plain `round()` on a `pin_point(..., margin=
    keepout_margin)` result can snap BACK across the padded edge whenever
    `grid["pitch"]/2 > keepout_margin` -- true here by construction
    (`KEEPOUT_MARGIN_MULT * min_metal_spacing_um` is a real DRC spacing
    value, unrelated in magnitude to the router's own grid pitch, a
    routability/tractability knob -- nothing keeps one bigger than the
    other). **Real bug found and fixed, not hypothetical**: on
    the reference design (pitch=0.56um, keepout_margin=0.14um, so
    quantization error of up to pitch/2=0.28um exceeds the entire margin),
    a `pin_point()` fallback landing meant for `diff_pair_XMN1_XMN2`'s
    bottom edge rounded to a grid cell BACK inside the macro's real
    territory, producing 4 real Magic DRC violations (17 rule instances:
    met1.2 + met2.2 spacing) right at that landing cell -- confirmed via
    `drc listall why` coordinates matching this exact point, and confirmed
    NOT a real-port issue (this net has no `world_port_map()` entry at
    all, so `filter_landable()` never touches it). The non-critical axis
    still rounds to nearest -- it only moves the landing point sideways
    along the padded edge, never closer to the macro, so nearest rounding
    there is already safe."""
    ix = (x - grid["x0"]) / grid["pitch"]
    iy = (y - grid["y0"]) / grid["pitch"]
    if axis == "x":
        ix = math.floor(ix) if sign < 0 else math.ceil(ix)
        iy = round(iy)
    else:
        iy = math.floor(iy) if sign < 0 else math.ceil(iy)
        ix = round(ix)
    return int(ix), int(iy)


def to_real(ix, iy, grid):
    return grid["x0"] + ix * grid["pitch"], grid["y0"] + iy * grid["pitch"]


KEEPOUT_MARGIN_MULT = 1.0  # see build_layer_obstacles()'s docstring. 2.0 was tried
# first and real-tested: it closed off 5/8 nets' routing channels entirely on
# the reference design's own already-tight (density-penalty-legal but not
# spacious) placement -- too aggressive against real channel widths. 1.0 (one
# real min_metal_spacing) is the smallest value that still gives a non-zero
# keepout on every macro edge; if a real short is still found with this
# margin, don't silently raise it here -- re-anneal with a higher
# --w-density first (the actual lever for wider channels), same guidance
# ../SKILL.md's --track-multiplier note already gives for a similar tradeoff.


OCCUPANCY_FULL_BLOCK = 0.02   # see build_layer_obstacles()
MAX_DILATE_POLYGONS = 600     # see build_layer_obstacles()

_MACRO_GDS_CACHE = {}


def macro_layer_geometry(gds_path):
    """{(gds_layer, gds_datatype): (list of Nx2 point arrays, total_area)}
    for one macro's flattened GDS, plus its own bbox. Cached per path --
    a macro's GDS is read once no matter how many layers ask for it."""
    key = str(gds_path)
    if key in _MACRO_GDS_CACHE:
        return _MACRO_GDS_CACHE[key]
    lib = gdstk.read_gds(key)
    top = lib.top_level()[0]
    by_layer = {}
    for p in top.get_polygons(depth=None):
        pts, area = by_layer.setdefault((p.layer, p.datatype), ([], [0.0]))
        pts.append(p.points)
        area[0] += abs(p.area())
    (gx0, gy0), (gx1, gy1) = top.bounding_box()
    result = ({k: (v[0], v[1][0]) for k, v in by_layer.items()},
              (float(gx0), float(gy0), float(gx1), float(gy1)))
    _MACRO_GDS_CACHE[key] = result
    return result


def macro_polygons_world(gds_path, p):
    """That macro's polygons transformed into world coordinates, using the
    EXACT transform render_gds() applies to the same GDS (bbox centre to
    the placed box's centre, then optional 90 degree rotation about that
    same point -- see local_to_world()). Shared code path is the point: an
    obstacle map derived from a different transform than the one that
    actually draws the metal is worse than no obstacle map at all."""
    by_layer, (gx0, gy0, gx1, gy1) = macro_layer_geometry(gds_path)
    gcx, gcy = (gx0 + gx1) / 2, (gy0 + gy1) / 2
    cx, cy = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
    rotated = bool(p.get("rotated"))
    out = {}
    for key, (polys, area) in by_layer.items():
        moved = []
        for pts in polys:
            lx = pts[:, 0] - gcx
            ly = pts[:, 1] - gcy
            if rotated:
                wx, wy = cx - ly, cy + lx
            else:
                wx, wy = cx + lx, cy + ly
            moved.append(np.column_stack((wx, wy)))
        out[key] = (moved, area)
    return out


def build_layer_obstacles(manifest, placement, grid, routing_layers, pdk,
                          wire_widths, margin=0.0, keepouts=None, feedthrough=True):
    """Per-LAYER obstacle map: a set of `(ix, iy, layer_index)` cells, plus
    the set of `(ix, iy)` cells that lie over some macro at all.

    This replaces a 2-D `(ix, iy)` blocked set that blocked every routing
    layer at once -- i.e. "macro interiors block every routing layer
    entirely (no feed-through)". That was a real, deliberate conservatism,
    but it had a consequence worth stating plainly: with identical
    obstacles on every layer, a layer change could never shorten a path,
    only pay for a direction change. The upper layers were therefore
    structurally useless for exactly the thing upper layers exist for.
    Measured on the reference design, the metal each macro actually
    draws is nothing like a solid box above met2:

        macro                      met2    met3    met4    met5
        current_mirror_XMN4_XMN3   35.7%    1.3%    0.9%    0.0%
        diff_pair_XMN1_XMN2        30.4%   11.1%    0.7%    0.0%
        XMP3                       26.6%    0.0%    0.0%    0.0%
        XC0 (MiM cap)               0.0%    0.0%  100.0%  116.4%

    -- so ~2000um2 of met5 over the two big FET macros was blocked while
    being completely empty, and XC0's 45x45um footprint was blocked on
    met2/met3 where the cap has no metal at all (a MiM cap is capmet
    between met4 and met5; met2/met3 pass under it untouched).

    The rule, per macro and per layer independently:

      - occupancy >= `OCCUPANCY_FULL_BLOCK` of the macro's own box: block
        the whole padded box, exactly as before. This is the macro's own
        device/routing territory -- weaving a foreign net between a FET's
        source/drain rails would be legal metal and terrible analog
        layout, and the router has no model of what it would be coupling
        into.
      - otherwise: block only the real polygons, dilated by that layer's
        own clearance, so the genuinely empty area opens up.

    Deliberately NOT monotone up the stack (no "block everything below the
    top occupied layer"): XC0 is the counterexample that kills that
    shortcut -- its occupied layers are met4/met5 with met2/met3 free
    underneath, which a monotone rule would get exactly backwards.

    `keepouts` (a macro's own larger clearance demand, e.g. a MiM cap's
    1.2um `capm.2b` spacing) applies on the layers that macro actually
    occupies -- which for a cap is met4/met5, the two layers capmet
    interacts with, and not the met2/met3 the rule now opens up."""
    keepouts = keepouts or {}
    blocked = set()
    over_macro = set()
    gds_by_name = {m["name"]: m.get("gds") for m in manifest["macros"]}
    pitch = grid["pitch"]
    stats = {}

    def block_box(x0, y0, x1, y1, li, sink):
        # Lattice points actually INSIDE [x0,x1] x [y0,y1]: ceil for the low
        # bound, floor for the high one. The box already carries its full
        # keepout margin, so a point just outside it is legal by
        # construction and must not be blocked. Rounding outward instead
        # (floor/ceil) silently pads every macro by up to a further cell per
        # side -- 0.72um here -- which narrows every channel between two
        # macros by ~1.4um and shows up as detour length, not as an error.
        ix_lo = int(math.ceil((x0 - grid["x0"]) / pitch - 1e-9))
        ix_hi = int(math.floor((x1 - grid["x0"]) / pitch + 1e-9))
        iy_lo = int(math.ceil((y0 - grid["y0"]) / pitch - 1e-9))
        iy_hi = int(math.floor((y1 - grid["y0"]) / pitch + 1e-9))
        for ix in range(ix_lo, ix_hi + 1):
            for iy in range(iy_lo, iy_hi + 1):
                sink((ix, iy, li))

    for name, p in placement["positions"].items():
        m_keep = max(margin, float(keepouts.get(name) or 0.0))
        bx0, by0 = p["x"], p["y"]
        bx1, by1 = p["x"] + p["w"], p["y"] + p["h"]
        box_area = max((bx1 - bx0) * (by1 - by0), 1e-9)
        block_box(bx0, by0, bx1, by1, 0, lambda c: over_macro.add((c[0], c[1])))

        gds_path = gds_by_name.get(name)
        geom = None
        if gds_path and Path(gds_path).exists():
            geom = macro_polygons_world(gds_path, p)

        for li, glayer in enumerate(routing_layers):
            gds_key = tuple(pdk.get_glayer(glayer))
            polys, area = (geom or {}).get(gds_key, ([], 0.0))
            occupancy = area / box_area
            # No geometry file, or the layer is genuinely busy -> the old
            # whole-box block. Also the fallback when a layer has so many
            # separate polygons that dilating them all would cost more
            # runtime than the freed area is worth.
            # Clearance a wire's CENTRELINE must keep from this macro's metal
            # on this layer. Identical formula in both branches below -- the
            # box branch used to pad by `m_keep` alone, i.e. by the spacing
            # rule but NOT by the wire's own half-width, so the first
            # unblocked track outside a macro put a wire edge only
            # `m_keep - width/2` from that macro's metal. On met2 that is
            # 0.14 - 0.105 = 0.035um against a 0.14um rule. Real, and it was
            # masked rather than absent before: the old whole-box blocker
            # rounded its bounds to the nearest grid line, which happened to
            # add up to half a pitch of accidental padding. Found as 8 met1.2
            # errors along diff_pair_XMN1_XMN2's right edge once the bounds
            # were rounded correctly.
            clearance = (max(m_keep, pdk.get_grule(glayer)["min_separation"])
                         + wire_widths[glayer] / 2)
            if (not feedthrough or geom is None or occupancy >= OCCUPANCY_FULL_BLOCK
                    or len(polys) > MAX_DILATE_POLYGONS):
                if not feedthrough:
                    pad = max(m_keep, clearance)
                    block_box(bx0 - pad, by0 - pad, bx1 + pad, by1 + pad, li, blocked.add)
                    stats.setdefault(name, {})[glayer] = "full"
                    continue
                if geom is not None and not polys and occupancy == 0.0:
                    stats.setdefault(name, {})[glayer] = "free"
                    continue
                pad = max(m_keep, clearance)
                block_box(bx0 - pad, by0 - pad, bx1 + pad, by1 + pad, li, blocked.add)
                stats.setdefault(name, {})[glayer] = "full"
                continue
            if not polys:
                stats.setdefault(name, {})[glayer] = "free"
                continue
            # Clearance a wire's CENTRELINE must keep from this macro's real
            # metal on this layer: the wire's own half-width plus the layer's
            # real minimum spacing. Dilating the macro geometry by exactly
            # that and testing grid POINTS is equivalent to (and much cheaper
            # than) testing every wire rectangle against every polygon --
            # the grid's cells are lattice points, and a wire centred on a
            # point outside the dilated shape is spacing-legal by
            # construction.
            dilated = gdstk.offset([gdstk.Polygon(pts) for pts in polys], clearance,
                                    join="bevel", use_union=True, precision=1e-4)
            cells = rasterize_polygons(dilated, grid, li)
            blocked |= cells
            stats.setdefault(name, {})[glayer] = f"geom({len(polys)}p,{len(cells)}c)"
    return blocked, over_macro, stats


def rasterize_polygons(polygons, grid, li):
    """Grid cells whose lattice point falls inside any of `polygons`."""
    if not polygons:
        return set()
    pitch = grid["pitch"]
    bx0 = min(float(p.points[:, 0].min()) for p in polygons)
    bx1 = max(float(p.points[:, 0].max()) for p in polygons)
    by0 = min(float(p.points[:, 1].min()) for p in polygons)
    by1 = max(float(p.points[:, 1].max()) for p in polygons)
    ix_lo = int(math.floor((bx0 - grid["x0"]) / pitch))
    ix_hi = int(math.ceil((bx1 - grid["x0"]) / pitch))
    iy_lo = int(math.floor((by0 - grid["y0"]) / pitch))
    iy_hi = int(math.ceil((by1 - grid["y0"]) / pitch))
    idx = [(ix, iy) for ix in range(ix_lo, ix_hi + 1) for iy in range(iy_lo, iy_hi + 1)]
    if not idx:
        return set()
    pts = [(grid["x0"] + ix * pitch, grid["y0"] + iy * pitch) for ix, iy in idx]
    flags = gdstk.inside(pts, polygons)
    return {(ix, iy, li) for (ix, iy), f in zip(idx, flags) if f}


# `build_obstacles()` (whole-box, all-layers) lived here and is now
# `build_layer_obstacles()` above. Two findings from it that still apply
# and must not be re-learned the hard way:
#   - The `margin` padding is not cosmetic. Blocking each macro's TRUE box
#     accounts for a wire's own half-width at the edge but NOT for sky130
#     minimum spacing beyond it: routing 8 nets on the reference design
#     without it produced real met2.2/met1.2 violations, one of which a
#     real LVS run showed had merged VDD into the shared nfet-substrate
#     plane -- a genuine short, not a nitpick.
#   - `keepout_um` (per macro) is likewise real: routing met4 alongside
#     XC0 at the general ~0.14um metal minimum produced 6 real "MiM cap
#     bottom plate spacing < 1.2um" violations even though every landing
#     point was clear. Per-macro rather than a bigger global margin --
#     raising it for everyone costs area on all 7 macros to fix one.


def _disc(ix, iy, l, reach, pitch):
    """Grid cells within `reach` um of (ix, iy) on layer `l`."""
    r = int(math.ceil(reach / pitch - 1e-9))
    return {(ix + dx, iy + dy, l) for dx in range(-r, r + 1) for dy in range(-r, r + 1)}


def via_pad_cells(ix, iy, li, grid, via_pads, wire_widths, routing_layers, pdk):
    """`(metal, halo)` cell sets for a via at `(ix, iy)` between layers `li`
    and `li+1`, on BOTH of those layers.

    `metal` is where the via's own pad actually is -- another net's wire
    there is a SHORT. `halo` is the ring beyond it that the pad's minimum
    spacing sterilizes -- another net's wire there is a spacing violation,
    not a short. They are separated because the penalties differ by an
    order of magnitude and lumping them made every pad look like a short.

    This exists at all because the pads are not small relative to the grid.
    `via_stack(met3, met4)` is 1.5um square (sky130's `via3` needs a 0.65um
    met4 enclosure -- a real queried rule, see via_footprints()), which on
    a 0.72um pitch spans more than two cells in every direction. The plain
    grid model -- "a via occupies one cell" -- is simply false for that
    transition, and with the upper layers now actually reachable it is
    exercised constantly rather than a handful of times per design."""
    pitch = grid["pitch"]
    metal, halo = set(), set()
    for l in (li, li + 1):
        if not (0 <= l < len(routing_layers)):
            continue
        glayer = routing_layers[l]
        # The pad ON THIS LAYER, not the via's overall bbox -- see
        # via_footprints() for why the difference matters (met3->met4 is
        # 1.50um of met4 but 0.58um of met3).
        pad = via_pads[li][l] / 2
        m = _disc(ix, iy, l, pad, pitch)
        metal |= m
        halo |= _disc(ix, iy, l, pad + pdk.get_grule(glayer)["min_separation"]
                       + wire_widths[glayer] / 2, pitch) - m
    return metal, halo


def landing_stub_cells(stub, grid, wire_widths, routing_layers, pdk):
    """Cells the landing stub for one pin will cover, plus that layer's
    spacing -- so the stub can be reserved for its own net BEFORE routing
    starts.

    Landing stubs are emitted after `route_design()` has finished, from the
    grid cell to the pin's exact off-grid coordinate (see main()'s
    `landing_stubs`). That put them entirely outside the congestion model:
    two nets' stubs, or one net's stub and another's path wire, could
    overlap and nothing would notice. Real short, found on
    the reference design once per-layer obstacles opened met2 over `R0`
    (a 2.0um-tall resistor whose own metal is met1, so met2 across its
    body is now free): `vout` and `net5` are R0's two terminals, both
    landed on it, and their stubs overlapped `net5`'s met2 wire by
    0.72 x 0.21um and 2 x 0.21 x 0.105um -- three real shorts between two
    different nets, invisible to a congestion check that only ever looked
    at path cells.

    Returns `(metal, halo)`, same split as via_pad_cells(): the stub's own
    rectangle versus the spacing ring around it.

    Deliberately conservative: the exact emitted rectangle depends on
    clamp/merge/via-expansion decisions made later, so the `metal` box is
    grid-cell-to-target padded by half-width, or by the via footprint
    where one is drawn there. Over-reserving costs a small detour;
    under-reserving costs a short."""
    layer = stub["layer"]
    gx, gy = to_real(stub["ix"], stub["iy"], grid)
    tx, ty = stub["x_um"], stub["y_um"]
    sep = pdk.get_grule(layer)["min_separation"]
    core = wire_widths[layer] / 2
    via_um = stub.get("via_um")
    if via_um and math.isfinite(via_um):
        core = max(core, via_um / 2)
    li = routing_layers.index(layer) if layer in routing_layers else 0
    pitch = grid["pitch"]

    def box(pad, contain):
        """`contain=True`: lattice points genuinely INSIDE the padded box
        (ceil the low bound, floor the high one). `contain=False`: round
        outward instead, covering any point the box touches at all.

        The distinction matters and is not cosmetic. The metal box says
        "this cell HAS another net's conductor on it", which costs
        `--shared-cell-penalty` -- so rounding it outward over-claims up to
        a full cell (0.72um) per side around geometry only 0.2-0.5um wide,
        and manufactures phantom shorts against nets that are nowhere near
        the real polygon. Same rounding bug as build_layer_obstacles()'s
        `block_box()` had, same fix. The halo is only a soft cost, so
        rounding it outward is the safe direction there."""
        x0, x1 = min(gx, tx) - pad, max(gx, tx) + pad
        y0, y1 = min(gy, ty) - pad, max(gy, ty) + pad
        if contain:
            xr = range(int(math.ceil((x0 - grid["x0"]) / pitch - 1e-9)),
                       int(math.floor((x1 - grid["x0"]) / pitch + 1e-9)) + 1)
            yr = range(int(math.ceil((y0 - grid["y0"]) / pitch - 1e-9)),
                       int(math.floor((y1 - grid["y0"]) / pitch + 1e-9)) + 1)
        else:
            xr = range(int(math.floor((x0 - grid["x0"]) / pitch)),
                       int(math.ceil((x1 - grid["x0"]) / pitch)) + 1)
            yr = range(int(math.floor((y0 - grid["y0"]) / pitch)),
                       int(math.ceil((y1 - grid["y0"]) / pitch)) + 1)
        return {(ix, iy, li) for ix in xr for iy in yr}

    # The stub's own grid cell is always its conductor, even when the stub
    # is narrower than one cell and the containment test would come back
    # empty -- that cell is where the wire actually attaches.
    metal = box(core, True) | {(stub["ix"], stub["iy"], li)}
    return metal, box(core + sep + wire_widths[layer] / 2, False) - metal


class GridState:
    """Live per-cell occupancy of the routing grid, committed to after
    EVERY routing action.

    This is the bookkeeping the negotiated-congestion loop runs on, and it
    replaces three separate ad-hoc dicts (`occupied`, `reserved`, `pads`)
    that each recorded part of the picture with its own conventions. The
    thing they had in common was a gap: nothing was written back to a
    shared grid as a net finished, so a later net in the same pass could
    not see what an earlier one had already put down. `history` reacted,
    but only one pass late, and a pass late is not good enough when the
    consequence is a real short. Every defect below was a symptom of that
    one omission:

      - two nets crossing at a grid point (real overlapping metal in
        `routes.json`, oscillating 2-6 cells indefinitely instead of
        converging);
      - a landing stub laid over another net's wire, because stubs were
        emitted after routing finished and were never in the model at all;
      - a wire run through another net's via pad, because a via was
        modelled as occupying its single cell when `via_stack(met3, met4)`
        is 1.5um square -- more than two cells wide on a 0.72um pitch.

    Two independent layers of state, because the two failures are not the
    same failure and must not carry the same price:

      `metal` -- real conductor: path cells, via pads, landing stubs.
                 Another net here is a SHORT.
      `halo`  -- the minimum-spacing ring around that conductor. Another
                 net here is a spacing violation, not a short.

    Both map cell -> net of the FIRST claimant. First-come is not a
    fairness policy, it is just a record of what is physically there; the
    ripup loop rebuilds this from scratch each pass and `history` (which
    does persist) is what actually arbitrates between passes."""

    __slots__ = ("metal", "halo", "metal_kind", "shorts", "exact_shorts", "spacing",
                 "_seed_shorts", "_seed_spacing")

    def __init__(self):
        self.metal = {}        # cell -> first net to put conductor here
        self.halo = {}         # cell -> first net whose spacing ring covers it
        self.metal_kind = {}   # cell -> "path" | "stub" (see claim())
        self.shorts = {}       # cell -> set of nets with conductor on one cell
        self.exact_shorts = {} # subset of `shorts` where both sides are path cells
        self.spacing = {}      # cell -> set of nets, conductor in another's ring
        self._seed_shorts = frozenset()
        self._seed_spacing = frozenset()

    def freeze_seed(self):
        """Mark everything committed so far as pre-existing, so
        `conflicts()` reports only what ROUTING went on to add.

        Called after the landing stubs are seeded. Their geometry is fixed
        before routing starts -- it follows from the port coordinates and
        the placement -- so a stub-on-stub overlap is a port-spacing
        problem, reported once as a warning, and not something any amount
        of ripup can resolve. Leaving it in the per-pass count made the
        loop stall at a constant 16 cells while `history` piled cost onto
        cells nothing could vacate, distorting the routes around them for
        no benefit.

        Note these seed overlaps are mostly quantization anyway: the
        rasterized stub footprint is whole 0.72um cells around geometry
        that is 0.2-0.5um wide, so cell-level overlap is much coarser than
        the real polygons (which come out DRC-clean)."""
        self._seed_shorts = frozenset(self.shorts)
        self._seed_spacing = frozenset(self.spacing)

    def claim(self, net, metal_cells=(), halo_cells=(), kind="path"):
        """Commit one routing action's footprint and record any collision
        it creates. Call this immediately after the action, not at the end
        of the pass -- the whole point is that the next search sees it.

        Collisions are detected HERE rather than by scanning the maps
        afterwards, because the maps keep only the first claimant per cell
        (they answer "what is physically here", which is what `penalty()`
        needs). A second claimant overwrites nothing and would leave no
        trace to scan for."""
        for c in metal_cells:
            owner = self.metal.setdefault(c, net)
            if owner != net:
                self.shorts.setdefault(c, {owner}).add(net)
                # A conflict is only exactly-modelled when BOTH sides are
                # path cells. A landing stub's footprint is a conservative
                # whole-cell box around geometry 0.2-0.5um wide on a 0.72um
                # grid, so a path "on" a stub cell is an upper bound, not a
                # measurement -- verify those against DRC rather than
                # treating them as proven shorts. Path-on-path has no such
                # slack and must reach zero.
                if kind == "path" and self.metal_kind.get(c) == "path":
                    self.exact_shorts.setdefault(c, {owner}).add(net)
            self.metal_kind.setdefault(c, kind)
            other = self.halo.get(c)
            if other is not None and other != net:
                self.spacing.setdefault(c, {other}).add(net)
        for c in halo_cells:
            self.halo.setdefault(c, net)
            owner = self.metal.get(c)
            if owner is not None and owner != net:
                self.spacing.setdefault(c, {owner}).add(net)

    def penalty(self, cell, net, weights):
        """Extra cost for `net` to route through `cell` given what is
        already committed there. Its own metal is free -- a net rejoining
        its own tree is the normal case, not a conflict."""
        cost = 0.0
        owner = self.metal.get(cell)
        if owner is not None and owner != net:
            cost += weights["shared_cell_penalty"]
        owner = self.halo.get(cell)
        if owner is not None and owner != net:
            cost += weights["via_pad_penalty"]
        return cost

    def conflicts(self):
        """`(shorts, spacing)` recorded during this pass.

        `shorts` is conductor on conductor -- a real short. `spacing` is
        one net's conductor inside another's clearance ring. Halo-on-halo
        is deliberately NOT a conflict: two clearance rings may overlap
        freely so long as neither net puts conductor in the other's, and
        counting them was what made an earlier version report 80-140
        congested cells on a design with no real conflict at all.

        Excludes anything present at `freeze_seed()` time -- see there."""
        return ({c: v for c, v in self.shorts.items() if c not in self._seed_shorts},
                {c: v for c, v in self.spacing.items() if c not in self._seed_spacing})

    def exact_conflicts(self):
        """Just the path-on-path shorts -- the subset the grid model gets
        EXACTLY right, with no whole-cell rasterization slack. This is the
        number that must be zero; see claim()'s note."""
        return {c: v for c, v in self.exact_shorts.items() if c not in self._seed_shorts}


def unblock_pins(blocked, nets_grid, n_layers, extra_corridors=()):
    """Carve each net's own pin cell back out of `blocked` -- the one
    doorway into its macro's otherwise fully-blocked box. Pin cells are
    computed on their OWN macro's boundary (pin_point()), and macros don't
    overlap (Step 2's SA placement is overlap-verified before this ever
    runs) -- so this can only carve a doorway into the macro that pin
    actually belongs to, not a foreign one, unless two macros sit closer
    together than one grid pitch (a real placement-density edge case;
    coarsen `--track-multiplier` down or check the placement if routing
    quality looks wrong near a tightly-packed pair).

    `extra_corridors`: cells from corridor_to_edge() -- a real port that
    world_port_map() found sits INSIDE a macro's box (see
    generate_primitives.py's PORT_EDGE_MARGIN_UM), not on its boundary
    like pin_point()'s cells always are. These get unblocked too so the
    router can actually reach that port from open space. Real, honestly-
    documented approximation, same category as this function's own
    boundary-carve-out above: the unblocked strip is shared grid state, so
    in principle some OTHER net's search could route through it too, not
    just the net that owns that port -- bounded in practice because
    corridors are short (<= PORT_EDGE_MARGIN_UM) and only ever carved
    through a macro's own guard-ring/tap-ring/already-routed-stub
    territory (see generate_primitives.py's near_edge_ports() docstring
    for why only those port families clear the margin), not through
    active device area -- verify with the DRC step below, don't just
    assume it's fine."""
    # `blocked` holds (ix, iy, layer) since build_layer_obstacles(). A pin
    # cell is carved out on EVERY routing layer at that (x, y), not just
    # the pin's own: the net has to be able to leave the pin, and with
    # per-layer obstacles the layer it leaves on is the router's choice,
    # not a foregone conclusion. Carving one layer only would leave a pin
    # reachable exclusively along its own layer -- the pre-per-layer
    # behaviour, minus the guarantee that that layer is open.
    for cells in nets_grid.values():
        for c in cells:
            for l in range(n_layers):
                blocked.discard((c[0], c[1], l))
    for c in extra_corridors:
        for l in range(n_layers):
            blocked.discard((c[0], c[1], l))


def corridor_to_edge(ix, iy, macro_name, placement, grid, margin=0.0):
    """Straight-line grid cells from an interior real-port cell (ix,iy) out
    to the NEAREST edge of its own macro's PADDED box (whichever of the 4
    directions is fewest grid steps) -- see unblock_pins()'s
    `extra_corridors` docstring for why this is needed and what its real
    scope/risk is. `margin` must match whatever build_layer_obstacles() padded
    with, or the corridor would stop short of actually-blocked cells."""
    x0, y0, x1, y1 = macro_box(macro_name, placement, margin=margin)
    ix0, iy0 = to_grid(x0, y0, grid)
    ix1, iy1 = to_grid(x1, y1, grid)
    d_left, d_right = ix - ix0, ix1 - ix
    d_bottom, d_top = iy - iy0, iy1 - iy
    dmin = min(d_left, d_right, d_bottom, d_top)
    if dmin == d_left:
        return [(x, iy) for x in range(ix0, ix + 1)]
    if dmin == d_right:
        return [(x, iy) for x in range(ix, ix1 + 1)]
    if dmin == d_bottom:
        return [(ix, y) for y in range(iy0, iy + 1)]
    return [(ix, y) for y in range(iy, iy1 + 1)]


# ---------------------------------------------------------------------------
# A* search
# ---------------------------------------------------------------------------

def astar(start, goals, blocked, grid, layer_dirs, history, proximity, current_net, weights,
          state=None):
    n_cols, n_rows, n_layers = grid["n_cols"], grid["n_rows"], len(layer_dirs)
    pitch = grid["pitch"]
    state = state if state is not None else GridState()
    over_macro = weights["over_macro"]
    over_cost = weights["over_macro_cost"] * (
        weights["sensitive_over_macro_multiplier"] if current_net in weights["sensitive_nets"] else 1.0)

    def heuristic(c):
        ix, iy, _ = c
        return min(pitch * (abs(ix - gx) + abs(iy - gy)) for gx, gy, _ in goals)

    def in_bounds(c):
        ix, iy, l = c
        return 0 <= ix < n_cols and 0 <= iy < n_rows and 0 <= l < n_layers

    def passable(nc):
        # `blocked` holds (ix, iy, layer) triples since
        # build_layer_obstacles() -- a macro no longer blocks every layer at
        # its (x, y), only the layers it actually occupies.
        return in_bounds(nc) and nc not in blocked

    def extra_cost(cell):
        c = weights["history_weight"] * history.get(cell, 0.0)
        prox = proximity.get(cell)
        if prox:
            c += weights["proximity_weight"] * sum(v for k, v in prox.items() if k != current_net)
        # PRESENT congestion, from the shared grid bookkeeping every
        # routing action commits to (see GridState): another net's
        # conductor on this cell, or its via-pad/stub clearance ring
        # covering it. Real PathFinder has both this and `history`; this
        # implementation had only history, and one pass late is not good
        # enough when the consequence is a short.
        #
        # A large COST, not a hard block. Blocking was tried first and is
        # wrong for exactly the reason PathFinder exists: it is an
        # irrevocable first-come-first-served claim, and nets are routed in
        # a fixed order, so an early net's 1.5um met3->met4 pad could wall a
        # later one out of a corridor it had no other way through --
        # measured on the reference design, `net5` and `net7` both failed
        # outright while routing either alone succeeded. As a cost the
        # router detours whenever a detour exists, the crowding is still
        # REPORTED, and `history` pushes the nets apart next pass, but no
        # net is ever made unroutable by another's geometry.
        c += state.penalty(cell, current_net, weights)
        # Crossing over a macro is now legal on any layer that macro leaves
        # empty -- but it is not free. A wire over a device couples into it,
        # and the router has no model of what it is coupling into, so this
        # keeps feed-through as something the search buys deliberately when
        # it genuinely shortens a route, not something it wanders into. Gate
        # ("sensitive") nets pay a multiple, same convention as their via
        # cost.
        if over_cost and (cell[0], cell[1]) in over_macro:
            c += over_cost
        return c

    def neighbors(c):
        ix, iy, l = c
        for dix, diy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nc = (ix + dix, iy + diy, l)
            if passable(nc):
                pref = layer_dirs[l]
                is_h_move = dix != 0
                mult = 1.0 if (is_h_move == (pref == "H")) else weights["wrong_direction_penalty"]
                yield nc, pitch * mult
        # No layer change AT a landing cell. The router used to be free to
        # via immediately on arrival, which put the via on the macro's own
        # port metal -- and on the reference design that landed a met3->met2
        # via directly on top of the via the diff_pair already had there
        # (its drain c_route's viam2m3). The two cuts partially overlapped
        # into an under-width composite: 1 real "Via1 width < 0.26um
        # (via.1a)" plus 10 "abut or partially overlap between subcells".
        # Forbidding the move here forces the first step off a landing to be
        # lateral on the port's own layer, so any via ends up at least one
        # pitch away, clear of the macro and of whatever it already has.
        if (ix, iy) not in weights["no_via_cells"]:
            for dl in (1, -1):
                nc = (ix, iy, l + dl)
                if passable(nc):
                    # Per-PAIR, from the real via_stack footprint -- see
                    # via_footprints(). met3->met4 costs ~3.5x met2->met3 in
                    # sky130 because via3 needs a 0.65um met4 enclosure; a
                    # flat cost cannot express that and made every layer
                    # change look equally (un)attractive.
                    vc = weights["via_cost"][min(l, l + dl)]
                    if current_net in weights["sensitive_nets"]:
                        vc *= weights["sensitive_via_multiplier"]
                    yield nc, vc

    goal_set = set(goals)
    open_heap = [(heuristic(start), 0.0, start)]
    came_from = {start: None}
    best_g = {start: 0.0}
    closed = set()
    while open_heap:
        _, g, cur = heapq.heappop(open_heap)
        if cur in closed:
            continue
        closed.add(cur)
        if cur in goal_set:
            path = [cur]
            n = came_from[cur]
            while n is not None:
                path.append(n)
                n = came_from[n]
            path.reverse()
            return path, g
        for nc, base_cost in neighbors(cur):
            ng = g + base_cost + extra_cost(nc)
            if nc not in best_g or ng < best_g[nc] - 1e-12:
                best_g[nc] = ng
                came_from[nc] = cur
                heapq.heappush(open_heap, (ng + heuristic(nc), ng, nc))
    return None, math.inf


def add_proximity(proximity, cell, net, radius, weight):
    ix, iy, l = cell
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            dist = max(abs(dx), abs(dy))
            if dist > radius:
                continue
            c = (ix + dx, iy + dy, l)
            contribution = weight / (1 + dist)
            proximity.setdefault(c, {})[net] = max(proximity.get(c, {}).get(net, 0.0), contribution)


def path_footprint(path, grid, weights):
    """`(cells, via_metal, halo)` for one routed path.

    Kept as three sets rather than two because they are known to different
    precisions, and the caller prices and reports them differently:

      `cells`     -- the path's own grid cells. EXACT: a wire runs from
                     lattice point to lattice point, so two nets sharing
                     one of these is a real short with no modelling slack.
      `via_metal` -- the pad at each layer change, as a rasterized disc.
                     Conservative: `via_stack(met3, met4)` is 1.5um square
                     and rounds out to a 2-cell radius on a 0.72um pitch,
                     so this over-claims. Still needed -- a via modelled as
                     occupying its single cell is simply wrong at that size
                     -- but an overlap here is an upper bound, not proof.
      `halo`      -- minimum-spacing ring around the pads."""
    cells = set(path)
    via_metal, halo = set(), set()
    for a, b in zip(path, path[1:]):
        if a[2] != b[2]:
            m, h = via_pad_cells(a[0], a[1], min(a[2], b[2]), grid,
                                  weights["via_pads"], weights["wire_widths"],
                                  weights["routing_layers"], weights["pdk"])
            via_metal |= m
            halo |= h
    return cells, via_metal - cells, halo - cells - via_metal


def route_net(net, pin_cells, blocked, grid, layer_dirs, history, proximity, weights,
              state=None):
    """Route one net and COMMIT it to `state` as it goes.

    Multi-pin nets are a chain of A* searches, and each leg is committed
    before the next one runs -- so a net's later legs see its own earlier
    ones as real geometry rather than rediscovering them through the tree
    set alone, and so the state is never stale mid-net."""
    if len(pin_cells) < 2:
        return None
    tree = {pin_cells[0]}
    all_paths = []
    metal, halo = set(), set()
    for target in pin_cells[1:]:
        path, _ = astar(target, tree, blocked, grid, layer_dirs, history, proximity, net,
                        weights, state=state)
        if path is None:
            return None
        all_paths.append(path)
        tree.update(path)
        cells, via_metal, h = path_footprint(path, grid, weights)
        metal |= cells | via_metal
        halo |= h
        if state is not None:
            state.claim(net, cells, (), kind="path")
            state.claim(net, via_metal, h, kind="via")
    for path in all_paths:
        for cell in path:
            add_proximity(proximity, cell, net, weights["proximity_radius"], weights["proximity_weight"])
    return all_paths, metal, halo


# ---------------------------------------------------------------------------
# Negotiated congestion outer loop
# ---------------------------------------------------------------------------

def route_design(nets_grid, blocked, grid, layer_dirs, weights, ripup_iters):
    order = sorted(nets_grid, key=lambda n: (len(nets_grid[n]), n))
    history = {}
    pin_cells = {c for cells in nets_grid.values() for c in cells}
    # Rank each pass by (num_failed, total_overflow) -- fewest failed nets
    # wins first, overflow only breaks ties. Always keep that pass's real
    # net_paths, even if it has some failures: reporting partial credit
    # (the nets that DID route) is honest and useful; silently discarding
    # everything to an empty result just because nothing achieved a
    # perfect pass is not -- a real bug this router's own testing caught
    # (a design that consistently left 2/6 nets unroutable across every
    # ripup pass was reporting "routed 0/6" instead of the true 4/6).
    best_result, best_status, best_rank = {}, None, (math.inf, math.inf, math.inf)
    for it in range(ripup_iters):
        proximity = {}
        net_paths = {}
        failed = []
        # Rebuilt every pass, exactly like `proximity`: the grid state is a
        # record of what the OTHER nets in this same pass have put down,
        # and the pass order is fixed, so carrying it across iterations
        # would freeze in whatever the first pass happened to do -- the
        # opposite of what ripup is for. `history` is the thing that
        # persists, and it is what arbitrates between passes.
        #
        # Seeded, not empty: every net's landing stub is committed for that
        # net before any net routes. Unlike paths and via pads these are
        # known up front -- the stub geometry follows from the pin cell and
        # the port coordinate, both fixed before routing starts.
        state = GridState()
        for stub_net, (m, h) in weights["stub_footprints"]:
            state.claim(stub_net, m, h, kind="stub")
        state.freeze_seed()
        for net in order:
            result = route_net(net, nets_grid[net], blocked, grid, layer_dirs, history,
                               proximity, weights, state=state)
            if result is None:
                failed.append(net)
                continue
            paths, _metal, _halo = result
            net_paths[net] = paths
        # Two kinds of conflict, recorded by GridState as each action was
        # committed, and they are NOT the same thing:
        #   - short:   two nets' conductor on one cell.
        #   - spacing: one net's conductor inside another's clearance ring.
        # Halo-on-halo is not counted -- two clearance rings may overlap
        # freely as long as neither net puts conductor in the other's.
        # Counting them (an earlier version did) reported 80-140 congested
        # cells on a design with no real conflict at all, because the
        # met3->met4 pad alone spans ~25 cells.
        shorts, spacing = state.conflicts()
        overflow_cells = dict(spacing)
        for c, users in shorts.items():
            overflow_cells.setdefault(c, set()).update(users)
        total_overflow = sum(len(u) - 1 for u in overflow_cells.values())
        # A conflict ON a pin cell is not something ripup can resolve: the
        # net has to occupy its own landing point, so no reroute moves it
        # and `history` just piles cost onto a cell nobody can vacate. Call
        # it out rather than letting the loop appear to be making progress
        # -- it is a placement/port-spacing signal, the same one the
        # landing-stub warning raises.
        exact = state.exact_conflicts()
        stuck = sum(1 for c in overflow_cells if c in pin_cells)
        note = f", {stuck} at pin cells (not routable away)" if stuck else ""
        print(f"  ripup iter {it}: {len(overflow_cells)} congested cell(s) "
              f"({len(exact)} path-on-path, {len(shorts) - len(exact)} via/stub-pad, "
              f"{len(spacing)} spacing{note}), "
              f"{len(failed)} failed net(s), total overflow={total_overflow}")
        if overflow_cells:
            pairs = {}
            for c, users in overflow_cells.items():
                pairs[tuple(sorted(users))] = pairs.get(tuple(sorted(users)), 0) + 1
            print("      " + ", ".join(f"{'+'.join(k)}:{v}" for k, v in
                                        sorted(pairs.items(), key=lambda kv: -kv[1])[:6]))
        # Rank on the EXACTLY-modelled conflicts first. A path-on-stub or
        # halo conflict is an upper bound from whole-cell rasterization
        # (see GridState.claim()), so letting it outrank a real path-on-path
        # short would trade a certain defect for a possible one.
        rank = (len(failed), len(exact), total_overflow)
        if rank < best_rank:
            best_rank = rank
            best_result = net_paths
            best_status = {"iterations": it + 1, "overflow_cells": len(overflow_cells),
                            "path_on_path_shorts": len(exact),
                            "conservative_pad_cells": len(shorts) - len(exact),
                            "spacing_cells": len(spacing),
                            "failed_nets": list(failed), "converged": False}
        # Convergence requires zero EXACT conflicts and zero failures. The
        # conservative residue is reported, not gated on: it is an upper
        # bound, and gating on it would fail a design whose real geometry
        # is clean -- which is exactly the case on
        # the reference decomposed netlist, where 6 such cells persist under
        # every penalty/history setting while the emitted polygons have 0
        # cross-net overlaps and Magic DRC passes clean. DRC stays the
        # authority (see ../SKILL.md).
        if not exact and not failed:
            best_status["converged"] = True
            best_status["residual_conservative_cells"] = len(overflow_cells)
            return net_paths, best_status
        for c in overflow_cells:
            history[c] = history.get(c, 0.0) + weights["history_step"]
    print(f"  WARNING: did not converge within {ripup_iters} ripup iterations -- "
          f"reporting best result found ({len(best_result)}/{len(nets_grid)} net(s) routed, "
          f"failed={best_status['failed_nets']})")
    return best_result, best_status


# ---------------------------------------------------------------------------
# Output: segments (JSON) + real GDS
# ---------------------------------------------------------------------------

def simplify_path(path):
    """Collapse consecutive collinear same-layer grid steps into runs, so
    output segments are real wire spans, not one polygon per grid cell."""
    runs = []
    i = 0
    while i < len(path) - 1:
        ix0, iy0, l0 = path[i]
        ix1, iy1, l1 = path[i + 1]
        if l1 != l0:
            runs.append((path[i], path[i + 1], "via"))
            i += 1
            continue
        ddx, ddy = ix1 - ix0, iy1 - iy0
        j = i + 1
        while j < len(path) - 1:
            ixc, iyc, lc = path[j]
            ixn, iyn, ln = path[j + 1]
            if ln != lc or (ixn - ixc, iyn - iyc) != (ddx, ddy):
                break
            j += 1
        runs.append((path[i], path[j], "wire"))
        i = j
    return runs


def wire_rect(x0, y0, x1, y1, width):
    hw = width / 2
    if abs(x1 - x0) >= abs(y1 - y0):
        lo, hi = min(x0, x1), max(x0, x1)
        return [(lo, y0 - hw), (hi, y0 - hw), (hi, y0 + hw), (lo, y0 + hw)]
    lo, hi = min(y0, y1), max(y0, y1)
    return [(x0 - hw, lo), (x0 + hw, lo), (x0 + hw, hi), (x0 - hw, hi)]


def paths_to_segments(paths, grid, routing_layers, wire_widths):
    """Wire and via segments for a routed path.

    Every wire run is drawn from grid point to grid point, padded by half
    the wire width PERPENDICULAR to the run only -- so a run ends exactly
    at its final grid point, not half a width past it. Where two runs meet
    at a bend that leaves a real notch: the horizontal arm stops at the
    corner's x, the vertical arm stops at the corner's y, and they overlap
    over only a half-width square, leaving the outer quadrant of the corner
    empty. Confirmed by Magic on the reference design, not deduced -- 13
    "Metal3 width < 0.3um (met3.1)" errors, whose reported error boxes are
    exactly 0.155 x 0.155um (half of met4's 0.31um wire) at the outside of
    each met4 bend, e.g. net3's arms x[-96.382,-48.862] y[72.714,73.024]
    and x[-49.017,-48.707] y[71.429,72.869] meeting at (-48.862, 72.869).
    On the thinner layers the same notch is 0.105um and Magic reports it
    under the SPACING rule instead (met1.2/met2.2) -- same defect, three
    different rule names, which is why it reads as unrelated errors.

    Fixed by emitting an explicit width x width square at each bend.
    Deliberately not by extending every run half a width at both ends: that
    also extends the two FREE ends of a path, pushing metal 0.155um further
    toward whatever the landing cell was keeping clear of. A corner square
    adds material only where two runs already meet."""
    segments = []
    for path in paths:
        runs = simplify_path(path)
        for (c0, c1, kind) in runs:
            if kind == "wire":
                layer = routing_layers[c0[2]]
                x0, y0 = to_real(c0[0], c0[1], grid)
                x1, y1 = to_real(c1[0], c1[1], grid)
                segments.append({"kind": "wire", "layer": layer,
                                  "points_um": wire_rect(x0, y0, x1, y1, wire_widths[layer])})
            else:
                x, y = to_real(c0[0], c0[1], grid)
                segments.append({"kind": "via", "x_um": x, "y_um": y,
                                  "from_layer": routing_layers[c0[2]], "to_layer": routing_layers[c1[2]]})
        for (a0, a1, ka), (b0, b1, kb) in zip(runs, runs[1:]):
            # Only a wire-to-wire bend on ONE layer needs this. A via
            # junction is already covered: every via_stack footprint in
            # sky130 (0.38um met4->met5, 0.43um met2->met3, 1.50um
            # met3->met4) is wider than the wire that meets it.
            if ka != "wire" or kb != "wire" or a1[2] != b0[2] or a1[:2] != b0[:2]:
                continue
            if (a1[0] - a0[0] != 0) == (b1[0] - b0[0] != 0):
                continue  # straight through, not a bend
            layer = routing_layers[a1[2]]
            hw = wire_widths[layer] / 2
            cx, cy = to_real(a1[0], a1[1], grid)
            segments.append({"kind": "wire", "layer": layer,
                              "points_um": [(cx - hw, cy - hw), (cx + hw, cy - hw),
                                            (cx + hw, cy + hw), (cx - hw, cy + hw)]})
    return segments


NET_LABEL_MAGNIFICATION = 4.0
# Instance names are ANNOTATION, and must not be on a layer the extraction
# tech maps to metal. They used to sit on `met5_label` (GDS 71/5), the same
# layer glayout uses to name real met5 nets -- so Magic's `port makeall`
# promoted every one of them. Five landed on no metal and became spurious
# disconnected top-level pins; the sixth landed on XC0's met5 top plate and
# Magic took it as that net's NAME, renaming `vout` to `XC0` throughout the
# extracted netlist. That is net-identity corruption, not cosmetics.
#
# 236/0 is unmapped in sky130A's Magic tech AND absent from its KLayout
# layer properties, so it is inert for extraction. Confirmed by direct
# test, not assumed: a cell with a met5 plate labelled on 71/5 plus texts
# on 83/44, 200/0 and 236/0 extracted with exactly ONE port -- the 71/5
# one. Kept as a raw (layer, datatype) rather than a glayer name precisely
# because no glayer should map here.
INSTANCE_LABEL_LAYER = (236, 0)
INSTANCE_LABEL_MAGNIFICATION = 4.0


def render_gds(manifest, placement, routes, grid, routing_layers, wire_widths, out_path):
    top_pins = set(manifest.get("top_pins") or [])
    """Real net-name labels, not just instance geometry -- added after the
    real DRC-clean/LVS run against an earlier version of this GDS found
    Magic's `port makeall` promoted ZERO top-level ports (0 vs. the golden
    netlist's 5), because nothing here ever labeled a net. Label each
    ROUTED net once, on the actual conductive layer at the midpoint of one
    of its own wire segments (not a separate label-only layer like
    instance names use) -- glayout's own port-labeling convention
    (`current_mirror.py`'s `add_cm_labels()`) uses this same
    layer-matched `<layer>_label` pattern, not a generic unused layer,
    specifically so Magic's extraction attaches the name to the real
    metal, not a floating annotation.

    Each placed macro ALSO gets its own instance-name label now (at its
    center, on `INSTANCE_LABEL_LAYER` -- an extraction-inert annotation
    layer, NOT `met5_label`, see that constant's own note) -- same idea
    `../../placer/script/render_placement.py`'s own `render()` already uses
    for the bare-placement visualization (same magnification,
    same `agents/scripts/redraw_layout.py`'s `label_device()` lineage),
    reused here rather than invented separately so a `routed.gds` reads
    the same way in KLayout as `placement_visualization.gds` does. This
    was a real, previously-missing piece: before this, `routed.gds` had
    NO instance labels at all (only the net labels above), making it
    impossible to tell which macro was which just by opening the GDS.
    Labels the macro-granularity name from `manifest.json`'s `macros`
    entry (e.g. `diff_pair_XMN1_XMN2`, `current_mirror_leg_XMN5`) -- the
    same granularity this whole router operates at (see the module
    docstring's macro-vs-per-device scope note); it does NOT break a
    multi-device macro like a current_mirror or diff_pair down into a
    separate label per individual transistor (XMN1 vs XMN2), since this
    router never tracks where an individual device sits inside its own
    macro's GDS -- only `generate_primitives.py`'s own per-device port
    extraction (`near_edge_ports()`) has that information, and only for
    the specific ports it extracts, not full per-device placement.

    Supply rails (VDD/VSS-style nets) and single-macro nets (e.g. Vip/Vin)
    are now included too (see main()'s `include_supply=True` and
    `single_pin_nets`) -- rails route like any other multi-macro net once
    a real near-edge bulk/tie port anchors each macro
    (`world_port_map()`); single-macro nets get a `"kind": "label_only"`
    segment instead of a synthesized wire (there's nothing to route
    between macros for a net that only touches one -- see main()'s
    docstring), labeled directly onto the real port coordinate
    generate_primitives.py's near_edge_ports() found, which already sits
    on real metal that macro's own GDS drew.

    Still NOT fully solved: any net with no real near-edge port on ANY of
    its macros (see generate_primitives.py's near_edge_ports() docstring
    for which port families qualify) has nothing this function can anchor
    a label to beyond the pre-existing box-edge approximation -- that net
    may still land off the real device geometry. Verify with the DRC/LVS
    steps below, don't assume every net in `routes` is actually LVS-real
    just because it has a label."""
    gds_by_name = {m["name"]: m.get("gds") for m in manifest["macros"]}
    top = Component(name="routed")
    for name, p in placement["positions"].items():
        gds_path = gds_by_name.get(name)
        if not gds_path or not Path(gds_path).exists():
            continue
        comp = import_gds(gds_path)
        ref = top << comp
        cx, cy = p["x"] + p["w"] / 2, p["y"] + p["h"] / 2
        # `origin=` must be the ref's OWN bbox centre -- see
        # ../../../placer/script/render_placement.py for the full note. In
        # short: move(destination=D) defaults to origin=(0,0), a pure
        # translate putting the macro's glayout ORIGIN at D rather than its
        # centre, which displaces every off-centre macro from the box the
        # annealer reserved for it (measured 6.04um on
        # current_mirror_XMN4_XMN3_XMN5). Here that also puts every macro's
        # real metal somewhere local_to_world() does not predict, so the
        # routes land next to the ports instead of on them -- extraction
        # then sees the macros as unconnected and LVS fails.
        (rbx0, rby0), (rbx1, rby1) = ref.bbox
        ref.move(origin=((float(rbx0) + float(rbx1)) / 2,
                         (float(rby0) + float(rby1)) / 2),
                 destination=(cx, cy))
        if p.get("rotated"):
            ref.rotate(90, center=(cx, cy))
        top.add_label(name, position=(cx, cy), layer=INSTANCE_LABEL_LAYER,
                       magnification=INSTANCE_LABEL_MAGNIFICATION)
    for net, segments in routes.items():
        labeled = False
        for seg in segments:
            if seg["kind"] == "wire":
                top.add_polygon(seg["points_um"], layer=PDK.get_glayer(seg["layer"]))
                if not labeled:
                    xs = [p[0] for p in seg["points_um"]]
                    ys = [p[1] for p in seg["points_um"]]
                    mx, my = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
                    # Only a real top-level PIN goes on a conductive
                    # `<layer>_label`, where Magic's `port makeall` will
                    # promote it. Internal nets get their name on the
                    # extraction-inert annotation layer instead: still
                    # visible in KLayout, but not turned into a port.
                    # Labelling everything conductively made the layout
                    # declare 10 top-level ports against the golden's 5 and
                    # LVS reported "Top level cell failed pin matching" even
                    # once the netlists were otherwise equivalent.
                    if net in top_pins:
                        top.add_label(net, position=(mx, my),
                                      layer=PDK.get_glayer(f"{seg['layer']}_label"),
                                      magnification=NET_LABEL_MAGNIFICATION)
                    else:
                        top.add_label(net, position=(mx, my),
                                      layer=INSTANCE_LABEL_LAYER,
                                      magnification=NET_LABEL_MAGNIFICATION)
                    labeled = True
            elif seg["kind"] == "via":
                vs = via_stack(PDK, seg["from_layer"], seg["to_layer"], centered=True)
                vref = top << vs
                vref.move(destination=(seg["x_um"], seg["y_um"]))
            elif seg["kind"] == "label_only" and not labeled:
                # No wire to draw -- this net only ever touches one macro,
                # so the real port coordinate generate_primitives.py found
                # (see world_port_map()) already sits on that macro's OWN
                # real metal; just label it there.
                top.add_label(net, position=(seg["x_um"], seg["y_um"]),
                               layer=(PDK.get_glayer(f"{seg['layer']}_label")
                                      if net in top_pins else INSTANCE_LABEL_LAYER),
                               magnification=NET_LABEL_MAGNIFICATION)
                labeled = True
    top.write_gds(str(out_path))
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def segment_span(seg):
    """`(axis, lo, hi, cross_lo, cross_hi)` for a wire segment's rectangle:
    which way it runs, and its extent along that axis."""
    xs = [p[0] for p in seg["points_um"]]
    ys = [p[1] for p in seg["points_um"]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if (x1 - x0) >= (y1 - y0):
        return "x", x0, x1, y0, y1
    return "y", y0, y1, x0, x1


def feedthrough_um(routes, placement):
    """`(over_macro_um, total_um)` -- how much routed wire runs across a
    macro's own footprint, on a layer that macro leaves empty.

    Worth measuring rather than assuming: this is the thing the per-layer
    obstacle map (build_layer_obstacles()) exists to permit, and it is the
    single number that says whether a given design actually benefited. A
    placement with no usable open layer over its macros will legitimately
    report ~0 here, and that is information, not a bug.

    Measured geometrically from the emitted rectangles rather than from
    grid cells, so it stays honest about what was really drawn."""
    boxes = [(p["x"], p["y"], p["x"] + p["w"], p["y"] + p["h"])
             for p in placement["positions"].values()]
    over = total = 0.0
    for segs in routes.values():
        for seg in segs:
            if seg["kind"] != "wire":
                continue
            axis, lo, hi, clo, chi = segment_span(seg)
            length = hi - lo
            total += length
            # Union of the sub-intervals of this run that fall inside any
            # macro box -- a run can cross several, and double counting
            # would report more feed-through than there is wire.
            spans = []
            for bx0, by0, bx1, by1 in boxes:
                if axis == "x":
                    if chi <= by0 or clo >= by1:
                        continue
                    a, b = max(lo, bx0), min(hi, bx1)
                else:
                    if chi <= bx0 or clo >= bx1:
                        continue
                    a, b = max(lo, by0), min(hi, by1)
                if b > a:
                    spans.append((a, b))
            spans.sort()
            cur = None
            for a, b in spans:
                if cur is None or a > cur[1]:
                    if cur:
                        over += cur[1] - cur[0]
                    cur = [a, b]
                else:
                    cur[1] = max(cur[1], b)
            if cur:
                over += cur[1] - cur[0]
    return over, total


def write_routing_summary(path, design, design_dir, routes, placement, grid, routing_layers,
                           layer_dirs, wire_widths, via_pads, blocked, obstacle_stats,
                           status, sensitive_nets, single_pin_nets, net_macros,
                           landing_stats, args, elapsed_s):
    """Human-readable routing-performance report, the router's counterpart
    to ../../../placer/script/anneal_placement.py's `placing_summary.txt`.

    `routes.json` already carries the geometry, but it is a machine
    artifact -- thousands of coordinates, no aggregation, nothing ranked.
    This is the file to open to answer "is this routing any good, and if
    not where": gates first, then totals, then the layers, nets and
    transitions actually responsible.

    Written every run, PASS or FAIL. A summary that only appears on success
    is the one you cannot diff against the run that regressed."""
    L = []
    A = L.append
    per_net_wire, per_net_via, per_net_seg = {}, {}, {}
    by_layer, by_via = {}, {}
    for net, segs in routes.items():
        for seg in segs:
            if seg["kind"] == "wire":
                _, lo, hi, _, _ = segment_span(seg)
                per_net_wire[net] = per_net_wire.get(net, 0.0) + (hi - lo)
                per_net_seg[net] = per_net_seg.get(net, 0) + 1
                by_layer[seg["layer"]] = by_layer.get(seg["layer"], 0.0) + (hi - lo)
            elif seg["kind"] == "via":
                per_net_via[net] = per_net_via.get(net, 0) + 1
                key = tuple(sorted((seg["from_layer"], seg["to_layer"])))
                by_via[key] = by_via.get(key, 0) + 1
    total_wire = sum(per_net_wire.values())
    total_vias = sum(per_net_via.values())
    over_um, _ = feedthrough_um(routes, placement)
    n_cells = grid["n_cols"] * grid["n_rows"]
    routed = [n for n in routes if per_net_wire.get(n)]
    ok = status.get("converged") and not status.get("failed_nets")

    A("=" * 72)
    A(f"ROUTING SUMMARY -- {design or '(unnamed design)'}")
    A("=" * 72)
    A(f"generated   : {datetime.datetime.now().isoformat(timespec='seconds')}")
    A(f"design dir  : {design_dir}")
    A(f"runtime     : {elapsed_s:.1f}s")
    A(f"overall     : {'PASS' if ok else 'FAIL'}")
    A("")
    A("-- quality gates ---------------------------------------------------")
    A(f"  nets routed        : {len(routed)}/{len(net_macros)} multi-macro"
      f"   (+{len(single_pin_nets)} single-macro pin(s) labeled)")
    A(f"  path-on-path shorts: {status.get('path_on_path_shorts', 0)}   (must be 0 -- "
      f"exactly modelled, no rasterization slack)")
    A(f"  congestion         : {'PASS (converged)' if ok else 'FAIL'}"
      f"   after {status.get('iterations', '?')} ripup pass(es)")
    A(f"  failed nets        : {', '.join(status.get('failed_nets') or []) or 'none'}")
    cons = status.get("residual_conservative_cells", status.get("overflow_cells", 0))
    A(f"  conservative cells : {cons}   (via/stub pad footprints rasterized to whole")
    A(f"                       grid cells -- an UPPER bound, not a violation; DRC is")
    A(f"                       the authority, see ../SKILL.md)")
    A("")
    A("-- wire length -----------------------------------------------------")
    A(f"  total wirelength   : {total_wire:.2f} um")
    A(f"  total vias         : {total_vias}"
      f"   ({sum(v for n, v in per_net_via.items() if n in sensitive_nets)} on "
      f"gate-sensitive nets)")
    if routed:
        A(f"  mean / max per net : {total_wire / len(routed):.2f} um"
          f"  /  {max(per_net_wire.values()):.2f} um")
    A(f"  feed-through       : {over_um:.2f} um ({100.0 * over_um / total_wire:.1f}%) runs"
      f" over a macro on a layer it leaves empty")
    A("")
    A("-- by layer --------------------------------------------------------")
    A(f"  {'layer':6s} {'dir':>4s} {'width':>7s} {'wire':>11s} {'share':>7s} {'blocked':>9s}")
    for li, g in enumerate(routing_layers):
        used = sum(1 for c in blocked if c[2] == li)
        w = by_layer.get(g, 0.0)
        A(f"  {g:6s} {layer_dirs[li]:>4s} {wire_widths[g]:7.3f} {w:9.2f}um "
          f"{100.0 * w / total_wire if total_wire else 0:6.1f}% "
          f"{100.0 * used / n_cells:8.1f}%")
    A("")
    A("-- vias by transition (pad = real via_stack footprint) -------------")
    for i in range(len(routing_layers) - 1):
        key = tuple(sorted((routing_layers[i], routing_layers[i + 1])))
        A(f"  {routing_layers[i]:5s} <-> {routing_layers[i + 1]:5s}  pad="
          f"{via_pads[i]['overall']:5.2f}um  count={by_via.get(key, 0)}")
    for key, n in sorted(by_via.items()):
        if key not in {tuple(sorted((routing_layers[i], routing_layers[i + 1])))
                        for i in range(len(routing_layers) - 1)}:
            A(f"  {key[0]:5s} <-> {key[1]:5s}  (port landing)   count={n}")
    A("")
    A("-- worst nets by wirelength ----------------------------------------")
    for net, w in sorted(per_net_wire.items(), key=lambda kv: -kv[1])[:10]:
        A(f"  {net:12s} {w:9.2f} um ({100.0 * w / total_wire:5.1f}%)  "
          f"{len(net_macros.get(net, [])):2d} macro(s)  {per_net_seg.get(net, 0):3d} seg  "
          f"{per_net_via.get(net, 0):2d} via"
          f"{'   [gate-sensitive]' if net in sensitive_nets else ''}")
    A("")
    A("-- macro obstacle map (per layer) ----------------------------------")
    A("   full=whole box blocked   geom=only real metal   free=open")
    for name in sorted(obstacle_stats):
        A(f"  {name:30s} " + "  ".join(
            f"{g}:{obstacle_stats[name].get(g, '-')}" for g in routing_layers))
    A("")
    A("-- landing points --------------------------------------------------")
    A(f"  real glayout ports : {landing_stats['real_port']}")
    A(f"  box-edge fallback  : {landing_stats['box_edge_fallback']}"
      f"   (macro had no near-edge port for that net -- see")
    A("                       generate_primitives.py's near_edge_ports())")
    A("")
    A("-- routing grid ----------------------------------------------------")
    A(f"  {grid['n_cols']} x {grid['n_rows']} cells x {len(routing_layers)} layers"
      f"   pitch={grid['pitch']:.4f}um (coarsest legal layer pitch "
      f"{grid.get('base_pitch', float('nan')):.4f} x{args.track_multiplier})")
    A(f"  origin=({grid['x0']:.3f}, {grid['y0']:.3f})")
    A("")
    A("-- reproducibility -------------------------------------------------")
    A(f"  layers={','.join(routing_layers)}  track_multiplier={args.track_multiplier}"
      f"  ripup_iters={args.ripup_iters}")
    A(f"  via_cost={args.via_cost} (x real pad um)  sensitive_via_multiplier="
      f"{args.sensitive_via_multiplier}")
    A(f"  wrong_direction_penalty={args.wrong_direction_penalty}"
      f"  over_macro_cost={args.over_macro_cost}"
      f"  sensitive_over_macro_multiplier={args.sensitive_over_macro_multiplier}")
    A(f"  shared_cell_penalty={args.shared_cell_penalty}"
      f"  via_pad_penalty={args.via_pad_penalty}  feedthrough={args.feedthrough}")
    A(f"  proximity_radius={args.proximity_radius}"
      f"  proximity_weight={args.proximity_weight}  history_step={args.history_step}")
    A(f"  sensitive nets: {', '.join(sorted(sensitive_nets)) or 'none'}")
    A("=" * 72)
    Path(path).write_text("\n".join(L) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("design_dir")
    parser.add_argument("--layers", default="met2,met3,met4,met5",
                         help="routing layer list, alternating H/V by index "
                              "(met2=H,met3=V,met4=H,met5=V). met5 is included now that macro "
                              "obstacles are per-layer -- see build_layer_obstacles(); before "
                              "that a 4th layer bought nothing, since every layer had identical "
                              "obstacles and so identical path lengths")
    parser.add_argument("--track-multiplier", type=float, default=1.0,
                         help="routing grid pitch = base_track_pitch() * this, where the base is "
                              "the smallest pitch legal on the COARSEST routing layer (met4/met5's "
                              "0.3/0.4 rules => 0.72um in sky130). The old default of 2.0 sat on "
                              "top of a base taken from the FINEST layer, which is not a legal "
                              "met4 track at all -- see base_track_pitch(). 1.0 keeps the absolute "
                              "pitch close to what real testing already validated (0.72 vs 0.56um) "
                              "while making every layer's tracks legal; raise it only if the grid "
                              "is too large to search, and expect narrow channels to close up")
    parser.add_argument("--via-cost", type=float, default=10.0,
                         help="cost SCALE, multiplied by each layer pair's real via_stack "
                              "footprint in um (see via_footprints()) -- not a flat per-via cost. "
                              "In sky130 that yields met2->met3 4.3, met3->met4 15.0, met4->met5 "
                              "3.8, which is the real area asymmetry via3's 0.65um met4 enclosure "
                              "creates")
    parser.add_argument("--over-macro-cost", type=float, default=0.4,
                         help="extra cost per grid step routed OVER a macro, on a layer that "
                              "macro leaves empty. Feed-through is what the per-layer obstacle "
                              "map unlocks, but a wire over a device couples into it and the "
                              "router has no model of that -- this keeps it a deliberate "
                              "shortcut rather than the default path. 0 disables")
    parser.add_argument("--sensitive-over-macro-multiplier", type=float, default=3.0,
                         help="--over-macro-cost multiplier for gate-connected nets, same "
                              "convention as --sensitive-via-multiplier")
    parser.add_argument("--no-feedthrough", dest="feedthrough", action="store_false", default=True,
                         help="block every macro's whole box on EVERY routing layer, the "
                              "pre-per-layer behaviour. Kept as a switch because it is the "
                              "conservative choice, not because it is wrong -- and because it is "
                              "the only way to measure what feed-through actually buys on a given "
                              "design (route both ways, compare total_wire)")
    parser.add_argument("--shared-cell-penalty", type=float, default=400.0,
                         help="present-congestion cost: entering a cell another net's path "
                              "already occupies in THIS pass. Sharing a cell is a real short, so "
                              "this is set far above any plausible detour -- it is a penalty "
                              "rather than a block only so a net is never made unroutable, and so "
                              "the overflow is still REPORTED instead of silently failing")
    parser.add_argument("--via-pad-penalty", type=float, default=40.0,
                         help="cost for entering a cell inside ANOTHER net's via pad (see "
                              "via_pad_cells()). High enough that the router detours whenever a "
                              "detour exists, but a cost rather than a hard block -- a block "
                              "makes later-routed nets fail outright, see astar()'s note")
    parser.add_argument("--sensitive-via-multiplier", type=float, default=3.0)
    parser.add_argument("--wrong-direction-penalty", type=float, default=1.5)
    parser.add_argument("--proximity-radius", type=int, default=2)
    parser.add_argument("--proximity-weight", type=float, default=0.5)
    parser.add_argument("--history-step", type=float, default=1.0)
    parser.add_argument("--ripup-iters", type=int, default=5)
    parser.add_argument("--out", default=None)
    parser.add_argument("--gds", default=None)
    parser.add_argument("--no-gds", dest="do_gds", action="store_false", default=True)
    parser.add_argument("--summary-out", default=None,
                         help="routing-performance report (default <design_dir>/"
                              "routing_summary.txt). Written every run, PASS or FAIL -- a "
                              "summary that only appears on success is the one you cannot diff "
                              "against the run that regressed")
    args = parser.parse_args()
    t_start = time.time()

    design_dir = Path(args.design_dir).resolve()
    manifest, placement = load_design(design_dir)
    routing_layers = [g.strip() for g in args.layers.split(",") if g.strip()]
    # Fail here, legibly, rather than deep inside glayout -- and note this
    # guards the DEFAULT too, on a PDK with a shallower stack.
    validate_routing_layers(routing_layers, PDK)
    layer_dirs = ["H" if i % 2 == 0 else "V" for i in range(len(routing_layers))]

    # include_supply=True: VDD/VSS-style rail nets are no longer excluded
    # -- see ../SKILL.md's Honest scope update. This alone wouldn't be
    # enough (a device's raw source/drain pin is almost always too deep
    # inside its macro to trust -- see generate_primitives.py's
    # near_edge_ports() docstring); world_port_map() below is what
    # actually gives most rail macros a real, near-edge anchor (their
    # substrate/well tie ring), not this flag by itself.
    net_macros, net_kinds = build_nets(manifest, placement, include_supply=True)
    sensitive_nets = {n for n, kinds in net_kinds.items() if "gate" in kinds}
    world_ports = world_port_map(manifest, placement)

    # Single-macro nets (e.g. a differential pair's own input pins) never
    # appear in net_macros (build_nets() only keeps nets touching >=2
    # macros -- there's nothing to ROUTE between macros for these), but if
    # generate_primitives.py found a real near-edge port for one, it can
    # still be exposed as a real top-level pin: just a label on that
    # macro's own already-existing metal, no synthesized wire needed. See
    # ../SKILL.md's Honest scope for what this does NOT fix (any net with
    # no real port at all still has no representation in the GDS).
    grid = compute_grid(placement, PDK, routing_layers, args.track_multiplier)
    print(f"  grid: {grid['n_cols']}x{grid['n_rows']} cells x {len(routing_layers)} layers, "
          f"pitch={grid['pitch']:.4f}um (coarsest legal layer pitch "
          f"{grid['base_pitch']:.4f}um x{args.track_multiplier})")
    # Real keepout beyond each macro's own box -- see build_layer_obstacles()'s
    # docstring for the real LVS short this margin fixes. Sourced from
    # manifest.json's own min_metal_spacing_um (queried from the PDK by
    # ../placer/script/generate_primitives.py, not guessed here).
    min_metal_spacing = manifest.get("min_metal_spacing_um", 0.0)
    keepout_margin = min_metal_spacing * KEEPOUT_MARGIN_MULT
    macro_keepouts = {m['name']: m.get('keepout_um') for m in manifest['macros']}

    def macro_margin(name):
        """The keepout actually applied to ONE macro -- the global margin,
        or that macro's own larger `keepout_um` if it declared one. Every
        consumer must agree on this number: build_layer_obstacles() blocks with
        it, and pin_point()/corridor_to_edge() must place their landing
        points OUTSIDE the same ring. Using the global value for the
        landing points while blocking with a bigger one leaves the pin
        stranded inside the blocked region -- measured, not theoretical:
        giving XC0 its 1.2um cap keepout while still siting its pins at
        0.14um made `net8` and `vout` unroutable (2 failed nets)."""
        return max(keepout_margin, float(macro_keepouts.get(name) or 0.0))

    # Computed here (moved up from just before paths_to_segments()) so the
    # pin-selection loop below can use it for stagger_pin_point()'s own
    # edge-collision spacing, not just final wire rendering. See that
    # function's docstring for why width-based spacing matters there too.
    wire_widths = layer_wire_widths(PDK, routing_layers, grid["pitch"])
    print("  wire widths: " + "  ".join(f"{g}={w:.3f}um" for g, w in wire_widths.items()))

    blocked, over_macro, obstacle_stats = build_layer_obstacles(
        manifest, placement, grid, routing_layers, PDK, wire_widths,
        margin=keepout_margin, keepouts=macro_keepouts, feedthrough=args.feedthrough)
    n_cells = grid["n_cols"] * grid["n_rows"]
    print("  per-layer obstacles (macro x layer -- 'full'=whole box blocked, "
          "'geom'=only real metal, 'free'=open):")
    for name in sorted(obstacle_stats):
        print(f"    {name:28s} " + "  ".join(
            f"{g}:{obstacle_stats[name].get(g, '-')}" for g in routing_layers))
    for li, g in enumerate(routing_layers):
        used = sum(1 for c in blocked if c[2] == li)
        print(f"    {g}: {used}/{n_cells} cells blocked ({100.0 * used / n_cells:.1f}%)")

    via_costs_um = via_footprints(PDK, routing_layers)
    via_costs = {i: args.via_cost * e["overall"] for i, e in via_costs_um.items()}
    print("  via cost (scale x real via_stack footprint): " + "  ".join(
        f"{routing_layers[i]}->{routing_layers[i + 1]} {via_costs_um[i]['overall']:.2f}um"
        f"(pads {via_costs_um[i][i]:.2f}/{via_costs_um[i][i + 1]:.2f}) -> {via_costs[i]:.1f}"
        for i in sorted(via_costs)))

    # A single-macro net is only "nothing to route" when it also touches a
    # single TERMINAL on that macro. If it spans several, those are separate
    # pieces of metal and the net still has to be wired up INSIDE the macro's
    # own footprint -- labelling one of them leaves the rest floating.
    # Measured on the reference design: VSS touches only
    # current_mirror_XMN4_XMN3_XMN5, but across 4 terminals (that mirror's
    # three per-branch source stubs plus its tap ring -- current_mirror.py
    # deliberately gives each branch its own source net rather than pre-tying
    # them to the rail). Label-only left 2 of them unconnected, which is
    # exactly 2 of the nets LVS could not match.
    single_pin_nets = {}
    for net, macros in world_ports.items():
        if len(macros) != 1 or net in net_macros:
            continue
        m = next(iter(macros))
        if len(group_by_pin(filter_landable(world_ports[net][m], PDK,
                                            routing_layers, wire_widths))) > 1:
            net_macros[net] = [m]          # route it: several terminals to join
            net_kinds.setdefault(net, set())
        else:
            single_pin_nets[net] = m

    print(f"=== Routing {len(net_macros)} net(s) ({len(sensitive_nets)} flagged sensitive: "
          f"gate-connected) + labeling {len(single_pin_nets)} single-macro pin(s) over "
          f"{len(placement['positions'])} placed macro(s) ===")


    nets_grid = {}
    extra_corridors = []
    used_edge_offsets = {}
    # A real port below routing_layers[0] (e.g. build_resistor()'s met1
    # p_top_met_*/n_top_met_* terminals -- routing defaults to met2+, kept
    # clear of glayout's own device-internal met1 wiring) still needs an
    # actual via down to it, not just a wire floating over its (x,y) on
    # met2. `filter_landable()` already confirmed the real metal there is
    # wide enough for this exact via (`min_via_footprint_um()`'s
    # below-routing_layers[0] branch); this list records the via itself
    # so it can be appended to `routes[net]` after path routing below --
    # real, confirmed gap this closes: before this existed, R0 on
    # the reference design had no port data at all (empty manifest
    # "ports"), so routing fell back entirely to pin_point()'s box-edge
    # approximation -- which picks whichever side faces the net's other
    # macro (left/right here) instead of the resistor's actual fixed
    # top/bottom terminals, a real visible placement/routing mismatch a
    # user directly spotted in a rendered GDS, not a hypothetical.
    off_grid_layer_vias = []
    # The grid is a real, but coarse (pitch=0.56um typically), quantization
    # of continuous layout space -- to_grid()/to_grid_outward() pick the
    # NEAREST (or nearest-safe) grid cell to a pin's exact target
    # coordinate, not that exact coordinate itself. Left alone, the routed
    # wire stops at that grid cell and nothing bridges the leftover gap
    # (up to half a grid pitch for a real port, up to a full pitch beyond
    # the keepout margin for a pin_point() fallback) to the ACTUAL target
    # -- a real, visible "trace stops short of its destination" gap,
    # confirmed by direct inspection after the off-grid-layer-via fix
    # above (R0's met2 wire ended ~0.11um short of its own new via's real
    # position) and reported directly by a user looking at a rendered
    # GDS. `landing_stubs` records the exact target for every pin cell so
    # an explicit short final wire segment can bridge grid-cell-to-exact-
    # target after path routing below (see that code for how it's drawn).
    landing_stubs = []

    # Pre-seed used_edge_offsets with every REAL port landing point this
    # design will actually choose, before any pin_point()/stagger_pin_point()
    # fallback runs -- see stagger_pin_point()'s own docstring ("Second real
    # bug") for the actual short this closes: without this, a fallback net
    # processed earlier in net_macros' iteration order has no way to know a
    # later net's real port already claims that spot on the same macro.
    # Mirrors the exact candidate-selection logic in the main loop below
    # (same `filter_landable`/nearest-to-other-macros-centroid pick) and the
    # single_pin_nets label choice (`candidates[0]`) later in main() -- kept
    # in sync with both, not re-derived independently.
    for net, macros in net_macros.items():
        for m in macros:
            others = [o for o in macros if o != m]
            candidates = filter_landable(world_ports.get(net, {}).get(m) or [], PDK, routing_layers, wire_widths)
            if candidates and others:
                ocx = sum(macro_center(o, placement)[0] for o in others) / len(others)
                ocy = sum(macro_center(o, placement)[1] for o in others) / len(others)
                for _pin, group in group_by_pin(candidates).items():
                    wx, wy = min(group, key=lambda c: (c[0] - ocx) ** 2 + (c[1] - ocy) ** 2)[:2]
                    used_edge_offsets.setdefault(m, []).append((wx, wy))
    for net, m in single_pin_nets.items():
        candidates = world_ports.get(net, {}).get(m) or []
        if candidates:
            wx, wy, _layer_name, _width, _pin = candidates[0]
            used_edge_offsets.setdefault(m, []).append((wx, wy))

    landing_stats = {"real_port": 0, "box_edge_fallback": 0}
    for net, macros in net_macros.items():
        cells = []
        for m in macros:
            others = [o for o in macros if o != m]
            candidates = filter_landable(world_ports.get(net, {}).get(m) or [], PDK, routing_layers, wire_widths)
            if candidates:
                # Centroid of the OTHER macros on this net, used to pick the
                # facing terminal. A net confined to ONE macro (several of
                # its terminals needing joining, see single_pin_nets) has no
                # others -- fall back to the macro's own centre so every
                # terminal is still scored consistently.
                ocx, ocy = macro_center(m, placement)
                if others:
                    ocx = sum(macro_center(o, placement)[0] for o in others) / len(others)
                    ocy = sum(macro_center(o, placement)[1] for o in others) / len(others)
                # One landing per (net, macro, TERMINAL), not per macro. Two
                # ports of one net on one macro are the same metal only when
                # they are the same terminal (a tap ring's compass quad);
                # across terminals they are physically separate and each
                # needs its own connection. Measured on the reference design:
                # landing once per macro left XMP4's drain floating (it is
                # diode-connected, so gate and drain are one netlist node but
                # two pieces of metal) and left both current mirrors' source
                # stubs floating -- 4 of the 7 nets LVS could not match.
                for _pin, group in group_by_pin(candidates).items():
                    wx, wy, layer_name, _width, _p = min(
                        group, key=lambda c: (c[0] - ocx) ** 2 + (c[1] - ocy) ** 2)
                    pin_layer = routing_layers.index(layer_name) if layer_name in routing_layers else 0
                    ix, iy = to_grid(wx, wy, grid)
                    extra_corridors.extend(corridor_to_edge(ix, iy, m, placement, grid, margin=macro_margin(m)))
                    # `via_um`: the footprint of the via that will be drawn
                    # AT this landing point, so the stub can be made wide
                    # enough to cover it (see the stub-emission loop for the
                    # real DRC failure this prevents). Only set when a via is
                    # actually drawn here -- port layer off the routing grid.
                    stub = {"net": net, "ix": ix, "iy": iy, "x_um": wx, "y_um": wy,
                            "layer": routing_layers[pin_layer]}
                    if layer_name not in routing_layers:
                        off_grid_layer_vias.append({"net": net, "x_um": wx, "y_um": wy,
                                                     "from_layer": layer_name,
                                                     "to_layer": routing_layers[0]})
                        stub["via_um"] = min_via_footprint_um(PDK, routing_layers, layer_name)
                    landing_stubs.append(stub)
                    landing_stats["real_port"] += 1
                    cells.append((ix, iy, pin_layer))
            else:
                # step = width + min_metal_spacing, not just width: two
                # stub rects need their CENTERS this far apart so their
                # EDGES clear the real minimum spacing -- centers exactly
                # `width` apart would leave 0 edge-to-edge gap (real bug
                # found this way, see stagger_pin_point()'s own docstring).
                step = wire_widths[routing_layers[0]] + min_metal_spacing
                px, py, axis, sign = stagger_pin_point(
                    m, others, placement, macro_margin(m), used_edge_offsets, step)
                pin_layer = 0  # box-edge fallback always lands on the first routing layer
                ix, iy = to_grid_outward(px, py, axis, sign, grid)
                landing_stubs.append({"net": net, "ix": ix, "iy": iy, "x_um": px, "y_um": py,
                                       "layer": routing_layers[pin_layer],
                                       "clamp_axis": axis, "clamp_sign": sign})
                landing_stats["box_edge_fallback"] += 1
                cells.append((ix, iy, pin_layer))
        nets_grid[net] = cells
    unblock_pins(blocked, nets_grid, len(routing_layers), extra_corridors=extra_corridors)

    # Each net's own landing stubs, claimed before routing -- see
    # landing_stub_cells() for the real cross-net short this prevents.
    # A cell wanted by two DIFFERENT nets' stubs is reported rather than
    # silently resolved: it means two ports of different nets are within a
    # stub's reach of each other, which routing cannot fix.
    stub_footprints = []
    stub_owner = {}
    stub_conflicts = set()
    for stub in landing_stubs:
        m, h = landing_stub_cells(stub, grid, wire_widths, routing_layers, PDK)
        stub_footprints.append((stub["net"], (m, h)))
        for c in m:
            owner = stub_owner.setdefault(c, stub["net"])
            if owner != stub["net"]:
                stub_conflicts.add(tuple(sorted((owner, stub["net"]))))
    if stub_conflicts:
        print("  WARNING: landing stubs of different nets claim the same cell(s): "
              + ", ".join(f"{a}+{b}" for a, b in sorted(stub_conflicts))
              + " -- these ports are too close to separate by routing; check the "
                "placement or the macro's own port spacing")

    weights = {
        # (ix, iy) of every net's landing cells -- see astar()'s via block.
        "no_via_cells": {(c[0], c[1]) for cells in nets_grid.values() for c in cells},
        "via_cost": via_costs, "sensitive_via_multiplier": args.sensitive_via_multiplier,
        "wrong_direction_penalty": args.wrong_direction_penalty,
        "proximity_radius": args.proximity_radius, "proximity_weight": args.proximity_weight,
        "history_step": args.history_step, "history_weight": 1.0, "sensitive_nets": sensitive_nets,
        "over_macro": over_macro, "over_macro_cost": args.over_macro_cost,
        "sensitive_over_macro_multiplier": args.sensitive_over_macro_multiplier,
        "via_pad_penalty": args.via_pad_penalty, "stub_footprints": stub_footprints,
        "shared_cell_penalty": args.shared_cell_penalty,
        # For via_pad_cells(), called from path_via_cells() after each net.
        "wire_widths": wire_widths, "routing_layers": routing_layers, "pdk": PDK,
        "via_pads": via_costs_um,
    }
    net_paths, status = route_design(nets_grid, blocked, grid, layer_dirs, weights, args.ripup_iters)

    # wire_widths (moved earlier, see above) already has WIRE_WIDTH_MARGIN
    # applied -- real DRC testing on this router's own output found width
    # violations when wires were drawn at EXACTLY the PDK's bare
    # min_width (floating-point/GDS-quantization round-trip can shave a
    # hair off, tripping Magic's strict "< min_width" check); confirmed by
    # re-running DRC after that fix, not just reasoned about.
    routes = {net: paths_to_segments(paths, grid, routing_layers, wire_widths)
              for net, paths in net_paths.items()}
    for v in off_grid_layer_vias:
        if v["net"] in routes:  # net actually routed (not a failed net) -- see route_design()
            routes[v["net"]].append({"kind": "via", "x_um": v["x_um"], "y_um": v["y_um"],
                                      "from_layer": v["from_layer"], "to_layer": v["to_layer"]})
    # Bridge each pin's grid-quantization gap with a short, real final wire
    # pad from the grid-snapped cell actually used for routing/obstacle
    # purposes to the EXACT target coordinate computed above -- see
    # landing_stubs' own docstring (in the pin-cell-selection loop) for the
    # real, confirmed gap this closes.
    #
    # **Two real bugs found and fixed here, not hypothetical, both via
    # actual Magic DRC on the reference design**: (1) an early version
    # called `wire_rect(gx, gy, tx, ty, width)` directly on what's
    # generally a DIAGONAL hop (differs in both x and y) -- `wire_rect()`
    # only knows how to draw a single axis-aligned rect (every OTHER
    # caller only ever gives it a same-layer straight run from
    # `simplify_path()`'s collinear-only grouping, where x0==x1 or
    # y0==y1 always holds), so its vertical branch silently drops the
    # off-axis `x1`, its horizontal branch drops `y1` -- produced a
    # `met1.1`/`met2.1` "Metal width < 0.14um" violation nowhere near the
    # intended target. (2) fixing that by splitting the hop into two
    # Manhattan legs (`wire_rect()` per leg, full `width` padding on
    # each) was WORSE, not better: whenever one leg's own length is
    # shorter than `width`, that leg's perpendicular width-padding sticks
    # out past the OTHER geometry it's supposed to join by only that
    # leg's own (short) length -- a real, genuinely-thin protruding nub,
    # not a measurement artifact (confirmed: a 0.033um leg on a 0.21um-
    # wide wire left a real 0.033um-wide sliver hanging off the main
    # wire's corner). Fixed by drawing ONE rectangle instead: the
    # bounding box of the grid point and the exact target, padded by
    # `width/2` on every side. This is guaranteed >= `width` in BOTH
    # dimensions no matter how small the gap is (so it can never itself
    # be the thin part), and its `width/2` padding around the grid point
    # exactly matches the main routed wire's own half-width reach there,
    # so it always overlaps that wire -- confirmed by re-running DRC
    # after this fix -- ALMOST: it reintroduced the very violation
    # `to_grid_outward()` exists to prevent. **Third real bug found and
    # fixed here, not hypothetical**: for a `pin_point()` fallback, `ty`
    # (or `tx`) IS already the exact safe keepout-margin boundary --
    # padding `width/2` PAST it on the toward-macro side (the same
    # direction `to_grid_outward()` deliberately rounds away from) pushes
    # the stub back into the keepout zone that fix carved out, close
    # enough to trigger a real `met1.2` spacing violation again (found on
    # all 3 of `net3`/`net4`/`net5`'s `diff_pair_XMN1_XMN2` fallback
    # connections). Real ports have no such constraint (there's no
    # keepout concern at the exact real metal they're landing on), so
    # this only applies to the `pin_point()` branch: `clamp_axis`/
    # `clamp_sign` (set only there, from `pin_point()`'s own return) pin
    # the critical-axis bound on the toward-macro side to the target
    # exactly, no further. **Fourth real bug, found immediately after
    # fixing the third**: clamping that ONE bound without touching the
    # other shrinks the span below `width` whenever the natural padded
    # span was close to `width` to begin with -- recreating the very
    # thin-nub problem the padding exists to prevent, just on the
    # opposite (safe) side this time (found on VSS's
    # `current_mirror_XMN4_XMN3` fallback connection: clamped span came
    # out 0.1055um, under the 0.14um real minimum). Fixed by extending
    # the SAFE bound to compensate whenever a clamp fires, so the span is
    # always >= `width` regardless of how close the raw gap was.
    # **Confirmed by re-running DRC after this fix**: 0 violations,
    # reproduced across repeated runs.
    emitted_stubs = []
    for stub in landing_stubs:
        if stub["net"] not in routes:  # net actually routed (not a failed net)
            continue
        gx, gy = to_real(stub["ix"], stub["iy"], grid)
        tx, ty = stub["x_um"], stub["y_um"]
        width = wire_widths[stub["layer"]]
        hw = width / 2
        x0, x1 = min(gx, tx) - hw, max(gx, tx) + hw
        y0, y1 = min(gy, ty) - hw, max(gy, ty) + hw
        # Where the track's own wire already covers the port in an axis,
        # align the stub to that wire exactly instead of spanning
        # port-to-track. Spanning makes the stub slightly TALLER than the
        # wire it abuts, and the step between them is a notch: measured on
        # the reference design, VDD's stub ran y -41.066..-40.815 against
        # the wire's -41.025..-40.815, a 0.041um step that Magic flagged as
        # 6 met2.2 + 3 met1.2 slivers. `hw` is the half-width of that same
        # wire, so `|t - g| <= hw` is exactly the condition "the wire
        # already reaches the port" -- aligning then loses no coverage.
        if abs(tx - gx) <= hw:
            x0, x1 = gx - hw, gx + hw
        if abs(ty - gy) <= hw:
            y0, y1 = gy - hw, gy + hw
        axis, sign = stub.get("clamp_axis"), stub.get("clamp_sign")
        # Clamping one bound to the target removes that side's padding --
        # extend the OTHER (safe/outward) bound to make up for it, or the
        # span collapses below `width` and recreates the exact thin-nub
        # problem the padding exists to prevent (real bug found this way:
        # see the third-bug note above).
        if axis == "x":
            if sign < 0:
                x1, x0 = tx, min(x0, tx - width)
            elif sign > 0:
                x0, x1 = tx, max(x1, tx + width)
        elif axis == "y":
            if sign < 0:
                y1, y0 = ty, min(y0, ty - width)
            elif sign > 0:
                y0, y1 = ty, max(y1, ty + width)
        # Where a via gets drawn at this landing point, the stub must cover
        # that via's whole footprint. Real DRC failure, not a precaution:
        # the stub is centred on the grid-point-to-target segment, which is
        # offset from the via's own centre, and the via's met2 pad (0.29um)
        # is wider than the stub (0.21um) -- so a sliver of pad stuck out
        # unbridged. On the reference design's R0 that sliver was 0.04um
        # wide and sat 0.02um below the horizontal track wire: 12 real Magic
        # "Metal1 spacing < 0.14um (met1.2)" errors at R0's two ports.
        # Expanding the stub to enclose the via's own box removes the
        # sliver -- the pad is then fully merged into the stub, so there is
        # no isolated edge left to violate spacing.
        via_um = stub.get("via_um")
        if via_um and math.isfinite(via_um):
            vhw = via_um / 2
            x0, x1 = min(x0, tx - vhw), max(x1, tx + vhw)
            y0, y1 = min(y0, ty - vhw), max(y1, ty + vhw)
        emitted_stubs.append((stub["net"], stub["layer"], x0, y0, x1, y1))

    # Merge same-net, same-layer landing stubs that overlap or sit closer
    # than the minimum metal spacing, into their union rectangle.
    #
    # Needed because one net can now land on SEVERAL terminals of one macro
    # (see the per-terminal loop above). Two such stubs are built from
    # different exact port coordinates, so they come out very slightly
    # different in the cross axis -- and where they overlap, the union has a
    # step. Measured on the reference design: VDD's stubs on
    # current_mirror_XMP1_XMP2 spanned y -41.066..-40.815 and
    # -41.025..-40.815, a 0.041um step, which Magic flagged as 6 met2.2 plus
    # 3 met1.2 slivers. They are the same net, so merging is electrically
    # identical and removes the step. Only overlapping/too-close pairs are
    # merged -- stubs genuinely far apart (a mirror's per-branch source
    # stubs, metres apart in grid terms) stay separate.
    merged = True
    while merged:
        merged = False
        for i in range(len(emitted_stubs)):
            for j in range(i + 1, len(emitted_stubs)):
                a, b = emitted_stubs[i], emitted_stubs[j]
                if a[0] != b[0] or a[1] != b[1]:
                    continue
                gap = max(max(b[2] - a[4], a[2] - b[4]), max(b[3] - a[5], a[3] - b[5]))
                if gap >= min_metal_spacing:
                    continue
                emitted_stubs[i] = (a[0], a[1], min(a[2], b[2]), min(a[3], b[3]),
                                    max(a[4], b[4]), max(a[5], b[5]))
                del emitted_stubs[j]
                merged = True
                break
            if merged:
                break
    for net, layer, x0, y0, x1, y1 in emitted_stubs:
        routes[net].append({"kind": "wire", "layer": layer,
                             "points_um": [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]})

    for net, macro in single_pin_nets.items():
        candidates = world_ports[net][macro]
        wx, wy, layer_name, _width, _pin = candidates[0]
        routes[net] = [{"kind": "label_only", "x_um": wx, "y_um": wy, "layer": layer_name}]

    total_wire_um = 0.0
    total_vias = 0
    sensitive_vias = 0
    for net, segs in routes.items():
        for s in segs:
            if s["kind"] == "wire":
                pts = s["points_um"]
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                total_wire_um += max(max(xs) - min(xs), max(ys) - min(ys))
            elif s["kind"] == "via":
                total_vias += 1
                if net in sensitive_nets:
                    sensitive_vias += 1

    routed_ok = status["converged"] and not status["failed_nets"]
    print(f"\n=== Routing result ===")
    print(f"  routed {len(net_paths)}/{len(net_macros)} multi-macro net(s), "
          f"labeled {len(single_pin_nets)}/{len(single_pin_nets)} single-macro pin(s)  "
          f"total_wire={total_wire_um:.2f}um  vias={total_vias} (sensitive={sensitive_vias})")
    print(f"  Congestion check: {'PASS (converged, no failed nets)' if routed_ok else 'FAIL -- ' + json.dumps(status)}")

    out_path = Path(args.out).resolve() if args.out else design_dir / "routes.json"
    out_path.write_text(json.dumps({
        "design": manifest.get("design"), "routes": routes, "status": status,
        "sensitive_nets": sorted(sensitive_nets), "total_wire_um": total_wire_um,
        "total_vias": total_vias, "sensitive_vias": sensitive_vias,
        "grid": {k: v for k, v in grid.items()}, "routing_layers": routing_layers,
    }, indent=2))
    print(f"  wrote {out_path}")

    summary_path = (Path(args.summary_out).resolve() if args.summary_out
                     else design_dir / "routing_summary.txt")
    write_routing_summary(summary_path, manifest.get("design"), design_dir, routes, placement,
                           grid, routing_layers, layer_dirs, wire_widths, via_costs_um,
                           blocked, obstacle_stats, status, sensitive_nets, single_pin_nets,
                           net_macros, landing_stats, args, time.time() - t_start)
    print(f"  wrote {summary_path} (routing performance report)")

    if args.do_gds:
        gds_path = Path(args.gds).resolve() if args.gds else design_dir / "routed.gds"
        render_gds(manifest, placement, routes, grid, routing_layers, wire_widths, gds_path)
        print(f"  wrote {gds_path}")

    if not routed_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
