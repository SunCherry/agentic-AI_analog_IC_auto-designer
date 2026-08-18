#!/usr/bin/env python3
"""Top-level placement: simulated annealing over macro positions.

Port of `/Users/cherrysun/Downloads/analog-placer`'s `placer/anneal.py` +
`placer/macros.py` cost function and annealing loop -- same algorithm
(HPWL + overlap + symmetry penalty, exponential cooling), but driven by
REAL inputs instead of that sketch's toy unit-finger footprint model, and
a third move type the sketch doesn't have: **rotate**, alongside displace
and swap (70%/15%/15% of moves respectively). Rotating a macro 90 degrees
swaps its placed width/height (`effective_wh()`) -- a real, useful lever
for HPWL/area/density, not just position: a tall narrow macro rotated
sideways can compact into a wide layout, or clear a tight neighbor, in a
way no amount of displacement alone achieves. Represented as a plain bool
per macro (`pos[name] = (x, y, rotated)`), not a 4-way angle -- 180/270
degrees give the identical bounding box to 0/90 respectively in this
box-only cost model (only real port-facing/routability would distinguish
them further, which this coarse macro-granularity model doesn't track --
see `../SKILL.md`'s Step 2). `render_placement.py` applies the real
90-degree rotation (`.rotate()`) to
each macro's actual glayout geometry when re-assembling the visualization,
about the same center point the annealer computed -- not just relabeling
a swapped bounding box.
  - macro width/height come from `generate_primitives.py`'s manifest.json
    (real glayout `Component` bounding boxes via `evaluate_bbox`, not a
    made-up "1 unit per finger" formula).
  - net connectivity comes from the same manifest's `device_index` (real
    netlist drain/gate/source per device, from the two parsers
    `generate_primitives.py` already merged), collapsed to macro
    granularity: a net's HPWL "pins" are the distinct MACROS it touches,
    using each macro's placed centroid. This is a deliberate scope for a
    coarse global floorplan pass -- per-port routing is
    `routing-handler/SKILL.md`'s job, done after placement, using each
    macro's real glayout ports.
  - supply-rail exclusion reads manifest.json's own `supply_rail_names`
    (written by `generate_primitives.py`'s `SUPPLY_RAIL_NAMES`), not a
    redeclared shorter tuple.

Two additional terms the sketch doesn't have:

**Routing-density clearance penalty**: a macro with more nets to route
needs more channel room around it, or its neighbors will congest. For
each macro, `min_distance = 10 * min_metal_spacing * num_nets` (`num_nets`
= how many distinct, non-supply-rail nets that macro's own devices touch
-- from `build_net_to_macros()`; `min_metal_spacing` = the finest real
metal `min_separation` on the target PDK, queried by
`generate_primitives.py` and carried in `manifest.json`, not hardcoded).
For every macro pair, the required clearance between them is
`max(min_distance_A, min_distance_B)` -- the denser macro's own channel
need dominates, since it needs that room from *any* neighbor, not just a
particular one. A pair closer than its required clearance (but not
literally overlapping -- that's the separate `overlap` term) is charged
the shortfall.

**Total footprint area penalty**: `area = (max_x - min_x) * (max_y -
min_y)`, taken over every placed macro's real extent (each macro's own
`x0/y0/x1/y1`, not just its anchor point -- same "bounding box of every
device" convention `../../placement-optimizer/script/generate_grid.py`'s
`overall_bbox()` already uses) -- a compaction pressure alongside HPWL, so
the annealer doesn't spread macros out further than wire length alone
would demand.

Symmetry groups (macroA, macroB, axis_x -- penalize centroid mismatch
across a vertical axis) are NOT auto-inferred here: which macros should
mirror each other structurally (e.g. two symmetric branches of a
folded-cascode) is a circuit-understanding judgment call, left to whoever
invokes this script (see `../SKILL.md`). Supply one via `--sym-groups
groups.json`: a JSON list of `[macroA, macroB, axis_x]` triples.

SA is a heuristic: it can converge close to zero overlap but not
guaranteed exactly zero. This script reports the REAL final overlap area
as an explicit PASS/FAIL line -- never silently treat a nonzero residual
as done. Run `placement-optimizer/script/generate_grid.py` for a DRC-legality
snap/check before trusting these coordinates as final routing input.

**Stopping criterion: acceptance ratio, not a fixed move count.** `--iters`
is an upper bound; `anneal()` actually stops once a full stage's accepted-
move fraction drops below `--accept-threshold` (default 0.02 = 2%) --
see `anneal()`'s own docstring for the full rationale (a raw temperature
floor needs you to already know the "right" T_min for this run's cost
magnitudes; the acceptance ratio adapts to whatever they turn out to be).

`--iters` is REQUIRED and deliberately has no default: the budget is the
user's call, and the old default of 20 silently produced a barely-perturbed
random placement that fails the overlap gate. Ask for it.

Usage:
  python anneal_placement.py <manifest.json> --iters N [--out placement_pos.json]
      [--t0 50.0] [--accept-threshold 0.02] [--stage-iters N] [--seed 1]
      [--w-wire 1.0] [--w-ov 50.0] [--w-sym 10.0] [--w-density 5.0] [--w-area 0.01]
      [--sym-groups groups.json] [--min-metal-spacing UM]
"""
import argparse
import datetime
import time
import json
import math
import random
import subprocess
import sys
from pathlib import Path


