import math
import re
from copy import deepcopy
from typing import Optional, Union

from glayout.backend import Component, cell, copy, rectangle, route_quad
from glayout.pdk.mappedpdk import MappedPDK
from glayout.util.comp_utils import align_comp_to_port, evaluate_bbox, movex, movey
from glayout.util.port_utils import (
    add_ports_perimeter,
    get_orientation,
    print_ports,
    rename_ports_by_list,
    rename_ports_by_orientation,
    set_port_orientation,
)
from glayout.util.snap_to_grid import component_snap_to_grid
from glayout.placement.common_centroid_ab_ba import common_centroid_ab_ba
from glayout.primitives.fet import fet_netlist, multiplier, nmos, pmos
from glayout.primitives.guardring import tapring
from glayout.primitives.via_gen import via_stack
from glayout.routing.c_route import c_route
from glayout.routing.smart_route import smart_route
from glayout.routing.straight_route import straight_route
from glayout.spice import Netlist
from glayout.pdk.sky130_mapped import sky130_mapped_pdk
try:
    from glayout.verification.evaluator_wrapper import run_evaluation
except ImportError:
    print("Warning: evaluator_wrapper not found. Evaluation will be skipped.")
    run_evaluation = None


def _point_on_wire(near_port, far_port, distance_from_near: float) -> tuple[float, float]:
	"""A point `distance_from_near` along the straight line from `near_port`
	to `far_port`'s centers -- used to land VN/VP on real copper of their
	own gate-bus wire regardless of that wire's orientation (vertical at
	angle=0, horizontal at angle=90), since a fixed (dx,dy) offset from one
	port does not survive diff_pair()'s own rotation the way this does."""
	nx, ny = near_port.center
	fx, fy = far_port.center
	length = ((fx - nx) ** 2 + (fy - ny) ** 2) ** 0.5
	ux, uy = (fx - nx) / length, (fy - ny) / length
	return (nx + ux * distance_from_near, ny + uy * distance_from_near)


def routing_ports(df: Component) -> dict:
	"""The routing-relevant nets of a diff_pair() Component -- VP/VN (the
	two gate inputs), VTAIL (shorted sources), diff_M1_drain/diff_M2_drain
	(the two independent drains), and B (bulk/tap ring) -- as a plain dict
	a router can consume directly (`{x, y, bbox, layer, width}` per port),
	mirroring current_mirror.py's own `routing_ports()` convention. `bbox`
	is a `width x width` square centered on the port, same landing-footprint
	convention `route_nets.py` uses. VP/VN aren't named Ports on the
	Component (see `add_df_labels()`) so their point is recomputed here the
	same way: interpolated along their own gate-bus wire's con_S/con_N pair,
	which stays correct after rotation -- used only when the cell was built
	without the top-edge gate breakouts (`VP_bo_N`/`VN_bo_N`), which are
	preferred when present for the same reason the drains' are."""
	info = {}
	named = {
		"VTAIL": ("bl_multiplier_0_source_S", "met2", 0.27),
		"diff_M1_drain": ("tl_multiplier_0_drain_N", "met2", 0.27),
		"diff_M2_drain": ("tr_multiplier_0_drain_N", "met2", 0.27),
		"B": ("tap_N_top_met_S", "met1", 0.5),
	}
	# Prefer each drain's boundary port where diff_pair() added one -- same
	# net, reached by a met3 trace run straight up from the device port, and
	# reported on the macro's top edge (the well outline, GDS 64/44) instead
	# of on the device itself ~2.9um inside, which is what lets a router
	# actually land on it.
	for label, bo in (("diff_M1_drain", "diff_M1_drain_bo_N"),
	                  ("diff_M2_drain", "diff_M2_drain_bo_N"),
	                  ("B", "B_bo_N"),
	                  ("VTAIL", "VTAIL_bo_S"),
	                  ("VP", "VP_bo_N"),
	                  ("VN", "VN_bo_N")):
		if bo in df.ports:
			named[label] = (bo, "met3", 0.27)
	for label, (pname, layer, _) in named.items():
		port = df.ports[pname]
		x, y = float(port.center[0]), float(port.center[1])
		w = float(port.width)
		info[label] = {"x": x, "y": y, "bbox": (x - w / 2, y - w / 2, x + w / 2, y + w / 2), "layer": layer, "width": w}

	w = 0.33
	if "VN" not in info:
		vn_x, vn_y = _point_on_wire(df.ports["MINUSgateroute_W_con_S"], df.ports["MINUSgateroute_W_con_N"], 0.235)
		info["VN"] = {"x": float(vn_x), "y": float(vn_y),
			"bbox": (vn_x - w / 2, vn_y - w / 2, vn_x + w / 2, vn_y + w / 2), "layer": "met2", "width": w}
	if "VP" not in info:
		vp_x, vp_y = _point_on_wire(df.ports["PLUSgateroute_E_con_S"], df.ports["PLUSgateroute_E_con_N"], 0.235)
		info["VP"] = {"x": float(vp_x), "y": float(vp_y),
			"bbox": (vp_x - w / 2, vp_y - w / 2, vp_x + w / 2, vp_y + w / 2), "layer": "met3", "width": w}
	return info


def add_df_labels(df_in: Component,
                        pdk: MappedPDK
                         ) -> Component:

	df_in.unlock()
	met1_pin = (67,16)
	met1_label = (67,5)
	met2_pin = (68,16)
	met2_label = (68,5)
    # list that will contain all port/comp info
	move_info = list()
    # create labels and append to info list
    # vtail
	vtaillabel = rectangle(layer=pdk.get_glayer("met2_pin"),size=(0.27,0.27),centered=True).copy()
	vtaillabel.add_label(text="VTAIL",layer=pdk.get_glayer("met2_label"))
	move_info.append((vtaillabel,df_in.ports["bl_multiplier_0_source_S"],None))

    # diff_M1_drain / diff_M2_drain — the two fets' drains. Named generically
    # (not VDD1/VDD2) since this diff pair cell is standalone and its drain
    # nets are not necessarily tied to a supply rail by the parent circuit.
    #
    # Anchored on the boundary port that diff_pair() runs up to the macro's
    # top edge, so the label and pin sit ON that edge with the port rather
    # than back on the device ~3um inside -- the pin is what a parent
    # extraction sees as this net's terminal, so leaving it behind would put
    # the electrical pin and the routable point in different places. That
    # trace is met3, so the pin/label pair moves to met3 too; a met2 pin
    # there would sit on no copper at all.
    #
    # Falls back to the device port when the breakout is absent (no well/
    # bbox to run to), which keeps this working for any caller that builds
    # the pair without it.
	drain_pin_size = 0.27
	for text, bo_port, dev_port, off_edge_layers, off_size in (
		("diff_M1_drain", "diff_M1_drain_bo_N", "tl_multiplier_0_drain_N", "met2", 0.27),
		("diff_M2_drain", "diff_M2_drain_bo_N", "tr_multiplier_0_drain_N", "met2", 0.27),
		# B (bulk) rides along: same boundary treatment, and when the
		# breakout is absent it falls back to the tap ring's own north
		# contact on met1, which is where it used to sit unconditionally.
		("B", "B_bo_N", "tap_N_top_met_S", "met1", 0.5),
		# VP/VN ride along too when diff_pair() ran them out to the top edge.
		# Their fallback is not a device port but the interpolated point on
		# their own gate-bus wire further down, so `None` marks "no device
		# fallback here" and the block below draws it instead.
		("VP", "VP_bo_N", None, "met3", 0.33),
		("VN", "VN_bo_N", None, "met2", 0.33),
	):
		on_edge = bo_port in df_in.ports
		pin_layer = "met3_pin" if on_edge else f"{off_edge_layers}_pin"
		label_layer = "met3_label" if on_edge else f"{off_edge_layers}_label"
		size = drain_pin_size if on_edge else off_size
		drain_label = rectangle(layer=pdk.get_glayer(pin_layer),size=(size,)*2,centered=True).copy()
		drain_label.add_label(text=text,layer=pdk.get_glayer(label_layer))
		if not on_edge:
			if dev_port is not None:
				move_info.append((drain_label,df_in.ports[dev_port],None))
			continue
		# Placed by hand rather than via align_comp_to_port's ('c','b'): that
		# tuple tucks the square inward along Y specifically, which is only
		# the inward direction while the port faces north. After diff_pair()'s
		# own angle=90 rotation the same port faces west, 'b' still nudges in
		# Y, and the square ends up merely CENTRED on the port -- overhanging
		# the macro edge by half its width. Measured: 0.135um of 69/16 outside
		# the 64/44 outline at angle=90, which also grew the cell's bbox.
		# Stepping in along the port's OWN outward normal is rotation-correct
		# by construction, same approach as VN/VP below.
		prt = df_in.ports[bo_port]
		theta = math.radians(float(prt.orientation))
		ref = df_in << drain_label
		ref.move(destination=(float(prt.center[0]) - math.cos(theta) * size / 2,
		                      float(prt.center[1]) - math.sin(theta) * size / 2))

    # move everything to position
	for comp, prt, alignment in move_info:
		alignment = ('c','b') if alignment is None else alignment
		compref = align_comp_to_port(comp, prt, alignment=alignment)
		df_in.add(compref)

    # VN / VP -- placed on the leftmost/rightmost real copper reached by
    # their own gate-bus net (MINUSgateroute_W's west via pad for VN,
    # PLUSgateroute_E's east via-stack run for VP) instead of at the
    # mid-layout gate_S port, so a parent router only needs to touch this
    # sub-block's bbox edge, not route into its interior. VP's point sits
    # on the met3 jumper segment of that net (the east route rises to met3
    # partway up to clear the neighboring wire), not met2.
    # Point is interpolated between the wire's own con_S/con_N ports (not a
    # fixed global offset) so it still lands on copper -- at the same
    # physical distance in from the con_S end -- after diff_pair()'s own
    # angle=90 rotation, where this wire runs along x instead of y.
	if "VN_bo_N" not in df_in.ports:
		vn_point = _point_on_wire(df_in.ports["MINUSgateroute_W_con_S"], df_in.ports["MINUSgateroute_W_con_N"], 0.235)
		vnlabel = rectangle(layer=pdk.get_glayer("met2_pin"),size=(0.33,0.33),centered=True).copy()
		vnlabel.add_label(text="VN",layer=pdk.get_glayer("met2_label"))
		vnref = df_in << vnlabel
		vnref.move(destination=vn_point)

	if "VP_bo_N" not in df_in.ports:
		vp_point = _point_on_wire(df_in.ports["PLUSgateroute_E_con_S"], df_in.ports["PLUSgateroute_E_con_N"], 0.235)
		vplabel = rectangle(layer=pdk.get_glayer("met3_pin"),size=(0.33,0.33),centered=True).copy()
		vplabel.add_label(text="VP",layer=pdk.get_glayer("met3_label"))
		vpref = df_in << vplabel
		vpref.move(destination=vp_point)

	return df_in.flatten()

