#!/usr/bin/env python3
"""Check that a design directory has the inputs this project needs before
a post-layout optimization run can start:

  1. golden netlist       *.sp      (one TOP-LEVEL .subckt, no behavioral
                                    elements) -- REQUIRED. A hierarchical
     design is normal: intake files the whole include closure into
     netlist/, so several *.sp there, and several .subckt blocks within
     one file, are both expected. What must be unambiguous is the top of
     that closure -- resolved by resolve_top_netlist()/root_subckts(), or
     named outright with --netlist.
  2. pre-layout testbench *.spice   (.include's the netlist, has .ac + .control)
                                    -- REQUIRED. OR, if the netlist itself
     already has an .ac line + .control block, the netlist is treated as
     self-contained and no separate *.spice is required.
  3. starting layout      *.gds     (valid GDSII header) -- OPTIONAL: absent
     means "generate the layout from scratch" (SKILL.md Step 0d), reported as
     INFO, not a failure. A GDS that is present but malformed IS a failure.
  4. target spec          target_spec.json -- REQUIRED, and required to
     follow ../spec_form_template.md: one object per spec key carrying
     Direction (FLOOR/CEILING/RANGE), Value (scalar, or [min, max] for a
     RANGE) and Units. Missing, unparseable, or not conforming to that
     template is reported as INCOMPLETE (exit 2), per key, with the
     correction. Nothing in this flow authors or repairs a spec: a
     non-conforming file goes back to the USER to complete or correct --
     `design-sheets-intake` treats it as one of three mandatory user files
     and stops intake without it. Step 8 must not hand off on a 2.

Each input is looked for in the subfolder `design-sheets-intake` files it
into (netlist/, testbench/, spec/, layout/), falling back to the top level
of design_dir for a flat directory that never went through intake.
`ori_gds/` is also accepted for the layout, for folders laid down before
intake existed.

This script does NOT check the specified PDK's device library beyond a
light prefix sanity check, and does NOT run DRC/LVS/simulation -- it is an
intake gate, not a correctness check. See SKILL.md for what happens after
each PASS (0) / FAIL (1) / INCOMPLETE (2).

Usage:
  python check_design_sheets.py path/to/design_dir [--pdk sky130A]
"""
import argparse
import glob
import json
import os
import re
import sys

EXCLUDE_DIR_HINTS = ("lvs_work", "pex_run", "drc_work", "layout_work", "__pycache__")
EXCLUDE_NAME_HINTS = ("_extracted", "_pex", "_lvs")

# `design-sheets-intake` files each input into its own subfolder, so by the
# time this runs, `design_dir/*.sp` matches nothing -- this script has to look
# where intake actually put the files. A design_dir may ALSO still be flat
# (this script pointed straight at a user's folder that never went through
# intake), so each input falls back to the top level when its subfolder is
# absent or empty. `ori_gds/` is a second layout candidate, for folders laid
# down before intake owned this structure.
#
# `user_inputs/` is deliberately never searched: it holds a verbatim copy of
# every file that also lives in netlist//testbench//spec//layout/, so scanning
# it would make every single input look ambiguous ("multiple *.sp candidates").
INTAKE_SUBDIRS = {".sp": ("netlist",), ".spice": ("testbench",),
                  ".gds": ("layout", "ori_gds")}


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


def _candidates(design_dir, ext):
    search_dirs = [os.path.join(design_dir, sub)
                   for sub in INTAKE_SUBDIRS.get(ext, ())]
    search_dirs.append(design_dir)
    for d in search_dirs:
        out = []
        for path in glob.glob(os.path.join(d, f"*{ext}")):
            name = os.path.basename(path)
            if any(h in name for h in EXCLUDE_NAME_HINTS):
                continue
            out.append(path)
        if out:
            return sorted(out)
    return []


_DEVICE_PARAM_RE = re.compile(r"\b(l|w|nf|m|r|c)\s*=\s*(\S+)", re.IGNORECASE)
_NUMERIC_VALUE_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?[a-zA-Z]{0,4}$")