# Macro TIERS for the two-phase placement below. A "composite" macro is a
# multi-device subcircuit block `generate_primitives.py` built as one unit
# (`cells/blocks/current_mirror.py`'s current_mirror(), `cells/blocks/diff_pair.py`'s
# diff_pair()) -- exactly the two kinds a DECOMPOSED netlist names as its
# own `.subckt` blocks (see `../../circuit-decomposition/SKILL.md` and
# `decomposition_patterns.py`'s MACRO_TOPOLOGIES, kept deliberately in step
# with this set). Everything else -- `single_nfet`/`single_pfet`/`single_res`/
# `single_cap`, and `current_mirror_leg` (a lone extra mirror output
# device, physically just one fet) -- is a single device, placed in the
# second phase around the skeleton the composites establish.
COMPOSITE_KINDS = {"current_mirror", "differential_pair"}


def split_tiers(macros):
    """(composite_macros, single_devices) -- see COMPOSITE_KINDS."""
    composite = [m for m in macros if m["kind"] in COMPOSITE_KINDS]
    singles = [m for m in macros if m["kind"] not in COMPOSITE_KINDS]
    return composite, singles


def effective_wh(m, rotated):
    """A macro's placed footprint depends on orientation: 90 degrees swaps
    width and height, 0 degrees doesn't. This is the full space of
    DISTINCT bounding boxes rotation can produce for a box-only model --
    180/270 degrees give the identical footprint to 0/90 respectively
    (only real port-facing/routability would distinguish them further,
    which this coarse macro-granularity model doesn't track -- see the
    module docstring's "Rotation" note). `rotated` is a plain bool, not a
    4-way angle, for exactly this reason."""
    return (m["h"], m["w"]) if rotated else (m["w"], m["h"])


def local_to_world(lx, ly, cx, cy, rotated):
    """Same transform `../../router/script/route_nets.py`'s own
    `local_to_world()` uses (kept bit-for-bit identical, not
    re-derived -- see that function's docstring for the CCW-90
    confirmation): `ref.move(destination=(cx,cy))` then, if rotated,
    `ref.rotate(90, center=(cx,cy))`, so a port's world coordinate lands
    exactly where the macro's actual drawn geometry lands."""
    wx, wy = lx + cx, ly + cy
    if not rotated:
        return wx, wy
    dx, dy = wx - cx, wy - cy
    return cx - dy, cy + dx


def ports_to_world(local_ports, x, y, w, h, rotated):
    """`local_ports`: manifest.json's per-macro `ports` field (net ->
    list of `{x, y, layer, width}` in LOCAL, pre-placement coordinates --
    see `generate_primitives.py`'s `near_edge_ports()`). `x, y, w, h`:
    this macro's placed position and EFFECTIVE (post-rotation) footprint,
    i.e. exactly `positions[name]`'s own fields -- `cx, cy` computed from
    these matches `route_nets.py`'s `world_port_map()` bit-for-bit, so a
    port's world coordinate here is the same one the router itself lands
    wires on, not a separately-reasoned-about approximation."""
    cx, cy = x + w / 2, y + h / 2
    world = {}
    for net, pts in (local_ports or {}).items():
        net_pts = []
        for pt in pts:
            wx, wy = local_to_world(pt["x"], pt["y"], cx, cy, rotated)
            net_pts.append({"x": wx, "y": wy, "layer": pt["layer"], "width": pt.get("width")})
        world[net] = net_pts
    return world


def load_macros(manifest):
    macros = []
    for m in manifest["macros"]:
        if m.get("w") is None or m.get("h") is None:
            print(f"  warning: {m['name']} has no generated geometry (generation={m['generation']}) "
                  f"-- excluded from placement, needs manual layout")
            continue
        macros.append({"name": m["name"], "w": m["w"], "h": m["h"], "kind": m["kind"]})
    return macros


def build_net_to_macros(manifest):
    supply = set(n.lower() for n in manifest.get("supply_rail_names", []))
    net_to_macros = {}
    for name, dev in manifest["device_index"].items():
        macro = dev.get("macro")
        if macro is None:
            continue
        for pin in ("drain", "gate", "source"):
            net = dev.get(pin)
            if not net or net.lower() in supply:
                continue
            net_to_macros.setdefault(net, set()).add(macro)
    return {net: macs for net, macs in net_to_macros.items() if len(macs) >= 2}


def hpwl_by_net(macro_by_name, net_to_macros, pos):
    """Per-net half-perimeter wire length, `{net: (hpwl_um, num_macros)}`.

    The same computation hpwl() totals up, kept per-net so the placement
    summary can name WHICH nets dominate the wire length -- an aggregate
    number says a placement is worse without saying where to look."""
    per_net = {}
    for net, macs in net_to_macros.items():
        xs, ys = [], []
        for name in macs:
            if name not in pos:
                continue
            mx, my, mrot = pos[name]
            ew, eh = effective_wh(macro_by_name[name], mrot)
            xs.append(mx + ew / 2)
            ys.append(my + eh / 2)
        if len(xs) >= 2:
            per_net[net] = ((max(xs) - min(xs)) + (max(ys) - min(ys)), len(xs))
    return per_net


def hpwl(macro_by_name, net_to_macros, pos):
    return sum(v[0] for v in hpwl_by_net(macro_by_name, net_to_macros, pos).values())


