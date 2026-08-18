import math
import os
import re
from typing import Optional, Union

import gdsfactory as gf
# Real, confirmed necessary, not optional -- this whole toolchain's geometry
# generation is NOT thread-safe against gdsfactory's default n_threads=8
# (parallel Component construction): confirmed elsewhere in this project
# (../skills/placer/SKILL.md's Step 1) via byte-different GDS and different
# DRC violation counts from an identical invocation with threading on, and
# re-confirmed directly here -- running this file's own __main__ without
# this line produced a real LVS pin-matching failure ("VSS" not found on
# the expected node) that a fresh, otherwise-identical run with this line
# set did NOT reproduce. Must be set before any Component gets built, so
# it's here, before the glayout imports below construct anything at
# import time.
gf.CONF.n_threads = 1

from glayout.backend import Component, cell, rectangle, route_quad
from glayout.pdk.mappedpdk import MappedPDK
from glayout.util.comp_utils import evaluate_bbox, align_comp_to_port
from glayout.util.port_utils import rename_ports_by_orientation, get_orientation
from glayout.util.snap_to_grid import component_snap_to_grid
from glayout.primitives.fet import nmos, pmos
from glayout.primitives.guardring import tapring
from glayout.routing.straight_route import straight_route
from glayout.spice.netlist import Netlist
from glayout.pdk.sky130_mapped import sky130_mapped_pdk
try:
    from glayout.verification.evaluator_wrapper import run_evaluation
except ImportError:
    print("Warning: evaluator_wrapper not found. Evaluation will be skipped.")
    run_evaluation = None


BREAKOUT_GLAYER = "met3"   # GDS 69/20 -- see _stretch_ports_to_ring()
BREAKOUT_VIA_GLAYER = "via2"


def _stretch_ports_to_ring(pdk: MappedPDK, cm: Component, ring_bbox,
                           north_specs: list, south_specs: list) -> list:
    """Run the named ports out to the TOP (drains) or BOTTOM (sources) edge
    of the tap ring's own bbox and add a real port there, so a router can
    land on this macro's boundary instead of having to cut into its
    interior.

    `north_specs`/`south_specs` are `(existing_port_name, new_port_name)`
    pairs. Returns the list of added port names.

    Splitting by TERMINAL (every drain up, every source down) rather than
    by position is what makes each stub a single straight vertical run at
    its own x, with no jogs and no shared horizontal lanes. Every device in
    this cell sits in one row, so the drains all share a y and the sources
    all share a y -- but they differ in x, and the two groups leave through
    opposite edges, so no two stubs can ever occupy the same track. An
    earlier version ran everything out the left/right edges instead, which
    forced every east-bound stub onto its own horizontal lane (all the
    branch drains being at one y would otherwise have merged VOUT1 into
    VOUT2) plus a strict lane ordering to stop the risers crossing those
    lanes. None of that machinery is needed here.

    **The stubs run on met3 (GDS 69/20), not the ports' own met2 (68/20).**
    That is forced, not stylistic: every one of these ports sits on a
    horizontal met2 source/drain bar, and met2 in the corridor is already
    occupied -- the row's common gate route, each unit's own bars, and (west
    of the reference device) the dummy's route. A met2 stub would run
    straight into them. met3 is completely EMPTY in this cell (measured:
    zero 69/20 polygons in both a 1-branch and a [2,3]-branch mirror), so
    the stubs have the layer to themselves and cross the ring -- which is
    met1 -- without touching it.

    **The via is hand-placed rather than glayout's `via_stack()`.** Measured,
    not preferred: `via_stack(pdk, "met2", "met3")` draws a 0.430um met2
    landing pad (glayout's own table demands a 0.14um met2 enclosure of
    via2). The bars are 0.290um wide and only 0.430um apart, so that pad
    lands 0.070um from the neighbouring bar where met2 spacing needs
    0.140um -- 7 real Magic "Metal1 spacing < 0.14um (met1.2)" violations
    (Magic's metal1 is GDS 68/20, glayout's met2). sky130's actual via
    enclosure is small enough that the 0.290um bar already encloses the
    0.150um cut on its own, so this places a bare via cut plus a met3
    landing pad and adds NO met2 at all: 0 violations, against a
    deliberately-illegal control arm in the same experiment that correctly
    reported 8. Do not "simplify" this back to via_stack()."""
    (rx0, ry0), (rx1, ry1) = ring_bbox
    glayer = pdk.get_glayer(BREAKOUT_GLAYER)
    cut = float(pdk.get_grule(BREAKOUT_VIA_GLAYER)["width"])
    pad = cut + 2 * float(pdk.get_grule(BREAKOUT_GLAYER, BREAKOUT_VIA_GLAYER)["min_enclosure"])
    min_w = float(pdk.get_grule(BREAKOUT_GLAYER)["min_width"])
    min_sep = float(pdk.get_grule(BREAKOUT_GLAYER)["min_separation"])
    # How far along its own bar the via sits, in from the port end: enough
    # met2 on every side of the cut to satisfy the via's enclosure in x
    # (the bar's 0.290um width covers y on its own -- that is the whole
    # point of not adding a pad).
    #
    # Two values, because one fixed number cannot serve both cases. The
    # MINIMUM is the bare enclosure requirement; the PREFERRED adds a small
    # cushion on top of it, which is what a wide device wants. But on a
    # NARROW device the cushion is actively harmful: a drain and a source
    # via sit at opposite ends of bars that are only ~0.94um long at
    # fingers=1, so every extra 0.04um of inset costs 0.08um of the
    # separation between their two met3 pads -- and those pads are already
    # only 0.10um apart in y (0.43um bar pitch minus a 0.33um pad).
    # Measured on example/test_miller4's real XMP1/XMP2 sizing (w=14.4,
    # fingers=1, pfet): the cushion left 0.090um of x-clearance and Magic
    # reported 8 "Metal2 spacing < 0.14um (met2.2)" errors; dropping to the
    # minimum gives 0.170um and DRC is clean. The earlier sweep missed this
    # because it only tried fingers=2, where the bars are long enough that
    # the two vias are 0.74um apart either way.
    enclosure = float(pdk.get_grule("met2", BREAKOUT_VIA_GLAYER)["min_enclosure"])
    inset_min = pdk.snap_to_2xgrid(cut / 2 + enclosure, return_type="float")
    inset_pref = pdk.snap_to_2xgrid(cut / 2 + enclosure + 0.04, return_type="float")
    inset = inset_pref

    def rect(x0, y0, x1, y1, layer):
        cm.add_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], layer=layer)

    def via_x(pname):
        """x of the via on this port's own bar. A "_W" port is that bar's
        west end (the bar runs east from it), a "_E" port its east end."""
        px = float(cm.ports[pname].center[0])
        return pdk.snap_to_2xgrid(px + inset if pname.endswith("_W") else px - inset,
                                  return_type="float")

    added = []

    def place(pname, new_name, going_north):
        port = cm.ports[pname]
        py = float(port.center[1])
        w = max(float(port.width), min_w)
        vx = via_x(pname)
        rect(vx - cut / 2, py - cut / 2, vx + cut / 2, py + cut / 2,
             pdk.get_glayer(BREAKOUT_VIA_GLAYER))
        rect(vx - pad / 2, py - pad / 2, vx + pad / 2, py + pad / 2, glayer)
        edge_y = ry1 if going_north else ry0
        y0, y1 = sorted((py, edge_y))
        rect(vx - w / 2, y0, vx + w / 2, y1, glayer)
        cm.add_port(name=new_name, center=(vx, edge_y), width=w,
                    orientation=90 if going_north else 270, layer=glayer)
        added.append(new_name)

    present = {label: [(p, n) for p, n in specs if p in cm.ports]
               for label, specs in (("north", north_specs), ("south", south_specs))}

    def clearances():
        """Worst clearance between any two breakout shapes, at the current
        `inset` -- both the via PADS and the stubs' own columns.

        Checking pads across BOTH edges is the point. An earlier version
        only compared stubs leaving through the same edge, on the reasoning
        that opposite-edge stubs never share a y. True of the stubs, false
        of their pads: a drain pad sits just above its bar and a source pad
        just below the next one, 0.10um apart in y, so if they also land
        close in x they violate spacing -- which is exactly what happened on
        the fingers=1 mirror and what that same-edge-only check could not
        see. Returns (worst_gap, description)."""
        boxes = []
        for label, items in present.items():
            for pname, _ in items:
                port = cm.ports[pname]
                px, py = via_x(pname), float(port.center[1])
                w = max(float(port.width), min_w)
                edge_y = ry1 if label == "north" else ry0
                y0, y1 = sorted((py, edge_y))
                boxes.append((px - pad / 2, py - pad / 2, px + pad / 2, py + pad / 2,
                              f"{pname} pad"))
                boxes.append((px - w / 2, y0, px + w / 2, y1, f"{pname} stub"))
        worst, why = float("inf"), ""
        for i, (ax0, ay0, ax1, ay1, an) in enumerate(boxes):
            for bx0, by0, bx1, by1, bn in boxes[i + 1:]:
                if an.split()[0] == bn.split()[0]:
                    continue          # same terminal's own pad + stub, connected
                gap = max(max(bx0 - ax1, ax0 - bx1), max(by0 - ay1, ay0 - by1))
                if gap < worst:
                    worst, why = gap, f"{an} vs {bn}"
        return worst, why

    worst, why = clearances()
    if worst < min_sep:
        # Retry at the bare-minimum inset, which buys separation on exactly
        # the narrow devices that need it (see the inset comment above).
        inset = inset_min
        worst, why = clearances()
    if worst <= 0:
        raise ValueError(
            f"met3 breakout shapes overlap even at the minimum via inset ({why}, "
            f"gap {worst:.3f}um). Drawing them would short two nets together. "
            f"This device is too narrow for opposite-edge breakouts as built.")
    if worst < min_sep:
        # Below glayout's own table but not overlapping. Not raised, because
        # that table is more conservative than the PDK's real rule (met3 is
        # GDS 69/20, whose Magic spacing is 0.14um against this table's
        # 0.28um) and Magic is the authority everywhere else in this project.
        # Surfaced rather than swallowed so a real DRC failure here is
        # traceable to a known-tight spot instead of looking like a surprise.
        print(f"  note: current_mirror met3 breakout clearance is {worst:.3f}um "
              f"({why}), under glayout's {min_sep}um met3 min_separation but above "
              f"the 0.14um Magic enforces on GDS 69/20 -- verify with DRC.")

    for label, items in present.items():
        for pname, new_name in items:
            place(pname, new_name, label == "north")
    return added


