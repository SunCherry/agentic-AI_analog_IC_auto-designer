#!/usr/bin/env python3
"""
Redraw a layout from <design_dir>/physical_map.json, then verify the redraw
against the original GDS it was extracted from.

The point of this script is NOT to produce a manufacturable layout -- it is to
prove that extraction captured the layout correctly. It rebuilds a GDS using
ONLY what physical_map.json recorded, and then diffs that reconstruction
against the original top-level geometry. Anything extraction dropped, doubled,
or misplaced shows up as a difference.

What gets drawn, all from the map:
  * every net's routing segments, replayed verbatim on their recorded layers
  * every device as a real instance of its recorded primitive cell, placed at
    its recorded rotation/mirror and origin -- actual poly/diff/contact
    geometry, not an outline
  * every device's placement bounding box, as an outline on a marker layer
  * an instance-name label at each device's centre, and a net-name label on
    each net's first segment

The map stores which primitive cell each device is, not that cell's internals,
so the device geometry is read back from <design_dir>/primitives/ -- the same
folder extraction bound against. Devices are emitted as SREFs to real cell
definitions rather than flattened, which keeps the top cell's own polygons
exactly the routing that the verification below diffs (flattening would mix
each device's internal li1/met1 into that comparison and report it as routing
the layout does not have). Without primitives/ the devices fall back to
outline boxes, and the run says so.

Unlike redraw_layout.py this needs no glayout and no gdsfactory -- it is pure
stdlib, the same as the extractor, so it runs wherever extraction runs.

Usage (from the repo root; one or more design dirs):
  python .claude/skills/layout-extractor/script/redraw_from_map.py <design_dir>
"""

import os
import sys
import json
import struct
from collections import defaultdict, Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_physical_info import (          # noqa: E402
    parse_gds, ROUTING_LAYERS, VIA_LINKS, _extract_named_net_cells,
    _transform_points, _load_pdk,
)

# Marker layer for the redraw's own annotation (device boxes + labels): a
# layer the process leaves unmapped, so annotation can never be mistaken for
# real drawn geometry. Which layer that is comes from the project guideline,
# `.claude/reference/pdk_options.json` (layers.annotation_layer).
ANNOT_LAYER, ANNOT_DATATYPE = _load_pdk().annotation_layer


# ---------------------------------------------------------------------------
# Minimal GDS2 writer (mirror of extract_physical_info.parse_gds)
# ---------------------------------------------------------------------------

def _rec(rec_type, payload=b''):
    """One GDS2 record: 2-byte total length, 2-byte type, payload."""
    return struct.pack('>HH', len(payload) + 4, rec_type) + payload


def _int2(*vals):
    return b''.join(struct.pack('>h', v) for v in vals)


def _int4(*vals):
    return b''.join(struct.pack('>i', v) for v in vals)


def _real8(value):
    """Encode a GDS2 8-byte excess-64 base-16 float (inverse of _gds_real)."""
    if value == 0:
        return b'\x00' * 8
    sign = 0x80 if value < 0 else 0x00
    v = abs(float(value))
    exp = 0
    while v >= 1.0:
        v /= 16.0
        exp += 1
    while v < 1.0 / 16.0:
        v *= 16.0
        exp -= 1
    mant = int(round(v * (1 << 56)))
    if mant >= (1 << 56):        # rounding pushed the mantissa over
        mant >>= 4
        exp += 1
    return bytes([sign | (exp + 64)]) + mant.to_bytes(7, 'big')


def _ascii(s):
    b = s.encode('ascii', errors='replace')
    return b + (b'\x00' if len(b) % 2 else b'')


