#!/usr/bin/env python3
"""Shared value/geometry helpers imported by schematic-sizing and device-shaper.

RESTORED 2026-08-26 by schematic-agent. The `parasitic-estimation` skill folder
was absent from this checkout (never tracked by git, not .gitignore'd) while
`schematic-sizing/script/{edit_netlist,setup_sizing,fold_wide_devices}.py` and
`device-shaper/script/*.py` all import from it -- every one of those modules
died with ModuleNotFoundError, so neither skill could run at all.

Only the symbols whose contract is stated EXACTLY somewhere in the repo are
restored here:

  _si_val  -- ngspice engineering-suffix number parse. Semantics and the
              suffix table (longest match first, so MEG beats M) are copied
              from the canonical implementation that survived in
              `design-sheets-checker/script/run_erc_check.py::_parse_spice_number`,
              which documents the same contract: `w=6u` -> 6e-06, `w=40` -> 40.
  _um_val  -- drawn dimension in MICRONS. The heuristic is quoted verbatim in
              `schematic-sizing/script/setup_sizing.py::_um`'s docstring:
              "anything at or below 1e-3 is metres and is scaled by 1e6,
              anything above is already microns. 1e-3 um is 1 nm -- below every
              process minimum, so no real drawn dimension is ambiguous."
  _MOS_RE  -- matches a MOS device line (`X`-wrapped subckt call or a bare `M`
              primitive) capturing name / nodes / model, as used by
              device-shaper's `.match(line.strip())` call sites.

DELIBERATELY NOT RESTORED -- `PDK_TABLES`, the per-process parasitic
COEFFICIENT table device-shaper's Nf sweep used to need. NOTE: that sweep no longer
exists -- device-shaper now chooses `nf` from geometry (unit device, square device,
square array) and needs no coefficient table at all, so an empty table here no
longer blocks shaping on any PDK. The table is still what `pre-layout-extrapolation`
reads. Those are measured
process numbers; inventing them would produce plausible, wrong nf decisions.
It is left EMPTY on purpose so `sweep_fingers.check_nf()` takes its own
already-written "no parasitic coefficient table for pdk=..." branch and the
caller reports the nf as never measured, rather than silently shaping against
fabricated physics.
"""
import re

# ngspice engineering-suffix scale factors. ORDER MATTERS: longest match wins,
# so "MEG" (1e6) must be tried before "M" (1e-3) and "MIL" before "M".
_SPICE_SUFFIXES = (
    ("MEG", 1e6), ("MIL", 25.4e-6), ("T", 1e12), ("G", 1e9), ("K", 1e3),
    ("M", 1e-3), ("U", 1e-6), ("N", 1e-9), ("P", 1e-12), ("F", 1e-15),
)
_NUM_HEAD_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")

# A MOS device line: `XM1 d g s b sky130_fd_pr__pfet_01v8 l=.. w=..` or the
# bare-primitive spelling `M1 d g s b <model> ...`. Group names match how the
# device-shaper call sites index the match.
_MOS_RE = re.compile(
    r"^(?P<name>[XxMm]\S+)\s+"
    r"(?P<d>\S+)\s+(?P<g>\S+)\s+(?P<s>\S+)\s+(?P<b>\S+)\s+"
    r"(?P<model>\S*(?:[np]fet|nmos|pmos)\S*)"
    r"(?P<params>.*)$", re.IGNORECASE)


def _si_val(raw):
    """Numeric value of a SPICE parameter token, honouring engineering
    suffixes. Returns None if the token is not a number at all.

    `4u` -> 4e-06, `150n` -> 1.5e-07, `40` -> 40.0, `21k` -> 21000.0.
    Trailing alphabetic noise after the suffix is ignored the way ngspice
    ignores it ("1.8V", "6uF", "10kOhm")."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    stripped = str(raw).strip()
    m = _NUM_HEAD_RE.match(stripped)
    if not m:
        return None
    num = float(m.group(0))
    tail = stripped[m.end():].upper()
    for suffix, scale in _SPICE_SUFFIXES:
        if tail.startswith(suffix):
            return num * scale
    return num


def _um_val(raw):
    """A drawn dimension (`w`/`l`) in MICRONS, whatever the netlist wrote.

    Netlists reaching this project spell drawn size three ways -- bare microns
    (`w=40.7`), an SI suffix (`w=4u`) and SI metres (`w=21.0e-7`) -- and
    `_si_val` faithfully returns a different SCALE for each. The single rule,
    quoted from setup_sizing._um: at or below 1e-3 the value is metres and is
    scaled by 1e6; above it, it is already microns. 1e-3 um is 1 nm, below
    every process minimum, so no real drawn dimension is ambiguous.

    Counts (`m`, `nf`) must NOT come through here -- they are dimensionless."""
    v = _si_val(raw)
    if v is None:
        return None
    return v * 1e6 if abs(v) <= 1e-3 and v != 0 else v


# See module docstring: intentionally empty, not forgotten.
PDK_TABLES = {}
