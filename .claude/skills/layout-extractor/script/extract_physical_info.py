"""
Extract physical information from analog layout primitives and build
a netlist-to-layout device map for AADA example designs.

Supports two layout formats:
  1. Hash-based (miller_ota, ahuja_ota, five_transistors_ota, ota_ff):
       GDS cells named {TYPE}_{HASH}_{Xx_Yy}_{TIMESTAMP}
  2. ALIGN-style (ota_lpf):
       GDS cells named {instance}_{model} with SREF hierarchy

Both a netlist and a layout are REQUIRED: the netlist supplies the device
identities, sizes and net names that the extracted geometry is bound to.
The GDS is looked up first, so a design with no layout at all is told
exactly that rather than something about netlists.

Usage (run from the repo root; <design_dir> is repo-root-relative; one or
more design dirs may be given):
  python .claude/skills/layout-extractor/script/extract_physical_info.py <design_dir>
"""

import os
import re
import sys
import json
import struct
from collections import defaultdict


def _load_pdk():
    """The project's active PDK, from `.claude/reference/pdk_options.json`.

    Imported by path rather than as a package so this script keeps working
    from any working directory, the same way the rest of it does.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    ref = os.path.abspath(os.path.join(here, *([os.pardir] * 3), 'reference'))
    if ref not in sys.path:
        sys.path.insert(0, ref)
    from pdk_config import pdk
    return pdk()


# ---------------------------------------------------------------------------
# GDS parsing
# ---------------------------------------------------------------------------

def _gds_records(data):
    """Yield (rec_type, rec_data) for every record in a GDS2 byte buffer."""
    i = 0
    while i < len(data) - 3:
        length = struct.unpack('>H', data[i:i+2])[0]
        if length < 4:
            i += 4
            continue
        rec_type = struct.unpack('>H', data[i+2:i+4])[0]
        yield rec_type, data[i+4:i+length]
        i += length


def _gds_real(b):
    """Decode a GDS2 8-byte IBM hex floating-point value."""
    sign = -1 if (b[0] & 0x80) else 1
    exp = (b[0] & 0x7F) - 64
    mantissa = int.from_bytes(b[1:], 'big') / (1 << 56)
    return sign * mantissa * (16.0 ** exp)


def _decode(rec_type, rec_data):
    dtype = rec_type & 0xFF
    if dtype == 0x06:
        return rec_data.rstrip(b'\x00').decode('ascii', errors='replace')
    if dtype == 0x02 and len(rec_data) >= 2:
        return [struct.unpack('>h', rec_data[j:j+2])[0] for j in range(0, len(rec_data), 2)]
    if dtype == 0x03 and len(rec_data) >= 4:
        return [struct.unpack('>i', rec_data[j:j+4])[0] for j in range(0, len(rec_data), 4)]
    if dtype == 0x05 and len(rec_data) >= 8:
        return [struct.unpack('>d', rec_data[j:j+8])[0] for j in range(0, len(rec_data), 8)]
    return None


# GDS2 record type constants
RT_STRNAME = 0x0606   # cell name string
RT_ENDSTR  = 0x0700   # end of cell
RT_BOUNDARY= 0x0800   # polygon element start
RT_TEXT    = 0x0C00   # text/label element start
RT_SREF    = 0x0A00   # structure reference start
RT_AREF    = 0x0B00   # array reference start
RT_ENDEL   = 0x1100   # end of element
RT_SNAME   = 0x1206   # name of referenced cell
RT_LAYER   = 0x0D02   # GDS layer number
RT_DATATYPE= 0x0E02   # boundary datatype
RT_TEXTTYPE= 0x1602   # text datatype
RT_STRING  = 0x1906   # text string content
RT_XY      = 0x1003   # coordinates (4-byte ints)
RT_STRANS  = 0x1A01   # transformation flags (bit 15 = reflect X before rotate)
RT_ANGLE   = 0x1C05   # rotation angle (8-byte GDS real, degrees CCW)


def parse_gds(filepath):
    """
    Parse a GDS2 file and return a dict:
      cell_name -> {
          'bbox': (x0, y0, x1, y1) in GDS units (typically nm),
          'srefs': [{'ref': str, 'xy': (x, y), 'angle': int, 'mirror': bool}, ...],
          'shapes': [{'layer': int, 'datatype': int,
                      'points': [(x, y), ...]}, ...]   (native polygons only)
          'texts': [{'layer': int, 'datatype': int,
                     'xy': (x, y), 'text': str}, ...]  (native labels only)
      }
    """
    with open(filepath, 'rb') as f:
        data = f.read()

    cells = {}
    current = None
    polygons_xy = []
    shapes = []
    texts = []
    srefs = []

    elem = None   # 'ref' | 'boundary' | 'text' | None
    ref_name = None
    ref_xy = None
    ref_angle = 0
    ref_mirror = False
    cur_layer = None
    cur_datatype = None
    cur_points = None
    cur_text = None
    cur_text_xy = None

    for rt, rd in _gds_records(data):
        if rt == RT_STRNAME:
            current = _decode(rt, rd)
            polygons_xy = []
            shapes = []
            texts = []
            srefs = []
        elif rt == RT_ENDSTR:
            if current is not None:
                # compute bbox from polygon vertices (skip 1- or 2-point elements)
                xs = [polygons_xy[k] for k in range(0, len(polygons_xy), 2)]
                ys = [polygons_xy[k] for k in range(1, len(polygons_xy), 2)]
                bbox = (min(xs), min(ys), max(xs), max(ys)) if xs else None
                cells[current] = {'bbox': bbox, 'srefs': srefs,
                                   'shapes': shapes, 'texts': texts}
            current = None
        elif rt in (RT_SREF, RT_AREF):
            elem = 'ref'
            ref_name = None
            ref_xy = None
            ref_angle = 0
            ref_mirror = False
        elif rt == RT_BOUNDARY:
            elem = 'boundary'
            cur_layer = None
            cur_datatype = None
            cur_points = None
        elif rt == RT_TEXT:
            elem = 'text'
            cur_layer = None
            cur_datatype = None
            cur_text = None
            cur_text_xy = None
        elif rt == RT_SNAME and elem == 'ref':
            ref_name = _decode(rt, rd)
        elif rt == RT_STRANS and elem == 'ref':
            ref_mirror = bool(struct.unpack('>H', rd[:2])[0] & 0x8000)
        elif rt == RT_ANGLE and elem == 'ref' and len(rd) == 8:
            ref_angle = round(_gds_real(rd))
        elif rt == RT_LAYER and elem in ('boundary', 'text'):
            cur_layer = _decode(rt, rd)[0]
        elif rt == RT_DATATYPE and elem == 'boundary':
            cur_datatype = _decode(rt, rd)[0]
        elif rt == RT_TEXTTYPE and elem == 'text':
            cur_datatype = _decode(rt, rd)[0]
        elif rt == RT_STRING and elem == 'text':
            cur_text = _decode(rt, rd)
        elif rt == RT_XY:
            coords = _decode(rt, rd)
            if elem == 'ref' and coords and ref_xy is None:
                ref_xy = (coords[0], coords[1])
            elif elem == 'boundary' and coords and len(coords) > 4:
                cur_points = [(coords[k], coords[k + 1])
                              for k in range(0, len(coords), 2)]
                polygons_xy.extend(coords)
            elif elem == 'text' and coords:
                cur_text_xy = (coords[0], coords[1])
        elif rt == RT_ENDEL:
            if elem == 'ref' and ref_name:
                srefs.append({'ref': ref_name, 'xy': ref_xy or (0, 0),
                              'angle': ref_angle, 'mirror': ref_mirror})
            elif elem == 'boundary' and cur_points is not None:
                shapes.append({'layer': cur_layer, 'datatype': cur_datatype,
                               'points': cur_points})
            elif elem == 'text' and cur_text_xy is not None:
                texts.append({'layer': cur_layer, 'datatype': cur_datatype,
                              'xy': cur_text_xy, 'text': cur_text})
            elem = None

    return cells


def cell_dimensions_um(bbox, dbu_nm=1.0):
    """Convert a GDS bbox tuple to (width_um, height_um)."""
    if bbox is None:
        return None, None
    x0, y0, x1, y1 = bbox
    return (x1 - x0) * dbu_nm / 1000.0, (y1 - y0) * dbu_nm / 1000.0


def sref_bbox_um(cell_bbox_nm, sref_x_nm, sref_y_nm, angle=0, mirror=False):
    """
    Return absolute bounding box (x0, y0, x1, y1) in µm for a placed cell.

    GDS2 transform order: reflect about X-axis (if mirror), then rotate by angle°
    CCW, then translate to (sref_x_nm, sref_y_nm).
    """
    import math
    if cell_bbox_nm is None:
        return None
    x0c, y0c, x1c, y1c = cell_bbox_nm
    corners = [(x0c, y0c), (x1c, y0c), (x1c, y1c), (x0c, y1c)]
    if mirror:
        corners = [(x, -y) for x, y in corners]
    if angle:
        rad = math.radians(angle)
        ca, sa = math.cos(rad), math.sin(rad)
        corners = [(ca * x - sa * y, sa * x + ca * y) for x, y in corners]
    xs = [x + sref_x_nm for x, _ in corners]
    ys = [y + sref_y_nm for _, y in corners]
    # round to 3 dp (1 nm = GDS resolution); +0.0 converts -0.0 to 0.0
    return (round(min(xs) / 1000.0, 3) + 0.0, round(min(ys) / 1000.0, 3) + 0.0,
            round(max(xs) / 1000.0, 3) + 0.0, round(max(ys) / 1000.0, 3) + 0.0)


# ---------------------------------------------------------------------------
# Netlist (SPICE) parsing
# ---------------------------------------------------------------------------

# Foundry primitive-library marker in a model name -- 'sky130_fd_pr__nfet_01v8',
# 'gf180mcu_fd_pr__nfet_03v3'. Matching the convention rather than one PDK's
# name is what keeps this parser process-independent; a PDK that names its
# primitives differently needs this pattern widened and nothing else.
_PDK = r'\S*' + re.escape(_load_pdk().model_marker)

_MOS_MODEL_RE = re.compile(
    r'^(?P<name>[MCX]\S+)\s+'        # instance name (M/C, or X for subckt calls)
    r'(?P<nets>(?:\S+\s+){3,4})'   # terminal nets (3–4)
    r'(?P<model>' + _PDK + r'[np]fet\S*)\s+'
    r'(?P<params>.*)',
    re.IGNORECASE
)
_CAP_RE = re.compile(
    r'^(?P<name>[CX]\S+)\s+'
    r'(?P<nets>(?:\S+\s+){2})'
    r'(?P<model>' + _PDK + r'cap\S*)\s+'
    r'(?P<params>.*)',
    re.IGNORECASE
)
_RES_RE = re.compile(
    r'^(?P<name>[RX]\S+)\s+'
    r'(?P<nets>(?:\S+\s+){2,3})'
    r'(?P<model>' + _PDK + r'res\S*|resistor)\s*'
    r'(?P<params>.*)',
    re.IGNORECASE
)
# Model-less resistor: 'R0 out1 net12 18k', 'R0 out1 net12 w=0.25 l=45.39',
# or both. Only ever tried after _RES_RE fails and only for an R-prefix line --
# a model-less device of any other type is stopped by the abstract-device gate
# before parsing, so this cannot quietly absorb one. Dropping these instead
# would delete a real device from the map and leave its genuinely-placed cell
# surfacing as an "unclaimed" primitive, which reads as dummy fill.
_RES_NOMODEL_RE = re.compile(
    r'^(?P<name>R\S+)\s+'
    r'(?P<nets>\S+\s+\S+)\s*'
    r'(?P<rest>.*)$',
    re.IGNORECASE
)
_PARAM_RE = re.compile(r'(\w+)\s*=\s*([^\s]+)')

# ---------------------------------------------------------------------------
# Abstract-device gate
# ---------------------------------------------------------------------------
#
# Every device in the netlist must name a real PDK primitive. A line like
#   C1 a b 1p
# names no model at all: it is an ABSTRACT device, an idealized element with
# no PDK cell behind it. Nothing can be bound to it -- there is no primitive
# to match, no geometry to measure, and no way to tell whether a layout cell
# belongs to it. Extraction therefore stops and reports, rather than guessing
# a binding or silently dropping the device (dropping is worse than it looks:
# the device's real placed cell then surfaces as an "unclaimed" primitive and
# reads as dummy fill).
#
# NOTHING HERE IS FATAL. An abstract device is reported and extraction
# continues, because a layout is worth mapping even when its netlist is
# imprecise about what was drawn -- the geometry is on disk either way.
#
# What must NOT happen is dropping the device: a dropped device leaves the map
# short, and its genuinely-placed cell then surfaces as an "unclaimed"
# primitive, which reads as dummy fill and invites a redraw to delete a real
# device. So every abstract device is kept, and how far it can be taken
# depends on what its SPICE prefix alone can establish:
#
#   R / C  -- the prefix names the device type outright, so these bind to a
#             RES_/CAP_ primitive and are measured and placed like any other.
#             A resistor additionally has its body measured from the layout,
#             which is why its w/l survive with no model at all.
#   M / X  -- the prefix says "transistor" but not nfet vs pfet, and polarity
#             is what a primitive is matched on. These are kept with kind
#             'unknown' and left unbound rather than guessed: inventing a
#             polarity would put a real device at fabricated coordinates,
#             which is worse than admitting it is unresolved.
#   others -- L/D/Q/J have no primitive type in this tool at all; kept, unbound.
# Same convention as _PDK above: a device is "real" if its model names a
# foundry primitive library, whichever process that is. From the guideline.
PDK_MODEL_MARKER = _load_pdk().model_marker

# SPICE prefixes whose device type is fully determined without a model.
_KIND_BY_PREFIX = {'R': 'res', 'C': 'cap'}

# SPICE reserves these leading letters for device instantiations. Anything
# else at the start of a line is a directive, comment, or continuation.
_DEVICE_PREFIXES = ('X', 'M', 'R', 'C', 'L', 'D', 'Q', 'J', 'K')


def _joined_lines(filepath):
    """Yield (line_number, text) with SPICE '+' continuations folded in."""
    out = []
    with open(filepath) as f:
        for n, raw in enumerate(f.read().splitlines(), start=1):
            line = raw.strip()
            if line.startswith('+') and out:
                out[-1] = (out[-1][0], out[-1][1] + ' ' + line[1:])
            else:
                out.append((n, line))
    return out


def find_abstract_devices(filepath):
    """Find device lines naming no PDK model.

    Returns [(line_no, text, kind), ...] where kind is what the SPICE prefix
    alone establishes ('res', 'cap') or 'unknown'. None of it is fatal -- see
    the module note above for what each case can and cannot be taken to.
    """
    found = []
    for n, line in _joined_lines(filepath):
        if not line or line.startswith(('*', '.', '+')):
            continue
        prefix = line[0].upper()
        if not prefix.startswith(_DEVICE_PREFIXES):
            continue
        if PDK_MODEL_MARKER in line.lower():
            continue
        found.append((n, line, _KIND_BY_PREFIX.get(prefix, 'unknown')))
    return found


def _parse_params(param_str):
    return {k.lower(): v for k, v in _PARAM_RE.findall(param_str)}


_SI_SUFFIXES = [
    # order matters: 'meg' must be checked before 'm' (SPICE 'm' = milli,
    # 'meg' = mega -- a bare 'm'-suffix check would wrongly match 'meg's
    # leading 'm' first if tried out of this order).
    ('meg', 1e6),
    ('t', 1e12), ('g', 1e9), ('k', 1e3),
    ('m', 1e-3), ('u', 1e-6), ('n', 1e-9), ('p', 1e-12), ('f', 1e-15),
]


def _si_val(s):
    """Convert a SPICE value string to a float -- handles both scientific
    notation ('21.0e-7') and SPICE engineering suffixes ('18k', '5meg',
    '10u'). Plain float() only handles the former: a bare suffixed value
    like a resistor's 'r=18k' silently fails to parse and the whole
    device disappears from params downstream, which is why this needs
    the suffix fallback rather than just wrapping float() in a try."""
    if s is None:
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        pass
    s_lower = s.lower()
    for suffix, mult in _SI_SUFFIXES:
        if s_lower.endswith(suffix):
            try:
                return float(s_lower[:-len(suffix)]) * mult
            except ValueError:
                continue
    return None


_IMPLAUSIBLE_METERS_THRESHOLD = 1e-3  # 1mm -- no real device L/W is this large in meters


def _normalize_length_m(val):
    """Normalize a parsed device L/W value to meters. Netlists in this
    repo use two different conventions: full SI notation already in
    meters (e.g. 'w=21.0e-7'), or bare microns (e.g. 'w=40.7', meaning
    40.7um) -- both are valid SPICE, and _si_val can't tell them apart
    from the string alone. Heuristic: a parsed value implausibly large
    to be a real device dimension in meters (>1mm) is almost certainly
    already in microns, so convert it; otherwise trust it as-is. Without
    this, a bare-micron netlist's values get treated as meters throughout
    the rest of this script (and by redraw_layout.py, which consumes
    these same fields), producing garbage sizes."""
    if val is None:
        return None
    if abs(val) > _IMPLAUSIBLE_METERS_THRESHOLD:
        return val * 1e-6
    return val


def parse_netlist(filepath):
    """
    Return a list of device dicts:
      {name, kind, model, nets, params, bulk_conn_type}
    kind: 'nfet' | 'pfet' | 'cap' | 'res'
    bulk_conn_type: '4T' | 'S' | None  (for MOS only)
    """
    devices = []
    with open(filepath) as f:
        lines = f.read().splitlines()

    # join continuation lines
    joined = []
    for line in lines:
        line = line.strip()
        if line.startswith('+') and joined:
            joined[-1] += ' ' + line[1:]
        else:
            joined.append(line)

    for line in joined:
        if not line or line.startswith(('*', '.')):
            continue

        # MOS transistors
        m = _MOS_MODEL_RE.match(line)
        if m:
            nets = m.group('nets').split()
            model = m.group('model').lower()
            params = _parse_params(m.group('params'))
            kind = 'nfet' if 'nfet' in model else 'pfet'

            # bulk connection type:
            #   SPICE format: M<name> drain gate source bulk model
            #   4T if bulk != source net, S if bulk == source net
            if len(nets) >= 4:
                source_net = nets[2]
                bulk_net = nets[3]
                bulk_type = 'S' if source_net == bulk_net else '4T'
            else:
                bulk_type = 'S'

            devices.append({
                'name': m.group('name'),
                'kind': kind,
                'model': model,
                'nets': nets,
                'params': params,
                'l': _normalize_length_m(_si_val(params.get('l'))),
                'w': _normalize_length_m(_si_val(params.get('w'))),
                'nf': int(params.get('nf', 1)),
                'bulk_type': bulk_type,
                # self-shorted (drain=gate=source) is the tied-off-dummy
                # convention this repo's cells use, e.g.
                # src/glayout/cells/elementary/current_mirror/current_mirror.py's
                # `XDUMMY {dum_node} {dum_node} {dum_node} VB {model} ...`
                'is_dummy': len(nets) >= 3 and nets[0] == nets[1] == nets[2],
            })
            continue

        # Capacitors
        m = _CAP_RE.match(line)
        if m:
            params = _parse_params(m.group('params'))
            devices.append({
                'name': m.group('name'),
                'kind': 'cap',
                'model': m.group('model').lower(),
                'nets': m.group('nets').split(),
                'params': params,
                'l': _normalize_length_m(_si_val(params.get('l'))),
                'w': _normalize_length_m(_si_val(params.get('w'))),
            })
            continue

        # Resistors
        m = _RES_RE.match(line)
        if m:
            params = _parse_params(m.group('params'))
            devices.append({
                'name': m.group('name'),
                'kind': 'res',
                'model': m.group('model').lower(),
                'nets': m.group('nets').split(),
                'params': params,
                'r': _si_val(params.get('r')),
            })
            continue

        # Model-less devices of every other type, kept rather than dropped so
        # the map stays complete and no placed cell turns into a phantom
        # "unclaimed" primitive. Type comes from the SPICE prefix where that
        # settles it; otherwise the device is recorded as 'unknown' and left
        # for the binder to leave alone.
        head = line.split()
        prefix = line[0].upper()
        if prefix in _DEVICE_PREFIXES and prefix not in ('R',) and len(head) >= 3:
            kind = _KIND_BY_PREFIX.get(prefix, 'unknown')
            # Trailing key=value params; everything before them is nets.
            n_params = sum(1 for t in head[1:] if '=' in t)
            nets = head[1:len(head) - n_params] if n_params else head[1:]
            params = _parse_params(line)
            devices.append({
                'name': head[0],
                'kind': kind,
                'model': None,
                'nets': nets,
                'params': params,
                'l': _normalize_length_m(_si_val(params.get('l'))),
                'w': _normalize_length_m(_si_val(params.get('w'))),
                'nf': int(params.get('nf', 1)),
                'bulk_type': None,
                'is_dummy': len(nets) >= 3 and nets[0] == nets[1] == nets[2],
            })
            continue

        # Model-less resistor, kept rather than dropped (see _RES_NOMODEL_RE).
        m = _RES_NOMODEL_RE.match(line)
        if m:
            rest = m.group('rest')
            params = _parse_params(rest)
            # A leading bare token -- one that is not key=value -- is the
            # resistance, the 'R0 a b 18k' form. An explicit r= wins over it.
            head = rest.split()
            bare = _si_val(head[0]) if head and '=' not in head[0] else None
            devices.append({
                'name': m.group('name'),
                'kind': 'res',
                'model': None,          # abstract: warned about by the gate
                'nets': m.group('nets').split(),
                'params': params,
                'r': _si_val(params.get('r')) if 'r' in params else bare,
            })
            continue

    return devices


# ---------------------------------------------------------------------------
# Primitive file catalogue
# ---------------------------------------------------------------------------

_PRIM_HASH_RE = re.compile(
    r'^(?P<type>(?:DCL_)?[NP]MOS(?:_(?:4T|S))?|CAP_2T|RES_2T)'
    r'_(?P<hash>\d+)'
    r'(?:_X(?P<x>\d+)_Y(?P<y>\d+))?'
    r'\.python\.gds$',
    re.IGNORECASE
)

_ALIGN_CELL_RE = re.compile(
    # '<instance>_<model>' where the model names any foundry primitive library,
    # e.g. 'M0_sky130_fd_pr__nfet_01v8', 'M0_gf180mcu_fd_pr__nfet_03v3'.
    r'^(?P<instance>[A-Z][^_]*)_\S*_fd_pr__',
    re.IGNORECASE
)


def catalogue_primitives(prim_dir, placed_cells=None):
    """
    Read all *.gds files in prim_dir and return a list of unique placed
    primitive cells (one entry per unique hash / device type).

    placed_cells: set of base names (e.g. {"NMOS_4T_2945252_X3_Y1", ...})
      taken from the timestamp-suffixed cells in the top-level GDS.
      When provided, only the orientation that was actually used in the
      layout is kept; otherwise the first file for each hash is used.
    """
    # Collect all candidates first
    raw = []
    if not os.path.isdir(prim_dir):
        return raw

    for fname in sorted(os.listdir(prim_dir)):
        if not fname.endswith('.gds'):
            continue
        fpath = os.path.join(prim_dir, fname)
        cells = parse_gds(fpath)
        if not cells:
            continue
        cell_name = next(iter(cells))
        bbox = cells[cell_name]['bbox']
        w, h = cell_dimensions_um(bbox)

        m = _PRIM_HASH_RE.match(fname)
        if m:
            base_name = (
                f"{m.group('type').upper()}_{m.group('hash')}"
                + (f"_X{m.group('x')}_Y{m.group('y')}" if m.group('x') else '')
            )
            raw.append({
                'filename': fname,
                'cell_name': cell_name,
                'base_name': base_name,
                'cell_type': m.group('type').upper(),
                'hash': m.group('hash'),
                'array_x': int(m.group('x') or 1),
                'array_y': int(m.group('y') or 1),
                'bbox': bbox,
                'width_um': w,
                'height_um': h,
            })
        else:
            instance = fname.replace('.gds', '')
            raw.append({
                'filename': fname,
                'cell_name': cell_name,
                'base_name': instance,
                'cell_type': 'DIRECT',
                'instance': instance,
                'hash': None,
                'array_x': 1,
                'array_y': 1,
                'bbox': bbox,
                'width_um': w,
                'height_um': h,
            })

    # For hash-based primitives: keep one entry per (type, hash).
    # Prefer the orientation that was actually placed (from placed_cells).
    seen = {}  # (type, hash) -> entry
    for entry in raw:
        if entry['cell_type'] == 'DIRECT':
            yield_key = entry['base_name']
            if yield_key not in seen:
                seen[yield_key] = entry
            continue
        key = (entry['cell_type'], entry['hash'])
        if key not in seen:
            seen[key] = entry
        elif placed_cells and entry['base_name'] in placed_cells:
            # prefer the orientation that was actually placed
            seen[key] = entry

    return list(seen.values())


# ---------------------------------------------------------------------------
# Mapping: hash-based format
# ---------------------------------------------------------------------------



def build_hash_map(devices, primitives, pairs):
    """
    Build mapping: instance_name -> primitive_entry.

    Two-pass strategy:
      Pass 1 – DCL assignment for matched pairs.
        For each DCL cell type, try to find the matched pair whose nf aligns
        with the DCL cell's array_x (array_x ≈ nf / nf_per_col, typically 2).
        Best-fit matching: sort DCL cells and candidate pairs by nf, zip.
        Pairs that find a DCL cell are removed from the pool.

      Pass 2 – Individual cell assignment for remaining devices (including
        pairs that did NOT get a DCL cell, each treated individually).
        Sort devices and individual-cell primitives by (nf, w) and zip 1:1.
    """
    pairs_map = {}          # instance -> partner name
    for a, b in pairs:
        pairs_map[a] = b
        pairs_map[b] = a

    dev_by_name = {d['name']: d for d in devices}

    # Unique (type, hash) → one primitive entry (orientation already resolved)
    prim_by_cat = defaultdict(list)
    for p in primitives:
        prim_by_cat[p['cell_type']].append(p)

    mapping = {}    # instance_name -> primitive_entry
    assigned = set()

    # --- helper: nf of a device ---
    def dev_nf(dev): return dev.get('nf', 1)

    # --- Pass 1: DCL cells ---
    # Canonical DCL types that might exist: DCL_NMOS_S, DCL_NMOS, DCL_PMOS_S, DCL_PMOS
    # For each DCL type, find pairs of the right polarity/bulk_type.
    # A DCL_NMOS cell has no _S → bulk_type=4T; DCL_NMOS_S → bulk_type=S.
    for dcl_type, dcl_prims in sorted(prim_by_cat.items()):
        if not dcl_type.startswith('DCL_'):
            continue
        rest = dcl_type[4:]  # e.g. NMOS_S, NMOS, PMOS_S, PMOS
        if rest.startswith('NMOS'):
            polarity = 'nfet'
            bt = 'S' if rest.endswith('_S') else '4T'
        else:
            polarity = 'pfet'
            bt = 'S' if rest.endswith('_S') else '4T'

        # Candidate pairs (represented by the lex-first member)
        candidate_pairs = []
        for a, b in pairs:
            if a in assigned or b in assigned:
                continue
            da, db = dev_by_name.get(a), dev_by_name.get(b)
            if da and db and da['kind'] == polarity and da.get('bulk_type') == bt:
                if da.get('nf') == db.get('nf'):   # sanity: same nf for a matched pair
                    candidate_pairs.append((a, b, da))

        if not candidate_pairs:
            continue

        # Sort both by nf so the smallest pair matches the smallest DCL cell
        dcl_prims_s = sorted(dcl_prims, key=lambda p: (p['array_x'], p['hash']))
        candidate_pairs_s = sorted(candidate_pairs, key=lambda t: dev_nf(t[2]))

        for (a, b, _), prim in zip(candidate_pairs_s, dcl_prims_s):
            mapping[a] = prim
            mapping[b] = prim
            assigned.add(a)
            assigned.add(b)

    # --- Pass 2: individual cells for everything else ---
    # Build a flat list of remaining devices and remaining individual primitives.
    individual_types = [t for t in prim_by_cat if not t.startswith('DCL_')]

    remaining_devs = [d for d in devices if d['name'] not in assigned]

    for cat in individual_types:
        # Match device kind/bulk_type to primitive category
        if cat == 'CAP_2T':
            pool = [d for d in remaining_devs if d['kind'] == 'cap']
        elif cat == 'RES_2T':
            pool = [d for d in remaining_devs if d['kind'] == 'res']
        else:
            # e.g. NMOS_4T, NMOS_S, PMOS_4T, PMOS_S, NMOS, PMOS
            rest = cat  # e.g. NMOS_4T or NMOS_S or PMOS_S
            if rest.startswith('NMOS'):
                polarity = 'nfet'
            elif rest.startswith('PMOS'):
                polarity = 'pfet'
            else:
                continue
            bt = '4T' if rest.endswith('_4T') else 'S'
            pool = [d for d in remaining_devs
                    if d['kind'] == polarity and d.get('bulk_type') == bt]

        if not pool:
            continue

        prims_s = sorted(prim_by_cat[cat], key=lambda p: (p['array_x'], p['hash']))
        pool_s = sorted(pool, key=lambda d: (dev_nf(d),
                                              round(d.get('w') or 0, 12)))

        for dev, prim in zip(pool_s, prims_s):
            if dev['name'] not in mapping:
                mapping[dev['name']] = prim
                assigned.add(dev['name'])

    # --- Pass 3: fallback — use any unclaimed primitive (incl. DCL) ---
    # Build set of primitives already claimed so we don't double-assign.
    claimed_prims = {id(p) for p in mapping.values() if p}

    def type_compatible(dev, prim_type):
        """Return True if prim_type can host this device (lenient match)."""
        kind = dev['kind']
        if kind == 'cap':
            return 'CAP' in prim_type
        if kind == 'res':
            return 'RES' in prim_type
        if kind not in ('nfet', 'pfet'):
            # Model-less M/X: the prefix says transistor but not which
            # polarity, and polarity is exactly what this matches on. Refuse
            # to bind rather than guess -- a wrong guess would hand back
            # fabricated coordinates for a real device.
            return False
        polarity = 'NMOS' if kind == 'nfet' else 'PMOS'
        return polarity in prim_type

    unclaimed = [p for p in primitives if id(p) not in claimed_prims]
    unclaimed_s = sorted(unclaimed, key=lambda p: (p['array_x'], p['hash']))

    for dev in sorted(devices, key=lambda d: (dev_nf(d), d['name'])):
        if mapping.get(dev['name']) is not None:
            continue
        for p in unclaimed_s:
            if type_compatible(dev, p['cell_type']) and id(p) not in claimed_prims:
                mapping[dev['name']] = p
                claimed_prims.add(id(p))
                break
        else:
            mapping[dev['name']] = None

    return mapping


# ---------------------------------------------------------------------------
# Mapping: ALIGN-style format (ota_lpf)
# ---------------------------------------------------------------------------

def build_align_map(devices, gds_cells):
    """
    In ALIGN format the GDS library has cells named {INSTANCE}_{model}.
    Extract placement (xy) from the SREFs in the top assembly cell.
    """
    # top cell is the one with the most SREFs
    top_cell = max(gds_cells.items(), key=lambda kv: len(kv[1]['srefs']))[0]
    srefs = gds_cells[top_cell]['srefs']

    # build instance -> cell_name map from SREF targets
    inst_cell = {}
    for sref in srefs:
        ref = sref['ref']
        # cell name like M0_sky130_fd_pr__nfet_01v8 → instance = M0
        m = _ALIGN_CELL_RE.match(ref)
        if m:
            inst = m.group('instance')
            inst_cell[inst] = ref

    mapping = {}
    for dev in devices:
        name = dev['name']
        cell_name = inst_cell.get(name)
        if cell_name and cell_name in gds_cells:
            cell = gds_cells[cell_name]
            w, h = cell_dimensions_um(cell['bbox'])
            # placement from SREF in top cell
            place_xy = next(
                (s['xy'] for s in srefs if s['ref'] == cell_name), None
            )
            mapping[name] = {
                'cell_name': cell_name,
                'width_um': w,
                'height_um': h,
                'place_xy_nm': place_xy,
                'place_um': (place_xy[0]/1000, place_xy[1]/1000) if place_xy else None,
            }
        else:
            mapping[name] = None
    return mapping


# ---------------------------------------------------------------------------
# Main driver
# ---------------------------------------------------------------------------

def detect_format(prim_dir, gds_cells):
    """Return 'align' or 'hash'."""
    # ALIGN format has cells named {instance}_{model} in the GDS
    for name in gds_cells:
        if _ALIGN_CELL_RE.match(name):
            return 'align'

    # Fallback: check primitive file names
    if os.path.isdir(prim_dir):
        for f in os.listdir(prim_dir):
            if _PRIM_HASH_RE.match(f):
                return 'hash'
    return 'hash'


# ---------------------------------------------------------------------------
# Routing / net extraction
# ---------------------------------------------------------------------------
#
# Routing-stack layer numbers and the via layers that bridge adjacent metals.
# GDS layer numbers are process assignments -- nothing in a netlist implies
# them -- so they are NOT literals here: they come from the project-wide PDK
# guideline, `.claude/reference/pdk_options.json`, whose "selected" key names
# the one active process. Retargeting is an edit to that file, not to this one.
_PDK_CFG = _load_pdk()
_PDK_CFG.require_layers('routing extraction')
ROUTING_LAYERS = _PDK_CFG.routing_layers
VIA_LINKS = _PDK_CFG.via_links
LABEL_DATATYPE = _PDK_CFG.label_datatype

# How far a port label's anchor point may sit from the drawing-layer shape it
# names. Module-level so the netlist-bound and layout-only paths cannot drift
# apart on it.
LABEL_MATCH_TOL_NM = 5000

# Placed hash-format cells carry a trailing 10+ digit timestamp; this strips it
# back to the base cell name that primitives/ files are catalogued under.
_TS_STRIP_RE = re.compile(r'^(.+?)_\d{10,}$')

# Netlist terminal order (SPICE positional) -> role name, per device kind.
_MOS_TERMS = ('drain', 'gate', 'source', 'bulk')
_CAP_TERMS = ('plus', 'minus')
_RES_TERMS = ('p', 'n')


def _terminal_names(kind):
    if kind in ('nfet', 'pfet'):
        return _MOS_TERMS
    if kind == 'cap':
        return _CAP_TERMS
    if kind == 'unknown':
        # Type unresolved, so terminal roles are unknowable. Positional names
        # keep endpoints usable without asserting which one is a gate.
        return ('t0', 't1', 't2', 't3')
    return _RES_TERMS


# ---------------------------------------------------------------------------
# Resistor body measurement
# ---------------------------------------------------------------------------
#
# A resistor's real drawn dimensions are NOT its placement bounding box: that
# box is a placement site including routing margin (the same reason a netlist
# 24x24um mimcap sits in a 28.38x31.5um box). Measuring w/l from the box would
# report a mismatch for essentially every device. The body has to be measured
# from the resistor-marker geometry inside the primitive cell instead.
#
# The marker for a resistor body is a datatype on the conducting
# layer it is drawn in (e.g. poly 66/13, li1 67/13), co-extensive with the
# 20-datatype drawing polygons. That marker is what identifies "this metal is
# a resistor" to the PDK, so it is the right thing to measure.
RES_MARKER_DATATYPE = _PDK_CFG.resistor_marker_datatype

# A serpentine resistor's body is long strips joined by short links. Anything
# whose long dimension reaches this fraction of the longest polygon's is a
# strip; the rest are links. Aspect ratio alone does NOT separate them - the
# links are themselves elongated (e.g. 0.68 x 0.25um, aspect 2.7).
_RES_STRIP_LENGTH_FRACTION = 0.5


def measure_resistor_body(cell):
    """Measure a resistor primitive cell's drawn body from its marker layer.

    Returns None when the cell carries no resistor marker at all (nothing to
    measure - say so rather than guessing from the bounding box). Otherwise a
    dict of the strip geometry and an estimated square count.
    """
    marked = [s for s in cell.get('shapes', [])
              if s.get('datatype') == RES_MARKER_DATATYPE]
    if not marked:
        return None

    dims = []
    for s in marked:
        x0, y0, x1, y1 = _shape_bbox(s['points'])
        w, h = (x1 - x0) / 1000.0, (y1 - y0) / 1000.0
        dims.append((min(w, h), max(w, h)))   # (short, long) in um
    if not dims:
        return None

    longest = max(d[1] for d in dims)
    strips = [d for d in dims if d[1] >= _RES_STRIP_LENGTH_FRACTION * longest]
    links = [d for d in dims if d not in strips]
    if not strips:
        return None

    strip_w = min(d[0] for d in strips)
    strip_l = max(d[1] for d in strips)
    squares = sum(d[1] / d[0] for d in strips if d[0] > 0)

    layers = sorted({s['layer'] for s in marked})
    return {
        'body_layer': [layers[0], RES_MARKER_DATATYPE] if len(layers) == 1
                      else [[l, RES_MARKER_DATATYPE] for l in layers],
        'n_body_polygons': len(marked),
        'n_strips': len(strips),
        'n_links': len(links),
        'w_um': round(strip_w, 4),
        'l_um': round(strip_l, 4),
        'squares_est': round(squares, 2),
        'note': ('w_um/l_um are one strip; squares_est sums length/width over '
                 'all strips and applies no corner-square correction at the '
                 'serpentine links, so it is a lower bound on total squares. '
                 'Converting squares to ohms needs a PDK sheet resistance, '
                 'which this tool deliberately does not carry.'),
    }


def _resistor_param_reconciliation(dev, measured):
    """Compare a resistor's netlist-declared geometry against the layout's.

    Returns (verdict, netlist_geometry_um, effective_params). The netlist's own
    numbers are never discarded - the caller keeps them under 'netlist_params'
    - but the EFFECTIVE w/l follow the layout whenever the two disagree or the
    netlist declared no geometry at all, since the layout is the physical
    truth about what was actually drawn.
    """
    raw = dev.get('params') or {}
    # Resistor w/l in these netlists are bare numbers in microns.
    nl = {}
    for key in ('w', 'l'):
        val = _si_val(raw.get(key)) if raw.get(key) is not None else None
        if val is not None:
            nl[key] = round(val, 4)

    if measured is None:
        return 'no_layout_measurement', (nl or None), None
    if not nl:
        return 'no_netlist_geometry', None, {'w': measured['w_um'],
                                             'l': measured['l_um']}

    def close(a, b):
        return abs(a - b) <= 0.01 * max(abs(a), abs(b), 1e-12)

    if close(nl.get('w', -1), measured['w_um']) and \
       close(nl.get('l', -1), measured['l_um']):
        return 'match', nl, None
    return 'mismatch', nl, {'w': measured['w_um'], 'l': measured['l_um']}


def _shape_bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _boxes_touch(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


class _UnionFind:
    def __init__(self, n):
        self.parent = list(range(n))

    def find(self, x):
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _trace_islands(shapes, boxes):
    """Group native top-cell routing shapes into electrically-connected
    'wire islands' via same-layer bbox overlap, bridged across metals
    through via-layer shapes."""
    n = len(shapes)
    uf = _UnionFind(n)

    by_layer = defaultdict(list)
    for i, s in enumerate(shapes):
        by_layer[(s['layer'], s['datatype'])].append(i)

    for lay in ROUTING_LAYERS:
        idxs = by_layer.get((lay, 20), [])
        for a in range(len(idxs)):
            ia = idxs[a]
            for b in range(a + 1, len(idxs)):
                ib = idxs[b]
                if _boxes_touch(boxes[ia], boxes[ib]):
                    uf.union(ia, ib)

    for via_key, (lo, hi) in VIA_LINKS.items():
        for v in by_layer.get(via_key, []):
            for i in by_layer.get((lo, 20), []):
                if _boxes_touch(boxes[v], boxes[i]):
                    uf.union(v, i)
            for i in by_layer.get((hi, 20), []):
                if _boxes_touch(boxes[v], boxes[i]):
                    uf.union(v, i)

    groups = defaultdict(list)
    for i in range(n):
        groups[uf.find(i)].append(i)
    return list(groups.values())


def _transform_points(points, sref_x, sref_y, angle=0, mirror=False):
    """Apply a GDS2 SREF transform (mirror about X, then rotate, then
    translate) to a list of (x, y) points - same convention as
    sref_bbox_um, generalized from corners to arbitrary point lists."""
    import math
    if mirror:
        points = [(x, -y) for x, y in points]
    if angle:
        rad = math.radians(angle)
        ca, sa = math.cos(rad), math.sin(rad)
        points = [(ca * x - sa * y, sa * x + ca * y) for x, y in points]
    return [(x + sref_x, y + sref_y) for x, y in points]


def _extract_named_net_cells(gds_cells, top_cell_name):
    """
    ALIGN-style layouts (e.g. ota_lpf) group each net's entire routing into
    its own dedicated cell named 'NET_<netname>', referenced once via SREF
    from the top cell - so the net name and its physical geometry are both
    directly available with no connectivity tracing or guessing needed.

    Returns {net_name: [{'layer': [l, dt], 'points_um': [[x, y], ...]}, ...]}
    or {} if this design doesn't use the convention.
    """
    top = gds_cells.get(top_cell_name, {})
    out = {}
    for sref in top.get('srefs', []):
        ref = sref['ref']
        if not ref.startswith('NET_'):
            continue
        net_name = ref[len('NET_'):]
        cell = gds_cells.get(ref)
        if not cell:
            continue
        segs = []
        for s in cell.get('shapes', []):
            pts = _transform_points(s['points'], sref['xy'][0], sref['xy'][1],
                                     sref.get('angle', 0), sref.get('mirror', False))
            segs.append({
                'layer': [s['layer'], s['datatype']],
                'points_um': [[round(x / 1000.0, 4), round(y / 1000.0, 4)]
                              for x, y in pts],
            })
        out[net_name] = segs
    return out


def extract_routing(gds_cells, top_cell_name, devices_with_coords):
    """
    Map each netlist net to its physical wiring geometry.

    Endpoints (which device terminal sits on which net) come directly from
    the netlist's positional terminal ordering - no geometric drain/source
    disambiguation needed. For the physical segments, two strategies are
    used depending on layout style:

      1. ALIGN-style layouts pre-group each net's routing into a dedicated
         'NET_<netname>' cell - used directly when present (source='named_cell').
      2. Otherwise (hash-based layouts), top-level routing shapes are traced
         into electrically-connected "wire islands" via same-layer overlap
         plus via-layer bridging, then matched to a net name either by a
         top-level port label (source='label') or by which devices' bboxes
         the island's shapes touch (source='matched').

    Returns a list of net dicts:
      {name, source ('named_cell'|'label'|'matched'|'unmatched'),
       endpoints: [{instance, terminal, terminal_index}, ...],
       segments: [{layer: [l, dt], points_um: [[x, y], ...]}, ...]}
    """
    # net name -> set(device instances), and net -> ordered endpoint list
    net_devices = defaultdict(set)
    net_endpoints = defaultdict(list)
    for dev in devices_with_coords:
        names = _terminal_names(dev['kind'])
        for idx, net in enumerate(dev['nets']):
            role = names[idx] if idx < len(names) else f'term{idx}'
            net_devices[net].add(dev['instance'])
            net_endpoints[net].append({
                'instance': dev['instance'], 'terminal': role,
                'terminal_index': idx,
            })

    named_nets = _extract_named_net_cells(gds_cells, top_cell_name)
    if named_nets:
        nets_out = []
        for net in sorted(net_devices):
            nets_out.append({
                'name': net,
                'source': 'named_cell' if net in named_nets else 'unmatched',
                'endpoints': net_endpoints[net],
                'segments': named_nets.get(net, []),
            })
        return nets_out

    top = gds_cells.get(top_cell_name, {})
    # keep only real routing-stack shapes (drawing layers + via layers) -
    # non-routing layers like die-outline/marker boxes (e.g. (104,0), (235,5))
    # are large, untouched-by-union singleton "islands" whose bbox can
    # spuriously overlap every device, so they must be excluded up front
    # rather than relying on the matching pass to ignore them.
    _relevant_lds = {(lay, 20) for lay in ROUTING_LAYERS} | set(VIA_LINKS)
    shapes = [s for s in top.get('shapes', [])
              if (s['layer'], s['datatype']) in _relevant_lds]
    texts = top.get('texts', [])
    boxes = [_shape_bbox(s['points']) for s in shapes]

    islands = _trace_islands(shapes, boxes)
    shape_island_of = {}
    for isl_idx, members in enumerate(islands):
        for m in members:
            shape_island_of[m] = isl_idx

    # device bbox in GDS units (nm) for shape-overlap testing
    dev_boxes = []
    for dev in devices_with_coords:
        if dev.get('x0_um') is None:
            continue
        dev_boxes.append((
            dev['instance'],
            (int(round(dev['x0_um'] * 1000)), int(round(dev['y0_um'] * 1000)),
             int(round(dev['x1_um'] * 1000)), int(round(dev['y1_um'] * 1000))),
        ))

    island_net = {}   # island index -> (net_name, source)

    # Pass 1: islands that sit under a top-level port label (e.g. OUT, VDD).
    # Label anchor points are not reliably inside, or even on the same metal
    # number as, the routing polygon they name (glayout drops the label at
    # the pin location, which can be a layer transition away from where the
    # label's own nominal layer field points) - so search the nearest
    # drawing-layer (datatype 20) shape across ALL routing layers, within a
    # small tolerance, rather than restricting to the label's layer number.
    drawing_shape_idxs = [i for i, s in enumerate(shapes) if s['datatype'] == 20]
    for t in texts:
        px, py = t['xy']
        best_i, best_d = None, None
        for i in drawing_shape_idxs:
            bx = boxes[i]
            dx = max(bx[0] - px, 0, px - bx[2])
            dy = max(bx[1] - py, 0, py - bx[3])
            d = dx * dx + dy * dy
            if best_d is None or d < best_d:
                best_d, best_i = d, i
        if best_i is not None and best_d <= LABEL_MATCH_TOL_NM ** 2:
            isl_idx = shape_island_of.get(best_i)
            if isl_idx is not None and isl_idx not in island_net:
                island_net[isl_idx] = (t['text'], 'label')

    # Pass 2: remaining islands matched by which devices' bboxes they touch
    for isl_idx, members in enumerate(islands):
        if isl_idx in island_net:
            continue
        touched = set()
        for m in members:
            bx = boxes[m]
            for inst, dbox in dev_boxes:
                if _boxes_touch(bx, dbox):
                    touched.add(inst)
        if not touched:
            continue
        best_net, best_score = None, 0.0
        for net, dset in net_devices.items():
            if not dset:
                continue
            inter = len(touched & dset)
            if inter == 0:
                continue
            score = inter / len(touched | dset)
            if touched <= dset:
                score += 1.0   # prefer islands whose touch-set is a subset
            if score > best_score:
                best_score, best_net = score, net
        if best_net:
            island_net[isl_idx] = (best_net, 'matched')

    # assemble segments per net, preferring 'label' provenance if any island
    # of that net was label-matched
    net_segments = defaultdict(list)
    net_source = {}
    for isl_idx, (net, src) in island_net.items():
        for m in islands[isl_idx]:
            s = shapes[m]
            net_segments[net].append({
                'layer': [s['layer'], s['datatype']],
                'points_um': [[round(x / 1000.0, 4), round(y / 1000.0, 4)]
                              for x, y in s['points']],
            })
        if net not in net_source or src == 'label':
            net_source[net] = src

    nets_out = []
    for net in sorted(net_devices):
        nets_out.append({
            'name': net,
            'source': net_source.get(net, 'unmatched'),
            'endpoints': net_endpoints[net],
            'segments': net_segments.get(net, []),
        })
    return nets_out


def _write_physical_map(design_dir, payload):
    """Single writer for the physical map."""
    out_path = os.path.join(design_dir, 'physical_map.json')
    with open(out_path, 'w') as f:
        json.dump(payload, f, indent=2)
    print(f"\nSaved: {out_path}")
    return payload['devices']


def process_design(design_dir):
    # locate files
    design_dir = os.path.abspath(design_dir)
    files = os.listdir(design_dir)

    # Find the netlist (numbered version like <design>_3.sp preferred). It is
    # OPTIONAL: without it this still extracts placement + routing, which is
    # the layout-only mode below. Only the GDS is a hard requirement, so the
    # GDS is what gets checked first -- a design with no layout at all should
    # not be told its netlist is missing.
    sp_files = [f for f in files if f.endswith('.sp') and re.search(r'_\d+\.sp$', f)]
    if not sp_files:
        sp_files = [f for f in files if f.endswith('.sp')]
    if not sp_files:
        # The lookup above is non-recursive, but design-sheets-intake puts the
        # netlist in <design_dir>/netlist/. Naming what was found one level
        # down turns "no netlist" into an actionable message instead of a
        # claim that is simply false for every intake-produced design.
        stray = []
        for sub in sorted(os.listdir(design_dir)):
            subdir = os.path.join(design_dir, sub)
            if os.path.isdir(subdir) and sub != 'primitives':
                stray += [os.path.join(sub, f) for f in sorted(os.listdir(subdir))
                          if f.endswith('.sp')]
        hint = (f" -- but {len(stray)} netlist(s) exist in subfolders: "
                f"{', '.join(stray)}. This tool does not search subfolders; "
                f"copy or symlink the intended one to the design dir root."
                if stray else '')
        raise FileNotFoundError(
            f"No .sp netlist at the top level of {design_dir}{hint}")
    sp_path = os.path.join(design_dir, sorted(sp_files)[-1])

    # find GDS. Exclude '_redrawn'/'validation' variants -- neither is the
    # canonical placed layout this tool reverse-engineers a device map
    # from ('_redrawn' is a from-scratch glayout regeneration, e.g. from
    # redraw_layout.py; 'validation' is a DRC/LVS check copy). Sort what's
    # left so the pick is deterministic -- os.listdir() order is NOT
    # stable, and picking arbitrarily among multiple real candidates
    # silently produces garbage (wrong primitive orientations, missing
    # coordinates) rather than an error.
    gds_files = sorted(
        f for f in files
        if f.upper().endswith('.GDS')
        and 'validation' not in f.lower()
        and 'redrawn' not in f.lower()
    )
    if not gds_files:
        raise FileNotFoundError(f"No .gds layout found in {design_dir}")
    if len(gds_files) > 1:
        print(f"  warning: multiple candidate GDS files {gds_files}, "
              f"using {gds_files[0]} -- disambiguate if this is wrong")
    gds_path = os.path.join(design_dir, gds_files[0])

    # pairs
    pairs_path = os.path.join(design_dir, 'pairs.json')
    pairs = []
    if os.path.exists(pairs_path):
        with open(pairs_path) as f:
            pairs = json.load(f).get('pairs', [])

    prim_dir = os.path.join(design_dir, 'primitives')

    # Gate: every device must name a real PDK primitive. Checked before any
    # output, so a design that cannot be extracted never prints a
    # success-shaped header, and an abstract device is reported as itself
    # rather than surfacing later as a missing device plus an unexplained
    # "unclaimed" primitive cell.
    abstract = find_abstract_devices(sp_path)

    print(f"\n{'='*60}")
    print(f"Design : {os.path.basename(design_dir)}")
    print(f"Netlist: {os.path.basename(sp_path)}")
    print(f"Layout : {os.path.basename(gds_path)}")
    print(f"{'='*60}")

    if abstract:
        # Warned, never fatal. Extraction continues and every one of these is
        # kept in the map; what differs is how far each can be taken, which is
        # stated per line rather than left for the reader to infer.
        n_typed = sum(1 for _n, _t, k in abstract if k != 'unknown')
        print(f"\nWARNING: {len(abstract)} device(s) name no process primitive "
              f"(no '*{PDK_MODEL_MARKER}*' model), so nothing in the netlist "
              f"confirms what was drawn. Extracted anyway, none dropped:")
        for n, t, k in abstract:
            note = ('bound by prefix; geometry from the layout' if k != 'unknown'
                    else 'kind unresolved (no polarity without a model) -- '
                         'kept, left unbound')
            print(f"    line {n}: {t}\n        -> {k}: {note}")
        if n_typed < len(abstract):
            print("    Name the real PDK device to bind the unresolved one(s); "
                  "their coordinates cannot be recovered from the netlist alone.")

    # parse
    devices = parse_netlist(sp_path)
    gds_cells = parse_gds(gds_path)

    # build base_name → timestamp_cell_name map (strip 10-digit timestamp suffix)
    _TS_RE = _TS_STRIP_RE
    base_to_ts = {}
    for cname in gds_cells:
        m = _TS_RE.match(cname)
        if m:
            base_to_ts[m.group(1)] = cname
    placed_cells = set(base_to_ts.keys())

    primitives = catalogue_primitives(prim_dir, placed_cells=placed_cells)
    fmt = detect_format(prim_dir, gds_cells)

    print(f"Format : {fmt}")
    print(f"Devices in netlist : {len(devices)}")
    print(f"Primitive cells    : {len(primitives)}")
    print(f"Matched pairs      : {pairs}")

    # build map
    if fmt == 'align':
        mapping = build_align_map(devices, gds_cells)
    else:
        mapping = build_hash_map(devices, primitives, pairs)

    # build SREF lookup for coordinate extraction
    # top cell = non-timestamp cell with most SREFs
    top_cell_name = max(
        (c for c in gds_cells if not _TS_RE.match(c)),
        key=lambda c: len(gds_cells[c]['srefs']),
    )
    sref_by_ref = {s['ref']: s for s in gds_cells[top_cell_name]['srefs']}

    # report
    print(f"\n{'Instance':<8} {'Kind':<5} {'Model/Type':<35} {'Params':<28} "
          f"{'Primitive Cell':<45} {'W(µm)':>7} {'H(µm)':>7}  "
          f"{'X0(µm)':>8} {'Y0(µm)':>8} {'X1(µm)':>8} {'Y1(µm)':>8}")
    print('-' * 175)

    results = []
    res_notes = []
    for dev in sorted(devices, key=lambda d: d['name']):
        name = dev['name']
        kind = dev['kind']

        # format device params
        if kind in ('nfet', 'pfet'):
            param_str = (f"l={dev['l']*1e9:.0f}n  w={dev['w']*1e9:.0f}n  "
                         f"nf={dev['nf']}  bulk={dev['bulk_type']}")
            model_short = 'nfet_01v8' if 'nfet' in kind else 'pfet_01v8'
        elif kind == 'cap':
            l_um = f"{dev['l']*1e6:.1f}µ" if dev.get('l') else '?'
            w_um = f"{dev['w']*1e6:.1f}µ" if dev.get('w') else '?'
            param_str = f"l={l_um}  w={w_um}"
            model_short = dev['model'] and 'cap_mim_m3_1' or '(value)'
        elif kind not in ('res',):
            # Model-less M/X: type unresolved, so report it as such instead of
            # falling through to the resistor branch and mislabelling it.
            param_str = ' '.join(f"{k}={v}" for k, v in dev['params'].items()) \
                or '(no params)'
            model_short = '(unresolved)'
        else:
            # A bare-value resistor ('R0 a b 18k') carries its resistance as
            # the parsed 'r', not as a key=value param, so fall back to it.
            r_disp = dev['params'].get('r', dev.get('r'))
            param_str = f"r={'?' if r_disp is None else r_disp}"
            # model is None for a model-less resistor, which is a real device
            # here rather than a parse failure -- label it as what it is.
            model_short = (dev['model'] or '(value)')[:20]

        entry = mapping.get(name)
        if entry is None:
            prim_cell = '(no match)'
            w_str = h_str = '-'
        elif fmt == 'align':
            prim_cell = entry.get('cell_name', '?')
            w_str = f"{entry['width_um']:.2f}" if entry['width_um'] else '-'
            h_str = f"{entry['height_um']:.2f}" if entry['height_um'] else '-'
        else:
            prim_cell = entry.get('cell_name', entry.get('filename', '?'))
            w_str = f"{entry['width_um']:.2f}" if entry['width_um'] else '-'
            h_str = f"{entry['height_um']:.2f}" if entry['height_um'] else '-'

        # compute absolute bounding box coordinates, and keep the SREF's own
        # orientation -- it is needed for the bbox math either way, and a
        # consumer redrawing this device needs to know it was placed rotated
        # or mirrored (layout-fixer.md expects both in this file).
        coords = None
        orient = None
        placed_cell = None
        if entry is not None:
            if fmt == 'hash':
                placed_cell = base_to_ts.get(entry.get('base_name', ''))
            else:  # align
                placed_cell = entry.get('cell_name', '')
            if placed_cell:
                sref = sref_by_ref.get(placed_cell)
                cell_bbox = gds_cells.get(placed_cell, {}).get('bbox')
                if sref and cell_bbox:
                    orient = (sref.get('angle', 0), sref.get('mirror', False))
                    coords = sref_bbox_um(cell_bbox,
                                          sref['xy'][0], sref['xy'][1],
                                          *orient)

        if coords:
            x0, y0, x1, y1 = coords
            coord_str = f"{x0:>8.3f} {y0:>8.3f} {x1:>8.3f} {y1:>8.3f}"
        else:
            coord_str = f"{'n/a':>8} {'':>8} {'':>8} {'':>8}"

        print(f"{name:<8} {kind:<5} {model_short:<35} {param_str:<28} "
              f"{prim_cell:<45} {w_str:>7} {h_str:>7}  {coord_str}")

        row = {
            'instance': name,
            'kind': kind,
            'params': {k: dev.get(k) for k in ('l','w','nf','bulk_type','r')
                       if dev.get(k) is not None},
            'nets': dev['nets'],
            'primitive_cell': prim_cell if entry else None,
            'width_um': float(w_str) if w_str != '-' else None,
            'height_um': float(h_str) if h_str != '-' else None,
            'is_dummy': dev.get('is_dummy', False),
            'rotation_deg': orient[0] if orient else None,
            'mirror': bool(orient[1]) if orient else None,
        }

        # Resistors: the layout is the physical truth about what was drawn, so
        # reconcile the netlist's declared geometry against the body actually
        # measured in the placed cell, and let the layout win where they
        # disagree. The netlist's own numbers are preserved, never discarded.
        if kind == 'res':
            measured = measure_resistor_body(gds_cells.get(placed_cell, {})) \
                if placed_cell else None
            verdict, nl_geom, effective = _resistor_param_reconciliation(dev, measured)
            row['layout_params'] = measured
            row['netlist_params'] = nl_geom
            row['param_source'] = 'layout' if effective else 'netlist'
            row['param_reconciliation'] = verdict
            # 'params' always carries the effective w/l, whichever source won.
            # A resistor's geometry lives in the netlist's trailing key=value
            # params rather than as top-level keys (unlike a MOS), so without
            # this it would be missing from 'params' entirely on a match.
            row['params'] = dict(row['params'], **(effective or nl_geom or {}))
            if effective:
                res_notes.append((name, verdict, nl_geom, effective))

        if coords:
            x0, y0, x1, y1 = coords
            row['x0_um'] = round(x0, 4)
            row['y0_um'] = round(y0, 4)
            row['x1_um'] = round(x1, 4)
            row['y1_um'] = round(y1, 4)
        results.append(row)

    if res_notes:
        print(f"\nResistor parameters taken from the layout ({len(res_notes)} "
              f"device(s) -- the netlist's own values are kept under "
              f"'netlist_params'):")
        for name, verdict, nl_geom, eff in res_notes:
            was = (f"netlist w={nl_geom.get('w')} l={nl_geom.get('l')}"
                   if nl_geom else "netlist declared no geometry")
            print(f"  {name:8s} {verdict:22s} {was} -> layout "
                  f"w={eff['w']}um l={eff['l']}um")

    # trace device-to-device wiring (nets) from the top cell's native routing
    nets = extract_routing(gds_cells, top_cell_name, results)
    n_named = sum(1 for n in nets if n['source'] == 'named_cell')
    n_label = sum(1 for n in nets if n['source'] == 'label')
    n_matched = sum(1 for n in nets if n['source'] == 'matched')
    n_unmatched = sum(1 for n in nets if n['source'] == 'unmatched')
    print(f"\nNets traced: {len(nets)} total "
          f"({n_named} via named net cell, {n_label} via port label, "
          f"{n_matched} via device match, {n_unmatched} unrouted/unmatched)")

    # dummy-device check, two independent signals:
    #   1. netlist-side: a MOS device that is self-shorted (drain=gate=source)
    #      is electrically inert - this repo's cells tie off dummy fingers
    #      this way (see the 'is_dummy' comment in parse_netlist above).
    #   2. GDS-side (hash format only, i.e. a primitives/ dir was supplied):
    #      any cataloged primitive cell that never got claimed by a netlist
    #      device is layout-only fill with no netlist counterpart - most
    #      often an LOD/WPE matching dummy finger placed purely for the
    #      layout's benefit. ALIGN-format designs have no primitives/ dir
    #      (devices are read directly from the main GDS's SREF library), so
    #      this signal is empty there by construction, not because nothing
    #      was found.
    netlist_dummies = [d['name'] for d in devices if d.get('is_dummy')]
    claimed_filenames = {v.get('filename') for v in mapping.values() if v}
    unclaimed_primitives = (
        [p['filename'] for p in primitives if p['filename'] not in claimed_filenames]
        if fmt == 'hash' else []
    )
    dummy_check = {
        'netlist_dummy_devices': netlist_dummies,
        'unclaimed_primitive_cells': unclaimed_primitives,
    }
    print(f"\nDummy check: {len(netlist_dummies)} netlist-side dummy device(s) "
          f"(drain=gate=source), {len(unclaimed_primitives)} unclaimed "
          f"primitive cell(s) in primitives/ (possible layout-only dummies)")

    # write JSON
    return _write_physical_map(design_dir, {
        'design': os.path.basename(design_dir),
        'netlist': os.path.basename(sp_path),
        'layout': os.path.basename(gds_path),
        'format': fmt,
        'pairs': pairs,
        'devices': results,
        'nets': nets,
        'dummy_check': dummy_check,
    })


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    for design_dir in sys.argv[1:]:
        process_design(design_dir)