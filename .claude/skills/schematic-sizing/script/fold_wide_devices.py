#!/usr/bin/env python3
"""Fold a MOS whose per-copy `w` exceeds the PDK's maximum width.

**The rule, in full**: a device's drawn per-copy `w` may go up to the maximum
width the PDK allows for its model. Above that, the width is carried as COPIES
instead -- `m` up, `w` down, total width unchanged. At or under it, nothing is
folded and the proposal is left exactly as written.

The fold uses the FEWEST copies the limit demands: `m` is multiplied and `w`
divided by the smallest whole factor that brings per-copy `w` to or under the
maximum. Any whole factor preserves the total exactly, so the minimum is chosen
-- it keeps per-copy width closest to what the caller asked for and adds the
fewest parallel devices.

**Why per-copy width, and why that limit.** A BSIM4 model is a set of binned
cards, each valid over its own `wmin..wmax` window, and the bin is selected on
per-copy `w` -- `m` divides that width, `nf` does not. Past the widest card
the model has, a device matches nothing and the simulation measures nothing.
The limit is therefore the PDK's, not a preference: it is where the device
stops having a model at all.

The number is read from the PDK's own model cards at the moment of the check
(`setup_sizing.width_bounds()`) -- the same value
`run_sizing_iteration.check_param_bounds` refuses a proposal above. This script
folds exactly what that check would reject, so the two never disagree, and
neither depends on a cached bound that could go stale.

**This is not a free re-parameterization: FOLDING MOVES THE OPERATING
POINT.** Same total width in fewer, wider copies vs. more, narrower copies
lands on different model cards and draws different current (measured on this
project's own reference device: the same total width folded 4 / 8 / 16 ways
drew 12.13 / 13.49 / 15.52 mA). The fold is applied BEFORE the iteration
simulates, so the numbers that iteration reports are the folded device's --
never carry a pre-fold measurement forward as if the fold were cosmetic.

**`m` is not a tuning variable here.** This script changes `m` only as the
bookkeeping half of a width change the caller already decided -- a
deterministic, total-preserving fold, never a free lever aimed at a spec. It is
the same operation this script's `netlist` mode performs on the golden netlist
before sizing starts; the `tuning` mode below keeps the invariant holding
while the loop moves.

Usage (dry-run report by default -- nothing is written without --apply):

  python script/fold_wide_devices.py tuning <design_dir>/sizing/<d>_tuning.sp
      --groups <design_dir>/sizing/structure_groups.json
      [--pdk sky130A] [--apply] [--json out.json]

With `--apply` it rewrites the tuning netlist in place -- the folded `w=` and
every member's `m=` together, including a mirror branch's tunable `_M` and a
reference's pinned unit count -- then prints the `book_keeper.md` line to log.
"""
import argparse
import json
import math
import os
import re
import sys

from netlist_devices import parse_devices
from edit_netlist import (load_groups, read_values, apply_values,  # noqa: E402
                          check_groups)
from setup_sizing import width_bounds  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "..", "parasitic-estimation", "script"))
from estimate_parasitics import _si_val, _um_val  # noqa: E402

# `w={{ MN1_W }}` -- the braces carry spaces, so a bare \S+ value pattern
_VALUE = r'(\{\{[^}]*\}\}|[^\s]+)'


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


def _token_re(key):
    return re.compile(rf'(?i)\b({re.escape(key)})\s*=\s*{_VALUE}')


def _read_token(line, key):
    """(matched_key, raw_value) for `key=<value>` on a device line, or
    (None, None). Handles both a literal and a `{{ VAR }}` placeholder."""
    m = _token_re(key).search(line)
    return (m.group(1), m.group(2)) if m else (None, None)


def _var_of(raw):
    """`{{ MN1_W }}` -> `MN1_W`; a literal -> None."""
    if raw is None:
        return None
    m = re.match(r'^\{\{\s*([^}\s]+)\s*\}\}$', raw.strip())
    return m.group(1) if m else None


def _num(x, default=None):
    if x is None:
        return default
    try:
        return float(_si_val(x))
    except Exception:
        return default


def _fmt(v):
    """Render a number the way a netlist should read it: `4` not `4.0`."""
    return f"{int(round(v))}" if abs(v - round(v)) < 1e-9 else f"{v:g}"


