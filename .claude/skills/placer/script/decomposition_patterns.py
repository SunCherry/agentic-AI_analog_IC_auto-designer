#!/usr/bin/env python3
"""Read `<design_dir>/circuit_decomposition.yaml` and hand
`generate_primitives.py` the circuit's recognized patterns.

This is the placer's ONLY source of subcircuit grouping. It replaces the
structural re-detection the placer used to run itself
(`sub-layout-handler/script/detect_topology.py`'s `detect()`): that scan and
this file answer the same question, and `circuit_decomposition.yaml` answers
it better --

  - it is user-confirmed (`confirmed_by_user`), where a re-run detector is a
    fresh guess nobody reviewed;
  - it carries an explicit per-device `role` (`reference` / `leg` / `half`),
    so "which device is the mirror reference" is read, not re-derived from
    diode-connection heuristics;
  - it covers patterns no detector matches -- passives especially. R and C
    never reached `detect_topology.py` at all, so a compensation network was
    invisible to grouping;
  - it states `unmatched_devices` as an authored conclusion, so "nothing
    matched here" is a decision rather than a silence.

**No yaml, no patterns.** There is no fallback detector any more: a design
without the file gets one standalone primitive per device (what
`--per-device` does explicitly) and a loud warning. That is a real floorplan
-- the annealer and router still place and connect everything -- it just
carries no mirror/pair matching, so run `../../circuit-decomposition/SKILL.md`
first whenever matching matters.

Findings are emitted in the exact dict shape `generate_primitives.py` already
consumes -- `{topology, devices, detail, doc}` with the mirror REFERENCE
first, since `gen_current_mirror()`/`gen_diff_pair()` index `devices`
positionally.
"""
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE.parent.parent

YAML_NAME = "circuit_decomposition.yaml"

# Patterns with a dedicated generator in `cells/blocks/`. Anything else the
# yaml names is reported for manual composition, never silently dropped.
MACRO_TOPOLOGIES = {"current_mirror", "differential_pair"}

# How many devices each buildable generator actually needs, as (min, max).
# `generate_primitives.py` indexes `devices` POSITIONALLY -- a mirror is
# `devices[0]` plus `devices[1:]` legs, a diff pair unpacks to exactly two
# halves -- so a yaml naming the wrong count used to raise a bare IndexError
# / "not enough values to unpack" mid-run instead of the warn-and-continue
# every other yaml/netlist disagreement here gets. Checked, warned about, and
# the pattern falls through to per-device primitives + the manual-composition
# list (see `load()` and generate_primitives.py's dispatch).
MACRO_DEVICE_COUNTS = {"current_mirror": (2, None), "differential_pair": (2, 2)}


def buildable_label(pattern: str, names: list) -> str:
    """"macro" only if this pattern has a generator AND a device count that
    generator can take -- the same two conditions
    `generate_primitives.py`'s dispatch applies, so the printed plan can't
    promise a macro the run then falls back on."""
    if pattern not in MACRO_TOPOLOGIES:
        return "manual composition"
    return "manual composition (bad device count)" if macro_arity_error(pattern, names) else "macro"


def macro_arity_error(pattern: str, names: list) -> str | None:
    """Why `pattern` can't be built from `names`, or None if it can."""
    bounds = MACRO_DEVICE_COUNTS.get(pattern)
    if bounds is None:
        return None
    lo, hi = bounds
    if len(names) < lo:
        return f"needs at least {lo} devices, the yaml names {len(names)} ({names})"
    if hi is not None and len(names) > hi:
        return f"takes exactly {hi} devices, the yaml names {len(names)} ({names})"
    return None

# Roles that identify the device a mirror is referenced FROM. `cells/blocks/
# current_mirror.py` builds the reference from `devices[0]`, so this ordering
# is load-bearing, not cosmetic.
REFERENCE_ROLES = {"reference", "ref", "master", "diode", "diode_connected"}

_DOC_DIR = SKILLS_DIR.parent / "reference" / "circuit-specific"


def _doc_for(pattern: str) -> str:
    """Path to the pattern's manual-composition write-up, if one exists."""
    slug = pattern.replace("_", "-")
    d = _DOC_DIR / slug
    if d.is_dir():
        md = sorted(d.glob("*.md"))
        if md:
            return f"circuit-specific/{slug}/{md[0].name}"
        return f"circuit-specific/{slug}/"
    return "../../circuit-decomposition/pattern-table.md"


def find_yaml(netlist_path: Path, design_dir: Path) -> Path | None:
    """`<design_dir>/circuit_decomposition.yaml`, else the netlist's own
    directory, else its parent -- covering the common
    `<design_dir>/netlist/<top>.sp` layout where the yaml sits one level up
    beside the design rather than beside the netlist."""
    netlist_path = Path(netlist_path).resolve()
    for cand in (Path(design_dir) / YAML_NAME,
                 netlist_path.parent / YAML_NAME,
                 netlist_path.parent.parent / YAML_NAME):
        if cand.is_file():
            return cand.resolve()
    return None


def _device_refs(entry) -> list:
    """A pattern's `devices:` is a list of `{ref, role, ...}` mappings, but
    tolerate a bare list of names -- an author writing the file by hand
    reasonably shortens it that way, and losing a whole pattern over the
    difference would be silent data loss."""
    out = []
    for d in entry.get("devices") or []:
        if isinstance(d, dict):
            ref = d.get("ref") or d.get("name")
            if ref:
                out.append((str(ref), str(d.get("role") or "")))
        else:
            out.append((str(d), ""))
    return out


