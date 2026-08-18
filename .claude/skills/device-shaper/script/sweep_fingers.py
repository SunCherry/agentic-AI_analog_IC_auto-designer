#!/usr/bin/env python3
"""Choose a finger count, and predict how much a sizing solution's specs
degrade once PDK-estimated parasitics are added.

This is `device-shaper`'s one script.

**Two results are compared throughout, and they are named for what they
are:**

  `ideal`      the sizing stage's converged result -- the netlist simulated
               against the ideal schematic, with no parasitics at all. Handed
               in via `--ideal-specs`; this script never re-derives it.
  `annotated`  that SAME netlist re-simulated with estimated parasitics
               annotated onto every device at a given finger count. One per
               swept Nf.

The whole question is how far `annotated` sits from what the design must
clear, and which finger count puts it closest.

**PDK-agnostic.** Every parasitic coefficient is looked up by `--pdk` from the
shared coefficient tables, so this script hardcodes nothing about any one
process -- adding a PDK is adding its table, not editing this file. A `--pdk`
with no table is refused up front, by name, rather than silently estimated
with another process's numbers.

"Significant drop" convention (documented, overridable -- see ../SKILL.md):
how far the ANNOTATED result falls short of `target_spec.json`'s own value
for that spec -- `(target - annotated) / |target|` -- NOT how far it fell
from the ideal result (neither as a fraction of the ideal value nor of
target). Adding parasitics generally causes a decline; that part is expected
and is not the question. The question this check exists to answer is "how far
is the annotated result from what the design actually needs to clear," which
depends only on the annotated value and the target, not on how much margin
the ideal result happened to carry (a spec with a lot of margin could drop a
lot in absolute terms while still comfortably clearing target -- not worth
flagging; conversely a spec with almost no margin could barely drop at all
and still miss target -- that IS worth flagging, and only a target-relative
measure catches it). Compared against `--drop-threshold` (default 0.05 = 5%,
tightened from an original 0.20/20%, to catch smaller parasitic-driven
shortfalls before they reach layout).
(Falls back to the ideal value as the reference point only when no
`--target-spec` was given at all, since there's nothing else to compare
against; a ceiling key like `Power` also keeps that ideal-relative
calculation, since it is never re-simulated here -- it is carried forward
unchanged, see below -- so it is always exactly 0, never significant,
regardless of formula.)
Only a DROP counts (an annotated result clearing target, however far it fell
along the way, is not a failure) -- Gain/UGBW/PM are all "bigger is not
worse" metrics in this project (see run_sizing_iteration.py's "Target met"
convention), so a positive value here means the annotated result fell short
of target by more than the threshold.

**Finger-count sweep and "optimal Nf" selection** (the default mode): sweeps
`--fingers` (default `1,2,4,8`) against the same sized netlist, and picks the
smallest Nf past the point where folding further stops meaningfully reducing
the worst-case spec drop -- see `select_optimal_nf()`'s own docstring for the
exact rule. Pass `--nf <N>` instead of `--fingers` to check a single finger
count with no sweep/selection.

**Stricter sizing targets for a flagged spec** (`compute_stricter_targets()`):
rather than reporting a vague "size harder", derives a concrete floor from
THIS run's own observed (target-relative) drop, PLUS a flat
`PARASITIC_HEADROOM` (default 0.10 = 10 percentage points) on top --
`target * (1 + (rel_drop + PARASITIC_HEADROOM))` -- i.e. the value a sizing
session would have to reach to survive not just the SAME drop measured here,
but that drop plus a 10-point buffer for parasitic degradation the estimate
itself may be under-predicting (e.g. a 15% observed drop implies a floor
built for a 25% drop, not just 15%: `target * 1.25`).
`--stricter-target-json` writes this out in `target_spec.json`'s own shape.
It is EVIDENCE for the hand-off report -- what a sizing session would have
had to clear for this design to survive its own estimated parasitics -- not
an input to a re-run: sizing and shaping are sequential stages, and nothing
here re-enters sizing.

Usage:
  python sweep_fingers.py <sized_netlist.sp> <testbench.spice>
      --ideal-specs <sizing's converged result>.json --pdk <pdk>
      --fingers 1,2,4,8 --drop-threshold 0.05 --target-spec target_spec.json
      [--nf-out nf_recommendation.json] [--json out.json] [--report-md out.md]
      [--stricter-target-json stricter_target_spec.json]

  # single-Nf mode, no sweep:
  python sweep_fingers.py ... --nf 2
"""
import argparse
import contextlib
import json
import os
import sys
import tempfile

# Shared helpers, imported rather than copied so there is one implementation of
# each: the PDK coefficient tables (which is what makes this script work on any
# PDK that has one), the AC metric reader, the netlist annotation + clamp-retry
# loop, and the FLOOR/CEILING target comparison.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "reference"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "parasitic-estimation", "script"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "pre-layout-extrapolation", "script"))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "schematic-sizing", "script"))

from compute_fidelity import ac_metrics  # noqa: E402
from estimate_parasitics import PDK_TABLES  # noqa: E402
from run_extrapolation import (  # noqa: E402
    mos_instance_names, make_variant_testbench, run_nf_variant, MIN_CLAMP_RETRIES,
)
from run_sizing_iteration import check_target, CEILING_METRICS  # noqa: E402

# Nothing here is process-specific: every coefficient comes from
# PDK_TABLES[pdk]. This default is only what a caller gets when it names no
# PDK -- any key present in PDK_TABLES is equally valid, and adding a process
# means adding its table, not editing this file.
def _guideline_pdk(default="sky130A", allowed=None):
    """Active PDK name from `.claude/reference/pdk_options.json`.

    The project selects one PDK there; this is how a script picks that up
    instead of hardcoding a process. Falls back to `default` if the guideline
    cannot be read, or names a PDK this script has no table for, so a
    standalone run still works.
    """
    import os as _os, sys as _sys
    try:
        _ref = _os.path.abspath(_os.path.join(
            _os.path.dirname(_os.path.abspath(__file__)),
            *([_os.pardir] * 3), "reference"))
        if _ref not in _sys.path:
            _sys.path.insert(0, _ref)
        from pdk_config import pdk as _pdk
        name = _pdk().name
    except Exception:
        return default
    return name if (allowed is None or name in allowed) else default


DEFAULT_PDK = _guideline_pdk()
DEFAULT_NF = 2
DEFAULT_FINGERS = [1, 2, 4, 8]
DEFAULT_DROP_THRESHOLD = 0.05
# flat extra headroom added on top of a flagged spec's own observed
# rel_drop when deriving its stricter sizing floor (compute_stricter_targets())
# -- a buffer against net-parasitic degradation the estimate itself may
# be under-predicting, not just "beat what this session already saw."
PARASITIC_HEADROOM = 0.10
# if increasing Nf further would reduce the worst-case spec drop by less
# than this many percentage points, it's not worth the added folding/
# routing complexity -- stop at the smallest Nf that already gets within
# this tolerance of the sweep's best achievable drop. See
# select_optimal_nf()'s docstring for the full rule.
NF_IMPROVEMENT_TOLERANCE = 0.03


def default_shaping_dir(netlist_path, cycle=None):
    """Where this skill's artifacts belong: `<design_dir>/device_shaping[/cycle_<k>]`.

    This script is handed a netlist that lives in schematic-sizing's tree
    (`<design_dir>/sizing/<design>_final.sp`), and it must NOT
    write there: that folder belongs to the other skill, and mixing this
    sweep's output into it is what makes "which skill produced this file"
    unanswerable. So walk up to the DESIGN folder -- the parent of
    `sizing` -- and put `device_shaping/` beside it.

    Falls back to the netlist's own directory when no `sizing`
    ancestor exists (a netlist passed from outside a design folder) -- a wrong
    guess about the tree is not a reason to refuse to run.
    """
    d = os.path.dirname(os.path.abspath(netlist_path))
    design_dir = None
    for _ in range(6):
        if os.path.basename(d) == "sizing":
            design_dir = os.path.dirname(d)
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    base = os.path.join(design_dir or os.path.dirname(os.path.abspath(netlist_path)),
                        "device_shaping")
    return os.path.join(base, f"cycle_{cycle}") if cycle is not None else base