def fold_factor(width, ceiling):
    """Smallest whole factor that brings `width` to or under `ceiling`.
    Returns 1 when the width is already within it -- the no-fold case.

    **As few copies as the limit demands, and no more.** The factor multiplies
    `m` and divides `w`, so ANY whole factor preserves the total exactly; the
    minimum is chosen because it leaves per-copy width as close as possible to
    the geometry the caller actually asked for, and adds the fewest parallel
    devices for the layout to draw.

    `<=` and not `<`: the PDK's maximum is a width the model card is valid AT,
    so a device sitting exactly on it is legal and is not folded. (Netlist mode
    below uses a strict, lower ceiling on purpose -- see `_copies_needed`.)"""
    if ceiling <= 0:
        return 1
    return max(1, int(math.ceil(width / ceiling - 1e-9)))


def plan_folds(netlist_text, groups, bounds):
    """One decision per `w` VARIABLE, not per device.

    **The ceiling is the PDK's own maximum width, and nothing else** -- read
    from the model cards at the moment of the check (`setup_sizing.width_bounds`),
    never cached. A proposal at or under it is left exactly as written; above
    it, the width is carried as copies instead. That is the same number
    `run_sizing_iteration.check_param_bounds` hard-stops above, so this
    pre-empts exactly the refusal the runner would issue.

    A variable with NO bound recorded cannot be checked -- it is reported as
    unchecked rather than silently passed, since an absent limit is missing
    information, not permission.

    A shared variable (a matched group, a mirror family) drives several devices
    at once, so the fold is a single factor applied to all of them -- folding
    one member and not its partner is exactly the symmetry break the shared
    variable exists to prevent. Every member's own `m` is multiplied by that one
    factor, which preserves each member's total width AND, in a mirror family,
    the ratio between them."""
    values = read_values(netlist_text, groups)
    # MOS only. A resistor's or capacitor's `w` is not bin-selected the same
    # way and is not this rule's business -- listing it would report every
    # passive as UNCHECKED and bury the MOS devices that do need checking.
    mos = {d["name"].lower() for d in parse_devices(netlist_text)
           if d["kind"] == "mos"}
    plans = []
    for var, spec in sorted(groups.items()):
        if spec.get("param", "").lower() != "w":
            continue
        if not any(inst.lower() in mos for inst in spec["members"]):
            continue
        proposed = values.get(var)
        if not isinstance(proposed, (int, float)):
            continue
        ceiling = (bounds.get(var) or {}).get("max")
        ceiling = float(ceiling) if isinstance(ceiling, (int, float)) else None
        k = 1 if ceiling is None else fold_factor(float(proposed), ceiling)
        plans.append({"var": var, "proposed_w": float(proposed),
                      "ceiling": ceiling, "factor": k,
                      "folded_w": float(proposed) / k,
                      "members": list(spec["members"]),
                      "unchecked": ceiling is None})
    return plans


def _m_var_for(inst, groups):
    """The `m` group variable driving this instance, or None when its `m` is a
    plain literal on the device line."""
    for var, spec in groups.items():
        if spec.get("param", "").lower() == "m" and inst in spec["members"]:
            return var
    return None


def apply_folds(netlist_text, groups, fixed, plans):
    """Apply every fold to the netlist. Returns `(new_text, new_fixed, changes)`.

    `m` is carried three different ways and all three must scale together:
    a tunable `_M` variable (a mirror branch), a `fixed` pin (a mirror
    reference's unit count) and a plain literal on the line. Missing any one of
    them would change a family's ratio while claiming to preserve the total."""
    text = netlist_text
    new_fixed = {k: dict(v) for k, v in (fixed or {}).items()}
    changes = []
    by_name = {d["name"].lower(): d for d in parse_devices(text)}

    for p in (x for x in plans if x["factor"] > 1):
        k = p["factor"]
        updates = {p["var"]: p["folded_w"]}
        changes.append(f"{p['var']}: w {p['proposed_w']:g} -> {p['folded_w']:g}")
        for inst in p["members"]:
            m_var = _m_var_for(inst, groups)
            if m_var:
                cur = read_values(text, groups).get(m_var, 1.0)
                updates[m_var] = cur * k
                changes.append(f"{inst}: {m_var} {cur:g} -> {cur * k:g}")
            elif inst in new_fixed and "m" in new_fixed[inst]:
                old = float(new_fixed[inst]["m"])
                new_fixed[inst]["m"] = old * k
                changes.append(f"{inst}: m (pinned) {old:g} -> {old * k:g}")
            else:
                d = by_name.get(inst.lower())
                old = 1.0
                if d:
                    raw = next((v for kk, v in d["params"].items()
                                if kk.lower() == "m"), None)
                    old = _num(raw, 1.0) or 1.0
                text = _set_literal_m(text, inst, old * k)
                changes.append(f"{inst}: m {old:g} -> {old * k:g}")
        text, _ = apply_values(text, updates, groups, new_fixed)
    return text, new_fixed, changes