def rail_name_for_device(device: str) -> str:
    """The current mirror's shared source/bulk rail is VSS for an NMOS
    mirror (source sits at ground) but VDD for a PMOS mirror (source sits
    at the positive supply) — physically different nets, not a cosmetic
    choice. Previously hardcoded "VSS" for both flavors (a real, user-
    caught bug: a PMOS current mirror's common-source label read "VSS"
    even though that node is actually VDD)."""
    return 'VSS' if device in ('nmos', 'nfet') else 'VDD'


def add_cm_labels(cm_in: Component, pdk: MappedPDK, bulk_rail_name: str = 'VSS',
                   source_rail_names: Optional[list[str]] = None) -> Component:
    """`source_rail_names`: `[reference_source_net, branch_1_source_net,
    branch_2_source_net, ...]`, same list (and same default,
    `["source_ref", "Out1_source", "Out2_source", ...]`)
    `current_mirror_netlist()` uses -- keep them in sync (`current_mirror()`
    passes the same list to both); its own length (`len - 1`) is how this
    function knows how many branches to label, matching however many
    `mirror_ratio` entries `current_mirror()` was actually called with.
    Source is no longer the same net as `bulk_rail_name` (see
    current_mirror_netlist()'s own docstring for why), so each gets its own
    label instead of one shared "VSS"/"VDD" label sitting on fetA's own
    source port. Uses `cm_in`'s own canonical `drain_ref_W`/`source_ref_E`/
    `drain_out{i}_W`/`source_out{i}_E` ports (built by current_mirror()
    itself, see that function's own port-construction section) rather than
    reaching into raw per-device sub-ports -- drain always on the
    left/west side of its own metal, source always on the right/east side
    of that SAME device, so the two never land in the same vertical
    routing corridor."""
    cm_in.unlock()
    move_info = list()
    if source_rail_names is None:
        source_rail_names = ["source_ref", "Out1_source"]
    n_branches = len(source_rail_names) - 1
    out_nets = ["VOUT"] if n_branches == 1 else [f"VOUT{i + 1}" for i in range(n_branches)]

    reflabel = rectangle(layer=pdk.get_glayer("met2_pin"), size=(0.27, 0.27), centered=True).copy()
    reflabel.add_label(text=source_rail_names[0], layer=pdk.get_glayer("met2_label"))
    move_info.append((reflabel, cm_in.ports["source_ref_E"], None))

    vreflabel = rectangle(layer=pdk.get_glayer("met2_pin"), size=(0.27, 0.27), centered=True).copy()
    vreflabel.add_label(text="drain_ref", layer=pdk.get_glayer("met2_label"))
    move_info.append((vreflabel, cm_in.ports["drain_ref_W"], None))

    for i in range(n_branches):
        outsrclabel = rectangle(layer=pdk.get_glayer("met2_pin"), size=(0.27, 0.27), centered=True).copy()
        outsrclabel.add_label(text=source_rail_names[i + 1], layer=pdk.get_glayer("met2_label"))
        move_info.append((outsrclabel, cm_in.ports[f"source_out{i + 1}_E"], None))

        voutlabel = rectangle(layer=pdk.get_glayer("met2_pin"), size=(0.27, 0.27), centered=True).copy()
        voutlabel.add_label(text=out_nets[i], layer=pdk.get_glayer("met2_label"))
        move_info.append((voutlabel, cm_in.ports[f"drain_out{i + 1}_W"], None))

    # Ring is tied to bulk_rail_name via the dummy-to-ring tie (see
    # current_mirror_netlist()'s docstring) -- NOT the source rail anymore,
    # so this is the only bulk_rail_name label now.
    #
    # met1_pin/met1_label, not met2_pin/met2_label -- real, confirmed bug
    # found and fixed here, not cosmetic: `tap_S_top_met_S`'s own real
    # drawn metal is met1 (tapring()'s own `horizontal_glayer="met1"`,
    # confirmed directly by checking that port's `.layer`), but this label
    # was marking it as a met2 pin -- a real layer mismatch that produced a
    # deterministic, reproducible LVS "disconnected node: VSS" / "port
    # errors" result on every run (the promoted pin, on the wrong layer,
    # not simply overlapping the ring's own met1 the way a same-layer pin
    # marker would). The overall LVS verdict still came out "match uniquely
    # with port errors" / is_pass=True via netgen's symmetry-based
    # fallback, not a true pass -- fixed here, not left as an accepted
    # quirk.
    ringlabel = rectangle(layer=pdk.get_glayer("met1_pin"), size=(0.5, 0.5), centered=True).copy()
    ringlabel.add_label(text=bulk_rail_name, layer=pdk.get_glayer("met1_label"))
    move_info.append((ringlabel, cm_in.ports["tap_S_top_met_S"], ('c', 'c')))

    for comp, prt, alignment in move_info:
        alignment = ('c', 'b') if alignment is None else alignment
        compref = align_comp_to_port(comp, prt, alignment=alignment)
        cm_in.add(compref)
    return cm_in.flatten()


