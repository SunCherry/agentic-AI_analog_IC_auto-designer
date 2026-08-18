#!/usr/bin/env python3
"""Confirm ngspice, Magic, and Netgen are installed AND actually launch,
before any DRC/LVS/simulation is attempted. Existence on disk is not
enough -- Magic in particular silently falls back to a broken PDK path
without PDK_ROOT set (see ../../../reference/environment.md), which this
script would catch as a smoke-test failure, not a "found" pass.

Usage:
  python check_eda_tools.py [--magic-bin PATH] [--netgen-bin PATH] [--pdk-root PATH] [--pdk sky130A]
"""
import argparse
import os
import shutil
import subprocess
import sys

TIMEOUT = 20


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


def _run(cmd, stdin_text=None, env=None, timeout=TIMEOUT):
    try:
        result = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )
        return result.returncode, result.stdout + result.stderr
    except FileNotFoundError:
        return None, "executable not found"
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout}s (may be hung waiting on stdin/DISPLAY)"


def check_ngspice():
    path = shutil.which("ngspice")
    if not path:
        return False, "ngspice not found on PATH"
    _, output = _run(["ngspice", "-v"])
    if "ngspice" not in output.lower():
        return False, f"ngspice found at {path} but -v didn't report a version banner:\n{output}"
    return True, f"{path} -- {output.splitlines()[1].strip() if len(output.splitlines()) > 1 else 'OK'}"


def check_magic(magic_bin, pdk_root, pdk):
    if not (os.path.isfile(magic_bin) and os.access(magic_bin, os.X_OK)):
        return False, f"magic not found or not executable at {magic_bin}"
    rcfile = os.path.join(pdk_root, pdk, "libs.tech/magic/sky130A.magicrc")
    if not os.path.isfile(rcfile):
        return False, f"magicrc not found at {rcfile} -- check --pdk-root/--pdk"
    env = dict(os.environ, PDK_ROOT=pdk_root)
    rc, output = _run(
        [magic_bin, "-dnull", "-noconsole", "-rcfile", rcfile],
        stdin_text='puts "MAGIC_SMOKE_OK"\nquit -noprompt\n',
        env=env,
    )
    if "MAGIC_SMOKE_OK" not in output:
        return False, f"magic launched but did not reach the smoke-test marker (PDK_ROOT/rcfile issue?):\n{output[-500:]}"
    return True, f"{magic_bin} -- loaded {pdk} via {rcfile}"


def check_netgen(netgen_bin):
    if not (os.path.isfile(netgen_bin) and os.access(netgen_bin, os.X_OK)):
        return False, f"netgen not found or not executable at {netgen_bin}"
    rc, output = _run([netgen_bin, "-batch", "quit"])
    if "netgen" not in output.lower():
        return False, f"netgen launched but didn't print its version banner:\n{output[-500:]}"
    return True, f"{netgen_bin} -- {output.splitlines()[0].strip() if output.splitlines() else 'OK'}"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--magic-bin", default="/usr/local/bin/magic")
    ap.add_argument("--netgen-bin", default=os.path.expanduser("~/.local/bin/netgen"))
    ap.add_argument("--pdk-root", default=os.path.expanduser("~/pdk/manual"))
    ap.add_argument("--pdk", default=_guideline_pdk())
    args = ap.parse_args()

    print("=== EDA tool availability check ===\n")

    checks = [
        ("ngspice (simulator)", lambda: check_ngspice()),
        ("Magic (DRC + PEX)", lambda: check_magic(args.magic_bin, args.pdk_root, args.pdk)),
        ("Netgen (LVS)", lambda: check_netgen(args.netgen_bin)),
    ]

    all_ok = True
    for label, fn in checks:
        ok, detail = fn()
        all_ok = all_ok and ok
        print(f"{label}: {'OK' if ok else 'FAIL'}")
        print(f"  {detail}\n")

    if not all_ok:
        print("Result: FAIL -- fix the tool(s) above before running any DRC/LVS/sim. "
              "Do not proceed to sanity DRC/LVS checks with a broken toolchain.")
        sys.exit(1)
    print("Result: PASS -- ngspice, Magic, and Netgen all launch correctly.")
    sys.exit(0)


if __name__ == "__main__":
    main()