def _set_literal_m(netlist_text, inst, value):
    """Write a plain `m=` literal on one device line (adding the token when the
    line carries none -- an absent `m` means 1)."""
    lines = netlist_text.splitlines(keepends=True)
    for d in parse_devices(netlist_text):
        if d["name"].lower() != inst.lower():
            continue
        idx = d["lineno"] - 1
        line = lines[idx]
        new_line, n = re.subn(r'(?i)(\bm\s*=\s*)(\S+)',
                              lambda mo: f"{mo.group(1)}{_fmt(value)}", line, count=1)
        lines[idx] = new_line if n else line.rstrip("\n") + f" m={_fmt(value)}\n"
        break
    return "".join(lines)


def report(plans):
    print("fold check -- per-copy w must stay within the PDK's maximum "
          "width (read from the model cards)\n")
    hdr = f"  {'variable':<16}{'current':>10}{'PDK max':>10}{'factor':>8}  result"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    folded = unchecked = 0
    for p in plans:
        if p["unchecked"]:
            unchecked += 1
            ceil_s, res = "--", "UNCHECKED -- no PDK bound for this variable"
        elif p["factor"] > 1:
            folded += 1
            ceil_s = f"{p['ceiling']:.4g}"
            res = f"FOLD  w={p['folded_w']:g}, m x{p['factor']}"
        else:
            ceil_s, res = f"{p['ceiling']:.4g}", "ok -- within the PDK max, not folded"
        print(f"  {p['var']:<16}{p['proposed_w']:>10.4g}{ceil_s:>10}"
              f"{p['factor']:>8}  {res}")
    print()
    if unchecked:
        print(f"  {unchecked} variable(s) have NO PDK bound -- the model cards "
              "were unreadable,\n  or the model is not binned. That is missing "
              "information, not permission:\n  re-run with the right "
              "--pdk/--pdk-root before trusting this check.\n")
    return folded, unchecked


# ---------------------------------------------------------------------------
# NETLIST MODE -- the pre-sizing fold, run once on the golden netlist.
#
# Same operation as the tuning mode above (more copies, narrower each, total
# width untouched), against a different ceiling and a different input file:
#
#   tuning mode    the tuning .sp  ceiling = the PDK MAX WIDTH, read from the
#                  + its groups    model cards -- fold only once a width
#                                  exceeds what the PDK allows
#   netlist mode   a plain .sp     ceiling = half the PDK MODEL BIN (or --max-w)
#                                  deliberately stricter: a device entering the
#                                  loop leaves room to be tuned UP without
#                                  immediately needing a fold
#
# Two modes, one rule -- per-copy `w` has a ceiling, and `m` carries whatever
# the total needs.
# ---------------------------------------------------------------------------

_PARAM_RE = re.compile(r"\b([A-Za-z_]\w*)\s*=\s*(\S+)")
_COMMENT_RE = re.compile(r"^\s*\*")

# `load_pdk_bin_widths` reads the PDK's own binned `.model` cards. It lives
# outside this skill and is imported rather than copied, so there is ONE reader
# of those cards rather than a second that drifts when a PDK is added.
_CHECKER = None
for _up in range(1, 7):
    _c = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), *([".."] * _up),
        "design-sheets-checker", "script"))
    if os.path.isfile(os.path.join(_c, "run_erc_check.py")):
        _CHECKER = _c
        break