_COMMENT_LINE_RE = re.compile(r"^\s*\*")
_INLINE_COMMENT_RE = re.compile(r"\s*;.*$")


def _strip_spice_comment(line):
    """`*`-comment lines and `;` inline comments carry no device
    parameters. Skipping them is not cosmetic: SKILL.md Step 2a REQUIRES
    the corrected netlist to carry a header comment explaining the unit
    rescale, and such a comment naturally contains text like
    "w=6.000e-06" -- scanning it made Step 3 hard-fail on the very file
    Step 2a had just told the flow to produce. Mirrors
    run_erc_check.py's own COMMENT_LINE_RE/INLINE_COMMENT_RE handling."""
    if _COMMENT_LINE_RE.match(line):
        return ""
    return _INLINE_COMMENT_RE.sub("", line)


_INCLUDE_RE = re.compile(r"""^\s*\.include\s+["']?([^"'\s]+)""",
                         re.MULTILINE | re.IGNORECASE)


def _read(path):
    try:
        return open(path).read()
    except OSError:
        return ""


def _includes_in(text):
    """Basenames this file `.include`s. Basename, not the written path:
    intake re-points includes as it files the closure, so the same
    sub-netlist is `preamp.sp` in one file and `../netlist/preamp.sp` in
    another."""
    return {os.path.basename(p) for p in _INCLUDE_RE.findall(text)}


def declared_subckts(text):
    return re.findall(r"^\.subckt\s+(\S+)", text, re.MULTILINE | re.IGNORECASE)


def _xline_target(line):
    """The subckt/model a SPICE `X` line instantiates: the last positional
    token, i.e. the last one that isn't a `k=v` parameter and isn't the
    instance name. `XM1 d g s b sky130_fd_pr__nfet_01v8 l=0.15 w=3.6` ->
    `sky130_fd_pr__nfet_01v8`."""
    toks = [t for t in line.split()[1:] if "=" not in t]
    return toks[-1] if toks else None


def root_subckts(text):
    """Declared `.subckt` blocks that nothing in THIS file instantiates.

    A hierarchical netlist legitimately declares several blocks in one
    file -- a DUT plus the sub-blocks it is built from -- so counting
    blocks does not identify the circuit under test. The DUT is the root
    of the local hierarchy: declared here, instantiated by nothing here
    (its only caller is the testbench, in another file). Exactly one root
    is what this flow needs; the count of blocks is irrelevant."""
    declared = declared_subckts(text)
    if not declared:
        return []
    instantiated = set()
    for line in text.splitlines():
        stripped = _strip_spice_comment(line).strip()
        if stripped[:1].lower() == "x":
            target = _xline_target(stripped)
            if target:
                instantiated.add(target.lower())
    return [s for s in declared if s.lower() not in instantiated]


def resolve_top_netlist(design_dir, cands):
    """Pick the top-level netlist out of an intake-filed `netlist/`.

    `design-sheets-intake` files the netlist's WHOLE include closure into
    `netlist/`, so more than one `.sp` there is the normal shape of a
    hierarchical design, not an ambiguity to fail on. Two signals name the
    top level, and they are checked together because either alone can be
    absent:

      A -- a testbench `.include`s it (the deck instantiates the DUT);
      B -- no sibling `.sp` includes it (sub-netlists are pulled in by the
           file above them; the top of the closure is pulled in by nobody).

    Returns (path_or_None, issues). Only a genuine tie is an issue -- and
    `--netlist` settles that without editing anything.
    """
    if len(cands) == 1:
        return cands[0], []
    by_base = {os.path.basename(p): p for p in cands}

    included_by_sibling = set()
    for p in cands:
        for base in _includes_in(_read(p)):
            sib = by_base.get(base)
            if sib and sib != p:
                included_by_sibling.add(sib)
    roots = [p for p in cands if p not in included_by_sibling]

    tb_named = []
    for tb in _candidates(design_dir, ".spice"):
        for base in _includes_in(_read(tb)):
            if base in by_base and by_base[base] not in tb_named:
                tb_named.append(by_base[base])

    both = [p for p in roots if p in tb_named]
    for pick in (both, tb_named, roots):
        if len(pick) == 1:
            return pick[0], []

    return None, [
        f"cannot identify the top-level netlist among {len(cands)} *.sp files: "
        f"{[os.path.basename(p) for p in cands]}. Included by a testbench: "
        f"{[os.path.basename(p) for p in tb_named] or 'none'}; included by no "
        f"sibling: {[os.path.basename(p) for p in roots] or 'none'}. "
        f"Name it explicitly with --netlist PATH (intake's hand-off block "
        f"reports which file is the top level)"
    ]