def current_mirror_netlist(
    fetA: Component,
    fetB_groups: list[list[Component]],
    pdk: Optional[MappedPDK] = None,
    dum_net: Optional[str] = None,
    bulk_rail_name: str = 'VSS',
    source_rail_names: Optional[list[str]] = None,
) -> Netlist:
    """Generalized for any number of output branches, not just one.
    `fetB_groups` is a list of BRANCHES, each branch itself a list of one or
    more electrically-identical unit-device Components in parallel (len of
    an inner list == that branch's own mirror_ratio entry) --
    `current_mirror()` calls this with one group per entry of its own
    `mirror_ratio` list (a plain int normalizes to a single-branch list),
    so this directly generalizes to however many output branches the
    caller actually built.

    Source is no longer forced onto one shared rail with bulk (see
    `current_mirror()`'s own port construction for the layout-side half of
    this change: source_ref/source_out{i} are now real, independently-
    routable ports, not aliases of the tap ring). `source_rail_names` is
    `[reference_source_net, branch_1_source_net, branch_2_source_net,
    ...]` -- length must be `1 + len(fetB_groups)`. Defaults to
    `["source_ref", "Out1_source", "Out2_source", ...]` if not given.
    `bulk_rail_name` is the one thing that STAYS a single shared net across
    every device (reference and every branch alike) -- VSS for an nmos
    mirror, VDD for a pfet one (see rail_name_for_device()'s docstring) --
    confirmed this still holds without any explicit source-based tie to the
    ring: `current_mirror()`'s own dummy-to-ring met1 tie plus ordinary
    WELL-REGION CONTIGUITY (every device in the row shares one unbroken
    pwell/nwell, no well break between them) is enough on its own for
    Magic/netgen to merge every device's bulk pin onto the ring's own net
    -- confirmed via a real LVS run with source left completely
    unconnected to the ring (two independent source nets, zero shared
    nodes with bulk), not assumed from prior (source-tied) behavior.

    The reference device's own drain/gate net is `"drain_ref"` (previously
    hardcoded `"VREF"` -- renamed for consistency with each branch's own
    `"VOUT{i}"`/`drain_out{i}` naming, a deliberate, requested change, not
    a random rename: it's diode-connected, so this same net also IS the
    common gate bias for every device, reference and every branch alike --
    the name reflects that it's fundamentally the reference device's own
    drain, not a separately-invented "VREF" concept). Each branch gets its
    own output net: `VOUT` if there's exactly one branch (matches the
    single-branch case's existing net name / `add_cm_labels()`'s "VOUT"
    label), else `VOUT1`/`VOUT2`/... for multiple branches -- unchanged,
    NOT renamed to `drain_out{i}` (the user's rename request was scoped to
    the reference device only)."""
    n_branches = len(fetB_groups)
    if source_rail_names is None:
        source_rail_names = ["source_ref"] + [f"Out{i + 1}_source" for i in range(n_branches)]
    if len(source_rail_names) != n_branches + 1:
        raise ValueError(
            f"source_rail_names must have {n_branches + 1} entries "
            f"(1 reference + {n_branches} branch(es)), got {len(source_rail_names)}"
        )
    out_nets = ["VOUT"] if n_branches == 1 else [f"VOUT{i + 1}" for i in range(n_branches)]

    nl = Netlist(circuit_name="CMIRROR", nodes=["drain_ref", *out_nets, bulk_rail_name, *source_rail_names])
    if dum_net is None:
        dum_net = bulk_rail_name if (pdk is not None and pdk.name.lower() == 'sky130') else 'dum'

    ref_source = source_rail_names[0]
    nl.connect_netlist(
        fetA.info['netlist'],
        [('D', 'drain_ref'), ('G', 'drain_ref'), ('S', ref_source), ('B', bulk_rail_name), ('DUM', dum_net)],
    )
    for branch_idx, group in enumerate(fetB_groups):
        out_net = out_nets[branch_idx]
        branch_source = source_rail_names[branch_idx + 1]
        for fetB in group:
            nl.connect_netlist(
                fetB.info['netlist'],
                [('D', out_net), ('G', 'drain_ref'), ('S', branch_source), ('B', bulk_rail_name), ('DUM', dum_net)],
            )
    return nl


# Generic annotation layer for instance-name text. Deliberately NOT a real
# `met*_pin`/`met*_label` pair on the device's own metal: a pin rectangle
# gets promoted to an electrical pin during extraction, and promoting the
# same net twice (or promoting a net that is already a pin elsewhere) breaks
# LVS with duplicate/ambiguous pin errors -- a real regression this file hit
# before, which is why `add_cm_labels()` keeps its label set to the small
# number of genuinely distinct nets. Instance names are pure human
# annotation, so they go on met5_label as TEXT ONLY, with no accompanying
# pin rectangle -- the same layer/purpose convention
# `../.claude/skills/router/script/label_device_ports.py` already uses for exactly
# this job on a routed top-level GDS.
INSTANCE_LABEL_MAGNIFICATION = 0.5


def instance_label_glayer(pdk: MappedPDK):
    """Which glayer instance-name TEXT goes on, for THIS process -- read from
    `.claude/reference/pdk_options.json`'s `label_glayer` when it names one
    the PDK actually has, else the top routing metal's own `_label` glayer.

    Was hardcoded `"met5_label"`, which is a sky130 fact: a process with a
    shallower stack has no met5 at all, and `pdk.get_glayer()` would raise
    mid-cell. Returns None when neither source yields a valid glayer -- the
    caller then leaves the cell unlabelled rather than failing, since these
    labels are human annotation and nothing electrical depends on them."""
    name = None
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _ref = _Path(__file__).resolve().parents[3] / ".claude" / "reference"
        if str(_ref) not in _sys.path:
            _sys.path.insert(0, str(_ref))
        from pdk_config import pdk as _pdk_option
        name = _pdk_option().label_glayer
    except Exception:
        name = None
    valid = set(getattr(pdk, "valid_glayers", ()) or ())
    if name and name in valid:
        return name
    metals = [g for g in valid if g.startswith("met")
              and not g.endswith(("_pin", "_label"))]
    if metals:
        candidate = f"{sorted(metals)[-1]}_label"
        if candidate in valid:
            return candidate
    return None