if _CHECKER:
    sys.path.insert(0, _CHECKER)
from run_erc_check import load_pdk_bin_widths, _parse_spice_number  # noqa: E402


def _fmt_exact(x):
    """Enough significant digits that `w*m` reproduces the original total.
    `236/3` printed at 6 digits drifts the total by 1e-4um; at 12 it does not
    drift at a scale any layout or model cares about. Distinct from `_fmt()`
    above, which rounds for a readable netlist value."""
    return f"{x:.12g}"


def _device_model(line):
    """The model/subckt name an `X` line instantiates: last positional token
    before the `k=v` parameters."""
    toks = [t for t in line.split()[1:] if "=" not in t]
    return toks[-1] if toks else None

# Fold to HALF the model's widest bin, not to the bin edge. A card valid up
# to `wmax` is valid AT `wmax`, so folding to the edge satisfies the model and
# still leaves every device sitting at the extreme of its own bin -- the worst
# place to be when a later step nudges `w` up, because the next nudge falls off
# the bin again and costs another iteration. Half the bin leaves room to tune
# in both directions. Override absolutely with --max-w.
BIN_UTILISATION = 0.5

# How far past the minimum copy count to look for one that divides the total
# EXACTLY. w/m must reproduce w*m, and a repeating per-copy width does not:
# 320/7 = 45.714... which, rounded for the netlist, no longer totals 320.
# A slightly larger m that divides cleanly preserves the total instead.
EXACT_DIVISOR_SEARCH = 4


def _copies_needed(w, ceiling):
    """Smallest copy count putting per-copy width STRICTLY under `ceiling`,
    nudged up to one that divides `w` exactly when a near one does.

    Strict, not `<=`: landing exactly on the ceiling is the boundary the
    ceiling exists to keep off, and `ceil()` lands there whenever `w` is an
    exact multiple of it (w=1000, ceiling=50 -> 20 copies of exactly 50).
    Returns an int >= 1."""
    need = max(1, int(math.floor(w / ceiling)) + 1
               if abs(w / ceiling - round(w / ceiling)) < 1e-9
               else int(math.ceil(w / ceiling)))
    for cand in range(need, need + EXACT_DIVISOR_SEARCH + 1):
        if abs(round(w / cand, 6) * cand - w) < 1e-9:
            return cand
    return need


def plan_bin_folds(text, bins, max_w=None):
    """[(lineno, name, model, w, m, need, new_w, new_m)] for every device
    over its ceiling -- BIN_UTILISATION x its model's bin, or `max_w` when
    one is given. Pure -- writes nothing."""
    plan = []
    for i, raw in enumerate(text.splitlines(), 1):
        if _COMMENT_RE.match(raw) or not raw.strip():
            continue
        if raw.strip()[:1].lower() != "x":
            continue
        model = _device_model(raw.strip())
        if not model:
            continue
        wmax = bins.get(f"{model}__model".lower()) or bins.get(model.lower())
        if not wmax:
            continue
        wmax = wmax * BIN_UTILISATION
        if max_w is not None:
            wmax = min(wmax, max_w)
        params = {k.lower(): v for k, v in _PARAM_RE.findall(raw)}
        w = _parse_spice_number(params.get("w", ""))
        if w is None or w < wmax:
            continue
        m = _parse_spice_number(params.get("m", "1")) or 1.0
        need = _copies_needed(w, wmax)
        plan.append((i, raw.split()[0], model, w, m, need, w / need, m * need))
    return plan


