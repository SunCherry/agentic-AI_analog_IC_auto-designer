#!/usr/bin/env python3
"""Set up a design for sizing: write the loop's working netlist and the
structure-group map it is edited through -- see ../SKILL.md's Step 1.

**No template, and no value file.** The working netlist IS the state: it is a
plain, always-readable `.sp`, and `edit_netlist.py` writes tunable values
straight into it. What this step works out is which devices must move TOGETHER,
and what each variable should start at.

Tunable parameters, deliberately narrow: **W and L only**, for MOS/resistor/cap
devices, PLUS one deliberate exception -- a mirror BRANCH's `m` (see
"Current-mirror family detection" below), never a mirror REFERENCE's `m`
(pinned to 1 in the group file's `fixed`) and never any other device's `m`/`nf`
(multiplicity/finger-count stay a layout-only lever). Not resistor/cap values
directly (this project sizes R/C via W/L, matching how the golden netlist
already writes them). Never ideal sources (`ibias`, `iprobe`, the AC stimulus,
supplies) -- not PDK devices, so there is nothing to sweep to a
layout-realizable geometry.

Variable naming: the instance name with a leading `X` stripped, suffixed
`_W`/`_L`/`_M` (`XMN1` -> `MN1_W`). A group of one is still a group, so every
`--set` resolves the same way.

**Current-mirror family detection (MOS only)** -- runs BEFORE, and takes
priority over, matched-pair grouping (below): a MOS device is
diode-connected if its own `drain` net equals its `gate` net -- the
textbook current-mirror reference configuration. Every OTHER MOS device
sharing that same (model, gate net, source net, bulk net) is a mirrored
BRANCH of that reference. When a gate node has exactly one diode-connected
member and at least one branch, that's a family:
  - The reference and every branch share ONE `{{ <ref>_W }}`/
    `{{ <ref>_L }}` pair (named after the REFERENCE's own prefix) --
    every finger across the whole family is the same physical unit
    device, the standard layout technique for best current-mirror
    matching (see the user-facing rationale: "these devices' single
    finger are equally in width").
  - The reference's `m` is forced to the literal `1` (never a template
    var) -- it's the one-copy "unit" the rest of the family scales from.
  - Each BRANCH's `m` becomes ITS OWN independent tunable var
    (`{{ <branch>_M }}`, suffix `_M`) -- the mirror RATIO is what
    actually varies branch-to-branch in a real design, not the unit
    device's own geometry, so `m` (an integer count of unit copies) is
    the physically correct lever, not an independently-sized `w`.
    **The token is ADDED when the golden line has no `m=`** (implicit
    `m=1`), not skipped: the branch's own `w=` has just become the
    family's shared per-copy width, so a branch left with one implicit
    copy renders at the REFERENCE's total and its whole ratio is gone --
    silently, since the family report reads the computed starting `M`
    rather than the rendered line.
  - **Starting values**: `{{ <ref>_W }}` seeds from the reference's own
    *original per-copy* `w` alone (never `w * m` -- confirmed empirically
    necessary: seeding from the reference's original TOTAL width can push
    a single now-`m=1` instance outside this PDK's valid BSIM4 model-bin
    width range, a real ngspice failure this project's own
    a reference design hit on `XMN4`; the per-copy `w` alone is
    guaranteed already-valid, since it's the exact same per-instance
    width the golden netlist's own `m` copies already relied on) and
    `{{ <ref>_L }}` from its original `l`. Each branch's starting `_M` =
    `max(1, round((branch's original total width / reference's original
    total width)))` -- i.e. the golden netlist's own branch-to-reference
    RATIO, floored at 1 -- **not** the branch's original total divided by
    the new (generally smaller) seed W directly, which was a real bug
    (found by actually simulating it, not just an approximation choice):
    dividing by the seed alone ignores how much the reference itself just
    shrank when its own `m` was forced to 1, systematically
    over-multiplying every branch by that same shrink factor and breaking
    the original ratio outright (confirmed on a reference design:
    this bug pushed the whole bias network into a non-functional
    operating point, not just a shifted one). Preserving the ratio
    directly is still only a non-degenerate (never < 1) starting guess,
    not a guarantee -- the reference's own total sizing generally does
    NOT match its original state once `m` is forced to 1, so the tuning
    loop still needs to refine the shared `W` from there, same as
    every other starting value this script produces.
  - This is purely topological (drain==gate identifies the reference),
    unlike matched-pair grouping's value-matching -- it does NOT require
    the family's original `w`/`l`/`m` to already match, since that's
    exactly the gap this detector exists to close (see this project's own
    a reference design: `XMN4` is the bias reference, `XMN3`/`XMN5`
    are its mirrored branches, but all three originally have different
    `w` AND `m` -- the OLD matched-pair heuristic below, which requires
    identical `w`/`l`/`m`/`nf`, would never group them).
  - A gate node with zero or more than one diode-connected member is left
    alone (ambiguous -- not a clean single-reference mirror) and falls
    through to matched-pair grouping instead. Pass `--no-auto-mirror` to
    disable this detector entirely.

**Matched-pair grouping (MOS only, for devices NOT already claimed by a
mirror family above)**: a naive per-instance template silently breaks a
differential pair the first time an independent tuning loop moves one
half's W/L without the other -- see
../set_tunable_params.md for the failure mode
this caused in this project's own first test run. Two (or more) MOS devices are grouped onto ONE shared
`{{ <name>_W }}`/`{{ <name>_L }}` pair (named after the first-encountered
device) when they share: the same model, the same
`source` AND `bulk` net, and identical original `w`/`l`/`m`/`nf` values.
This one connectivity+value signature is what catches a **differential
pair** (e.g. `XMN1`/`XMN2`): different gate (the two inputs) AND
different drain (the two load-branch nets) -- only `source`/`bulk` (the
shared tail/rail node) match; `m`/`nf` are never templated for these,
only `w`/`l` (both halves' `m` stays fixed at its original, identical,
value). This is a heuristic, not
a certainty -- it can over-group two independent devices that
coincidentally share size and rail connections, or under-group an exotic
topology it doesn't recognize. Every detected group is printed for
review; pass `--no-auto-group` to disable it entirely and fall back to
one independent variable per instance.

Usage:
  python setup_sizing.py <golden.sp> -o <design>_tuning.sp
      --groups-out structure_groups.json [--no-auto-group] [--no-auto-mirror]
"""
import argparse
import json
import os
import re
import sys