def check_nf(netlist_path, testbench_path, ideal_specs, pdk=DEFAULT_PDK,
                  nf=DEFAULT_NF, drop_threshold=DEFAULT_DROP_THRESHOLD,
                  ldiff_um=None, max_retries=None, work_dir=None, target_spec=None,
                  parasitic_headroom=PARASITIC_HEADROOM, pinned_nf=None):
    netlist_path = os.path.abspath(netlist_path)
    design_name = os.path.splitext(os.path.basename(netlist_path))[0]
    work_dir = work_dir or default_shaping_dir(netlist_path)
    os.makedirs(work_dir, exist_ok=True)

    if pdk not in PDK_TABLES:
        return {"ok": False, "error": f"no parasitic coefficient table for "
                f"pdk={pdk!r} -- available: {sorted(PDK_TABLES)}. The method is "
                f"PDK-agnostic; what is missing is this process's numbers. Add "
                f"its table to PDK_TABLES (estimate_parasitics.py) and this "
                f"sweep works unchanged, or skip finger-count selection for "
                f"this PDK and say the nf was never measured."}

    coeffs = PDK_TABLES[pdk]
    ldiff_um = ldiff_um if ldiff_um is not None else coeffs["ldiff_default_um"]
    mos_names = mos_instance_names(netlist_path)
    max_retries = max_retries if max_retries is not None else max(MIN_CLAMP_RETRIES, 3 * len(mos_names))

    base_tb_text = open(testbench_path).read()
    out_path, per_device_nf, overrides, n_attempts = run_nf_variant(
        netlist_path, base_tb_text, work_dir, design_name, nf,
        mos_names, pdk, ldiff_um, max_retries, base_overrides=pinned_nf)

    if out_path is None:
        return {"ok": False, "error": f"could not simulate at Nf={nf} even after "
                f"{n_attempts} clamp-retry attempt(s)", "clamped": overrides}

    gain_dB, ugb_Hz, pm_deg = ac_metrics(out_path)
    annotated_specs = {"gain_dB": float(gain_dB),
                     "ugb_Hz": None if ugb_Hz is None else float(ugb_Hz),
                     "pm_deg": None if pm_deg is None else float(pm_deg)}
    # Power (if tracked) isn't re-simulated here -- the junction/gate-resistance
    # parasitics this annotation adds (ad/as/pd/ps/nrd/nrs, a series Rg) are
    # small-signal/resistive elements with no DC current path of their own, so
    # they don't change the circuit's DC bias point -- carry the ideal result's own
    # power forward unchanged rather than reporting it as unmet/missing
    # (annotated_specs otherwise has no "power_mw" key at all, since this
    # annotated-netlist simulation itself never computes it).
    if "power_mw" in ideal_specs:
        annotated_specs["power_mw"] = ideal_specs["power_mw"]

    per_spec = {}
    any_significant_drop = False
    tracked_keys = ("gain_dB", "ugb_Hz", "pm_deg") + (
        ("power_mw",) if "power_mw" in ideal_specs else ())
    for key in tracked_keys:
        ideal_v = ideal_specs.get(key)
        annot_v = annotated_specs.get(key)
        if ideal_v is None or annot_v is None:
            per_spec[key] = {"ideal": ideal_v, "annotated": annot_v, "rel_drop": None, "significant_drop": None}
            continue
        # "significant" is measured as how far the annotated result itself falls short
        # of target_spec.json's own value -- `(target - annotated) / target`
        # -- NOT how far it fell from the ideal result (neither as a fraction
        # of the ideal value nor of target). Adding parasitics generally
        # causes an ideal->annotated decline, that's expected and not the
        # question; the question this check exists to answer is "how far
        # is the annotated result from what the design actually needs to clear," which
        # depends only on the annotated value and target, not on how much margin the
        # ideal result happened to carry (see ../SKILL.md's "Significant drop").
        # target_spec is in its own display units (e.g. UGBW in MHz) --
        # divide by _DISPLAY_SCALE[key] to convert to this spec's raw
        # internal units (e.g. ugb_Hz, in Hz) before using.
        target_key = _TARGET_KEY[key]
        target_val_raw = _target_value(target_spec, target_key) if target_spec else None
        is_ceiling = target_key in CEILING_METRICS
        if target_val_raw is None:
            # no target given at all -- nothing to measure a shortfall
            # against, fall back to ideal-relative (the only other
            # reference point available).
            rel_drop = ((ideal_v - annot_v) / abs(ideal_v)
                        if ideal_v != 0 else float("inf"))
        elif is_ceiling:
            # a ceiling metric (Power) isn't re-simulated under annotation --
            # it's carried forward unchanged from the ideal run (see
            # annotated_specs["power_mw"] above) -- so there's no real
            # ideal->annotated change to measure here; this is always
            # exactly 0 by construction (the two are equal), never "significant".
            rel_drop = ((ideal_v - annot_v) / abs(ideal_v)
                        if ideal_v != 0 else float("inf"))
        else:
            target_val = target_val_raw / _DISPLAY_SCALE[key]
            denom = abs(target_val)
            rel_drop = (target_val - annot_v) / denom if denom != 0 else float("inf")
        significant = rel_drop > drop_threshold
        any_significant_drop = any_significant_drop or significant
        per_spec[key] = {"ideal": ideal_v, "annotated": annot_v, "rel_drop": rel_drop,
                          "significant_drop": significant}

    ideal_target = check_target(ideal_specs, target_spec) if target_spec else None
    annotated_target = check_target(annotated_specs, target_spec) if target_spec else None
    # `any_significant_drop` alone is NOT sufficient to decide "is this
    # cycle actually done" -- a spec with thin ideal-result margin can miss
    # target_spec.json outright (met=False) while its target-relative
    # shortfall still falls under the significance threshold (a real,
    # observed case, not hypothetical -- see ../SKILL.md's "Significant
    # drop"). `needs_resizing` is the union of both signals: significant
    # OR any spec failing to meet target_spec.json under annotation. This is
    # what the exit code and the "needs resizing" report lines below
    # actually gate on -- `any_significant_drop` is kept as its own,
    # narrower field for anyone who specifically wants just the
    # significance signal.
    any_missed_target = (
        any(s.get("met") is False for s in annotated_target["per_spec"].values())
        if annotated_target else False)
    needs_resizing = any_significant_drop or any_missed_target

    # A device pinned by --shape-devices arrives in `overrides` too, but it was
    # HELD at its netlist value on purpose, not reduced for feasibility.
    # Reporting the two together reads as "the sweep had to clamp 4 devices",
    # which is a different and alarming claim -- so split them.
    pinned_nf = pinned_nf or {}
    real_clamps = {k: v for k, v in overrides.items()
                   if k not in pinned_nf or pinned_nf[k] != v}

    result = {
        "ok": True,
        "nf": nf,
        "clamped": real_clamps,
        "pinned": dict(pinned_nf),
        "swept_devices": [n for n in mos_names if n not in pinned_nf],
        "attempts": n_attempts,
        "ideal_specs": ideal_specs,
        "annotated_specs": annotated_specs,
        "per_spec": per_spec,
        "drop_threshold": drop_threshold,
        "any_significant_drop": any_significant_drop,
        "needs_resizing": needs_resizing,
        "target_spec": target_spec,
        "ideal_target": ideal_target,
        "annotated_target": annotated_target,
    }
    result["stricter_targets"] = compute_stricter_targets(result, parasitic_headroom=parasitic_headroom)
    return result


