#!/usr/bin/env python3
"""Run one sizing iteration: write the given W/L values into the tuning
netlist (through its structure groups), splice in the op-point probe lines
(`generate_op_probe.py`) right after the testbench's `run`, simulate,
compute the analysis's metrics (through the extractor registry in
`../../../reference/compute_fidelity.py`, not reimplemented here) and per-device
op-point data,
and compare against `target_spec.json` -- see ../SKILL.md's
"Step 2: The tuning loop".

This script does ONE iteration and returns/prints a JSON result; it does
NOT decide what to try next -- the caller reasons over this result (plus
`book_keeper.md`'s history) to pick the next iteration's W/L values. A script
measures, the caller decides.

Also computes total DC power (`specs["power_mw"]`, via
`compute_power.py`'s `compute_total_power_mw()`, from the SAME op-point
data already probed for `id` -- no extra simulation) whenever `id` is
among `--attrs` (the default). See ../general/power_measurement.md.

"Target met" convention (documented, overridable): each key is either a
FLOOR (bigger is better, `sim_value >= target_value`) or a CEILING
(smaller is better, `sim_value <= target_value`, listed in
`CEILING_METRICS`) -- direction is per-key, not universal. Today the
floors are Gain dB / UGBW MHz / PM deg and the only ceiling is Power mW.

**Which keys exist is read from `target_spec.json`, not assumed here.**
`check_target()` resolves each key through `METRIC_MAP` and then through
`specs` itself, so a design specified against something this project has
not met before needs no edit to this file -- only a `specs` entry under
that key's own name. A key that resolves to neither is reported
UNMEASURED (`met: None`, plus an `unmeasured` list on the result), never
scored as a missed target: sizing can close a gap, but it cannot invent a
measurement, and conflating the two burns the whole iteration budget
reporting NOT MET without ever naming the cause.

Usage:
  python run_sizing_iteration.py <design_dir> <design>_tuning.sp <testbench.spice>
      --netlist-basename two_stage_rz.sp --groups structure_groups.json
      [--set MN1_W=45.2,MN1_L=0.2] --pdk sky130A --iter 3
      [--target-spec target_spec.json] [--attrs gm,gds,id,vds,vgs,vdsat,cgg]
      [--json out.json]
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "..", "..", "reference"))
from compute_fidelity import metrics_from  # noqa: E402

from generate_op_probe import detect_hier_prefix, build_probe_lines  # noqa: E402
from edit_netlist import (apply_values, read_values, check_groups,  # noqa: E402
                          load_groups)
from setup_sizing import width_bounds  # noqa: E402
from compute_power import (compute_total_power_mw, extract_vdd,  # noqa: E402
                            extract_supply_volts)

# metrics compared as `sim <= target` (smaller is better) -- every other
# tracked metric is a floor (`sim >= target`, bigger is better). See
# check_target()'s "Target met" convention.
CEILING_METRICS = {"Power"}

# How a target_spec.json key finds its simulated value:
#     <spec key> -> (key in `specs`, factor converting sim unit -> spec unit)
#
# This is DATA, and it is not the only route -- see check_target(), which
# falls back to `specs[<spec key>]` for any key not listed here. So a new
# circuit class does not need an entry: a measurement stored in `specs`
# under the spec's own name (`specs["Fosc"] = ...`) is picked up with no
# edit to this file at all. The table exists only for the keys whose sim
# name differs from their spec name, or whose units differ.
#
# It is deliberately NOT a statement that these four are the measurable
# set. They are the ones an AC sweep plus the op-point probe happen to
# produce, which is why they are all this project has needed so far --
# an amplifier's. A key with neither an entry here nor a value in `specs`
# is reported UNMEASURED, never silently scored as missed.
# `metrics_from()` returns metrics under their SPEC-file names (Gain/UGBW/PM);
# this script's `specs` dict has historically used simulator-side names, and
# METRIC_MAP below maps spec key -> that name. Translate once here so both
# spellings keep working and neither file has to change.
SPEC_KEY_ALIASES = {
    "Gain": "gain_dB",
    "UGBW": "ugb_Hz",
    "PM": "pm_deg",
}

METRIC_MAP = {
    "Gain":  ("gain_dB",  1.0),
    "UGBW":  ("ugb_Hz",   1e-6),   # simulated in Hz, spec'd in MHz
    "PM":    ("pm_deg",   1.0),
    "Power": ("power_mw", 1.0),
}

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


def check_param_bounds(netlist_text, groups, pdk, pdk_root=None):
    """Refuse geometry outside the PDK's model bins, BEFORE it costs an
    iteration.

    The ceiling is read from the PDK's own model cards at the moment of the
    check (`setup_sizing.width_bounds`) -- nothing is cached, so there is no
    stored bound to fall out of step with the netlist or the PDK.

    A width past that ceiling resolves to no model card, so the run produces no
    raw file and reports `no AC .out file produced` -- a message that names
    neither the device nor the ceiling, spent from a budget. Raising here turns
    that into a named refusal that costs nothing.

    Deliberately a hard stop rather than a clamp: silently rewriting a proposal
    would report results for geometry the caller did not choose, and the caller
    is the one who has to decide whether to lower `w` or raise `m`."""
    bounds = width_bounds(netlist_text, groups, pdk_root=pdk_root, pdk=pdk)
    if not bounds:
        return
    values = read_values(netlist_text, groups)
    bad = []
    for var, spec in bounds.items():
        val, mx = values.get(var), spec.get("max")
        if isinstance(val, (int, float)) and mx and val > mx:
            need = int(-(-val // mx))
            bad.append(f"  {var}={val:g} > {mx:g}{spec.get('unit', '')} per copy"
                       f" -- same total needs w={val / need:g} with m={need}"
                       f"  ({spec.get('reason', '')})")
    if bad:
        raise SystemExit(
            "proposed geometry is outside the PDK's model bins, so this "
            "iteration could not simulate:\n" + "\n".join(bad) +
            "\nLower w, or raise m to carry the total -- nf does not divide "
            "the width the model bins on.")


def repoint_netlist_include(testbench_text, netlist_basename):
    """Re-point the deck's `.include` of the netlist to a bare basename.

    This iteration renders the netlist as a SIBLING of the deck inside the
    iteration directory and runs ngspice with that directory as cwd -- so
    any path component in the include no longer resolves. It is not a
    hypothetical: a design folder files the netlist and the testbench into
    separate subfolders, so the deck's include reads `../netlist/<design>.sp`
    -- correct there and fatal here. Without this, EVERY such design dies on
    iteration 1 with "Could not find include file", naming a path that is
    right for the design folder and wrong for this one.

    Only the include naming THIS netlist is touched. A PDK `.lib` line, or
    an include of some other file, is left exactly as written -- those
    resolve by absolute path or are not this function's business.
    """
    pattern = re.compile(
        r'^(\s*\.include\s+)(["\']?)([^"\'\s]*)(["\']?)\s*$',
        re.MULTILINE | re.IGNORECASE)

    def _sub(m):
        lead, q1, path, q2 = m.groups()
        if os.path.basename(path) != netlist_basename:
            return m.group(0)
        return f"{lead}{q1}{netlist_basename}{q2}"

    return pattern.sub(_sub, testbench_text)


def augment_testbench(testbench_text, probe_lines):
    probe_block = "\n".join(probe_lines)
    new_text, n = re.subn(r'(\n[ \t]*run[ \t]*\n)', r'\1' + probe_block + '\n',
                           testbench_text, count=1)
    if n == 0:
        raise ValueError("no bare 'run' line found in the testbench's .control "
                          "block to splice op-probe lines after")
    return new_text


def find_write_out(testbench_text):
    m = re.search(r'^\s*write\s+\S*?([^/\\\s]+\.out)\s+v\((\S+)\)',
                   testbench_text, re.MULTILINE | re.IGNORECASE)
    if not m:
        raise ValueError("no 'write ... v(<node>)' line found in the testbench")
    return m.group(1), m.group(2)


def parse_opinfo(path):
    result = {}
    for line in open(path):
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        try:
            result[k.strip()] = float(v.strip())
        except ValueError:
            continue
    return result


# Sim-unit -> spec-unit factors, keyed by the name a measurement is stored
# under in `specs`. This is what makes `target_spec.json`'s own `Units` field
# load-bearing rather than decorative: a spec that says `"Units": "Hz"` is
# compared in Hz, one that says `"MHz"` in MHz, off the SAME simulated value.
# Before this existed the factor was fixed per key in METRIC_MAP, so a spec
# writing UGBW in Hz was silently scored against an MHz conversion.
#
# A unit this table does not know is not an error: the factor falls back to
# METRIC_MAP's, and check_target() records `unit_note` so the assumption is
# visible instead of buried.
_UNIT_SCALE = {
    "ugb_Hz":   {"hz": 1.0, "khz": 1e-3, "mhz": 1e-6, "ghz": 1e-9},
    "power_mw": {"mw": 1.0, "w": 1e-3, "uw": 1e3, "\u00b5w": 1e3, "nw": 1e6},
    "gain_dB":  {"db": 1.0},
    "pm_deg":   {"degree": 1.0, "degrees": 1.0, "deg": 1.0},
}


def parse_spec_entry(key, entry):
    """One `target_spec.json` entry -> `(direction, value, units)`.

    `direction` is `FLOOR`, `CEILING` or `RANGE`; `value` is a number for
    the first two and a `(lo, hi)` pair for `RANGE`; `units` is the
    declared unit string, or None when the file does not carry one.

    **Two forms are accepted, and the direction comes from the file
    whenever the file states one.** The structured form
    carries `Direction`/`Value`/`Units` per key and is authoritative -- no key's
    direction is guessed when it is declared. The older flat form
    (`{"Gain": 40}`) states neither, so it falls back to `CEILING_METRICS`
    for direction and to METRIC_MAP's fixed factor for units. That
    fallback is the ONLY thing CEILING_METRICS is still for: it is a
    default for specs that predate the template, not a second source of
    truth competing with it.

    Raises ValueError on a structured entry whose shape is wrong -- a
    malformed spec is caught and handed back to the user upstream, so
    reaching here with one means a gate was skipped, and guessing past it
    would score a key nobody defined.
    """
    if isinstance(entry, dict):
        direction = str(entry.get("Direction", "")).strip().upper()
        if direction not in ("FLOOR", "CEILING", "RANGE"):
            raise ValueError(
                f"target_spec key {key!r}: Direction must be FLOOR, CEILING or "
                f"RANGE, got {entry.get('Direction')!r} -- run "
                f"spec-form validation on this file")
        value = entry.get("Value")
        if direction == "RANGE":
            if (not isinstance(value, (list, tuple)) or len(value) != 2
                    or not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                               for v in value) or not value[0] < value[1]):
                raise ValueError(
                    f"target_spec key {key!r}: a RANGE Value must be "
                    f"[min, max] with min < max, got {value!r}")
            value = (float(value[0]), float(value[1]))
        else:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(
                    f"target_spec key {key!r}: a {direction} Value must be a "
                    f"number, got {value!r}")
            value = float(value)
        units = entry.get("Units")
        return direction, value, (str(units) if units else None)

    if isinstance(entry, (int, float)) and not isinstance(entry, bool):
        return ("CEILING" if key in CEILING_METRICS else "FLOOR"), float(entry), None

    raise ValueError(f"target_spec key {key!r}: expected a number or a "
                      f"{{Direction, Value, Units}} object, got {type(entry).__name__}")


def resolve_metric(key, specs, units=None):
    """Find the simulated value for a target_spec key, in the SPEC's unit.

    The keys are read off `target_spec` -- nothing here assumes which
    metrics a design is specified against. Resolution order:

      1. `METRIC_MAP[key]`  -- for keys whose simulated name or unit
         differs from the spec's (`UGBW` is `ugb_Hz`, in Hz not MHz).
      2. `specs[key]`       -- any measurement stored under the spec's own
         name, same unit, no table entry needed. This is the extension
         path: produce `specs["Fosc"]` and an `Fosc` target just works.
      3. neither            -- UNMEASURED. Returns None, and the caller
         must NOT report that as a missed target.

    Returns `(value_in_spec_units, measurable)`. `measurable` False means
    nothing in this run could have produced the key; `measurable` True
    with a `None` value means the measurement exists but this particular
    run didn't yield it (an AC sweep with no 0 dB crossing gives
    `ugb_Hz = None`), which IS a real miss.
    """
    if key in METRIC_MAP:
        sim_key, scale = METRIC_MAP[key]
        if sim_key not in specs:
            return None, False
        if units:
            table = _UNIT_SCALE.get(sim_key) or {}
            declared = table.get(units.strip().lower())
            if declared is not None:
                scale = declared
        val = specs[sim_key]
        return (None if val is None else float(val) * scale), True
    if key in specs:
        val = specs[key]
        return (None if val is None else float(val)), True
    return None, False


def check_target(specs, target_spec):
    """`sim_value >= target_value` (floor) for every key present in
    target_spec, EXCEPT keys in `CEILING_METRICS` (currently just
    `Power`), which flip to `sim_value <= target_value` -- see module
    docstring's 'Target met' convention.

    **Keys come from target_spec, and a key this run cannot measure is
    reported as such rather than scored.** Its `met` is `None` (not
    `False`) and its name is listed under `unmeasured`. The distinction is
    the whole point: `met=False` means "measured, missed, size harder",
    while an unmeasurable key means no amount of sizing will ever move it
    and the run should stop and say so. Scoring the two the same way is
    what let an unrecognized key report NOT MET on every iteration until
    the budget ran out, with nothing anywhere naming the reason.

    `all_met` stays False whenever anything is unmeasured, so no caller
    can read this as success; `unmeasured` is what tells them why.

    **Direction comes from the spec file when the file states one.** Each
    entry goes through `parse_spec_entry()`, so a structured
    `{Direction, Value, Units}` key is scored exactly as declared --
    including `RANGE`, met when `Value[0] <= sim <= Value[1]`. A flat
    numeric key predates that form and falls back to `CEILING_METRICS`.

    Unit conversion happens in `resolve_metric()`, driven by the entry's
    own `Units` where the file states one and by `METRIC_MAP`'s fixed
    factor where it does not. `specs["ugb_Hz"]` is raw Hz while a spec
    typically writes MHz, and comparing raw Hz against an MHz target is
    trivially always true (ugb_Hz is in the tens of millions), so the
    conversion is not cosmetic -- which is also why a spec that writes
    `"Units": "Hz"` must NOT be scaled by 1e-6 the way an MHz one is.
    """
    if not target_spec:
        return None
    per_spec = {}
    unmeasured = []
    for key, entry in target_spec.items():
        direction, target_val, units = parse_spec_entry(key, entry)
        sim_val, measurable = resolve_metric(key, specs, units=units)
        ceiling = direction == "CEILING"
        if not measurable:
            unmeasured.append(key)
            met = None
        elif sim_val is None:
            met = False
        elif direction == "RANGE":
            met = bool(target_val[0] <= sim_val <= target_val[1])
        elif ceiling:
            met = bool(sim_val <= target_val)
        else:
            met = bool(sim_val >= target_val)
        per_spec[key] = {"target": (list(target_val) if direction == "RANGE"
                                    else target_val),
                          "sim": None if sim_val is None else float(sim_val),
                          "met": met, "ceiling": ceiling,
                          "direction": direction, "units": units,
                          "measurable": measurable}
    all_met = all(v["met"] is True for v in per_spec.values())
    out = {"per_spec": per_spec, "all_met": all_met}
    if unmeasured:
        out["unmeasured"] = unmeasured
        out["unmeasured_note"] = (
            "no measurement is produced for " + ", ".join(unmeasured)
            + " -- this is not a missed target and sizing cannot move it. "
              "Either the spec key is outside what this loop measures -- "
              "it tracks frequency-response and bias-point keys only -- or "
              "the testbench never saved the data it needs. Stop and resolve "
              "it rather than iterating.")
    return out


def run_iteration(design_dir, tuning_netlist, testbench_path, netlist_basename,
                   values, groups_path, pdk, iter_tag, attrs, target_spec=None,
                   out_root=None, timeout=120, save_artifacts=False,
                   supply_nets=("VDD",), vdd_value=None, analysis="ac",
                   supply_volts=None, pdk_root=None):
    """Run one iteration.

    **`tuning_netlist` is both the input and the state.** `values` (a
    `--set`-style `{var: value}`) is written INTO it through the structure
    groups before the run, so after this returns the file on disk is exactly
    what simulated. There is no separate value store, and therefore nothing
    that can disagree with the netlist.

    **`save_artifacts=False` (the default) leaves nothing on disk.** The
    simulation runs in a temp directory, auto-deleted on return, and the
    iteration's record is the returned `result` dict -- which the caller writes
    into `book_keeper.md` (../SKILL.md's "Logging"). That is the run's history;
    these files are not.

    `save_artifacts=True` persists the rendered netlist, the augmented deck and
    `opinfo.txt` to `<out_root>/iter_<iter_tag>/`, for an iteration worth
    debugging afterwards. **`out_root` defaults to
    `<design_dir>/sizing/debug`** -- a folder that exists only
    because someone asked for it. Pass `out_root` only to put those files
    somewhere else deliberately.

    The returned `result` is identical either way; only the on-disk side effect
    changes. `--json` saves that one result file regardless of this flag."""
    if save_artifacts:
        out_root = out_root or os.path.join(design_dir, "sizing",
                                            "debug")
        iter_dir = os.path.join(out_root, f"iter_{iter_tag}")
        os.makedirs(iter_dir, exist_ok=True)
        return _run_iteration_in_dir(iter_dir, tuning_netlist, testbench_path,
                                      netlist_basename, values, groups_path,
                                      pdk, iter_tag, attrs, target_spec, timeout,
                                      supply_nets, vdd_value, analysis,
                                      supply_volts, pdk_root)
    with tempfile.TemporaryDirectory(prefix=f"sizing_iter{iter_tag}_") as tmp_dir:
        return _run_iteration_in_dir(tmp_dir, tuning_netlist, testbench_path,
                                      netlist_basename, values, groups_path,
                                      pdk, iter_tag, attrs, target_spec, timeout,
                                      supply_nets, vdd_value, analysis,
                                      supply_volts, pdk_root)


def _run_iteration_in_dir(iter_dir, tuning_netlist, testbench_path, netlist_basename,
                           values, groups_path, pdk, iter_tag, attrs, target_spec,
                           timeout, supply_nets=("VDD",), vdd_value=None,
                           analysis="ac", supply_volts=None, pdk_root=None):
    groups, fixed = load_groups(groups_path)
    text = open(tuning_netlist).read()

    # A group whose members already disagree is a hand edit that broke a
    # symmetry -- report it rather than measuring the broken design.
    desync = check_groups(text, groups)
    if desync:
        raise SystemExit(
            "the tuning netlist has desynchronised groups, so it is not the "
            "design the variables describe:\n" +
            "\n".join(f"  {v}: " + ", ".join(f"{k}={x:g}" for k, x in seen.items())
                       for v, seen in desync) +
            "\nSet each variable to re-synchronise its members before iterating.")

    if values:
        text, _ = apply_values(text, values, groups, fixed)
        with open(tuning_netlist, "w") as f:   # the netlist IS the state
            f.write(text)

    check_param_bounds(text, groups, pdk, pdk_root=pdk_root)
    rendered = text
    netlist_path = os.path.join(iter_dir, netlist_basename)
    with open(netlist_path, "w") as f:
        f.write(rendered)

    testbench_text = repoint_netlist_include(open(testbench_path).read(),
                                             netlist_basename)
    hier_prefix = detect_hier_prefix(testbench_text)
    opinfo_name = f"opinfo_iter{iter_tag}.txt"
    probe_lines, mos_names = build_probe_lines(netlist_path, pdk, hier_prefix,
                                                attrs, opinfo_name)
    augmented_text = augment_testbench(testbench_text, probe_lines)
    out_filename, out_node = find_write_out(augmented_text)

    testbench_basename = os.path.basename(testbench_path)
    iter_testbench_path = os.path.join(iter_dir, testbench_basename)
    with open(iter_testbench_path, "w") as f:
        f.write(augmented_text)

    proc = subprocess.run(["ngspice", "-b", testbench_basename], cwd=iter_dir,
                           capture_output=True, text=True, timeout=timeout)

    result = {
        "iter": iter_tag, "params": read_values(rendered, groups),
        "mos_devices": mos_names,
        "ngspice_returncode": proc.returncode,
    }

    out_path = os.path.join(iter_dir, out_filename)
    opinfo_path = os.path.join(iter_dir, opinfo_name)
    if not os.path.isfile(out_path):
        result["ok"] = False
        result["error"] = ("the deck's `write` produced no raw file at %s -- "
                            "check the .control block's own filename"
                            % out_filename)
        result["ngspice_stderr_tail"] = "\n".join(proc.stderr.splitlines()[-15:])
        result["ngspice_stdout_tail"] = "\n".join(proc.stdout.splitlines()[-15:])
        return result

    # Extract via the KEYED registry, not the fixed AC triple. `analysis`
    # names which extractor reads this design's raw file, so a design measured
    # by something other than an AC sweep is a registry entry
    # (compute_fidelity.EXTRACTORS) rather than a fork of this script. A raw
    # file the chosen extractor cannot read is REPORTED, never fatal: the
    # op-point data below is measured by a different mechanism and is still
    # good, and killing the iteration would throw it away too.
    specs = {}
    try:
        measured = metrics_from(out_path, analysis=analysis)
        for key, val in measured.items():
            specs[SPEC_KEY_ALIASES.get(key, key)] = (
                None if val is None else float(val))
    except NotImplementedError as e:
        result["extract_note"] = (
            "no extractor for analysis %r: %s" % (analysis, e))
    except (SystemExit, ValueError, KeyError, IndexError) as e:
        result["extract_note"] = (
            "analysis %r could not read %s: %s. Op-point data below is "
            "unaffected." % (analysis, out_filename, e))

    op_points = parse_opinfo(opinfo_path) if os.path.isfile(opinfo_path) else {}

    # Power is ONE key among several, so failing to identify the supply must
    # not lose the AC specs measured in the same run. Record why it is absent
    # (`power_note`) and leave `power_mw` out -- check_target() then lists a
    # power-like key under `unmeasured` rather than scoring it as missed:
    # record, don't crash.
    if "id" in attrs and op_points:
        try:
            # One voltage per rail, each read from the deck's own DC source --
            # a single scalar times the summed current of several rails is
            # wrong by however much they differ. An explicit --vdd is the
            # caller overriding that, and applies to every named net.
            # Resolution order, most specific first:
            #   1. a PER-RAIL dict from the caller -- what a generated runner
            #      carries in its CONFIG, each rail at its own volts;
            #   2. an explicit scalar override, applied to every named net;
            #   3. read the deck here.
            # 1 ahead of 2 is the whole point on a split-rail design: collapse
            # the dict to one number and a 1.8V rail and a 3.3V rail are both
            # summed at whichever value survived.
            if supply_volts:
                supply_volts = {n.upper(): float(v)
                                for n, v in supply_volts.items()}
            elif vdd_value is not None:
                supply_volts = {n.upper(): vdd_value for n in supply_nets}
            else:
                supply_volts = extract_supply_volts(testbench_text, supply_nets)
            power_mw, per_dev = compute_total_power_mw(
                rendered, op_points, supply_nets=supply_nets,
                supply_volts=supply_volts)
            if per_dev:
                specs["power_mw"] = power_mw
            else:
                # LEAVE power_mw OUT rather than reporting the 0.0 that an
                # unmatched supply net produces. `Power` is a CEILING metric,
                # so a 0.0 clears any budget: check_target() would score it
                # MET, all_met would go True, and the loop would converge on a
                # rail nobody identified. Absent, it lands in `unmeasured`,
                # all_met stays False, and the note below names the cause.
                result["power_note"] = (
                    "no device draws through %s -- power is NOT measured this "
                    "iteration (reported as unmeasured, not as 0.0 mW); pass "
                    "--vdd-net with this design's own supply net(s)"
                    % (list(supply_nets),))
        except ValueError as e:
            result["power_note"] = (
                "power not computed: %s. Every other spec in this result is "
                "unaffected." % e)

    # `ok` means "this iteration produced usable measurements", NOT "the
    # circuit has a 0 dB crossing". Those were the same thing only while every
    # design was an amplifier: an oscillator or a reference has no crossing, so
    # the old definition reported ok=False on every iteration however good the
    # sizing was. Whether a spec is MET is check_target()'s answer, and a key
    # that came back None lands in its `unmeasured` list -- which still keeps
    # `all_met` False, so an amplifier whose sweep found no crossing cannot
    # falsely converge.
    result["ok"] = any(v is not None for v in specs.values())
    if not result["ok"]:
        result["error"] = ("no spec could be measured from %s -- see "
                            "extract_note" % out_filename)
    result["specs"] = specs
    result["op_points"] = op_points
    result["target"] = check_target(specs, target_spec)
    return result


def _parse_set_arg(s):
    """`--set VAR=VAL,...` -> `{var: float}`. Empty when nothing was passed --
    an iteration with no `--set` re-measures the netlist exactly as it stands."""
    out = {}
    for pair in (s or "").split(","):
        name, _, val = pair.partition("=")
        if name.strip() and val:
            try:
                out[name.strip()] = float(val)
            except ValueError:
                raise SystemExit(f"--set {pair.strip()}: {val!r} is not a number "
                                 f"(geometry is in microns; write 45, not 45u)")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design_dir")
    ap.add_argument("tuning_netlist", help="<design_name>_tuning.sp -- read AND written")
    ap.add_argument("testbench")
    ap.add_argument("--netlist-basename", required=True)
    ap.add_argument("--groups", required=True, help="structure_groups.json")
    ap.add_argument("--set", default=None, help="comma-separated VAR=VALUE overrides")
    ap.add_argument("--pdk", default=_guideline_pdk())
    ap.add_argument("--iter", required=True)
    ap.add_argument("--attrs", default="gm,gds,id,vds,vgs,vdsat,cgg")
    ap.add_argument("--analysis", default="ac",
                     help="which extractor reads this design's raw file, keyed "
                          "into compute_fidelity.EXTRACTORS. Default 'ac' "
                          "(gain/UGBW/phase margin). A design measured any other "
                          "way needs an extractor registered THERE -- not a "
                          "forked copy of this script; see ../SKILL.md's "
                          "\"What is shared and what is per-design\".")
    ap.add_argument("--vdd-net", default="VDD",
                     help="the supply net(s) this design's operating current is "
                          "drawn through, comma-separated for a multi-rail design "
                          "(e.g. VDDA,VDDD). Power sums every device whose source "
                          "sits on one of them, so a net named anything other than "
                          "the default MUST be given here or the number is an "
                          "undercount. Default: VDD.")
    ap.add_argument("--vdd", type=float, default=None,
                     help="supply voltage. Default: parsed from the testbench's own "
                          "DC source on the first --vdd-net. Pass it when the deck "
                          "sets the rail some way this parser cannot read.")
    ap.add_argument("--target-spec", default=None)
    ap.add_argument("--out-root", default=None,
                     help="where --save-artifacts writes iter_<n>/ folders. "
                          "Default: <design_dir>/sizing/debug.")
    ap.add_argument("--json", default=None)
    ap.add_argument("--save-artifacts", action="store_true",
                     help="persist this iteration's rendered netlist/"
                          "testbench/opinfo.txt under <out_root>/iter_<n>/, for "
                          "an iteration worth debugging. Default is a temp dir, "
                          "auto-deleted on exit -- the run's history lives in "
                          "book_keeper.md, not in these files. The result is "
                          "unaffected either way.")
    args = ap.parse_args()

    values = _parse_set_arg(args.set)
    attrs = [a.strip() for a in args.attrs.split(",") if a.strip()]
    target_spec = json.load(open(args.target_spec)) if args.target_spec else None

    try:
        result = run_iteration(
            args.design_dir, args.tuning_netlist, args.testbench,
            args.netlist_basename, values, args.groups,
            args.pdk, args.iter, attrs, target_spec=target_spec,
            out_root=args.out_root, save_artifacts=args.save_artifacts,
            supply_nets=tuple(n.strip() for n in args.vdd_net.split(",") if n.strip()),
            vdd_value=args.vdd, analysis=args.analysis)
    except (ValueError, subprocess.TimeoutExpired) as e:
        result = {"iter": args.iter, "ok": False, "error": str(e)}

    print(json.dumps(result, indent=2))
    if args.json:
        if os.path.dirname(args.json):
            os.makedirs(os.path.dirname(args.json), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump(result, f, indent=2)

    sys.exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
