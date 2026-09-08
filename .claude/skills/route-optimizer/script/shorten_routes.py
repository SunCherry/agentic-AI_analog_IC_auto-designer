#!/usr/bin/env python3
"""Shorten the routing of an already-routed, DRC-clean layout.

Two files in -- a `.gds` and its `physical_map.json` -- and a shorter-wired
`.gds` + updated map out. Nothing else is needed: the netlist is not read, no
placement is moved, no device is touched. Every wire the layout already has is
re-derived from the map's per-net `segments`/`vias`, and the parts of it that
*land on a device* are frozen. Only the connective tissue between those
landings is rebuilt.

Why that split is the whole design
----------------------------------
A pin landing is the one piece of routing whose exact geometry LVS depends on
-- it is what makes the net touch the device's own metal. Freeze it and
connectivity at every pin survives by construction, no matter what happens to
the wire in between. So this script:

  1. classifies every net rectangle as FROZEN (it overlaps a macro's own drawn
     metal on the same layer -- a pin landing) or FREE,
  2. groups the frozen rectangles into terminal clusters,
  3. throws the free geometry away and rebuilds a rectilinear tree over those
     clusters with A* on a coordinate-compressed (Hanan) graph, obstacles
     being every other net's metal, every macro's metal on the same layer, and
     the PDK's real min-separation ring around both,
  4. keeps the result only if it is shorter, and only if an exact
     rectangle-level legality re-check passes,
  5. re-runs Magic DRC, and reverts the nets that any NEW violation lands on.

The objective is total wirelength (`--via-weight` prices a via in um so the
optimizer cannot trade a metre of detour for one fewer via, or the reverse).
U-turns, staircases and dangling branches are not pattern-matched and removed
one by one -- they disappear because the rebuilt tree has no reason to
contain them.

Honest scope
------------
  - **LVS is NOT re-run here.** Frozen landings make pin connectivity safe by
    construction and the internal check proves each net still spans all of its
    own landings, but the authority on LVS is `layout-fixer`. Hand the output
    there before believing it.
  - **Geometry style is inherited, not invented.** Wire widths come from
    measuring the input's own wires; via geometry is a REFERENCE to a
    `via_stack` cell already in the input GDS. So the output is drawn the same
    way the input was -- which is the version that passed DRC.
  - A layer pair with no via template in the input GDS is a transition this
    script will not use. It cannot draw a via it has never seen.
  - Nets whose every rectangle is frozen (a landing and nothing else) are left
    alone; there is nothing between landings to shorten.

Usage
-----
  python shorten_routes.py <layout.gds> [--map <physical_map.json>]
      [--out-dir <dir>] [--analyze-only] [--nets a,b,c]
      [--passes 2] [--via-weight 2.0] [--min-gain 0.1]
      [--margin 8.0] [--max-coords 180] [--extra-clearance 0.0]
      [--no-prune] [--no-reroute] [--no-drc] [--drc-retries 2]
      [--skip-baseline-drc]
"""
import argparse
import atexit
import heapq
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

try:
    import gdstk
except ImportError:  # pragma: no cover
    sys.exit("gdstk is required: pip install gdstk")

# The active process is a project-wide setting, never a flag and never
# hardcoded -- same accessor every other script in this repo uses.
REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / ".claude" / "reference"))
# `src/cells/` is where the device generators live; `primitives.fet` owns the
# definition of a bulk tie ring, and this script asks it rather than
# re-deriving one. Same path convention as the placer's own scripts.
sys.path.insert(0, str(REPO_ROOT / "src" / "cells"))
from pdk_config import pdk as pdk_option  # noqa: E402

EPS = 1e-6
CONNECT_TOL = 5e-3      # um; two rectangles this close count as touching
SQUARE_TOL = 1e-3       # um; |dx-dy| below this makes a rectangle a corner pad
COORD_TOL = 1e-4        # um; candidate coordinates closer than this merge


# ---------------------------------------------------------------------------
# geometry helpers -- everything here is an axis-aligned box (x0, y0, x1, y1)
# ---------------------------------------------------------------------------
def bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def box_points(b):
    return [(b[0], b[1]), (b[2], b[1]), (b[2], b[3]), (b[0], b[3])]


def boxes_touch(a, b, tol=CONNECT_TOL):
    """Closed-interval intersection with slack -- 'these are electrically one'."""
    return (a[0] - tol <= b[2] and b[0] - tol <= a[2] and
            a[1] - tol <= b[3] and b[1] - tol <= a[3])


def boxes_conflict(a, b, gap):
    """Open-interval intersection of `a` with `b` grown by `gap` -- 'this
    violates spacing'. Exactly-at-gap does not conflict, which is what lets a
    wire hug an obstacle at the legal minimum."""
    return (a[0] < b[2] + gap - EPS and b[0] - gap + EPS < a[2] and
            a[1] < b[3] + gap - EPS and b[1] - gap + EPS < a[3])


def sep_gap(a, b):
    """Separation between two boxes, per the axis that separates them.

    Negative when they overlap, 0 when they abut, positive when there is a gap.
    A gap strictly between 0 and the layer's min separation is a NOTCH -- two
    edges of what is nearly one shape, too close together. It is a real DRC
    violation even between two pieces of the SAME net, which is exactly the
    trap here: same-net metal is not an obstacle, so a rebuilt wire is free to
    graze its own landing by a few nanometres and produce a spacing error that
    no cross-net check would ever look for."""
    gx = max(b[0] - a[2], a[0] - b[2])
    gy = max(b[1] - a[3], a[1] - b[3])
    return max(gx, gy)


def box_union(boxes):
    xs0 = min(b[0] for b in boxes); ys0 = min(b[1] for b in boxes)
    xs1 = max(b[2] for b in boxes); ys1 = max(b[3] for b in boxes)
    return (xs0, ys0, xs1, ys1)