def _worst_case_drop(result):
    """Max rel_drop across specs for one Nf's check_nf() result -- the
    single scalar select_optimal_nf() ranks Nf choices by. A larger value
    is worse (more degradation); a spec that IMPROVED under parasitics
    still counts via its own (possibly negative) rel_drop, so this can go
    negative only if EVERY spec improved, which just means "no drop
    problem at this Nf" and ranks accordingly."""
    drops = [s["rel_drop"] for s in result["per_spec"].values() if s["rel_drop"] is not None]
    return max(drops) if drops else float("inf")


def select_optimal_nf(per_nf_results):
    """Pick the smallest Nf past the point where folding further stops
    meaningfully helping -- see the user-facing rule this implements:
    "if performance drop is not significantly compensated by increasing
    any more, make number of fingers as small as possible."

    Rule, precisely: among successfully-simulated Nf values, prefer ones
    that keep target_spec.json fully met (if a target was given and at
    least one Nf achieves it); within that preferred set (or the whole
    successful set if none meet target), find the best (lowest)
    worst-case spec drop anywhere in the sweep, then pick the SMALLEST Nf
    whose own worst-case drop is within NF_IMPROVEMENT_TOLERANCE of that
    best value -- i.e. the first point past which folding further buys
    less than ~3 percentage points of additional drop reduction.

    Returns a dict: {"nf": int_or_None, "reasoning": str,
    "worst_case_drop_by_nf": {nf: float}, "meets_target_by_nf": {nf: bool_or_None}}."""
    successful = {nf: r for nf, r in per_nf_results.items() if r.get("ok")}
    if not successful:
        return {"nf": None, "reasoning": "no finger count in the sweep could be "
                "simulated (see per-Nf errors)", "worst_case_drop_by_nf": {},
                "meets_target_by_nf": {}}

    worst_by_nf = {nf: _worst_case_drop(r) for nf, r in successful.items()}
    has_target = any(r.get("annotated_target") is not None for r in successful.values())
    meets_target_by_nf = ({nf: r["annotated_target"]["all_met"] for nf, r in successful.items()}
                           if has_target else {})

    candidates = sorted(successful.keys())
    used_target_filter = False
    if has_target:
        meeting = sorted(nf for nf in candidates if meets_target_by_nf.get(nf))
        if meeting:
            candidates = meeting
            used_target_filter = True

    best_drop = min(worst_by_nf[nf] for nf in candidates)
    chosen = candidates[0]
    for nf in candidates:
        if worst_by_nf[nf] - best_drop <= NF_IMPROVEMENT_TOLERANCE:
            chosen = nf
            break

    trace = ", ".join(f"Nf={nf}: {worst_by_nf[nf]*100:.1f}%" for nf in sorted(successful))
    reasoning = (
        f"worst-case spec drop across the sweep: {trace}. "
        + (f"Restricted to Nf values that still meet target_spec.json ({sorted(candidates)}). "
           if used_target_filter else
           "No Nf fully met target_spec.json -- ranking by drop alone (informational, not a pass). "
           if has_target else "")
        + f"Best achievable drop is {best_drop*100:.1f}% (at Nf={min(candidates, key=lambda n: worst_by_nf[n])}); "
        f"picked the smallest Nf ({chosen}) within {NF_IMPROVEMENT_TOLERANCE*100:.0f}pp of that -- "
        f"folding further than Nf={chosen} isn't worth the added routing complexity."
    )
    return {"nf": chosen, "reasoning": reasoning, "worst_case_drop_by_nf": worst_by_nf,
            "meets_target_by_nf": meets_target_by_nf, "used_target_filter": used_target_filter}


def sweep_nf(netlist_path, testbench_path, ideal_specs, pdk=DEFAULT_PDK,
                  nf_list=None, drop_threshold=DEFAULT_DROP_THRESHOLD,
                  ldiff_um=None, max_retries=None, work_dir=None, target_spec=None,
                  parasitic_headroom=PARASITIC_HEADROOM, pinned_nf=None):
    """Run check_nf() at every Nf in nf_list (each in its own
    subfolder), then select_optimal_nf() over the results. Returns the
    OPTIMAL Nf's own check_nf() result dict (so callers/formatters
    that already understand that shape need no changes), with two keys
    added: "sweep" (every Nf's result, for the sweep table) and
    "nf_selection" (select_optimal_nf()'s own return dict)."""
    netlist_path = os.path.abspath(netlist_path)
    base_work_dir = work_dir or default_shaping_dir(netlist_path)
    nf_list = nf_list or DEFAULT_FINGERS

    per_nf = {}
    for nf in nf_list:
        per_nf[nf] = check_nf(
            netlist_path, testbench_path, ideal_specs, pdk=pdk, nf=nf,
            drop_threshold=drop_threshold, ldiff_um=ldiff_um, max_retries=max_retries,
            work_dir=os.path.join(base_work_dir, f"nf_{nf}"), target_spec=target_spec,
            parasitic_headroom=parasitic_headroom, pinned_nf=pinned_nf)

    selection = select_optimal_nf(per_nf)
    if selection["nf"] is None:
        failed = {nf: r.get("error") for nf, r in per_nf.items()}
        result = {"ok": False, "error": f"no finger count in {nf_list} could be simulated: {failed}"}
    else:
        result = dict(per_nf[selection["nf"]])  # the optimal Nf's own full result

    result["sweep"] = per_nf
    result["nf_selection"] = selection
    return result


_LABELS = {"gain_dB": "Gain (dB)", "ugb_Hz": "UGBW (MHz)", "pm_deg": "PM (deg)",
           "power_mw": "Power (mW)"}
_TARGET_KEY = {"gain_dB": "Gain", "ugb_Hz": "UGBW", "pm_deg": "PM", "power_mw": "Power"}
_DISPLAY_SCALE = {"gain_dB": 1.0, "ugb_Hz": 1e-6, "pm_deg": 1.0, "power_mw": 1.0}


def _fmt_target(val):
    """Render a target for DISPLAY, scalar or RANGE.

    BUGFIX (same run as `_target_value`): the console table and the markdown
    table both did `f"{t['target']:.4g}"`, which raises
    `TypeError: unsupported format string passed to list.__format__` on a
    RANGE key, whose target is a two-element list (PM `[50, 70]`). The
    arithmetic paths already special-case RANGE (see
    `compute_stricter_targets()`); only the two rendering sites did not.
    """
    if isinstance(val, (list, tuple)):
        return "[" + ", ".join(f"{float(v):.4g}" for v in val) + "]"
    return f"{val:.4g}"


def _target_value(target_spec, target_key):
    """The comparable NUMBER for `target_key`, from either target-spec form.

    BUGFIX (schematic-agent, two_stage_rz run): `check_nf()` used to do a bare
    `target_spec.get(target_key)` and then divide the result by
    `_DISPLAY_SCALE`, which works only for the LEGACY FLAT form
    (`{"UGBW": 15}`). Every spec file this project produces is the NORMATIVE
    form design-sheets-checker mandates -- `{"Direction","Value","Units"}` --
    so that division raised
    `TypeError: unsupported operand type(s) for /: 'dict' and 'float'`
    and the sweep could not run at all. This helper is the same
    `isinstance(entry, dict)` handling `write_stricter_target_spec()` already
    does at the bottom of this file; only `check_nf()` was left unmigrated.

    RANGE keys (e.g. PM `[50, 70]`) resolve to their LOWER bound: this
    function feeds a shortfall measure whose whole question is "how far short
    of what the design must clear", and for a window that bar is its floor.
    """
    entry = target_spec.get(target_key)
    if isinstance(entry, dict):
        entry = entry.get("Value")
    if isinstance(entry, (list, tuple)):
        return float(entry[0]) if entry else None
    return entry


