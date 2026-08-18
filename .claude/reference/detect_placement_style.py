#!/usr/bin/env python3
"""Detect which special placement convention (if any) an existing layout
used for each structurally-matched device pair, so a redraw can preserve
it instead of defaulting to a generic placement (see ../SKILL.md's
"Detecting Existing Placement Conventions").

Reads <design_dir>/physical_map.json (written by extract_physical_info.py)
-- specifically each device's 'kind', 'nets' (drain/gate/source/bulk
order for MOS, per extract_physical_info.py's _MOS_TERMS), and its
placement bbox (x0_um/y0_um/x1_um/y1_um).

For every structurally-matched pair found (same signatures
../../sub-layout-handler/script/detect_topology.py uses -- diff pair:
same-type devices sharing a source net with distinct gates/drains;
current mirror: one diode-connected device sharing gate+source with a
second device with a distinct drain), classifies the GEOMETRIC placement
style from the two devices' recorded bounding boxes:

  side-by-side     bboxes barely overlap -- no special matching
                    structure (e.g. a plain two_transistor_place() call,
                    or manual placement).
  interdigitized    bboxes overlap substantially AND the combined region
                    is about ONE device-row tall -- consistent with
                    src/glayout/placement/two_transistor_interdigitized.py's
                    ABAB... row (what current_mirror() typically uses).
  common-centroid   bboxes overlap substantially AND the combined region
                    is about TWO device-rows tall (a top row + bottom
                    row quadrant split, confirmed by reading
                    src/glayout/placement/common_centroid_ab_ba.py's own
                    placement math: device A gets the top-left +
                    bottom-right quadrants, device B gets top-right +
                    bottom-left) -- consistent with that module, which is
                    what diff_pair() calls internally for its two input
                    devices.
  unrecognized      doesn't cleanly fit any of the above -- flagged for
                    manual inspection rather than forcing a guess.

This is a coarse, bbox-level heuristic (see ../SKILL.md's caveats) -- it
cannot see actual finger-level geometry, only each netlist instance's
overall bounding box, so treat the classification as a strong hint for
layout-agent to verify, not a certainty. The overlap/height-ratio
thresholds are starting points, not calibrated against a labeled
dataset -- override via --overlap-threshold/--row-tolerance if they
misclassify a real design.

Usage:
  python detect_placement_style.py <design_dir>

  e.g. python detect_placement_style.py <design_dir>

Writes <design_dir>/placement_style_report.json.
"""
import argparse
import json
import os
import sys

DEFAULT_OVERLAP_THRESHOLD = 0.5   # >=50% bbox overlap -> "interleaved", not side-by-side
DEFAULT_ROW_TOLERANCE = 1.35      # combined-height / single-height ratio cutoff between
                                   # "one row" (interdigitized) and "two rows" (common-centroid)

SUPPLY_LIKE_NAMES = {"vdd", "vss", "gnd", "vcc", "vee", "avdd", "avss", "dvdd", "dvss"}

GLAYOUT_REFS = {
    "interdigitized": "src/glayout/placement/two_transistor_interdigitized.py "
                       "(two_nfet_interdigitized/two_pfet_interdigitized)",
    "common-centroid": "src/glayout/placement/common_centroid_ab_ba.py "
                        "(common_centroid_ab_ba)",
    "side-by-side": "src/glayout/placement/two_transistor_place.py "
                     "(two_transistor_place), or a plain manual placement",
    "unrecognized": None,
}


def load_devices(design_dir):
    map_path = os.path.join(design_dir, "physical_map.json")
    if not os.path.isfile(map_path):
        raise FileNotFoundError(
            f"no physical_map.json in {design_dir} -- run "
            f"extract_physical_info.py first (see ../../../.claude/agents/layout-agent.md)"
        )
    data = json.load(open(map_path))
    return [d for d in data["devices"]
            if all(k in d for k in ("x0_um", "y0_um", "x1_um", "y1_um"))
            and d.get("kind") in ("nfet", "pfet")]