def diff_pair_netlist(fetL: Component, fetR: Component, pdk: Optional[MappedPDK] = None, dum_net: Optional[str] = None, interleaved: bool = False) -> Netlist:
	diff_pair_netlist = Netlist(circuit_name='DIFF_PAIR', nodes=['VP', 'VN', 'diff_M1_drain', 'diff_M2_drain', 'VTAIL', 'B'])

	# The physical layout uses an AB/BA common-centroid placement with four
	# mirrored device references (two copies of the left device and two copies of
	# the right device). Model that explicitly in the reference netlist so LVS
	# compares against the same effective device count/width.
	#
	# DUM maps to the dummies' G/S/D net. Standalone:
	# * gf180 klayout extracts the four dummies' diffusion fingers as one
	#   shared floating net (the inter-dummy contacts merge them), so we
	#   map DUM→'dum' (a local subckt-level net).
	# * sky130 magic+netgen absorbs the floating dummies into the bulk
	#   during parallel-device merging, so the schematic must put them on B
	#   directly — leaving DUM as a separate `dum` net there counts an extra
	#   net on the schematic side and trips the LVS comparison.
	# `dum_net` lets a composite parent override this when the surrounding
	# layout context (extra tap rings, shared pwell paths) physically forces
	# the dummies onto a different net than the standalone-cell extraction.
	if dum_net is None:
		dum_net = 'B' if (pdk is not None and pdk.name.lower() == 'sky130') else 'dum'
	# Which of the four placed quadrants belongs to which device. Side by side,
	# device A is two copies of fetL and B two of fetR. Interleaved, fetL/fetR
	# are the outer/inner ROLES of a row instead, and each device takes one of
	# each -- outer in one row, inner in the other.
	quadrants = ((('diff_M1_drain', fetL), ('diff_M1_drain', fetR),
	              ('diff_M2_drain', fetL), ('diff_M2_drain', fetR)) if interleaved else
	             (('diff_M1_drain', fetL), ('diff_M1_drain', fetL),
	              ('diff_M2_drain', fetR), ('diff_M2_drain', fetR)))
	for net, fet in quadrants:
		gate = 'VP' if net == 'diff_M1_drain' else 'VN'
		diff_pair_netlist.connect_netlist(
			fet.info['netlist'],
			[('D', net), ('G', gate), ('S', 'VTAIL'), ('B', 'B'), ('DUM', dum_net)],
		)
	return diff_pair_netlist


# See current_mirror.py's INSTANCE_LABEL_LAYER comment for why instance
# names go on a generic annotation layer as TEXT ONLY (no met*_pin
# rectangle): a pin rectangle gets promoted to a real electrical pin during
# extraction and can break LVS, while instance names are pure human
# annotation. Same layer/purpose convention
# `../.claude/skills/router/script/label_device_ports.py` uses for this job.
INSTANCE_LABEL_MAGNIFICATION = 0.5


def instance_label_glayer(pdk):
	"""Which glayer instance-name TEXT goes on -- `current_mirror.py`'s own
	`instance_label_glayer()` (read from pdk_options.json's `label_glayer`,
	falling back to the top metal's `_label`). Imported lazily and by flat
	name, which is how this directory's modules are loaded (no package
	`__init__.py` here); the fallback below keeps a bare import working."""
	try:
		from current_mirror import instance_label_glayer as _shared
		return _shared(pdk)
	except Exception:
		valid = set(getattr(pdk, "valid_glayers", ()) or ())
		metals = [g for g in valid if g.startswith("met")
		          and not g.endswith(("_pin", "_label"))]
		candidate = f"{sorted(metals)[-1]}_label" if metals else None
		return candidate if candidate in valid else None


def device_centers(df: Component) -> dict:
	"""{quadrant_prefix: (x, y)} for each of diff_pair()'s four placed
	device references -- `tl`, `tr`, `bl`, `br`.

	The point is the CENTER of the bounding box of every port carrying that
	prefix, not one chosen port. That matters here specifically: this cell's
	bottom row is MIRRORED (`mirror_y()`), and glayout drops some compass
	sides on a mirrored device -- confirmed directly, `bl`/`br` have no
	`well_N` port at all -- so anchoring on a single named port would
	silently bias the label to an edge on half the devices.

	Works unchanged after diff_pair()'s own `angle` rotation, since
	gdsfactory transforms a reference's port coordinates under rotation."""
	groups = {}
	for name, port in df.ports.items():
		m = re.match(r'^(tl|tr|bl|br)_', name)
		if not m:
			continue
		x, y = float(port.center[0]), float(port.center[1])
		b = groups.setdefault(m.group(1), [x, y, x, y])
		b[0], b[1] = min(b[0], x), min(b[1], y)
		b[2], b[3] = max(b[2], x), max(b[3], y)
	return {k: ((v[0] + v[2]) / 2, (v[1] + v[3]) / 2) for k, v in groups.items()}


def add_instance_labels(df: Component, pdk: MappedPDK, name_a: str = "MA",
                        name_b: str = "MB") -> Component:
	"""Draw each device's INSTANCE name as text at that device's own
	location -- e.g. "XMN1"/"XMN2" -- so a human opening the GDS can tell
	which physical transistor is which. Returns `df` (modified in place,
	then flattened).

	**Each name appears TWICE, and that is correct, not a duplicate.** This
	is a common-centroid AB/BA layout: device A is split into two physical
	halves placed diagonally opposite each other, and so is device B. The
	quadrant->device mapping is `tl`/`br` = A, `tr`/`bl` = B -- taken from
	`diff_pair()`'s own `add_ports()` calls below (`a_topl`/`a_botr` are
	prefixed `tl_`/`br_`, `b_topr`/`b_botl` are `tr_`/`bl_`), not guessed,
	and the same mapping `../.claude/skills/router/script/label_device_ports.py`
	documents for this cell.

	Instance-name annotation only, NOT net labelling -- `add_df_labels()`
	is what marks real electrical pins (VP/VN/VTAIL/...). Both can be
	applied to the same cell; this one is text-only on a layer no
	extraction rule reads, so it is LVS-neutral by construction."""
	df.unlock()
	centers = device_centers(df)
	glayer = instance_label_glayer(pdk)
	if glayer is None:
		print(f"  note: {getattr(pdk, 'name', 'this PDK')} exposes no usable text/label "
		      f"glayer -- diff_pair instance names not drawn")
		return df.flatten()
	layer = pdk.get_glayer(glayer)
	device_of = {"tl": name_a, "br": name_a, "tr": name_b, "bl": name_b}
	for prefix, (x, y) in sorted(centers.items()):
		df.add_label(text=device_of[prefix], position=(x, y), layer=layer,
		             magnification=INSTANCE_LABEL_MAGNIFICATION)
	return df.flatten()


