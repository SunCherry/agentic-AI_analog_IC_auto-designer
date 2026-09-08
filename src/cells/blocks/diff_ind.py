#!/usr/bin/env python3
"""Differential inductor pair: two matched spiral inductors placed as one cell.

Built from the unit coil in `primitives/inductor.py` -- same winding, same
process rules, drawn once and placed twice. What this module adds is the part
that only exists once there are two of them: how they sit relative to each
other, whether their outer ends are tied into a centre tap, and what their
mutual coupling does to the differential inductance.

Two arrangements, and the choice is electrical, not cosmetic
------------------------------------------------------------
    mirror                       parallel
    (reflected about x=0)        (pure translation)

      /```\    /```\               /```\    /```\
     |  P  |  |  N  |             |  P  |  |  N  |
      \___/    \___/               \___/    \___/
     ccw       cw                 ccw       ccw

`mirror` reflects the second coil, so the pair is symmetric about the axis
between them -- the usual choice when the pair has to line up with a mirrored
differential circuit. But reflecting a spiral reverses its handedness, and
that reverses the sign of the coupling: under differential drive the two coils'
magnetic moments end up PARALLEL, they oppose, and the differential inductance
comes out as 2*(L - |M|).

`parallel` keeps both coils wound the same way. Under differential drive their
moments are antiparallel, the coupling AIDS, and the differential inductance is
2*(L + |M|) -- more inductance from the same two coils and the same area. The
two coils are still identical devices, just matched by translation rather than
by reflection.

Neither is universally right, so this module does not assert one: it computes
M for the placement it actually drew (Neumann integration over the two
conduction paths) and reports both the coupling coefficient and the resulting
differential inductance. Pick the arrangement from the numbers.

The centre tap
--------------
`tie="center_tap"` joins the two OUTER terminals with a bar on the coil layer,
giving the three-terminal device a differential LC tank actually wants: AP and
AN to the two drains, CT to the supply. It is a bar between two lead tips, so
it only exists when those tips face somewhere it can go -- which depends on
which quarter-turn the winding ends on. When it does not fit, the geometric
check below says so with the distance it was short by, rather than drawing a
shorted turn.

Why the clearance check is not left to DRC
------------------------------------------
For the same reason `inductor.py` checks its own turns: Magic cannot see this
class of error. If the tie bar drifts into the A lead, or the two coils merge,
the metal simply joins -- no spacing rule is broken, DRC reports a clean
layout, and what you have is a shorted turn with a fraction of the intended
inductance. So every pair of conductors that must stay apart is measured here,
on the centreline, before any polygon is emitted.

Usage
-----
    from diff_ind import diff_ind
    pair = diff_ind(n_turns=2.0, inner_diameter=40.0, separation=30.0)
    pair.write_gds("diff_ind.gds")
    print(pair.report())

CLI:
    python diff_ind.py --turns 2 --inner 40 --sep 30 --arrangement mirror \
        --tie center_tap --out diff_ind.gds --drc
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
	import numpy as np
except ImportError:  # pragma: no cover
	raise SystemExit("numpy is required: pip install numpy")

try:
	import gdstk
except ImportError:  # pragma: no cover
	raise SystemExit("gdstk is required: pip install gdstk")

# `inductor.py` lives in the sibling primitives/ directory and is reached by
# path, the same way `diff_cap.py` picks up `mimcap.py`. `_seg_seg_dist` comes
# with it deliberately: the pair's clearances are measured with exactly the
# routine the single coil already uses on its own turns.
_PRIMITIVES = Path(__file__).resolve().parent.parent / "primitives"
if str(_PRIMITIVES) not in sys.path:
	sys.path.insert(0, str(_PRIMITIVES))
from inductor import (  # noqa: E402
	MU0, OCT_K, Inductor, spiral_inductor, run_drc, _seg_seg_dist)

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / ".claude" / "reference"))
from pdk_config import pdk, PDKConfigError  # noqa: E402


ARRANGEMENTS = ("mirror", "parallel")
TIES = ("none", "center_tap")


# --------------------------------------------------------------------------
# placement
# --------------------------------------------------------------------------

def _place(pts, mirror: bool, dx: float, dy: float):
	"""Apply the pair's placement transform to a list of (x, y) in um.

	`mirror` reflects about x=0, which is what GDS expresses as a reflection
	about the x-axis followed by a 180-degree rotation -- the form used for the
	gdstk Reference below, so the polygons and these coordinates stay in step.
	"""
	return [((-x if mirror else x) + dx, y + dy) for x, y in pts]


def _segments(pts):
	return list(zip(pts, pts[1:]))


def _clearance(segs_a, segs_b, skip_a=(), skip_b=()):
	"""Closest approach between two centrelines, ignoring named indices.

	Returns (distance_um, index_a, index_b). Indices in `skip_a`/`skip_b` are
	segments that legitimately touch the other conductor -- a lead the tie bar
	lands on, say -- and are not spacing pairs.
	"""
	worst = (float('inf'), -1, -1)
	for i, sa in enumerate(segs_a):
		if i in skip_a:
			continue
		for j, sb in enumerate(segs_b):
			if j in skip_b:
				continue
			d = _seg_seg_dist(*sa, *sb)
			if d < worst[0]:
				worst = (d, i, j)
	return worst


# --------------------------------------------------------------------------
# mutual inductance
# --------------------------------------------------------------------------

def _filaments(path, ds: float):
	"""Chop a polyline into pieces of at most `ds`; return midpoints + vectors."""
	mids, vecs = [], []
	for (x0, y0), (x1, y1) in zip(path, path[1:]):
		length = math.hypot(x1 - x0, y1 - y0)
		if length == 0.0:
			continue
		n = max(1, int(math.ceil(length / ds)))
		for k in range(n):
			t0, t1 = k / n, (k + 1) / n
			ax, ay = x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0
			bx, by = x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1
			mids.append(((ax + bx) / 2.0, (ay + by) / 2.0))
			vecs.append((bx - ax, by - ay))
	return np.asarray(mids), np.asarray(vecs)


def mutual_inductance(path_a, path_b, ds: float = 0.5,
                      dmin: float = 0.0) -> float:
	"""Neumann double integral between two closed filaments, in henries.

	    M = (mu0/4pi) * int int (dl1 . dl2) / r

	Coordinates are um; both paths are treated as coplanar. That is exact
	enough here -- the only out-of-plane part of a coil is its underpass, about
	a micron below, against coil-to-coil distances measured in tens of microns.
	`dmin` floors the separation, which is how a path is integrated against
	ITSELF: the floor stands in for the conductor's geometric mean distance,
	since two coincident filaments have none.

	Checked against Maxwell's closed form for concentric coplanar loops (the
	one lateral case with an analytic answer) at five significant figures.

	Both paths must be CLOSED. The Neumann integral over open paths is a
	partial inductance -- it depends on return paths that are not in the
	integral -- and for this cell that is not a technicality: run it over the
	two coils' full conduction paths and the answer is dominated by their long
	parallel lead-in runs, which sit the same way round in both arrangements.
	It then reports the same sign for mirrored and unmirrored placements, which
	is wrong, and wrong in the direction that hides the entire effect this
	module exists to measure.
	"""
	m_a, v_a = _filaments(path_a, ds)
	m_b, v_b = _filaments(path_b, ds)
	dot = v_a @ v_b.T
	dist = np.sqrt(((m_a[:, None, :] - m_b[None, :, :]) ** 2).sum(-1))
	if dmin > 0.0:
		dist = np.maximum(dist, dmin)
	# um^2 / um = um of length, so scale to metres for mu0's H/m
	return float(MU0 / (4 * math.pi) * np.sum(dot / dist) * 1e-6)


def _turn_loops(unit: Inductor, centre_y: float):
	"""The coil as concentric closed turns: (weight, points) per turn.

	This is the same physical picture the inductance already comes from -- the
	current-sheet expressions in `inductor.py` model a spiral as concentric
	current loops -- so M and L are computed from one model rather than two
	that cannot be divided into each other. The fiction is the radial jog
	between turns, which this drops; the alternative fiction, closing the whole
	spiral with a chord, is no more real and is not what L was derived from.

	Turn i sits at centreline half-size d_in/2 + w/2 + i*pitch, wound
	counterclockwise to match the generator's E-N-W-S direction cycle. A
	fractional turn enters weighted by its fraction, which averages over where
	on the lap it actually falls.

	Cross-checked at run time: `report()` prints the self-inductance this model
	gives beside the current-sheet value, and they agree to a few percent.
	"""
	w, s = unit.width_um, unit.space_um
	pitch = w + s
	loops = []
	for i in range(math.ceil(unit.n_turns)):
		r = unit.d_in_um / 2.0 + w / 2.0 + i * pitch
		weight = min(1.0, unit.n_turns - i)
		if unit.shape == "square":
			v = [(r, r), (-r, r), (-r, -r), (r, -r)]
		else:
			c = OCT_K * r
			v = [(r, r - c), (r - c, r), (-(r - c), r), (-r, r - c),
			     (-r, -(r - c)), (-(r - c), -r), (r - c, -r), (r, -(r - c))]
		v = [(x, y + centre_y) for x, y in v]
		loops.append((weight, v + [v[0]]))
	return loops


def _couple(loops_a, loops_b, place_a, place_b, ds=0.2, dmin=0.0) -> float:
	"""Turn-by-turn sum of mutual inductances between two placed coils, in H."""
	total = 0.0
	for wi, vi in loops_a:
		a = place_a(vi)
		for wj, vj in loops_b:
			total += wi * wj * mutual_inductance(a, place_b(vj), ds=ds, dmin=dmin)
	return total


# --------------------------------------------------------------------------
# result
# --------------------------------------------------------------------------

@dataclass
class DifferentialInductor:
	"""A placed pair: its cell, the unit coil it was built from, its coupling."""
	cell: "gdstk.Cell"
	library: "gdstk.Library"
	name: str
	unit: Inductor
	arrangement: str
	tie: str
	separation_um: float
	pitch_um: float
	bbox_um: tuple
	m_nH: float
	k: float
	l_loop_nH: float
	coil_clearance_um: float
	tie_clearance_um: float
	terminals: dict
	warnings: list = field(default_factory=list)

	@property
	def l_single_nH(self) -> float:
		return self.unit.l_currentsheet_nH

	@property
	def l_diff_nH(self) -> float:
		"""Inductance seen across AP-AN, both coils in series through CT.

		Differential drive puts +I into coil P at its A terminal and takes the
		same current out of coil N at its A terminal, so relative to the
		reference direction M was computed in, I_N = -I_P:

		    v = 2*(L - M) * di/dt

		M signed, so a negative M (same-handed coils) ADDS.
		"""
		return 2.0 * (self.l_single_nH - self.m_nH)

	@property
	def l_comm_nH(self) -> float:
		"""Inductance of one coil when both are driven in common mode."""
		return self.l_single_nH + self.m_nH

	def q_diff_at(self, freq_hz: float) -> float:
		"""Differential Q from DC resistance only -- an optimistic ceiling."""
		return (2 * math.pi * freq_hz * self.l_diff_nH * 1e-9
		        / (2 * self.unit.r_dc_ohm))

	def spice_subckt(self) -> str:
		"""A coupled two-coil model of the pair, for pre-layout use.

		Series L-R per coil plus the K statement, with L from the current-sheet
		expression and M from the Neumann integral above. No substrate network
		and no frequency dependence, so it is a low-frequency model in exactly
		the way the single coil's is.

		K carries a magnitude only: ngspice documents the coupling coefficient
		as greater than zero. A negative coupling is expressed the way it
		physically arises instead -- by reversing one coil's reference
		direction, i.e. writing LN's nodes the other way round. Same topology,
		opposite dot.
		"""
		ports = " ".join(self.terminals)
		bp = "CT" if self.tie == "center_tap" else "BP"
		bn = "CT" if self.tie == "center_tap" else "BN"
		ln = (f"LN AN nN {self.l_single_nH:.4f}n" if self.m_nH >= 0
		      else f"LN nN AN {self.l_single_nH:.4f}n")
		note = ("" if self.m_nH >= 0 else
		        "* LN's nodes are reversed: the coupling is negative, and K "
		        "carries magnitude only.\n")
		return (f".subckt {self.name} {ports}\n"
		        f"{note}"
		        f"LP AP nP {self.l_single_nH:.4f}n\n"
		        f"RP nP {bp} {self.unit.r_dc_ohm:.4f}\n"
		        f"{ln}\n"
		        f"RN nN {bn} {self.unit.r_dc_ohm:.4f}\n"
		        f"KPN LP LN {abs(self.k):.6f}\n"
		        f".ends {self.name}\n")

	def netgen_blackbox(self) -> str:
		"""netgen setup lines that let this pair pass LVS.

		Same reason as the single coil: Magic has no `device` line for an
		inductor, so each spiral extracts as plain wire and its two terminals
		land on one net. Here that is worse, not better -- with a centre tap
		BOTH coils extract onto a single net, so the layout cannot present the
		two-inductor topology any schematic will have.
		"""
		return (f"# {self.name}: spirals extract as wire, not devices.\n"
		        f'ignore class "-circuit1 {self.name}"\n'
		        f'ignore class "-circuit2 {self.name}"\n')

	def write_gds(self, path: str) -> str:
		self.library.write_gds(path)
		return path

	def report(self, freq_ghz: float = 2.4) -> str:
		w, h = self.bbox_um
		u = self.unit
		lines = [
			f"{self.name}: two {u.shape} spirals, {u.n_turns:g} turns each, "
			f"{self.arrangement}",
			f"  trace           : w={u.width_um:g}um  s={u.space_um:g}um",
			f"  per coil        : d_in={u.d_in_um:.2f}um  d_out={u.d_out_um:.2f}um  "
			f"{u.coil_len_um:.1f}um of conductor",
			f"  placement       : {self.separation_um:.2f}um edge gap, "
			f"{self.pitch_um:.2f}um centre pitch, tie={self.tie}",
			f"  footprint       : {w:.2f} x {h:.2f} um",
			f"  terminals       : " + "  ".join(
				f"{n}({x:.2f},{y:.2f})" for n, (x, y) in self.terminals.items()),
			"",
			f"  L per coil       : {self.l_single_nH:.3f} nH   "
			f"R per coil: {u.r_dc_ohm:.3f} ohm",
			f"                     (loop-model cross-check: "
			f"{self.l_loop_nH:.3f} nH, {100 * (self.l_loop_nH / self.l_single_nH - 1):+.1f}%)",
			f"  M                : {self.m_nH:+.4f} nH   k = {self.k:+.4f}",
			f"  L differential   : {self.l_diff_nH:.3f} nH  (AP-AN, = 2*(L-M); "
			f"coupling {'opposes' if self.m_nH > 0 else 'aids'})",
			f"  L common-mode    : {self.l_comm_nH:.3f} nH per coil",
			f"  Q_diff @ {freq_ghz:g}GHz  : {self.q_diff_at(freq_ghz * 1e9):.1f} "
			f"(DC-R only, no substrate loss -- an upper bound)",
			f"  coil-to-coil     : {self.coil_clearance_um:.2f} um centreline "
			f"(need {u.width_um + u.space_um:.2f})",
		]
		if self.tie == "center_tap":
			lines.append(f"  tie-to-winding   : {self.tie_clearance_um:.2f} um "
			             f"centreline (need {u.width_um + u.space_um:.2f})")
		for wmsg in list(u.warnings) + list(self.warnings):
			lines.append(f"  WARNING: {wmsg}")
		return "\n".join(lines)


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------

def diff_ind(n_turns: float = 2.0,
             inner_diameter: float = 40.0,
             width: float | None = None,
             space: float | None = None,
             shape: str = "octagon",
             separation: float | None = None,
             arrangement: str = "mirror",
             tie: str = "none",
             lead_length: float = 5.0,
             lead_side: str = "north",
             n_via_rows: int = 4,
             pin_labels: bool = True,
             cell_name: str = "diff_ind",
             pdk_cfg=None) -> DifferentialInductor:
	"""Place two identical spiral inductors as one differential block.

	n_turns .. n_via_rows  passed straight through to `spiral_inductor`; both
	                       coils are the same call, so they are identical by
	                       construction rather than by two sets of arguments
	                       that have to be kept in step
	separation             clear gap between the two coils' bounding boxes, um.
	                       Defaults to half the outer diameter, which puts k in
	                       the low hundredths -- close enough to matter, far
	                       enough not to dominate. Coupling is what this
	                       parameter buys or spends, so `report()` prints it.
	arrangement            "mirror" (reflected, axis-symmetric, coupling
	                       opposes) or "parallel" (translated, same handedness,
	                       coupling aids)
	tie                    "none" for four terminals AP/BP/AN/BN, or
	                       "center_tap" to join the outer pair into CT
	pin_labels             draw the terminal pins and labels

	Terminal P is the left coil, N the right. A is each coil's inner terminal
	(the one that comes out on the underpass), B its outer.
	"""
	if arrangement not in ARRANGEMENTS:
		raise ValueError(f"arrangement must be one of {ARRANGEMENTS}, not {arrangement!r}")
	if tie not in TIES:
		raise ValueError(f"tie must be one of {TIES}, not {tie!r}")

	p = pdk_cfg or pdk()
	ind_cfg = p.require_inductor("drawing a differential inductor pair")
	grid = ind_cfg['grid_nm'] / 1000.0

	def snap(v):
		return round(v / grid) * grid

	# One coil, drawn once. Its own pins are suppressed: this cell places it
	# twice, and two ports both called "A" is not something `port makeall` can
	# resolve -- the pair labels its own four terminals below.
	unit = spiral_inductor(
		n_turns=n_turns, inner_diameter=inner_diameter, width=width,
		space=space, shape=shape, lead_length=lead_length, lead_side=lead_side,
		n_via_rows=n_via_rows, pin_labels=False,
		cell_name=f"{cell_name}_coil", pdk_cfg=p)

	W, S = unit.width_um, unit.space_um
	need = W + S
	half_w = W / 2.0
	sep = 0.5 * unit.d_out_um if separation is None else float(separation)
	if sep < S:
		raise ValueError(
			f"separation {sep}um is below the coil layer's {S:g}um spacing "
			f"minimum; the two coils would be a DRC violation apart")
	sep = snap(sep)

	(bx0, by0), (bx1, by1) = unit.cell.bounding_box()
	mirror = (arrangement == "mirror")
	dy = snap(-(by0 + by1) / 2.0)
	# Left coil's right edge at -sep/2, right coil's left edge at +sep/2, so
	# the pair is centred on x=0 whichever way the second one is placed.
	dx_p = snap(-sep / 2.0 - bx1)
	dx_n = snap(sep / 2.0 + bx1) if mirror else snap(sep / 2.0 - bx0)
	bxc = (bx0 + bx1) / 2.0
	pitch = abs((-bxc if mirror else bxc) + dx_n - (bxc + dx_p))

	path_p = _place(unit.path_um, False, dx_p, dy)
	path_n = _place(unit.path_um, mirror, dx_n, dy)
	segs_p, segs_n = _segments(path_p), _segments(path_n)

	# The two coils are separate nets, so metal that merges between them is a
	# dead short -- and a short DRC cannot report, because merged metal breaks
	# no spacing rule. Measured here instead.
	coil_clear, ip, jn = _clearance(segs_p, segs_n)
	if coil_clear < need - grid:
		raise ValueError(
			f"the two coils would short: segment {ip} of coil P and segment "
			f"{jn} of coil N come within {coil_clear:.2f}um, but need "
			f"{need:.2f}um (width + space). Magic would NOT flag a merge -- "
			f"the metal simply joins. Increase separation.")

	warnings: list[str] = []
	tie_clear = float('inf')
	tie_rects: list[tuple] = []

	# --- terminals -------------------------------------------------------
	a_p, b_p = path_p[0], path_p[-1]
	a_n, b_n = path_n[0], path_n[-1]

	if tie == "center_tap":
		if abs(b_p[1] - b_n[1]) > grid:
			raise ValueError(
				"the two outer leads do not end at the same height, so no "
				"straight tie bar joins them")
		bar_y = b_p[1]
		# Run the bar half a trace width PAST each tip. Stopping on the tip
		# leaves the lead poking out beyond the bar's end face by exactly that
		# much whenever the two are perpendicular -- a half-width sliver of
		# metal, and a met5.1 width violation at each end. Overshooting lands
		# the end face flush with the lead's outer edge instead, and costs
		# nothing when the two are collinear, where it just runs back up the
		# lead it is already joined to.
		x_lo, x_hi = sorted((b_p[0], b_n[0]))
		x_lo, x_hi = x_lo - half_w, x_hi + half_w
		tie_rects.append((x_lo, bar_y - half_w, x_hi, bar_y + half_w))
		bar_seg = [((x_lo, bar_y), (x_hi, bar_y))]

		# The bar joins the two outer leads, so those two segments are
		# contiguous conductor with it; everything else it passes has to stay
		# a full width+space away or it shorts a turn out.
		last_p, last_n = len(segs_p) - 1, len(segs_n) - 1
		d_p = _clearance(bar_seg, segs_p, skip_b={last_p})
		d_n = _clearance(bar_seg, segs_n, skip_b={last_n})
		tie_clear = min(d_p[0], d_n[0])
		if tie_clear < need - grid:
			which, idx = ("P", d_p[2]) if d_p[0] <= d_n[0] else ("N", d_n[2])
			raise ValueError(
				f"the centre-tap bar would short a turn: it passes "
				f"{tie_clear:.2f}um from segment {idx} of coil {which}, but "
				f"needs {need:.2f}um. Magic would NOT flag it -- the bar is "
				f"already on that coil's net, so the merged metal is legal "
				f"geometry and simply bypasses part of the winding. The outer "
				f"lead has to end somewhere a straight bar can reach: try a "
				f"different quarter-turn for n_turns, put the inner lead on "
				f"the other side (lead_side), or lengthen lead_length.")

		# A tap buried between the coils is hard to reach, so run a stub out
		# the far side of the bar when there is room for one. Optional by
		# construction: it is checked the same way and dropped if it does not
		# fit, rather than drawn on faith.
		if abs(bar_y) > grid:
			out = math.copysign(1.0, bar_y)
			tip = (0.0, snap(bar_y + out * lead_length))
			stub_seg = [((0.0, bar_y), tip)]
			s_clear = min(_clearance(stub_seg, segs_p)[0],
			              _clearance(stub_seg, segs_n)[0])
			if s_clear >= need - grid:
				tie_rects.append((-half_w, min(bar_y, tip[1]),
				                  half_w, max(bar_y, tip[1])))
				ct_pt = tip
			else:
				ct_pt = (0.0, bar_y)
				warnings.append(
					f"no room for a centre-tap stub ({s_clear:.2f}um clear, "
					f"needs {need:.2f}um); CT is pinned on the tie bar itself")
		else:
			ct_pt = (0.0, bar_y)
			warnings.append(
				"the tie bar runs in the gap between the coils; CT is pinned "
				"on the bar, with no stub out of the array")

		terminals = {"AP": a_p, "AN": a_n, "CT": ct_pt}
	else:
		terminals = {"AP": a_p, "BP": b_p, "AN": a_n, "BN": b_n}

	# --- emit ------------------------------------------------------------
	lib = gdstk.Library(name=cell_name, unit=1e-6, precision=1e-9)
	top = lib.new_cell(cell_name)
	L_COIL = tuple(ind_cfg['coil_layer'])
	L_PIN = tuple(ind_cfg['coil_pin_layer'])

	# x_reflection then a 180-degree rotation is (x, y) -> (-x, y): the mirror
	# about the vertical axis, expressed the only way GDS has of saying it.
	top.add(gdstk.Reference(unit.cell, origin=(dx_p, dy)))
	top.add(gdstk.Reference(unit.cell, origin=(dx_n, dy),
	                        rotation=math.pi if mirror else 0.0,
	                        x_reflection=mirror))
	# Flattened, so the pair is one cell with two disjoint nets rather than two
	# instances of a cell whose ports would collide on extraction.
	top.flatten()

	for x0, y0, x1, y1 in tie_rects:
		top.add(gdstk.rectangle((x0, y0), (x1, y1),
		                        layer=L_COIL[0], datatype=L_COIL[1]))

	if pin_labels:
		for nm, (x, y) in terminals.items():
			top.add(gdstk.rectangle((x - half_w, y - half_w),
			                        (x + half_w, y + half_w),
			                        layer=L_PIN[0], datatype=L_PIN[1]))
			top.add(gdstk.Label(nm, (x, y), layer=L_PIN[0], texttype=L_PIN[1]))

	# --- coupling --------------------------------------------------------
	# Measured on closed turns, not on the conduction paths above: see
	# `mutual_inductance`. The winding's centre is not the origin -- the
	# generator pulls the inner end back to the middle of the innermost side
	# and only re-centres in x -- so it is taken from the winding itself, with
	# the outer lead trimmed back off the end so the lead cannot drag it.
	wind = list(unit.path_um[2:])
	ex, ey = wind[-1][0] - wind[-2][0], wind[-1][1] - wind[-2][1]
	elen = math.hypot(ex, ey)
	wind[-1] = (wind[-1][0] - ex / elen * lead_length,
	            wind[-1][1] - ey / elen * lead_length)
	ys = [y for _, y in wind]
	loops = _turn_loops(unit, (min(ys) + max(ys)) / 2.0)

	m_nh = _couple(loops, loops,
	               lambda v: _place(v, False, dx_p, dy),
	               lambda v: _place(v, mirror, dx_n, dy)) * 1e9
	# The same model turned on one coil alone, as a check that it is worth
	# believing: the geometric mean distance of the conductor cross-section
	# stands in for the separation of a filament from itself.
	gmd = 0.2235 * (W + ind_cfg['physical']['coil_thickness_um'])
	l_loop_nh = _couple(loops, loops,
	                    lambda v: _place(v, False, 0.0, 0.0),
	                    lambda v: _place(v, False, 0.0, 0.0),
	                    dmin=gmd) * 1e9
	k = m_nh / unit.l_currentsheet_nH
	if abs(k) >= 1.0:
		raise ValueError(
			f"the two coils compute to |k| = {abs(k):.3f}, which is not "
			f"physical; the coupling model has been pushed outside its range")
	if l_loop_nh > 0 and abs(l_loop_nh - unit.l_currentsheet_nH) \
			> 0.25 * unit.l_currentsheet_nH:
		warnings.append(
			f"the loop model puts one coil at {l_loop_nh:.3f}nH against the "
			f"current-sheet {unit.l_currentsheet_nH:.3f}nH, more than 25% "
			f"apart; treat M and k as indicative only")

	bb = top.bounding_box()
	bbox = (bb[1][0] - bb[0][0], bb[1][1] - bb[0][1])

	return DifferentialInductor(
		cell=top, library=lib, name=cell_name, unit=unit,
		arrangement=arrangement, tie=tie, separation_um=sep, pitch_um=pitch,
		bbox_um=bbox, m_nH=m_nh, k=k, l_loop_nH=l_loop_nh,
		coil_clearance_um=coil_clear,
		tie_clearance_um=tie_clear, terminals=terminals, warnings=warnings)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None):
	ap = argparse.ArgumentParser(
		description="Place two matched spiral inductors as one differential "
		            "block on the active PDK's top thick metal.")
	ap.add_argument('--turns', type=float, default=2.0)
	ap.add_argument('--inner', type=float, default=40.0,
	                help="inner diameter of each coil (clear opening), um")
	ap.add_argument('--width', type=float, default=None,
	                help="trace width, um (default: process minimum)")
	ap.add_argument('--space', type=float, default=None,
	                help="turn-to-turn space, um (default: process minimum)")
	ap.add_argument('--shape', choices=('octagon', 'square'), default='octagon')
	ap.add_argument('--sep', type=float, default=None,
	                help="clear gap between the coils, um (default: d_out/2)")
	ap.add_argument('--arrangement', choices=ARRANGEMENTS, default='mirror')
	ap.add_argument('--tie', choices=TIES, default='none')
	ap.add_argument('--lead', type=float, default=5.0, help="lead length, um")
	ap.add_argument('--lead-side', choices=('north', 'south'), default='north')
	ap.add_argument('--via-rows', type=int, default=4)
	ap.add_argument('--no-pins', action='store_true',
	                help="skip the terminal pins and labels")
	ap.add_argument('--name', default='diff_ind')
	ap.add_argument('--freq', type=float, default=2.4, help="report Q at GHz")
	ap.add_argument('--out', default='diff_ind.gds')
	ap.add_argument('--spice', default=None,
	                help="also write the coupled SPICE model here")
	ap.add_argument('--drc', action='store_true', help="run Magic DRC on the result")
	a = ap.parse_args(argv)

	try:
		pair = diff_ind(
			n_turns=a.turns, inner_diameter=a.inner, width=a.width,
			space=a.space, shape=a.shape, separation=a.sep,
			arrangement=a.arrangement, tie=a.tie, lead_length=a.lead,
			lead_side=a.lead_side, n_via_rows=a.via_rows,
			pin_labels=not a.no_pins, cell_name=a.name)
	except (ValueError, PDKConfigError) as e:
		raise SystemExit(f"error: {e}")

	pair.write_gds(a.out)
	print(pair.report(a.freq))
	print(f"  wrote            : {a.out}")

	spice_path = a.spice or str(Path(a.out).with_suffix('.spice'))
	with open(spice_path, 'w') as f:
		f.write(pair.spice_subckt())
		f.write("\n* LVS: Magic extracts a spiral as wire, so black-box both sides.\n")
		f.write("".join(f"* {ln}\n" for ln in
		                pair.netgen_blackbox().splitlines() if ln))
	print(f"  wrote            : {spice_path}")

	if a.drc:
		total, rules = run_drc(a.out, a.name)
		print(f"  Magic DRC        : {total} violation(s)")
		for r in rules:
			print(f"    {r}")
		return 1 if total else 0
	return 0


if __name__ == '__main__':
	sys.exit(main())