from netlist_devices import parse_devices
from edit_netlist import apply_values

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "..", "parasitic-estimation", "script"))
from estimate_parasitics import _si_val, _um_val  # noqa: E402


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


def _um(raw):
    """A drawn dimension (`w`/`l`) in MICRONS, whatever the netlist wrote.

    **Every geometry value this module stores or compares goes through here.**
    Real netlists reaching this project write drawn size three ways -- bare
    microns (`w=40.7`), an SI suffix (`w=4u`) and SI metres (`w=21.0e-7`, seen
    on this project's own `ahuja_ota_4.sp`) -- and `_si_val` faithfully returns
    a different SCALE for each: `4u` becomes 4e-06 while `40` stays 40.

    Storing that raw mixture is silent corruption downstream, not a cosmetic
    inconsistency:
      - `finalize_netlist.round_tunables()` rounds to 2 decimals, so a stored
        4e-06 renders as `w=0.0` -- a zero-geometry device, exit code 0.
      - `_bounds` is in um, so `check_param_bounds` compares 4e-06 against a
        100um ceiling and never fires.
      - a mirror's branch/reference ratio is computed from two totals; mix the
        conventions across a family and the ratio is off by 1e6.
      - `--set MN1_W=45` has no defined meaning if the stored scale varies.

    `_um_val`'s heuristic (imported, not re-implemented, so there is ONE
    rule): anything at or below 1e-3 is metres and is scaled by 1e6, anything above
    is already microns. 1e-3 um is 1 nm -- below every process minimum, so no
    real drawn dimension is ambiguous.

    Counts (`m`, `nf`) must NOT come through here -- they are dimensionless."""
    return _um_val(raw)