def check_netlist(design_dir, netlist_override=None):
    if netlist_override:
        if not os.path.isfile(netlist_override):
            return None, [f"--netlist {netlist_override}: no such file"]
        path, issues = netlist_override, []
    else:
        cands = _candidates(design_dir, ".sp")
        if not cands:
            return None, ["no *.sp file found in design_dir"]
        path, issues = resolve_top_netlist(design_dir, cands)
        if path is None:
            return None, issues
    text = open(path).read()
    roots = root_subckts(text)
    if not declared_subckts(text):
        issues.append("no .subckt found")
    elif len(roots) != 1:
        issues.append(
            f"expected exactly one top-level .subckt (one declared here and "
            f"instantiated by nothing here), found {len(roots)}: {roots}. "
            f"Declared in this file: {declared_subckts(text)}"
        )
    if not re.search(r"^\.ends\b", text, re.MULTILINE | re.IGNORECASE):
        issues.append("no .ends found")
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = _strip_spice_comment(line).strip()
        if not stripped:
            continue
        if re.match(r"^[EG]\S*\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+", stripped, re.IGNORECASE):
            issues.append(
                f"line {lineno}: behavioral-looking E/G source in golden netlist "
                f"(not layout-realizable): {stripped!r}"
            )
        # unresolved template placeholders (e.g. 'nf=ZZZ' left over from a
        # sizing-generator template that was never filled in) -- a device
        # parameter value that isn't numeric can't be laid out or simulated,
        # so this must be a hard fail, not a silent PASS.
        for pname, pval in _DEVICE_PARAM_RE.findall(stripped):
            if not _NUMERIC_VALUE_RE.match(pval):
                issues.append(
                    f"line {lineno}: non-numeric {pname}={pval!r} -- looks like an "
                    f"unresolved template placeholder, not a real device parameter: {stripped!r}"
                )
    return path, issues


# Every analysis ngspice can be asked to perform. A testbench needs at
# least ONE of these -- WHICH one depends entirely on the circuit class,
# and requiring any particular one rejects correct decks: .tran for an
# oscillator, .dc swept over temperature for a reference, an input ramp
# for a comparator. Matched with an optional leading dot so both the
# deck-level form (`.tran 1n 200n`) and the bare .control-block form
# (`tran 1n 200n`, `op`) are recognized.
ANALYSIS_CMDS = ("ac", "dc", "tran", "op", "noise", "disto",
                 "pz", "sens", "tf", "four")
_ANALYSIS_RE = re.compile(
    r"^\s*\.?(" + "|".join(ANALYSIS_CMDS) + r")\b",
    re.MULTILINE | re.IGNORECASE)


def analyses_in(text):
    """Return the set of analysis statements present, lower-cased."""
    return {m.lower() for m in _ANALYSIS_RE.findall(text)}


def netlist_has_testbench(netlist_path):
    """True if the golden netlist file itself already contains a
    testbench (a self-contained single-file design) -- same
    any-analysis + .control signature check_testbench() looks for in a
    separate file."""
    if not netlist_path or not os.path.isfile(netlist_path):
        return False
    text = open(netlist_path).read()
    has_control = ".control" in text.lower()
    return bool(analyses_in(text)) and has_control