def _cell_records(cell_name, polygons, labels=(), instances=()):
    """Serialize one GDS2 structure (BGNSTR..ENDSTR).

    polygons:  [(layer, datatype, [(x_nm, y_nm), ...]), ...]
    labels:    [(layer, texttype, (x_nm, y_nm), text), ...]
    instances: [(ref_cell_name, (x_nm, y_nm), angle_deg, mirror_bool), ...]
    """
    out = bytearray()
    out += _rec(0x0502, _int2(*([0] * 12)))                          # BGNSTR
    out += _rec(0x0606, _ascii(cell_name))                           # STRNAME

    for layer, datatype, pts in polygons:
        if len(pts) < 4:
            continue
        if pts[0] != pts[-1]:                # GDS boundaries must be closed
            pts = list(pts) + [pts[0]]
        flat = []
        for x, y in pts:
            flat += [int(round(x)), int(round(y))]
        out += _rec(0x0800)                                          # BOUNDARY
        out += _rec(0x0D02, _int2(layer))                            # LAYER
        out += _rec(0x0E02, _int2(datatype))                         # DATATYPE
        out += _rec(0x1003, _int4(*flat))                            # XY
        out += _rec(0x1100)                                          # ENDEL

    for layer, texttype, (x, y), text in labels:
        out += _rec(0x0C00)                                          # TEXT
        out += _rec(0x0D02, _int2(layer))                            # LAYER
        out += _rec(0x1602, _int2(texttype))                         # TEXTTYPE
        out += _rec(0x1003, _int4(int(round(x)), int(round(y))))     # XY
        out += _rec(0x1906, _ascii(str(text)))                       # STRING
        out += _rec(0x1100)                                          # ENDEL

    # SREF. STRANS/ANGLE are omitted for an unrotated, unmirrored placement
    # (the common case) and must be written in that order when present --
    # mirror is about the X axis and applies before the rotation, matching
    # parse_gds's read side and sref_bbox_um's transform order.
    for ref, (x, y), angle, mirror in instances:
        out += _rec(0x0A00)                                          # SREF
        out += _rec(0x1206, _ascii(ref))                             # SNAME
        if mirror or angle:
            out += _rec(0x1A01, _int2(-32768 if mirror else 0))      # STRANS
            if angle:
                out += _rec(0x1C05, _real8(float(angle)))            # ANGLE
        out += _rec(0x1003, _int4(int(round(x)), int(round(y))))     # XY
        out += _rec(0x1100)                                          # ENDEL

    out += _rec(0x0700)                                              # ENDSTR
    return out


def write_gds(path, cell_name, polygons, labels, cell_defs=(), instances=(),
              units=(1e-9, 1e-9)):
    """Write a GDS2 file: the referenced cell definitions, then the top cell.

    cell_defs: [(cell_name, polygons), ...] -- written before the top cell so
      every SREF resolves against a definition that already appeared.
    """
    out = bytearray()
    out += _rec(0x0002, _int2(600))                                  # HEADER
    out += _rec(0x0102, _int2(*([0] * 12)))                          # BGNLIB
    out += _rec(0x0206, _ascii(cell_name))                           # LIBNAME
    out += _rec(0x0305, _real8(units[0]) + _real8(units[1]))         # UNITS

    for def_name, def_polys in cell_defs:
        out += _cell_records(def_name, def_polys)
    out += _cell_records(cell_name, polygons, labels, instances)

    out += _rec(0x0400)                                              # ENDLIB

    with open(path, 'wb') as f:
        f.write(bytes(out))
    return path


# ---------------------------------------------------------------------------
# Redraw
# ---------------------------------------------------------------------------

def _um_to_nm(v):
    return int(round(float(v) * 1000.0))


def _cell_entry(cell_name, cell):
    return {
        'cell_name': cell_name,
        'bbox': cell['bbox'],
        'polygons': [(s['layer'], s['datatype'], s['points'])
                     for s in cell['shapes']],
    }


def load_primitive_cells(design_dir, layout_cells=None):
    """Index every cell a device could be drawn from, by name -> geometry.

    Two sources, because the two supported formats keep device geometry in
    different places. Hash-based designs keep it in <design_dir>/primitives/,
    one file per device, and the map records those cell names. ALIGN designs
    define each device cell inside the main GDS itself ('M0_sky130_fd_pr__...')
    and have no separate file to read, so the layout's own cell definitions are
    indexed too. primitives/ wins on a name collision, being what extraction
    bound against.

    primitives/ files are read the way catalogue_primitives reads them -- the
    file's first cell is the device -- and indexed under both that cell's name
    and the filename stem, since a map records whichever the extractor resolved.
    """
    out = {}
    for cell_name, cell in (layout_cells or {}).items():
        if cell.get('shapes'):
            out[cell_name] = _cell_entry(cell_name, cell)

    prim_dir = os.path.join(design_dir, 'primitives')
    if not os.path.isdir(prim_dir):
        return out
    for fname in sorted(os.listdir(prim_dir)):
        if not fname.endswith('.gds'):
            continue
        cells = parse_gds(os.path.join(prim_dir, fname))
        if not cells:
            continue
        cell_name = next(iter(cells))
        entry = _cell_entry(cell_name, cells[cell_name])
        out[cell_name] = entry
        out.setdefault(fname.split('.')[0], entry)
    return out