TUNABLE_KINDS = ("mos", "res", "cap")
TUNABLE_PARAMS = ("w", "l")
GROUPABLE_KINDS = ("mos",)
MOS_TERMINALS = ("drain", "gate", "source", "bulk")


def _var_prefix(name):
    return name[1:] if name[:1].upper() == "X" else name


def _param_lookup(params, key):
    """Case-insensitive lookup into a device's params dict; returns
    (actual_key, value_str) or (None, None)."""
    for k, v in params.items():
        if k.lower() == key:
            return k, v
    return None, None


def _match_key(d):
    """MOS-only grouping key -- see module docstring's 'Matched-pair
    grouping' section for exactly what this does and doesn't catch.
    Returns None if this device can't be safely keyed (fewer than 4
    terminals resolved, e.g. an unusual MOS line)."""
    if len(d["nets"]) < 4:
        return None
    _, _, source, bulk = d["nets"][:4]

    def _numeric(key):
        actual_key, raw_val = _param_lookup(d["params"], key)
        if actual_key is None:
            return None
        # w/l are dimensions -> microns; m/nf are counts -> as written. Without
        # this, two identical devices written `w=4u` and `w=4` compare unequal
        # and the group is silently not formed.
        return _um(raw_val) if key in ("w", "l") else _si_val(raw_val)

    return (d["model"].lower(), source, bulk,
            _numeric("w"), _numeric("l"), _numeric("m"), _numeric("nf"))


def _build_groups(devices, exclude=frozenset()):
    """Return {device_name: shared_var_prefix} for every groupable
    device, plus a list of (shared_prefix, [member_names]) for groups
    with 2+ members, for reporting. `exclude` (device names already
    claimed by a mirror family, see _find_mirror_families()) are skipped
    entirely -- a mirror family's own W/L/M handling takes priority and
    must not be re-grouped here too."""
    by_key = {}
    for d in devices:
        if d["kind"] not in GROUPABLE_KINDS or d["name"] in exclude:
            continue
        key = _match_key(d)
        if key is None:
            continue
        by_key.setdefault(key, []).append(d)

    prefix_for = {}
    reported_groups = []
    for key, members in by_key.items():
        members.sort(key=lambda d: d["lineno"])
        shared_prefix = _var_prefix(members[0]["name"])
        for d in members:
            prefix_for[d["name"]] = shared_prefix
        if len(members) > 1:
            reported_groups.append((shared_prefix, [d["name"] for d in members]))
    return prefix_for, reported_groups


def _is_diode_connected(d):
    """A MOS device is diode-connected if its own drain net equals its
    gate net -- the textbook current-mirror reference configuration."""
    if len(d["nets"]) < 4:
        return False
    drain, gate = d["nets"][0], d["nets"][1]
    return drain == gate


def _find_mirror_families(devices):
    """Identify current-mirror families -- see module docstring's
    'Current-mirror family detection' for the full rationale. Returns a
    list of dicts: {"reference": device, "branches": [device, ...]},
    sorted by the reference's lineno. Purely topological (drain==gate
    identifies the reference); does NOT require the family's original
    w/l/m to already match."""
    by_node = {}
    for d in devices:
        if d["kind"] != "mos" or len(d["nets"]) < 4:
            continue
        drain, gate, source, bulk = d["nets"][:4]
        key = (d["model"].lower(), gate, source, bulk)
        by_node.setdefault(key, []).append(d)

    families = []
    for members in by_node.values():
        if len(members) < 2:
            continue
        diode_connected = [d for d in members if _is_diode_connected(d)]
        if len(diode_connected) != 1:
            continue  # ambiguous or no clean single reference -- skip
        reference = diode_connected[0]
        branches = [d for d in members if d["name"] != reference["name"]]
        branches.sort(key=lambda d: d["lineno"])
        families.append({"reference": reference, "branches": branches})
    families.sort(key=lambda f: f["reference"]["lineno"])
    return families