def apply_bin_folds(text, plan, max_w=None):
    lines = text.splitlines(keepends=True)
    reason = ("to fit the PDK model bin" if max_w is None
              else f"to fit w<={_fmt_exact(max_w)}um")
    for (lineno, _name, _model, _w, _m, _need, new_w, new_m) in plan:
        line = lines[lineno - 1]
        line = re.sub(r"(\bw\s*=\s*)(\S+)", lambda mo: mo.group(1) + _fmt_exact(new_w),
                      line, count=1, flags=re.IGNORECASE)
        if re.search(r"\bm\s*=", line, re.IGNORECASE):
            line = re.sub(r"(\bm\s*=\s*)(\S+)",
                          lambda mo: mo.group(1) + _fmt_exact(new_m),
                          line, count=1, flags=re.IGNORECASE)
        else:
            line = line.rstrip("\n") + f" m={_fmt_exact(new_m)}\n"
        lines[lineno - 1] = line

    if max_w is None:
        header = ["* MODIFIED by fold_wide_devices.py netlist: folded to fit "
                  "the PDK model bins.\n"]
    else:
        header = [f"* MODIFIED by fold_wide_devices.py netlist --max-w "
                  f"{_fmt_exact(max_w)}: a REQUESTED "
                  "per-copy width\n* ceiling, stricter than the PDK bin alone.\n"
                  "* Ceiling applied: min(PDK model bin, "
                  f"{_fmt_exact(max_w)}um), inclusive.\n"]
    for (_ln, name, _model, w, m, need, new_w, new_m) in plan:
        header.append(f"*   {name}: w={_fmt_exact(w)} m={_fmt_exact(m)} -> w={_fmt_exact(new_w)} "
                      f"m={_fmt_exact(new_m)}  (total {_fmt_exact(w * m)}um unchanged, "
                      f"{need}x copies {reason})\n")
    if max_w is None:
        header.append("* Re-expression only: same total width, same topology, same "
                      "nets. `nf` untouched --\n* the model bins on per-copy w and "
                      "nf does not divide it. Original in user_inputs/.\n")
    else:
        header.append("* Same total width, same topology, same nets; `nf` untouched. "
                      "The input netlist\n* is unchanged. NOT operating-point "
                      "neutral -- per-copy source/drain parasitics do\n* not scale "
                      "out of the total, so RE-SIMULATE this file against its own "
                      "spec.\n")

    # SPICE reads line 1 of a deck it is pointed at directly as the TITLE and
    # never as a statement, so a header prepended above it would shift a real
    # statement into that position. Insert after line 1 when line 1 is already
    # a comment; only prepend when the file has no lines at all.
    if lines and _COMMENT_RE.match(lines[0]):
        return "".join(lines[:1] + header + lines[1:])
    return "".join(header + lines)


def main_netlist(argv):
    """NETLIST MODE -- fold a plain `.sp` against the PDK model bin.

    The pre-sizing pass, run once on the golden netlist: a device past its bin
    has no model card, so the design cannot simulate at all until it is
    folded."""
    ap = argparse.ArgumentParser(
        prog="fold_wide_devices.py netlist",
        description="Fold a netlist's over-bin devices into copies that fit.")
    ap.add_argument("netlist")
    ap.add_argument("--pdk", default=_guideline_pdk())
    ap.add_argument("--pdk-root", default=None)
    ap.add_argument("--max-w", type=float, default=None,
                    help="per-copy width ceiling in um, applied on top of the "
                         "PDK bin (never above it). Inclusive.")
    ap.add_argument("--out", default=None,
                    help="write the folded netlist here instead of editing "
                         "the input in place")
    ap.add_argument("--dry-run", action="store_true",
                    help="report the folding, write nothing")
    args = ap.parse_args(argv)
    if args.max_w is not None and args.max_w <= 0:
        ap.error("--max-w must be positive")

    bins = load_pdk_bin_widths(pdk_root=args.pdk_root, pdk=args.pdk)
    if not bins:
        print(f"PDK '{args.pdk}' not readable -- no bins loaded, nothing checked.")
        return 0

    text = open(args.netlist).read()
    plan = plan_bin_folds(text, bins, max_w=args.max_w)
    ceiling = ("the PDK model bin" if args.max_w is None
               else f"min(model bin, {_fmt_exact(args.max_w)}um)")
    if not plan:
        print(f"{args.netlist}: every device is inside {ceiling} -- no change.")
        return 0

    print(f"{args.netlist}: {len(plan)} device(s) folded to fit {ceiling}\n")
    print(f"  {'device':8s} {'was':>22s}    {'now':>22s}   total")
    for (_ln, name, _model, w, m, need, new_w, new_m) in plan:
        print(f"  {name:8s} {f'w={_fmt_exact(w)} m={_fmt_exact(m)}':>22s} -> "
              f"{f'w={_fmt_exact(new_w)} m={_fmt_exact(new_m)}':>22s}   "
              f"{_fmt_exact(w * m)}um (unchanged)")
    print("\n  Same total width, same topology -- the copy count carries it.")
    print("  The operating point DOES move with the copy count; re-simulate.")

    if args.dry_run:
        print("\n  --dry-run: nothing written.")
        return 0

    dest = args.out or args.netlist
    open(dest, "w").write(apply_bin_folds(text, plan, max_w=args.max_w))
    if args.out:
        print(f"\n  Written to {dest}; {args.netlist} is unchanged.")
    else:
        print(f"\n  Written to {dest}. The original is untouched in "
              f"user_inputs/. Re-run this check to confirm it now exits 0.")
    return 0