def check_testbench(design_dir, netlist_path):
    if netlist_has_testbench(netlist_path):
        # self-contained: the netlist IS the testbench, no separate *.spice needed
        return netlist_path, []
    cands = _candidates(design_dir, ".spice")
    if not cands:
        return None, ["no *.spice file found in design_dir"]
    netlist_name = os.path.basename(netlist_path) if netlist_path else None
    dut = subckt_name(netlist_path)
    scored = []
    for path in cands:
        text = open(path).read()
        found = analyses_in(text)
        has_analysis = bool(found)
        has_control = ".control" in text.lower()
        includes_netlist = bool(netlist_name and netlist_name in text)
        # A testbench that includes the right FILE can still drive a
        # different circuit, and one that names the right DUT can include
        # the wrong file -- both are real ways a testbench and a netlist
        # get paired up wrongly during intake, so they are checked
        # separately rather than collapsed into one "looks related" flag.
        instantiates_dut = bool(dut and re.search(
            r"\b" + re.escape(dut) + r"\b", text, re.IGNORECASE))
        name_hints_pre = "pre" in os.path.basename(path).lower()
        score = sum([has_analysis, has_control, includes_netlist,
                     instantiates_dut, name_hints_pre])
        scored.append((score, path, sorted(found), has_control,
                       includes_netlist, instantiates_dut))
    scored.sort(reverse=True)
    (best_score, best_path, found, has_control,
     includes_netlist, instantiates_dut) = scored[0]
    issues = []
    if len(cands) > 1 and scored[1][0] == best_score:
        issues.append(
            f"multiple equally-plausible *.spice candidates, picked {best_path}; "
            f"disambiguate by naming the pre-layout one with 'pre' in its filename: {cands}"
        )
    if not found:
        issues.append(
            f"{best_path}: no analysis statement found -- expected at least "
            f"one of {', '.join('.' + c for c in ANALYSIS_CMDS)} "
            f"(a testbench that performs no analysis measures nothing). "
            f"WHICH analysis is right depends on the circuit; this check "
            f"does not require any particular one"
        )
    if not has_control:
        issues.append(f"{best_path}: no .control block found")
    if netlist_name and not includes_netlist:
        issues.append(f"{best_path}: does not appear to .include the golden netlist ({netlist_name})")
    if dut and not instantiates_dut:
        issues.append(
            f"{best_path}: never mentions '{dut}', the golden netlist's own "
            f".subckt -- this testbench drives a different circuit, so it "
            f"cannot exercise this netlist no matter what it .includes"
        )
    return best_path, issues


SUPPLY_LIKE = ("vdd", "vss", "gnd", "vcc", "vee", "avdd", "avss", "dvdd", "dvss")


def subckt_name(netlist_path):
    """The DUT's name -- the netlist's ROOT `.subckt`, not its first one.

    In a file declaring a DUT plus the sub-blocks it instantiates, the
    first `.subckt` is as likely to be a sub-block as the DUT, and every
    consumer here wants the DUT: the testbench pairing check asks whether
    the deck drives it, and guess_output_ports() reads its ports."""
    if not netlist_path or not os.path.isfile(netlist_path):
        return None
    roots = root_subckts(_read(netlist_path))
    if len(roots) == 1:
        return roots[0]
    declared = declared_subckts(_read(netlist_path))
    return declared[0] if declared else None


def subckt_ports(netlist_path):
    """Port list of the DUT `.subckt` (see subckt_name), or []."""
    dut = subckt_name(netlist_path)
    if not dut:
        return []
    m = re.search(r"^\.subckt\s+" + re.escape(dut) + r"\s+(.+)$",
                  _read(netlist_path), re.MULTILINE | re.IGNORECASE)
    return m.group(1).split() if m else []


def guess_output_ports(netlist_path):
    """Heuristic candidate output ports: name contains 'out'
    (case-insensitive) and isn't a supply-rail-looking name.
    Analysis-agnostic -- this is the node whose response the deck should
    record, whichever analysis produces it.
    Deliberately returns 0 or 2+ candidates when it can't be sure --
    see SKILL.md's testbench-data-saving step, which asks the user
    rather than silently guessing in that case."""
    ports = subckt_ports(netlist_path)
    return [p for p in ports
            if "out" in p.lower() and p.lower() not in SUPPLY_LIKE]