def compute_stricter_targets(result, parasitic_headroom=PARASITIC_HEADROOM):
    """For every spec that dropped significantly at this Nf, derive a
    QUANTITATIVE sizing floor from THIS session's own observed relative
    drop PLUS a flat extra headroom, instead of the vaguer "just beat
    this sized result" rule -- see ../SKILL.md's "When a spec is flagged" section.

    `rel_drop` (computed in check_nf()) is `(target - annotated) /
    target` -- how far the annotated result ITSELF falls short of target, not how far
    it fell from the ideal result -- e.g. target=40dB, ideal=41.58dB,
    annotated=38.08dB gives `rel_drop = (40-38.08)/40` = 4.8%, NOT
    `(41.58-38.08)/40` = 8.7% and NOT `(41.58-38.08)/41.58` = 8.4% (two
    earlier, rejected formulations -- see ../SKILL.md's "Significant
    drop" for why the ideal value doesn't belong in this calculation at all: the
    question this whole mechanism answers is "how far is the annotated result from
    what's actually needed," not "how much did parasitics cost relative
    to some other number"). The floor is `target * (1 + effective_drop)`
    where `effective_drop = rel_drop + parasitic_headroom` -- i.e. close
    the exact gap this session observed (`target - annotated`, as a
    fraction of target), PLUS a flat `PARASITIC_HEADROOM` (default 0.10
    = 10 percentage points) buffer on top, for net-parasitic degradation
    this one session's estimate may be under-predicting. E.g. a spec
    that fell 15% short of its target this session derives a floor sized
    for a 25% shortfall margin, not just 15%: `target * 1.25`. Compared
    against the ideal result's own achieved value too (the stricter -- larger --
    of the two wins): a spec that already cleared target by a wide
    margin but still crossed the significance threshold is a fragility
    signal on its own, so it still gets pushed higher, not held flat
    just because target was technically met this session.

    **A real limitation, stated plainly, not glossed over**: because
    this floor is derived purely from target and the annotated result (not from
    the ideal value), it does NOT model how big a sizing change is
    actually needed to close the gap -- it assumes closing the same
    ABSOLUTE shortfall (`target - annotated`) is sufficient, not the
    (generally larger) ideal-to-annotated degradation a bigger sized
    device actually sees under real parasitic annotation (which scales
    with device geometry, i.e. exactly what sizing changes). Concretely,
    on one design's real data: this formula's floor
    (`target=40, annotated=38.08` -> ~41.92 before headroom) is barely above
    the ideal result's OWN already-achieved 41.58 -- a real sizing session
    chasing this floor should expect the actual required increase to be
    larger than this number alone suggests, and should keep re-deriving
    from each new cycle's own observed shortfall (not assume this first
    number is final) until the annotated result actually clears.

    Returns {target_key: {"original_target", "rel_drop",
    "effective_drop", "ideal_value", "stricter_target", "note"}} for
    every flagged spec that has a target in target_spec.json.
    `effective_drop` is `rel_drop + parasitic_headroom`, the value
    actually used to derive the floor. `stricter_target` is None (with
    an explanatory `note`) when the annotated result's own raw value collapsed to
    `<=0` (or is missing) -- unlike the old ideal-relative formula, the
    `target * (1 + effective_drop)` floor above is always finite no
    matter how large `effective_drop` gets, so a large-but-finite ask is
    reported as a real (if steep) number, not treated as impossible;
    only an actual annotated collapse (the circuit fundamentally breaking
    under parasitics, not just dropping a lot) gets the "no margin can
    fix this" null case -- that is a topology/compensation problem, not
    a sizing-margin one.

    **Ceiling-metric relaxation** (e.g. `Power`, see `CEILING_METRICS` in
    run_sizing_iteration.py): tightening every FLOOR spec (Gain/UGBW/PM)
    by the amounts above and leaving a ceiling spec's own bar unchanged
    makes the next sizing session's job strictly harder on every axis at
    once, with no offsetting slack anywhere -- exactly the failure mode
    this project's own `test_miller` restart hit (a power ceiling closing
    off "just add bias current" as the cheap way to buy more Gain/UGBW
    margin, see ../SKILL.md's "When a spec is flagged"). So every tracked
    `CEILING_METRICS` key gets RELAXED (its ceiling raised) by the SAME
    fraction the floor specs just got tightened by -- `worst_effective_drop`,
    the max `effective_drop` across this call's own newly-derived floor
    entries (not an independent number) -- via `target * (1 +
    worst_effective_drop)`. Using the worst (not average) floor spec's
    number keeps this consistent with the rest of this function's own
    worst-case-wins convention (see the floor-vs-ideal-value `max()`
    above); a design that needed a big Gain floor bump gets
    proportionally more power slack to go find it with, not a diluted
    average that under-relaxes the metric that actually needs the room.
    Only runs when at least one floor spec was actually flagged this
    call AND the ceiling key is present in target_spec.json -- an
    all-quiet cycle (nothing flagged) leaves every ceiling target
    untouched, there being nothing to offset."""
    if not result.get("ok") or not result.get("target_spec") or not result.get("ideal_target"):
        return {}
    out = {}
    annotated_target = result.get("annotated_target") or {}
    for key, s in result["per_spec"].items():
        target_key = _TARGET_KEY[key]
        # Trigger on EITHER signal, not "significant_drop" alone -- a spec
        # can miss target_spec.json outright (met=False) while still
        # falling under the significance threshold (the ideal result had thin
        # margin to begin with, so even a small target-relative shortfall
        # is enough to miss -- a real, observed case, not hypothetical,
        # see ../SKILL.md's "Significant drop"). Skipping stricter-target
        # derivation for a spec that plainly fails target just because it
        # wasn't flagged "significant" would silently leave a failing
        # design with no stricter floor to aim for -- the "significant"
        # threshold is about catching parasitic-driven regressions early,
        # not about deciding whether a real miss deserves a floor at all.
        annotated_met = (annotated_target.get("per_spec", {}).get(target_key) or {}).get("met")
        if not s.get("significant_drop") and annotated_met is not False:
            continue
        t = result["ideal_target"]["per_spec"].get(target_key)
        if t is None:
            continue  # this spec isn't tracked in target_spec.json -- nothing to derive
        if t.get("direction") == "RANGE":
            # A two-sided spec has no single bar to raise. The drop this
            # function measures is one-sided (target - annotated), so there is no
            # honest way to turn it into a narrower interval: falling out the
            # BOTTOM of a range and falling out the TOP demand opposite moves,
            # and this number cannot tell them apart. Report it and leave the
            # interval alone rather than deriving a bound nobody can justify.
            out[target_key] = {
                "original_target": t["target"], "rel_drop": s.get("rel_drop"),
                "effective_drop": None, "ideal_value": t["sim"],
                "stricter_target": None, "kind": "floor",
                "note": f"{target_key} is a RANGE spec -- no stricter bound is "
                        f"derived. Tightening a two-sided interval needs to know "
                        f"WHICH end the design is drifting toward, which the "
                        f"one-sided drop measured here does not say. Narrow it by "
                        f"hand, or let 'Harder target' do it up front.",
            }
            continue
        rel_drop = s["rel_drop"]
        # t["sim"] (not s["ideal"]) -- check_target() already converts to
        # target_spec.json's own units (e.g. UGBW: Hz -> MHz); s["ideal"]
        # is raw ugb_Hz, comparing/maxing that against a target-scale
        # floor_from_drop below silently produced a nonsense floor
        # (~1.4e7 "MHz") until caught by actually re-running this against
        # a flagged UGBW case, not just Gain/PM (which have no unit
        # mismatch, so the bug stayed hidden until now)
        ideal_value = t["sim"]
        target_val = t["target"]
        annotated_value = s.get("annotated")
        effective_drop = rel_drop + parasitic_headroom if rel_drop is not None else None
        # a real annotated collapse (the circuit itself breaking under
        # parasitics -- not just dropping a lot) is checked directly on
        # the annotated result's own raw value, not via effective_drop's magnitude:
        # target*(1+effective_drop) below is finite for ANY effective_drop,
        # so a large-but-finite drop no longer implies "impossible" the
        # way it did under the old ideal-relative division formula.
        if effective_drop is None or annotated_value is None or annotated_value <= 0:
            out[target_key] = {
                "original_target": target_val, "rel_drop": rel_drop,
                "effective_drop": effective_drop,
                "ideal_value": ideal_value, "stricter_target": None,
                "kind": "floor",
                "note": "the annotated value collapsed to <=0 (or is "
                        "undefined) at this Nf -- no sizing floor can "
                        "compensate for the circuit itself breaking under "
                        "parasitics by margin alone; treat this as a "
                        "topology/compensation problem, not a "
                        "sizing-margin one.",
            }
            continue
        floor_from_drop = target_val * (1 + effective_drop)
        stricter = max(floor_from_drop, ideal_value) if ideal_value is not None else floor_from_drop
        flagged_because = ("significant_drop" if s.get("significant_drop")
                            else "missed_target_below_threshold")
        drop_threshold_for_note = result.get("drop_threshold", DEFAULT_DROP_THRESHOLD)
        below_threshold_note = (
            "" if s.get("significant_drop") else
            f" (this spec's {rel_drop*100:.1f}% shortfall is UNDER the "
            f"{drop_threshold_for_note:.0%} significance threshold, but "
            f"the annotated result still misses target outright -- flagged anyway, since "
            f"a real miss always needs a floor regardless of whether it "
            f"crossed the significance bar)")
        out[target_key] = {
            "original_target": target_val, "rel_drop": rel_drop,
            "effective_drop": effective_drop,
            "ideal_value": ideal_value, "stricter_target": stricter,
            "kind": "floor", "flagged_because": flagged_because,
            "note": f"the annotated result ({annotated_value:.4g}) fell {rel_drop*100:.1f}% short "
                    f"of target ({target_val:.4g}) this session -- NOT measured "
                    f"against the ideal result's own {ideal_value:.4g}"
                    f"{below_threshold_note}, "
                    f"+{parasitic_headroom*100:.0f}pp parasitic headroom = "
                    f"{effective_drop*100:.1f}% used to derive the floor -- "
                    f"if the next sizing session clears {stricter:.4g} and "
                    f"the annotated result falls the same amount short again, it should "
                    f"still land back at ~{target_val:.4g} (the original "
                    f"target).",
        }

    floor_drops = [info["effective_drop"] for info in out.values()
                   if info.get("effective_drop") is not None]
    if not floor_drops:
        return out  # nothing flagged this cycle -- no offsetting relaxation to give
    worst_effective_drop = max(floor_drops)
    # Which keys are ceilings comes from check_target()'s per-spec `direction`,
    # which reads target_spec.json's own `Direction` field. CEILING_METRICS is
    # only the fallback for a flat spec that declares none -- iterating it
    # instead would miss a declared ceiling whose key isn't in that set (any
    # noise, offset or area spec) and relax nothing for it.
    per_spec = result["ideal_target"]["per_spec"]
    ceiling_keys = [k for k, v in per_spec.items()
                    if (v.get("direction") == "CEILING"
                        or (v.get("direction") is None and k in CEILING_METRICS))]
    for ceiling_key in ceiling_keys:
        if ceiling_key in out:
            continue  # a ceiling metric can't itself be a flagged floor entry
        t = per_spec.get(ceiling_key)
        if t is None:
            continue  # this ceiling metric isn't tracked in target_spec.json
        if t.get("measurable") is False:
            # Nothing in this sweep produces a value for this key (check_target()
            # reports it `unmeasured`, met=None). Relaxing a bar nobody measured
            # would write a looser spec into stricter_target_spec.json -- which
            # then becomes the current target -- on no evidence at all. Leave it
            # at its original value; the write-through copies that unchanged.
            continue
        original_ceiling = t["target"]
        relaxed_ceiling = original_ceiling * (1 + worst_effective_drop)
        out[ceiling_key] = {
            "original_target": original_ceiling, "rel_drop": None,
            "effective_drop": None, "ideal_value": t["sim"],
            "stricter_target": relaxed_ceiling, "kind": "ceiling_relaxed",
            "note": f"RELAXED (not tightened -- {ceiling_key} is a ceiling metric, "
                    f"see CEILING_METRICS) by {worst_effective_drop*100:.1f}% -- the "
                    f"worst effective_drop among this cycle's flagged floor specs -- "
                    f"so the next sizing session has proportionally more power "
                    f"budget to go find the tightened floor margin with, instead of "
                    f"every axis getting strictly harder at once.",
        }
    return out


