#!/usr/bin/env python3
"""Lay down a design folder and place the user's resolved inputs into it.

Used by `circuit-agent` at its 0i, once intake has resolved the four
mandatory inputs. This is deliberately a script and not a list of steps in
a role file: the verbatim backup is the part that reads as redundant
("the user still has the originals") and gets skipped, and the meta file's
shape drifts when it is described rather than written. Both are mechanical,
so they belong here.

Layout produced (matching design-sheets-checker's Step 1 convention):

    <design_root>/<design_name>/
    |-- user_inputs/       verbatim backup -- never edited after this runs
    |-- netlist/           working netlist
    |-- testbench/         working testbench (.include rewritten, see below)
    |-- ori_gds/           original .gds, if one was supplied
    |-- ori_primitives/    original primitive cells, if supplied
    |-- target_spec.json
    `-- target_spec_meta.json   units + directions (--spec-meta)

Splitting netlist/ from testbench/ breaks a testbench that includes its
netlist by bare name (`.include "foo.sp"`), which is the normal case. The
working copy's include is rewritten to `../netlist/<file>`; `user_inputs/`
keeps the original untouched.

Nothing is ever moved or deleted -- the user's sources are copy-only,
wherever they live.

Usage:
  python .claude/reference/place_inputs.py <design_name> \\
      --netlist PATH --testbench PATH [--spec PATH] [--spec-meta PATH] \\
      [--gds PATH] [--primitives DIR] [--design-root DIR] [--reuse]

`--spec-meta` takes a JSON file of {key: {value, unit, direction}} (any
reasonable nesting; a top-level "keys" wrapper is accepted) and re-emits it
in the canonical shape, so the format cannot drift between runs.

Exits 0 only if every copy verified byte-identical.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_verified(src, dst, log):
    """Copy src -> dst and confirm the bytes match. Never moves."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    a, b = sha256(src), sha256(dst)
    ok = a == b
    log.append((src, dst, ok, a[:12]))
    return ok


def rewrite_include(tb_path, netlist_basename):
    """Point the working testbench's bare `.include "<netlist>"` at ../netlist/.

    Returns (status, old, new). Status is 'rewritten', 'already-relative',
    or 'not-found' -- the caller reports it rather than failing, because a
    testbench that includes nothing is unusual but not invalid.
    """
    text = open(tb_path, encoding="utf-8", errors="replace").read()
    pat = re.compile(
        r'^([ \t]*\.include[ \t]+)(["\']?)([^"\'\n]*%s)\2'
        % re.escape(netlist_basename),
        re.MULTILINE | re.IGNORECASE,
    )
    m = pat.search(text)
    if not m:
        return "not-found", None, None
    old = m.group(3)
    if "/" in old:
        return "already-relative", old, old
    new = "../netlist/" + netlist_basename
    text = pat.sub(lambda mm: mm.group(1) + mm.group(2) + new + mm.group(2), text, count=1)
    open(tb_path, "w", encoding="utf-8").write(text)
    return "rewritten", old, new