# Ports worth writing into the GDS as text when a human is going to open it
# and check the cell by eye. Deliberately NOT every port: a diff_pair carries
# several thousand of them (every finger's every contact, on every compass
# side), and drawing all of them buries the geometry in overlapping text.
# These are the terminal-level ones -- each placed device's own
# gate/drain/source, the endpoints of every route this cell lays, the tap
# ring, and the boundary breakouts -- which is what "which port is this?"
# actually means when reading the layout.
PORT_LABEL_PATTERNS = (
	r'^(tl|tr|bl|br)_multiplier_\d+_(gate|drain|source)_S$',
	r'^(tl|tr|bl|br)_col_\d+_(gate|drain|source)_S$',
	r'_bo_[NS]$',
	r'^(MINUS|PLUS)gateroute_[EW]_con_[NS]$',
	r'^source_route[EW]_con_[NS]$',
	r'^drain_route(TR_BL|TL_BR)_con_[NS]$',
	r'^tap_[NSEW]_top_met_[NSEW]$',
)

PORT_LABEL_MAGNIFICATION = 0.25


def port_table(df: Component) -> list:
	"""Every port on the Component as `(name, x, y, width, orientation,
	layer)` rows, sorted by name -- the exhaustive list that
	`add_port_debug_labels()` deliberately does not draw. Written out
	alongside the GDS so a manual check can look a port up by name and
	confirm the coordinate it was expected at."""
	rows = []
	for name in sorted(df.ports.keys()):
		prt = df.ports[name]
		rows.append((name, float(prt.center[0]), float(prt.center[1]),
		             float(prt.width), float(prt.orientation), tuple(prt.layer)))
	return rows


def add_port_debug_labels(df: Component, pdk: MappedPDK, all_ports: bool = False) -> Component:
	"""Draw each port's OWN NAME as text at its own location, for a human
	reading the GDS. Returns `df` (modified in place, then flattened).

	Text only, on the same generic annotation glayer `add_instance_labels()`
	uses (`instance_label_glayer()`), for the same reason given there: a
	met*_pin rectangle gets promoted to a real electrical pin during
	extraction and can break LVS, while a name is pure human annotation. So
	this pass is LVS-neutral by construction and can be stacked on top of
	`add_df_labels()`'s real pins.

	`all_ports=True` labels literally every port instead of the terminal-level
	subset in `PORT_LABEL_PATTERNS` -- thousands of them, drawn on top of each
	other. Useful only when hunting one specific per-finger contact by name;
	`port_table()` is the readable way to see all of them."""
	df.unlock()
	glayer = instance_label_glayer(pdk)
	if glayer is None:
		print(f"  note: {getattr(pdk, 'name', 'this PDK')} exposes no usable text/label "
		      f"glayer -- diff_pair port names not drawn")
		return df.flatten()
	layer = pdk.get_glayer(glayer)
	patterns = [re.compile(pat) for pat in PORT_LABEL_PATTERNS]
	drawn = 0
	for name in sorted(df.ports.keys()):
		if not all_ports and not any(pat.search(name) for pat in patterns):
			continue
		prt = df.ports[name]
		df.add_label(text=name, position=(float(prt.center[0]), float(prt.center[1])),
		             layer=layer, magnification=PORT_LABEL_MAGNIFICATION)
		drawn += 1
	print(f"  port labels drawn: {drawn} of {len(df.ports)} ports"
	      f"{'' if all_ports else ' (terminal-level subset)'}")
	return df.flatten()


def write_netlist_sp(component: Component, gds_path: str) -> str:
	"""Writes `component.info['netlist']`'s SPICE text to a `.sp` file next
	to a given `.gds` path (same basename, `.sp` extension instead) --
	mirrors current_mirror.py's own `write_netlist_sp()`. Returns the path
	written."""
	sp_path = str(gds_path).rsplit('.', 1)[0] + '.sp'
	with open(sp_path, 'w') as f:
		f.write(component.info['netlist'].generate_netlist())
	return sp_path


# Matched by shape (name, 4 nets, a *fet* model, trailing params), not by
# instance-name prefix -- same regex convention
# `../.claude/skills/sub-layout-handler/script/detect_topology.py` uses, kept as
# its own copy here (not imported) so this file stays runnable standalone,
# same reasoning that script's own module docstring gives for not
# importing its sibling parser.
_DEVICE_LINE_RE = re.compile(
	r'^\s*(?P<name>\S+)\s+(?P<nets>(?:\S+\s+){3}\S+)\s+(?P<model>\S*fet\S*)\s*(?P<params>.*)$',
	re.IGNORECASE,
)


def _parse_spice_params(params_str: str) -> dict:
	out = {}
	for tok in params_str.split():
		if '=' in tok:
			k, v = tok.split('=', 1)
			out[k.lower()] = v
	return out


def diff_pair_params_from_subckt(sp_path: str, subckt_name: Optional[str] = None) -> dict:
	"""Reads `width`/`fingers`/`length`/`n_or_p_fet` for `diff_pair()` out of
	a 2-MOSFET `.subckt` netlist -- e.g. one
	`../.claude/skills/circuit-decomposition/script/decompose_netlist.py` wrote
	(`DIFF_PAIR_1.sp`'s own `VP VN diff_M1_drain diff_M2_drain VTAIL B`
	pins) -- from the matched device pair's own `w=`/`m=`/`l=`/model text,
	not guessed. `m=` maps to `fingers`: this project's schematic-sizing
	convention uses `m`/`nf` as the fold count Nf, separate from `w` (per-
	finger width) -- matches how `nmos()`/`pmos()`'s OWN generated netlist
	uses one literal X-instance per finger at a fixed `w=<width>` with no
	`m=` at all (see `write_netlist_sp()`'s own output), so reading back
	`m=N` as `fingers=N` reproduces the same physical device. The matched
	pair must agree on kind/w/l/m (a diff pair's two devices always do) --
	raises `ValueError` if they don't rather than silently picking one
	side."""
	with open(sp_path) as f:
		lines = f.read().splitlines()
	joined = []
	for line in lines:
		if line.strip().startswith('+') and joined:
			joined[-1] += ' ' + line.strip()[1:]
		else:
			joined.append(line)

	if subckt_name is not None:
		block, in_block = [], False
		for line in joined:
			stripped = line.strip()
			if stripped.lower().startswith('.subckt'):
				in_block = stripped.split()[1] == subckt_name
			if in_block:
				block.append(line)
			if in_block and stripped.lower().startswith('.ends'):
				break
		joined = block

	devices = []
	for line in joined:
		m = _DEVICE_LINE_RE.match(line)
		if m:
			devices.append({'name': m.group('name'), 'model': m.group('model'),
				'params': _parse_spice_params(m.group('params'))})
	if len(devices) != 2:
		raise ValueError(f"{sp_path}: expected exactly 2 MOSFET device lines for a diff pair, found {len(devices)}")

	d0, d1 = devices
	if d0['model'].lower() != d1['model'].lower():
		raise ValueError(f"{sp_path}: device model mismatch between {d0['name']}/{d1['name']} "
			f"({d0['model']} != {d1['model']})")
	for key in ('w', 'l', 'm'):
		if d0['params'].get(key) != d1['params'].get(key):
			raise ValueError(f"{sp_path}: '{key}' mismatch between {d0['name']}/{d1['name']} "
				f"({d0['params'].get(key)} != {d1['params'].get(key)})")

	model = d0['model'].lower()
	if 'nfet' in model:
		n_or_p_fet = True
	elif 'pfet' in model:
		n_or_p_fet = False
	else:
		raise ValueError(f"{sp_path}: device model '{d0['model']}' is neither nfet nor pfet")

	return {
		'width': float(d0['params']['w']),
		'fingers': int(float(d0['params']['m'])) if 'm' in d0['params'] else 1,
		'length': float(d0['params']['l']) if 'l' in d0['params'] else None,
		'n_or_p_fet': n_or_p_fet,
	}


def diff_pair_from_subckt(pdk: MappedPDK, sp_path: str, subckt_name: Optional[str] = None, **kwargs) -> Component:
	"""Builds a `diff_pair()` Component sized directly from a 2-device
	`.subckt` netlist's own device parameters (see
	`diff_pair_params_from_subckt()`), instead of the caller hand-specifying
	width/fingers/n_or_p_fet. Any other `diff_pair()` keyword (`angle`,
	`dummy`, `substrate_tap`, `rmult`, `dum_net`, `plus_minus_seperation`)
	still passes through via `**kwargs`."""
	params = diff_pair_params_from_subckt(sp_path, subckt_name=subckt_name)
	return diff_pair(pdk, **params, **kwargs)


