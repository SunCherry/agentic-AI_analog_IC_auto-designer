#!/usr/bin/env python3
"""Generate ngspice `let`/`print` op-point-probe lines for every MOS
device in a netlist, to be inserted into a testbench's `.control` block
right after `run` -- one `let <attr>_<name> = @m....[<attr>]` line per
attribute per device.

The hierarchical instance path and the PDK-specific op-info instance suffix are
both DERIVED rather than configured: the suffix is the sky130-vs-gf180 quirk
(model name vs the fixed `m0`) and comes from `PDK_CONFIGS` (imported below),
so neither PDK's convention is assumed everywhere.

Hierarchical path: the validated testbench pattern always
wraps the golden netlist as `X0 ... <design_subckt>` inside a
`.subckt <wrapper> ...` block, itself instantiated at the top level as
`x<wrapper> (...) <wrapper>`. `detect_hier_prefix()` parses exactly that
pattern to build `x<wrapper>.x0.` -- pass `--hier-prefix` explicitly for
a testbench that doesn't follow it (never silently guessed wrong).

Usage:
  python generate_op_probe.py <netlist.sp> <testbench.spice> --pdk sky130A
      [--hier-prefix xwrapper.x0.] [--attrs gm,gds,id,vds,vgs,vdsat,cgg]
      --out-txt opinfo.txt
  -> prints the `let ...` / `print ... > opinfo.txt` lines to stdout
     (redirect into a file to `.include`, or splice into the testbench's
     `.control` block -- see run_sizing_iteration.py, which does this
     automatically).
"""
import argparse
import os
import re
import sys

from netlist_devices import parse_devices

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "..", "pdk-characterization", "script"))
from characterize_gmid import PDK_CONFIGS  # noqa: E402

DEFAULT_ATTRS = ["gm", "gds", "id", "vds", "vgs", "vdsat", "cgg"]


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


def detect_hier_prefix(testbench_text):
    """Return 'x<wrapper>.x0.' by parsing this project's validated
    testbench wrapping pattern, or raise ValueError if it doesn't match."""
    m_wrapper = re.search(r'^\.subckt\s+(\S+)', testbench_text, re.MULTILINE | re.IGNORECASE)
    if not m_wrapper:
        raise ValueError("no .subckt wrapper found -- pass --hier-prefix explicitly")
    wrapper = m_wrapper.group(1)

    m_inner = re.search(r'^(X\S+)\s+.*$', testbench_text, re.MULTILINE | re.IGNORECASE)
    if not m_inner:
        raise ValueError("no inner X<n> instantiation found inside the wrapper -- "
                          "pass --hier-prefix explicitly")
    inner_name = m_inner.group(1)

    m_top = re.search(rf'^x{re.escape(wrapper)}\b', testbench_text, re.MULTILINE | re.IGNORECASE)
    if not m_top:
        raise ValueError(f"no top-level 'x{wrapper}' instantiation found -- "
                          "pass --hier-prefix explicitly")

    return f"x{wrapper.lower()}.{inner_name.lower()}."


def build_probe_lines(netlist_path, pdk, hier_prefix, attrs, out_txt):
    # `attrs=None` means DEFAULT_ATTRS, so the default the CLI advertises is a
    # property of this function too -- a direct caller passing None used to
    # get a TypeError instead.
    if attrs is None:
        attrs = list(DEFAULT_ATTRS)
    if pdk not in PDK_CONFIGS:
        raise ValueError(f"unknown PDK {pdk!r} -- see PDK_CONFIGS in "
                          f"../../pdk-characterization/script/characterize_gmid.py")
    cfg = PDK_CONFIGS[pdk]
    mos_instance_name = cfg["mos_instance_name"]

    text = open(netlist_path).read()
    devices = [d for d in parse_devices(text) if d["kind"] == "mos"]
    if not devices:
        raise ValueError(f"no MOS devices found in {netlist_path}")

    lines = []
    var_names = []
    for d in devices:
        inst_suffix = mos_instance_name(d["model"])
        path = f"@m.{hier_prefix}{d['name'].lower()}.{inst_suffix}"
        for attr in attrs:
            var = f"{attr}_{d['name']}"
            lines.append(f"let {var} = {path}[{attr}]")
            var_names.append(var)
    lines.append(f"print {', '.join(var_names)} > {out_txt}")
    return lines, [d["name"] for d in devices]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist")
    ap.add_argument("testbench")
    ap.add_argument("--pdk", default=_guideline_pdk())
    ap.add_argument("--hier-prefix", default=None)
    ap.add_argument("--attrs", default=",".join(DEFAULT_ATTRS))
    ap.add_argument("--out-txt", default="opinfo.txt")
    args = ap.parse_args()

    hier_prefix = args.hier_prefix
    if hier_prefix is None:
        try:
            hier_prefix = detect_hier_prefix(open(args.testbench).read())
        except ValueError as e:
            sys.exit(f"error: {e}")

    attrs = [a.strip() for a in args.attrs.split(",") if a.strip()]
    try:
        lines, mos_names = build_probe_lines(args.netlist, args.pdk, hier_prefix,
                                              attrs, args.out_txt)
    except ValueError as e:
        sys.exit(f"error: {e}")

    print("\n".join(lines))
    print(f"\n* hier_prefix={hier_prefix!r} mos_devices={mos_names}", file=sys.stderr)


if __name__ == "__main__":
    main()