def canonical_meta(raw, source_tag):
    """Force --spec-meta input into the one shape downstream can rely on."""
    keys = raw.get("keys", raw)
    out = {}
    for k, v in keys.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        direction = str(v.get("direction", "")).upper()
        if direction not in ("FLOOR", "CEILING"):
            raise SystemExit(
                "place_inputs: key %r has direction %r -- must be FLOOR or "
                "CEILING. Ask the user; do not guess." % (k, v.get("direction"))
            )
        if not v.get("unit"):
            raise SystemExit(
                "place_inputs: key %r has no unit. Ask the user; a unit is "
                "not recoverable from the spec file later." % k
            )
        out[k] = {"value": v.get("value"),
                  "unit": v["unit"],
                  "direction": direction}
    if not out:
        raise SystemExit("place_inputs: --spec-meta contained no usable keys")
    return {
        "_comment": ("Units and directions for target_spec.json, confirmed by "
                     "the user at intake. Not recoverable from that file. "
                     "FLOOR = bigger is better (a minimum); CEILING = smaller "
                     "is better (a maximum)."),
        "source": source_tag,
        "keys": out,
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design_name", help="<design_name> -- the experiment folder name")
    ap.add_argument("--netlist", required=True)
    ap.add_argument("--testbench", required=True)
    ap.add_argument("--spec", default=None,
                    help="target_spec.json. Omit for a [deferred] spec.")
    ap.add_argument("--spec-meta", default=None,
                    help="JSON of {key: {value, unit, direction}}; re-emitted canonically")
    ap.add_argument("--spec-source", default="user-supplied",
                    choices=["user-supplied", "collected", "deferred"],
                    help="'collected' means YOU wrote it from conversation -- "
                         "it is then not a user input and is not backed up")
    ap.add_argument("--gds", default=None)
    ap.add_argument("--primitives", default=None, help="a directory of primitive cell GDS files")
    # No default folder is assumed to exist: the caller passes the root
    # design-sheets-intake settled on at its Step 0, and it is created here
    # if absent. Hardcoding a parent directory is what breaks the day the
    # repo is reorganized.
    ap.add_argument("--design-root", default="designs")
    ap.add_argument("--reuse", action="store_true",
                    help="allow an existing <design_dir> (an in-progress design)")
    args = ap.parse_args()

    os.makedirs(args.design_root, exist_ok=True)
    ddir = os.path.join(args.design_root, args.design_name)
    if os.path.exists(ddir) and not args.reuse:
        raise SystemExit(
            "place_inputs: %s already exists. Intake must stop and ask the user "
            "whether to reuse it or pick another name (0a); pass --reuse only "
            "once they have said so." % ddir)

    for missing in [p for p in (args.netlist, args.testbench, args.spec, args.gds,
                                args.primitives) if p and not os.path.exists(p)]:
        raise SystemExit("place_inputs: input does not exist: " + missing)

    for sub in ("user_inputs", "netlist", "testbench"):
        os.makedirs(os.path.join(ddir, sub), exist_ok=True)

    log = []
    nl_base = os.path.basename(args.netlist)
    tb_base = os.path.basename(args.testbench)

    # --- backup FIRST, then derive the working copies from it -------------
    copy_verified(args.netlist, os.path.join(ddir, "user_inputs", nl_base), log)
    copy_verified(args.testbench, os.path.join(ddir, "user_inputs", tb_base), log)
    if args.spec and args.spec_source == "user-supplied":
        copy_verified(args.spec, os.path.join(ddir, "user_inputs", "target_spec.json"), log)

    working_tb = os.path.join(ddir, "testbench", tb_base)
    copy_verified(os.path.join(ddir, "user_inputs", nl_base),
                  os.path.join(ddir, "netlist", nl_base), log)
    copy_verified(os.path.join(ddir, "user_inputs", tb_base), working_tb, log)
    if args.spec:
        copy_verified(args.spec, os.path.join(ddir, "target_spec.json"), log)

    if args.gds:
        os.makedirs(os.path.join(ddir, "ori_gds"), exist_ok=True)
        g = os.path.basename(args.gds)
        copy_verified(args.gds, os.path.join(ddir, "user_inputs", g), log)
        copy_verified(args.gds, os.path.join(ddir, "ori_gds", g), log)
    if args.primitives:
        for f in sorted(os.listdir(args.primitives)):
            src = os.path.join(args.primitives, f)
            if os.path.isfile(src):
                copy_verified(src, os.path.join(ddir, "ori_primitives", f), log)

    inc_status, inc_old, inc_new = rewrite_include(working_tb, nl_base)

    meta_path = None
    if args.spec_meta:
        raw = json.load(open(args.spec_meta, encoding="utf-8"))
        meta_path = os.path.join(ddir, "target_spec_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(canonical_meta(raw, args.spec_source), f, indent=2)
            f.write("\n")

    # --- report -----------------------------------------------------------
    print("PLACED -- %s" % ddir)
    failed = 0
    for src, dst, ok, digest in log:
        rel = os.path.relpath(dst, ddir)
        print("  %-4s %-34s <- %s  (%s)" % ("OK" if ok else "FAIL", rel, src, digest))
        failed += (not ok)

    print("\n  .include fix-up: %s" % inc_status
          + ("  %r -> %r" % (inc_old, inc_new) if inc_new else ""))
    if inc_status == "not-found":
        print("    (the testbench does not include %s by name -- check it "
              "resolves the netlist some other way)" % nl_base)
    if meta_path:
        print("  target_spec_meta.json: written, canonical shape")
    elif args.spec_source != "deferred":
        print("  target_spec_meta.json: NOT written -- pass --spec-meta; units "
              "and directions are not recoverable from target_spec.json")

    print("\n  user's sources: untouched (copy-only, nothing moved or deleted)")
    if failed:
        print("\n%d copy/copies did NOT verify -- do not report intake complete."
              % failed)
        return 1
    print("  every copy verified byte-identical (sha256)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