def _mirror_family_starting_values(family):
    """Compute the shared reference W/L seed and each branch's starting
    integer M -- see module docstring's 'Starting values' for the exact
    rule and why it's a starting guess, not a ratio-preserving
    guarantee.

    Seeds `{{ <ref>_W }}` from the reference's own ORIGINAL PER-COPY `w`
    alone (ignoring its original `m`), never `w * m` -- confirmed
    empirically necessary, not just simpler: seeding from the reference's
    original TOTAL width (`w * m`) can push a single now-`m=1` instance
    outside this PDK's valid BSIM4 model-bin width range (a real ngspice
    "could not find a valid modelname" failure on this project's own
    a reference design -- `XMN4`'s original `w=58.8, m=4.0` seeded a
    single `w=235.2` instance that has no valid bin). The reference's own
    per-copy `w` is guaranteed already-valid -- it's the exact same
    per-instance width the golden netlist's own `m` copies already relied
    on -- so this is the safe seed, even though it means the reference's
    own total sizing generally does NOT match its original state once
    `m` is forced to 1 (the tuning loop corrects for that, same as
    every other starting value here).

    Each branch's starting `_M` preserves the golden netlist's own
    **branch-to-reference TOTAL WIDTH RATIO** (`(br_w*br_m) / (ref_w*ref_m)`),
    NOT the branch's own original total width divided directly by the
    (generally smaller) seed W -- confirmed a real bug, not just an
    approximation choice, by actually simulating it: dividing by the
    seed alone ignores how much the reference itself just shrank (`m`
    forced 1), so it systematically OVER-multiplies every branch by the
    same factor the reference shrank by, breaking the mirror ratio the
    golden netlist actually had (caught on a reference design: this
    bug gave `XMP2` and `XMN5` 4x/6x too many unit copies relative to
    their reference, which is exactly what pushed the whole bias network
    into a non-functional operating point -- confirmed by re-simulating
    after the fix, see
    ../set_tunable_params.md)."""
    ref = family["reference"]
    _, ref_w_raw = _param_lookup(ref["params"], "w")
    _, ref_l_raw = _param_lookup(ref["params"], "l")
    _, ref_m_raw = _param_lookup(ref["params"], "m")
    ref_w = _um(ref_w_raw) if ref_w_raw is not None else None
    ref_l = _um(ref_l_raw) if ref_l_raw is not None else None
    ref_m = _si_val(ref_m_raw) if ref_m_raw is not None else 1.0
    if ref_w is None or ref_l is None:
        return None  # reference has no w=/l= -- can't seed this family

    seed_w = ref_w
    ref_total = ref_w * ref_m
    branch_m = {}
    for br in family["branches"]:
        _, br_w_raw = _param_lookup(br["params"], "w")
        _, br_m_raw = _param_lookup(br["params"], "m")
        br_w = _um(br_w_raw) if br_w_raw is not None else None
        br_m = _si_val(br_m_raw) if br_m_raw is not None else 1.0
        if br_w is None:
            branch_m[br["name"]] = 1  # no w= to estimate a ratio from -- default to 1 unit
            continue
        branch_total = br_w * br_m
        branch_m[br["name"]] = max(1, round(branch_total / ref_total)) if ref_total else 1
    return {"seed_w": seed_w, "seed_l": ref_l, "branch_m": branch_m}