def device_centers(cm: Component) -> dict:
    """{device_prefix: (x, y)} for every physical device in a
    current_mirror() -- `fetA` (the diode-connected reference) plus one
    `fetOut{branch}_{unit}` entry per placed unit, however many branches
    and parallel units the cell was actually built with.

    The point is the CENTER of the bounding box of every port carrying
    that prefix, not one chosen port: each device contributes hundreds of
    ports spread over its own footprint, so their bbox center lands on the
    device itself. Using a single named port instead (`well_N`, say) is
    not reliable here -- glayout omits some compass sides on a mirrored
    device (confirmed: `diff_pair()`'s bottom row has no `well_N` at all),
    which would silently bias the point to an edge.

    Works unchanged after `current_mirror()`'s own `angle` rotation:
    gdsfactory transforms a reference's port coordinates under rotation,
    so these are always post-rotation world coordinates."""
    groups = {}
    for name, port in cm.ports.items():
        m = re.match(r'^(fetA|fetOut\d+_\d+)_', name)
        if not m:
            continue
        x, y = float(port.center[0]), float(port.center[1])
        b = groups.setdefault(m.group(1), [x, y, x, y])
        b[0], b[1] = min(b[0], x), min(b[1], y)
        b[2], b[3] = max(b[2], x), max(b[3], y)
    return {k: ((v[0] + v[2]) / 2, (v[1] + v[3]) / 2) for k, v in groups.items()}


def add_instance_labels(cm: Component, pdk: MappedPDK, ref_name: str = "MREF",
                        branch_names: Optional[list[str]] = None) -> Component:
    """Draw each device's INSTANCE name as text at that device's own
    location -- e.g. "XMN4" on the reference and "XMN3"/"XMN5" on the
    output branches -- so a human opening the GDS can tell which physical
    transistor is which. Returns `cm` (modified in place, then flattened).

    This is instance-name annotation, NOT net labelling: `add_cm_labels()`
    is what marks real electrical pins (VOUT/VSS/drain_ref/...). These two
    are independent and can both be applied to the same cell; this one is
    text-only on a layer no extraction rule reads (see
    INSTANCE_LABEL_LAYER), so it is LVS-neutral by construction.

    `branch_names` is one name per OUTPUT BRANCH, in branch order. A branch
    built with a mirror_ratio > 1 is several parallel unit devices that are
    all the SAME circuit device, so every unit of that branch gets the same
    name at its own physical location -- real silicon for that device, not
    a duplicated label. Missing/short `branch_names` falls back to
    `MOUT{i}`, so calling this with no arguments still produces a usable,
    if generic, annotated cell."""
    cm.unlock()
    centers = device_centers(cm)
    glayer = instance_label_glayer(pdk)
    if glayer is None:
        print(f"  note: {getattr(pdk, 'name', 'this PDK')} exposes no usable text/label "
              f"glayer -- current_mirror instance names not drawn")
        return cm.flatten()
    layer = pdk.get_glayer(glayer)
    branch_names = list(branch_names or [])

    for prefix, (x, y) in sorted(centers.items()):
        if prefix == "fetA":
            text = ref_name
        else:
            b = int(re.match(r'^fetOut(\d+)_', prefix).group(1))   # 1-indexed
            text = branch_names[b - 1] if b - 1 < len(branch_names) else f"MOUT{b}"
        cm.add_label(text=text, position=(x, y), layer=layer,
                     magnification=INSTANCE_LABEL_MAGNIFICATION)
    return cm.flatten()


def write_netlist_sp(component: Component, gds_path: str) -> str:
    """Writes `component.info['netlist']`'s SPICE text to a `.sp` file next
    to a given `.gds` path (same basename, `.sp` extension instead) --
    every current_mirror() GDS this module writes should have one of these
    alongside it, not just the GDS by itself. Returns the path written."""
    sp_path = str(gds_path).rsplit('.', 1)[0] + '.sp'
    with open(sp_path, 'w') as f:
        f.write(component.info['netlist'].generate_netlist())
    return sp_path


def routing_ports(cm: Component) -> dict:
    """The routing-relevant ports of a current_mirror() Component -- common
    gate, the reference device's own source/drain (`source_ref`/
    `drain_ref`), and, for EACH output branch found on `cm` (however many
    `current_mirror()` was actually built with -- discovered by scanning
    for `drain_out{i}_W` ports, not assumed to be exactly one), that
    branch's own `source_out{i}`/`drain_out{i}` -- as a plain dict a router
    can consume directly (`{x, y, bbox, layer, width}` per port), without
    needing to know this cell's internal port-naming scheme (fetA_/
    fetOut{b}_{i}_ prefixes, multiplier_0_ sub-ports, etc).

    `bbox` is a `width x width` square centered on the port -- the same
    point+width landing-footprint convention `../skills/router/script/
    route_nets.py`'s own `filter_landable()`/`min_via_footprint_um()`
    already use for a port, not a literal trace of the full drawn metal
    strip each of these sits on (that strip can run considerably longer
    than this bbox for a multi-unit branch -- see current_mirror()'s own
    port-construction section for exactly where each point comes from).
    Every drain port sits on the LEFT/WEST side of its own metal (its own
    device's, or its own branch's leftmost unit's, west edge); every source
    port sits on the RIGHT/EAST side of that SAME device -- a deliberate
    split (not device-specific, gate is the one exception, staying at the
    row's own midpoint): drain_W and source_W would land at the IDENTICAL
    x (just different y, drain stacked above source on the same device),
    putting both in the same vertical routing corridor -- opposite sides
    keeps them apart."""
    names = {"gate": "gate_N", "source_ref": "source_ref_E", "drain_ref": "drain_ref_W"}
    # Boundary port for each label, where one exists (`_stretch_ports_to_
    # ring()` adds these for every drain and every source). Spelled out
    # rather than derived from the name above: a breakout's suffix is set by
    # the edge it leaves through, not by the side of its own metal the
    # original port sits on -- drain_out{i}_W leaves NORTH -- so deriving it
    # by string substitution silently finds nothing and quietly falls back
    # to the buried port. `gate` has no breakout and stays where it is.
    breakout = {"drain_ref": "drain_ref_bo_N", "source_ref": "source_ref_bo_S"}
    i = 1
    while f"drain_out{i}_W" in cm.ports:
        names[f"drain_out{i}"] = f"drain_out{i}_W"
        names[f"source_out{i}"] = f"source_out{i}_E"
        breakout[f"drain_out{i}"] = f"drain_out{i}_bo_N"
        breakout[f"source_out{i}"] = f"source_out{i}_bo_S"
        i += 1
    # Same net, same electrical point -- just reported at the tap ring's own
    # edge instead of buried in the middle of the macro, which is the whole
    # reason a router can land on it.
    layer_of = {}
    for label, bo in breakout.items():
        if bo in cm.ports:
            names[label] = bo
            layer_of[label] = BREAKOUT_GLAYER
    info = {}
    for label, pname in names.items():
        port = cm.ports[pname]
        x, y = float(port.center[0]), float(port.center[1])
        w = float(port.width)
        info[label] = {
            "x": x, "y": y,
            "bbox": (x - w / 2, y - w / 2, x + w / 2, y + w / 2),
            "layer": layer_of.get(label, "met2"),
            "width": w,
        }
    return info