def _common_centroid_row_groups(pdk: MappedPDK, sdlayer: str, model: str,
                                width: float, length: Optional[float], fingers: int,
                                rmult: int, multipliers: int,
                                outer_dummy: tuple[bool, bool], tap_sdlayer: str) -> tuple:
	"""The two devices' halves of ONE common-centroid row, as
	`(outer_group, inner_group)` -- both centred on the row centre so the
	caller places them CONCENTRICALLY, interleaved rather than side by side.

	The row is `2 * multipliers` columns wide and reads A B B A: each device
	puts `multipliers/2` columns at each end of its own pattern, the outer
	device taking the two ends of the row and the inner device the middle.
	`diff_pair()` then gives the bottom row the opposite roles, so each device
	sits at all four positions and both centroids land on the row centre in x
	and on the cell centre in y. That is why `multipliers` must be even -- a
	device with an odd column count cannot be halved.

	The whole difficulty of an interleaved pair is that a device's columns are
	no longer contiguous, so something has to carry each device's terminals
	ACROSS the other device's columns. This does it the way glayout's own
	`two_transistor_interdigitized` does, which is the reason that cell works:
	the inner device's source/drain and gate rails are pushed further out from
	its fingers (`sd_route_extension` / `gate_route_extension`), so the two
	devices' rails sit at DIFFERENT heights. Each device then gets one bar
	spanning the full row at its own rail height, crossing the other device's
	columns without touching anything of theirs -- confirmed by tracing
	glayout's own cell: A and B stay separate nets while each one's west end
	is connected to its east end.

	The pay-off is that both devices present rail ends at the row's outer
	edges, exactly where a single contiguous device would put them, so
	`diff_pair()`'s existing routing reads the interleaved row unchanged.

	`multiplier()` is called directly rather than `nmos()`/`pmos()` because
	only it exposes those two extensions; the well, the well ports and the
	netlist that `nmos()` would have added are rebuilt here the same way it
	builds them.
	"""
	if multipliers % 2:
		raise ValueError(f"diff_pair: multipliers must be even for a common-centroid "
		                 f"placement (each device is split into two halves), got {multipliers}")
	half = multipliers // 2
	total_slots = 2 * multipliers
	kwargs = dict(pdk=pdk, sdlayer=sdlayer, width=width, length=length,
	              fingers=fingers, rmult=rmult)

	# Column variants. Dummies only at the row's two outer faces, which belong
	# to the outer device; every interior face meets another column.
	outer_plain = multiplier(**kwargs, dummy=False)
	west_dummy = (True, False) if outer_dummy[0] else False
	east_dummy = (False, True) if outer_dummy[1] else False
	outer_west = multiplier(**kwargs, dummy=west_dummy) if outer_dummy[0] else outer_plain
	outer_east = multiplier(**kwargs, dummy=east_dummy) if outer_dummy[1] else outer_plain
	# The inner device's rails clear the outer device's by the same margin
	# glayout's interdigitized macro uses: one metal separation past the
	# distance from the rail to the edge of the diffusion it sits over.
	sd_extension = pdk.snap_to_2xgrid(
		pdk.util_max_metal_seperation()
		+ abs(float(outer_plain.ports["drain_N"].center[1])
		      - float(outer_plain.ports["diff_N"].center[1])))
	gate_extension = pdk.snap_to_2xgrid(
		pdk.util_max_metal_seperation()
		+ abs(float(outer_plain.ports["row0_col0_gate_S"].center[1])
		      - float(outer_plain.ports["gate_S"].center[1])))
	inner_plain = multiplier(**kwargs, dummy=False, sd_route_extension=sd_extension,
	                         gate_route_extension=gate_extension)

	# One substrate tap on the row's symmetry axis, between the inner device's
	# two halves. The row grows with `multipliers` while the tap ring stays on
	# the perimeter, so without it the middle of a wide row ends up further
	# from a tap than latch-up allows; on the axis it costs the pattern
	# nothing, since the halves it separates are tied by the bar above anyway.
	column_height = float(outer_plain.ymax - outer_plain.ymin)
	tap_opening = max(pdk.get_grule("active_tap")["min_separation"],
	                  pdk.get_grule("active_tap")["min_width"]
	                  + 2 * pdk.get_grule(tap_sdlayer, "active_tap")["min_enclosure"])
	probe = tapring(pdk, enclosed_rectangle=(tap_opening, column_height),
	                sdlayer=tap_sdlayer, horizontal_glayer="met1", vertical_glayer="met1")
	tap = tapring(pdk, enclosed_rectangle=(tap_opening,
	                                       max(column_height - (float(probe.ymax - probe.ymin) - column_height),
	                                           tap_opening)),
	              sdlayer=tap_sdlayer, horizontal_glayer="met1", vertical_glayer="met1")
	clearance = max(pdk.get_grule("active_diff", "active_tap")["min_separation"],
	                pdk.util_max_metal_seperation())

	column_width = float(evaluate_bbox(outer_plain)[0])
	pitch = pdk.snap_to_2xgrid(column_width + pdk.get_grule("active_diff")["min_separation"],
	                           return_type="float")
	axis_extra = max(0.0, float(tap.xmax - tap.xmin) + 2 * clearance - (pitch - column_width))

	outer_group, inner_group = Component(), Component()
	slot_x, refs = {}, {}
	for slot in range(total_slots):
		is_outer = slot < half or slot >= total_slots - half
		x = (slot - (total_slots - 1) / 2) * pitch
		x += axis_extra / 2 if slot >= multipliers else -axis_extra / 2
		x = pdk.snap_to_2xgrid(x, return_type="float")
		column = (outer_west if slot == 0 else
		          outer_east if slot == total_slots - 1 else
		          outer_plain if is_outer else inner_plain)
		group = outer_group if is_outer else inner_group
		ref = group << column
		ref.movex(x)
		slot_x[slot] = x
		refs[slot] = (ref, is_outer)
		for name, prt in ref.ports.items():
			group.add_port(name=f"col_{slot}_{name}", port=prt)
	tap_ref = inner_group << tap
	tap_ref.movex(0 - (float(tap.xmax) + float(tap.xmin)) / 2)

	# One bar per terminal per device, spanning the row at that device's own
	# rail height. Drawn from the row's outer rail ends so both devices report
	# their terminals at the same place a contiguous device would.
	west_ref, east_ref = refs[0][0], refs[total_slots - 1][0]
	for group, sample in ((outer_group, refs[0][0]), (inner_group, refs[half][0])):
		for terminal in ("source", "drain", "gate"):
			x0 = float(west_ref.ports[f"{terminal}_W"].center[0])
			x1 = float(east_ref.ports[f"{terminal}_E"].center[0])
			own = sample.ports[f"{terminal}_E"]
			y, w, layer = float(own.center[1]), float(own.width), own.layer
			group.add_polygon([(x0, y - w / 2), (x1, y - w / 2),
			                   (x1, y + w / 2), (x0, y + w / 2)], layer=layer)
			# The N/S pair is what the drain breakout reads its x from, so the
			# two devices' are offset to opposite quarters of the row -- both
			# still land on their own bar, but the two breakout columns that
			# run up from them do not land on top of each other.
			nx = (x0 + x1) / 2 + (x1 - x0) / 4 * (1 if group is inner_group else -1)
			for base in (f"multiplier_0_{terminal}", terminal):
				group.add_port(name=f"{base}_W", center=(x0, y), width=w,
				               orientation=180, layer=layer)
				group.add_port(name=f"{base}_E", center=(x1, y), width=w,
				               orientation=0, layer=layer)
				group.add_port(name=f"{base}_N", center=(nx, y + w / 2), width=x1 - x0,
				               orientation=90, layer=layer)
				group.add_port(name=f"{base}_S", center=(nx, y - w / 2), width=x1 - x0,
				               orientation=270, layer=layer)

	netlists = {}
	for group, name in ((outer_group, "outer"), (inner_group, "inner")):
		own_slots = [s for s in range(total_slots) if (refs[s][1] == (group is outer_group))]
		for slot in own_slots:
			ref = refs[slot][0]
			for side, tag in (("L", "W"), ("R", "E")):
				port = f"dummy_{side}_gsdcon_top_met_{tag}"
				if port in ref.ports:
					for edge in ("N", "E", "S", "W"):
						src = f"dummy_{side}_gsdcon_top_met_{edge}"
						if src in ref.ports:
							group.add_port(name=f"multiplier_0_{src}", port=ref.ports[src])
		for edge, slot in (("W", own_slots[0]), ("E", own_slots[-1])):
			group.add_port(name=f"multiplier_0_plusdoped_{edge}",
			               port=refs[slot][0].ports[f"plusdoped_{edge}"])
		group.add_padding(layers=(pdk.get_glayer("pwell" if "n+" in sdlayer else "nwell"),),
		                  default=pdk.get_grule("pwell", "active_tap")["min_enclosure"])
		# One netlist instance per column, each with that column's own dummy
		# count -- columns that differ are named apart so they do not collide
		# on one `.subckt` in the generated SPICE.
		netlist = Netlist(circuit_name=f"{model_subckt_name(model)}_{name.upper()}_M{multipliers}",
		                  nodes=["D", "G", "S", "B", "DUM"])
		for slot in own_slots:
			cfg = (west_dummy if slot == 0 else
			       east_dummy if slot == total_slots - 1 else False)
			column_netlist = fet_netlist(pdk, circuit_name=model_subckt_name(model),
			                             model=model, width=width, length=length,
			                             fingers=fingers, multipliers=1, with_dummy=cfg)
			column_netlist.circuit_name += "_D" + (
				("L" if cfg and cfg[0] else "") + ("R" if cfg and cfg[1] else "") or "0")
			netlist.connect_netlist(column_netlist,
			                        [(n, n) for n in ("D", "G", "S", "B", "DUM")])
		netlists[group] = netlist

	groups = []
	for group in (outer_group, inner_group):
		x0, y0 = float(group.xmin), float(group.ymin)
		x1, y1 = float(group.xmax), float(group.ymax)
		well_layer = pdk.get_glayer("pwell" if "n+" in sdlayer else "nwell")
		for side, center, w, orientation in (
				("E", (x1, (y0 + y1) / 2), y1 - y0, 0),
				("W", (x0, (y0 + y1) / 2), y1 - y0, 180),
				("N", ((x0 + x1) / 2, y1), x1 - x0, 90),
				("S", ((x0 + x1) / 2, y0), x1 - x0, 270)):
			group.add_port(name=f"well_{side}", center=center, width=w,
			               orientation=orientation, layer=well_layer)
		snapped = component_snap_to_grid(group)
		snapped.info["netlist"] = netlists[group]
		groups.append(snapped)
	return tuple(groups)


