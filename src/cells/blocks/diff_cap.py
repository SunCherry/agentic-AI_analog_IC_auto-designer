"""Differential MiM cap pair: two nominally identical capacitors sharing one
bottom plate, built from the unit cap in `mimcap.py`.

Three terminals, not four. The bottom plates of every unit merge into a single
solid plate -- `CM`, the shielded side, which a differential circuit ties to a
low-impedance node -- and each capacitor keeps its own top plate, `CA` and
`CB`. That is what makes the interleaved arrangement below tractable: with a
shared bottom there is exactly one net to route per capacitor, on one layer,
and the plate that would otherwise have to thread between the other cap's units
is simply the ground plane underneath everything.

Two arrangements, both drawn from the same unit and the same routing:

  common_centroid          side_by_side
  (A B B A in one column)  (two columns, mirrored)

      row3   A                 A  |  B
      row2   B                 A  |  B
      row1   B
      row0   A

`common_centroid` puts each capacitor's units symmetrically about the array's
centre, so both centroids land on the same point and a linear process gradient
across the die shifts the two equally -- the ratio survives it. It needs an
even `multipliers`, since each capacitor is split into two halves. Prefer it
whenever the two caps' RATIO matters. `side_by_side` is smaller and simpler,
and still mirror-symmetric, but the two centroids sit a block apart, so a
gradient makes one cap larger than the other.
"""
import math
import os
import sys
from pathlib import Path
from typing import Optional

from glayout.backend import Component, cell, rectangle
from glayout.pdk.mappedpdk import MappedPDK
from glayout.spice import Netlist
from glayout.util.port_utils import rename_ports_by_orientation

# `mimcap.py` lives in the sibling primitives/ directory, and it is THIS repo's
# copy that matters: it carries fixes glayout's own does not -- the l/w
# convention Magic actually extracts, literal dimensions in the netlist, and
# the compact array spacing. Reached by path because these modules are loaded
# flat, the same way `diff_pair.py` picks up `current_mirror.py` beside it.
_PRIMITIVES = Path(__file__).resolve().parent.parent / "primitives"
if str(_PRIMITIVES) not in sys.path:
	sys.path.insert(0, str(_PRIMITIVES))
from mimcap import mimcap, __get_mimcap_layerconstruction_info as _layerinfo


ARRANGEMENTS = ("common_centroid", "side_by_side")


def _add_pin(component: Component, pdk: MappedPDK, glayer: str, port_name: str,
             text: str) -> None:
	"""One terminal pin plus its label, stepped IN along the port's own outward
	normal so it sits on the metal it names instead of half over the edge.

	Same reasoning as `mimcap.py`'s own pins: without them Magic's
	`port makeall` has nothing to work from, the extracted cell comes out with
	no pins, and netgen ends on "failed pin matching" however correct the
	layout is. `GLAYOUT_NO_PIN_LABELS` suppresses them, the switch a composite
	parent uses to keep an inner cell's pins out of its own GDS.
	"""
	if os.environ.get("GLAYOUT_NO_PIN_LABELS"):
		return
	port = component.ports[port_name]
	size = pdk.snap_to_2xgrid(min(float(port.width), 1.0), return_type="float")
	pin = rectangle(layer=pdk.get_glayer(f"{glayer}_pin"), size=(size, size),
	                centered=True).copy()
	pin.add_label(text=text, layer=pdk.get_glayer(f"{glayer}_label"))
	theta = math.radians(float(port.orientation))
	ref = component << pin
	ref.move(destination=(float(port.center[0]) - math.cos(theta) * size / 2,
	                      float(port.center[1]) - math.sin(theta) * size / 2))


def _unit_slots(multipliers: int, arrangement: str) -> list:
	"""`(owner, column, row)` for every unit cap drawn, in placement order.

	`common_centroid` is TWO ROWS of `multipliers` columns, each row split in
	half and the halves swapped between rows:

	    row 1   B B A A
	    row 0   A A B B

	so each capacitor holds one half-row at the bottom and the opposite
	half-row at the top. Its units are then symmetric about the array's centre
	in x (a left half and a right half) and in y (one block per row), which
	puts both centroids on that centre -- the whole point of the arrangement.
	At `multipliers=2` this is the classic 2x2, one unit per corner and each
	capacitor on a diagonal. `multipliers` must be even for the halves to
	exist, which is checked by the caller.

	`side_by_side` is one column per capacitor, contiguous, no interleaving.
	"""
	if arrangement == "side_by_side":
		return ([("A", 0, i) for i in range(multipliers)]
		        + [("B", 1, i) for i in range(multipliers)])
	half = multipliers // 2
	slots = []
	for row in (0, 1):
		for col in range(multipliers):
			left = col < half
			slots.append(("A" if left == (row == 0) else "B", col, row))
	return slots