def main_template(argv):
    ap = argparse.ArgumentParser(
        prog="fold_wide_devices.py tuning",
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist", help="this design's <design_name>_tuning.sp")
    ap.add_argument("--groups", required=True, help="structure_groups.json")
    ap.add_argument("--pdk", default=_guideline_pdk())
    ap.add_argument("--pdk-root", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="write the folded netlist (and any changed `fixed` pin) "
                         "back -- default: report only, change nothing")
    ap.add_argument("--json", default=None, help="also write the plan as JSON")
    args = ap.parse_args(argv)

    groups, fixed = load_groups(args.groups)
    text = open(args.netlist).read()

    desync = check_groups(text, groups)
    if desync:
        raise SystemExit(
            "the netlist has desynchronised groups -- fix that before folding, "
            "or the fold preserves a total that is already wrong:\n" +
            "\n".join(f"  {v}: " + ", ".join(f"{k}={x:g}" for k, x in seen.items())
                       for v, seen in desync))

    bounds = width_bounds(text, groups, pdk_root=args.pdk_root, pdk=args.pdk)
    plans = plan_folds(text, groups, bounds)
    if not plans:
        raise SystemExit(
            f"no `w` variable found in {args.groups} -- this rule bounds drawn "
            "MOS width, so there is nothing here to check.")
    folded, unchecked = report(plans)

    changes = []
    if folded and args.apply:
        new_text, new_fixed, changes = apply_folds(text, groups, fixed, plans)
        with open(args.netlist, "w") as f:
            f.write(new_text)
        if new_fixed != fixed:
            with open(args.groups, "w") as f:
                json.dump({"groups": groups, "fixed": new_fixed}, f, indent=2)
        print("applied:")
        for c in changes:
            print(f"  {c}")
        print(f"\n  {args.netlist} rewritten. Log in book_keeper.md, e.g.:\n"
              "  - Folded: " + "; ".join(
                  f"{p['var']} {p['proposed_w']:g}->{p['folded_w']:g} "
                  f"(m x{p['factor']}, total unchanged)"
                  for p in plans if p["factor"] > 1) +
              "\n  (the op point moves with the fold -- this iteration's "
              "numbers are the FOLDED device's)")
    elif folded:
        print("report only -- nothing written. Re-run with --apply to fold, "
              "THEN run the iteration.")
    elif not unchecked:
        print("every width is within the PDK maximum; nothing to fold.")

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w") as f:
            json.dump({"applied": bool(folded and args.apply), "changes": changes,
                       "plans": plans}, f, indent=2)
    return 2 if unchecked else 0


def main():
    """Two modes, one rule. The first argument selects which.

    Kept as explicit subcommands rather than sniffed from the file extension:
    the two take different inputs, different ceilings and different flags, and
    a caller that means one and gets the other would silently fold against the
    wrong bound."""
    modes = {"netlist": main_netlist, "tuning": main_template}
    if len(sys.argv) > 1 and sys.argv[1] in modes:
        return modes[sys.argv[1]](sys.argv[2:])
    print(__doc__)
    print("\nUsage:\n"
          "  fold_wide_devices.py netlist  <netlist.sp> [--pdk sky130A] "
          "[--max-w UM] [--out PATH] [--dry-run]\n"
          "  fold_wide_devices.py tuning   <design>_tuning.sp --groups "
          "structure_groups.json [--pdk sky130A] [--apply]\n",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