def place_devices(pmap, primitives):
    """Bind each placed device to its primitive cell as an SREF.

    Returns (cell_defs, instances, placed, unbound, misaligned, shared).

    The SREF origin is solved, not assumed: the cell's own bbox corners are put
    through the same mirror-then-rotate transform the map recorded, and the
    origin is whatever lands that transformed box's lower-left corner on the
    device's recorded x0/y0. That keeps the placement correct for a rotated or
    mirrored cell, whose local origin is no longer its lower-left corner.

    Placements are deduplicated by (cell, origin, orientation), because a
    matched pair drawn as one DCL cell is two netlist devices inside a single
    physical cell -- the map gives both the same primitive_cell and the same
    box. Emitting an SREF per device would stack that cell on itself and
    double its geometry, which verification would correctly flag as invented.
    """
    cell_defs = {}
    instances = []
    seen = {}
    placed, unbound, misaligned, shared = [], [], [], []

    for dev in pmap.get('devices', []):
        if dev.get('x0_um') is None:
            continue
        prim_name = dev.get('primitive_cell')
        prim = primitives.get(prim_name) if prim_name else None
        if prim is None or not prim['bbox']:
            unbound.append((dev['instance'], prim_name))
            continue

        angle = int(dev.get('rotation_deg') or 0)
        mirror = bool(dev.get('mirror'))
        x0c, y0c, x1c, y1c = prim['bbox']
        corners = _transform_points([(x0c, y0c), (x1c, y0c), (x1c, y1c),
                                     (x0c, y1c)], 0, 0, angle, mirror)
        tx0 = min(p[0] for p in corners)
        ty0 = min(p[1] for p in corners)
        ox = _um_to_nm(dev['x0_um']) - tx0
        oy = _um_to_nm(dev['y0_um']) - ty0

        # The placed footprint must reproduce the box the map recorded; if it
        # does not, this device is bound to the wrong primitive cell and the
        # geometry would be quietly misplaced rather than obviously absent.
        pw = max(p[0] for p in corners) - tx0
        ph = max(p[1] for p in corners) - ty0
        rw = _um_to_nm(dev['x1_um']) - _um_to_nm(dev['x0_um'])
        rh = _um_to_nm(dev['y1_um']) - _um_to_nm(dev['y0_um'])
        if abs(pw - rw) > 1 or abs(ph - rh) > 1:
            misaligned.append((dev['instance'], prim['cell_name'],
                               (pw / 1000.0, ph / 1000.0),
                               (rw / 1000.0, rh / 1000.0)))

        cell_defs.setdefault(prim['cell_name'], prim['polygons'])
        placed.append(dev['instance'])

        key = (prim['cell_name'], ox, oy, angle, mirror)
        if key in seen:
            seen[key].append(dev['instance'])
            continue
        seen[key] = [dev['instance']]
        instances.append((prim['cell_name'], (ox, oy), angle, mirror))

    shared = [(cell, insts) for (cell, _x, _y, _a, _m), insts in seen.items()
              if len(insts) > 1]
    return (sorted(cell_defs.items()), instances, placed, unbound, misaligned,
            shared)