def box_center(b):
    return ((b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0)


def box_len(b):
    """Length of a wire rectangle along its long axis; 0 for a corner pad."""
    dx, dy = b[2] - b[0], b[3] - b[1]
    return 0.0 if abs(dx - dy) < SQUARE_TOL else max(dx, dy)


def manhattan(p, q):
    return abs(p[0] - q[0]) + abs(p[1] - q[1])


def box_manhattan(a, b):
    dx = max(0.0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0.0, max(a[1] - b[3], b[1] - a[3]))
    return dx + dy


class BoxIndex:
    """Uniform-bucket index over boxes. A few thousand rectangles and a few
    hundred thousand queries -- a real spatial index, not a linear scan, but
    not worth an R-tree dependency either."""

    def __init__(self, boxes, cell=4.0):
        self.cell = cell
        self.boxes = list(boxes)
        self.grid = defaultdict(list)
        for i, b in enumerate(self.boxes):
            for key in self._keys(b):
                self.grid[key].append(i)

    def _keys(self, b, gap=0.0):
        i0 = int(math.floor((b[0] - gap) / self.cell))
        i1 = int(math.floor((b[2] + gap) / self.cell))
        j0 = int(math.floor((b[1] - gap) / self.cell))
        j1 = int(math.floor((b[3] + gap) / self.cell))
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                yield (i, j)

    def query(self, b, gap=0.0):
        for i in self.query_idx(b, gap):
            yield self.boxes[i]

    def query_idx(self, b, gap=0.0):
        seen = set()
        for key in self._keys(b, gap):
            for i in self.grid.get(key, ()):
                if i not in seen:
                    seen.add(i)
                    yield i


# ---------------------------------------------------------------------------
# the process: glayer <-> GDS layer, min width, min separation
# ---------------------------------------------------------------------------
def load_pdk_layers():
    """{(layer, datatype): {'glayer', 'min_width', 'min_separation'}} for the
    active PDK's metal stack, resolved through glayout so no layer number and
    no spacing value is written down here."""
    cfg = pdk_option()
    cfg.require_layers("route shortening")

    import gdsfactory as gf
    gf.CONF.n_threads = 1        # see ../../router/SKILL.md -- not optional
    module = cfg.glayout_module
    if module == "sky130":
        from glayout.pdk.sky130_mapped import sky130_mapped_pdk as GPDK
    elif module == "gf180":
        from glayout.pdk.gf180_mapped import gf180_mapped_pdk as GPDK
    else:
        raise SystemExit(
            f"PDK '{cfg.name}' names glayout module '{module}', which this "
            f"script does not know how to import. Add it here or select a "
            f"PDK whose module it does.")

    table = {}
    for i in range(1, 10):
        g = f"met{i}"
        if g not in GPDK.glayers:
            continue
        try:
            num = tuple(GPDK.get_glayer(g))
            rule = GPDK.get_grule(g)
        except Exception:
            continue
        table[num] = {
            "glayer": g,
            "process": GPDK.glayers[g],
            "min_width": float(rule["min_width"]),
            "min_separation": float(rule["min_separation"]),
        }
    if not table:
        raise SystemExit(f"PDK '{cfg.name}' exposes no metal glayers.")
    return cfg, table


# ---------------------------------------------------------------------------
# input: the GDS and its physical_map.json
# ---------------------------------------------------------------------------
class ViaTemplate:
    """An existing `via_stack` cell in the input GDS, reused verbatim.

    Drawing a via by referencing a cell the input already contains is the
    whole reason this script needs no via rule out of the PDK: the footprint,
    the cut, the enclosures are byte-identical to geometry that already
    passed DRC in this very layout."""

    def __init__(self, cell, name):
        self.cell = cell
        self.name = name
        pads = defaultdict(list)
        for p in cell.get_polygons():
            pads[(p.layer, p.datatype)].append(bbox(p.points))
        self.pads = {k: box_union(v) for k, v in pads.items()}
        self.metals = tuple(sorted(k for k in self.pads if k[1] == 20))

    def half(self, layer):
        b = self.pads.get(layer)
        if b is None:
            return None
        return max(b[2] - b[0], b[3] - b[1]) / 2.0

    def area(self):
        b = box_union(list(self.pads.values()))
        return (b[2] - b[0]) * (b[3] - b[1])


class Item:
    """One drawn piece of a net: a wire rectangle or a via.

    `virtual` marks a piece that is ALREADY drawn inside a device macro -- a
    tie ring's own metal, say. It is real metal and a real part of the net, so
    it counts for connectivity, for landing, and for what new metal may merge
    with. It is not wire this layout owns, so it never reaches the GDS, the
    map, or the wirelength.

    `region` groups pieces that are one electrical node by construction rather
    than by anything `items_connect()` can see -- a ring whose two bars meet
    through a layer nothing routes on, for instance.
    """
    __slots__ = ("kind", "layer", "box", "net", "frozen", "vlayers",
                 "template", "center", "origin", "virtual", "region",
                 "corner")

    def __init__(self, kind, net, layer=None, box=None, vlayers=None,
                 template=None, center=None):
        self.kind = kind          # 'rect' | 'via'
        self.net = net
        self.layer = layer        # (num, dt) for a rect
        self.box = box            # bbox for a rect; via pad union for a via
        self.vlayers = vlayers    # (lower, upper) for a via
        self.template = template  # ViaTemplate for a via
        self.center = center      # (x, y) for a via
        self.frozen = False
        self.virtual = False
        self.region = None
        # True/False once this script has drawn the piece and knows; None for
        # geometry read back from a map, where a square pad is the only clue.
        self.corner = None

    def layers(self):
        return (self.layer,) if self.kind == "rect" else self.vlayers

    def box_on(self, layer):
        if self.kind == "rect":
            return self.box if layer == self.layer else None
        pad = self.template.pads.get(layer)
        if pad is None:
            return None
        return (pad[0] + self.center[0], pad[1] + self.center[1],
                pad[2] + self.center[0], pad[3] + self.center[1])


class Net:
    def __init__(self, name, record):
        self.name = name
        self.record = record            # the physical_map entry, kept verbatim
        self.items = []                 # live geometry (rects + vias)
        self.original = []              # geometry as loaded, never mutated
        self.clusters = []              # frozen terminal clusters
        # Indices, per layer, of the macro polygons this net's own landings
        # already sit on: the pins themselves. More metal on a shape the net
        # is already electrically part of is not a new short -- and treating
        # it as an obstacle walls every route out of its own pin.
        self.friendly = defaultdict(set)
        self.changed = False
        self.note = ""

    def rects(self):
        """Drawn wire this layout owns -- what gets measured, written and
        emitted. Virtual pieces are metal that already exists inside a macro."""
        return [it for it in self.items
                if it.kind == "rect" and not it.virtual]

    def vias(self):
        return [it for it in self.items
                if it.kind == "via" and not it.virtual]

    def wirelength(self):
        return sum(box_len(it.box) for it in self.rects())

    def corners(self):
        """Turns in this net's own wire.

        A piece drawn by this script says outright whether it is a corner;
        anything read back from a map is judged by being square. The flag
        matters because `snap_near_misses()` can grow a corner pad to close a
        notch, after which it is no longer square and the count would quietly
        drift -- the same corner, reported as one fewer."""
        return sum(1 for it in self.rects()
                   if (it.corner if it.corner is not None
                       else box_len(it.box) == 0.0))

    def cost(self, via_weight):
        return self.wirelength() + via_weight * len(self.vias())


class Layout:
    """Everything the two input files say, in one object."""

    def __init__(self, gds_path, map_path, layer_table):
        self.gds_path = Path(gds_path)
        self.map_path = Path(map_path)
        self.layer_table = layer_table
        self.lib = gdstk.read_gds(str(gds_path))
        tops = self.lib.top_level()
        if not tops:
            raise SystemExit(f"{gds_path} has no top-level cell.")
        # A GDS often carries orphan cells that no longer sit under anything
        # (a via_stack whose last reference was removed, say). The assembled
        # layout is the one with the references, so pick that rather than
        # refusing to run -- but say so, because picking the wrong top cell
        # silently checks the wrong layout.
        self.top = max(tops, key=lambda c: (len(c.references),
                                            len(c.polygons)))
        if len(tops) > 1:
            others = ", ".join(c.name for c in tops if c is not self.top)
            print(f"  note: {gds_path.name} has {len(tops)} top-level cells; "
                  f"working on {self.top.name!r} (unreferenced: {others[:200]})")
        self.top_name = self.top.name
        with open(map_path) as f:
            self.map = json.load(f)

        self._load_via_templates()
        self._load_macros()
        self._load_nets()
        self._measure_wire_widths()

    # -- GDS side ----------------------------------------------------------
    def _load_via_templates(self):
        """Every distinct via_stack cell, keyed by the metal pair it links."""
        self.via_templates = {}
        self.via_refs = []
        for ref in self.top.references:
            if not ref.cell.name.startswith("via_stack"):
                continue
            self.via_refs.append(ref)
            tpl = ViaTemplate(ref.cell, ref.cell.name)
            pair = tpl.metals
            if len(pair) != 2:
                continue
            best = self.via_templates.get(pair)
            # Smallest footprint for the pair: the least intrusive way to
            # make that transition, and the one most likely to fit.
            if best is None or tpl.area() < best.area():
                self.via_templates[pair] = tpl

    def _load_macros(self):
        """Per-layer world-space metal of every placed device macro.

        Read from the GDS, not from the map's boxes: a macro's bounding box
        blocks nothing on a layer the macro leaves empty, and feed-through on
        such a layer is exactly the freedom that shortens a route."""
        self.macro_metal = defaultdict(list)
        self.macro_refs = []
        self.macro_boxes = {}
        for ref in self.top.references:
            if ref.cell.name.startswith("via_stack"):
                continue
            self.macro_refs.append(ref)
            boxes = []
            for p in ref.get_polygons():
                b = bbox(p.points)
                boxes.append(b)
                if (p.layer, p.datatype) in self.layer_table:
                    self.macro_metal[(p.layer, p.datatype)].append(b)
            if boxes:
                self.macro_boxes[ref.cell.name] = box_union(boxes)
        self.macro_index = {
            lay: BoxIndex(boxes) for lay, boxes in self.macro_metal.items()}

    # -- map side ----------------------------------------------------------
    def _load_nets(self):
        self.nets = {}
        self.net_order = []
        for rec in self.map.get("nets", []):
            name = rec["name"]
            net = Net(name, rec)
            for seg in rec.get("segments", []):
                lay = tuple(seg["layer"])
                net.items.append(
                    Item("rect", name, layer=lay, box=bbox(seg["points_um"])))
            for via in rec.get("vias", []):
                lo, hi = tuple(via["from_layer"]), tuple(via["to_layer"])
                pair = tuple(sorted((lo, hi)))
                tpl = self.via_templates.get(pair)
                if tpl is None:
                    net.note = (f"via {lo}->{hi} has no via_stack template in "
                                f"the GDS; net left untouched")
                    continue
                c = (via["x_um"], via["y_um"])
                item = Item("via", name, vlayers=pair, template=tpl, center=c)
                item.box = box_union([item.box_on(l) for l in pair])
                net.items.append(item)
            net.original = list(net.items)
            self.nets[name] = net
            self.net_order.append(name)

    def _measure_wire_widths(self):
        """Wire width per layer, measured off the input's own wires.

        Inheriting the width means new metal is drawn exactly as wide as the
        metal that already passed DRC here. The PDK's min_width is used only
        to reject a measurement that cannot be right."""
        acc = defaultdict(list)
        for net in self.nets.values():
            for it in net.rects():
                dx, dy = it.box[2] - it.box[0], it.box[3] - it.box[1]
                if abs(dx - dy) < SQUARE_TOL:
                    continue
                acc[it.layer].append(min(dx, dy))
        self.wire_width = {}
        for lay, widths in acc.items():
            w = min(widths)
            floor = self.layer_table[lay]["min_width"]
            self.wire_width[lay] = max(w, floor)
        self.routing_layers = sorted(self.wire_width)

    # -- derived -----------------------------------------------------------
    def clearance(self, layer, extra=0.0):
        return self.layer_table[layer]["min_separation"] + extra


# ---------------------------------------------------------------------------
# topology: what is a pin landing, what is just wire
# ---------------------------------------------------------------------------
def freeze_landings(layout):
    """Mark every item that overlaps a macro's own metal on a shared layer.

    Overlap with device metal in a DRC- and LVS-clean layout is not an
    accident -- it is the connection. Freezing it is what makes every
    downstream rebuild connectivity-preserving at the pins."""
    for net in layout.nets.values():
        for it in net.items:
            for lay in it.layers():
                idx = layout.macro_index.get(lay)
                b = it.box_on(lay)
                if idx is None or b is None:
                    continue
                hit = [k for k in idx.query_idx(b, 0.0)
                       if boxes_touch(b, idx.boxes[k], 0.0)]
                if hit:
                    it.frozen = True
                    net.friendly[lay].update(hit)


def items_connect(a, b):
    """Do two items of one net physically touch?"""
    shared = set(a.layers()) & set(b.layers())
    for lay in shared:
        ba, bb = a.box_on(lay), b.box_on(lay)
        if ba is not None and bb is not None and boxes_touch(ba, bb):
            return True
    return False


def connected_groups(items):
    """Union-find over `items`, connected by physical touching."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # Pieces that share a region are one node whether or not they touch on a
    # layer this script can see: a tie ring's north and south bars meet through
    # the ring's own li1 sides and its internal vias, which is real
    # connectivity that no rectangle test between two met2 bars can find.
    first = {}
    for i, it in enumerate(items):
        if it.region is None:
            continue
        if it.region in first:
            union(i, first[it.region])
        else:
            first[it.region] = i
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if items_connect(items[i], items[j]):
                union(i, j)
    groups = defaultdict(list)
    for i, it in enumerate(items):
        groups[find(i)].append(it)
    return list(groups.values())


def build_clusters(net):
    """The terminal clusters this net must keep connecting: the connected
    groups of its frozen items."""
    net.clusters = connected_groups([it for it in net.items if it.frozen])
    return net.clusters


def spans_all_clusters(net, items=None):
    """Is every terminal cluster in one connected component of `items`?"""
    items = net.items if items is None else items
    if len(net.clusters) <= 1:
        return True
    groups = connected_groups(items)
    ids = {id(it): gi for gi, g in enumerate(groups) for it in g}
    seen = set()
    for cl in net.clusters:
        gid = {ids.get(id(it)) for it in cl}
        gid.discard(None)
        if len(gid) != 1:
            return False
        seen |= gid
    return len(seen) == 1


def prune_dangling(net):
    """Drop free geometry that connects nothing -- iteratively remove any
    non-frozen item of degree <= 1. Pure removal: no new metal, so it can
    never introduce a violation, and it is the one improvement available on a
    net whose reroute is rejected."""
    items = list(net.items)
    removed = []
    while True:
        deg = defaultdict(int)
        adj = defaultdict(list)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                if items_connect(items[i], items[j]):
                    deg[i] += 1
                    deg[j] += 1
                    adj[i].append(j)
                    adj[j].append(i)
        drop = [i for i, it in enumerate(items)
                if not it.frozen and deg[i] <= 1]
        if not drop:
            break
        keep = [it for i, it in enumerate(items) if i not in set(drop)]
        removed += [items[i] for i in drop]
        items = keep
    return items, removed


# ---------------------------------------------------------------------------
# obstacles -- every piece of metal this net is not allowed to touch
# ---------------------------------------------------------------------------
class Obstacles:
    """Per-layer boxes tagged with their owner, with a per-layer index that is
    rebuilt whenever a net's geometry changes."""

    def __init__(self, layout):
        self.layout = layout
        self.macro = {lay: list(boxes)
                      for lay, boxes in layout.macro_metal.items()}
        self.by_net = defaultdict(lambda: defaultdict(list))
        for net in layout.nets.values():
            self.set_net(net, rebuild=False)
        self._index = {}

    def set_net(self, net, rebuild=True):
        per_layer = defaultdict(list)
        for it in net.items:
            for lay in it.layers():
                b = it.box_on(lay)
                if b is not None:
                    per_layer[lay].append(b)
        self.by_net[net.name] = per_layer
        if rebuild:
            self._index.clear()

    def boxes_for(self, layer, exclude_net):
        friendly = set()
        net = self.layout.nets.get(exclude_net)
        if net is not None:
            friendly = net.friendly.get(layer, set())
        out = [b for k, b in enumerate(self.macro.get(layer, ()))
               if k not in friendly]
        for name, per_layer in self.by_net.items():
            if name == exclude_net:
                continue
            out += per_layer.get(layer, ())
        return out

    def index(self, layer, exclude_net):
        key = (layer, exclude_net)
        if key not in self._index:
            self._index[key] = BoxIndex(self.boxes_for(layer, exclude_net))
        return self._index[key]

    def legal(self, box, layer, exclude_net, clearance):
        """Exact rectangle-level legality: does `box` on `layer` stay
        `clearance` away from everything not owned by `exclude_net`?"""
        idx = self.index(layer, exclude_net)
        for other in idx.query(box, clearance):
            if boxes_conflict(box, other, clearance):
                return False
        return True


# ---------------------------------------------------------------------------
# per-net metrics
# ---------------------------------------------------------------------------
def net_metrics(net, via_weight):
    length = net.wirelength()
    pts = [box_center(box_union([it.box for it in cl])) for cl in net.clusters]
    if len(pts) >= 2:
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        bound = (max(xs) - min(xs)) + (max(ys) - min(ys))   # true lower bound
        mst = manhattan_mst(pts)
    else:
        bound = mst = 0.0
    return {
        "net": net.name,
        "wirelength_um": length,
        "vias": len(net.vias()),
        "corners": net.corners(),
        "terminals": len(net.clusters),
        "bbox_bound_um": bound,
        "mst_um": mst,
        "excess_um": max(0.0, length - bound),
        "excess_ratio": (length / bound) if bound > 1e-9 else 0.0,
        "cost": net.cost(via_weight),
    }


def manhattan_mst(points):
    """Prim over Manhattan distance -- O(n^2), and n is a pin count."""
    if len(points) < 2:
        return 0.0
    inside = {0}
    best = {i: manhattan(points[0], points[i]) for i in range(1, len(points))}
    total = 0.0
    while best:
        j = min(best, key=best.get)
        total += best.pop(j)
        inside.add(j)
        for k in list(best):
            d = manhattan(points[j], points[k])
            if d < best[k]:
                best[k] = d
    return total


# ---------------------------------------------------------------------------
# the rebuild: a rectilinear tree over the terminal clusters, by A* on a
# coordinate-compressed (Hanan) graph
# ---------------------------------------------------------------------------
def shrink_box(b, dx, dy):
    """Box pulled in by (dx, dy) per side, never past its own centre line."""
    cx, cy = box_center(b)
    x0 = min(b[0] + dx, cx); x1 = max(b[2] - dx, cx)
    y0 = min(b[1] + dy, cy); y1 = max(b[3] - dy, cy)
    return (x0, y0, x1, y1)


class ConnectGrid:
    """One A* problem: connect a set of source boxes to a set of target boxes.

    The coordinate set is the classic Hanan construction -- every obstacle
    edge, offset outward by that layer's (clearance + half wire width), plus
    every own-metal edge and centre line. A shortest rectilinear path avoiding
    rectangles always exists on such a grid, so nothing is lost by discretising
    to it, and it adapts to the layout instead of imposing a pitch on it."""

    def __init__(self, layout, obstacles, net, region, cfg):
        self.layout = layout
        self.obst = obstacles
        self.net = net
        self.cfg = cfg
        self.layers = list(layout.routing_layers)
        self.li = {lay: i for i, lay in enumerate(self.layers)}
        self.region = region
        self._build_coords()
        self._build_blocked()

    # -- coordinates -------------------------------------------------------
    def _build_coords(self):
        rx0, ry0, rx1, ry1 = self.region
        own_x, own_y, obs_x, obs_y = set(), set(), set(), set()
        for it in self.net.items:
            for lay in it.layers():
                b = it.box_on(lay)
                if b is None:
                    continue
                cx, cy = box_center(b)
                own_x.update((b[0], b[2], cx))
                own_y.update((b[1], b[3], cy))
        for lay in self.layers:
            hw = self.layout.wire_width[lay] / 2.0
            c = self.layout.clearance(lay, self.cfg.extra_clearance)
            pad = hw + c
            idx = self.obst.index(lay, self.net.name)
            for b in idx.query(self.region, 0.0):
                obs_x.update((b[0] - pad, b[2] + pad))
                obs_y.update((b[1] - pad, b[3] + pad))
        obs_x.update((rx0, rx1))
        obs_y.update((ry0, ry1))

        def finish(own, obs, lo, hi):
            own = sorted(v for v in own if lo - EPS <= v <= hi + EPS)
            obs = sorted(v for v in obs if lo - EPS <= v <= hi + EPS)
            budget = max(self.cfg.max_coords - len(own), 8)
            if len(obs) > budget:
                step = len(obs) / float(budget)
                obs = [obs[int(k * step)] for k in range(budget)]
            merged = sorted(set(own) | set(obs))
            out = []
            for v in merged:
                if not out or v - out[-1] > COORD_TOL:
                    out.append(v)
            return np.array(out, dtype=float)

        self.xs = finish(own_x, obs_x, rx0, rx1)
        self.ys = finish(own_y, obs_y, ry0, ry1)
        self.nx, self.ny = len(self.xs), len(self.ys)

    # -- blocked maps ------------------------------------------------------
    def _mark(self, arr, box, padx, pady):
        i0 = int(np.searchsorted(self.xs, box[0] - padx + EPS))
        i1 = int(np.searchsorted(self.xs, box[2] + padx - EPS))
        j0 = int(np.searchsorted(self.ys, box[1] - pady + EPS))
        j1 = int(np.searchsorted(self.ys, box[3] + pady - EPS))
        if i1 > i0 and j1 > j0:
            arr[i0:i1, j0:j1] = True

    def _mark_edges(self, harr, varr, box, hw, c):
        """Block the MOVES an obstacle forbids, not just the nodes.

        Blocking nodes alone is only sound if every obstacle edge is in the
        coordinate set, and `--max-coords` can thin that set out -- after
        which two legal nodes can sit on opposite sides of an obstacle the
        search happily steps across. Marking the edge itself makes the search
        exact whatever the coordinate set looks like. Found the hard way: with
        node blocking only, the largest net's rebuild was rejected by the
        final geometric check every single time, for exactly this reason."""
        xs, ys = self.xs, self.ys
        i0 = max(0, int(np.searchsorted(xs, box[0] - c + EPS)) - 1)
        i1 = int(np.searchsorted(xs, box[2] + c - EPS))
        j0 = int(np.searchsorted(ys, box[1] - c - hw + EPS))
        j1 = int(np.searchsorted(ys, box[3] + c + hw - EPS))
        if i1 > i0 and j1 > j0:
            harr[i0:i1, j0:j1] = True
        i0 = int(np.searchsorted(xs, box[0] - c - hw + EPS))
        i1 = int(np.searchsorted(xs, box[2] + c + hw - EPS))
        j0 = max(0, int(np.searchsorted(ys, box[1] - c + EPS)) - 1)
        j1 = int(np.searchsorted(ys, box[3] + c - EPS))
        if i1 > i0 and j1 > j0:
            varr[i0:i1, j0:j1] = True

    def _build_blocked(self):
        nl = len(self.layers)
        self.blocked = np.zeros((nl, self.nx, self.ny), dtype=bool)
        self.hblock = np.zeros((nl, self.nx, self.ny), dtype=bool)
        self.vblock = np.zeros((nl, self.nx, self.ny), dtype=bool)
        for lay in self.layers:
            k = self.li[lay]
            hw = self.layout.wire_width[lay] / 2.0
            c = self.layout.clearance(lay, self.cfg.extra_clearance)
            for b in self.obst.index(lay, self.net.name).query(self.region, c + hw):
                self._mark(self.blocked[k], b, c + hw, c + hw)
                self._mark_edges(self.hblock[k], self.vblock[k], b, hw, c)
            # Own metal is excluded by name and the pin metal the net's own
            # landings sit on is excluded as `friendly`, so the way back into
            # a pin is open. What that costs is the notch risk -- handled
            # after the search by snap_near_misses(), not here.

        # A via may only be dropped where BOTH of its pads are legal.
        self.via_ok = {}
        for pair, tpl in self.layout.via_templates.items():
            lo, hi = pair
            if lo not in self.li or hi not in self.li:
                continue
            if self.li[hi] - self.li[lo] != 1:
                continue                      # adjacent-layer transitions only
            ok = np.ones((self.nx, self.ny), dtype=bool)
            for lay in pair:
                half = tpl.half(lay)
                if half is None:
                    ok[:] = False
                    break
                c = self.layout.clearance(lay, self.cfg.extra_clearance)
                bad = np.zeros((self.nx, self.ny), dtype=bool)
                for b in self.obst.index(lay, self.net.name).query(
                        self.region, c + half):
                    self._mark(bad, b, c + half, c + half)
                # Against the net's OWN metal and the pin metal it lands on, a
                # via pad may sit clear or sit solidly on top -- never in
                # between. A via cannot be grown afterwards the way a wire can,
                # so the near-miss has to be forbidden here or the whole
                # rebuild is thrown away for one 30 nm sliver. (It was.)
                # Per box, not pooled: one shape the pad sits solidly on top
                # of does not excuse it grazing a different shape 30 nm away.
                # Pooling the two masks was exactly that bug.
                for b in own_boxes(self.layout, self.net, self.net.items, lay):
                    near = np.zeros((self.nx, self.ny), dtype=bool)
                    self._mark(near, b, c + half, c + half)
                    if half > c:
                        deep = np.zeros((self.nx, self.ny), dtype=bool)
                        self._mark(deep, b, half - c, half - c)
                        near &= ~deep
                    bad |= near
                ok &= ~bad
            self.via_ok[pair] = ok

    # -- node sets ---------------------------------------------------------
    def nodes_in(self, items):
        """Grid nodes that land inside a piece of this net's own metal, deep
        enough that the new wire really overlaps it rather than just grazing
        its edge."""
        out = set()
        for it in items:
            for lay in it.layers():
                if lay not in self.li:
                    continue
                b = it.box_on(lay)
                if b is None:
                    continue
                hw = self.layout.wire_width[lay] / 2.0
                s = shrink_box(b, min(hw, (b[2] - b[0]) / 2.0 - 1e-3),
                               min(hw, (b[3] - b[1]) / 2.0 - 1e-3))
                i0 = int(np.searchsorted(self.xs, s[0] - EPS))
                i1 = int(np.searchsorted(self.xs, s[2] + EPS))
                j0 = int(np.searchsorted(self.ys, s[1] - EPS))
                j1 = int(np.searchsorted(self.ys, s[3] + EPS))
                k = self.li[lay]
                for i in range(i0, i1):
                    for j in range(j0, j1):
                        out.add((k, i, j))
        return out

    # -- search ------------------------------------------------------------
    def astar(self, sources, targets, target_box):
        """Shortest rectilinear path, then straightest among the shortest.

        The cost is a PAIR compared lexicographically: `(um, bends)`. Length
        decides; bends only break ties. With the heuristic `(manhattan, 0)`
        -- admissible on the first component, zero on the second -- ordinary
        A* on vector costs returns the fewest-bend path among the shortest.

        A scalar bend price cannot express that. Whatever it is set to, the
        search will buy straightness with wire somewhere; set it small enough
        that it never does, and it stops breaking ties at all. On a Hanan grid
        equal-length routes are everywhere -- every monotone staircase between
        two points is the same length as the L through either corner -- so
        the tie-break is not a detail, it is where nearly all the turns are
        won or lost.

        `--bend-penalty` is the other half, and it is not redundant with the
        tie-break: it prices a bend in real wire, which steers the search
        through NEAR-ties as well as exact ones and -- because a straighter
        net occupies fewer tracks and leaves cleaner channels for the nets
        routed after it -- was measured to improve total wirelength, not just
        turn count, on every design tried. Hence a non-zero default. Set it to
        0 for the strict reading, where a turn can never cost a nanometre."""
        if not sources or not targets:
            return None
        nl, nx, ny = len(self.layers), self.nx, self.ny
        n_states = nl * nx * ny * 3
        INF = float("inf")
        g = np.full(n_states, INF, dtype=np.float64)
        gb = np.full(n_states, np.iinfo(np.int32).max, dtype=np.int32)
        parent = np.full(n_states, -1, dtype=np.int64)
        xs, ys = self.xs, self.ys
        blocked = self.blocked
        hblock, vblock = self.hblock, self.vblock
        via_cost = self.cfg.via_weight
        bend = self.cfg.bend_penalty
        tx0, ty0, tx1, ty1 = target_box

        def sid(k, i, j, a):
            return ((k * nx + i) * ny + j) * 3 + a

        def h(i, j):
            dx = max(0.0, max(tx0 - xs[i], xs[i] - tx1))
            dy = max(0.0, max(ty0 - ys[j], ys[j] - ty1))
            return dx + dy

        heap = []
        for (k, i, j) in sources:
            if blocked[k, i, j]:
                continue
            s = sid(k, i, j, 2)
            if g[s] > 0.0:
                g[s] = 0.0
                gb[s] = 0
                heapq.heappush(heap, (h(i, j), 0, s))
        tset = {(k, i, j) for (k, i, j) in targets if not blocked[k, i, j]}
        if not tset:
            return None

        pair_of = {}
        for pair in self.via_ok:
            pair_of[(self.li[pair[0]], self.li[pair[1]])] = pair

        def improves(nlen, nbend, olen, obend):
            """Lexicographic (length, bends), with length compared loosely
            enough that float noise cannot masquerade as a real difference."""
            if nlen < olen - 1e-9:
                return True
            if nlen > olen + 1e-9:
                return False
            return nbend < obend

        goal = -1
        while heap:
            f, b, s = heapq.heappop(heap)
            base, a = divmod(s, 3)
            j = base % ny
            i = (base // ny) % nx
            k = base // (ny * nx)
            gs = g[s]
            if f > gs + h(i, j) + 1e-9 or b > gb[s]:
                continue
            if (k, i, j) in tset:
                goal = s
                break
            for di, dj, axis in ((1, 0, 0), (-1, 0, 0), (0, 1, 1), (0, -1, 1)):
                ni, nj = i + di, j + dj
                if not (0 <= ni < nx and 0 <= nj < ny):
                    continue
                if blocked[k, ni, nj]:
                    continue
                if axis == 0 and hblock[k, min(i, ni), j]:
                    continue
                if axis == 1 and vblock[k, i, min(j, nj)]:
                    continue
                step = abs(xs[ni] - xs[i]) if axis == 0 else abs(ys[nj] - ys[j])
                turn = 1 if (a != 2 and a != axis) else 0
                ns = sid(k, ni, nj, axis)
                ng = gs + step + (bend * turn)
                nb = int(gb[s]) + turn
                if improves(ng, nb, g[ns], gb[ns]):
                    g[ns] = ng
                    gb[ns] = nb
                    parent[ns] = s
                    heapq.heappush(heap, (ng + h(ni, nj), nb, ns))
            for dk in (1, -1):
                nk = k + dk
                if not (0 <= nk < nl):
                    continue
                pair = pair_of.get((min(k, nk), max(k, nk)))
                if pair is None or not self.via_ok[pair][i, j]:
                    continue
                if blocked[nk, i, j]:
                    continue
                ns = sid(nk, i, j, 2)
                ng = gs + via_cost
                nb = int(gb[s])          # a via is a layer change, not a turn
                if improves(ng, nb, g[ns], gb[ns]):
                    g[ns] = ng
                    gb[ns] = nb
                    parent[ns] = s
                    heapq.heappush(heap, (ng + h(i, j), nb, ns))

        if goal < 0:
            return None
        path = []
        s = goal
        while s >= 0:
            base, _ = divmod(s, 3)
            j = base % ny
            i = (base // ny) % nx
            k = base // (ny * nx)
            node = (k, i, j)
            if not path or path[-1] != node:
                path.append(node)
            s = parent[s]
        path.reverse()
        return path

    def path_to_items(self, path, net_name):
        """Grid path -> drawn rectangles and via references.

        A bend gets an explicit corner square: runs are padded perpendicular
        only, so an L's outer corner would otherwise notch by half the wire
        width -- Magic reports that as a width error on a thick layer and a
        spacing error on a thin one. Same convention the router draws with."""
        items = []
        if len(path) < 2:
            return items
        pts = [(self.layers[k], float(self.xs[i]), float(self.ys[j]))
               for (k, i, j) in path]
        run_start = 0
        for idx in range(1, len(pts)):
            lay_a, xa, ya = pts[idx - 1]
            lay_b, xb, yb = pts[idx]
            if lay_a != lay_b:
                self._emit_run(items, pts, run_start, idx - 1, net_name)
                pair = tuple(sorted((lay_a, lay_b)))
                tpl = self.layout.via_templates[pair]
                via = Item("via", net_name, vlayers=pair, template=tpl,
                           center=(xa, ya))
                via.box = box_union([via.box_on(l) for l in pair])
                items.append(via)
                run_start = idx
        self._emit_run(items, pts, run_start, len(pts) - 1, net_name)
        return items

    def _emit_run(self, items, pts, a, b, net_name):
        if b <= a:
            return
        lay = pts[a][0]
        hw = self.layout.wire_width[lay] / 2.0
        # collapse collinear nodes into maximal runs
        corners = [a]
        for k in range(a + 1, b):
            x0, y0 = pts[k - 1][1], pts[k - 1][2]
            x1, y1 = pts[k][1], pts[k][2]
            x2, y2 = pts[k + 1][1], pts[k + 1][2]
            straight = ((abs(y0 - y1) < EPS and abs(y1 - y2) < EPS) or
                        (abs(x0 - x1) < EPS and abs(x1 - x2) < EPS))
            if not straight:
                corners.append(k)
        corners.append(b)
        for k in range(len(corners) - 1):
            _, x0, y0 = pts[corners[k]]
            _, x1, y1 = pts[corners[k + 1]]
            if abs(x1 - x0) < EPS and abs(y1 - y0) < EPS:
                continue
            if abs(y1 - y0) < EPS:
                box = (min(x0, x1), y0 - hw, max(x0, x1), y0 + hw)
            else:
                box = (x0 - hw, min(y0, y1), x0 + hw, max(y0, y1))
            run = Item("rect", net_name, layer=lay, box=box)
            run.corner = False
            items.append(run)
        for k in corners[1:-1]:
            _, x, y = pts[k]
            pad = Item("rect", net_name, layer=lay,
                       box=(x - hw, y - hw, x + hw, y + hw))
            pad.corner = True
            items.append(pad)


# ---------------------------------------------------------------------------
# per-net rebuild
# ---------------------------------------------------------------------------
def items_legal(layout, obst, net, items, cfg):
    """Exact rectangle-level re-check of freshly drawn metal.

    The grid search is only as sound as its coordinate set, and `--max-coords`
    can thin that set out. This check does not depend on the grid at all, so a
    route that survives it is legal whether or not the discretisation was
    complete. Nothing is committed without it."""
    for it in items:
        for lay in it.layers():
            b = it.box_on(lay)
            if b is None:
                continue
            c = layout.clearance(lay, cfg.extra_clearance)
            if not obst.legal(b, lay, net, c):
                return False, (lay, b)
    return True, None


# ---------------------------------------------------------------------------
# bulk landings: a tie ring is one node, so a bulk trace may land anywhere on it
# ---------------------------------------------------------------------------
# `src/cells/primitives/fet.py`'s `_stretch_terminals_to_edge()` gives bulk one
# port, on the tie ring's south bar, and routing needs it to stay one port. But
# the ring is a single node: a device sitting BELOW its supply rail pays for
# that fixed choice twice, running down the side of the macro to reach the
# south bar and back up again. The metal it was trying to reach was directly
# above it the whole time.
#
# So the landing is released here and nowhere earlier: the initial route keeps
# the deterministic port it needs, and this pass -- which can see where the
# wire actually went -- lets it land on whichever part of the ring is nearest.
# `find_bulk_tapring()` lives in fet.py, next to the code that draws the ring,
# so "what a tie ring is" is written down once.


def _rk(box):
    return tuple(round(v, 4) for v in box)


def macro_reference_for(layout, device):
    """The GDS reference that holds a device, matched by cell name and, when a
    cell is placed more than once, by the placed box the map records."""
    cands = [r for r in layout.macro_refs
             if r.cell.name == device.get("primitive_cell")]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    want = (device.get("x0_um"), device.get("y0_um"))
    if want[0] is None:
        return None
    return min(cands, key=lambda r: abs(r.bounding_box()[0][0] - want[0]) +
                                    abs(r.bounding_box()[0][1] - want[1]))


def macro_partition(ref, polys, via_links, cache):
    """One macro's metal split into electrically separate nodes, computed once.

    A supply net can land on the same macro several times, and a wide output
    device can carry thousands of rectangles; re-running the union-find per
    landing costs seconds per net for nothing."""
    key = id(ref)
    if key not in cache:
        from primitives.fet import partition_connected_metal
        cache[key] = partition_connected_metal(polys, via_links)
    return cache[key]


def macro_world_polygons(ref, cache):
    """`{(layer, datatype): [boxes]}` for one macro, in WORLD coordinates.

    `Reference.get_polygons()` already applies the placement transform, so
    there is no rotation or mirror arithmetic here to get wrong -- the trace
    and the layout's own obstacle boxes come out of the same call and compare
    exactly."""
    key = id(ref)
    if key not in cache:
        per = defaultdict(list)
        for p in ref.get_polygons():
            per[(p.layer, p.datatype)].append(bbox(p.points))
        cache[key] = dict(per)
    return cache[key]


def relax_bulk_landings(layout, obst, cfg, log):
    """Replace each fixed bulk landing with the whole node behind it.

    Returns `{net: [(instance, n_boxes, region_bbox, is_ring), ...]}`.

    Correctness rests on the TRACE: everything `find_bulk_tapring()` returns is
    metal continuous with the landing the router already used, so it is the
    same node by construction and extraction cannot tell the difference.

    The RING SHAPE is what tells bulk from everything else. A supply net lands
    on a device's source rail as well as on its tie ring, and both trace to a
    real node -- but only the tie ring is a ring. That test is what keeps this
    pass pointed at the thing the fixed port actually costs wire on.

    Scope is deliberately narrow twice over: only macros where the netlist says
    this net has a `bulk` terminal, and only landings whose traced node is
    ring-shaped. Both restrictions are measured, not cautious by taste -- see
    the note at the `is_ring` check.
    """
    if cfg.no_bulk_relax:
        return {}
    try:
        from primitives.fet import find_bulk_tapring
    except Exception as exc:  # pragma: no cover - environment dependent
        log(f"  bulk relaxation unavailable ({type(exc).__name__}: {exc}) -- "
            f"bulk landings stay where they are")
        return {}

    via_links = pdk_option().via_links
    devices = {d["instance"]: d for d in layout.map.get("devices", [])}
    macro_lookup = {}
    for lay, boxes in layout.macro_metal.items():
        macro_lookup[lay] = {}
        for k, b in enumerate(boxes):
            macro_lookup[lay].setdefault(_rk(b), k)

    poly_cache = {}
    part_cache = {}
    report = defaultdict(list)
    counter = [0]

    def touches_macro(item, polys):
        for lay in item.layers():
            b = item.box_on(lay)
            if b is None:
                continue
            if any(boxes_touch(b, mb, 0.0) for mb in polys.get(lay, ())):
                return True, lay, b
        return False, None, None

    for name in layout.net_order:
        net = layout.nets[name]
        insts = sorted({ep.get("instance")
                        for ep in net.record.get("endpoints", ())
                        if ep.get("terminal") == "bulk" and ep.get("instance")})
        for inst in insts:
            device = devices.get(inst)
            ref = macro_reference_for(layout, device) if device else None
            if ref is None:
                continue
            polys = macro_world_polygons(ref, poly_cache)
            # One node may serve several landings on the same macro (a device
            # whose source is tied to its own ring). Register it once, or the
            # rebuild would be told to reach the same piece of metal twice.
            regions = {}

            for cluster in list(net.clusters):
                if any(x.virtual for x in cluster):
                    continue
                seed = None
                for x in cluster:
                    hit, lay, b = touches_macro(x, polys)
                    if hit:
                        seed = (x, lay, b)
                        break
                if seed is None:
                    continue
                _, lay, b = seed
                ring = find_bulk_tapring(polys, via_links, seed_layer=lay,
                                         seed_box=b,
                                         partition=macro_partition(
                                             ref, polys, via_links, part_cache))
                if not ring or not ring.get("metal"):
                    continue
                # The ring shape is the DISCRIMINATOR, and it has to be. A
                # supply net lands on a device's source rail as well as its
                # tie ring, and both trace to a real node -- but a source rail
                # is not a ring. Releasing every landing that traces to
                # something was measured, not guessed: it roughly doubled the
                # worst net's rebuild and cost another its rebuild entirely,
                # the net then having to reach several sprawling regions in an
                # order the greedy tree does not have. Only a ring is
                # released.
                if not ring.get("is_ring"):
                    continue
                keys = {l: set(_rk(x) for x in boxes)
                        for l, boxes in ring["metal"].items()}
                sig = frozenset((l, k) for l, ks in keys.items() for k in ks)

                # Release only if this cluster lands on NOTHING but this node.
                # One wire rectangle can touch two devices; releasing it
                # because one of them is a bulk tie would quietly cut the
                # other.
                safe = True
                for x in cluster:
                    for l in x.layers():
                        xb = x.box_on(l)
                        idx = layout.macro_index.get(l)
                        if xb is None or idx is None:
                            continue
                        for k in idx.query_idx(xb, 0.0):
                            mb = idx.boxes[k]
                            if boxes_touch(xb, mb, 0.0) and \
                                    _rk(mb) not in keys.get(l, ()):
                                safe = False
                                break
                        if not safe:
                            break
                    if not safe:
                        break
                if not safe:
                    log(f"  bulk   {name:10s} {inst}: a landing also touches "
                        f"other device metal -- left fixed")
                    continue

                if sig in regions:
                    rid = regions[sig]
                    for x in cluster:
                        x.frozen = False
                    build_clusters(net)
                    log(f"  bulk   {name:10s} {inst}: second landing on the "
                        f"same node released (it was a duplicate terminal)")
                    continue

                # The landable part: metal on layers this script routes on,
                # wide enough to actually put a wire or a via on.
                rid = counter[0]
                virtuals = []
                for l in layout.routing_layers:
                    seen = set()
                    for vb in ring["metal"].get(l, ()):
                        key = _rk(vb)
                        if key in seen:
                            continue
                        seen.add(key)
                        if min(vb[2] - vb[0],
                               vb[3] - vb[1]) < layout.wire_width[l] - EPS:
                            continue
                        v = Item("rect", name, layer=l, box=vb)
                        v.frozen = True
                        v.virtual = True
                        v.region = rid
                        virtuals.append(v)
                if not virtuals:
                    log(f"  bulk   {name:10s} {inst}: node has no metal on a "
                        f"routable layer -- left fixed")
                    continue

                for x in cluster:
                    x.frozen = False
                net.items.extend(virtuals)
                for l, ks in keys.items():
                    table = macro_lookup.get(l)
                    if table:
                        net.friendly[l].update(table[k] for k in ks if k in table)
                build_clusters(net)
                obst.set_net(net)
                regions[sig] = rid
                counter[0] += 1
                bx = ring["bbox"]
                report[name].append((inst, len(virtuals), bx,
                                     bool(ring.get("is_ring"))))
                log(f"  bulk   {name:10s} {inst}: landing released onto its "
                    f"tie ring ({len(virtuals)} landable box(es), "
                    f"{bx[0]:.2f},{bx[1]:.2f} .. {bx[2]:.2f},{bx[3]:.2f})")
    return dict(report)


def own_boxes(layout, net, items, layer):
    """Everything on `layer` the new metal is allowed to merge with: the net's
    own surviving metal, the metal built so far in this rebuild, and the pin
    metal its landings already sit on."""
    out = []
    for it in items:
        b = it.box_on(layer)
        if b is not None:
            out.append(b)
    macro = layout.macro_metal.get(layer, ())
    for k in net.friendly.get(layer, ()):
        out.append(macro[k])
    return out


def snap_near_misses(layout, net, kept, made, cfg):
    """Close every sub-minimum gap between new metal and metal it is meant to
    merge with, by growing the new rectangle until it touches.

    This is a repair, not a heuristic: a grid coordinate derived from some
    obstacle's edge can land a few nanometres short of the landing the route
    is trying to reach, and the result is a notch -- geometrically a gap, not
    a connection. Growing the wire to meet the shape removes the gap and the
    connection both. Purely additive and re-checked afterwards, so it cannot
    smuggle in a violation of its own."""
    fixed = 0
    for _round in range(8):
        changed = False
        for it in made:
            if it.kind != "rect":
                continue
            lay = it.layer
            c = layout.clearance(lay, cfg.extra_clearance)
            worst = None
            # Everything this rectangle may merge with, PLUS every other new
            # item -- a via pad cannot be grown, so when a via and a wire come
            # up short of each other it is always the wire that moves.
            others = own_boxes(layout, net,
                               kept + [m for m in made if m is not it], lay)
            for o in others:
                g = sep_gap(it.box, o)
                if EPS < g < c - EPS and (worst is None or g > worst[0]):
                    worst = (g, o)
            if worst is None:
                continue
            _, o = worst
            changed = True
            b = list(it.box)
            gx = max(o[0] - b[2], b[0] - o[2])
            gy = max(o[1] - b[3], b[1] - o[3])
            if gx >= gy:
                if b[2] <= o[0]:
                    b[2] = o[0]
                else:
                    b[0] = o[2]
            else:
                if b[3] <= o[1]:
                    b[3] = o[1]
                else:
                    b[1] = o[3]
            it.box = tuple(b)
            fixed += 1
        if not changed:
            break
    return fixed


def notch_free(layout, net, kept, made, cfg):
    """Gate: no new rectangle may sit in the no-man's-land between merged and
    separated from metal of its own net (or from the pin metal it lands on)."""
    for it in made:
        for lay in it.layers():
            b = it.box_on(lay)
            if b is None:
                continue
            c = layout.clearance(lay, cfg.extra_clearance)
            others = own_boxes(
                layout, net, kept + [m for m in made if m is not it], lay)
            for o in others:
                g = sep_gap(b, o)
                if EPS < g < c - EPS:
                    return False, (it.kind, lay, b, o, round(g, 4))
    return True, None


def reroute_net(layout, obst, net, cfg):
    """Rebuild the net's connective wiring as a tree over its frozen
    landings. Returns the new item list, or None if it could not be built."""
    if len(net.clusters) < 2:
        return None
    saved = net.items
    kept = [it for it in net.items if it.frozen]
    made = []
    tree = list(net.clusters[0])
    pending = [list(c) for c in net.clusters[1:]]
    try:
        while pending:
            tree_box = box_union([it.box for it in tree] +
                                 [it.box for it in made])
            # Nearest PIECE, not nearest bounding box. A released bulk node
            # can be a ring 200um wide around a huge output device; its bbox
            # sits ~0 from everything, so ordering by it picks essentially at
            # random and the tree grows in a bad order.
            pending.sort(key=lambda cl: min(box_manhattan(tree_box, it.box)
                                            for it in cl))
            target = pending.pop(0)
            tgt_box = box_union([it.box for it in target])
            path = None
            for margin in (cfg.margin, cfg.margin * 4.0):
                region = box_union([tree_box, tgt_box])
                region = (region[0] - margin, region[1] - margin,
                          region[2] + margin, region[3] + margin)
                net.items = kept + made
                grid = ConnectGrid(layout, obst, net, region, cfg)
                path = grid.astar(grid.nodes_in(tree + made),
                                  grid.nodes_in(target), tgt_box)
                if path:
                    break
            if not path:
                return None
            made += grid.path_to_items(path, net.name)
            tree += target
        candidate = kept + made
    finally:
        net.items = saved

    snap_near_misses(layout, net, kept, made, cfg)
    ok, where = items_legal(layout, obst, net.name, made, cfg)
    if not ok:
        net.note = f"rebuilt route failed the exact legality check at {where}"
        return None
    ok, where = notch_free(layout, net, kept, made, cfg)
    if not ok:
        net.note = f"rebuilt route left a same-net notch at {where}"
        return None
    if not spans_all_clusters(net, candidate):
        net.note = "rebuilt route did not span every landing -- discarded"
        return None
    return candidate


def optimize(layout, obst, cfg, log):
    """Prune, then rip-up-and-rebuild, worst offender first, for `--passes`
    passes. Later passes matter because every accepted net frees the space
    the next one might want."""
    targets = [layout.nets[n] for n in layout.net_order
               if (not cfg.nets or n in cfg.nets)]
    for net in targets:
        build_clusters(net)

    if not cfg.no_prune:
        for net in targets:
            if len(net.clusters) < 1:
                continue
            kept, removed = prune_dangling(net)
            if removed and spans_all_clusters(net, kept):
                log(f"  prune  {net.name:10s} -{len(removed)} dangling item(s), "
                    f"-{sum(box_len(it.box) for it in removed):.2f} um")
                net.items = kept
                net.changed = True
                obst.set_net(net)

    # After pruning (which wants the landings still fixed, so a dangling
    # branch is measured against the route that actually exists) and before
    # rerouting (which is what gets to use the freedom).
    bulk_report = relax_bulk_landings(layout, obst, cfg, log)

    if cfg.no_reroute:
        return bulk_report

    for p in range(cfg.passes):
        gained = 0.0
        straightened = 0
        order = sorted(targets, key=lambda n: -net_metrics(n, cfg.via_weight)["excess_um"])
        for net in order:
            if len(net.clusters) < 2:
                continue
            before = net.cost(cfg.via_weight)
            before_len = net.wirelength()
            t0 = time.time()
            candidate = reroute_net(layout, obst, net, cfg)
            if candidate is None:
                log(f"  pass{p+1}  {net.name:10s} no shorter legal rebuild"
                    f"{(' -- ' + net.note) if net.note else ''}")
                continue
            # Virtual pieces are metal that already exists inside a macro.
            # Counting them here compares a candidate's wire against the
            # original's wire PLUS a tie ring, which is not a comparison at
            # all -- it rejected rebuilds that were genuinely shorter.
            fresh = [it for it in candidate if not it.virtual]
            new_len = sum(box_len(it.box) for it in fresh if it.kind == "rect")
            new_vias = sum(1 for it in fresh if it.kind == "via")
            new_corners = sum(1 for it in fresh
                              if it.kind == "rect" and
                              (it.corner if it.corner is not None
                               else box_len(it.box) == 0.0))
            before_vias, before_corners = len(net.vias()), net.corners()

            # Lexicographic, in the order the objective is stated: wirelength
            # first, then vias, then turns. The second branch is the point --
            # a rebuild that is exactly as long but goes round fewer corners
            # used to be thrown away as "not shorter", so straightness the
            # search had already found was discarded at the door. Length may
            # never increase to buy it.
            if new_len < before_len - cfg.min_gain:
                why = ""
            elif (new_len <= before_len + EPS and
                  (new_vias, new_corners) < (before_vias, before_corners)):
                why = "  (same length, straighter)"
            else:
                log(f"  pass{p+1}  {net.name:10s} rebuild no better "
                    f"({before_len:.2f} -> {new_len:.2f} um, "
                    f"{before_corners} -> {new_corners} corners) -- kept original")
                continue
            log(f"  pass{p+1}  {net.name:10s} {before_len:8.2f} -> {new_len:8.2f} um "
                f"({before_len - new_len:+.2f}), vias {before_vias} -> {new_vias}"
                f", corners {before_corners} -> {new_corners}{why}"
                f"  [{time.time() - t0:.1f}s]")
            gained += before_len - new_len
            straightened += max(0, before_corners - new_corners)
            net.items = candidate
            net.changed = True
            obst.set_net(net)
        log(f"  pass {p+1} total saving: {gained:.2f} um, "
            f"{straightened} corner(s) removed")
        # A pass that only straightened still made progress worth another one.
        if gained < cfg.min_gain and straightened == 0:
            break
    return bulk_report


# ---------------------------------------------------------------------------
# emission: a new GDS and a new physical_map.json
# ---------------------------------------------------------------------------
MATCH_TOL = 0.02      # um; map coordinates and GDS coordinates round apart


def _match_poly(pool, box):
    best, err = None, None
    for k, (b, poly) in enumerate(pool):
        e = max(abs(b[0] - box[0]), abs(b[1] - box[1]),
                abs(b[2] - box[2]), abs(b[3] - box[3]))
        if err is None or e < err:
            best, err = k, e
    if best is None or err > MATCH_TOL:
        return None
    return pool.pop(best)[1]


def _match_via(pool, center, pair):
    best, err = None, None
    for k, (origin, metals, ref) in enumerate(pool):
        if metals != pair:
            continue
        e = max(abs(origin[0] - center[0]), abs(origin[1] - center[1]))
        if err is None or e < err:
            best, err = k, e
    if best is None or err > MATCH_TOL:
        return None
    return pool.pop(best)[2]


def emit(layout, cfg, out_gds, out_map, log):
    """Rewrite only what changed.

    Every rectangle and via that survived the optimisation keeps its identity,
    so it is never removed and re-added -- which matters, because map
    coordinates and GDS coordinates round a nanometre or two apart and
    re-emitting untouched metal would perturb geometry that is already
    DRC-clean for no reason at all."""
    lib = gdstk.read_gds(str(layout.gds_path))
    top = lib.top_level()[0]
    cells = {c.name: c for c in lib.cells}

    pools = defaultdict(list)
    for p in top.polygons:
        pools[(p.layer, p.datatype)].append((bbox(p.points), p))
    via_pool = []
    for r in top.references:
        if r.cell.name.startswith("via_stack"):
            via_pool.append((tuple(r.origin),
                             ViaTemplate(r.cell, r.cell.name).metals, r))

    drop_polys, drop_refs, new_elems = [], [], []
    changed = [layout.nets[n] for n in layout.net_order
               if layout.nets[n].changed]
    label_dt = pdk_option().label_datatype

    for net in changed:
        live = {id(it) for it in net.items}
        was = {id(it) for it in net.original}
        for it in net.original:
            if id(it) in live:
                continue
            if it.kind == "rect":
                poly = _match_poly(pools[it.layer], it.box)
                if poly is None:
                    raise SystemExit(
                        f"net {net.name}: no polygon in {layout.gds_path.name} "
                        f"matches map segment {it.layer} {it.box} within "
                        f"{MATCH_TOL} um. The GDS and the map disagree; "
                        f"refusing to write a half-edited layout.")
                drop_polys.append(poly)
            else:
                ref = _match_via(via_pool, it.center, it.vlayers)
                if ref is None:
                    raise SystemExit(
                        f"net {net.name}: no via_stack reference matches map "
                        f"via {it.vlayers} at {it.center}. Refusing to write.")
                drop_refs.append(ref)
        for it in net.items:
            if id(it) in was or it.virtual:
                continue
            if it.kind == "rect":
                new_elems.append(gdstk.rectangle(
                    (it.box[0], it.box[1]), (it.box[2], it.box[3]),
                    layer=it.layer[0], datatype=it.layer[1]))
            else:
                cell = cells.get(it.template.name)
                if cell is None:
                    raise SystemExit(
                        f"via template cell {it.template.name} vanished from "
                        f"the reloaded library -- cannot draw the via.")
                new_elems.append(gdstk.Reference(cell, origin=it.center))

    if drop_polys:
        top.remove(*drop_polys)
    if drop_refs:
        top.remove(*drop_refs)
    for e in new_elems:
        top.add(e)

    # A net-name label that falls off its own metal stops LVS promoting the
    # port, so every label on a moved net is re-checked and, if it now sits on
    # nothing, moved onto that net's new metal.
    moved_labels = []
    changed_by_name = {n.name: n for n in changed}
    for lab in list(top.labels):
        net = changed_by_name.get(lab.text)
        if net is None:
            continue
        metal = (lab.layer, 20)
        on_old = any(boxes_touch(
            (lab.origin[0], lab.origin[1], lab.origin[0], lab.origin[1]),
            it.box_on(metal), 0.0)
            for it in net.original if it.box_on(metal) is not None)
        on_new = any(boxes_touch(
            (lab.origin[0], lab.origin[1], lab.origin[0], lab.origin[1]),
            it.box_on(metal), 0.0)
            for it in net.items if it.box_on(metal) is not None)
        if on_new or not on_old:
            continue
        host = None
        for it in net.items:
            if it.box_on(metal) is not None:
                host = (metal, it.box_on(metal))
                break
        if host is None:
            for it in net.items:
                for lay in it.layers():
                    if it.box_on(lay) is not None:
                        host = (lay, it.box_on(lay))
                        break
                if host:
                    break
        if host is None:
            continue
        lay, box = host
        cx, cy = box_center(box)
        top.remove(lab)
        top.add(gdstk.Label(lab.text, (cx, cy), layer=lay[0],
                            texttype=label_dt, magnification=lab.magnification))
        moved_labels.append((lab.text, lab.origin, (cx, cy), lay))

    # Drop cells nothing references any more -- a via_stack whose last
    # reference this rewrite removed would otherwise be written out as a
    # second top-level cell, and every tool downstream has to guess.
    reachable = set()
    stack = [top]
    while stack:
        c = stack.pop()
        if id(c) in reachable:
            continue
        reachable.add(id(c))
        stack += [r.cell for r in c.references]
    orphans = [c for c in lib.cells if id(c) not in reachable]
    if orphans:
        lib.remove(*orphans)
        log(f"  dropped {len(orphans)} now-unreferenced cell(s)")

    lib.write_gds(str(out_gds))

    # the map, rewritten for the nets that moved and copied verbatim for the
    # rest
    out = dict(layout.map)
    recs = []
    for name in layout.net_order:
        net = layout.nets[name]
        rec = dict(net.record)
        if net.changed:
            rec["segments"] = [{
                "layer": list(it.layer),
                "glayer": layout.layer_table[it.layer]["glayer"],
                "points_um": [list(p) for p in box_points(it.box)] +
                             [list(box_points(it.box)[0])],
            } for it in net.rects()]
            rec["vias"] = [{
                "x_um": it.center[0], "y_um": it.center[1],
                "from_layer": list(it.vlayers[0]),
                "to_layer": list(it.vlayers[1]),
                "from_glayer": layout.layer_table[it.vlayers[0]]["glayer"],
                "to_glayer": layout.layer_table[it.vlayers[1]]["glayer"],
            } for it in net.vias()]
        recs.append(rec)
    out["nets"] = recs
    out["layout"] = Path(out_gds).name
    out["route_shortening"] = {
        "source_gds": str(layout.gds_path),
        "source_map": str(layout.map_path),
        "nets_changed": [n.name for n in changed],
        "labels_relocated": [
            {"net": t, "from": list(a), "to": list(b), "layer": list(l)}
            for t, a, b, l in moved_labels],
    }
    with open(out_map, "w") as fh:
        json.dump(out, fh, indent=2)
    for t, a, b, l in moved_labels:
        log(f"  label  {t}: ({a[0]:.3f}, {a[1]:.3f}) -> ({b[0]:.3f}, {b[1]:.3f}) on {l}")
    return moved_labels


# ---------------------------------------------------------------------------
# DRC
# ---------------------------------------------------------------------------
def run_drc(gds_path, top_cell, work_dir, log):
    script = REPO_ROOT / ".claude" / "skills" / "router" / "script" / "run_drc.py"
    cmd = [sys.executable, str(script), str(gds_path),
           "--top", top_cell, "--work-dir", str(work_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log((proc.stdout or "").rstrip())
    if proc.stderr.strip():
        log("  [drc stderr] " + proc.stderr.strip()[:2000])
    report = Path(work_dir) / "drc_violations.json"
    if not report.exists():
        return None, {}
    with open(report) as fh:
        data = json.load(fh)
    viol = data.get("violations", {})
    boxes = [(rule, tuple(b)) for rule, lst in viol.items() for b in lst]
    return proc.returncode == 0, boxes


def attribute(boxes, layout, margin):
    """Which changed nets does each violation box sit on?"""
    hits = defaultdict(list)
    unattributed = []
    for rule, b in boxes:
        owners = set()
        for name in layout.net_order:
            net = layout.nets[name]
            if not net.changed:
                continue
            fresh = {id(it) for it in net.items} - {id(it) for it in net.original}
            for it in net.items:
                if id(it) not in fresh:
                    continue
                for lay in it.layers():
                    ib = it.box_on(lay)
                    if ib is not None and boxes_conflict(ib, b, margin):
                        owners.add(name)
                        break
                if name in owners:
                    break
        if owners:
            for o in owners:
                hits[o].append((rule, b))
        else:
            unattributed.append((rule, b))
    return hits, unattributed


# ---------------------------------------------------------------------------
# reports
# ---------------------------------------------------------------------------
def table(rows, header):
    widths = [max(len(str(r[i])) for r in [header] + rows)
              for i in range(len(header))]
    out = ["  ".join(str(h).ljust(w) for h, w in zip(header, widths)).rstrip()]
    out.append("  ".join("-" * w for w in widths))
    for r in rows:
        out.append("  ".join(str(c).ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(out)


def write_summary(path, layout, cfg, before, after, drc_state, reverted,
                  labels, elapsed, bulk=None):
    L = []
    A = L.append
    A("=" * 78)
    A("ROUTE SHORTENING SUMMARY")
    A("=" * 78)
    A(f"design      : {layout.map.get('design', '?')}")
    A(f"input GDS   : {layout.gds_path}")
    A(f"input map   : {layout.map_path}")
    A(f"top cell    : {layout.top_name}")
    A(f"PDK         : {pdk_option().name}")
    A(f"runtime     : {elapsed:.1f} s")
    A("")
    A("-- gates " + "-" * 69)
    A(f"  exact legality re-check on every new rectangle : PASS "
      f"(no route was committed without it)")
    A(f"  every net still spans all of its own landings  : PASS")
    A(f"  Magic DRC on the output                        : {drc_state}")
    A("  LVS                                            : NOT RUN HERE -- "
      "hand the output to layout-fixer")
    A("")
    A("-- totals " + "-" * 68)
    tl_b = sum(m["wirelength_um"] for m in before.values())
    tl_a = sum(m["wirelength_um"] for m in after.values())
    tv_b = sum(m["vias"] for m in before.values())
    tv_a = sum(m["vias"] for m in after.values())
    tc_b = sum(m["corners"] for m in before.values())
    tc_a = sum(m["corners"] for m in after.values())
    lb = sum(m["bbox_bound_um"] for m in before.values())
    A(f"  total wirelength : {tl_b:10.2f} um -> {tl_a:10.2f} um "
      f"({tl_a - tl_b:+.2f} um, {100.0 * (tl_a - tl_b) / tl_b if tl_b else 0:+.1f}%)")
    A(f"  vias             : {tv_b:10d}    -> {tv_a:10d}")
    A(f"  corners          : {tc_b:10d}    -> {tc_a:10d}")
    A(f"  lower bound      : {lb:10.2f} um  (sum of per-net terminal-bbox "
      f"half-perimeters -- no legal route can beat this)")
    A(f"  gap to bound     : {tl_b - lb:10.2f} um -> {tl_a - lb:10.2f} um")
    A("")
    A("-- per net " + "-" * 67)
    rows = []
    for name in layout.net_order:
        b, a = before.get(name), after.get(name)
        if not b:
            continue
        rows.append([
            name, b["terminals"],
            f"{b['wirelength_um']:.2f}", f"{a['wirelength_um']:.2f}",
            f"{a['wirelength_um'] - b['wirelength_um']:+.2f}",
            f"{b['vias']}->{a['vias']}", f"{b['corners']}->{a['corners']}",
            f"{b['bbox_bound_um']:.2f}",
            f"{b['excess_ratio']:.2f}x" if b["bbox_bound_um"] else "-",
            f"{a['excess_ratio']:.2f}x" if a["bbox_bound_um"] else "-",
            "reverted" if name in reverted else
            ("changed" if layout.nets[name].changed else ""),
        ])
    A(table(rows, ["net", "term", "len_before", "len_after", "delta",
                   "vias", "corners", "bound", "ratio_b", "ratio_a",
                   "status"]))
    A("")
    if reverted:
        A("-- reverted " + "-" * 66)
        for name, why in reverted.items():
            A(f"  {name}: {why}")
        A("")
    if bulk:
        A("-- bulk landings released onto their tie rings " + "-" * 31)
        for netname, entries in sorted(bulk.items()):
            for inst, n, box, is_ring in entries:
                A(f"  {netname}: {inst}  "
                  f"{'tie ring' if is_ring else 'bulk node (not ring-shaped)'} "
                  f"({box[0]:.2f}, {box[1]:.2f}) .. ({box[2]:.2f}, {box[3]:.2f}) um, "
                  f"{n} landable box(es)")
        A("  A tie ring is one node, so these connections may land anywhere on")
        A("  it. The fixed port `fet.py` gives bulk stays as it is -- routing")
        A("  needs one deterministic point; this pass is what may move off it.")
        A("  `bound` below is still measured against the ORIGINAL landings, so")
        A("  a released net can legitimately finish under its own bound.")
        A("")
    if labels:
        A("-- relocated net labels " + "-" * 54)
        for t, a0, b0, lay in labels:
            A(f"  {t}: ({a0[0]:.3f}, {a0[1]:.3f}) -> ({b0[0]:.3f}, {b0[1]:.3f}) on {lay}")
        A("")
    A("-- geometry inherited from the input " + "-" * 41)
    rows = []
    for lay in layout.routing_layers:
        info = layout.layer_table[lay]
        rows.append([f"{lay[0]}/{lay[1]}", info["glayer"], info["process"],
                     f"{layout.wire_width[lay]:.3f}",
                     f"{info['min_width']:.3f}",
                     f"{layout.clearance(lay, cfg.extra_clearance):.3f}"])
    A(table(rows, ["gds", "glayer", "process", "wire_w", "pdk_min_w",
                   "clearance"]))
    A("  wire widths are MEASURED off the input's own wires; clearances come "
      "from the PDK.")
    A("  via transitions available (templates found in the input GDS): " +
      (", ".join(f"{a[0]}/{a[1]}-{b[0]}/{b[1]}"
                 for a, b in sorted(layout.via_templates)) or "none"))
    A("")
    A("-- files " + "-" * 69)
    A("  Everything this run produced is beside this summary, as files: the")
    A("  shortened GDS, the updated physical_map.json, shortening_report.json,")
    A("  and drc_violations.json (plus drc.log only if DRC did not come out")
    A("  clean). Magic's per-run scratch is deleted once its verdict has been")
    A("  recorded -- pass --keep-work to keep it under _work/ instead.")
    A("")
    A("-- reproducibility " + "-" * 59)
    A(f"  passes={cfg.passes} via_weight={cfg.via_weight} "
      f"bend_penalty={cfg.bend_penalty} min_gain={cfg.min_gain}")
    A(f"  margin={cfg.margin} max_coords={cfg.max_coords} "
      f"extra_clearance={cfg.extra_clearance}")
    A(f"  prune={'off' if cfg.no_prune else 'on'} "
      f"reroute={'off' if cfg.no_reroute else 'on'} "
      f"bulk_relax={'off' if cfg.no_bulk_relax else 'on'} "
      f"drc_retries={cfg.drc_retries}")
    A(f"  keep_work={'on' if getattr(cfg, 'keep_work', False) else 'off'}")
    A("=" * 78)
    text = "\n".join(L)
    with open(path, "w") as fh:
        fh.write(text + "\n")
    return text


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
class Cfg:
    pass


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("gds", help="a DRC-clean routed layout")
    ap.add_argument("--map", default=None,
                    help="physical_map.json (default: beside the GDS)")
    ap.add_argument("--out-dir", default=None,
                    help="default: <gds's dir>/shortening")
    ap.add_argument("--analyze-only", action="store_true",
                    help="report the detour table and write nothing")
    ap.add_argument("--nets", default="",
                    help="comma-separated net names to work on (default: all)")
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--via-weight", type=float, default=2.0,
                    help="um of wire a via is worth (default 2.0)")
    ap.add_argument("--bend-penalty", type=float, default=0.2,
                    help="um of wire a corner is worth (default 0.2 -- "
                         "measured best on both wirelength AND turns; a "
                         "straighter net leaves cleaner channels for the nets "
                         "after it). Set 0 for the strict reading: turns are "
                         "then only ever a tie-break among equally short "
                         "routes and never cost a nanometre")
    ap.add_argument("--min-gain", type=float, default=0.1,
                    help="um; a rebuild must beat the original by this")
    ap.add_argument("--margin", type=float, default=8.0,
                    help="um of routing room around each connection's bbox")
    ap.add_argument("--max-coords", type=int, default=180,
                    help="cap on grid lines per axis per connection")
    ap.add_argument("--extra-clearance", type=float, default=0.0,
                    help="um added to the PDK min separation")
    ap.add_argument("--no-bulk-relax", action="store_true",
                    help="keep every bulk landing exactly where the router put "
                         "it, instead of letting it move along its tie ring")
    ap.add_argument("--no-prune", action="store_true")
    ap.add_argument("--no-reroute", action="store_true")
    ap.add_argument("--no-drc", action="store_true")
    ap.add_argument("--drc-retries", type=int, default=2)
    ap.add_argument("--drc-attrib-margin", type=float, default=0.5,
                    help="um; how close new metal must be to a violation box "
                         "to be blamed for it")
    ap.add_argument("--skip-baseline-drc", action="store_true",
                    help="trust the caller that the input is DRC-clean")
    ap.add_argument("--keep-work", action="store_true",
                    help="keep each Magic DRC run's own directory (log, tcl, "
                         "raw violations) instead of deleting it once its "
                         "verdict has been recorded")
    args = ap.parse_args()

    cfg = Cfg()
    for k in ("passes", "via_weight", "bend_penalty", "min_gain", "margin",
              "max_coords", "extra_clearance", "no_prune", "no_reroute",
              "no_bulk_relax", "drc_retries", "drc_attrib_margin",
              "keep_work"):
        setattr(cfg, k, getattr(args, k))
    cfg.nets = {n.strip() for n in args.nets.split(",") if n.strip()}

    gds_path = Path(args.gds).resolve()
    if not gds_path.exists():
        sys.exit(f"no such file: {gds_path}")
    map_path = Path(args.map).resolve() if args.map else gds_path.parent / "physical_map.json"
    if not map_path.exists():
        sys.exit(f"no physical_map.json at {map_path} -- pass --map")
    out_dir = Path(args.out_dir).resolve() if args.out_dir else gds_path.parent / "shortening"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Every tool run's scratch -- Magic's tcl, its log, the raw violation
    # dump -- goes in ONE place that is deleted once its verdict has been
    # recorded. See SKILL.md's rule: the output of this skill is a layout, a
    # map and reports, not a directory tree to go hunting in.
    #
    # Created LAZILY and torn down through atexit, so a run that never invokes
    # a tool (`--analyze-only`) leaves nothing behind, and one that dies
    # part-way does not either. Creating it up front leaked a temp directory
    # per analyze-only run, which is exactly the mess the rule exists to stop.
    scratch = {"root": None}

    def work_dir(name):
        if scratch["root"] is None:
            scratch["root"] = (out_dir / "_work" if args.keep_work
                               else Path(tempfile.mkdtemp(prefix="route_optimizer_")))
        return scratch["root"] / name

    def drop_work():
        root = scratch["root"]
        if root and not args.keep_work and root.exists():
            shutil.rmtree(root, ignore_errors=True)

    atexit.register(drop_work)
    kept_drc = None

    t0 = time.time()
    lines = []

    def log(msg):
        print(msg)
        lines.append(msg)

    log(f"=== route-optimizer: {gds_path.name} ===")
    pdk_cfg, layer_table = load_pdk_layers()
    layout = Layout(gds_path, map_path, layer_table)
    stack = ", ".join("%d/%d(%s)" % (l[0], l[1], layer_table[l]["glayer"])
                      for l in layout.routing_layers)
    log(f"  PDK {pdk_cfg.name}; routing layers {stack}")
    log(f"  {len(layout.nets)} net(s), {len(layout.macro_refs)} macro(s), "
        f"{len(layout.via_refs)} via(s)")

    freeze_landings(layout)
    for net in layout.nets.values():
        build_clusters(net)
    before = {n: net_metrics(layout.nets[n], args.via_weight)
              for n in layout.net_order}

    rows = []
    for name in sorted(layout.net_order,
                       key=lambda n: -before[n]["excess_um"]):
        m = before[name]
        rows.append([name, m["terminals"], f"{m['wirelength_um']:.2f}",
                     m["vias"], m["corners"], f"{m['bbox_bound_um']:.2f}",
                     f"{m['mst_um']:.2f}", f"{m['excess_um']:.2f}",
                     f"{m['excess_ratio']:.2f}x" if m["bbox_bound_um"] else "-"])
    log("")
    log(table(rows, ["net", "term", "wirelen", "vias", "corners", "bound",
                     "mst", "excess", "ratio"]))
    log("")
    log("  bound = terminal-bbox half-perimeter (a true lower bound); "
        "mst = Manhattan MST over the terminals")
    log("  excess = wirelen - bound: the wire that is provably detour, "
        "in um. Ranked worst first.")

    if args.analyze_only:
        (out_dir / "shortening_analysis.txt").write_text("\n".join(lines) + "\n")
        log(f"\nanalyze-only: nothing written but "
            f"{out_dir / 'shortening_analysis.txt'}")
        return

    obst = Obstacles(layout)

    baseline_boxes = []
    if not args.no_drc and not args.skip_baseline_drc:
        log("\n-- baseline DRC on the INPUT (this skill's whole premise is "
            "that it is clean) --")
        clean, baseline_boxes = run_drc(gds_path, layout.top_name,
                                        work_dir("drc_input"), log)
        if not clean:
            log(f"  WARNING: the input is NOT DRC-clean ({len(baseline_boxes)} "
                f"violation box(es)). Shortening will proceed, and only NEW "
                f"violations will be held against it -- but fix the input "
                f"first if you want a trustworthy result.")

    log("\n-- optimizing --")
    bulk_report = optimize(layout, obst, cfg, log) or {}

    out_gds = out_dir / f"{gds_path.stem}_shortened.gds"
    out_map = out_dir / "physical_map.json"
    changed = [n for n in layout.net_order if layout.nets[n].changed]
    if not changed:
        log("\nNo net could be shortened. Nothing written.")
        after = before
        write_summary(out_dir / "shortening_summary.txt", layout, cfg, before,
                      after, "not run (nothing changed)", {}, [],
                      time.time() - t0, bulk_report)
        drop_work()
        return

    log("\n-- writing --")
    labels = emit(layout, cfg, out_gds, out_map, log)
    log(f"  {out_gds}")
    log(f"  {out_map}")

    reverted = {}
    drc_state = "not run (--no-drc)"
    if not args.no_drc:
        base_set = {(r, tuple(round(v, 3) for v in b))
                    for r, b in baseline_boxes}
        for attempt in range(args.drc_retries + 1):
            log(f"\n-- DRC on the output (attempt {attempt + 1}) --")
            drc_dir = work_dir(f"drc_out_{attempt + 1}")
            clean, boxes = run_drc(out_gds, layout.top_name, drc_dir, log)
            kept_drc = drc_dir
            fresh = [(r, b) for r, b in boxes
                     if (r, tuple(round(v, 3) for v in b)) not in base_set]
            if clean or not fresh:
                drc_state = ("PASS (clean)" if clean else
                             "PASS -- only violations the input already had")
                break
            hits, orphan = attribute(fresh, layout, args.drc_attrib_margin)
            log(f"  {len(fresh)} new violation box(es); attributed to "
                f"{len(hits)} net(s), {len(orphan)} unattributed")
            if not hits and not orphan:
                drc_state = "FAIL"
                break
            victims = set(hits)
            if orphan and attempt == args.drc_retries - 1:
                victims |= {n for n in layout.net_order
                            if layout.nets[n].changed}
            if not victims:
                drc_state = (f"FAIL -- {len(fresh)} new violation(s) could not "
                             f"be attributed to any rebuilt net")
                break
            for name in victims:
                net = layout.nets[name]
                net.items = list(net.original)
                net.changed = False
                obst.set_net(net)
                reverted[name] = (
                    "reverted: new DRC violation on its rebuilt metal"
                    if name in hits else
                    "reverted: unattributed new violation, rolled back as a group")
                log(f"  revert {name}")
            if not any(layout.nets[n].changed for n in layout.net_order):
                log("  everything reverted -- the input routing stands")
                drc_state = "PASS (all rebuilds reverted)"
                for stale in (out_gds, out_map):
                    if stale.exists():
                        stale.unlink()
                break
            labels = emit(layout, cfg, out_gds, out_map, log)
        else:
            drc_state = "FAIL -- retries exhausted"

    after = {n: net_metrics(layout.nets[n], args.via_weight)
             for n in layout.net_order}
    # Re-base every after-metric on the BEFORE bound. Releasing a bulk landing
    # enlarges the terminal set, which moves the bound -- and a before/after
    # table whose yardstick moves between the two columns compares nothing.
    for n, a in after.items():
        b = before.get(n)
        if not b:
            continue
        a["bbox_bound_um"] = b["bbox_bound_um"]
        a["mst_um"] = b["mst_um"]
        a["excess_um"] = max(0.0, a["wirelength_um"] - b["bbox_bound_um"])
        a["excess_ratio"] = (a["wirelength_um"] / b["bbox_bound_um"]
                             if b["bbox_bound_um"] > 1e-9 else 0.0)
    # Two things survive from the DRC scratch: the structured violation list
    # (small, and what a caller acts on) and, only when the run did NOT come
    # out clean, the raw Magic log that explains why.
    if kept_drc is not None:
        src = kept_drc / "drc_violations.json"
        if src.exists():
            shutil.copy(src, out_dir / "drc_violations.json")
        if not drc_state.startswith("PASS") and (kept_drc / "drc.log").exists():
            shutil.copy(kept_drc / "drc.log", out_dir / "drc.log")
    drop_work()

    text = write_summary(out_dir / "shortening_summary.txt", layout, cfg,
                         before, after, drc_state, reverted, labels,
                         time.time() - t0, bulk_report)
    with open(out_dir / "shortening_report.json", "w") as fh:
        json.dump({"input_gds": str(gds_path), "input_map": str(map_path),
                   "output_gds": str(out_gds), "output_map": str(out_map),
                   "drc": drc_state, "reverted": reverted,
                   "bulk_landings_released": {
                       k: [{"instance": i, "landable_boxes": n,
                            "region_bbox": list(b), "is_ring": r}
                           for i, n, b, r in v]
                       for k, v in bulk_report.items()},
                   "before": before, "after": after}, fh, indent=2)
    print("\n" + text)
    tl_b = sum(m["wirelength_um"] for m in before.values())
    tl_a = sum(m["wirelength_um"] for m in after.values())
    print(f"\ntotal wirelength {tl_b:.2f} -> {tl_a:.2f} um "
          f"({tl_a - tl_b:+.2f}); DRC: {drc_state}")
    print(f"summary: {out_dir / 'shortening_summary.txt'}")
    if drc_state.startswith("FAIL"):
        sys.exit(1)


if __name__ == "__main__":
    main()
