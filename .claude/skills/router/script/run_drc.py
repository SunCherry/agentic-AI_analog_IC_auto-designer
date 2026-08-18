#!/usr/bin/env python3
"""Run Magic DRC on `route_nets.py`'s output GDS -- the last step of this
skill, a real DRC-plausibility check on the actual routed geometry (not
just this router's own printed "Congestion check").

Reuses `../../../reference/environment.md`'s "Magic DRC" section's exact
validated Tcl pattern verbatim (`drc on` + `drc catchup` before `drc
check`/`drc list count`, `expand` before checking, `drc listall why`
cross-checked against `DRC_TOTAL` before trusting a "0 violations"
claim) -- the same pattern `../../../agents/layout-fixer.md` follows for
every other GDS in this project. Not re-derived here; if that pattern
ever changes, update it there and this script picks it up by staying
textually identical.

Real DRC on this router's output can find genuine violations even when
`route_nets.py` reported a clean "Congestion check: PASS" -- that check
only verifies capacity-1 grid-cell sharing and macro-box avoidance, not
the active PDK's real spacing/width/enclosure rules on the rendered
wire/via polygons. Treat this DRC pass as authoritative over the router's own
summary, same as layout-fixer's DRC gate is authoritative over
layout-agent's own claims elsewhere in this project.

Usage:
  python run_drc.py <routed.gds> [--top <GDS filename stem>]
      [--work-dir <gds's dir>/drc_work]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# The active process and its magicrc come from
# `.claude/reference/pdk_options.json`, never from a hardcoded `sky130A` --
# the project's PDK is a project-wide setting, so retargeting is the one-word
# edit to that file's `selected` key (same accessor `route_nets.py` uses).
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "reference"))
from pdk_config import pdk as pdk_option  # noqa: E402

PDK_CFG = pdk_option()
PDK_ROOT = os.environ.get("PDK_ROOT") or PDK_CFG.pdk_root or str(Path.home() / "pdk" / "manual")
# The Magic BINARY is a machine fact, not a PDK one -- see
# ../../../reference/environment.md's "Paths". That documented location wins
# so this machine stays reproducible, but fall back to PATH rather than dying
# with a bare FileNotFoundError traceback on a host that installed Magic
# somewhere else.
MAGIC = "/usr/local/bin/magic"
if not os.path.exists(MAGIC):
    MAGIC = shutil.which("magic") or MAGIC
MAGICRC = PDK_CFG.tool("magicrc")

# Magic internal units -> um. See environment.md's "Magic DRC".
MAGIC_UNIT_UM = 0.005
# How many coordinates to print per rule before deferring to the JSON file.
COORD_PRINT_LIMIT = 5


def check_tools():
    """Refuse to start without the tools, naming what is missing.

    The whole point of this step is that a DRC verdict can be trusted, so a
    missing binary or magicrc must read as "could not check", never as a
    silent skip or a confusing crash."""
    if not (os.path.exists(MAGIC) or shutil.which(MAGIC)):
        sys.exit(f"magic not found (looked at /usr/local/bin/magic and on PATH) -- "
                 f"see ../../../reference/environment.md's \"Paths\"")
    if not MAGICRC or not os.path.exists(MAGICRC):
        sys.exit(f"magicrc for {PDK_CFG.name} not found at {MAGICRC!r} -- check "
                 f"pdk_options.json's tools.magicrc and PDK_ROOT={PDK_ROOT!r}")

TCL_TEMPLATE = """gds read {gds_path}
load {top_cell}
select top cell
drc euclidean on
drc style drc(full)
drc on
select top cell
expand
drc check
drc catchup
set n [drc list count total]
puts "DRC_TOTAL: $n"
foreach pair [drc listall count] {{ puts "CELLCOUNT: [lindex $pair 0] [lindex $pair 1]" }}
set res [drc listall why]
foreach {{rule coords}} $res {{
  puts "RULE: ([llength $coords]) $rule"
  foreach c $coords {{ puts "COORD: $rule @ $c" }}
}}
quit -noprompt
"""


def run_magic_drc(gds_path: Path, top_cell: str, work_dir: Path):
    work_dir.mkdir(parents=True, exist_ok=True)
    tcl_path = work_dir / "drc.tcl"
    tcl_path.write_text(TCL_TEMPLATE.format(gds_path=gds_path, top_cell=top_cell))

    env = dict(os.environ)
    env["PDK_ROOT"] = PDK_ROOT
    result = subprocess.run(
        [MAGIC, "-dnull", "-noconsole", "-rcfile", MAGICRC, str(tcl_path)],
        cwd=str(work_dir), env=env, capture_output=True, text=True, timeout=600,
    )
    log_path = work_dir / "drc.log"
    log_path.write_text(result.stdout + "\n" + result.stderr)
    return result.stdout, log_path


def hierarchical_error_count(stdout: str, top_cell: str):
    """The real error count across the WHOLE cell hierarchy, from `drc
    listall count`'s per-cell numbers.

    **`drc list count total` alone is not it, and can read 0 on a layout
    with hundreds of real violations** -- confirmed directly, not a
    theoretical worry: Magic reported `DRC_TOTAL: 0` for
    `current_mirror_XMN4_XMN3.gds` while `drc listall count` reported 118
    errors in its own child cell (`current_mirror_49d776e9`) and `drc
    listall why` listed a real licon.2 rule. glayout wraps every generated
    macro in an outer `Component(name=...)` whose own top level holds no
    geometry, so all the real violations live one level down and the top
    cell's count is legitimately zero.

    Magic rolls a parent cell's count up to include its children (verified
    on the same design's assembled `placement_visualization.gds`: top cell
    `placement_visualization` = 120 = the nfet mirror's 118 + the pfet
    mirror's 2). So: use the top cell's own entry when Magic emitted one,
    else sum the per-cell entries, which are then distinct subtrees."""
    counts = {cell: int(n) for cell, n in re.findall(r"CELLCOUNT:\s*(\S+)\s+(\d+)", stdout)}
    if not counts:
        return 0, counts
    if top_cell in counts:
        return counts[top_cell], counts
    return sum(counts.values()), counts


def parse_drc(stdout: str):
    total_match = re.search(r"DRC_TOTAL:\s*(\d+)", stdout)
    total = int(total_match.group(1)) if total_match else None
    # "RULE: (<n>) <rule name>" -- <n> is the coordinate-list length for
    # that rule from `drc listall why`, the authoritative per-rule listing
    # (see environment.md: cross-check this against DRC_TOTAL, don't trust
    # DRC_TOTAL alone).
    rules = [(name.strip(), int(count)) for count, name in
              re.findall(r"RULE:\s*\((\d+)\)\s*(.+)", stdout)]
    warnings = [line for line in stdout.splitlines() if "Unknown layer/datatype" in line]
    # "COORD: <rule> @ <llx> <lly> <urx> <ury>" -- Magic emits DRC coordinates in
    # INTERNAL units; x0.005 gives um (../../../reference/environment.md).
    # Without these the caller can name a violated rule but cannot say WHERE,
    # which is exactly what ../../../agents/layout-fixer.md's attribution step
    # needs -- it previously had to hand-write its own Tcl to recover them.
    coords = {}
    for m in re.finditer(r"COORD:\s*(.+?)\s+@\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)", stdout):
        rule = m.group(1).strip()
        box = tuple(round(int(v) * MAGIC_UNIT_UM, 4) for v in m.groups()[1:])
        coords.setdefault(rule, []).append(box)
    return total, rules, warnings, coords


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("gds", help="routed GDS, e.g. <design_dir>/routed.gds")
    parser.add_argument("--top", default=None, help="top cell name -- default: GDS filename stem")
    parser.add_argument("--work-dir", default=None, help="default: <gds's dir>/drc_work")
    args = parser.parse_args()

    check_tools()
    gds_path = Path(args.gds).resolve()
    if not gds_path.exists():
        sys.exit(f"no such file: {gds_path}")
    top_cell = args.top or gds_path.stem
    # Magic's `load <name>` silently CREATES an empty cell when <name>
    # isn't in the layout, and DRC on an empty cell reports 0 errors --
    # a false PASS with no warning anywhere. That is not hypothetical:
    # this defaults `--top` to the FILE STEM, but every routed GDS this
    # project writes has top cell "routed", so DRC'ing a copy saved under
    # any other filename (`r_e.gds`, a sweep's `vc6_hs15.gds`, ...)
    # checked nothing at all and printed "PASS (clean)". Verify up front
    # instead, and say what the real top cells are.
    try:
        import gdstk
        cells = {c.name for c in gdstk.read_gds(str(gds_path)).cells}
        tops = [c.name for c in gdstk.read_gds(str(gds_path)).top_level()]
        if top_cell not in cells:
            sys.exit(f"cell {top_cell!r} is not in {gds_path.name} -- DRC would silently check an "
                     f"EMPTY cell and report a false PASS. Top-level cell(s) present: {tops}. "
                     f"Re-run with --top <name>.")
    except ImportError:
        print("  warning: gdstk unavailable -- cannot verify that the top cell exists; a wrong "
              "--top would make Magic check an empty cell and report a false PASS")
    work_dir = Path(args.work_dir).resolve() if args.work_dir else gds_path.parent / "drc_work"
    coords_path = work_dir / "drc_violations.json"

    print(f"=== Magic DRC: {gds_path.name} (top cell {top_cell!r}) ===")
    stdout, log_path = run_magic_drc(gds_path, top_cell, work_dir)
    total, rules, warnings, coords = parse_drc(stdout)

    if total is None:
        print(f"  ERROR: no DRC_TOTAL found in Magic's output -- Magic likely crashed or the "
              f"script didn't run to completion. Full log: {log_path}")
        sys.exit(2)

    # DRC_TOTAL alone isn't trusted -- cross-check against the real
    # per-rule listing, exactly as environment.md warns.
    rules_total = sum(c for _, c in rules)
    hier_total, cell_counts = hierarchical_error_count(stdout, top_cell)
    print(f"  DRC errors (per-cell, from `drc listall count`): {hier_total}")
    if total != hier_total:
        # Not a discrepancy to paper over: `drc list count total` reports
        # only the loaded cell's own count, which is 0 for every
        # glayout-generated macro (all geometry sits in a child cell).
        # See hierarchical_error_count()'s docstring.
        print(f"    (note: `drc list count total` reported {total} for the top cell "
              f"{top_cell!r} alone -- the per-cell numbers above see more)")
    if cell_counts:
        print("  Errors by cell: " + ", ".join(f"{c}={n}" for c, n in sorted(cell_counts.items())))
    print(f"  Cross-check: {len(rules)} distinct violated rule(s), {rules_total} instance(s) "
          f"from `drc listall why`")
    if hier_total > 0 and not rules:
        # `drc listall count` also counts errors a cell has when checked ON
        # ITS OWN, which is not the same question as "is the assembled
        # layout legal". A cell can be illegal in isolation and perfectly
        # legal in place, because the geometry that satisfies the rule
        # lives in its parent. **Confirmed on this project, not assumed**:
        # the reference design's routed GDS reported 1 error in each of 3
        # `via_stack_*` cells while `drc listall why` on the expanded top
        # cell said "No errors found"; loading one of those cells alone
        # showed the rule -- "Local interconnect minimum area < 0.0561um^2
        # (li.6)" -- i.e. the via's own li island is under-area by itself
        # but merges with the parent's wire once placed. Flattening the
        # whole routed GDS and re-running DRC on it (no hierarchy left at
        # all) reported 0 errors and 0 rules, settling it.
        # So: `drc listall why` on the expanded top cell is the authority
        # for the ASSEMBLED layout, and that is what the verdict uses. The
        # per-cell counts stay visible above because they are still the
        # thing that catches real child-cell violations (they are what
        # exposed 118 licon.2 errors that `drc list count total` reported
        # as 0) -- but there, `drc listall why` reported them too.
        print(f"  NOTE: {hier_total} error(s) are counted in subcells but `drc listall why` "
              f"reports none in the expanded top cell -- these are out-of-context subcell "
              f"errors (geometry completing them lives in the parent), not violations of the "
              f"assembled layout. Verify by flattening if in doubt.")
    if rules:
        print("  Violations by rule:")
        for name, count in rules:
            print(f"    {count:4d}  {name}")
            # WHERE, not just what -- the attribution any fix starts from.
            for box in coords.get(name, [])[:COORD_PRINT_LIMIT]:
                print(f"          at ({box[0]}, {box[1]}) .. ({box[2]}, {box[3]}) um")
            extra = len(coords.get(name, [])) - COORD_PRINT_LIMIT
            if extra > 0:
                print(f"          (+{extra} more -- full list in {coords_path.name})")
    # Written on EVERY run, clean or not. It used to be written only when
    # `coords` was non-empty, so a clean layout produced no file at all and
    # a downstream consumer (layout-fixer categorizes violations from it)
    # could not distinguish "no violations" from "DRC never ran" without
    # re-parsing the log. An empty `violations` dict says the first
    # unambiguously.
    with open(coords_path, "w") as fh:
        json.dump({"gds": str(gds_path), "top_cell": top_cell,
                   "units": "um", "drc_total": total,
                   "violations": coords}, fh, indent=2)
    if coords:
        print(f"  violation coordinates: {coords_path}")
    else:
        print(f"  no violations -- empty report written to {coords_path}")
    if warnings:
        print(f"  ({len(warnings)} expected/harmless 'Unknown layer/datatype' GDS-read warning(s) "
              f"-- glayout's internal pwell mapping, see ../../../reference/environment.md, not a "
              f"real DRC issue)")
    print(f"  full Magic log: {log_path}")

    # `drc listall why` on the EXPANDED top cell is the authority for the
    # assembled layout (see the NOTE branch above for why the per-cell
    # counts alone are not). DRC_TOTAL is kept in the condition only as a
    # cheap extra guard; it never carries the verdict by itself, since it
    # legitimately reads 0 on a macro whose geometry all sits in a child.
    clean = (total == 0) and not rules
    print(f"  DRC check: {'PASS (clean)' if clean else 'FAIL -- see violations above'}")
    if not clean:
        sys.exit(1)


if __name__ == "__main__":
    main()