def testbench_saves_node(testbench_path, node):
    """True if the testbench's .control block writes/prints this node --
    the `write ... v(<node>)` signature ../../../reference/environment.md's
    validated testbench pattern uses. Independent of which analysis
    produced the data: a .tran deck writing v(vout) matches the same way
    an .ac one does."""
    if not testbench_path or not node or not os.path.isfile(testbench_path):
        return False
    text = open(testbench_path).read()
    pattern = re.compile(
        r"^\s*(write|print)\b.*\bv\(\s*" + re.escape(node) + r"\s*\)",
        re.MULTILINE | re.IGNORECASE,
    )
    return bool(pattern.search(text))


def check_gds(design_dir):
    cands = _candidates(design_dir, ".gds")
    if not cands:
        # NOT a failure. SKILL.md Step 0d makes "no starting layout" a
        # supported, interviewed path -- layout-agent generates one from
        # scratch. Only a GDS that is PRESENT and malformed (or ambiguous)
        # is a hard error.
        return None, ["INFO: no *.gds found -- no starting layout, so the "
                      "layout will be generated from scratch "
                      "(design-sheets-intake's layout branch settled this; "
                      "its tracker shows the row as [-])"]
    if len(cands) > 1:
        return None, [f"multiple *.gds candidates, ambiguous: {cands}"]
    path = cands[0]
    issues = []
    with open(path, "rb") as f:
        header = f.read(4)
    if len(header) < 4 or header[2:4] != b"\x00\x02":
        issues.append(f"{path}: does not look like a valid GDSII stream (missing HEADER record)")
    return path, issues


def check_pdk(pdk_name):
    pdk_root = os.environ.get("PDK_ROOT", os.path.expanduser("~/pdk/manual"))
    pdk_path = os.path.join(pdk_root, pdk_name)
    issues = []
    if not os.path.isdir(pdk_path):
        issues.append(
            f"PDK '{pdk_name}' not found at {pdk_path} -- set PDK_ROOT or confirm the "
            f"PDK name (see ../../../reference/environment.md)"
        )
    return pdk_path, issues


# The form every spec entry must take -- see ../spec_form_template.md, which
# is the normative statement of it and the file to quote back at the user
# when one does not conform. Kept as data here so the diagnostics below can
# name the legal values rather than restating them in prose.
SPEC_ENTRY_KEYS = ("Direction", "Value", "Units")
SPEC_DIRECTIONS = ("FLOOR", "CEILING", "RANGE")

# Misspellings seen in real spec files, mapped to what was meant. `CEILLING`
# earns its place: it is one letter from the legal token, reads as correct,
# and a spec file carrying it would otherwise be rejected with a bare "not
# one of FLOOR/CEILING/RANGE" that the user has to diff by eye.
_DIRECTION_TYPOS = {
    "CEILLING": "CEILING", "CIELING": "CEILING", "CEILNG": "CEILING",
    "FLOOOR": "FLOOR", "FLOR": "FLOOR",
    "RANG": "RANGE", "RANGES": "RANGE",
    "MIN": "FLOOR", "MINIMUM": "FLOOR", "AT_LEAST": "FLOOR",
    "MAX": "CEILING", "MAXIMUM": "CEILING", "AT_MOST": "CEILING",
    "BETWEEN": "RANGE", "INTERVAL": "RANGE",
}

# Unit each key is written in by project convention (../../../reference/metrics.md).
# Advisory only: any key set is accepted, since the circuit class decides
# which specs exist. Alias lists exist so `deg`/`degree`/`degrees` do not
# generate noise about a file that is already right.
_UNIT_CONVENTION = {
    "Gain": ("dB",),
    "UGBW": ("MHz",),
    "PM": ("degree", "degrees", "deg"),
    "Power": ("mW",),
}