def write_stricter_target_spec(result, path):
    """Write a target_spec.json-shaped file -- same keys as the
    original -- where every flagged spec's value is replaced by its
    derived stricter_target (see compute_stricter_targets()) and every
    other spec keeps its original value -- the concrete bar a sizing
    session would have to clear, reported in the hand-off rather than fed
    back into one (sizing and shaping are sequential stages). A ceiling
    key present in target_spec.json (e.g.
    `Power`) gets its OWN entry in `stricter_targets` too when any floor
    spec was flagged this cycle -- a relaxed (raised) ceiling, not a
    tightened one -- so this same write-through also carries that
    relaxation into the file; nothing metric-direction-specific for the
    caller to handle here. Returns False (writes nothing) if there's no
    target_spec or no flagged spec has a finite stricter floor to
    write."""
    target_spec = result.get("target_spec")
    stricter = result.get("stricter_targets") or {}
    if not target_spec:
        return False
    out = dict(target_spec)
    wrote_any = False
    for target_key, info in stricter.items():
        if info["stricter_target"] is not None:
            # Keep the entry's own shape. A structured
            # {Direction, Value, Units} spec must come back structured, or the
            # file this writes cannot be read by the same check_target() that
            # produced the numbers in it -- see design-sheets-checker's
            # spec_form_template.md.
            entry = target_spec.get(target_key)
            if isinstance(entry, dict):
                new_entry = dict(entry)
                new_entry["Value"] = info["stricter_target"]
                out[target_key] = new_entry
            else:
                out[target_key] = info["stricter_target"]
            wrote_any = True
    if not wrote_any:
        return False
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(out, f, indent=2)
    return True


def _format_stricter_targets(result, markdown=False):
    """Lines for the derived stricter-sizing-target table -- shared by
    both formatters, same pattern as _format_nf_sweep(). Returns [] if
    no spec was flagged (no "stricter_targets", or an empty dict)."""
    stricter = result.get("stricter_targets") or {}
    if not stricter:
        return []
    b = "**" if markdown else ""
    lines = [""]
    lines.append(f"{b}Stricter sizing targets{b} -- a FINDING for the hand-off, not "
                 f"an input to a re-run (derived from "
                 f"THIS session's own observed drop plus a flat parasitic "
                 f"headroom -- ceiling metrics like Power are RELAXED by the same "
                 f"worst-case fraction instead of tightened -- see ../SKILL.md's "
                 f"\"When a spec is flagged\"):")
    if markdown:
        lines.append("| Spec | Original target | Observed drop | +Headroom = Effective drop | New sizing bar |")
        lines.append("|---|---|---|---|---|")
    for target_key, info in stricter.items():
        # Read the direction off the entry compute_stricter_targets() recorded,
        # not off CEILING_METRICS membership: that set is only the fallback for
        # a flat spec declaring no Direction, so a DECLARED ceiling outside it
        # (a noise, offset or area key) was relaxed by that function and then
        # printed here as though it had been tightened. `kind` is absent only in
        # a result.json written before this field existed -- hence the fallback.
        is_ceiling = (info.get("kind") == "ceiling_relaxed"
                      if info.get("kind") is not None
                      else target_key in CEILING_METRICS)
        drop_str = f"{info['rel_drop']*100:.1f}%" if info["rel_drop"] is not None else "n/a"
        eff_str = f"{info['effective_drop']*100:.1f}%" if info.get("effective_drop") is not None else "n/a"
        bar_str = (f"{info['stricter_target']:.4g}{' (relaxed)' if is_ceiling else ''}"
                   if info["stricter_target"] is not None
                   else "n/a -- needs a topology/compensation fix")
        if markdown:
            lines.append(f"| {target_key} | {_fmt_target(info['original_target'])} | {drop_str} | {eff_str} | {bar_str} |")
        else:
            lines.append(f"  {target_key}: {_fmt_target(info['original_target'])} -> {bar_str} "
                         f"(observed drop {drop_str}, effective {eff_str})")
    return lines


