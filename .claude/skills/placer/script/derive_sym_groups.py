#!/usr/bin/env python3
"""Derive placement symmetry groups from the circuit read.

Why this exists
---------------
`anneal_placement.py` has always had a symmetry term, but its groups were
"NOT auto-inferred -- a circuit-understanding judgment call, left to whoever
invokes this script". In practice that meant symmetry was inactive on every
run: nothing in the flow produced the file, the annealer's objective is
dominated by HPWL, and an agent optimizing wirelength has an active reason
NOT to hand-write one. A differential LC VCO shipped through the whole gate
chain with one tank coil upright and its twin rotated 90 degrees, passing
every gate, because no stage ever asked whether the two halves matched.

The judgment call was already made, though -- one stage earlier, and written
down. `circuit_decomposition.yaml` records which devices must be IDENTICAL
(`tie_groups` with `tied` set and `ratio_carrier: none`) as opposed to merely
ratioed, and says why: TG1's own rationale on that VCO reads "the two halves
must be IDENTICAL, not ratioed; asymmetry is duty-cycle error and static
offset". That is a symmetry constraint in everything but name. This script
reads it and hands the annealer the constraint the circuit read already
justified, so the two stages stop disagreeing.

Two derivations, because one is not enough
------------------------------------------
1. MATCHED TIE GROUPS. A `tie_groups` entry naming exactly two devices, with
   a non-empty `tied` (or `also_must_match`) list and no ratio carrier, is a
   matched pair. A group carrying a ratio (`ratio_carrier: m`, or the VCO's
   `status: ratio_conflict` tail mirror) is NOT: its devices are deliberately
   different sizes, and mirroring them would be wrong.

2. DIFFERENTIAL-NET PROPAGATION. Twins come first from the circuit read's
   own `matched_nets` section, where `circuit-decomposition` Step 4 states
   them outright (`kind: differential`) -- including pairs no tie group
   implies, such as a passive twin or a pairing carried outward past the
   pattern that named it. Where that section is absent or empty (a file
   written before Step 4 existed, or a run that skipped it) the pairs above
   are used to re-derive them instead: compare the two devices TERMINAL BY
   TERMINAL, and the terminals that disagree name a differential net pair.
   (Set comparison does not work: a cross-coupled pair's two devices touch
   the same two nets, just swapped.) Any two so-far-unpaired macros drawn from the SAME cell,
   one on each side of such a net pair, are then also twins.

   Derivation 2 is not a refinement -- it is the only thing that catches the
   VCO's two tank inductors. They have no tie group at all, because
   `pattern-table.md` registers no inductor-bearing pattern, so
   `circuit-decomposition` files them under `unmatched_devices`. They are
   nonetheless the most symmetry-critical objects on the die: 196 x 213 um
   each, and the entire tank hangs off them.

Devices sharing ONE macro are dropped, not paired. A current mirror or a
diff pair composed into a single glayout cell is already matched inside that
cell (common-centroid, shared bias); asking placement to mirror a macro
against itself is meaningless.

The axis
--------
Emitted as `null` by default, meaning "a shared free axis": every pair
mirrors about ONE common vertical midline whose position the annealer
chooses. Pinning a numeric axis requires guessing a coordinate before
anything is placed, which over-constrains the floorplan for no benefit --
what matters is that the halves mirror EACH OTHER, not where the mirror sits.
`--axis X` pins it when a design really does need a fixed spine.

Usage
-----
    python derive_sym_groups.py <layout>/primitives/manifest.json
        [--decomposition circuit_decomposition.yaml]   # default: found by walking up
        [--out <layout>/sym_groups.json]
        [--axis X]            # pin the mirror axis; default is a shared free axis
        [--report PATH]       # default: alongside --out, as sym_groups.txt

Then feed the result straight to the annealer:

    python anneal_placement.py <manifest> --iters N --sym-groups <layout>/sym_groups.json

Exit codes: 0 = groups written (possibly zero, for a genuinely single-ended
circuit); 2 = inputs unusable (no manifest, no decomposition, malformed yaml).
Zero groups is a real answer for a single-ended design and is not an error --
but it is printed loudly, because on a differential circuit it means the
circuit read is missing something and placement is about to run blind.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

# Terminals compared device-to-device when hunting for differential nets.
# `bulk` is deliberately absent: both halves of a differential pair sit in
# the same well and tie to the same bulk, so it can never disagree and would
# only add noise.
SIGNAL_TERMINALS = ("drain", "gate", "source")

# A tie group is a MATCHED pair only when its devices must be identical.
# These statuses mean the circuit read explicitly declined to tie them.
NON_MATCHING_STATUSES = {"ratio_conflict", "not_tunable", "conflict"}


def find_decomposition(manifest_path: Path) -> Path | None:
    """Walk up from the manifest looking for the design's circuit read.

    Layout lives at <design>/layout/primitives/manifest.json and the
    decomposition at <design>/circuit_decomposition.yaml, so this is two
    levels up -- but the walk is generic so a re-arranged tree still works.
    """
    for parent in manifest_path.resolve().parents:
        cand = parent / "circuit_decomposition.yaml"
        if cand.is_file():
            return cand
    return None


def matched_device_pairs(decomp: dict, rejected=None):
    """(devA, devB, group_id, why) for every tie group that means IDENTICAL.

    Ratioed groups are rejected here rather than filtered later, and the
    reason is appended to `rejected` so the report can show the reasoning
    instead of silently shrinking the group list.

    The `ratio_carrier` test is deliberately strict: only the literal "none"
    is accepted. A carrier like the VCO cap bank's "m (nominal only -- ...
    intent is 1:1, not a weighted array)" is prose, and guessing at prose to
    assert a hard geometric constraint is how a placer ends up mirroring two
    devices the designer meant to be different sizes. Such a pair is not
    lost: if the two macros really are twins, derivation 2 recovers them
    from structure -- same cell, opposite sides of a differential net --
    which is evidence rather than interpretation.
    """
    out = []
    for tg in decomp.get("tie_groups") or []:
        gid = str(tg.get("id", "?"))
        devices = tg.get("devices") or []
        if len(devices) != 2:
            continue
        status = str(tg.get("status", "")).strip().lower()
        if status in NON_MATCHING_STATUSES:
            if rejected is not None:
                rejected.append((f"{devices[0]}/{devices[1]}",
                                 f"{gid}: status `{status}` -- circuit read declined to tie these"))
            continue
        carrier = str(tg.get("ratio_carrier", "none") or "none").strip().lower()
        # "none" is the only carrier value that means "not ratioed". Anything
        # else -- `m`, or a prose value like the VCO cap bank's "m (nominal
        # only ...)" -- describes a group whose devices may legitimately
        # differ, and a mirror constraint would be a lie about the circuit.
        if not carrier.startswith("none"):
            if rejected is not None:
                rejected.append((f"{devices[0]}/{devices[1]}",
                                 f"{gid}: ratio_carrier is not `none` -- may be ratioed, "
                                 f"not asserted as a mirror from the tie group alone"))
            continue
        tied = list(tg.get("tied") or [])
        also = list(tg.get("also_must_match") or [])
        if not tied and not also:
            if rejected is not None:
                rejected.append((f"{devices[0]}/{devices[1]}",
                                 f"{gid}: nothing tied and nothing must match"))
            continue
        shared = "+".join(tied + also)
        out.append((devices[0], devices[1], gid,
                    f"tie group {gid} ({tg.get('pattern', 'none')}), identical {shared}"))
    return out


def declared_differential_nets(decomp: dict) -> dict[str, str]:
    """Net -> its twin, as `circuit-decomposition` Step 4 wrote it down.

    Read in preference to re-deriving, because the authored section knows
    things a device-pair comparison cannot: a twin pair carried outward
    into a load or a compensation branch, and any pair whose devices have
    no tie group at all. Only `kind: differential` is taken. `common` is
    one shared node, `branch` is a ratioed family whose nets are NOT twins
    unless the legs are 1:1, and `complementary`/`sensitive` are RC
    constraints rather than mirror constraints -- treating any of them as
    a mirror axis would place two macros symmetrically on the strength of
    a net that was never claimed to be symmetric.
    """
    twin: dict[str, str] = {}
    for entry in decomp.get("matched_nets") or []:
        if str(entry.get("kind", "")).strip().lower() != "differential":
            continue
        nets = [n for n in (entry.get("nets") or []) if n]
        if len(nets) != 2:
            continue  # a 4-way set is not a mirror axis; skip rather than guess
        a, b = nets
        twin.setdefault(a, b)
        twin.setdefault(b, a)
    return twin


def differential_nets(pairs, device_index) -> dict[str, str]:
    """Net -> its differential twin, read off already-matched device pairs.

    Compared terminal by terminal on purpose. A cross-coupled pair's two
    devices touch the SAME set of nets (drain of one is the gate of the
    other), so a set difference finds nothing; the disagreement only shows
    up per terminal.
    """
    twin: dict[str, str] = {}
    for a, b, _gid, _why in pairs:
        da, db = device_index.get(a), device_index.get(b)
        if not da or not db:
            continue
        for term in SIGNAL_TERMINALS:
            na, nb = da.get(term), db.get(term)
            if na and nb and na != nb:
                twin.setdefault(na, nb)
                twin.setdefault(nb, na)
    return twin


def macro_cell(macro: dict) -> str:
    """What a macro IS -- two macros are twins only if this matches.

    Kind plus footprint rounded to 1nm, NOT the GDS basename.
    `generate_primitives.py` writes one file per macro (`XC0.gds`,
    `XC1.gds`), so comparing filenames would call two identical MiM caps
    different cells and miss the tank capacitor pair entirely. Kind and
    footprint are what actually decide whether one macro can mirror the
    other. Two same-shaped macros are still only paired when they also sit
    on opposite sides of a differential net, which is what keeps this from
    matching unrelated devices that happen to share a bounding box.
    """
    return f"{macro.get('kind')}:{macro.get('w', 0):.3f}x{macro.get('h', 0):.3f}"


def twin_macros_across(twin_nets, macros_by_name, device_index, already):
    """Same-cell macro pairs sitting on opposite sides of a differential net.

    This is what catches a pair of tank inductors, or any twin passive the
    pattern table has no row for.
    """
    # macro -> the nets it touches, and the cell it is drawn from
    nets_of: dict[str, set] = {}
    for dev, info in device_index.items():
        mac = info.get("macro")
        if mac not in macros_by_name:
            continue
        nets_of.setdefault(mac, set()).update(
            info[t] for t in SIGNAL_TERMINALS if info.get(t))

    found = []
    names = sorted(macros_by_name)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if (a, b) in already or (b, a) in already:
                continue
            if macro_cell(macros_by_name[a]) != macro_cell(macros_by_name[b]):
                continue
            na, nb = nets_of.get(a, set()), nets_of.get(b, set())
            # exactly one differential net pair should separate them, and
            # every other net they touch should be common (both tank coils
            # share VDD; only their output net differs).
            only_a = na - nb
            only_b = nb - na
            if len(only_a) != 1 or len(only_b) != 1:
                continue
            xa, xb = only_a.pop(), only_b.pop()
            if twin_nets.get(xa) != xb:
                continue
            found.append((a, b, f"same cell {macro_cell(macros_by_name[a])}, "
                                f"differential nets {xa}/{xb}"))
    return found


def derive(manifest_path: Path, decomp_path: Path, axis):
    manifest = json.loads(manifest_path.read_text())
    decomp = yaml.safe_load(decomp_path.read_text()) or {}

    device_index = manifest.get("device_index") or {}
    macros_by_name = {m["name"]: m for m in manifest.get("macros") or []}

    groups = []          # (macroA, macroB, why)
    seen = set()
    dropped = []

    pairs = matched_device_pairs(decomp, rejected=dropped)
    for a, b, gid, why in pairs:
        ma = (device_index.get(a) or {}).get("macro")
        mb = (device_index.get(b) or {}).get("macro")
        if not ma or not mb:
            dropped.append((f"{a}/{b}", f"{gid}: device not in manifest device_index"))
            continue
        if ma == mb:
            # Already matched INSIDE one cell -- a composed mirror or diff
            # pair. Nothing for placement to do.
            dropped.append((f"{a}/{b}", f"{gid}: both devices in macro {ma}, matched inside the cell"))
            continue
        key = tuple(sorted((ma, mb)))
        if key in seen:
            continue
        seen.add(key)
        groups.append((ma, mb, why))

    twin_nets = declared_differential_nets(decomp)
    twin_source = "matched_nets (authored)"
    if not twin_nets:
        twin_nets = differential_nets(pairs, device_index)
        twin_source = "re-derived from tie-group device pairs"

    for a, b, why in twin_macros_across(twin_nets, macros_by_name, device_index, seen):
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        groups.append((a, b, why))

    triples = [[a, b, axis] for a, b, _why in groups]
    return triples, groups, dropped, twin_nets, twin_source


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("manifest", help="primitives/manifest.json from generate_primitives.py")
    ap.add_argument("--decomposition", default=None,
                    help="circuit_decomposition.yaml (default: found by walking up from the manifest)")
    ap.add_argument("--out", default=None,
                    help="default: <manifest's grandparent>/sym_groups.json, i.e. layout/")
    ap.add_argument("--axis", type=float, default=None,
                    help="pin the mirror axis at this x. Default: a shared FREE axis (null), "
                         "which is what you want unless the design needs a fixed spine.")
    ap.add_argument("--report", default=None,
                    help="default: sym_groups.txt beside --out")
    args = ap.parse_args(argv)

    manifest_path = Path(args.manifest)
    if not manifest_path.is_file():
        print(f"ERROR: no manifest at {manifest_path}", file=sys.stderr)
        return 2

    decomp_path = Path(args.decomposition) if args.decomposition else find_decomposition(manifest_path)
    if not decomp_path or not decomp_path.is_file():
        print("ERROR: no circuit_decomposition.yaml found. Run "
              "`.claude/skills/circuit-decomposition/SKILL.md` first -- symmetry groups are "
              "derived from its tie_groups, not guessed from the netlist.", file=sys.stderr)
        return 2

    out_path = Path(args.out) if args.out else manifest_path.parent.parent / "sym_groups.json"
    report_path = Path(args.report) if args.report else out_path.with_suffix(".txt")

    triples, groups, dropped, twin_nets, twin_source = derive(manifest_path, decomp_path, args.axis)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(triples, indent=2) + "\n")

    lines = []
    A = lines.append
    A("symmetry groups derived from the circuit read")
    A(f"  manifest      : {manifest_path}")
    A(f"  decomposition : {decomp_path}")
    A(f"  axis          : {'shared free axis' if args.axis is None else f'pinned at x={args.axis}'}")
    A("")
    if groups:
        A(f"-- {len(groups)} mirror pair(s) ------------------------------------------")
        for a, b, why in groups:
            A(f"  {a:24s} <-> {b:24s}")
            A(f"      {why}")
    else:
        A("-- NO mirror pairs derived -----------------------------------------")
        A("  For a single-ended circuit this is correct. For a differential one it")
        A("  means circuit_decomposition.yaml carries neither a `matched_nets` entry")
        A("  with `kind: differential` nor a two-device tie group with `tied` set and")
        A("  `ratio_carrier: none` -- fix the circuit read (circuit-decomposition")
        A("  Steps 3-4) rather than hand-writing groups here.")
    if twin_nets:
        A("")
        A(f"-- differential nets identified ({twin_source}) --")
        for n in sorted(set(map(tuple, (sorted((k, v)) for k, v in twin_nets.items())))):
            A(f"  {n[0]} <-> {n[1]}")
    if dropped:
        A("")
        A("-- considered and dropped -------------------------------------------")
        for what, why in dropped:
            A(f"  {what:24s} {why}")
    report_path.write_text("\n".join(lines) + "\n")

    print("\n".join(lines))
    print()
    print(f"wrote {out_path}")
    print(f"wrote {report_path}")
    if not groups:
        print("WARNING: zero symmetry groups -- placement will run with symmetry inactive.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