def overlap(macros, pos):
    pen = 0.0
    for i, a in enumerate(macros):
        ax, ay, arot = pos[a["name"]]
        aw, ah = effective_wh(a, arot)
        for b in macros[i + 1:]:
            bx, by, brot = pos[b["name"]]
            bw, bh = effective_wh(b, brot)
            ox = min(ax + aw, bx + bw) - max(ax, bx)
            oy = min(ay + ah, by + bh) - max(ay, by)
            if ox > 0 and oy > 0:
                pen += ox * oy
    return pen


def total_overlap_area(macros, pos):
    """Same computation as overlap(), kept separate so callers get the raw
    area (for the PASS/FAIL report) independent of any cost weighting."""
    return overlap(macros, pos)


def bbox_area(macros, pos):
    """area = (max_x - min_x) * (max_y - min_y) over every placed macro's
    real extent (x0/y0/x1/y1, using each macro's EFFECTIVE -- post-
    rotation -- w/h, not just its anchor point) -- the total footprint the
    annealer is compacting, same role HPWL plays for wire length."""
    min_x = min(pos[m["name"]][0] for m in macros)
    min_y = min(pos[m["name"]][1] for m in macros)
    max_x, max_y = float("-inf"), float("-inf")
    for m in macros:
        x, y, rot = pos[m["name"]]
        ew, eh = effective_wh(m, rot)
        max_x = max(max_x, x + ew)
        max_y = max(max_y, y + eh)
    return (max_x - min_x) * (max_y - min_y)


def placement_density(macros, pos):
    """Real placement density (area utilization), as a dict.

    Distinct from this script's `density_penalty()`, which despite the name
    measures routing-CLEARANCE shortfall -- how much closer than
    `min_distance` some pair of macros sits. Both are worth reporting and
    they answer different questions:

        utilization = sum(macro areas) / bounding-box area

    -- what fraction of the floorplan is actually occupied by cells. Low
    utilization on an analog block is not automatically bad (the clearance
    penalty deliberately buys routing channels), but it is the number that
    says whether the annealer compacted anything, and together with
    `bbox_aspect` it is what a human reads first."""
    x0 = min(pos[m["name"]][0] for m in macros)
    y0 = min(pos[m["name"]][1] for m in macros)
    x1 = y1 = float("-inf")
    cell_area = 0.0
    for m in macros:
        x, y, rot = pos[m["name"]]
        ew, eh = effective_wh(m, rot)
        x1 = max(x1, x + ew)
        y1 = max(y1, y + eh)
        cell_area += ew * eh
    bw, bh = x1 - x0, y1 - y0
    box = bw * bh
    return {
        "bbox_w_um": bw, "bbox_h_um": bh, "bbox_area_um2": box,
        "bbox_aspect": (max(bw, bh) / min(bw, bh)) if min(bw, bh) > 0 else float("inf"),
        "cell_area_um2": cell_area,
        "utilization": (cell_area / box) if box > 0 else 0.0,
        "free_area_um2": box - cell_area,
    }


def compute_macro_net_counts(macro_by_name, net_to_macros):
    """How many distinct (non-supply-rail) nets each macro touches."""
    counts = {name: 0 for name in macro_by_name}
    for macs in net_to_macros.values():
        for name in macs:
            if name in counts:
                counts[name] += 1
    return counts


def compute_min_distances(macro_net_counts, min_metal_spacing_um):
    """min_distance = 10 * min_metal_spacing * num_nets -- the routing-
    channel clearance a macro needs from ANY neighbor, scaling with how
    many nets have to be routed in/out of it."""
    return {name: 10.0 * min_metal_spacing_um * n for name, n in macro_net_counts.items()}


def _box_gap(ax, ay, aw, ah, bx, by, bw, bh):
    """Chebyshev-style separation between two boxes -- same box-edge math
    as overlap()'s ox/oy, just measuring gap instead of intersection.
    Positive on the axis that actually separates the boxes; if the boxes
    are only separated diagonally, this returns the larger of the two
    axis gaps (a routing channel is Manhattan, not diagonal, so the
    binding constraint is whichever axis has less room). Takes explicit
    dims (not macro dicts) so callers pass EFFECTIVE, post-rotation w/h."""
    x_gap = max(bx - (ax + aw), ax - (bx + bw))
    y_gap = max(by - (ay + ah), ay - (by + bh))
    return max(x_gap, y_gap)


def density_penalty(macros, pos, min_distances):
    pen = 0.0
    worst_shortfall = 0.0
    for i, a in enumerate(macros):
        ax, ay, arot = pos[a["name"]]
        aw, ah = effective_wh(a, arot)
        for b in macros[i + 1:]:
            bx, by, brot = pos[b["name"]]
            bw, bh = effective_wh(b, brot)
            gap = _box_gap(ax, ay, aw, ah, bx, by, bw, bh)
            required = max(min_distances[a["name"]], min_distances[b["name"]])
            shortfall = max(0.0, required - gap)
            pen += shortfall
            worst_shortfall = max(worst_shortfall, shortfall)
    return pen, worst_shortfall


def symmetry_penalty(macro_by_name, pos, sym_groups):
    pen = 0.0
    for a, b, axis in sym_groups:
        if a not in pos or b not in pos:
            continue
        ax, ay, arot = pos[a]
        bx, by_, brot = pos[b]
        aw, _ = effective_wh(macro_by_name[a], arot)
        bw, _ = effective_wh(macro_by_name[b], brot)
        ca = ax + aw / 2
        cb = bx + bw / 2
        pen += abs((ca + cb) / 2 - axis) + abs(ay - by_)
    return pen