def _format_nf_sweep(result, markdown=False):
    """Lines for the finger-count sweep + optimal-Nf selection -- shared
    by format_comparison_table() and format_comparison_table_markdown()
    so the sweep table only has one implementation. Returns [] if this
    result didn't come from sweep_nf() (e.g. single-Nf mode)."""
    if "sweep" not in result:
        return []
    b = "**" if markdown else ""
    # normalize every Nf key to int, robust to a JSON round-trip (dict
    # keys serialize to strings in JSON, but selection["nf"] -- a dict
    # VALUE, not a key -- stays an int; comparing the two without
    # normalizing silently breaks the "OPTIMAL" marker after a save/load,
    # and string-sorts "16" before "2" for double-digit Nf) -- found by
    # actually reloading a saved result.json, not assumed.
    sweep = {int(nf): r for nf, r in result["sweep"].items()}
    selection = dict(result["nf_selection"])
    selection["worst_case_drop_by_nf"] = {int(nf): v for nf, v in selection["worst_case_drop_by_nf"].items()}
    selection["meets_target_by_nf"] = {int(nf): v for nf, v in selection["meets_target_by_nf"].items()}
    if selection["nf"] is not None:
        selection["nf"] = int(selection["nf"])
    if markdown:
        lines = [""]  # caller already wrote the "## Finger-count sweep" heading
    else:
        lines = ["", f"{b}Finger-count sweep{b} (optimal Nf selection -- see "
                     f"../SKILL.md's \"Finger-count sweep\"):"]

    if markdown:
        lines.append("| Nf | Worst-case drop | Meets target? | Status |")
        lines.append("|---|---|---|---|")
    else:
        lines.append(f"{'Nf':<6}{'Worst-case drop':>17}{'Meets target?':>15}{'Status':>10}")

    for nf in sorted(sweep):
        r = sweep[nf]
        if not r.get("ok"):
            if markdown:
                lines.append(f"| {nf} | FAILED | -- | -- |")
            else:
                lines.append(f"{nf:<6}{'FAILED':>17}{'--':>15}{'--':>10}")
            continue
        worst = selection["worst_case_drop_by_nf"].get(nf)
        meets = selection["meets_target_by_nf"].get(nf)
        meets_str = ("yes" if meets else "no") if meets is not None else "n/a"
        worst_str = f"{worst*100:.1f}%" if worst is not None else "n/a"
        is_optimal = nf == selection["nf"]
        if markdown:
            status = "**OPTIMAL**" if is_optimal else ""
            lines.append(f"| {nf} | {worst_str} | {meets_str} | {status} |")
        else:
            chosen_str = "<= OPTIMAL" if is_optimal else ""
            lines.append(f"{nf:<6}{worst_str:>17}{meets_str:>15}{chosen_str:>10}")

    lines.append("")
    lines.append(f"{b}Optimal Nf: {selection['nf']}{b} -- {selection['reasoning']}")
    return lines


def format_comparison_table(result):
    """The final ideal-vs-parasitic-annotated comparison table -- see
    ../SKILL.md's "Report" section, which requires schematic-agent
    to always print exactly this table (success or budget-exhausted) so
    a run's outcome is never just a prose claim."""
    if not result.get("ok"):
        lines = [f"PARASITIC-ANNOTATED CHECK FAILED: {result.get('error')}"]
        lines += _format_nf_sweep(result, markdown=False)
        return "\n".join(lines)

    has_target = bool(result.get("target_spec"))
    lines = [f"=== Ideal-schematic vs parasitic-annotated (Nf={result['nf']}) ==="]
    header = f"{'Metric':<12}"
    if has_target:
        header += f"{'Target':>10}"
    header += f"{'Ideal':>17}"
    if has_target:
        header += f"{'Met?':>6}"
    header += f"{'Annotated (parasitic)':>23}"
    if has_target:
        header += f"{'Met?':>6}"
    header += f"{'Rel. drop':>11}{'Significant?':>14}"
    lines.append(header)

    for key, label in _LABELS.items():
        s = result["per_spec"].get(key)
        if s is None:
            continue  # this metric wasn't tracked for this run (e.g. no Power target) -- skip the row
        if has_target and _TARGET_KEY[key] not in result["ideal_target"]["per_spec"]:
            continue  # tracked in specs, but target_spec.json has no entry for it -- skip the row
        scale = _DISPLAY_SCALE[key]
        row = f"{label:<12}"
        if has_target:
            t = result["ideal_target"]["per_spec"][_TARGET_KEY[key]]
            row += f"{_fmt_target(t['target']):>10}"
        if s["ideal"] is None:
            row += f"{'n/a':>17}"
        else:
            row += f"{s['ideal']*scale:>17.4g}"
        if has_target:
            ideal_met = result["ideal_target"]["per_spec"][_TARGET_KEY[key]]["met"]
            row += f"{('yes' if ideal_met else 'NO'):>6}"
        if s["annotated"] is None:
            row += f"{'n/a':>23}"
        else:
            row += f"{s['annotated']*scale:>23.4g}"
        if has_target:
            annot_met = result["annotated_target"]["per_spec"][_TARGET_KEY[key]]["met"]
            row += f"{('yes' if annot_met else 'NO'):>6}"
        if s["rel_drop"] is None:
            row += f"{'n/a':>11}{'n/a':>14}"
        else:
            row += f"{s['rel_drop']*100:>10.1f}%{('YES' if s['significant_drop'] else 'no'):>14}"
        lines.append(row)

    lines.append("")
    lines.append(f"Any spec fell >{result['drop_threshold']*100:.0f}% short of target "
                  f"(ideal -> annotated): "
                  f"{'YES' if result['any_significant_drop'] else 'no'}")
    lines.append(f"Needs resizing (significant shortfall OR any spec outright misses "
                  f"target_spec.json, even under the threshold): "
                  f"{'YES -- report it in the hand-off; sizing is NOT re-run' if result['needs_resizing'] else 'no'}")
    if has_target:
        ideal_ok = result["ideal_target"]["all_met"]
        annot_ok = result["annotated_target"]["all_met"]
        lines.append(f"All target_spec.json specs met -- ideal: {'yes' if ideal_ok else 'NO'}"
                     f" | annotated: {'yes' if annot_ok else 'NO'}")
        if ideal_ok and not annot_ok:
            lines.append("-- the ideal result looked like a clean pass, but the parasitic-annotated "
                          "prediction shows it would NOT survive realistic layout parasitics; "
                          "this is exactly the case this check exists to catch.")
    if result.get("swept_devices") and result.get("pinned"):
        lines.append(f"Swept (parasitic_sensitivity severity=high): "
                      f"{', '.join(result['swept_devices'])}")
        lines.append(f"Pinned at netlist nf (not flagged): "
                      f"{', '.join(f'{k}=nf{v}' for k, v in result['pinned'].items())}")
    if result["clamped"]:
        lines.append(f"Clamped devices (finger count reduced below Nf={result['nf']} for "
                      f"feasibility): {result['clamped']}")
    lines += _format_stricter_targets(result, markdown=False)
    lines += _format_nf_sweep(result, markdown=False)
    return "\n".join(lines)