def _is_number(v):
    """JSON `true`/`false` are `int` subclasses in Python; a boolean where a
    target belongs is a malformed spec, not a 1."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _direction_hint(raw):
    """The correction to suggest for a `Direction` that is not legal, or
    None when nothing close enough is recognized."""
    if not isinstance(raw, str):
        return None
    norm = raw.strip().upper().replace(" ", "_").replace("-", "_")
    if norm in SPEC_DIRECTIONS:
        return norm          # right word, wrong case/whitespace
    return _DIRECTION_TYPOS.get(norm)


def _check_spec_entry(key, entry):
    """Validate one `target_spec.json` entry against ../spec_form_template.md.
    Returns a list of INCOMPLETE strings (empty when the entry conforms),
    each naming the key and the exact correction, since the user fixes these
    one key at a time."""
    p = f"INCOMPLETE: target_spec.json key '{key}'"

    # The flat form the older spec files used. Called out on its own because
    # it is the migration case, not a typo: the file is well-formed JSON that
    # simply predates this template, and the user needs the shape to write
    # rather than a complaint that a dict was expected.
    if _is_number(entry):
        return [f"{p} is a bare number ({entry!r}) -- the old flat form. It "
                f"declares no direction and no unit, so nothing downstream can "
                f"tell whether bigger or smaller passes, or what the number is "
                f"written in. Write it as "
                f'{{"Direction": "FLOOR"|"CEILING"|"RANGE", "Value": {entry!r}, '
                f'"Units": "<unit>"}} -- see spec_form_template.md. Ask the '
                f"user which direction and unit apply; do not guess either."]
    if not isinstance(entry, dict):
        return [f"{p} must be an object with {list(SPEC_ENTRY_KEYS)}, not "
                f"{type(entry).__name__} -- see spec_form_template.md"]

    issues = []
    missing = [k for k in SPEC_ENTRY_KEYS if k not in entry]
    if missing:
        issues.append(f"{p} is missing {missing} -- every spec states all of "
                      f"{list(SPEC_ENTRY_KEYS)}. Ask the user to complete it; "
                      f"do not fill in a default")
    unknown = [k for k in entry if k not in SPEC_ENTRY_KEYS]
    if unknown:
        issues.append(f"INFO: target_spec.json key '{key}' carries extra "
                      f"field(s) {unknown}, ignored by every downstream check")

    direction = entry.get("Direction")
    if "Direction" in entry:
        if direction not in SPEC_DIRECTIONS:
            hint = _direction_hint(direction)
            fix = (f" -- did you mean '{hint}'?" if hint else "")
            issues.append(f"{p} has Direction={direction!r}, which is not one "
                          f"of {list(SPEC_DIRECTIONS)}{fix} Direction is "
                          f"upper case and is a property of the requirement, "
                          f"not of the number's sign (a -100 dBc/Hz phase "
                          f"noise is a CEILING)")
            direction = hint if hint in SPEC_DIRECTIONS else None

    if "Value" in entry:
        value = entry["Value"]
        if direction == "RANGE":
            if not (isinstance(value, list) and len(value) == 2
                    and all(_is_number(x) for x in value)):
                issues.append(f"{p} is a RANGE, so Value must be a two-number "
                              f"array [min, max] -- got {value!r}. A Python "
                              f"tuple (50, 70) is written [50, 70] in JSON")
            elif value[0] >= value[1]:
                issues.append(f"{p} has Value={value!r}, but a RANGE reads "
                              f"[min, max] and needs min < max -- swap them if "
                              f"they are reversed")
        elif direction in ("FLOOR", "CEILING"):
            if not _is_number(value):
                extra = ""
                if isinstance(value, list):
                    extra = (" An array is only for Direction RANGE; if this "
                             "spec is two-sided, set Direction to RANGE.")
                elif isinstance(value, str):
                    extra = (" Write the number unquoted, and put the unit in "
                             "the Units field.")
                issues.append(f"{p} is a {direction}, so Value must be a "
                              f"single number -- got {value!r}.{extra}")
        elif direction is None:
            pass  # already reported; the Value rule depends on the Direction

    if "Units" in entry:
        units = entry["Units"]
        if not isinstance(units, str) or not units.strip():
            issues.append(f"{p} has Units={units!r} -- Units is a non-empty "
                          f"string naming what Value is written in ('MHz', "
                          f"'dB', 'mW'), or '-' if dimensionless. Every "
                          f"downstream comparison converts using this field")
        else:
            expected = _UNIT_CONVENTION.get(key)
            if expected and units.strip() not in expected:
                issues.append(f"INFO: target_spec.json key '{key}' declares "
                              f"Units={units!r}; this project writes it in "
                              f"'{expected[0]}' (../../../reference/metrics.md). "
                              f"Confirm with the user rather than rescaling")
    return issues


def check_target_spec(design_dir):
    """`target_spec.json` is a MANDATORY user file that
    `design-sheets-intake` collects and files into `spec/`.

    Its required form is `../spec_form_template.md`: an object per spec key
    carrying `Direction` (FLOOR/CEILING/RANGE), `Value` (a scalar, or a
    two-element [min, max] for RANGE) and `Units`. A file that does not
    conform is reported per key with the correction, and the user is asked
    to complete or correct it -- **which is not the same as writing one**.
    Nothing in this flow authors a target, derives one from a simulation, or
    fills in a `Direction`/`Value`/`Units` the user did not state; a target
    nobody set is one every later iteration reports against as though they
    had. So missing and malformed remain the same finding: exit 2. Malformed
    stays out of the exit-1 bucket because it is a gap in what arrived rather
    than a defect in the circuit, and the two route to different places.

    Looked for in `spec/` first (where intake files it), then at the top
    level, which is where the older flat layout kept it and where
    `../../../reference/process_results_template.py` still falls back to."""
    path = os.path.join(design_dir, "spec", "target_spec.json")
    if not os.path.isfile(path):
        path = os.path.join(design_dir, "target_spec.json")
    if not os.path.isfile(path):
        return None, ["INCOMPLETE: no target_spec.json in spec/ or at the top "
                      "level -- it is a mandatory user file, so hand back to "
                      "design-sheets-intake; do not write one"]
    try:
        data = json.load(open(path))
    except json.JSONDecodeError as e:
        return path, [f"INCOMPLETE: target_spec.json is not valid JSON: {e}"]
    if not isinstance(data, dict) or not data:
        return path, ["INCOMPLETE: target_spec.json must be a non-empty JSON "
                      "object, one entry per spec key -- see "
                      ".claude/skills/design-sheets-checker/spec_form_template.md"]

    issues = []
    for key, entry in data.items():
        issues.extend(_check_spec_entry(key, entry))

    # `UGBW` is in MHz here, not Hz -- the project convention encoded in
    # ../../schematic-sizing/script/run_sizing_iteration.py's check_target().
    # A value in the millions is a Hz figure written into an MHz field, which
    # produces a plausible-looking file and silently wrong comparisons at
    # every downstream gate. Informational rather than fatal: it is a
    # heuristic, and a legitimately huge target is the user's call.
    ugbw = data.get("UGBW")
    if isinstance(ugbw, dict):
        ugbw = ugbw.get("Value")
    if isinstance(ugbw, list) and ugbw and _is_number(ugbw[-1]):
        ugbw = ugbw[-1]
    if _is_number(ugbw) and ugbw > 1e6:
        issues.append(
            f"INFO: UGBW={ugbw:g} -- this field is in MHz, so that reads as "
            f"{ugbw:g} MHz. If you meant {ugbw / 1e6:g} MHz, write "
            f"{ugbw / 1e6:g}. Ask the user; do not rescale it here."
        )

    if any(i.startswith("INCOMPLETE") for i in issues):
        issues.append("INCOMPLETE: the target spec does not follow "
                      ".claude/skills/design-sheets-checker/spec_form_template.md "
                      "-- show the user the failing keys above beside that "
                      "template and ask them to complete or correct the file, "
                      "then re-run this check. Do not repair it yourself and "
                      "do not proceed on a partially-formed spec")
    return path, issues


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("design_dir")
    ap.add_argument("--pdk", default=_guideline_pdk())
    ap.add_argument("--netlist", default=None,
                    help="path to the TOP-LEVEL netlist. Only needed when "
                         "netlist/ holds several *.sp and neither signal "
                         "resolve_top_netlist() uses settles which is the "
                         "top of the closure")
    args = ap.parse_args()

    print(f"=== Design sheets check: {args.design_dir} ===\n")

    netlist_path, netlist_issues = check_netlist(args.design_dir, args.netlist)
    testbench_path, testbench_issues = check_testbench(args.design_dir, netlist_path)
    gds_path, gds_issues = check_gds(args.design_dir)
    pdk_path, pdk_issues = check_pdk(args.pdk)
    target_path, target_issues = check_target_spec(args.design_dir)

    # informational only -- does NOT run a simulation and does NOT affect
    # the exit code below; generating a corrected testbench or running
    # ngspice is a workflow-level action (see SKILL.md's "Testbench
    # data-saving check" step), not something this intake script does.
    output_candidates = guess_output_ports(netlist_path) if netlist_path else []
    saved_candidates = [p for p in output_candidates
                         if testbench_saves_node(testbench_path, p)]

    sections = [
        ("NETLIST (*.sp)", netlist_path, netlist_issues),
        ("TESTBENCH (*.spice)", testbench_path, testbench_issues),
        ("GDS (*.gds)", gds_path, gds_issues),
        (f"PDK ({args.pdk})", pdk_path, pdk_issues),
        ("TARGET SPEC (target_spec.json)", target_path, target_issues),
    ]

    hard_fail = False
    incomplete = False
    for label, found, issues in sections:
        # "MISSING" for a file that is present but could not be picked out
        # of several sends the reader looking for a file that is already
        # on disk -- a different problem with a different fix.
        if found:
            status = "FOUND"
        elif any("cannot identify" in i for i in issues):
            status = "AMBIGUOUS"
        else:
            status = "MISSING"
        print(f"{label}: {status}" + (f" ({found})" if found else ""))
        for issue in issues:
            if issue.startswith("INCOMPLETE"):
                incomplete = True
                print(f"  ! {issue}")
            elif issue.startswith("INFO"):
                print(f"  - {issue}")  # no exit-code impact
            else:
                hard_fail = True
                print(f"  x {issue}")
        print()

    print("OUTPUT NODE SAVE CHECK (informational -- see SKILL.md):")
    if not netlist_path:
        print("  ? skipped -- netlist not resolved above (see NETLIST section)")
    elif not output_candidates:
        print("  ? no obvious output port found in the .subckt port list "
              "(none contain 'out') -- ask the user which port carries the "
              "response to record")
    elif len(output_candidates) > 1:
        print(f"  ? multiple candidate output ports {output_candidates} -- "
              f"ask the user which one to track")
    elif saved_candidates:
        print(f"  + '{saved_candidates[0]}' is written in the testbench's "
              f".control block -- ready for compute_fidelity.py")
    else:
        print(f"  ! '{output_candidates[0]}' looks like the output port but "
              f"the testbench never writes/prints v({output_candidates[0]}) "
              f"-- generate a corrected testbench before simulating "
              f"(see SKILL.md)")
    print()

    if hard_fail:
        print("Result: FAIL -- fix the issues above before starting the outer loop.")
        sys.exit(1)
    if incomplete:
        print("Result: INCOMPLETE -- target_spec.json is a mandatory user file, "
              "and is missing,\n"
              "        unparseable, or not in the form required by\n"
              "        .claude/skills/design-sheets-checker/spec_form_template.md "
              "(one object\n"
              "        per spec: Direction FLOOR/CEILING/RANGE, Value, Units).\n"
              "        Nothing in this flow authors, derives or repairs one. Show "
              "the user the\n"
              "        failing keys above beside that template and ask them to "
              "complete or\n"
              "        correct the file (a file that never went through intake goes "
              "back\n"
              "        through it), then re-run this check until it exits 0. Do NOT "
              "hand off on a 2.")
        sys.exit(2)
    print("Result: PASS -- every required input present and structurally sane.")
    sys.exit(0)


if __name__ == "__main__":
    main()