def diff_cap(
	pdk: MappedPDK,
	size: tuple[float, float] = (5.0, 5.0),
	multipliers: int = 2,
	arrangement: str = "common_centroid",
	pin_labels: bool = True,
) -> Component:
	"""Two matched MiM caps sharing a bottom plate.

	args:
	pdk = pdk to use
	size = (X, Y) of ONE unit cap's capmet plate; W is the X extent and L the Y
		one, the same convention `mimcap.py` reads a drawn plate back with
	multipliers = unit caps PER CAPACITOR (so 2*multipliers are drawn). Must be
		even for `common_centroid`
	arrangement = "common_centroid" (A B B A, one column) or "side_by_side"
		(two mirrored columns) -- see this module's docstring
	pin_labels = draw the CM/CA/CB pins (default True; off inside a parent)

	ports:
	CM_bo_S = shared bottom plate, leaving south
	CA_bo_W / CB_bo_E = the two top plates, leaving west and east
	"""
	if arrangement not in ARRANGEMENTS:
		raise ValueError(f"diff_cap: arrangement must be one of {ARRANGEMENTS}, "
		                 f"got {arrangement!r}")
	if multipliers < 1:
		raise ValueError(f"diff_cap: multipliers must be >= 1, got {multipliers}")
	if arrangement == "common_centroid" and multipliers % 2:
		raise ValueError(f"diff_cap: common_centroid splits each capacitor into two "
		                 f"halves, so multipliers must be even, got {multipliers}")

	capmettop, capmetbottom = _layerinfo(pdk)
	size = pdk.snap_to_2xgrid(size)
	unit = mimcap(pdk, size=size, edge_breakout=False, pin_labels=False)
	unit_w = float(unit.xmax - unit.xmin)
	unit_h = float(unit.ymax - unit.ymin)

	# Unit pitch. The rule is between CAPMET plates while this spacing is
	# between unit boxes, and a unit's box stands `min_enclosure` proud of its
	# own plate on every side -- so the two enclosures come off, exactly as in
	# `mimcap_array()`. Without that the plates sit further apart than the rule
	# asks for, and the pitch is spent on nothing.
	plate_gap = float(pdk.get_grule("capmet")["min_separation"])
	enclosure = float(pdk.get_grule(capmetbottom, "capmet")["min_enclosure"])
	box_gap = max(plate_gap - 2 * enclosure, 0.0)
	pitch_y = pdk.snap_to_2xgrid(unit_h + box_gap, return_type="float")
	pitch_x = pdk.snap_to_2xgrid(unit_w + box_gap, return_type="float")

	slots = _unit_slots(multipliers, arrangement)
	diffcap = Component()
	units = {"A": [], "B": []}                 # (x, y) centre of each cap's units
	if arrangement == "side_by_side":
		for owner, col, i in slots:
			x = (col - 0.5) * pitch_x
			y = (i - (multipliers - 1) / 2) * pitch_y
			(diffcap << unit).move((x, y))
			units[owner].append((x, y))
	else:
		for owner, col, row in slots:
			x = (col - (multipliers - 1) / 2) * pitch_x
			y = (row - 0.5) * pitch_y
			(diffcap << unit).move((x, y))
			units[owner].append((x, y))
	rows = units

	# CM: one solid plate under everything. Every unit's bottom plate is the
	# same net, so the plate that ties them is the plate itself -- and it is
	# also what lets each capacitor's own routing stay on ONE layer above.
	# The 0.4um pad past the array is `mimcap_array()`'s, for the same reason:
	# it swallows the per-unit plate edges into one region so klayout's
	# huge-metal rules do not fire on the seams.
	#
	# Measured from the units alone and drawn LAST, after the routing has told
	# it where to leave holes: taking the extent later would let the routing
	# inflate the plate, and the holes are not known until the descents are
	# placed.
	pad = 0.4
	x0 = float(diffcap.xmin) - pad
	y0 = float(diffcap.ymin) - pad
	x1 = float(diffcap.xmax) + pad
	y1 = float(diffcap.ymax) + pad

	# Terminal stubs. A landing point has to sit clear of the capmet keepout or
	# a router's own via straddles the cap -- `mimcap.py` documents the 40 real
	# violations that produced -- so each stub is the capmet separation plus
	# room for that via, measured from the metal it leaves.
	via_room = 1.0
	ext = pdk.snap_to_2xgrid(plate_gap + via_room, return_type="float")
	stub_w = pdk.snap_to_2xgrid(min(2.0, (y1 - y0) / 2), return_type="float")

	def stub(glayer, name, x_from, y_from, dx, dy, orientation, width):
		x_to, y_to = x_from + dx, y_from + dy
		xs, ys_ = sorted((x_from, x_to)), sorted((y_from, y_to))
		half = width / 2
		if dx:
			box = [(xs[0], y_from - half), (xs[1], y_from - half),
			       (xs[1], y_from + half), (xs[0], y_from + half)]
		else:
			box = [(x_from - half, ys_[0]), (x_from + half, ys_[0]),
			       (x_from + half, ys_[1]), (x_from - half, ys_[1])]
		diffcap.add_polygon(box, layer=pdk.get_glayer(glayer))
		diffcap.add_port(name=name, center=(x_to, y_to), width=width,
		                 orientation=orientation, layer=pdk.get_glayer(glayer))

	# CA and CB: the top plates, one net per capacitor. Each capacitor's units
	# are first merged block by block -- a block is the run of its units inside
	# one row, and one rectangle over that run bridges the column gaps.
	top_sep = float(pdk.get_grule(capmettop)["min_separation"])
	bus_w = pdk.snap_to_2xgrid(max(4 * float(pdk.get_grule(capmettop)["min_width"]), 0.6),
	                           return_type="float")
	half_plate_x, half_plate_y = float(size[0]) / 2, float(size[1]) / 2
	plate_extent = max(abs(cx) for owner in ("A", "B") for cx, _ in rows[owner]) + half_plate_x

	def merge_block(pts):
		"""One rectangle over a run of units sharing a row."""
		xs = [cx for cx, _ in pts]
		cy = pts[0][1]
		diffcap.add_polygon(
			[(min(xs) - half_plate_x, cy - half_plate_y),
			 (max(xs) + half_plate_x, cy - half_plate_y),
			 (max(xs) + half_plate_x, cy + half_plate_y),
			 (min(xs) - half_plate_x, cy + half_plate_y)],
			layer=pdk.get_glayer(capmettop))

	def bar(x_lo, x_hi, y_lo, y_hi, glayer=None):
		diffcap.add_polygon([(x_lo, y_lo), (x_hi, y_lo), (x_hi, y_hi), (x_lo, y_hi)],
		                    layer=pdk.get_glayer(glayer or capmettop))

	blocks = {owner: {} for owner in ("A", "B")}
	for owner in ("A", "B"):
		for cx, cy in rows[owner]:
			blocks[owner].setdefault(cy, []).append((cx, cy))
		for pts in blocks[owner].values():
			merge_block(pts)

	if arrangement == "side_by_side":
		# Each capacitor is one contiguous column, so a bus down its own
		# outward side reaches every block, with one rectangle per block.
		bus_x = {"A": -(plate_extent + top_sep + bus_w / 2),
		         "B": plate_extent + top_sep + bus_w / 2}
		for owner, side in (("A", -1.0), ("B", 1.0)):
			bx = bus_x[owner]
			ys = [cy for _, cy in rows[owner]]
			bar(bx - bus_w / 2, bx + bus_w / 2,
			    min(ys) - half_plate_y, max(ys) + half_plate_y)
			for cx, cy in rows[owner]:
				near, far = bx - side * bus_w / 2, cx + side * half_plate_x
				bar(min(near, far), max(near, far), cy - half_plate_y, cy + half_plate_y)
		terminals = {"CA": (bus_x["A"] - bus_w / 2, 0.0, min(stub_w, bus_w * 4)),
		             "CB": (bus_x["B"] + bus_w / 2, 0.0, min(stub_w, bus_w * 4))}
	else:
		gap_h = pitch_y - float(size[1])
		wire_w = pdk.snap_to_2xgrid(min(0.4, max(gap_h - 2 * top_sep, 0.0)),
		                            return_type="float")
		if wire_w < float(pdk.get_grule(capmettop)["min_width"]):
			raise ValueError("diff_cap: the gap between rows is too narrow to route "
			                 "CA through; widen the unit or the spacing")

		def span(pts):
			xs = [cx for cx, _ in pts]
			return min(xs), max(xs), pts[0][1]

		a_lo = span(blocks["A"][min(blocks["A"])])       # CA in the bottom row
		a_hi = span(blocks["A"][max(blocks["A"])])       # CA in the top row
		b_lo = span(blocks["B"][min(blocks["B"])])
		b_hi = span(blocks["B"][max(blocks["B"])])

		# CA takes the gap between the rows, on the plate metal: down into its
		# lower block, across, up into its upper block. CB drops through the
		# same gap, so the two are kept apart along it: CA enters each of its
		# blocks at the end NEAREST the centre and CB drops at the end FURTHEST
		# from it, both inset far enough to stay over their own plate.
		#
		# Insetting from the block EDGE, not from a unit's centre. With one
		# unit per block -- which is what multipliers=2 gives -- a block's
		# innermost and outermost unit are the same unit, so entering at unit
		# centres put CA's stub and CB's stack at the identical x in the same
		# gap. They overlapped, and the two capacitors came out as one net:
		# Magic reported `Ports "CA" and "CB" are electrically shorted` on a
		# layout DRC passes, since metal on metal breaks no spacing rule.
		def entry(block, toward_centre, inset):
			"""x to enter a block at, stepped IN from its inner or outer end.

			The step is toward the block's own middle -- stepping the other way
			leaves the plate entirely and the stub lands on nothing, which
			extracts as a floating cap (`X1 c1_120_258# CM ...` against a
			netlist expecting CA)."""
			lo, hi = block[0] - half_plate_x, block[1] + half_plate_x
			middle = (lo + hi) / 2
			near, far = (hi, lo) if middle < 0 else (lo, hi)
			edge = near if toward_centre else far
			return edge + inset if middle > edge else edge - inset

		a_x0 = entry(a_lo, True, wire_w)
		a_x1 = entry(a_hi, True, wire_w)
		# CB turns down into its upper block at that block's OUTER end -- the
		# furthest point from where CA runs through the gap.
		b_turn = entry(b_hi, False, wire_w)
		bar(a_x0 - wire_w / 2, a_x0 + wire_w / 2, a_lo[2] + half_plate_y - wire_w, wire_w / 2)
		bar(a_x1 - wire_w / 2, a_x1 + wire_w / 2, -wire_w / 2, a_hi[2] - half_plate_y + wire_w)
		bar(min(a_x0, a_x1) - wire_w / 2, max(a_x0, a_x1) + wire_w / 2,
		    -wire_w / 2, wire_w / 2)

		# CB goes around the outside, on the same plate metal -- east up the
		# side, then west along the top, into its upper block. No via, no hole
		# in the shared plate: the only two layers this cell uses are the two
		# the capacitors are already built from.
		#
		# It is the long way round, and that is the price of one routing layer:
		# the two capacitors' connections cross, and crossing nets cannot share
		# a layer. What matters is that the detour is THIN. This strip was four
		# times the minimum width, standing 1.5um clear of the plates on two
		# sides and adding more area than the units it was connecting; at the
		# same width as CA's own run it costs 0.6um a side. Dropping to a lower
		# metal instead removes even that, but pays for it with two via stacks,
		# two holes cut in the shared plate, and a wider gap between the rows to
		# fit them -- more machinery, and measurably more area, than the strip
		# it replaces.
		east_x = plate_extent + top_sep + wire_w / 2
		north_y = (max(abs(cy) for _, cy in rows["A"] + rows["B"]) + half_plate_y
		           + top_sep + wire_w / 2)
		bar(b_lo[1] + half_plate_x, east_x + wire_w / 2,
		    b_lo[2] - wire_w / 2, b_lo[2] + wire_w / 2)
		bar(east_x - wire_w / 2, east_x + wire_w / 2,
		    b_lo[2] - wire_w / 2, north_y + wire_w / 2)
		bar(b_turn - wire_w / 2, east_x + wire_w / 2,
		    north_y - wire_w / 2, north_y + wire_w / 2)
		bar(b_turn - wire_w / 2, b_turn + wire_w / 2,
		    b_hi[2] + half_plate_y - wire_w, north_y + wire_w / 2)

		# Both terminals leave straight off each capacitor's own bottom-row
		# block, on opposite sides -- mirror images of one another, and no
		# metal outside the plates other than the stubs themselves.
		terminals = {"CA": (a_lo[0] - half_plate_x, a_lo[2], wire_w),
		             "CB": (b_lo[1] + half_plate_x, b_lo[2], wire_w)}

	# The shared plate. Solid: nothing descends through it, so it needs no
	# holes cut for a via to pass.
	diffcap.add_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
	                    layer=pdk.get_glayer(capmetbottom))

	ca_x, ca_y, ca_w = terminals["CA"]
	cb_x, cb_y, cb_w = terminals["CB"]
	stub(capmettop, "CA_bo_W", ca_x, ca_y, -ext, 0.0, 180, ca_w)
	stub(capmettop, "CB_bo_E", cb_x, cb_y, ext, 0.0, 0, cb_w)
	stub(capmetbottom, "CM_bo_S", 0.0, y0, 0.0, -ext, 270, stub_w)

	if pin_labels:
		_add_pin(diffcap, pdk, capmettop, "CA_bo_W", "CA")
		_add_pin(diffcap, pdk, capmettop, "CB_bo_E", "CB")
		_add_pin(diffcap, pdk, capmetbottom, "CM_bo_S", "CM")

	# One instance of the unit cap per unit drawn, its top plate on that
	# capacitor's node and its bottom on the shared one -- `mimcap()`'s own
	# netlist calls those V1 and V2, in that order, which a Magic extraction of
	# this cell confirms (`X0 <capm net> <lower metal net> <model>`).
	netlist = Netlist(circuit_name="DIFF_CAP", nodes=["CM", "CA", "CB"])
	for owner in ("A", "B"):
		for _ in rows[owner]:
			netlist.connect_netlist(unit.info["netlist"],
			                        [("V1", f"C{owner}"), ("V2", "CM")])

	component = rename_ports_by_orientation(diffcap).flatten()
	component.info["netlist"] = netlist
	return component


