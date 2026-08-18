#!/usr/bin/env python3
"""Step 1 of `../SKILL.md`: turn the design's top-level netlist into ONE flat
netlist inside the layout folder, then gate it -- ERC clean AND still
electrically the same circuit as the original.

Three things happen here, in this order, because each one only means
something if the previous one held:

  1. **Flatten.** `subckt_macros.flatten()` expands every top-level
     `.subckt` instance one level, mapping each instance's internal nets to
     real top-level names. A netlist that is already flat (one `.subckt`
     wrapping every device, no instances of local subckts) is copied
     VERBATIM -- `flatten()` returns None there, and rewriting a file that
     needs no rewriting would only invent differences to explain. Either
     way the layout folder ends up with exactly one self-contained netlist,
     which is what every later step reads.
  2. **ERC.** `../../design-sheets-checker/script/run_erc_check.py` on the
     FLAT file, not the original -- flattening is where a net name can
     collide or an instance name can be duplicated, so checking upstream of
     it would check the wrong file. Hard errors fail this step.
  3. **Equivalence.** netgen LVS, flat vs original. Flattening rewrites
     every device line in a decomposed netlist; nothing else in the flow
     would notice if a pin mapping came out wrong, and every downstream
     step (device table, patterns, placement, LVS) would then be built on a
     netlist that is not the circuit that was signed off. This is the check
     that makes "flattened" a claim rather than an assumption.

**Nothing here edits the golden netlist.** The flat file is a derived copy
in the layout folder; the original stays frozen (`../../../../CLAUDE.md`).
That is also what bounds a "fix": see `../SKILL.md`'s Step 1 for which ERC
findings may be repaired in the flat copy (a syntax/unit/naming defect that
equivalence still holds across) and which must go back to
`../../../agents/schematic-agent.md` instead (anything that changes
connectivity, device count or sizing).

The only file this leaves in the layout folder is the flat netlist itself
(plus netgen's own log under `equivalence/`). The ERC findings and the verdict
are printed, not written -- a report file nothing downstream reads is just
another thing to go stale. `--json PATH` is there for a caller that does want
the summary machine-readable.

Usage:
  python flatten_netlist.py <netlist.sp> [--layout-dir <design_dir>/layout]
      [--out NAME] [--top-subckt NAME] [--no-equivalence] [--json report.json]

Exit codes: 0 = flat netlist is ERC-clean and equivalent; 2 = ERC warnings
only (review, then proceed); 1 = ERC hard errors, an equivalence mismatch,
or a flatten failure -- Step 2 must not run.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE.parent.parent
REFERENCE_DIR = SKILLS_DIR.parent / "reference"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SKILLS_DIR / "schematic-sizing" / "script"))
sys.path.insert(0, str(REFERENCE_DIR))

import netlist_devices as devparse   # noqa: E402
import subckt_macros                 # noqa: E402

ERC_SCRIPT = SKILLS_DIR / "design-sheets-checker" / "script" / "run_erc_check.py"

# `.include`/`.inc` with the included path as the last token. Rewritten to an
# absolute path when the original netlist is copied into the equivalence work
# dir -- a relative include is resolved against the INCLUDING file's own
# directory, so a copy placed elsewhere would silently lose whatever it
# includes (and netgen would then compare against a circuit missing devices,
# reporting a mismatch that is an artifact of the copy).
_INCLUDE_RE = re.compile(r'^(\s*\.inc(?:lude)?\s+)(["\']?)([^"\'\s]+)(["\']?)\s*$',
                         re.IGNORECASE)


def _netgen_binary():
    """netgen's real path -- `$NETGEN`, else the documented
    `~/.local/bin/netgen` (`../../../reference/environment.md`), else
    whatever is on PATH. Returns None if none of those exist, so the
    equivalence check can report "not run" instead of crashing."""
    for cand in (os.environ.get("NETGEN"),
                 str(Path.home() / ".local" / "bin" / "netgen"),
                 shutil.which("netgen")):
        if cand and Path(cand).exists():
            return cand
    return None


def _netgen_setup():
    """The active PDK's netgen setup script, from
    `../../../reference/pdk_options.json` -- not a hardcoded sky130 path."""
    try:
        from pdk_config import pdk
        return pdk().tool("netgen_setup")
    except Exception:
        return None


def top_subckt_name(text: str):
    for line in text.splitlines():
        m = re.match(r"\s*\.subckt\s+(\S+)", line, re.I)
        if m:
            return m.group(1)
    return None


def circuit_size(path: Path):
    """(device count, net count) as this project's own netlist parser sees
    them. Printed for both files whatever netgen does, so a gross flattening
    loss (the failure mode that matters -- devices inside a subckt silently
    dropped) is visible even when netgen cannot run."""
    devices = devparse.parse_devices(Path(path).read_text())
    nets = {n for d in devices for n in d["nets"]}
    return len(devices), len(nets)


def flatten_to(netlist_path: Path, layout_dir: Path, out_name=None,
               top_subckt=None):
    """Write the single flat netlist into `layout_dir` and return
    (path, info)."""
    layout_dir.mkdir(parents=True, exist_ok=True)
    out_path = layout_dir / (out_name or f"{netlist_path.stem}_flat.sp")

    flat = subckt_macros.flatten(netlist_path, top_subckt=top_subckt)
    if flat is None:
        # Already flat: copy verbatim, including comments and the header
        # block, so the flat netlist stays diffable against the original.
        shutil.copyfile(netlist_path, out_path)
        info = {"mode": "already_flat", "expanded_instances": [],
                "nested_unexpanded": [], "top_subckt": top_subckt_name(
                    netlist_path.read_text())}
    else:
        out_path.write_text(flat.flat_text())
        info = {
            "mode": "flattened",
            "expanded_instances": [
                {"instance": g["instance"], "subckt": g["subckt"],
                 "devices": g["devices"]} for g in flat.groups],
            "nested_unexpanded": flat.nested,
            "top_subckt": flat.top_subckt,
        }
    info["path"] = str(out_path)
    return out_path, info


def run_erc(flat_path: Path, layout_dir: Path):
    """Full ERC pass on the flat netlist. Returns the counts, the findings
    themselves, and the raw report text (printed in full by the caller -- the
    `fix:` strings are the useful part, so they are never summarized away).

    The machine-readable findings come from a JSON file written into a TEMP
    dir and read straight back, so the layout folder keeps only artifacts a
    later step actually consumes -- the report itself is the terminal
    output."""
    with tempfile.TemporaryDirectory() as tmp:
        json_path = Path(tmp) / "erc.json"
        proc = subprocess.run(
            [sys.executable, str(ERC_SCRIPT), str(flat_path), "--json", str(json_path)],
            capture_output=True, text=True)
        findings = []
        if json_path.exists():
            try:
                findings = json.loads(json_path.read_text()).get("findings", [])
            except json.JSONDecodeError:
                pass
    counts = {sev: sum(1 for f in findings if f["severity"] == sev)
              for sev in ("error", "warning", "info")}
    # Drop the checker's own "Saved: <path>" line: that path is the temp file
    # above, and printing it would advertise an artifact that no longer exists
    # by the time anyone reads the output.
    report = "\n".join(line for line in (proc.stdout + proc.stderr).splitlines()
                       if not line.startswith("Saved:"))
    return {"exit_code": proc.returncode, "report": report,
            "counts": counts, "findings": findings}


def _copy_with_absolute_includes(src: Path, dst: Path):
    out = []
    for line in src.read_text().splitlines():
        m = _INCLUDE_RE.match(line)
        if m:
            inc = Path(m.group(3))
            if not inc.is_absolute():
                inc = (src.parent / inc).resolve()
            line = f"{m.group(1)}{m.group(2)}{inc}{m.group(4)}"
        out.append(line)
    dst.write_text("\n".join(out) + "\n")


def check_equivalence(original: Path, flat: Path, layout_dir: Path):
    """netgen LVS, flat netlist vs original. Returns a verdict dict; a
    missing netgen or setup script is reported as `status: "skipped"`, never
    silently treated as a pass."""
    netgen = _netgen_binary()
    setup = _netgen_setup()
    work = layout_dir / "equivalence"
    work.mkdir(parents=True, exist_ok=True)

    top_orig = top_subckt_name(original.read_text())
    top_flat = top_subckt_name(flat.read_text())
    if not top_orig or not top_flat:
        return {"status": "skipped",
                "reason": f"no .subckt line to compare (original top="
                          f"{top_orig!r}, flat top={top_flat!r}) -- netgen "
                          f"compares named subcircuits"}
    if netgen is None:
        return {"status": "skipped", "reason": "netgen not found ($NETGEN, "
                "~/.local/bin/netgen, PATH) -- see "
                "reference/environment.md"}
    if not setup or not Path(setup).exists():
        return {"status": "skipped",
                "reason": f"the active PDK's netgen setup script is missing "
                          f"({setup}) -- see reference/pdk_options.json"}

    # netgen rejects a `.sp` extension outright ("don't know type of file"),
    # so both sides are copied to `.spice` first -- reference/environment.md.
    orig_spice = work / "original.spice"
    flat_spice = work / "flattened.spice"
    _copy_with_absolute_includes(original, orig_spice)
    _copy_with_absolute_includes(flat, flat_spice)
    report = work / "netgen_equivalence.out"

    env = dict(os.environ)
    try:
        from pdk_config import pdk
        env.setdefault("PDK_ROOT", pdk().pdk_root)
    except Exception:
        pass

    cmd = [netgen, "-batch", "lvs",
           f"{flat_spice} {top_flat}", f"{orig_spice} {top_orig}",
           str(setup), str(report)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(work))
    text = (report.read_text() if report.exists() else "") + proc.stdout + proc.stderr
    if "match uniquely" in text:
        status = "match"
    elif re.search(r"Circuits match correctly|Netlists match", text):
        status = "match"
    else:
        status = "mismatch"
    return {"status": status, "report": str(report), "command": " ".join(cmd),
            "top_flat": top_flat, "top_original": top_orig,
            "netgen_exit": proc.returncode,
            "tail": "\n".join(text.strip().splitlines()[-25:])}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist", help="the design's top-level (frozen) netlist")
    ap.add_argument("--layout-dir", default=None,
                    help="default: <netlist_dir_or_design_dir>/layout -- Step 0's folder")
    ap.add_argument("--out", default=None,
                    help="flat netlist filename inside the layout dir "
                         "(default: <stem>_flat.sp)")
    ap.add_argument("--top-subckt", default=None,
                    help="which .subckt is the top-level circuit, when more "
                         "than one is never instantiated")
    ap.add_argument("--no-equivalence", action="store_true",
                    help="skip the netgen flat-vs-original comparison "
                         "(reported as skipped, and the step still gates on ERC)")
    ap.add_argument("--json", default=None, help="also write the summary as JSON")
    args = ap.parse_args()

    netlist = Path(args.netlist).resolve()
    if not netlist.exists():
        sys.exit(f"no such netlist: {netlist}")
    if args.layout_dir:
        layout_dir = Path(args.layout_dir).resolve()
    else:
        # `<design>/netlist/foo.sp` and `<design>/sizing/foo_final.sp` both
        # want `<design>/layout`, not `<design>/netlist/layout`.
        parent = netlist.parent
        base = parent.parent if parent.name in ("netlist", "sizing",
                                                "device_shaping") else parent
        layout_dir = base / "layout"

    print(f"=== Step 1: flatten {netlist.name} -> {layout_dir} ===")
    flat_path, finfo = flatten_to(netlist, layout_dir, out_name=args.out,
                                  top_subckt=args.top_subckt)
    print(f"  mode: {finfo['mode']}  top .subckt: {finfo['top_subckt']}")
    for g in finfo["expanded_instances"]:
        print(f"  [{g['instance']}] {g['subckt']}: {g['devices']}")
    if finfo["nested_unexpanded"]:
        print(f"  WARNING: nested subckt instance(s) inside "
              f"{finfo['nested_unexpanded']} left unexpanded -- their devices "
              f"keep LOCAL net names")
    size_orig, size_flat = circuit_size(netlist), circuit_size(flat_path)
    print(f"  devices/nets: original {size_orig[0]}/{size_orig[1]}, "
          f"flat {size_flat[0]}/{size_flat[1]}")
    print(f"  wrote {flat_path}")

    print(f"\n=== ERC on the flat netlist ===")
    erc = run_erc(flat_path, layout_dir)
    print(erc["report"].rstrip())

    print(f"\n=== Equivalence: flat vs original (netgen) ===")
    if args.no_equivalence:
        eq = {"status": "skipped", "reason": "--no-equivalence"}
    else:
        eq = check_equivalence(netlist, flat_path, layout_dir)
    print(f"  status: {eq['status'].upper()}"
          + (f" -- {eq['reason']}" if eq.get("reason") else ""))
    if eq.get("tail") and eq["status"] != "match":
        print("  netgen tail:")
        for line in eq["tail"].splitlines():
            print(f"    {line}")

    n_err = erc["counts"]["error"]
    n_warn = erc["counts"]["warning"]
    if n_err or eq["status"] == "mismatch":
        verdict, code = "FAIL -- do not run Step 2", 1
    elif n_warn or eq["status"] == "skipped":
        verdict, code = "PASS with warnings -- review before Step 2", 2
    else:
        verdict, code = "PASS -- ERC clean and equivalent", 0

    # No report file: the printout above IS the report, and the only artifact
    # a later step consumes is the flat netlist itself. `--json` stays for a
    # caller that wants the same summary machine-readable, at a path it picks.
    if args.json:
        Path(args.json).write_text(json.dumps(
            {"original": str(netlist), "flatten": finfo,
             "erc": {k: v for k, v in erc.items() if k != "report"},
             "equivalence": eq, "size_original": size_orig,
             "size_flat": size_flat, "verdict": verdict}, indent=2))
        print(f"\n  wrote {args.json}")
    print(f"\nVerdict: {verdict}")
    sys.exit(code)


if __name__ == "__main__":
    main()