def build_from_map(pmap):
    """Turn physical_map.json into (polygons, labels), all coordinates in nm."""
    polygons = []
    labels = []

    # 1. Routing, replayed verbatim on the layers it was recorded on.
    for net in pmap.get('nets', []):
        first_pt = None
        for seg in net.get('segments', []):
            layer, datatype = seg['layer']
            pts = [(_um_to_nm(x), _um_to_nm(y)) for x, y in seg['points_um']]
            if not pts:
                continue
            polygons.append((layer, datatype, pts))
            if first_pt is None:
                first_pt = pts[0]
        if first_pt is not None and net.get('name'):
            labels.append((ANNOT_LAYER, ANNOT_DATATYPE, first_pt, net['name']))

    # 2. Device placement boxes + instance labels, on the annotation layer.
    for dev in pmap.get('devices', []):
        if dev.get('x0_um') is None:
            continue
        x0, y0 = _um_to_nm(dev['x0_um']), _um_to_nm(dev['y0_um'])
        x1, y1 = _um_to_nm(dev['x1_um']), _um_to_nm(dev['y1_um'])
        polygons.append((ANNOT_LAYER, ANNOT_DATATYPE,
                         [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]))
        labels.append((ANNOT_LAYER, ANNOT_DATATYPE,
                       ((x0 + x1) // 2, (y0 + y1) // 2), dev['instance']))
    return polygons, labels


# ---------------------------------------------------------------------------
# Verification against the original
# ---------------------------------------------------------------------------

def _poly_area(pts):
    """Absolute shoelace area, in nm^2."""
    a = 0
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        a += x0 * y1 - x1 * y0
    return abs(a) / 2.0


def _routing_lds():
    return {(lay, 20) for lay in ROUTING_LAYERS} | set(VIA_LINKS)


def _flatten_placed(cells, top_name):
    """Multiset of every polygon contributed by the top cell's placed cells.

    'NET_<name>' cells are skipped: an ALIGN layout keeps its routing in those,
    and routing is compared separately as top-level geometry. One level deep,
    which is all the primitive cells this project's generators emit.
    """
    out = Counter()
    for sr in cells.get(top_name, {}).get('srefs', []):
        if sr['ref'].startswith('NET_'):
            continue
        sub = cells.get(sr['ref'])
        if not sub:
            continue
        for s in sub['shapes']:
            pts = _transform_points(s['points'], sr['xy'][0], sr['xy'][1],
                                    sr.get('angle', 0), sr.get('mirror', False))
            key = (s['layer'], s['datatype'],
                   tuple((int(round(x)), int(round(y))) for x, y in pts))
            out[key] += 1
    return out


def verify(pmap, cells, polygons, redrawn_cells=None, redrawn_top=None):
    """Diff the reconstruction against the original layout's geometry.

    `cells` is the already-parsed original (process() needs it before this
    point anyway, to source ALIGN device definitions from).
    """
    refd = {s['ref'] for c in cells.values() for s in c['srefs']}
    tops = [n for n in cells if n not in refd]
    top = max(tops, key=lambda c: len(cells[c]['srefs'])) if tops else None
    if top is None:
        print("  cannot verify: no top cell found in the original")
        return False
    orig = cells[top]

    lds = _routing_lds()
    # The reference set is exactly what extraction treats as routing: the top
    # cell's own polygons, PLUS the contents of any 'NET_<name>' cell. An
    # ALIGN-style layout pre-groups each net's wiring into such a cell and has
    # no top-level routing at all, so comparing against top-level polygons
    # alone would report every one of its nets as invented.
    orig_all = [{'layer': s['layer'], 'datatype': s['datatype'],
                 'points': s['points']}
                for s in orig['shapes'] if (s['layer'], s['datatype']) in lds]
    for _net, segs in _extract_named_net_cells(cells, top).items():
        for seg in segs:
            layer, datatype = seg['layer']
            if (layer, datatype) not in lds:
                continue
            orig_all.append({
                'layer': layer, 'datatype': datatype,
                'points': [(_um_to_nm(x), _um_to_nm(y))
                           for x, y in seg['points_um']],
            })

    # Degenerate polygons (zero area -- typically all-identical vertices left
    # behind by the generator) carry no geometry and are excluded from the
    # comparison. Extraction drops them because a zero-area shape lands in an
    # island that resolves to no net, which is the correct thing to do; they
    # are reported separately so the exclusion is visible, not silent.
    orig_shapes = [s for s in orig_all if _poly_area(s['points']) > 0]
    n_degenerate = len(orig_all) - len(orig_shapes)
    redraw_shapes = [(l, d, p) for (l, d, p) in polygons
                     if (l, d) in lds and _poly_area(p) > 0]

    o_cnt, o_area = defaultdict(int), defaultdict(float)
    for s in orig_shapes:
        k = (s['layer'], s['datatype'])
        o_cnt[k] += 1
        o_area[k] += _poly_area(s['points'])
    r_cnt, r_area = defaultdict(int), defaultdict(float)
    for l, d, p in redraw_shapes:
        r_cnt[(l, d)] += 1
        r_area[(l, d)] += _poly_area(p)

    print(f"\n  Routing geometry, redrawn vs original top cell '{top}'"
          f" (excluding {n_degenerate} zero-area polygon(s) in the source):")
    print(f"  {'layer':>10}  {'original':>9} {'redrawn':>9} {'delta':>7}   "
          f"{'area':>10}")
    ok = True
    for k in sorted(set(o_cnt) | set(r_cnt)):
        d = r_cnt[k] - o_cnt[k]
        am = 'exact' if abs(r_area[k] - o_area[k]) < 1.0 else \
             f"{(r_area[k] / o_area[k] * 100 if o_area[k] else 0):.1f}%"
        if d or am != 'exact':
            ok = False
        print(f"  {str(k):>10}  {o_cnt[k]:>9} {r_cnt[k]:>9} {d:>+7}   {am:>10}")

    o_tot, r_tot = sum(o_cnt.values()), sum(r_cnt.values())
    print(f"  {'TOTAL':>10}  {o_tot:>9} {r_tot:>9} {r_tot - o_tot:>+7}")

    # Strongest check: the two sets of polygons must match as multisets, so a
    # dropped polygon cannot be masked by a duplicated one of equal area.
    o_ms = Counter((s['layer'], s['datatype'], tuple(s['points']))
                   for s in orig_shapes)
    r_ms = Counter((l, d, tuple(p)) for l, d, p in redraw_shapes)
    missing, extra = o_ms - r_ms, r_ms - o_ms
    print(f"\n  Exact polygon match: {sum(missing.values())} in the layout but "
          f"not in the map, {sum(extra.values())} in the map but not in the "
          f"layout")
    if missing or extra:
        ok = False
        for (l, d, pts), n in list((missing + extra).items())[:5]:
            print(f"    layer ({l},{d}) x{n}: {pts[:3]}...")

    # Device geometry. The routing above is top-level polygons; devices are
    # placed cells, so both sides are flattened through their SREF transforms
    # and compared as multisets. This is what proves a device landed on the
    # right coordinates in the right orientation -- a wrong rotation or a
    # mirrored-the-other-way placement leaves every count identical and shows
    # up only as a geometry difference.
    if redrawn_cells and redrawn_top:
        o_dev = _flatten_placed(cells, top)
        r_dev = _flatten_placed(redrawn_cells, redrawn_top)
        if not o_dev and not r_dev:
            pass                      # neither side places cells; nothing to say
        elif not o_dev:
            print(f"\n  Device geometry: the original defines no placed cell "
                  f"contents in-file, so the {sum(r_dev.values())} redrawn "
                  f"device polygon(s) cannot be diffed against it")
        else:
            # Asymmetric on purpose. Geometry in the redraw that the layout does
            # not have is a real failure -- the map placed a device somewhere it
            # is not, or at the wrong orientation. Geometry in the layout that
            # the redraw lacks is NOT: a layout legitimately places taps, guard
            # rings and fill that are not netlist devices, the same reason the
            # SREF count below is context rather than a target.
            invented = r_dev - o_dev
            reproduced = sum((o_dev & r_dev).values())
            surplus = sum((o_dev - r_dev).values())
            print(f"\n  Device geometry, flattened through each placement: "
                  f"{sum(o_dev.values())} polygon(s) in the original, "
                  f"{sum(r_dev.values())} redrawn")
            print(f"  {reproduced} reproduced exactly; "
                  f"{sum(invented.values())} in the redraw but not the layout; "
                  f"{surplus} in the layout but not owned by the map "
                  f"(taps, guard rings, fill)")
            if invented:
                ok = False
                for (l, d, pts), n in list(invented.items())[:5]:
                    print(f"    layer ({l},{d}) x{n}: {pts[:3]}...")

    # Device placements. The question that matters is whether every netlist
    # device got coordinates -- NOT whether the map has one entry per SREF.
    # A layout legitimately places cells that are not netlist devices (taps,
    # guard rings, fill), so a raw SREF count is context, not a pass/fail.
    devs = pmap.get('devices', [])
    placed = [d for d in devs if d.get('x0_um') is not None]
    unplaced = [d['instance'] for d in devs if d.get('x0_um') is None]
    print(f"\n  Placements: {len(placed)}/{len(devs)} netlist device(s) have "
          f"coordinates ({len(orig['srefs'])} cell instance(s) placed in the "
          f"original, including any non-device cells)")
    if unplaced:
        ok = False
        print(f"    no coordinates for: {', '.join(unplaced)}")
    return ok


def process(design_dir):
    design_dir = os.path.abspath(design_dir)
    map_path = os.path.join(design_dir, 'physical_map.json')
    if not os.path.exists(map_path):
        raise FileNotFoundError(
            f"No physical_map.json in {design_dir} -- run "
            f"extract_physical_info.py first")
    with open(map_path) as f:
        pmap = json.load(f)

    design = pmap.get('design') or os.path.basename(design_dir)
    print(f"\n{'='*60}")
    print(f"Redraw from map: {design}")
    print(f"{'='*60}")

    # Resolve the original up front: it is both the thing verification diffs
    # against and, for ALIGN-style designs, the only place the device cell
    # definitions exist. The map records which GDS it was extracted from, so
    # this cannot drift onto a '_validation' copy or a previous redraw.
    layout = pmap.get('layout')
    orig_path = os.path.join(design_dir, layout) if layout else None
    orig_cells = (parse_gds(orig_path)
                  if orig_path and os.path.exists(orig_path) else {})

    polygons, labels = build_from_map(pmap)
    primitives = load_primitive_cells(design_dir, orig_cells)
    cell_defs, instances, placed, unbound, misaligned, shared = place_devices(
        pmap, primitives)

    n_annot = sum(1 for p in polygons if p[0] == ANNOT_LAYER)
    n_dev_polys = sum(len(p) for _n, p in cell_defs)
    print(f"  routing polygons replayed : {len(polygons) - n_annot}")
    print(f"  device instances placed   : {len(instances)} for "
          f"{len(placed)} device(s) ({len(cell_defs)} unique primitive "
          f"cell(s), {n_dev_polys} polygon(s) of device geometry)")
    for cell, insts in shared:
        print(f"  NOTE: {' + '.join(insts)} share one placed cell '{cell}' "
              f"(matched pair drawn as a single DCL cell), instantiated once")
    print(f"  device boxes drawn        : {n_annot}")
    print(f"  labels                    : {len(labels)}")
    if unbound and not os.path.isdir(os.path.join(design_dir, 'primitives')):
        # One line, not one per device: with no primitives/ at all every device
        # is unbound for the same single reason, and listing them all buries it.
        # Keyed on the folder, not on an empty index -- an ALIGN design has no
        # primitives/ and still binds, from the layout's own cell definitions.
        print(f"  NOTE: no primitives/ folder -- all {len(unbound)} device(s) "
              f"are outline boxes only; the device geometry lives there, not "
              f"in the map")
    else:
        for inst, prim in unbound:
            print(f"  NOTE: no primitive cell geometry for {inst} "
                  f"(map says '{prim}') -- outline box only")
    for inst, prim, got, want in misaligned:
        print(f"  WARNING: {inst} -> {prim} footprint {got[0]}x{got[1]}um does "
              f"not match its recorded box {want[0]}x{want[1]}um")

    out_path = os.path.join(design_dir, f'{design}_redrawn_from_map.gds')
    write_gds(out_path, f'{design}_redrawn', polygons, labels,
              cell_defs=cell_defs, instances=instances)
    print(f"  wrote {out_path} ({os.path.getsize(out_path)} bytes)")

    # Round-trip the file we just wrote: if it cannot be read back, the redraw
    # is not a real GDS regardless of what the numbers say. The top cell is
    # checked on its own -- the device cells carry their own polygon counts,
    # and it is the top cell's routing that the verification below diffs.
    back = parse_gds(out_path)
    top_back = back.get(f'{design}_redrawn', {})
    n_top = len(top_back.get('shapes', []))
    n_srefs = len(top_back.get('srefs', []))
    ok_back = (n_top == len(polygons) and n_srefs == len(instances))
    print(f"  re-parsed OK: {len(back)} cell(s); top cell has {n_top} "
          f"polygon(s) + {n_srefs} instance(s) "
          f"({'matches what was written' if ok_back else 'MISMATCH'})")

    # Verify against the layout extraction actually read (resolved above).
    if not layout:
        print("\n  map has no 'layout' key (written by an older extractor); "
              "re-run extract_physical_info.py to verify against the right GDS")
        return None
    if not orig_cells:
        print(f"\n  original layout {layout} is no longer alongside the map; "
              f"wrote the redraw without verifying it")
        return None
    ok = verify(pmap, orig_cells, polygons,
                redrawn_cells=back, redrawn_top=f'{design}_redrawn')
    print(f"\n  VERDICT: extraction {'VERIFIED' if ok else 'INCOMPLETE'} "
          f"against {layout}")
    return ok


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)
    for d in sys.argv[1:]:
        process(d)