def build_sizing_setup(netlist_path, auto_group=True, auto_mirror=True):
    """Return `(tuning_netlist_text, groups, fixed, seeds, skipped,
    reported_groups, mirror_report)`.

    **No template and no value file.** The netlist itself carries the values
    from here on (`edit_netlist.py`); this step only works out WHICH devices
    share a variable, and what each variable should start at.

    - `groups` -- `{var: {"param": "w"|"l"|"m", "members": [instance, ...]}}`,
      the structure every later step reads. A singleton device gets a group of
      one, so `--set` always resolves the same way.
    - `fixed`  -- `{instance: {param: value}}` that must not move: a mirror
      reference's `m`, pinned to the unit its family counts in.
    - `seeds`  -- `{var: value}` to write into the tuning netlist, which is
      how a mirror family's re-seeded reference W/L and each branch's starting
      M reach the netlist at all.
    """
    text = open(netlist_path).read()
    devices = parse_devices(text)
    families = _find_mirror_families(devices) if auto_mirror else []

    mirror_prefix_for, mirror_m_var_for = {}, {}
    reference_names, reference_seed_wl, branch_starting_m = set(), {}, {}
    claimed, mirror_report = set(), []

    for family in families:
        ref = family["reference"]
        s = _mirror_family_starting_values(family)
        if s is None:
            continue
        ref_prefix = _var_prefix(ref["name"])
        claimed.add(ref["name"])
        mirror_prefix_for[ref["name"]] = ref_prefix
        reference_names.add(ref["name"])
        reference_seed_wl[ref["name"]] = (s["seed_w"], s["seed_l"])
        branch_report = []
        for br in family["branches"]:
            claimed.add(br["name"])
            mirror_prefix_for[br["name"]] = ref_prefix
            m_var = f"{_var_prefix(br['name'])}_M"
            mirror_m_var_for[br["name"]] = m_var
            branch_starting_m[br["name"]] = s["branch_m"][br["name"]]
            branch_report.append((br["name"], m_var, s["branch_m"][br["name"]]))
        mirror_report.append((ref["name"], ref_prefix, s["seed_w"], s["seed_l"],
                              branch_report))

    if auto_group:
        group_prefix_for, reported_groups = _build_groups(devices, exclude=claimed)
    else:
        group_prefix_for, reported_groups = {}, []

    groups, fixed, seeds, skipped = {}, {}, {}, []
    for d in devices:
        if d["kind"] not in TUNABLE_KINDS:
            if d["kind"] == "source":
                skipped.append(f"{d['name']} (ideal source, e.g. bias/probe -- never tuned)")
            elif d["kind"] in ("bjt", "other"):
                skipped.append(f"{d['name']} ({d['kind']} -- not a MOS/resistor/cap, not tuned)")
            continue

        prefix = (mirror_prefix_for.get(d["name"])
                  or group_prefix_for.get(d["name"]) or _var_prefix(d["name"]))
        touched = False
        for key in TUNABLE_PARAMS:
            actual_key, raw_val = _param_lookup(d["params"], key)
            if actual_key is None:
                continue
            var = f"{prefix}_{key.upper()}"
            groups.setdefault(var, {"param": key, "members": []})
            if d["name"] not in groups[var]["members"]:
                groups[var]["members"].append(d["name"])
            if d["name"] in reference_names:
                sw, sl = reference_seed_wl[d["name"]]
                seeds[var] = sw if key == "w" else sl
            else:
                seeds.setdefault(var, _um(raw_val))
            touched = True

        if d["name"] in mirror_m_var_for:
            m_var = mirror_m_var_for[d["name"]]
            groups[m_var] = {"param": "m", "members": [d["name"]]}
            seeds[m_var] = branch_starting_m[d["name"]]
            touched = True
        elif d["name"] in reference_names:
            fixed[d["name"]] = {"m": 1}

        if not touched:
            skipped.append(f"{d['name']} ({d['kind']}, no w=/l= param found on its line -- not tuned)")

    return text, groups, fixed, seeds, skipped, reported_groups, mirror_report