def flagged_specs_summary(result):
    """Ranked, scripted (not reasoned) summary of which specs need
    attention, worst first -- the mechanical part a script can do
    reliably. See ../SKILL.md's "Report" -- the schematic-agent is
    expected to APPEND a "Performance drop analysis" section to this file
    with the actual causal reasoning (which device's added parasitic
    likely did it), which requires netlist/topology understanding this
    script doesn't have.

    Same "significant_drop OR missed target outright" criterion as
    compute_stricter_targets() (see that function's own docstring) --
    kept in sync deliberately, so this list and the "Stricter sizing
    targets" table below it in the same report never disagree about
    which specs are flagged."""
    if not result.get("ok"):
        return []
    annotated_target = result.get("annotated_target") or {}
    flagged = []
    for key, s in result["per_spec"].items():
        target_key = _TARGET_KEY.get(key)
        annotated_met = (annotated_target.get("per_spec", {}).get(target_key) or {}).get("met")
        if s.get("significant_drop") or annotated_met is False:
            flagged.append((key, s))
    # `rel_drop` is None whenever the annotated run produced no value for a spec -- an AC
    # sweep that never crosses 0 dB gives `ugb_Hz = None`, which check_target()
    # scores as met=False (measurable, not produced), so such a spec DOES reach
    # this list. Sorting those against floats raised TypeError and took the
    # whole report down after the sweep had already run. Rank them worst-first
    # instead: a spec with no annotated value at all is not a small drop.
    flagged.sort(key=lambda kv: (float("inf") if kv[1]["rel_drop"] is None
                                 else kv[1]["rel_drop"]), reverse=True)
    return [(_LABELS[key], s["rel_drop"]) for key, s in flagged]


def format_comparison_table_markdown(result):
    """Same content as format_comparison_table(), as a GFM markdown
    table -- for saving to <design_dir>/device_shaping/shaping_report.md
    (see ../SKILL.md's "Report"), not just a console print. NOT anywhere
    under sizing/, which belongs to the sizing skill (see
    default_shaping_dir())."""
    if not result.get("ok"):
        lines = [f"**PARASITIC-ANNOTATED CHECK FAILED**: {result.get('error')}"]
        if "sweep" in result:
            lines.append("")
            lines.append("## Finger-count sweep")
            lines += _format_nf_sweep(result, markdown=True)
        return "\n".join(lines) + "\n"

    has_target = bool(result.get("target_spec"))
    lines = [f"# Final report: ideal-schematic vs parasitic-annotated (Nf={result['nf']})", ""]

    header = ["Metric"]
    if has_target:
        header.append("Target")
    header.append("Ideal")
    if has_target:
        header.append("Met?")
    header.append("Annotated (parasitic)")
    if has_target:
        header.append("Met?")
    header += ["Rel. drop", "Significant?"]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))

    for key, label in _LABELS.items():
        s = result["per_spec"].get(key)
        if s is None:
            continue  # this metric wasn't tracked for this run (e.g. no Power target) -- skip the row
        if has_target and _TARGET_KEY[key] not in result["ideal_target"]["per_spec"]:
            continue  # tracked in specs, but target_spec.json has no entry for it -- skip the row
        scale = _DISPLAY_SCALE[key]
        row = [label]
        if has_target:
            t = result["ideal_target"]["per_spec"][_TARGET_KEY[key]]
            row.append(_fmt_target(t['target']))
        row.append("n/a" if s["ideal"] is None else f"{s['ideal']*scale:.4g}")
        if has_target:
            ideal_met = result["ideal_target"]["per_spec"][_TARGET_KEY[key]]["met"]
            row.append("yes" if ideal_met else "**NO**")
        row.append("n/a" if s["annotated"] is None else f"{s['annotated']*scale:.4g}")
        if has_target:
            annot_met = result["annotated_target"]["per_spec"][_TARGET_KEY[key]]["met"]
            row.append("yes" if annot_met else "**NO**")
        if s["rel_drop"] is None:
            row += ["n/a", "n/a"]
        else:
            row.append(f"{s['rel_drop']*100:.1f}%")
            row.append("**YES**" if s["significant_drop"] else "no")
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    lines.append(f"**Any spec fell >{result['drop_threshold']*100:.0f}% short of target** "
                  f"(ideal -> annotated): "
                  f"{'**YES**' if result['any_significant_drop'] else 'no'}")
    lines.append("")
    lines.append(f"**Needs resizing** (significant shortfall OR any spec outright misses "
                  f"target_spec.json, even under the threshold): "
                  f"{'**YES** -- report it in the hand-off; sizing is NOT re-run' if result['needs_resizing'] else 'no'}")
    if has_target:
        ideal_ok = result["ideal_target"]["all_met"]
        annot_ok = result["annotated_target"]["all_met"]
        lines.append("")
        lines.append(f"**All target_spec.json specs met** -- ideal: "
                     f"{'yes' if ideal_ok else '**NO**'} | annotated: "
                     f"{'yes' if annot_ok else '**NO**'}")
        if ideal_ok and not annot_ok:
            lines.append("")
            lines.append("> The ideal result looked like a clean pass, but the parasitic-annotated "
                          "prediction shows it would **not** survive realistic layout "
                          "parasitics -- exactly the case this check exists to catch.")
    if result.get("swept_devices") and result.get("pinned"):
        lines.append("")
        lines.append(f"**Swept** (`parasitic_sensitivity` severity=high): "
                      f"`{', '.join(result['swept_devices'])}`")
        lines.append("")
        lines.append(f"**Pinned** at their netlist nf (not flagged): "
                      f"`{', '.join(f'{k}=nf{v}' for k, v in result['pinned'].items())}`")
    if result["clamped"]:
        lines.append("")
        lines.append(f"Clamped devices (finger count reduced below Nf={result['nf']} for "
                      f"feasibility): `{result['clamped']}`")

    if "sweep" in result:
        lines.append("")
        lines.append("## Finger-count sweep")
        lines += _format_nf_sweep(result, markdown=True)

    flagged = flagged_specs_summary(result)
    if flagged:
        lines.append("")
        lines.append("## Flagged specs (ranked worst first)")
        for label, rel_drop in flagged:
            if rel_drop is None:
                lines.append(f"- **{label}**: no annotated value at this Nf -- the "
                              f"annotated run produced none (e.g. an AC sweep with no "
                              f"0 dB crossing), so it misses target outright and no "
                              f"drop fraction exists. See \"Performance drop analysis\" "
                              f"below.")
            else:
                lines.append(f"- **{label}**: dropped {rel_drop*100:.1f}% -- see "
                              f"\"Performance drop analysis\" below for the likely cause.")
        lines += _format_stricter_targets(result, markdown=True)
        lines.append("")
        lines.append("## Performance drop analysis")
        lines.append("*(schematic-agent: append your own causal reasoning here, grounded "
                      "in the sizing stage's per-device op-point data and this netlist's topology -- "
                      "which specific device's added parasitic most likely drove each flagged "
                      "spec's drop, and why -- before presenting this report. Do not leave "
                      "this placeholder in the final file.)*")

    return "\n".join(lines) + "\n"


def save_report(result, path):
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(format_comparison_table_markdown(result))


def write_nf_recommendation(out_path, result):
    """Record the selected Nf as a standalone `nf_recommendation.json`, in
    THIS skill's own output folder.

    It used to be written as an `_nf_recommendation` block inside the sizing
    skill's `params_values.json`. That file no longer exists -- sizing keeps its
    values in the netlist itself -- and writing into another skill's state was
    the wrong shape regardless. The finger count reaches the design the way it
    should: baked into the hand-off netlist by `finalize_netlist.py --nf`
    (see ../SKILL.md's "Writing the finger count in"). This file is the
    provenance beside it -- what was chosen, and on what evidence.

    No-op if `result` didn't come from sweep_nf() (no "nf_selection")."""
    if "nf_selection" not in result:
        return
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({
            "nf": result["nf_selection"]["nf"],
            "basis": result["nf_selection"]["reasoning"],
            "worst_case_drop_by_nf": result["nf_selection"]["worst_case_drop_by_nf"],
            "meets_target_by_nf": result["nf_selection"]["meets_target_by_nf"],
        }, f, indent=2)