if __name__ == "__main__":
	import argparse
	from pathlib import Path

	from glayout import sky130

	parser = argparse.ArgumentParser(
		description="Build a differential MiM cap pair -- two matched caps sharing a "
		            "bottom plate -- and write its GDS plus SPICE netlist next to it.")
	parser.add_argument("--width", type=float, default=5.0,
	                    help="unit cap width w (um), the capmet extent in X")
	parser.add_argument("--length", type=float, default=5.0,
	                    help="unit cap length l (um), the capmet extent in Y")
	parser.add_argument("--multipliers", type=int, default=2,
	                    help="unit caps PER capacitor (even for common_centroid)")
	parser.add_argument("--arrangement", choices=ARRANGEMENTS, default="common_centroid")
	parser.add_argument("--no-pins", action="store_true",
	                    help="skip the CM/CA/CB pins")
	parser.add_argument("--out", default="diff_cap.gds", help="output .gds path")
	parser.add_argument("--top", default="DIFF_CAP", help="top cell name")
	args = parser.parse_args()

	cap = diff_cap(sky130, size=(args.width, args.length),
	               multipliers=args.multipliers, arrangement=args.arrangement,
	               pin_labels=not args.no_pins)
	cap.name = args.top

	out = Path(args.out)
	cap.write_gds(str(out))
	spice_path = out.with_suffix(".spice")
	with open(spice_path, "w") as f:
		f.write(cap.info["netlist"].generate_netlist())

	print(f"\n{cap.name}: w={args.width} l={args.length} m={args.multipliers} per cap, "
	      f"{args.arrangement}")
	print(f"  size    {cap.xmax - cap.xmin:.3f} x {cap.ymax - cap.ymin:.3f} um")
	print(f"  {out}\n  {spice_path}")
	for name in ("CM_bo_S", "CA_bo_W", "CB_bo_E"):
		if name in cap.ports:
			port = cap.ports[name]
			print(f"    {name:10s} ({float(port.center[0]):8.3f}, "
			      f"{float(port.center[1]):8.3f})  w={float(port.width):.3f}")