def model_subckt_name(model: str) -> str:
	"""A SPICE-safe subcircuit name for a device model -- `NMOS`/`PMOS` where
	the model says so, mirroring what `nmos()`/`pmos()` name their own."""
	return "PMOS" if "pfet" in model.lower() else "NMOS"


@cell
def diff_pair(
	pdk: MappedPDK,
	width: float = 3,
	fingers: int = 4,
	multipliers: int = 1,
	length: Optional[float] = None,
	n_or_p_fet: bool = True,
	plus_minus_seperation: float = 0,
	rmult: int = 1,
	dummy: Union[bool, tuple[bool, bool]] = True,
	substrate_tap: bool=True,
	dum_net: Optional[str] = None,
	angle: int = 0,
) -> Component:
	"""create a diffpair with 2 transistors placed in two rows with common centroid place. Sources are shorted
	width = width of the transistors
	fingers = number of fingers in the transistors (must be 2 or more)
	multipliers = number of parallel copies of the transistor (SPICE `m`), each
		of `fingers` fingers of `width`. Drawn as COLUMNS: the copies sit side
		by side across the width direction, rails abutting, tied end to end --
		see `_multiplier_array_x()`, including why they are not glayout's own
		stacked rows. Total drawn width per transistor is
		width * fingers * multipliers, and the netlist carries one instance of
		the single-column subcircuit per copy, so LVS sees the same device
		count either way. multipliers=1 draws exactly what it always did.
	length = length of the transistors, None or 0 means use min length
	short_source = if true connects source of both transistors
	n_or_p_fet = if true the diffpair is made of nfets else it is made of pfets
	substrate_tap: if true place a tapring around the diffpair (connects on met1)
	angle: rotate the finished cell by this many degrees (0 or 90 only). Applied
		last, after ports/netlist are set, via Component.rotate() -- which
		auto-transforms port position/orientation and carries component.info
		(so the netlist survives). Port names keep their pre-rotation N/S/E/W
		suffix (e.g. "tl_gate_W" stays "tl_gate_W" even once physically
		pointing north after a 90 rotation) since they identify the same
		signal, not a direction.
	"""
	# TODO: error checking
	pdk.activate()
	diffpair = Component()
	# create transistors
	well = None
	if isinstance(dummy, bool):
		dummy = (dummy, dummy)
	# multipliers > 1 is drawn as a common-centroid row -- see
	# `_common_centroid_row_groups()`. `outer`/`inner` are the two ROLES in that
	# row, not the two devices: each device takes the outer role in one row and
	# the inner role in the other, which is what puts both centroids on the
	# cell centre. With a single multiplier there is nothing to interleave and
	# the pair is built exactly as it always was.
	interleaved = multipliers > 1
	if n_or_p_fet:
		well, tap_sdlayer, sdlayer, model = "pwell", "p+s/d", "n+s/d", pdk.models["nfet"]
	else:
		well, tap_sdlayer, sdlayer, model = "nwell", "n+s/d", "p+s/d", pdk.models["pfet"]
	if interleaved:
		fetL, fetR = _common_centroid_row_groups(
			pdk, sdlayer, model, width, length, fingers, rmult, multipliers,
			(dummy[0], dummy[1]), tap_sdlayer)
	elif n_or_p_fet:
		fetL = nmos(pdk, width=width, fingers=fingers,length=length,multipliers=1,with_tie=False,with_dummy=(dummy[0], False),with_dnwell=False,with_substrate_tap=False,rmult=rmult)
		fetR = nmos(pdk, width=width, fingers=fingers,length=length,multipliers=1,with_tie=False,with_dummy=(False,dummy[1]),with_dnwell=False,with_substrate_tap=False,rmult=rmult)
	else:
		fetL = pmos(pdk, width=width, fingers=fingers,length=length,multipliers=1,with_tie=False,with_dummy=(dummy[0], False),dnwell=False,with_substrate_tap=False,rmult=rmult)
		fetR = pmos(pdk, width=width, fingers=fingers,length=length,multipliers=1,with_tie=False,with_dummy=(False,dummy[1]),dnwell=False,with_substrate_tap=False,rmult=rmult)
	min_spacing_x = pdk.get_grule(sdlayer)["min_separation"] - 2*(fetL.xmax - fetL.ports["multiplier_0_plusdoped_E"].center[0])
	# place transistors
	viam2m3 = via_stack(pdk,"met2","met3",centered=True)
	metal_min_dim = max(pdk.get_grule("met2")["min_width"],pdk.get_grule("met3")["min_width"])
	metal_space = max(pdk.get_grule("met2")["min_separation"],pdk.get_grule("met3")["min_separation"],metal_min_dim)
	gate_route_os = evaluate_bbox(viam2m3)[0] - fetL.ports["multiplier_0_gate_W"].width + metal_space
	min_spacing_y = metal_space + 2*gate_route_os
	min_spacing_y = min_spacing_y - 2*abs(fetL.ports["well_S"].center[1] - fetL.ports["multiplier_0_gate_S"].center[1])
	# TODO: fix spacing where you see +-0.5
	# Interleaved, the two roles occupy the SAME row and are placed
	# concentrically -- one device's columns sit in the gaps of the other's, so
	# there is no x offset to compute and `min_spacing_x` does not apply. The
	# roles swap between rows: A is outer on top and inner underneath, B the
	# other way round, so each device covers all four positions.
	row_ymax = max(float(fetL.ymax), float(fetR.ymax))
	x_offset = 0.0 if interleaved else fetL.xmax + min_spacing_x/2
	top_outer, top_inner = fetL, fetR
	bot_outer, bot_inner = fetL, fetR
	a_topl = (diffpair << top_outer).movey(row_ymax+min_spacing_y/2+0.5).movex(0-x_offset)
	b_topr = (diffpair << top_inner).movey(row_ymax+min_spacing_y/2+0.5).movex(x_offset)
	a_botr = (diffpair << bot_inner)
	a_botr = a_botr.mirror_y()
	a_botr.movey(0-0.5-row_ymax-min_spacing_y/2).movex(x_offset)
	b_botl = (diffpair << bot_outer)
	b_botl = b_botl.mirror_y()
	b_botl.movey(0-0.5-row_ymax-min_spacing_y/2).movex(0-x_offset)
	# if substrate tap place substrate tap
	if substrate_tap:
		# The ring must be doped to tie THIS pair's well, so its `sdlayer`
		# follows `well` above: p+ taps a pwell (nfet pair), n+ taps an
		# nwell (pfet pair). `tapring()`'s default is "p+s/d", so omitting
		# this drew a P+ ring around a PFET pair's nwell -- 935um^2 of
		# nwell with zero N+ tie, which Magic reports as `nwell.4` ("all
		# nwells must contain metal-connected N+ taps") plus a spray of
		# `LU.3` latch-up violations, none of them reachable by placement
		# or routing. `substrate_tap` is not a workaround: it gates whether
		# a ring is drawn at all, not what it is doped. See
		# `current_mirror.py`'s `tap_sdlayer`, which has always done this.
		tapref = diffpair << tapring(pdk,evaluate_bbox(diffpair,padding=1),sdlayer=tap_sdlayer,horizontal_glayer="met1")
		diffpair.add_ports(tapref.get_ports_list(),prefix="tap_")
		# Every dummy this pair draws, tied to the ring. Which reference owns
		# which dummy depends on the placement: side by side, the left device
		# carries the west dummy and the right device the east one, while
		# interleaved BOTH belong to whichever device took the outer role in
		# that row -- the inner device has no dummies at all, since none of its
		# faces meet open canvas. Trying every combination covers both, and an
		# absent dummy is simply a port that is not there.
		for ref, side, tapside in ((a_topl, "L", "W"), (a_topl, "R", "E"),
		                           (b_topr, "L", "W"), (b_topr, "R", "E"),
		                           (b_botl, "L", "W"), (b_botl, "R", "E"),
		                           (a_botr, "L", "W"), (a_botr, "R", "E")):
			try:
				diffpair << straight_route(pdk,
					ref.ports[f"multiplier_0_dummy_{side}_gsdcon_top_met_W"],
					diffpair.ports[f"tap_{tapside}_top_met_{tapside}"], glayer2="met1")
			except KeyError:
				pass
	# route sources (short sources)
	if interleaved:
		# A quad between the two devices' source ports is what does this in a
		# side-by-side pair, and it is exactly wrong here: interleaved, the two
		# devices' rails sit at DIFFERENT heights, so a quad spanning from one
		# source to the other covers the whole band between them -- including
		# the outer device's drain bar, which runs between the two source bars.
		# Measured: drain shorted to source and to the other drain, on both
		# rows. The two source bars are tied with a jumper over that drain bar
		# instead: up to met3 on one, across, down on the other.
		#
		# Bare via cuts rather than a via_stack, for the same reason the drain
		# breakout below uses them: the stack's 0.43um met2 pad is wider than
		# the 0.29um bar it would sit on and would crowd the drain bar 0.14um
		# above it. The bar encloses the cut along its own length.
		via_cut = float(pdk.get_grule("via2")["width"])
		via_pad = via_cut + 2 * float(pdk.get_grule("met3", "via2")["min_enclosure"])
		for outer_ref, inner_ref in ((a_topl, b_topr), (b_botl, a_botr)):
			ys = [float(outer_ref.ports["multiplier_0_source_E"].center[1]),
			      float(inner_ref.ports["multiplier_0_source_E"].center[1])]
			# On the row's symmetry axis: clear of the drain breakouts, which
			# sit a quarter of the row either side of it.
			jx = 0.0
			for jy in ys:
				for layer, size in ((pdk.get_glayer("via2"), via_cut),
				                    (pdk.get_glayer("met3"), via_pad)):
					diffpair.add_polygon(
						[(jx - size / 2, jy - size / 2), (jx + size / 2, jy - size / 2),
						 (jx + size / 2, jy + size / 2), (jx - size / 2, jy + size / 2)],
						layer=layer)
			diffpair.add_polygon(
				[(jx - via_pad / 2, min(ys)), (jx + via_pad / 2, min(ys)),
				 (jx + via_pad / 2, max(ys)), (jx - via_pad / 2, max(ys))],
				layer=pdk.get_glayer("met3"))
	else:
		diffpair << route_quad(a_topl.ports["multiplier_0_source_E"], b_topr.ports["multiplier_0_source_W"], layer=pdk.get_glayer("met2"))
		diffpair << route_quad(b_botl.ports["multiplier_0_source_E"], a_botr.ports["multiplier_0_source_W"], layer=pdk.get_glayer("met2"))
	# How far out of the device the source c_route's vertical riser sits. The
	# well edge is enough with one multiplier, but not with more: glayout ties
	# multiplier rows together with its own c_routes that stick out PAST the
	# well on the source/drain side, and a riser placed at the well edge lands
	# on top of them -- measured on a width=16/fingers=2/multipliers=2 pair,
	# 0.04um met3 slivers where the two overlap plus 0.125um spacing gaps
	# against the tie riser. Each reference's own bbox does include those ties,
	# so the riser is pushed to whichever is further out, plus a metal spacing.
	sextension = b_topr.ports["well_E"].center[0] - b_topr.ports["multiplier_0_source_E"].center[0]
	source_routeE = diffpair << c_route(pdk, b_topr.ports["multiplier_0_source_E"], a_botr.ports["multiplier_0_source_E"],extension=sextension, viaoffset=False)
	source_routeW = diffpair << c_route(pdk, a_topl.ports["multiplier_0_source_W"], b_botl.ports["multiplier_0_source_W"],extension=sextension, viaoffset=False)
	# route drains
	# place via at the drain
	drain_br_via = diffpair << viam2m3
	drain_bl_via = diffpair << viam2m3
	drain_br_via.move(a_botr.ports["multiplier_0_drain_N"].center).movey(viam2m3.ymin)
	drain_bl_via.move(b_botl.ports["multiplier_0_drain_N"].center).movey(viam2m3.ymin)
	drain_br_viatm = diffpair << viam2m3
	drain_bl_viatm = diffpair << viam2m3
	drain_br_viatm.move(a_botr.ports["multiplier_0_drain_N"].center).movey(viam2m3.ymin)
	# Interleaved, the two devices' rails are staggered, so the OUTER device's
	# drain bar -- which this crossover hangs off -- is no longer the lowest
	# thing in the row: the inner device's source and drain bars sit below it.
	# Everything the crossover drops past them has to clear that stagger, or
	# the descent lands straight on them (measured: the intermediate via came
	# down on the inner device's source bar, and the route's arm overlapped its
	# drain bar). Zero when there is nothing interleaved.
	rail_stagger = (abs(float(fetR.ports["multiplier_0_drain_E"].center[1])
	                    - float(fetL.ports["multiplier_0_drain_E"].center[1]))
	                if interleaved else 0.0)
	drain_bl_viatm.move(b_botl.ports["multiplier_0_drain_N"].center).movey(-1.5 * evaluate_bbox(viam2m3)[1] - metal_space - rail_stagger)
	# create route to drain via
	width_drain_route = b_topr.ports["multiplier_0_drain_E"].width
	# Add an rmult-scaled margin so the drain c-bar clears the source c-bar
	# even at higher rmult (where both bars get wider). The original
	# `+ metal_space` left only 0.05um at rmult=2 and 0.1um at rmult=3 on
	# gf180 (M3.2a slivers); scaling with rmult keeps a full met3 spacing.
	dextension = source_routeE.xmax - b_topr.ports["multiplier_0_drain_E"].center[0] + (1 + rmult) * metal_space
	bottom_extension = viam2m3.ymax + width_drain_route/2 + 2*metal_space + rail_stagger
	drain_br_viatm.movey(0-bottom_extension - metal_space - width_drain_route/2 - viam2m3.ymax)
	diffpair << route_quad(drain_br_viatm.ports["top_met_N"], drain_br_via.ports["top_met_S"], layer=pdk.get_glayer("met3"))
	diffpair << route_quad(drain_bl_viatm.ports["top_met_N"], drain_bl_via.ports["top_met_S"], layer=pdk.get_glayer("met3"))
	floating_port_drain_bottom_L = set_port_orientation(movey(drain_bl_via.ports["bottom_met_W"],0-bottom_extension), get_orientation("E"))
	floating_port_drain_bottom_R = set_port_orientation(movey(drain_br_via.ports["bottom_met_E"],0-bottom_extension - metal_space - width_drain_route), get_orientation("W"))
	drain_routeTR_BL = diffpair << c_route(pdk, floating_port_drain_bottom_L, b_topr.ports["multiplier_0_drain_E"],extension=dextension, width1=width_drain_route,width2=width_drain_route)
	drain_routeTL_BR = diffpair << c_route(pdk, floating_port_drain_bottom_R, a_topl.ports["multiplier_0_drain_W"],extension=dextension, width1=width_drain_route,width2=width_drain_route)
	# cross gate route top with c_route. bar_minus ABOVE bar_plus
	get_left_extension = lambda bar, a_topl=a_topl, diffpair=diffpair, pdk=pdk : (abs(diffpair.xmin-min(a_topl.ports["multiplier_0_gate_W"].center[0],bar.ports["e1"].center[0])) + pdk.get_grule("met2")["min_separation"])
	get_right_extension = lambda bar, b_topr=b_topr, diffpair=diffpair, pdk=pdk : (abs(diffpair.xmax-max(b_topr.ports["multiplier_0_gate_E"].center[0],bar.ports["e3"].center[0])) + pdk.get_grule("met2")["min_separation"])
	# lay bar plus and PLUSgate_routeW
	bar_comp = rectangle(centered=True,size=(abs(b_topr.xmax-a_topl.xmin), b_topr.ports["multiplier_0_gate_E"].width),layer=pdk.get_glayer("met2"))
	bar_plus = (diffpair << bar_comp).movey(diffpair.ymax + bar_comp.ymax + pdk.get_grule("met2")["min_separation"])
	PLUSgate_routeW = diffpair << c_route(pdk, a_topl.ports["multiplier_0_gate_W"], bar_plus.ports["e1"], extension=get_left_extension(bar_plus))
	# lay bar minus and MINUSgate_routeE
	plus_minus_seperation = max(pdk.get_grule("met2")["min_separation"], plus_minus_seperation)
	bar_minus = (diffpair << bar_comp).movey(diffpair.ymax +bar_comp.ymax + plus_minus_seperation)
	MINUSgate_routeE = diffpair << c_route(pdk, b_topr.ports["multiplier_0_gate_E"], bar_minus.ports["e3"], extension=get_right_extension(bar_minus))
	# lay MINUSgate_routeW and PLUSgate_routeE
	MINUSgate_routeW = diffpair << c_route(pdk, set_port_orientation(b_botl.ports["multiplier_0_gate_E"],"W"), bar_minus.ports["e1"], extension=get_left_extension(bar_minus))
	PLUSgate_routeE = diffpair << c_route(pdk, set_port_orientation(a_botr.ports["multiplier_0_gate_W"],"E"), bar_plus.ports["e3"], extension=get_right_extension(bar_plus))
	# correct pwell place, add ports, flatten, and return
	diffpair.add_ports(a_topl.get_ports_list(),prefix="tl_")
	diffpair.add_ports(b_topr.get_ports_list(),prefix="tr_")
	diffpair.add_ports(b_botl.get_ports_list(),prefix="bl_")
	diffpair.add_ports(a_botr.get_ports_list(),prefix="br_")
	diffpair.add_ports(source_routeE.get_ports_list(),prefix="source_routeE_")
	diffpair.add_ports(source_routeW.get_ports_list(),prefix="source_routeW_")
	diffpair.add_ports(drain_routeTR_BL.get_ports_list(),prefix="drain_routeTR_BL_")
	diffpair.add_ports(drain_routeTL_BR.get_ports_list(),prefix="drain_routeTL_BR_")
	diffpair.add_ports(MINUSgate_routeW.get_ports_list(),prefix="MINUSgateroute_W_")
	diffpair.add_ports(MINUSgate_routeE.get_ports_list(),prefix="MINUSgateroute_E_")
	diffpair.add_ports(PLUSgate_routeW.get_ports_list(),prefix="PLUSgateroute_W_")
	diffpair.add_ports(PLUSgate_routeE.get_ports_list(),prefix="PLUSgateroute_E_")

	# Bring both drains straight UP from their own device ports to the TOP
	# edge of the macro on met3 (GDS 69/20), and put a real port there, so a
	# router can land on each at the boundary instead of having to cut into
	# the macro to reach a drain sitting ~2.9um inside it.
	#
	# The edge is the WELL outline -- the `well` layer padded on immediately
	# below, GDS 64/44 for an nfet pair -- not the tap ring's bbox (94/20);
	# the two differ (measured: ring top y=7.710, well top y=8.850, the gate
	# bars and gate c_routes overhanging the ring).
	#
	# `add_padding(..., default=0)` draws that well at exactly diffpair.bbox,
	# so the bbox read here IS the well outline. Read BEFORE drawing, since
	# the traces must not be what defines the edge they run to; they only
	# ever run up to the existing top, so the bbox cannot grow and the two
	# stay consistent.
	#
	# The trace runs on met3 rather than the drain's own met2 because met2 in
	# this corridor is taken -- the two gate bars and their c_routes span the
	# full cell width just above the devices. met3 is free here: measured,
	# every met3 polygon in this cell above the devices sits at |x| > 5.4
	# (the drain crossover and gate c_route columns hug the outer edges),
	# while these two traces run at x = -1.845 and +1.845.
	#
	# The via is a bare cut with NO widened met2 pad, for the reason measured
	# in ../current_mirror.py's own _stretch_ports_to_ring(): glayout's
	# via_stack(met2,met3) draws a 0.430um met2 pad, wider than the 0.290um
	# drain strip it would sit on. The strip encloses the 0.150um cut on its
	# own.
	well_y1 = float(diffpair.bbox[1][1])
	drain_bo_x = []
	via_cut = float(pdk.get_grule("via2")["width"])
	via_pad = via_cut + 2 * float(pdk.get_grule("met3", "via2")["min_enclosure"])
	for dev_prefix, port_name in (("tl", "diff_M1_drain_bo_N"),
	                               ("tr", "diff_M2_drain_bo_N")):
		n_port = diffpair.ports[f"{dev_prefix}_multiplier_0_drain_N"]
		s_port = diffpair.ports[f"{dev_prefix}_multiplier_0_drain_S"]
		e_port = diffpair.ports[f"{dev_prefix}_multiplier_0_drain_E"]
		cx = float(n_port.center[0])
		# Centre of the drain strip, not its N edge -- the via has to sit on
		# the metal, and `..._drain_N` is that strip's top boundary.
		cy = (float(n_port.center[1]) + float(s_port.center[1])) / 2
		w = float(e_port.width)          # strip thickness, 0.290um by default
		diffpair.add_polygon(
			[(cx - via_cut / 2, cy - via_cut / 2), (cx + via_cut / 2, cy - via_cut / 2),
			 (cx + via_cut / 2, cy + via_cut / 2), (cx - via_cut / 2, cy + via_cut / 2)],
			layer=pdk.get_glayer("via2"))
		diffpair.add_polygon(
			[(cx - via_pad / 2, cy - via_pad / 2), (cx + via_pad / 2, cy - via_pad / 2),
			 (cx + via_pad / 2, cy + via_pad / 2), (cx - via_pad / 2, cy + via_pad / 2)],
			layer=pdk.get_glayer("met3"))
		diffpair.add_polygon(
			[(cx - w / 2, cy - via_pad / 2), (cx + w / 2, cy - via_pad / 2),
			 (cx + w / 2, well_y1), (cx - w / 2, well_y1)],
			layer=pdk.get_glayer("met3"))
		diffpair.add_port(name=port_name, center=(cx, well_y1), width=w,
		                  orientation=90, layer=pdk.get_glayer("met3"))
		drain_bo_x.append(cx)
		drain_bo_w = w

	# VTAIL (the two devices' shared source) out to the BOTTOM edge. It is
	# the last terminal here with no boundary port: measured, the nearest
	# real source metal sits 2.42um inside, so the router fell back to the
	# box-edge approximation and the tail simply never got connected --
	# after every other fix it was the single remaining net LVS could not
	# match, showing up as an internal diff_pair net touching 2 nfet
	# terminals (XMN1's and XMN2's sources).
	#
	# Cheap like the drains, and for the same reason: `source_routeE` is
	# already a full-height met3 column (the c_route shorting the two
	# sources), so this only extends it. It leaves through the bottom while
	# the drains leave through the top, and sits at x=+-5.635 -- clear of
	# the drain columns at +-6.625 by 0.63um of met3, and of the drain/B
	# breakouts at x=0/+-1.845 entirely.
	src = diffpair.ports["source_routeE_con_S"]
	scx, scy = float(src.center[0]), float(src.center[1])
	sw = float(src.width)
	well_y0 = float(diffpair.bbox[0][1])
	if scy > well_y0:
		diffpair.add_polygon(
			[(scx - sw / 2, well_y0), (scx + sw / 2, well_y0),
			 (scx + sw / 2, scy), (scx - sw / 2, scy)],
			layer=pdk.get_glayer("met3"))
	diffpair.add_port(name="VTAIL_bo_S", center=(scx, well_y0), width=sw,
	                  orientation=270, layer=pdk.get_glayer("met3"))

	# Bulk (B) gets the same treatment, brought up to the top edge MIDWAY
	# between the two drain traces. x is the mean of the two rather than a
	# literal 0 so it stays centred if the devices are ever resized.
	#
	# This one needs a real via_stack, unlike the drains: the tap ring's top
	# metal is met1 (`tapring(..., horizontal_glayer="met1")` above), so the
	# climb is met1->met2->met3, two vias, not one. via_stack is safe here
	# for the same reason it was NOT safe on the drains -- there is no
	# neighbouring bar 0.43um away for its 0.430um met2 pad to crowd. The
	# pad lands on the ring's north bar, whose nearest met2 in this corridor
	# is a gate bar 0.26um clear.
	if "tap_N_top_met_S" in diffpair.ports and len(drain_bo_x) == 2:
		bar_s = diffpair.ports["tap_N_top_met_S"]
		bar_n = diffpair.ports["tap_N_top_met_N"]
		bx = pdk.snap_to_2xgrid(sum(drain_bo_x) / 2, return_type="float")
		by = pdk.snap_to_2xgrid((float(bar_s.center[1]) + float(bar_n.center[1])) / 2,
		                        return_type="float")
		bvia = diffpair << via_stack(pdk, "met1", "met3")
		bvia.move((bx, by))
		# The stack's own met3 pad width -- NOT evaluate_bbox(), which would
		# return the wider met2 pad underneath and leave the trace overhanging
		# the pad it lands on.
		bw = float(bvia.ports["top_met_N"].width)
		diffpair.add_polygon(
			[(bx - bw / 2, by), (bx + bw / 2, by),
			 (bx + bw / 2, well_y1), (bx - bw / 2, well_y1)],
			layer=pdk.get_glayer("met3"))
		diffpair.add_port(name="B_bo_N", center=(bx, well_y1), width=bw,
		                  orientation=90, layer=pdk.get_glayer("met3"))

	# VP / VN -- the two gate buses -- brought out to the TOP edge as well,
	# one on either side of the drain pair, so the cell's two GATE terminals
	# leave the macro within a few um of each other and its two DRAIN
	# terminals likewise. Before this they did not: the only landing points
	# VP/VN had were on the gate c_routes' own side columns, at x = -+6.585
	# on the LEFT and RIGHT edges -- measured 13.17um apart on a
	# width=16/fingers=2 pair, and at mid-height, nowhere near the drains.
	# A differential parent routes VP and VN as one mirrored bus, so the two
	# of them sitting on opposite faces of the macro forced that bus all the
	# way around the cell.
	#
	# Each bar is a full-width horizontal met2 rectangle, so the x of a
	# breakout on it is free. It is placed one met3 spacing outside its own
	# device's drain column -- bar_plus feeds tl/br = diff_M1_drain, bar_minus
	# feeds tr/bl = diff_M2_drain, read off the add_ports() prefixes above --
	# so the top edge reads VP, M1_drain, B, M2_drain, VN left to right, and
	# stays mirror-symmetric about x=0 the way the rest of the cell is.
	#
	# This one needs a real via_stack where the drains needed a bare cut, and
	# for the mirror-image reason: met2 must enclose a via2 by 0.14um per side
	# (0.43um total) while a gate bar is only 0.33um tall, so a bare cut on a
	# bar would be a met2-enclosure violation. via_stack's own 0.43um met2 pad
	# supplies the enclosure and still fits -- it grows the bar by 0.05um per
	# side, and the two bars sit 0.24um apart, leaving 0.19um > met2's 0.14um
	# min separation.
	#
	# VP's met3 stub crosses over bar_minus on its way up. There is no via
	# there, so that is a layer crossing, not a short.
	if len(drain_bo_x) == 2:
		met3_sep = float(pdk.get_grule("met3")["min_separation"])
		for port_name, bar, drain_x, outward in (("VP_bo_N", bar_plus, drain_bo_x[0], -1.0),
		                                         ("VN_bo_N", bar_minus, drain_bo_x[1], 1.0)):
			gvia = diffpair << via_stack(pdk, "met2", "met3")
			# The stack's met3 pad width, not evaluate_bbox() -- same reason as
			# the B breakout above, where the wider met2 pad underneath would
			# leave the stub overhanging the pad it lands on.
			gw = float(gvia.ports["top_met_N"].width)
			gx = pdk.snap_to_2xgrid(drain_x + outward * (drain_bo_w / 2 + met3_sep + gw / 2),
			                        return_type="float")
			gy = pdk.snap_to_2xgrid((float(bar.ymin) + float(bar.ymax)) / 2, return_type="float")
			gvia.move((gx, gy))
			diffpair.add_polygon(
				[(gx - gw / 2, gy), (gx + gw / 2, gy),
				 (gx + gw / 2, well_y1), (gx - gw / 2, well_y1)],
				layer=pdk.get_glayer("met3"))
			diffpair.add_port(name=port_name, center=(gx, well_y1), width=gw,
			                  orientation=90, layer=pdk.get_glayer("met3"))

	diffpair.add_padding(layers=(pdk.get_glayer(well),), default=0)

	component = component_snap_to_grid(rename_ports_by_orientation(diffpair))

	component.info['netlist'] = diff_pair_netlist(fetL, fetR, pdk=pdk, dum_net=dum_net, interleaved=interleaved)

	if angle not in (0, 90):
		raise ValueError(f"diff_pair: angle must be 0 or 90, got {angle}")
	if angle != 0:
		component = component_snap_to_grid(component.rotate(angle))

	# gf180 LVS uses klayout's official deck which strictly requires named
	# pin labels on met*_label layers — without them, klayout extracts the
	# cell with only an implicit substrate port and LVS fails. sky130 LVS
	# via magic+netgen tolerates missing labels, so only emit the labels
	# for gf180. The B (bulk) label needs `substrate_tap=True` since it
	# anchors on `tap_N_top_met_S`, which only exists when the diffpair's
	# tap ring is drawn. Composite cells suppress this via GLAYOUT_NO_PIN_LABELS
	# so inner labels don't leak into the parent cell's GDS.
	import os
	if pdk.name.lower() == "gf180" and substrate_tap and not os.environ.get("GLAYOUT_NO_PIN_LABELS"):
		component = add_df_labels(component, pdk)
	return component