def print_report(result):
    print(format_comparison_table(result))


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist")
    ap.add_argument("testbench")
    ap.add_argument("--ideal-specs", required=True,
                     help="JSON file with {\"gain_dB\":.., \"ugb_Hz\":.., \"pm_deg\":..} "
                          "-- e.g. run_sizing_iteration.py's --json output's \"specs\" key")
    ap.add_argument("--pdk", default=DEFAULT_PDK, choices=sorted(PDK_TABLES),
                     help="which PDK's coefficient table to annotate with. The "
                          "choices are whatever PDK_TABLES currently carries -- "
                          "this script is not written around any one process")
    ap.add_argument("--nf", type=int, default=None,
                     help="check a single finger count, no sweep/selection "
                          "(default: sweep --fingers instead)")
    ap.add_argument("--fingers", default=",".join(str(n) for n in DEFAULT_FINGERS),
                     help="finger counts to sweep for optimal-Nf selection "
                          "(ignored if --nf is given)")
    ap.add_argument("--drop-threshold", type=float, default=DEFAULT_DROP_THRESHOLD)
    ap.add_argument("--parasitic-headroom", type=float, default=PARASITIC_HEADROOM,
                     help="flat extra fraction added on top of a flagged spec's own "
                          "observed rel_drop when deriving its stricter sizing floor "
                          "(see compute_stricter_targets()) -- a buffer for net-"
                          "parasitic degradation this session's single observed drop "
                          "may be under-predicting")
    ap.add_argument("--ldiff-um", type=float, default=None)
    ap.add_argument("--cycle", type=int, default=None,
                     help="this invocation's number. With it, saved artifacts land "
                          "in <design_dir>/device_shaping/cycle_<k>/ so a re-run "
                          "cannot overwrite an earlier sweep's evidence. Normally 1 "
                          "-- shaping runs once per design, after sizing. Ignored "
                          "when --work-dir is given explicitly.")
    ap.add_argument("--work-dir", default=None,
                     help="where the sweep's nf_<N>/ folders go. Default: "
                          "<design_dir>/device_shaping (see "
                          "default_shaping_dir) -- deliberately NOT anywhere "
                          "under sizing/, which belongs to the sizing "
                          "skill. Pass device_shaping/cycle_<k> to keep a "
                          "re-run's sweep separate from an earlier one's.")
    ap.add_argument("--target-spec", default=None,
                     help="target_spec.json -- if given, the printed table also shows "
                          "whether the ideal AND the annotated result each independently meet it, and "
                          "the optimal-Nf selection prefers Nf values that still meet it")
    ap.add_argument("--nf-out", default=None,
                     help="where to write nf_recommendation.json, this skill's own "
                          "record of the selected Nf (sweep mode only) -- see "
                          "write_nf_recommendation()")
    ap.add_argument("--json", default=None)
    ap.add_argument("--report-md", default=None,
                     help="also save the comparison table as markdown here "
                          "(<design_dir>/device_shaping/shaping_report.md -- NOT "
                          "under sizing/) -- schematic-agent must then append its "
                          "own 'Performance drop analysis' section, see "
                          "../SKILL.md's 'Report'")
    ap.add_argument("--stricter-target-json", default=None,
                     help="write a target_spec.json-shaped file here with every "
                          "flagged spec's value replaced by its derived stricter "
                          "sizing floor (see compute_stricter_targets()) -- this "
                          "is EVIDENCE for the hand-off report, not an input to a "
                          "sizing re-run: sizing and shaping are sequential stages")
    ap.add_argument("--save-artifacts", action="store_true",
                     help="persist the per-Nf annotated netlists/testbenches/"
                          "clamp-retry attempts under <work_dir>/nf_<n>/ -- "
                          "default is a temp dir instead, auto-deleted on exit "
                          "(most annotated checks are exploratory bounce-back "
                          "cycles and this avoids accumulating dozens of "
                          "sweep subfolders over a multi-cycle run). The "
                          "result (--json/--report-md/--stricter-target-json) "
                          "is unaffected either way. Ignored if --work-dir is "
                          "given explicitly (that's already an explicit "
                          "request to keep it there). Use this flag for a "
                          "check specifically worth keeping on disk -- see "
                          "../SKILL.md's \"Working folder\"")
    ap.add_argument("--shape-devices", default=None,
                     help="restrict the sweep to these MOS devices: either a "
                          "select_shape_devices.py JSON, or a comma-separated "
                          "instance list. Every OTHER MOS device is pinned at "
                          "the nf the netlist already carries, so the sweep "
                          "moves only the devices circuit_decomposition.yaml "
                          "flagged high-severity. Default (omitted) sweeps "
                          "every MOS device, the original behaviour.")
    args = ap.parse_args()

    ideal_data = json.load(open(args.ideal_specs))
    ideal_specs = ideal_data.get("specs", ideal_data)
    target_spec = json.load(open(args.target_spec)) if args.target_spec else None

    pinned_nf = None
    if args.shape_devices:
        sys.path.insert(0, _HERE)
        from select_shape_devices import mos_devices  # noqa: E402
        known = mos_devices(args.netlist)
        if os.path.isfile(args.shape_devices):
            sel_doc = json.load(open(args.shape_devices))
            selected = sel_doc.get("shape_devices") or []
        else:
            selected = [d.strip() for d in args.shape_devices.split(",") if d.strip()]
        unknown = [d for d in selected if d not in known]
        if unknown:
            sys.exit(f"error: --shape-devices names non-MOS/unknown instances: {unknown}")
        if not selected:
            sys.exit("error: --shape-devices selected nothing -- nothing to sweep.")
        # Pin everything NOT selected at its own netlist nf (default 1 when the
        # netlist never set one), so the swept value reaches only the flagged
        # devices.
        pinned_nf = {n: (v if v is not None else 1)
                     for n, v in known.items() if n not in selected}
        print(f"Sweeping {len(selected)} flagged device(s): {', '.join(selected)}")
        print(f"Pinning {len(pinned_nf)} other device(s) at their netlist nf.\n")

    with contextlib.ExitStack() as stack:
        work_dir = args.work_dir
        if work_dir is None and args.save_artifacts:
            # Persisting: put it in the documented place rather than beside the
            # sizing netlist we were handed (see default_shaping_dir).
            work_dir = default_shaping_dir(args.netlist, args.cycle)
        if work_dir is None:
            work_dir = stack.enter_context(tempfile.TemporaryDirectory(prefix="check_nf_"))

        if args.nf is not None:
            result = check_nf(args.netlist, args.testbench, ideal_specs, pdk=args.pdk,
                                   nf=args.nf, drop_threshold=args.drop_threshold,
                                   ldiff_um=args.ldiff_um, work_dir=work_dir,
                                   target_spec=target_spec,
                                   parasitic_headroom=args.parasitic_headroom,
                                   pinned_nf=pinned_nf)
        else:
            nf_list = [int(x) for x in args.fingers.split(",")]
            result = sweep_nf(args.netlist, args.testbench, ideal_specs, pdk=args.pdk,
                                   nf_list=nf_list, drop_threshold=args.drop_threshold,
                                   ldiff_um=args.ldiff_um, work_dir=work_dir,
                                   target_spec=target_spec,
                                   parasitic_headroom=args.parasitic_headroom,
                                   pinned_nf=pinned_nf)
    print_report(result)

    if args.json:
        if os.path.dirname(args.json):
            os.makedirs(os.path.dirname(args.json), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nSaved: {args.json}")

    if args.report_md:
        save_report(result, args.report_md)
        print(f"Saved: {args.report_md}")

    if args.stricter_target_json:
        if write_stricter_target_spec(result, args.stricter_target_json):
            print(f"Saved: {args.stricter_target_json}")
        else:
            print("No stricter-target file written (no flagged spec had a "
                  "finite derived floor to write).")

    if args.nf_out:
        write_nf_recommendation(args.nf_out, result)
        print(f"Saved Nf recommendation into: {args.nf_out}")

    sys.exit(0 if result.get("ok") and not result.get("needs_resizing") else 1)


if __name__ == "__main__":
    main()