def width_bounds(netlist_text, groups, pdk_root=None, pdk="sky130A"):
    """`{var: {"max": um, "unit": "um", "reason": ...}}` for every `w` group --
    the widest per-copy `w` the PDK's model bins accept for that group's
    devices.

    **Read from the PDK every time, never cached.** The bound is a property of
    the PDK and the device model, not of this run, so there is no file to go
    stale and nothing to keep in sync with the netlist. Callers who need it
    (`run_sizing_iteration.check_param_bounds`, `fold_wide_devices`) ask for it
    at the moment they check.

    Resolved from the group's own MEMBERS, and taken as the strictest of them:
    a group can in principle span two models, and the lower ceiling is the one
    that holds for all of them.

    Returns {} when the PDK is unreadable -- an absent bound must not become a
    false constraint. Callers report that as unchecked, never as permission."""
    try:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "..", "design-sheets-checker", "script"))
        from run_erc_check import load_pdk_bin_widths
    except Exception:
        return {}
    bins = load_pdk_bin_widths(pdk_root=pdk_root, pdk=pdk)
    if not bins:
        return {}

    by_name = {d["name"].lower(): d for d in parse_devices(netlist_text)}
    out = {}
    for var, spec in groups.items():
        if spec.get("param", "").lower() != "w":
            continue
        ceilings = []
        for inst in spec["members"]:
            d = by_name.get(inst.lower())
            if not d:
                continue
            model = (d.get("model") or "").lower()
            wmax = bins.get(f"{model}__model") or bins.get(model)
            if wmax:
                ceilings.append(wmax)
        if ceilings:
            out[var] = {"max": min(ceilings), "unit": "um",
                        "reason": "widest PDK model bin for this device, per copy; "
                                  "exceed it and the device has no model card. "
                                  "Raise total width with m, not with w."}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist", help="the golden .sp")
    ap.add_argument("-o", "--out", required=True,
                    help="output <design_name>_tuning.sp -- the loop's working netlist")
    ap.add_argument("--groups-out", required=True,
                    help="output structure_groups.json")
    ap.add_argument("--no-auto-group", action="store_true",
                     help="disable matched-pair grouping -- one independent "
                          "variable per MOS instance instead")
    ap.add_argument("--pdk", default=_guideline_pdk(),
                    help="PDK whose model bins are checked against the seeds")
    ap.add_argument("--pdk-root", default=None,
                    help="PDK root; defaults to $PDK_ROOT")
    ap.add_argument("--no-auto-mirror", action="store_true",
                     help="disable current-mirror family detection -- reference "
                          "and branches each get independent variables instead")
    args = ap.parse_args()

    text, groups, fixed, seeds, skipped, reported_groups, mirror_families = \
        build_sizing_setup(args.netlist, auto_group=not args.no_auto_group,
                           auto_mirror=not args.no_auto_mirror)

    # Seed the working netlist: a mirror family's re-seeded reference W/L and
    # each branch's starting M only reach the design through this write.
    tuning_text, _ = apply_values(text, seeds, groups, fixed)

    for path in (args.out, args.groups_out):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(args.out, "w") as f:
        f.write(tuning_text)
    with open(args.groups_out, "w") as f:
        json.dump({"groups": groups, "fixed": fixed}, f, indent=2)

    print(f"Wrote {args.out} (the loop's working netlist -- values live here)")
    print(f"Wrote {args.groups_out} ({len(groups)} tunable variables)")
    print(f"\nTunable: {sorted(groups)}")

    bounds = width_bounds(tuning_text, groups, pdk_root=args.pdk_root, pdk=args.pdk)
    over = [(v, seeds[v], b["max"]) for v, b in bounds.items()
            if isinstance(seeds.get(v), (int, float)) and seeds[v] > b["max"]]
    if over:
        print("\nSEED OVER PDK BOUND -- these start outside every model bin, so "
              "iteration 1 cannot simulate:")
        for var, val, mx in sorted(over):
            need = int(-(-val // mx))
            print(f"  {var}={val:g} > {mx:g}um per copy -- fold the total into "
                  f"copies (w={val / need:g}, m={need}) in the golden netlist first.")

    if mirror_families:
        print("\nCurrent-mirror families auto-detected (reference m forced to 1, "
              "shared W/L, each branch's own independent M -- review this, it's a "
              "topological heuristic, see this script's docstring):")
        for ref_name, prefix, seed_w, seed_l, branches in mirror_families:
            print(f"  {prefix}_W={seed_w:g} {prefix}_L={seed_l:g} <- reference {ref_name} (m=1, fixed)")
            for br_name, m_var, starting_m in branches:
                print(f"    branch {br_name}: w/l <- {prefix}_W/{prefix}_L, {m_var}={starting_m:g} (tunable)")
    elif not args.no_auto_mirror:
        print("\nNo current-mirror families detected.")

    if reported_groups:
        print("\nMatched-pair groups auto-detected (share ONE variable pair -- "
              "review this, it's a heuristic, see this script's docstring):")
        for shared_prefix, member_names in reported_groups:
            print(f"  {shared_prefix}_W/{shared_prefix}_L <- {', '.join(member_names)}")
    elif not args.no_auto_group:
        print("\nNo matched-pair groups detected (every MOS device got its own variable).")

    if skipped:
        print("\nNot tuned (by design -- see this script's docstring):")
        for s in skipped:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