@cell
def diff_pair_generic(
	pdk: MappedPDK,
	width: float = 3,
	fingers: int = 4,
	length: Optional[float] = None,
	n_or_p_fet: bool = True,
	plus_minus_seperation: float = 0,
	rmult: int = 1,
	dummy: Union[bool, tuple[bool, bool]] = True,
	substrate_tap: bool=True
) -> Component:
	diffpair = common_centroid_ab_ba(pdk,width,fingers,length,n_or_p_fet,rmult,dummy,substrate_tap)
	diffpair << smart_route(pdk,diffpair.ports["A_source_E"],diffpair.ports["B_source_E"],diffpair, diffpair)
	return diffpair

if __name__ == "__main__":
	import argparse
	import csv
	from pathlib import Path

	parser = argparse.ArgumentParser(
		description="Build one diff_pair() cell and write its GDS, its SPICE "
		            "netlist and a table of every port. Defaults reproduce the "
		            "demo cell this file has always built.")
	parser.add_argument("--width", type=float, default=3, help="per-finger width w (um)")
	parser.add_argument("--fingers", type=int, default=4, help="fingers per row (nf)")
	parser.add_argument("--multipliers", type=int, default=1, help="multiplier rows per device (m)")
	parser.add_argument("--length", type=float, default=None, help="channel length (um); default min")
	parser.add_argument("--pfet", action="store_true", help="build a PFET pair instead of NFET")
	parser.add_argument("--angle", type=int, default=0, choices=(0, 90))
	parser.add_argument("--out", default="diff_pair.gds", help="output .gds path")
	parser.add_argument("--top", default="DIFF_PAIR", help="top cell name")
	parser.add_argument("--all-ports", action="store_true",
	                    help="label EVERY port in the GDS, not just the terminal-level subset")
	parser.add_argument("--evaluate", action="store_true", help="run the glayout evaluator on the result")
	args = parser.parse_args()

	pdk = sky130_mapped_pdk
	comp = diff_pair(pdk, width=args.width, fingers=args.fingers,
	                 multipliers=args.multipliers, length=args.length,
	                 n_or_p_fet=not args.pfet, angle=args.angle)
	# Three independent labelling passes, all applied: real electrical pins
	# (add_df_labels), instance names (add_instance_labels), and port names
	# (add_port_debug_labels). Only the first draws pin rectangles; the other
	# two are text on an annotation layer.
	comp = add_df_labels(comp, pdk)
	comp = add_instance_labels(comp, pdk)
	comp = add_port_debug_labels(comp, pdk, all_ports=args.all_ports)
	comp.name = args.top

	out = Path(args.out)
	comp.write_gds(str(out))
	sp_path = write_netlist_sp(comp, str(out))
	csv_path = out.with_name(out.stem + "_ports.csv")
	with open(csv_path, "w", newline="") as f:
		writer = csv.writer(f)
		writer.writerow(["port", "x_um", "y_um", "width_um", "orientation_deg", "gds_layer"])
		for name, x, y, w, o, layer in port_table(comp):
			writer.writerow([name, f"{x:.4f}", f"{y:.4f}", f"{w:.4f}", f"{o:.1f}", f"{layer[0]}/{layer[1]}"])

	print(f"\n{args.top}: w={args.width} nf={args.fingers} m={args.multipliers} "
	      f"{'pfet' if args.pfet else 'nfet'}  bbox={comp.bbox.tolist()}")
	print(f"  {out}\n  {sp_path}\n  {csv_path}")
	print("  terminal ports:")
	for label, val in routing_ports(comp).items():
		print(f"    {label:15s} ({val['x']:8.3f}, {val['y']:8.3f})  {val['layer']}  w={val['width']:.3f}")

	if args.evaluate and run_evaluation is not None:
		run_evaluation(str(out), comp.name, comp)