@cell
def current_mirror(
    pdk: MappedPDK,
    width: float = 3,
    fingers: int = 1,
    length: Optional[float] = None,
    device: str = 'nfet',
    rmult: int = 1,
    dummy: Union[bool, tuple[bool, bool]] = True,
    substrate_tap: bool = True,
    mirror_ratio: Union[int, list[int]] = 1,
    dum_net: Optional[str] = None,
    angle: int = 0,
) -> Component:
    """A side-by-side (non-interdigitized) current mirror, with one or more
    independent output branches.

    Transistor A is the reference (diode-connected: gate shorted to its own
    drain). Each output BRANCH is `mirror_ratio[i]` *unit-sized* copies of
    the same transistor placed in a row (all branches placed consecutively,
    A first then branch 1's units then branch 2's units, etc, one
    continuous row) -- gate is tied to A's common gate net across the WHOLE
    row (every branch, and A -- this is what makes it a mirror at all), but
    each branch's own drain/source are only shorted WITHIN that branch (not
    across branches, and not to A) -- see current_mirror_netlist()'s own
    docstring for the full electrical picture (each branch gets its own
    output net `VOUT{i}` and its own source net `Out{i}_source`; A gets its
    own `drain_ref`/`source_ref`, and separately, BULK stays one shared
    rail tied to the ring across every device regardless of branch).

    mirror_ratio: a plain int for the single-branch case (backward
        compatible -- normalized to `[mirror_ratio]` internally), or a list
        of ints, one per independent output branch, e.g. `[2, 3]` for two
        branches (the first built from 2 unit devices in parallel, the
        second from 3). Each entry's own output current is approximately
        that many times the reference current, same meaning as the old
        single-branch `mirror_ratio` had, just per-branch now.
    width: transistor width (of each unit device -- A and every branch's
        units all share this, no per-branch width today)
    fingers: number of fingers per unit transistor
    length: transistor length, None means use min length
    device: 'nfet'/'nmos' or 'pfet'/'pmos'
    rmult: routing multiplier passed to nmos/pmos
    dummy: place a dummy on A's outer edge (bool, or a tuple for
        backwards compatibility — only the first element is used; see the
        note above `fetB_unit` for why branch units never get one)
    substrate_tap: place a tapring around the row (connects on met1)
    angle: 0/90/180/270 -- rotates the FINISHED cell (applied last, after
        every device/route/tapring/netlist is already built -- see the
        `component.rotate(angle)` call at the very end of this function).
        Port coordinates/orientations (including the four routing_ports()
        points) are NOT computed separately for this -- `Component.rotate()`
        wraps a rotated ComponentReference of the unrotated cell and re-adds
        that reference's own ports (gdsfactory transforms a reference's
        ports automatically under rotation), so every port listed under its
        ORIGINAL name (`gate_N`, `fetA_multiplier_0_drain_E`, etc.) already
        reflects the POST-rotation world coordinate -- calling
        routing_ports() on the returned Component is always correct, no
        separate update step needed. Port NAMES keep their pre-rotation
        compass suffix (e.g. `gate_N`) even though the physical direction
        that name refers to changes after a 90/270 rotation -- same
        "name is a fixed identifier, not a live compass reading" convention
        already used for `fetA_source_W` etc. under any placement rotation
        elsewhere in this codebase (`route_nets.py`/`render_placement.py`'s
        own `rotated` macro handling looks up ports by name post-rotation
        the same way).
    """
    pdk.activate()
    cm = Component()
    if isinstance(dummy, bool):
        dummy = (dummy, dummy)
    if isinstance(mirror_ratio, int):
        mirror_ratio = [mirror_ratio]
    if not mirror_ratio or any(r < 1 for r in mirror_ratio):
        raise ValueError(f"mirror_ratio must be a positive int or a list of positive ints, got {mirror_ratio}")
    if angle not in (0, 90, 180, 270):
        raise ValueError(f"angle must be 0/90/180/270, got {angle}")

    fet_fn = {'nmos': nmos, 'nfet': nmos, 'pmos': pmos, 'pfet': pmos}.get(device)
    if fet_fn is None:
        raise ValueError(f"device must be nfet/nmos or pfet/pmos, got {device!r}")
    dnwell_kwarg = {"with_dnwell": False} if fet_fn is nmos else {"dnwell": False}

    # fetA gets a dummy on its outer/left side. Every B copy is built from
    # the SAME unit Component with no dummy on either side — giving the
    # rightmost B copy its own outer dummy (as a distinct build) changes its
    # extracted source/drain diffusion area vs the interior copies, which
    # breaks netgen's parallel-device merge across the mirror_ratio copies
    # (confirmed: LVS device-count mismatch, "4 vs 3" classes, until all B
    # copies were forced identical). Trading the rightmost dummy for correct
    # LVS matching across ratios > 1. fetA's own single dummy tie (below,
    # `fullbottom=True`) turned out to be enough on its own for bulk to
    # merge onto the ring for EVERY device in the row, A and B group alike
    # -- see that tie's own comment for the real, repeated-LVS-run story of
    # how this was found (a second dummy on B was tried first and wasn't
    # even necessary once fullbottom=True was in place).
    fetA = fet_fn(pdk, width=width, fingers=fingers, length=length, multipliers=1, with_tie=False, with_dummy=(dummy[0], False), with_substrate_tap=False, rmult=rmult, **dnwell_kwarg)
    fetB_unit = fet_fn(pdk, width=width, fingers=fingers, length=length, multipliers=1, with_tie=False, with_dummy=(False, False), with_substrate_tap=False, rmult=rmult, **dnwell_kwarg)

    if device in ('nmos', 'nfet'):
        min_spacing_x = pdk.get_grule("n+s/d")["min_separation"] - 2 * (fetA.xmax - fetA.ports["multiplier_0_plusdoped_E"].center[0])
        well = "pwell"
        tap_sdlayer = "p+s/d"
    else:
        min_spacing_x = pdk.get_grule("p+s/d")["min_separation"] - 2 * (fetA.xmax - fetA.ports["multiplier_0_plusdoped_E"].center[0])
        well = "nwell"
        tap_sdlayer = "n+s/d"

    fetA_ref = (cm << fetA).movex(0 - fetA.xmax - min_spacing_x / 2)

    # Place every branch's unit devices consecutively in one continuous row
    # to A's right -- branch 1's units, then branch 2's, etc, same
    # `fetB_unit` Component reused for every unit in every branch (no
    # per-branch W/L yet, so they're all physically identical devices, only
    # grouped differently by ROUTING below). `branch_refs[b]` is branch b's
    # own list of placed references (len == mirror_ratio[b]).
    branch_refs: list[list] = []
    cursor_x = fetA_ref.xmax + min_spacing_x
    for branch_ratio in mirror_ratio:
        refs = []
        for _ in range(branch_ratio):
            ref = (cm << fetB_unit).movex(cursor_x + fetB_unit.xmax)
            refs.append(ref)
            cursor_x = ref.xmax + min_spacing_x
        branch_refs.append(refs)
    last_ref = branch_refs[-1][-1]  # last unit of the last branch, kept for tap/dummy/bbox wiring below
    all_row_refs = [fetA_ref] + [ref for refs in branch_refs for ref in refs]

    # Short gates (common gate net, tied to drain_ref by the diode
    # connection below) across the WHOLE row: A, then every unit of every branch --
    # this is what makes it a mirror (one shared bias net across every
    # output branch). Source and drain are NOT shorted across the row
    # anymore (real, deliberate change -- see current_mirror_netlist()'s
    # docstring): A and each branch get their own independent source/drain
    # nets, not one shared rail with each other, with the tap ring, or with
    # other branches. Source/drain ARE still shorted WITHIN each branch's
    # own unit list, since those units together represent ONE logical
    # output-branch device (mirror_ratio[b]-way parallel), not separate
    # branches.
    for left, right in zip(all_row_refs, all_row_refs[1:]):
        cm << route_quad(left.ports["multiplier_0_gate_E"], right.ports["multiplier_0_gate_W"], layer=pdk.get_glayer("met2"))
    for refs in branch_refs:
        for left, right in zip(refs, refs[1:]):
            cm << route_quad(left.ports["multiplier_0_source_E"], right.ports["multiplier_0_source_W"], layer=pdk.get_glayer("met2"))
            cm << route_quad(left.ports["multiplier_0_drain_E"], right.ports["multiplier_0_drain_W"], layer=pdk.get_glayer("met2"))

    # Diode-connect the reference device: tie its drain to its own gate.
    # A straight shot down A's west side (drain_W -> gate_N) cuts right
    # through A's own source/drain-to-diffusion via array (it lives directly
    # under the drain/source top-met rectangles, not just under a dummy) —
    # confirmed by DRC met1.2 spacing violations against that array. Instead
    # route out to a met2 bar placed clear of A's whole bbox (west of xmin,
    # past the via array and any west dummy) and drop down outside it.
    metal_space = pdk.get_grule("met2")["min_separation"]
    bar_w = pdk.get_grule("met2")["min_width"]
    drain_y = fetA_ref.ports["multiplier_0_drain_W"].center[1]
    gate_y = fetA_ref.ports["multiplier_0_gate_W"].center[1]
    bar_x = fetA_ref.xmin - metal_space - bar_w / 2
    bar_cy = (drain_y + gate_y) / 2
    # Land the bar on the PDK grid before placing it, same class of fix as
    # the tap ring's center below. `bar_cy` is a midpoint (a /2) and
    # `bar_x` carries a `bar_w/2`, so both routinely land on a HALF grid
    # unit. A half-grid rectangle survives angle=0 fine -- both its edges
    # round the same way -- but under current_mirror()'s own final
    # `component_snap_to_grid(component.rotate(angle))` the rotated
    # corners no longer round consistently: at angle=90 the bar came out
    # with its two right corners at x=2.815 and x=2.820, one grid unit
    # apart, which Magic flags as "Only 45 and 90 degree angles permitted
    # on metal1 (x.3a)". That was the last remaining instance of the
    # long-standing rotation DRC gap this file's `angle` docs used to
    # describe as unresolved.
    # x is floored rather than rounded to nearest: rounding could move the
    # bar up to half a grid unit TOWARD fetA, eating into the
    # `metal_space` clearance this line just reserved; flooring can only
    # ever move it further away.
    grid2 = 2 * pdk.grid_size
    bar_x = math.floor(bar_x / grid2) * grid2
    bar_cy = pdk.snap_to_2xgrid(bar_cy, return_type="float")
    bar = rectangle(layer=pdk.get_glayer("met2"), size=(bar_w, abs(drain_y - gate_y) + 1), centered=True)
    bar_ref = (cm << bar).move((bar_x, bar_cy))
    # bar_ref.ports["e3"] spans the bar's full height (it's a vertical edge
    # port) — narrow it to match each connector's actual port width before
    # routing, otherwise route_quad flares a tall trapezoid off a narrow
    # drain/gate port, which magic flags as a non-Manhattan met1 shape. The
    # move delta is computed off e3's *actual* (grid-snapped) center rather
    # than the theoretical bar_cy, otherwise sub-grid rounding drift leaves
    # a few-nm sliver that also trips the same Manhattan-angle check.
    drain_w_port = fetA_ref.ports["multiplier_0_drain_W"]
    gate_w_port = fetA_ref.ports["multiplier_0_gate_W"]

    def bar_port_facing(dev_port):
        """A bar-side port sitting at EXACTLY the device port's own y and
        width, so route_quad() between the two is a true rectangle.

        Built by copying e3 and assigning the center outright rather than
        via movey()/set_port_orientation(). Those two do not preserve the
        coordinate here: the bar's own e3 center lands on a half-grid y
        (0.0225um -- a bbox midpoint, same origin as the tap-ring offset
        fixed below), and `movey(e3, gate_y - e3_y)` came back at
        y=-2.6175 rather than the requested -2.615, a 0.0025um drift that
        left route_quad drawing a PARALLELOGRAM: both ends 0.33um tall but
        offset 0.005um from each other. Magic flags both of its long
        edges -- "Only 45 and 90 degree angles permitted on metal1
        (x.3a)", the 2 errors the skills/placer run on
        example/test_miller4 reported against the pfet mirror. Taking the
        y straight from the device port removes the arithmetic that was
        drifting, instead of trying to round it back afterwards."""
        p = bar_ref.ports["e3"].copy()
        p.center = (float(bar_ref.ports["e3"].center[0]), float(dev_port.center[1]))
        p.width = dev_port.width
        p.orientation = get_orientation("E")
        return p

    bar_top_port = bar_port_facing(drain_w_port)
    bar_bot_port = bar_port_facing(gate_w_port)
    cm << route_quad(drain_w_port, bar_top_port, layer=pdk.get_glayer("met2"))
    cm << route_quad(gate_w_port, bar_bot_port, layer=pdk.get_glayer("met2"))

    # bbox of the tap ring, once it exists -- the target the routing ports
    # get stretched out to at the end of this function.
    ring_bbox = None
    if substrate_tap:
        # tapring() builds its ring centered on the origin from a plain
        # (width, height) size — it has no idea where cm's content actually
        # sits. diff_pair/current_mirror's own rows are roughly centered on
        # x=0 so this goes unnoticed there, but this row is not (A sits far
        # left, the B array grows only to the right), so the ring drifts off
        #-center as mirror_ratio grows and eventually clips the last B unit's
        # diffusion — confirmed by DRC (P-tap/diffusion spacing violations)
        # starting at mirror_ratio=4. Recenter the ring onto cm's actual bbox.
        cm_bbox = cm.bbox
        cm_center = ((cm_bbox[0][0] + cm_bbox[1][0]) / 2, (cm_bbox[0][1] + cm_bbox[1][1]) / 2)
        # Snap the ring's placement center to the PDK grid BEFORE moving.
        # Real, confirmed DRC bug, not a precaution: a bbox midpoint is a
        # /2, so it lands on a HALF grid unit whenever the bbox spans an
        # odd number of grid units -- for the nfet mirror at width=58.8 the
        # center came out y=0.0225um, exactly 4.5 x the 0.005um grid.
        # tapring() lays its tap licons on a correct, uniform 0.34um pitch
        # (= licon width 0.17 + licon.2 min_separation 0.17, verified
        # against the PDK rules), but moving the whole ring by that
        # half-grid offset puts every licon on a half-grid y -- and the
        # component_snap_to_grid() at the end of this function then rounds
        # each one independently, alternating them up and down so the
        # pitch becomes 0.335/0.345 instead of a uniform 0.34. A 0.335
        # pitch leaves a 0.165um gap: licon.2 needs 0.17um. That produced
        # 118 real "Diffusion contact spacing < 0.17um" errors in the
        # skills/placer run on example/test_miller4 (236 instances by
        # `drc listall why`) -- the violations were in the tap ring's own
        # left/right columns, at exactly the y positions where rounding
        # went the wrong way. Snapping the center keeps the ring's already-
        # correct pitch intact through the final snap. The ring moves by at
        # most half a grid unit, far inside its own 1um padding, so the
        # recentering this line exists for is unaffected.
        cm_center = pdk.snap_to_2xgrid(cm_center, return_type="float")
        tapref = (cm << tapring(pdk, evaluate_bbox(cm, padding=1), sdlayer=tap_sdlayer, horizontal_glayer="met1")).move(cm_center)
        cm.add_ports(tapref.get_ports_list(), prefix="tap_")
        ring_bbox = tapref.bbox
        # width pinned to the (much narrower) dummy-side port — the tap
        # ring's own port spans its whole side, and leaving straight_route
        # to infer a width off that flares a sliver of a non-Manhattan
        # trapezoid at the dummy end, same class of issue as the bar ports.
        # Only A ever has a dummy (see the fetB_unit note above), so this is
        # the only dummy-to-ring tie -- and now the ONLY explicit tie at
        # all (no more source-to-ring tie, see below).
        #
        # `fullbottom=True` matters here, not decorative -- real, confirmed
        # necessity, not precautionary. Once the old source-to-ring tie
        # (which used to run from fetB_ref's own source_E, and was the only
        # route with this flag) was removed as part of separating
        # source_ref/source_out{i} from the bulk rail (see
        # current_mirror_netlist()'s docstring), this dummy tie WITHOUT
        # `fullbottom=True` was not reliably enough on its own: repeated,
        # otherwise-identical LVS runs sometimes matched "VSS" onto the
        # ring's own extracted node, sometimes reported it unmatched ("Top
        # level cell failed pin matching") -- non-deterministic, same code,
        # same inputs. Adding `fullbottom=True` here (this file's own prior
        # note calls it "the critical fix that made a via stack reach deep
        # enough for Magic/Netgen to recognize bulk-tie connectivity")
        # fixed it -- confirmed via 4 repeated, otherwise-identical LVS
        # runs, all clean ("LVS Pass: Netlists match", no disconnected-pin
        # warnings). A second dummy on the row's last unit, tied to the
        # ring's east side the same way, was tried FIRST and did NOT fix it
        # by itself (still non-deterministic without fullbottom=True, and
        # once fullbottom=True was added, the single A-side tie alone was
        # already sufficient -- the second dummy was unnecessary
        # complexity, removed again). Only tested at 1-2 branches so far --
        # if a much longer multi-branch row ever shows the same
        # non-deterministic bulk-matching symptom again, revisit whether a
        # second (east-side, on the row's actual last unit) dummy tie is
        # needed for longer rows specifically, same diagnostic approach
        # (repeated LVS runs, not a single "it passed once" check).
        try:
            dl_port = fetA_ref.ports["multiplier_0_dummy_L_gsdcon_top_met_W"]
            # Pin the ring-side port onto the dummy port's OWN centerline
            # before routing. Pinning the width alone (above) is not enough:
            # the two ports' y centers differed by one 0.005um grid unit
            # (dummy at y=-2.615, ring at y=-2.620 on the pfet mirror), and
            # straight_route() then draws a quad whose left and right edges
            # sit at different y -- a real trapezoid sloping 0.005um over
            # its 2.575um length. Magic flags both of its long edges:
            # "Only 45 and 90 degree angles permitted on metal1 (x.3a)", 2
            # errors, found in the skills/placer run on example/test_miller4.
            # The ring's west segment is one tall vertical bar, so moving
            # its port along y stays on the same drawn metal -- the tie
            # lands exactly where it did, just strictly horizontal.
            tap_w_port = cm.ports["tap_W_top_met_W"].copy()
            # Assigned outright, same reason as bar_port_facing() above --
            # movey() is not coordinate-preserving on a half-grid center.
            tap_w_port.center = (float(tap_w_port.center[0]), float(dl_port.center[1]))
            cm << straight_route(pdk, dl_port, tap_w_port, glayer2="met1", width=dl_port.width, fullbottom=True)
        except KeyError:
            pass

    # Every device's own top-level "gate_*"/"source_*"/"drain_*" port
    # aliases a net that's shared with at least one other device (gate:
    # across the whole row; source/drain: within their own branch's unit
    # list only, now that A and every other branch are separate -- see the
    # route_quad loops above). Expose clean, routing-ready ports instead --
    # "gate_N" (merged at the row's own midpoint), "source_ref_E"/
    # "drain_ref_W" (A's own real ports), and, for EACH branch b
    # (1-indexed), "source_out{b}_E"/"drain_out{b}_W" (that branch's own
    # leftmost unit's real ports) -- rather than a separate, redundant,
    # misleadingly per-device-looking gate/source/drain port under every
    # fetA_/fetOut{b}_{i}_ prefix. Drain always sits on the LEFT/WEST side
    # of its own metal, source always on the RIGHT/EAST side of that SAME
    # device (a deliberate, uniform placement rule -- see routing_ports()'s
    # own docstring for why, and not device-specific choices like the old
    # drainA=east/drainB=midpoint scheme this replaces). See
    # routing_ports() below for a plain-dict view of all of these.
    _MERGED_PORT_NAMES = {f"{term}_{side}" for term in ("gate", "source", "drain") for side in "NSEW"}

    def _unmerged_ports(ref):
        return [p for p in ref.get_ports_list() if p.name not in _MERGED_PORT_NAMES]

    cm.add_ports(_unmerged_ports(fetA_ref), prefix="fetA_")
    for b, refs in enumerate(branch_refs):
        for i, ref in enumerate(refs):
            cm.add_ports(_unmerged_ports(ref), prefix=f"fetOut{b + 1}_{i}_")

    # Named "..._N"/"..._W" (not bare "gate"/"source_ref"/"drain_ref"/
    # "source_out{b}"/"drain_out{b}") because rename_ports_by_orientation()
    # below requires an underscore-separated direction suffix on every port
    # name (raises ValueError otherwise) -- each one's own orientation
    # (below) is the suffix it would assign anyway, so this is just
    # spelling out the name each one ends up with, not fighting that
    # rename step.
    gate_w_end = fetA_ref.ports["multiplier_0_gate_W"]
    gate_e_end = last_ref.ports["multiplier_0_gate_E"]
    gate_mid_x = (gate_w_end.center[0] + gate_e_end.center[0]) / 2
    cm.add_port(name="gate_N", center=(gate_mid_x, gate_w_end.center[1]), width=gate_w_end.width,
                orientation=90, layer=pdk.get_glayer("met2"))

    # A's own drain/source: real device ports (A only ever has one physical
    # copy). Drain always on the west/left side, source always on the
    # east/right side of the SAME device -- a deliberate split, not an
    # arbitrary choice: drain_W and source_W would sit at the IDENTICAL x
    # (just different y, drain stacked above source on the same device),
    # putting both ports in the same vertical routing corridor -- opposite
    # sides keeps them from ever landing in each other's way.
    source_ref_port = fetA_ref.ports["multiplier_0_source_E"]
    cm.add_port(name="source_ref_E", center=source_ref_port.center, width=source_ref_port.width,
                orientation=0, layer=pdk.get_glayer("met2"))

    drain_ref_port = fetA_ref.ports["multiplier_0_drain_W"]
    cm.add_port(name="drain_ref_W", center=drain_ref_port.center, width=drain_ref_port.width,
                orientation=180, layer=pdk.get_glayer("met2"))

    # Each branch's own drain/source: the branch's own LEFTMOST unit's real
    # ports -- drain on that unit's west/left side ("always on the left
    # side of its metal"), source on that SAME unit's east/right side (not
    # a different unit -- same drain/source vertical-corridor reasoning as
    # A's, above), not the row's own midpoint the way the single-branch
    # version used to merge drainB, and not the branch's rightmost unit the
    # way source used to sit either.
    for b, refs in enumerate(branch_refs):
        leftmost = refs[0]
        branch_source_port = leftmost.ports["multiplier_0_source_E"]
        cm.add_port(name=f"source_out{b + 1}_E", center=branch_source_port.center, width=branch_source_port.width,
                    orientation=0, layer=pdk.get_glayer("met2"))
        branch_drain_port = leftmost.ports["multiplier_0_drain_W"]
        cm.add_port(name=f"drain_out{b + 1}_W", center=branch_drain_port.center, width=branch_drain_port.width,
                    orientation=180, layer=pdk.get_glayer("met2"))

    # Stretch the routing-relevant ports out to the tap ring's own edge:
    # every DRAIN (the reference's and every branch's) to the top, every
    # SOURCE to the bottom. The ports above stay exactly where they are
    # (they are what add_cm_labels()/the netlist/this file's own callers key
    # on) -- these are ADDITIONAL "_bo_" ports on the boundary, same
    # convention as ./primitives/fet.py's own edge breakouts.
    if ring_bbox is not None:
        n_branches = len(branch_refs)
        _stretch_ports_to_ring(
            pdk, cm, ring_bbox,
            north_specs=([("drain_ref_W", "drain_ref_bo_N")] +
                         [(f"drain_out{b + 1}_W", f"drain_out{b + 1}_bo_N")
                          for b in range(n_branches)]),
            south_specs=([("source_ref_E", "source_ref_bo_S")] +
                         [(f"source_out{b + 1}_E", f"source_out{b + 1}_bo_S")
                          for b in range(n_branches)]),
        )

    cm.add_padding(layers=(pdk.get_glayer(well),), default=0)
    component = component_snap_to_grid(rename_ports_by_orientation(cm))

    bulk_rail_name = rail_name_for_device(device)
    fetB_groups = [[fetB_unit] * len(refs) for refs in branch_refs]
    component.info['netlist'] = current_mirror_netlist(
        fetA, fetB_groups, pdk=pdk, dum_net=dum_net, bulk_rail_name=bulk_rail_name,
    )

    if pdk.name.lower() == "gf180" and substrate_tap and not os.environ.get("GLAYOUT_NO_PIN_LABELS"):
        component = add_cm_labels(component, pdk, bulk_rail_name=bulk_rail_name)

    # Rotation happens LAST, after every device/route/tapring/netlist/label
    # is already built -- Component.rotate() wraps a rotated
    # ComponentReference of `component` in a fresh Component and re-adds
    # that reference's own ports, which gdsfactory already transforms
    # (position AND orientation) under rotation -- no separate port-info
    # update needed, `routing_ports()`/`add_cm_labels()`/any `cm.ports[...]`
    # lookup by name on the RETURNED component already reflects the
    # POST-rotation world coordinate. `copy_child_info()` (inside
    # `rotate()`) also carries `component.info['netlist']` over, so the
    # LVS-verification netlist set above survives rotation unchanged (a
    # rotation is a rigid transform, not a circuit change).
    #
    # The angle=90/180 x.3a DRC gap this comment used to describe as
    # unresolved is FIXED, and it was never a rotation bug: the earlier
    # diagnosis (a "pre-existing 5nm-scale sliver ... not a floating-point
    # grid-snap issue") was right that re-snapping here couldn't fix it,
    # but wrong about the cause. The real source was upstream -- the
    # diode-connect bar and the tap ring were each PLACED at a half-grid
    # coordinate (a bbox midpoint / a `bar_w/2` offset). A half-grid
    # rectangle survives angle=0 intact because both of its edges round
    # the same way, but under the rotate-then-snap below the rotated
    # corners stop rounding consistently, leaving one corner a single
    # 0.005um grid unit out of line -- which is exactly the "45/90 degree
    # angles" complaint. Snapping those two placements (see their own
    # comments above) removed it at the source. All four angles now come
    # out DRC 0 errors and LVS "Netlists match", each verified in its own
    # fresh process.
    #
    # This re-snap is kept as cheap insurance against genuine off-grid
    # float drift from the rotation matrix itself.
    if angle != 0:
        component = component_snap_to_grid(component.rotate(angle))
    return component


if __name__ == "__main__":
    cm = current_mirror(sky130_mapped_pdk, device='nfet')
    cm = add_cm_labels(cm, sky130_mapped_pdk, bulk_rail_name='VSS')
    # Net labels (above) and instance names (here) are independent passes on
    # separate layers -- both are applied so the written GDS carries both.
    cm = add_instance_labels(cm, sky130_mapped_pdk)
    cm.name = "CMIRROR"
    cm_gds = cm.write_gds("current_mirror.gds")
    write_netlist_sp(cm, "current_mirror.gds")  # every GDS this module writes gets a matching .sp
    if run_evaluation is not None:
        res = run_evaluation("current_mirror.gds", cm.name, cm)
    else:
        print("Skipping evaluation because evaluator_wrapper was not found.")