def cost(macro_by_name, net_to_macros, pos, sym_groups, min_distances, w_wire, w_ov, w_sym, w_density, w_area):
    macros = list(macro_by_name.values())
    density_pen, _ = density_penalty(macros, pos, min_distances)
    return (w_wire * hpwl(macro_by_name, net_to_macros, pos)
            + w_ov * overlap(macros, pos)
            + w_sym * symmetry_penalty(macro_by_name, pos, sym_groups)
            + w_density * density_pen
            + w_area * bbox_area(macros, pos))


def anneal(macros, net_to_macros, sym_groups, min_distances, iters, t0, seed, w_wire, w_ov, w_sym, w_density, w_area,
           accept_threshold=0.02, stage_iters=None, movable=None, init_pos=None, label=None):
    """`iters` is now an UPPER BOUND, not a guaranteed move count -- see the
    acceptance-ratio stopping criterion below, which usually ends the run
    earlier than that.

    **`movable` / `init_pos` -- what makes the two-phase (macros-first)
    placement in `main()` possible.** `macros` is everything that
    participates in the COST (overlap, density clearance, HPWL, bbox area);
    `movable` is the subset the annealer is actually allowed to move
    (default: all of them). `init_pos` seeds positions already decided by an
    earlier phase; any macro not in it starts at a random point as before.
    So phase 2 passes every macro as `macros` (so single devices are
    genuinely charged for overlapping or crowding an already-placed
    composite block) but only the single devices as `movable`, with phase
    1's result as `init_pos` -- the composites stay exactly where phase 1
    put them. Passing neither reproduces the original single-phase
    behavior exactly.

    **Acceptance-ratio stopping** (replaces a raw temperature floor):
    moves are grouped into stages of `stage_iters` each (default
    `max(50, 20 * len(movable))` -- a stage scales with problem size, same
    reasoning as VLSI SA placers that size a stage off the number of
    movable objects, not a fixed constant; in phase 2 that is the single
    devices, not every macro being charged for in the cost). After each full stage, if the
    fraction of ACCEPTED moves (improving moves, plus uphill moves the
    Metropolis criterion let through -- not just improving ones) in that
    stage is below `accept_threshold` (default 0.02 = 2%, within the
    commonly-used 1-5% range; some placers go as low as ~0.5%), the run
    stops right there instead of continuing to `iters`. Rationale: a low
    acceptance ratio means the search has effectively become greedy local
    descent at the CURRENT temperature -- further cooling from here buys
    almost nothing. This is more robust than picking a `t0 * 0.001**k`
    floor and hoping it's low enough: the "right" stopping temperature
    depends on this run's own cost magnitudes (which scale with
    `w_wire`/`w_ov`/`w_sym`/`w_density`/`w_area` and the design's own
    macro count/sizes), not something knowable in advance -- the
    acceptance ratio adapts to whatever that landscape turns out to be."""
    rng = random.Random(seed)
    macro_by_name = {m["name"]: m for m in macros}
    movable = list(macros) if movable is None else list(movable)
    span = max(3.0, math.sqrt(sum(m["w"] * m["h"] for m in macros)) * 1.6)
    # pos[name] = (x, y, rotated) -- rotated=True swaps the macro's placed
    # w/h (see effective_wh()). Start unrotated; the "rotate" move below is
    # what discovers a better orientation, same as "displace"/"swap"
    # discover a better position. Macros carried in via `init_pos` keep the
    # position (and rotation) an earlier phase already chose for them.
    pos = dict(init_pos or {})
    for m in macros:
        if m["name"] not in pos:
            pos[m["name"]] = (rng.uniform(0, span), rng.uniform(0, span), False)

    c = cost(macro_by_name, net_to_macros, pos, sym_groups, min_distances, w_wire, w_ov, w_sym, w_density, w_area)
    if not movable:
        return pos, c
    if label:
        print(f"  --- {label}: {len(movable)} movable of {len(macros)} macro(s) ---")
    best_pos, best_c = dict(pos), c
    t = t0
    report_every = max(1, iters // 10)
    if stage_iters is None:
        stage_iters = max(50, 20 * len(movable))
    stage_accepted = 0
    stage_moves = 0
    # Move mix: displace dominates (it's what actually explores the plane);
    # swap and rotate each get a meaningful but smaller share -- enough to
    # be tried regularly without drowning out displacement moves.
    it = 0
    while it < iters:
        m = rng.choice(movable)
        old = pos[m["name"]]
        move = "displace"
        m2 = None
        old2 = None
        r = rng.random()
        if r < 0.70:
            step = max(0.1, span * t / t0 * 0.3)
            pos[m["name"]] = (old[0] + rng.uniform(-step, step), old[1] + rng.uniform(-step, step), old[2])
        elif r < 0.85:
            move = "swap"
            # Swap only among MOVABLE macros -- swapping with a macro an
            # earlier phase already placed would move it, which is exactly
            # what this phase is not allowed to do.
            m2 = rng.choice(movable)
            old2 = pos[m2["name"]]
            # Swap WHERE the two macros sit, not their individual
            # orientation -- each keeps its own rotation flag.
            pos[m["name"]] = (old2[0], old2[1], old[2])
            pos[m2["name"]] = (old[0], old[1], old2[2])
        else:
            move = "rotate"
            pos[m["name"]] = (old[0], old[1], not old[2])
        c2 = cost(macro_by_name, net_to_macros, pos, sym_groups, min_distances, w_wire, w_ov, w_sym, w_density, w_area)
        accepted = c2 < c or rng.random() < math.exp((c - c2) / max(t, 1e-9))
        if accepted:
            c = c2
            if c < best_c:
                best_pos, best_c = dict(pos), c
        else:
            pos[m["name"]] = old
            if move == "swap":
                pos[m2["name"]] = old2
        stage_moves += 1
        stage_accepted += 1 if accepted else 0
        t = t0 * (0.001 ** (it / iters))
        if it % report_every == 0:
            print(f"  iter {it:6d}/{iters}  t={t:8.3f}  cost={c:10.2f}  best={best_c:10.2f}")
        if stage_moves >= stage_iters:
            ratio = stage_accepted / stage_moves
            if ratio < accept_threshold:
                print(f"  acceptance ratio {ratio:.2%} < threshold {accept_threshold:.2%} over the last "
                      f"{stage_moves} moves (t={t:.4f}) -- stopping early at iter {it + 1}/{iters}: "
                      f"greedy local descent, further cooling would buy little")
                it += 1
                break
            stage_accepted = 0
            stage_moves = 0
        it += 1
    return best_pos, best_c


def write_placing_summary(path, result, macros, pos, macro_by_name, net_to_macros,
                           macro_net_counts, min_distances, density, per_net,
                           composite, singles, elapsed_s):
    """Human-readable placement-performance report.

    `placement_pos.json` already carries every number here, but it is a
    machine artifact -- the positions dict dwarfs the metrics, and nothing
    in it ranks anything. This is the file to open to answer "is this
    placement any good, and if not where is it bad": the quality gates
    first, then the metrics, then the nets and macro pairs actually
    responsible.

    Written every run, PASS or FAIL. A summary that only appears on
    success is the one you cannot diff against the run that regressed."""
    L = []
    A = L.append
    ok = result["overlap_ok"] and result["density_ok"]
    A("=" * 72)
    A(f"PLACEMENT SUMMARY -- {result.get('design') or '(unnamed design)'}")
    A("=" * 72)
    A(f"generated   : {datetime.datetime.now().isoformat(timespec='seconds')}")
    A(f"manifest    : {result['manifest']}")
    A(f"macros      : {len(macros)} placed"
      f"  ({sum(1 for m in macros if pos[m['name']][2])} rotated 90deg)")
    A(f"runtime     : {elapsed_s:.1f}s")
    A(f"overall     : {'PASS' if ok else 'FAIL'}")
    A("")
    A("-- quality gates ---------------------------------------------------")
    A(f"  overlap            : {'PASS' if result['overlap_ok'] else 'FAIL'}"
      f"   area={result['final_overlap_area_um2']:.4f} um^2  (must be 0)")
    A(f"  routing clearance  : {'PASS' if result['density_ok'] else 'FAIL'}"
      f"   worst shortfall={result['worst_density_shortfall_um']:.4f} um  (must be 0)")
    A("       clearance rule: each pair must be >= max of the two macros'")
    A("       min_distance = 10 * min_metal_spacing * num_nets")
    A("")
    A("-- wire length -----------------------------------------------------")
    A(f"  total HPWL         : {result['final_hpwl_um']:.2f} um")
    A(f"  nets counted       : {len(per_net)}  (nets touching >= 2 placed macros)")
    if per_net:
        A(f"  mean / max per net : {result['final_hpwl_um'] / len(per_net):.2f} um"
          f"  /  {max(v[0] for v in per_net.values()):.2f} um")
    A("")
    A("-- placement density -----------------------------------------------")
    A(f"  bounding box       : {density['bbox_w_um']:.2f} x {density['bbox_h_um']:.2f} um"
      f"   = {density['bbox_area_um2']:.2f} um^2   (aspect {density['bbox_aspect']:.2f}:1)")
    A(f"  cell area          : {density['cell_area_um2']:.2f} um^2")
    A(f"  utilization        : {100.0 * density['utilization']:.1f}%"
      f"   ({density['free_area_um2']:.2f} um^2 free)")
    A("       low utilization is not automatically bad here -- the clearance")
    A("       penalty deliberately buys routing channels between macros")
    A("")
    A("-- cost breakdown (weight x term = contribution) --------------------")
    w = result["weights"]
    terms = [("wire (HPWL)", w["w_wire"], result["final_hpwl_um"]),
             ("overlap", w["w_ov"], result["final_overlap_area_um2"]),
             ("symmetry", w["w_sym"], result["final_symmetry_penalty"]),
             ("clearance", w["w_density"], result["final_density_penalty_um"]),
             ("area", w["w_area"], result["final_bbox_area_um2"])]
    for name, weight, raw in terms:
        contrib = weight * raw
        share = (100.0 * contrib / result["final_cost"]) if result["final_cost"] else 0.0
        A(f"  {name:18s}: {weight:8.3f} x {raw:12.4f} = {contrib:12.2f}  ({share:5.1f}%)")
    A(f"  {'TOTAL':18s}: {' ' * 25}{result['final_cost']:12.2f}")
    A("")
    A("-- worst nets by HPWL ----------------------------------------------")
    for net, (length, n) in sorted(per_net.items(), key=lambda kv: -kv[1][0])[:10]:
        share = 100.0 * length / result["final_hpwl_um"] if result["final_hpwl_um"] else 0.0
        A(f"  {net:16s} {length:9.2f} um  ({share:5.1f}%)  spans {n} macro(s)")
    A("")
    A("-- tightest macro pairs (gap vs required clearance) -----------------")
    pairs = []
    for i, a in enumerate(macros):
        ax, ay, arot = pos[a["name"]]
        aw, ah = effective_wh(a, arot)
        for b in macros[i + 1:]:
            bx, by, brot = pos[b["name"]]
            bw, bh = effective_wh(b, brot)
            gap = _box_gap(ax, ay, aw, ah, bx, by, bw, bh)
            req = max(min_distances[a["name"]], min_distances[b["name"]])
            pairs.append((gap - req, a["name"], b["name"], gap, req))
    nw = max([len(m["name"]) for m in macros] + [10])
    for slack, an, bn, gap, req in sorted(pairs)[:8]:
        flag = "  <-- VIOLATION" if slack < -1e-9 else ""
        A(f"  {an:{nw}s} <-> {bn:{nw}s} gap={gap:8.2f} required={req:7.2f} "
          f"slack={slack:+8.2f}{flag}")
    A("")
    A("-- placed macros ---------------------------------------------------")
    A(f"  {'macro':{nw}s} {'x':>9s} {'y':>9s} {'w':>7s} {'h':>7s} {'rot':>4s} "
      f"{'nets':>5s} {'min_dist':>9s}")
    for tier_name, tier in (("composite macros (placed first)", composite),
                             ("single devices", singles)) if result["two_phase"] else (("", macros),):
        if tier_name:
            A(f"  -- {tier_name} --")
        for m in sorted(tier, key=lambda mm: mm["name"]):
            x, y, rot = pos[m["name"]]
            ew, eh = effective_wh(m, rot)
            A(f"  {m['name']:{nw}s} {x:9.2f} {y:9.2f} {ew:7.2f} {eh:7.2f} "
              f"{'90' if rot else '0':>4s} {macro_net_counts[m['name']]:5d} "
              f"{min_distances[m['name']]:9.2f}")
    A("")
    A("-- reproducibility -------------------------------------------------")
    A(f"  seed={result['seed']}  iters(upper bound)={result['iters']}  "
      f"two_phase={result['two_phase']}")
    A(f"  weights: " + "  ".join(f"{k}={v}" for k, v in w.items()))
    A(f"  min_metal_spacing_um={result['min_metal_spacing_um']}")
    if result.get("excluded_no_geometry"):
        A(f"  EXCLUDED (no geometry): {', '.join(result['excluded_no_geometry'])}")
    A("=" * 72)
    Path(path).write_text("\n".join(L) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", help="manifest.json from generate_primitives.py")
    parser.add_argument("--out", default=None, help="default: <manifest's dir>/../placement_pos.json")
    parser.add_argument("--iters", type=int, required=True,
                         help="REQUIRED, no default: the iteration budget is the user's call, and "
                              "the old default (20) silently produced a barely-perturbed random "
                              "placement that fails overlap. UPPER BOUND on moves -- the run "
                              "usually stops earlier once the acceptance ratio drops below "
                              "--accept-threshold, so a large value costs nothing. Ask the user "
                              "for it (20000 is a reasonable suggestion) rather than picking one")
    parser.add_argument("--t0", type=float, default=50.0)
    parser.add_argument("--accept-threshold", type=float, default=0.02,
                         help="stop once a full stage's accepted-move fraction drops below this "
                              "(default 0.02 = 2%%, commonly-used 1-5%% range) -- see anneal()'s own "
                              "docstring for why this replaces a raw temperature floor")
    parser.add_argument("--stage-iters", type=int, default=None,
                         help="moves per acceptance-ratio stage -- default max(50, 20*num_macros)")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--single-phase", dest="two_phase", action="store_false", default=True,
                         help="anneal every macro jointly in one pass (the pre-existing behavior) "
                              "instead of the default two-phase 'composite macros first, then single "
                              "devices around them' -- see split_tiers()/COMPOSITE_KINDS")
    parser.add_argument("--joint-refine-iters", type=int, default=0,
                         help="after the two phases, optionally re-anneal with EVERYTHING movable "
                              "for this many moves (default 0 = off; it can undo the macros-first "
                              "hierarchy the two phases establish)")
    parser.add_argument("--w-wire", type=float, default=1.0)
    parser.add_argument("--w-ov", type=float, default=50.0)
    parser.add_argument("--w-sym", type=float, default=10.0)
    parser.add_argument("--w-density", type=float, default=5.0,
                         help="weight on the routing-density clearance penalty "
                              "(min_distance = 10 * min_metal_spacing * num_nets per macro)")
    parser.add_argument("--w-area", type=float, default=0.01,
                         help="weight on the total footprint area penalty "
                              "((max_x-min_x)*(max_y-min_y) over every placed macro's real extent)")
    parser.add_argument("--min-metal-spacing", type=float, default=None,
                         help="override manifest.json's min_metal_spacing_um (real PDK value, "
                              "written by generate_primitives.py) -- normally not needed")
    parser.add_argument("--sym-groups", default=None, help="JSON list of [macroA, macroB, axis_x] triples")
    parser.add_argument("--summary-out", default=None,
                         help="placement-performance report (default: "
                              "<placement_pos.json's dir>/placing_summary.txt). Written every "
                              "run, PASS or FAIL -- a summary that only appears on success is "
                              "the one you cannot diff against the run that regressed")
    # `--physical-map-out` is gone: every macro's placed box (`x0_um`/`y0_um`/
    # `x1_um`/`y1_um`) is now part of each `positions` entry in
    # placement_pos.json itself, so the separate physical_map.json snapshot --
    # the same numbers in a second file, free to drift -- is no longer written.
    parser.add_argument("--no-render", dest="render", action="store_false", default=True,
                         help="skip auto-rendering placement_visualization.gds (render_placement.py) at "
                              "the end -- rendering imports glayout (slow PDK activation); pass this to "
                              "keep repeated anneal runs (weight/seed sweeps) fast, and render once "
                              "separately when you land on a result worth looking at")
    args = parser.parse_args()
    t_start = time.time()

    manifest_path = Path(args.manifest).resolve()
    manifest = json.loads(manifest_path.read_text())

    macros = load_macros(manifest)
    if not macros:
        sys.exit("no macros with real geometry to place")
    macro_by_name = {m["name"]: m for m in macros}
    # Unlike macro_by_name (load_macros()'s trimmed {name,w,h,kind} view,
    # all the anneal loop itself needs), this keeps every manifest.json
    # field, including "ports" -- needed below to write real world-space
    # port coordinates into placement_pos.json.
    manifest_macro_by_name = {m["name"]: m for m in manifest["macros"]}
    net_to_macros = build_net_to_macros(manifest)

    min_metal_spacing = args.min_metal_spacing
    if min_metal_spacing is None:
        min_metal_spacing = manifest.get("min_metal_spacing_um")
        if min_metal_spacing is None:
            sys.exit("manifest.json has no min_metal_spacing_um (regenerate it with the current "
                      "generate_primitives.py) and no --min-metal-spacing override was given")
    macro_net_counts = compute_macro_net_counts(macro_by_name, net_to_macros)
    min_distances = compute_min_distances(macro_net_counts, min_metal_spacing)

    sym_groups = []
    if args.sym_groups:
        sym_groups = [tuple(g) for g in json.loads(Path(args.sym_groups).read_text())]

    composite, singles = split_tiers(macros)
    two_phase = args.two_phase and composite and singles

    print(f"=== Annealing placement: {len(macros)} macros, {len(net_to_macros)} nets (excl. supply rails) ===")
    print(f"  min_metal_spacing={min_metal_spacing:.4f}um -- min_distance per macro (10x spacing x num_nets): "
          + ", ".join(f"{n}={min_distances[n]:.2f}um" for n in min_distances))
    common = dict(accept_threshold=args.accept_threshold, stage_iters=args.stage_iters)
    weights = (args.w_wire, args.w_ov, args.w_sym, args.w_density, args.w_area)

    if two_phase:
        # MACROS FIRST, then single devices. The composite subcircuit
        # blocks (current_mirror/diff_pair) are the big, structurally
        # meaningful pieces -- they establish the floorplan skeleton with
        # the plane to themselves, then the single devices fill in around
        # that skeleton without being able to shove it aside. Phase 2 still
        # charges singles the full cost against the placed composites
        # (overlap, density clearance, HPWL), it just can't move them --
        # see anneal()'s `movable`/`init_pos` docstring.
        print(f"  two-phase: {len(composite)} composite macro(s) placed first, "
              f"then {len(singles)} single device(s) around them "
              f"(--single-phase to anneal everything jointly instead)")
        phase1_pos, phase1_cost = anneal(composite, net_to_macros, sym_groups, min_distances,
                                          args.iters, args.t0, args.seed, *weights, **common,
                                          label="phase 1/2: composite macros")
        pos, final_cost = anneal(macros, net_to_macros, sym_groups, min_distances,
                                  args.iters, args.t0, args.seed + 1, *weights, **common,
                                  movable=singles, init_pos=phase1_pos,
                                  label="phase 2/2: single devices (composites fixed)")
        if args.joint_refine_iters > 0:
            # Optional third pass letting EVERYTHING move again, seeded
            # from the two-phase result. Off by default (0): it can undo
            # the macros-first hierarchy the two phases just established,
            # which is the whole point of running them.
            pos, final_cost = anneal(macros, net_to_macros, sym_groups, min_distances,
                                      args.joint_refine_iters, args.t0, args.seed + 2, *weights, **common,
                                      init_pos=pos, label="phase 3/3: joint refinement (all movable)")
    else:
        if args.two_phase and not (composite and singles):
            print(f"  single-phase: nothing to tier "
                  f"({len(composite)} composite macro(s), {len(singles)} single device(s))")
        phase1_cost = None
        pos, final_cost = anneal(macros, net_to_macros, sym_groups, min_distances, args.iters, args.t0, args.seed,
                                  *weights, **common)

    final_hpwl = hpwl(macro_by_name, net_to_macros, pos)
    final_overlap = total_overlap_area(macros, pos)
    final_sym = symmetry_penalty(macro_by_name, pos, sym_groups)
    final_density_pen, worst_density_shortfall = density_penalty(macros, pos, min_distances)
    final_area = bbox_area(macros, pos)
    density = placement_density(macros, pos)
    per_net = hpwl_by_net(macro_by_name, net_to_macros, pos)

    overlap_ok = final_overlap < 1e-6
    density_ok = worst_density_shortfall < 1e-6
    print(f"\n=== Final placement ===")
    print(f"  cost={final_cost:.2f}  hpwl={final_hpwl:.2f}um  overlap_area={final_overlap:.4f}um^2  "
          f"symmetry_penalty={final_sym:.2f}  density_penalty={final_density_pen:.4f}um "
          f"(worst shortfall={worst_density_shortfall:.4f}um)  bbox_area={final_area:.2f}um^2")
    print(f"  bbox={density['bbox_w_um']:.2f}x{density['bbox_h_um']:.2f}um "
          f"(aspect {density['bbox_aspect']:.2f}:1)  cell_area={density['cell_area_um2']:.2f}um^2  "
          f"utilization={100.0 * density['utilization']:.1f}%")
    print(f"  Overlap check: {'PASS (0 overlap)' if overlap_ok else 'FAIL -- residual overlap, must legalize before use'}")
    print(f"  Density/clearance check: {'PASS (every pair meets its required min_distance)' if density_ok else 'FAIL -- some pair is closer than 10*min_metal_spacing*num_nets, routing congestion risk'}")
    for tier_name, tier in (("composite macros (placed first)", composite),
                             ("single devices", singles)) if two_phase else (("", macros),):
        if tier_name:
            print(f"  -- {tier_name} --")
        for m in tier:
            x, y, rot = pos[m["name"]]
            ew, eh = effective_wh(m, rot)
            print(f"  {m['name']:30s} @ ({x:8.2f}, {y:8.2f})  {ew:6.2f} x {eh:6.2f}"
                  f"{'  [rotated 90]' if rot else '':14s}  "
                  f"num_nets={macro_net_counts[m['name']]}  min_distance={min_distances[m['name']]:.2f}um")

    out_path = Path(args.out).resolve() if args.out else manifest_path.parent.parent / "placement_pos.json"
    result = {
        "design": manifest.get("design"),
        "manifest": str(manifest_path),
        "final_cost": final_cost,
        "final_hpwl_um": final_hpwl,
        "final_overlap_area_um2": final_overlap,
        "overlap_ok": overlap_ok,
        "final_symmetry_penalty": final_sym,
        "final_density_penalty_um": final_density_pen,
        "worst_density_shortfall_um": worst_density_shortfall,
        "density_ok": density_ok,
        "final_bbox_area_um2": final_area,
        # Real area utilization -- NOT the same thing as
        # `final_density_penalty_um`, which is routing-clearance shortfall
        # despite the name. See placement_density().
        "placement_density": density,
        "min_metal_spacing_um": min_metal_spacing,
        "two_phase": bool(two_phase),
        "phase1_composite_macros": [m["name"] for m in composite],
        "phase2_single_devices": [m["name"] for m in singles],
        "phase1_cost": phase1_cost,
        "iters": args.iters, "seed": args.seed,
        "weights": {"w_wire": args.w_wire, "w_ov": args.w_ov, "w_sym": args.w_sym,
                    "w_density": args.w_density, "w_area": args.w_area},
        "positions": {
            name: {
                "x": xyr[0], "y": xyr[1], "rotated": xyr[2],
                **dict(zip(("w", "h"), effective_wh(macro_by_name[name], xyr[2]))),
                # The placed BOX, absolute -- what a grid legalizer wants, and
                # what used to be written out separately as
                # `physical_map.json`'s own `devices` list. Same numbers
                # (`x`/`y` plus the effective post-rotation `w`/`h`), so
                # keeping them in two files only created a way for the two to
                # disagree. `x0_um`/`y0_um` are deliberately the same
                # convention as `x`/`y`: bbox lower-left.
                **dict(zip(("x0_um", "y0_um", "x1_um", "y1_um"),
                           (xyr[0], xyr[1],
                            xyr[0] + effective_wh(macro_by_name[name], xyr[2])[0],
                            xyr[1] + effective_wh(macro_by_name[name], xyr[2])[1]))),
                "num_nets": macro_net_counts[name], "min_distance_um": min_distances[name],
                # Real WORLD-coordinate port locations (net -> [{x, y,
                # layer, width}, ...]) -- the SAME transform + `x`/`y`/`w`/
                # `h` used two lines up, so these match exactly where
                # ../../router/script/route_nets.py's own
                # `world_port_map()` lands wires (same math, kept in sync
                # deliberately, not just coincidentally similar). Empty
                # `{}` for a macro `generate_primitives.py` never found
                # real ports for -- same "no port here" meaning as an
                # absent entry in `world_port_map()`, not a placeholder.
                "ports": ports_to_world(
                    manifest_macro_by_name[name].get("ports"),
                    xyr[0], xyr[1], *effective_wh(macro_by_name[name], xyr[2]), xyr[2]),
            }
            for name, xyr in pos.items()
        },
        "excluded_no_geometry": [m["name"] for m in manifest["macros"]
                                  if m["name"] not in macro_by_name
                                  and (m.get("w") is None or m.get("h") is None)],
    }
    out_path.write_text(json.dumps(result, indent=2))
    print(f"\n  wrote {out_path}")

    summary_path = (Path(args.summary_out).resolve() if args.summary_out
                     else out_path.parent / "placing_summary.txt")
    write_placing_summary(summary_path, result, macros, pos, macro_by_name, net_to_macros,
                           macro_net_counts, min_distances, density, per_net,
                           composite, singles, time.time() - t_start)
    print(f"  wrote {summary_path} (placement performance report)")

    if args.render:
        render_script = Path(__file__).resolve().parent / "render_placement.py"
        viz_path = out_path.parent / "placement_visualization.gds"
        try:
            subprocess.run([sys.executable, str(render_script), str(manifest_path), str(out_path),
                             "--out", str(viz_path)], check=True)
        except subprocess.CalledProcessError as e:
            print(f"  warning: render_placement.py failed ({e}) -- run it manually to get "
                  f"the KLayout-viewable GDS")
    else:
        print("  --no-render passed -- run script/render_placement.py manually for the "
              "KLayout-viewable GDS")

    if not overlap_ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