def _ordered(refs: list, pattern: str) -> list:
    """Reference-first for a mirror; yaml order otherwise (a diff pair's two
    halves are interchangeable by definition)."""
    if pattern != "current_mirror":
        return [r for r, _role in refs]
    ref_first = [r for r, role in refs if role.lower() in REFERENCE_ROLES]
    rest = [r for r, role in refs if role.lower() not in REFERENCE_ROLES]
    return ref_first + rest


def load(yaml_path: Path, dev_by_name: dict):
    """Returns `(findings, unmatched_names, warnings)`.

    `dev_by_name` is the netlist's own device table. Every pattern is
    reconciled against it: a `ref` the netlist doesn't have is a real
    inconsistency between the two files (a stale yaml, or a renamed device),
    reported as a warning rather than crashing or -- worse -- building a
    macro from a device that isn't there.
    """
    warnings = []
    data = yaml.safe_load(Path(yaml_path).read_text()) or {}

    if not data.get("confirmed_by_user"):
        warnings.append(
            f"{yaml_path.name}: confirmed_by_user is not true -- the circuit read "
            f"has not been signed off, so the grouping below is provisional")

    patterns = data.get("patterns") or []
    if not patterns:
        warnings.append(
            f"{yaml_path.name}: `patterns` is empty. Per circuit-decomposition/SKILL.md "
            f"that means its judgment steps have NOT run yet -- empty is 'not done', "
            f"never 'none found'. Every device falls back to a standalone primitive.")

    findings = []
    claimed = set()
    for entry in patterns:
        pattern = str(entry.get("pattern") or "").strip()
        refs = _device_refs(entry)
        if not pattern or not refs:
            continue
        pid = entry.get("id") or pattern
        names = _ordered(refs, pattern)

        missing = [n for n in names if n not in dev_by_name]
        if missing:
            warnings.append(
                f"{yaml_path.name}: pattern {pid} names device(s) {missing} that the "
                f"netlist does not contain -- pattern skipped. The yaml and the netlist "
                f"disagree; re-run circuit-decomposition against THIS netlist.")
            continue

        arity = macro_arity_error(pattern, names)
        if arity:
            warnings.append(
                f"{yaml_path.name}: {pattern} {pid} {arity} -- it cannot be built as one "
                f"matched macro, so its devices fall back to standalone primitives and the "
                f"pattern is listed for manual composition. Fix the device list in the yaml "
                f"if this is a real {pattern}.")

        if pattern == "current_mirror" and not any(
                role.lower() in REFERENCE_ROLES for _r, role in refs):
            warnings.append(
                f"{yaml_path.name}: current_mirror {pid} marks no device `role: reference` "
                f"-- using yaml order, so {names[0]} is drawn as the reference. Check that "
                f"against the netlist's diode-connected device.")

        claimed.update(names)
        findings.append({
            "topology": pattern,
            "devices": names,
            "detail": (f"{yaml_path.name} pattern {pid} "
                       f"(confidence: {entry.get('confidence', 'unstated')})"
                       + (f" -- {entry['matched_by']}" if entry.get("matched_by") else "")),
            "doc": _doc_for(pattern),
            "pattern_id": pid,
        })

    # A mirror family and a stacked-diode pattern can legitimately name the
    # same device (the yaml's own `partner_device` case), but two BUILDABLE
    # macros claiming one device would draw it twice.
    seen_in_macro = {}
    for f in findings:
        if f["topology"] not in MACRO_TOPOLOGIES:
            continue
        for n in f["devices"]:
            if n in seen_in_macro:
                warnings.append(
                    f"{yaml_path.name}: device {n} is claimed by both {seen_in_macro[n]} "
                    f"and {f['pattern_id']}, and both are buildable macros -- it would be "
                    f"drawn twice. Resolve in the yaml before trusting this layout.")
            else:
                seen_in_macro[n] = f["pattern_id"]

    # Authored `unmatched_devices` plus anything the file simply never
    # mentions -- both end up as standalone primitives, but only the second
    # is a gap worth reporting.
    authored = []
    for u in data.get("unmatched_devices") or []:
        authored.append(str(u.get("ref") or u.get("name")) if isinstance(u, dict) else str(u))
    silent = [n for n in dev_by_name if n not in claimed and n not in authored]
    if silent:
        warnings.append(
            f"{yaml_path.name}: {len(silent)} device(s) appear in neither `patterns` nor "
            f"`unmatched_devices`: {silent}. circuit-decomposition/SKILL.md requires every "
            f"device to appear exactly once, so this file is out of step with the netlist.")

    unmatched = [n for n in dev_by_name if n not in claimed]
    return findings, unmatched, warnings


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("yaml_path", help=f"a {YAML_NAME}")
    args = parser.parse_args()

    path = Path(args.yaml_path).resolve()
    data = yaml.safe_load(path.read_text()) or {}
    # Standalone preview: reconcile the file against ITSELF (every ref it
    # names), since no netlist is given. `load()`'s netlist cross-checks --
    # missing refs, silently omitted devices -- therefore can't fire here;
    # run `generate_primitives.py` for those.
    refs = {r for e in (data.get("patterns") or []) for r, _ in _device_refs(e)}
    findings, _unmatched, warnings = load(path, {r: {} for r in refs})
    print(f"=== {path} ===")
    for f in findings:
        print(f"  [{f['topology']}] {f['pattern_id']}: {f['devices']}  -> "
              f"{buildable_label(f['topology'], f['devices'])}")
    authored = data.get("unmatched_devices") or []
    print(f"  yaml `unmatched_devices`: {authored or '[]'}")
    for w in warnings:
        print(f"  WARNING: {w}")


if __name__ == "__main__":
    main()