def find_diff_pairs(devices):
    """Same-type devices sharing a source net (nets[2]), distinct gate
    (nets[1]) and drain (nets[0]) -- same signature
    ../../sub-layout-handler/script/detect_topology.py's is_diff_pair()
    uses -- plus two cheap false-positive filters this bbox-only version
    needs that the real netlist-level detect_topology.py doesn't (it has
    other structural context to lean on): reject a candidate device that
    is itself diode-connected (drain==gate -- that's the current-mirror
    signature, not a diff-pair input), and reject a pair whose "shared
    source" is actually a supply rail (VDD/VSS/etc.) -- any two same-type
    devices tied to the raw supply will spuriously match a naive
    shared-source check, but that's not a real differential pair (whose
    tail is a bias/current-source node, not the rail itself)."""
    pairs = []
    for i, a in enumerate(devices):
        an = a["nets"]
        if len(an) < 3 or an[0] == an[1]:
            continue  # too short, or diode-connected -> not a diff-pair candidate
        for b in devices[i + 1:]:
            if a["kind"] != b["kind"]:
                continue
            bn = b["nets"]
            if len(bn) < 3 or bn[0] == bn[1]:
                continue
            shared_source = an[2]
            if shared_source.lower() in SUPPLY_LIKE_NAMES:
                continue
            if an[2] == bn[2] and an[1] != bn[1] and an[0] != bn[0]:
                pairs.append(("diff_pair", a, b))
    return pairs


def find_current_mirrors(devices):
    """One diode-connected device (drain==gate) sharing gate+source with
    a second device whose drain differs -- same signature
    ../../sub-layout-handler/script/detect_topology.py's
    is_current_mirror() uses."""
    pairs = []
    for a in devices:
        an = a["nets"]
        if len(an) < 3 or an[0] != an[1]:
            continue  # not diode-connected
        for b in devices:
            if b is a or b["kind"] != a["kind"]:
                continue
            bn = b["nets"]
            if len(bn) < 3:
                continue
            if bn[1] == an[1] and bn[2] == an[2] and bn[0] != an[0]:
                pairs.append(("current_mirror", a, b))
    return pairs


def _overlap_fraction(a, b):
    """Intersection area / smaller device's own area -- normalized so a
    small device fully swallowed by a larger combined region still reads
    as ~1.0, not diluted by the union's size."""
    ax0, ay0, ax1, ay1 = a["x0_um"], a["y0_um"], a["x1_um"], a["y1_um"]
    bx0, by0, bx1, by1 = b["x0_um"], b["y0_um"], b["x1_um"], b["y1_um"]
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    smaller = min(area_a, area_b)
    return inter / smaller if smaller > 0 else 0.0


def classify_placement(a, b, overlap_threshold, row_tolerance):
    overlap = _overlap_fraction(a, b)
    if overlap < overlap_threshold:
        return "side-by-side", overlap, None

    combined_h = max(a["y1_um"], b["y1_um"]) - min(a["y0_um"], b["y0_um"])
    own_h = min(a["y1_um"] - a["y0_um"], b["y1_um"] - b["y0_um"])
    if own_h <= 0:
        return "unrecognized", overlap, None
    height_ratio = combined_h / own_h

    if height_ratio <= row_tolerance:
        return "interdigitized", overlap, height_ratio
    if height_ratio <= 2 * row_tolerance:
        return "common-centroid", overlap, height_ratio
    return "unrecognized", overlap, height_ratio


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design_dir")
    ap.add_argument("--overlap-threshold", type=float, default=DEFAULT_OVERLAP_THRESHOLD)
    ap.add_argument("--row-tolerance", type=float, default=DEFAULT_ROW_TOLERANCE)
    args = ap.parse_args()

    try:
        devices = load_devices(args.design_dir)
    except FileNotFoundError as e:
        sys.exit(f"error: {e}")

    findings = []
    matched = set()
    all_pairs = find_diff_pairs(devices) + find_current_mirrors(devices)
    for topology, a, b in all_pairs:
        key = frozenset((a["instance"], b["instance"]))
        if key in matched:
            continue
        matched.add(key)
        style, overlap, height_ratio = classify_placement(
            a, b, args.overlap_threshold, args.row_tolerance)
        findings.append({
            "topology": topology,
            "devices": [a["instance"], b["instance"]],
            "placement_style": style,
            "bbox_overlap_fraction": round(overlap, 3),
            "combined_to_single_height_ratio": round(height_ratio, 3) if height_ratio else None,
            "suggested_glayout_call": GLAYOUT_REFS.get(style),
        })

    print(f"=== Placement style report: {args.design_dir} ===")
    if not findings:
        print("No structurally-matched pairs (diff pair / current mirror) found "
              "(or no devices have placement coordinates).")
    for f in findings:
        print(f"{f['topology']:15s} {f['devices'][0]:>6s} <-> {f['devices'][1]:<6s}  "
              f"-> {f['placement_style']:15s} "
              f"(overlap={f['bbox_overlap_fraction']}, "
              f"h_ratio={f['combined_to_single_height_ratio']})")

    out_path = os.path.join(args.design_dir, "placement_style_report.json")
    with open(out_path, "w") as f:
        json.dump({
            "design_dir": args.design_dir,
            "overlap_threshold": args.overlap_threshold,
            "row_tolerance": args.row_tolerance,
            "findings": findings,
        }, f, indent=2)
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
