#!/usr/bin/env python3
"""Detect elementary analog sub-circuit topologies in a SPICE netlist.

Scans every same-type MOSFET pair (and every complementary nfet/pfet pair)
for the exact drain/gate/source signature that glayout's elementary cells
produce, so a match here means "draw this pair with that glayout call",
not just "these devices are vaguely related":

  current mirror  (src/glayout/cells/elementary/current_mirror/current_mirror.py)
    device A is diode-connected (gate == drain); device B shares A's gate
    net and A's source net. B's drain is the mirrored output.

  differential pair (src/glayout/cells/elementary/diff_pair/diff_pair.py)
    same-type devices A, B share a source net (the tail) but have distinct
    gates and distinct drains.

  transmission gate (src/glayout/cells/elementary/transmission_gate/transmission_gate.py)
    one nfet + one pfet whose drains coincide and whose sources coincide
    (they switch the same two nodes), with distinct (complementary) gates.

  flipped voltage follower (src/glayout/cells/elementary/FVF/fvf.py)
    same-type devices A, B where A's drain is B's gate and A's source is
    B's drain (the feedback loop that defines an FVF).

  quad pair / cross-coupled linearized diff pair (no dedicated glayout
  module -- composed from 2x diff_pair(); see
  circuit-specific/quad-pair/quad-pair-layout.md)
    two diff-pair-like half-pairs on two different tail nodes, driving
    the same two input nets and the same two output nets, but with the
    gate->drain mapping inverted between the halves (each output node
    sums one device from each half-pair with opposite input polarity).
    Detected by pairing up diff-pair-like candidates and checking for
    this inversion; a match consumes both half-pairs into one finding
    instead of two separate (individually misleading) diff-pair findings.

  cascode current mirror (see
  circuit-specific/cascode-current-mirror/cascode-current-mirror-layout.md)
    a current mirror where each branch is two devices stacked in series,
    biased level-for-level. Two variants, both detected:
    - stacked-diode (self-biased): reference branch is two *independently*
      diode-connected devices in series; no dedicated glayout module,
      compose from 4x nmos()/pmos().
    - FVF-biased (low-voltage): two FVF instances' Ib nodes bias a
      stacked 2-level output branch instead of a diode stack; matches
      cells/composite/low_voltage_cmirror/low_voltage_cmirror.py.
    Either variant consumes its constituent pairs (the bottom mirror pair,
    or the two FVF matches) so they aren't ALSO reported as separate
    current_mirror / flipped_voltage_follower findings.

Bipolar transistors (NPN/PNP) build current mirrors and differential
pairs too -- BJT base/collector/emitter map onto the same gate/drain/
source dict keys used for MOSFETs (base~gate, collector~drain,
emitter~source), so is_current_mirror()/is_diff_pair()/
is_cross_coupled_quad() apply to BJT pairs unchanged (they only ever
check 'kind' for equality, never assume it's specifically nfet/pfet).
Transmission gate and FVF are intentionally NOT generalized to bipolar --
see transmission-gate-layout.md / fvf-layout.md for why those two are
MOS-specific techniques.

Devices that don't match any signature are reported as unclassified --
that's expected for single-device gain/output stages, or for devices that
are part of a *composite* pattern (see reference.md) that this script does
not try to auto-detect.

Usage:
  python detect_topology.py path/to/design.sp
      [--out-json path/to/circuit_structure.json] [--out-md path/to/circuit_structure.md]

`--out-json`/`--out-md` write the SAME `findings`/`unclassified` this
script prints, structured -- e.g. for `../../../agents/schematic-agent.md`'s
"Circuit understanding" step to build its own `circuit_structure.json`
on top of (this script's structural findings, annotated with which one is
functionally "the input pair" vs. a load/bias structure -- a judgment call
this script deliberately doesn't make, see its own module docstring:
detection here is by device SHAPE, not circuit FUNCTION), or any other
consumer that wants this scan's raw structural result without scraping
stdout. Optional -- omit both for the original print-only behavior."""
import argparse
import json
import re
import sys
from pathlib import Path

# Matched by shape + model-name keyword, not by instance-name prefix, so
# this doesn't presuppose any particular instance-naming convention (see
# the naming-convention skill's check_naming.py, which hit exactly this
# bug when it anchored on "starts with M").
_MOS_MODEL_RE = re.compile(
    r'^\s*(?P<name>\S+)\s+(?P<nets>(?:\S+\s+){3}\S+)\s+(?P<model>\S*fet\S*)\s*(?P<params>.*)$',
    re.IGNORECASE,
)
_BJT_MODEL_RE = re.compile(
    r'^\s*(?P<name>\S+)\s+(?P<nets>(?:\S+\s+){2,3}\S+?)\s+(?P<model>\S*(?:npn|pnp)\S*)\s*(?P<params>.*)$',
    re.IGNORECASE,
)

BJT_KINDS = {'npn', 'pnp'}
MOS_KINDS = {'nfet', 'pfet'}


def parse_devices(filepath: str) -> list[dict]:
    """Netlist parser for MOSFETs and BJTs: name, kind (nfet/pfet/npn/pnp),
    and normalized drain/gate/source keys (BJT collector/base/emitter map
    onto the same keys, so every is_*() check below applies unchanged to
    both device families). Mirrors the device-line convention used by
    extract_physical_info.py's parse_netlist(), which this script
    intentionally does not import from (kept dependency-free so it can
    run standalone)."""
    devices = []
    with open(filepath) as f:
        lines = f.read().splitlines()

    joined = []
    for line in lines:
        line = line.strip()
        if line.startswith('+') and joined:
            joined[-1] += ' ' + line[1:]
        else:
            joined.append(line)

    for line in joined:
        if not line or line.startswith(('*', '.')):
            continue
        m = _MOS_MODEL_RE.match(line)
        if m:
            nets = m.group('nets').split()
            model = m.group('model').lower()
            devices.append({
                'name': m.group('name'),
                'kind': 'nfet' if 'nfet' in model else 'pfet',
                'drain': nets[0],
                'gate': nets[1],
                'source': nets[2],
                'bulk': nets[3] if len(nets) > 3 else None,
            })
            continue
        m = _BJT_MODEL_RE.match(line)
        if m:
            nets = m.group('nets').split()
            model = m.group('model').lower()
            devices.append({
                'name': m.group('name'),
                'kind': 'npn' if 'npn' in model else 'pnp',
                'drain': nets[0],   # collector
                'gate': nets[1],    # base
                'source': nets[2],  # emitter
                'bulk': None,
            })
    return devices


def is_current_mirror(a: dict, b: dict) -> str | None:
    """Returns the name of the diode-connected (reference) device, or None."""
    if a['kind'] != b['kind']:
        return None
    for ref, other in ((a, b), (b, a)):
        if (ref['gate'] == ref['drain']                # ref is diode-connected
                and other['gate'] == ref['gate']        # other shares the ref's gate
                and other['source'] == ref['source']    # shared source
                and other['drain'] != ref['drain']):    # distinct mirrored output
            return ref['name']
    return None


SUPPLY_RAIL_NAMES = {"vdd", "vss", "gnd", "vcc", "vee", "avdd", "avss", "dvdd", "dvss",
                      "vbulk", "vnb", "vpb"}  # bulk/well-tie nodes -- see fvf_netlist()'s
                      # VBULK, which an FVF's feedback device's source lands on by
                      # construction; that's a shared bias-tie node, not a diff-pair tail.


def is_diff_pair(a: dict, b: dict) -> bool:
    # A shared *supply rail* isn't a diff pair's tail node -- it just means
    # two unrelated single-device stages both connect to VDD/GND. A real
    # diff pair's tail is the (non-rail) node feeding a shared tail current
    # source, so require the shared source to not be a recognized rail name.
    if a['source'].lower() in SUPPLY_RAIL_NAMES:
        return False
    return (a['kind'] == b['kind']
            and a['source'] == b['source']
            and a['gate'] != b['gate']
            and a['drain'] != b['drain'])


def is_cross_coupled_quad(pair1: tuple[dict, dict], pair2: tuple[dict, dict]) -> bool:
    """pair1, pair2 are device tuples that already individually satisfy
    is_diff_pair(). Returns True if they form a "quad pair" -- a
    linearized differential pair split into two half-pairs on separate
    tail branches, cross-coupled so each output node sums one device from
    each half-pair with *opposite* input polarity (this is what cancels
    third-order distortion; see circuit-specific/quad-pair/quad-pair-layout.md).
    """
    a1, b1 = pair1
    a2, b2 = pair2
    if a1['source'] == a2['source']:
        return False  # must be two distinct tail branches, not one bigger pair
    gate_to_drain_1 = {a1['gate']: a1['drain'], b1['gate']: b1['drain']}
    gate_to_drain_2 = {a2['gate']: a2['drain'], b2['gate']: b2['drain']}
    if set(gate_to_drain_1) != set(gate_to_drain_2):
        return False       # must drive the same two input nets
    if set(gate_to_drain_1.values()) != set(gate_to_drain_2.values()):
        return False       # must output to the same two nodes
    # cross-coupled: each shared gate net drives a *different* output node
    # in pair2 than it did in pair1 (the "swap" that defines the quad)
    return all(gate_to_drain_1[g] != gate_to_drain_2[g] for g in gate_to_drain_1)


def is_transmission_gate(a: dict, b: dict) -> bool:
    # MOS-only by construction: a transmission gate relies on complementary
    # n/p threshold behavior, which has no bipolar equivalent -- see
    # transmission-gate-layout.md. Restricting to MOS_KINDS here (rather
    # than just "kind differs") avoids ever mislabeling some coincidental
    # BJT connectivity as a transmission gate.
    if a['kind'] not in MOS_KINDS or b['kind'] not in MOS_KINDS:
        return False
    return (a['kind'] != b['kind']
            and a['drain'] == b['drain']
            and a['source'] == b['source']
            and a['gate'] != b['gate'])


def is_fvf(a: dict, b: dict) -> str | None:
    """Returns (input_fet_name, feedback_fet_name) order, or None."""
    # MOS-only: the FVF's low-voltage-headroom benefit specifically relies
    # on a MOSFET gate drawing no DC current -- a bipolar base draws real
    # base current, so the same feedback topology doesn't deliver the same
    # benefit. See fvf-layout.md.
    if a['kind'] not in MOS_KINDS or b['kind'] not in MOS_KINDS:
        return None
    if a['kind'] != b['kind']:
        return None
    for fet1, fet2 in ((a, b), (b, a)):
        if fet1['drain'] == fet2['gate'] and fet1['source'] == fet2['drain']:
            return (fet1['name'], fet2['name'])
    return None


def find_stacked_diode_cascode_mirrors(devices: list[dict]) -> list[dict]:
    """Find self-biased cascode current mirrors: a reference branch of two
    *independently* diode-connected devices stacked in series (bottom
    device's drain feeds the top/cascode device's source), mirrored by a
    second stacked pair whose gates are bias-matched level-for-level.
    Only reports 1 reference stack : 1 output stack groups (see
    quad-pair/current-mirror precedent for the general N-output-leg case,
    which this does not attempt to generalize to)."""
    results = []
    used = set()
    for ref_bot in devices:
        if ref_bot['name'] in used or ref_bot['gate'] != ref_bot['drain']:
            continue  # ref_bot must be diode-connected
        for ref_top in devices:
            if ref_top['name'] in used or ref_top['name'] == ref_bot['name']:
                continue
            if ref_top['kind'] != ref_bot['kind']:
                continue
            if ref_top['source'] != ref_bot['drain']:
                continue          # must stack directly on ref_bot
            if ref_top['gate'] != ref_top['drain']:
                continue          # ref_top must ALSO be independently diode-connected
            for mirror_bot in devices:
                if mirror_bot['name'] in used or mirror_bot['name'] in (ref_bot['name'], ref_top['name']):
                    continue
                if is_current_mirror(ref_bot, mirror_bot) != ref_bot['name']:
                    continue      # mirror_bot must mirror ref_bot's bottom-level bias
                for mirror_top in devices:
                    if mirror_top['name'] in used or mirror_top['name'] in (ref_bot['name'], ref_top['name'], mirror_bot['name']):
                        continue
                    if mirror_top['kind'] != ref_top['kind']:
                        continue
                    if mirror_top['source'] != mirror_bot['drain']:
                        continue           # must stack directly on mirror_bot
                    if mirror_top['gate'] != ref_top['drain']:
                        continue           # must mirror ref_top's cascode bias
                    if mirror_top['drain'] == ref_top['drain']:
                        continue           # output must be a distinct node from the reference
                    results.append({
                        'ref_bot': ref_bot['name'], 'ref_top': ref_top['name'],
                        'mirror_bot': mirror_bot['name'], 'mirror_top': mirror_top['name'],
                    })
                    used.update((ref_bot['name'], ref_top['name'], mirror_bot['name'], mirror_top['name']))
    return results


def find_fvf_cascode_mirrors(devices: list[dict], fvf_matches: list[tuple[str, str]]) -> list[dict]:
    """Find low-voltage (FVF-biased) cascode current mirrors: two FVF
    instances whose feedback (Ib) nodes bias a stacked 2-level output
    branch, instead of a stacked-diode reference branch (see
    find_stacked_diode_cascode_mirrors). Mirrors src/glayout/cells/
    composite/low_voltage_cmirror/low_voltage_cmirror.py's structure."""
    dev_by_name = {d['name']: d for d in devices}
    results = []
    used_fvf = set()
    for i, (in1, fb1) in enumerate(fvf_matches):
        if i in used_fvf:
            continue
        bias_node_1 = dev_by_name[in1]['drain']  # = fb1's gate (the FVF's "Ib" bias output)
        for j in range(i + 1, len(fvf_matches)):
            if j in used_fvf:
                continue
            in2, fb2 = fvf_matches[j]
            bias_node_2 = dev_by_name[in2]['drain']
            if bias_node_1 == bias_node_2:
                continue  # must be two distinct bias levels, not the same FVF counted twice
            for mirror_bot in devices:
                if mirror_bot['gate'] != bias_node_1:
                    continue
                for mirror_top in devices:
                    if mirror_top['kind'] != mirror_bot['kind']:
                        continue
                    if mirror_top['source'] != mirror_bot['drain']:
                        continue
                    if mirror_top['gate'] != bias_node_2:
                        continue
                    results.append({
                        'bias_fvf': [in1, fb1], 'cascode_fvf': [in2, fb2],
                        'mirror_bot': mirror_bot['name'], 'mirror_top': mirror_top['name'],
                    })
                    used_fvf.update((i, j))
    return results


def current_mirror_glayout_call(kind: str, n: int) -> str:
    if kind in BJT_KINDS:
        return (f"no dedicated module -- compose from {n + 1}x npn()/pnp() primitives "
                f"(1 reference + {n} mirror leg{'s' if n != 1 else ''}); mind base-current "
                f"error (Widlar/Wilson), see doc")
    return (f"current_mirror(pdk, device='nfet' or 'pfet', numcols=...)"
            f" -- call once per (reference, mirror) leg: {n} call{'s' if n != 1 else ''} total")


def diff_pair_glayout_call(kind: str) -> str:
    if kind in BJT_KINDS:
        return "no dedicated module -- compose from 2x npn()/pnp() primitives with matched interdigitized placement, see doc"
    return "diff_pair(pdk, width=..., fingers=..., n_or_p_fet=...)"


def quad_pair_glayout_call(kind: str) -> str:
    if kind in BJT_KINDS:
        return "no dedicated module -- compose from 2x hand-built BJT diff pairs + manual cross-coupled routing, see doc"
    return "two diff_pair() calls (one per half-pair) + manual cross-coupled drain routing -- no dedicated glayout module, see doc"


def detect(devices: list[dict]) -> list[dict]:
    findings = []
    classified = set()
    dev_by_name = {d['name']: d for d in devices}
    # reference device name -> set of mirror device names. A single
    # diode-connected reference commonly fans out to several mirror legs
    # (one bias branch feeding N different current sinks/sources), so
    # group by reference instead of emitting one finding per pair -- that
    # would misrepresent one 1:N mirror as N unrelated 1:1 mirrors.
    mirror_groups: dict[str, set[str]] = {}
    diff_pair_candidates: list[tuple[dict, dict]] = []
    fvf_matches: list[tuple[str, str]] = []
    for i, a in enumerate(devices):
        for b in devices[i + 1:]:
            ref = is_current_mirror(a, b)
            if ref:
                mirror = b['name'] if ref == a['name'] else a['name']
                mirror_groups.setdefault(ref, set()).add(mirror)
                classified.update((a['name'], b['name']))
            if is_diff_pair(a, b):
                # Don't emit yet -- a diff-pair-like pair might turn out to
                # be one half of a cross-coupled quad pair (see below),
                # which should be reported as one quad finding, not as two
                # separate (and individually misleading) diff-pair findings.
                diff_pair_candidates.append((a, b))
            if is_transmission_gate(a, b):
                findings.append({
                    'topology': 'transmission_gate',
                    'devices': [a['name'], b['name']],
                    'detail': f"shared D/S nodes={a['drain']}/{a['source']}, gates={a['gate']}(n)/{b['gate']}(p)",
                    'glayout_call': "transmission_gate(pdk, width=(n,p), fingers=(n,p), ...)",
                    'doc': 'circuit-specific/transmission-gate/transmission-gate-layout.md',
                })
                classified.update((a['name'], b['name']))
            fvf = is_fvf(a, b)
            if fvf:
                # Don't emit yet -- a matched pair of FVFs might turn out to
                # be the two bias branches of an FVF-based (low-voltage)
                # cascode current mirror (see below), reported as one
                # combined finding instead of two separate FVF findings.
                fvf_matches.append(fvf)

    # Stacked-diode (self-biased) cascode current mirrors: consume the
    # bottom pair out of mirror_groups so it isn't ALSO reported as a
    # plain 2-device current_mirror finding.
    for stack in find_stacked_diode_cascode_mirrors(devices):
        stack_kind = dev_by_name[stack['ref_bot']]['kind']
        primitive_call = "4x npn()/pnp() primitives" if stack_kind in BJT_KINDS else "4x nmos()/pmos() primitives"
        findings.append({
            'topology': 'cascode_current_mirror',
            'devices': [stack['ref_bot'], stack['ref_top'], stack['mirror_bot'], stack['mirror_top']],
            'detail': (f"stacked-diode reference: bottom={stack['ref_bot']}, cascode={stack['ref_top']}; "
                       f"mirrored output: bottom={stack['mirror_bot']}, cascode={stack['mirror_top']}"),
            'glayout_call': f"no dedicated module -- compose from {primitive_call} stacked in series, see doc",
            'doc': 'circuit-specific/cascode-current-mirror/cascode-current-mirror-layout.md',
        })
        classified.update(stack.values())
        ref_group = mirror_groups.get(stack['ref_bot'])
        if ref_group is not None:
            ref_group.discard(stack['mirror_bot'])
            if not ref_group:
                del mirror_groups[stack['ref_bot']]

    # FVF-based (low-voltage) cascode current mirrors: consume both FVF
    # matches so they aren't ALSO reported as standalone FVF findings.
    fvf_consumed: set[int] = set()
    for cascode in find_fvf_cascode_mirrors(devices, fvf_matches):
        findings.append({
            'topology': 'cascode_current_mirror',
            'devices': [*cascode['bias_fvf'], *cascode['cascode_fvf'], cascode['mirror_bot'], cascode['mirror_top']],
            'detail': (f"FVF-biased (low-voltage): bias_fvf={cascode['bias_fvf']}, "
                       f"cascode_fvf={cascode['cascode_fvf']}; "
                       f"mirrored output: bottom={cascode['mirror_bot']}, cascode={cascode['mirror_top']}"),
            'glayout_call': "low_voltage_cmirror(pdk, width=(...), length=..., fingers=(...), multipliers=(...))",
            'doc': 'circuit-specific/cascode-current-mirror/cascode-current-mirror-layout.md',
        })
        classified.update((*cascode['bias_fvf'], *cascode['cascode_fvf'], cascode['mirror_bot'], cascode['mirror_top']))
        for idx, pair in enumerate(fvf_matches):
            if pair == tuple(cascode['bias_fvf']) or pair == tuple(cascode['cascode_fvf']):
                fvf_consumed.add(idx)

    for idx, fvf in enumerate(fvf_matches):
        if idx in fvf_consumed:
            continue
        findings.append({
            'topology': 'flipped_voltage_follower',
            'devices': list(fvf),
            'detail': f"input_fet={fvf[0]}, feedback_fet={fvf[1]}",
            'glayout_call': "flipped_voltage_follower(pdk, device_type=..., width=(...), ...)",
            'doc': 'circuit-specific/flipped-voltage-follower/fvf-layout.md',
        })
        classified.update(fvf)

    # Check every pair of diff-pair-like candidates for cross-coupling.
    # A matched pair of candidates becomes one quad_pair finding covering
    # all 4 devices; unmatched candidates fall through as plain diff pairs.
    consumed_pair_indices = set()
    for i, pair1 in enumerate(diff_pair_candidates):
        if i in consumed_pair_indices:
            continue
        for j in range(i + 1, len(diff_pair_candidates)):
            if j in consumed_pair_indices:
                continue
            pair2 = diff_pair_candidates[j]
            if is_cross_coupled_quad(pair1, pair2):
                devs = [pair1[0]['name'], pair1[1]['name'], pair2[0]['name'], pair2[1]['name']]
                findings.append({
                    'topology': 'quad_pair',
                    'devices': devs,
                    'detail': (f"half-pair 1 tail={pair1[0]['source']} ({pair1[0]['name']},{pair1[1]['name']}), "
                               f"half-pair 2 tail={pair2[0]['source']} ({pair2[0]['name']},{pair2[1]['name']}), "
                               f"cross-coupled at outputs {sorted({pair1[0]['drain'], pair1[1]['drain']})}"),
                    'glayout_call': quad_pair_glayout_call(pair1[0]['kind']),
                    'doc': 'circuit-specific/quad-pair/quad-pair-layout.md',
                })
                classified.update(devs)
                consumed_pair_indices.update((i, j))
                break

    for idx, (a, b) in enumerate(diff_pair_candidates):
        if idx in consumed_pair_indices:
            continue
        findings.append({
            'topology': 'differential_pair',
            'devices': [a['name'], b['name']],
            'detail': f"shared tail={a['source']}, gates={a['gate']}/{b['gate']}",
            'glayout_call': diff_pair_glayout_call(a['kind']),
            'doc': 'circuit-specific/differential-pair/differential-pair-layout.md',
        })
        classified.update((a['name'], b['name']))

    for ref, mirrors in mirror_groups.items():
        mirrors_s = sorted(mirrors)
        n = len(mirrors_s)
        findings.insert(0, {
            'topology': 'current_mirror',
            'devices': [ref] + mirrors_s,
            'detail': (f"reference(diode-connected)={ref}, mirrors={mirrors_s} "
                       f"({n} output leg{'s' if n != 1 else ''})"),
            'glayout_call': current_mirror_glayout_call(dev_by_name[ref]['kind'], n),
            'doc': 'circuit-specific/current-mirror/current-mirror-layout.md',
        })

    unclassified = [d['name'] for d in devices if d['name'] not in classified]
    return findings, unclassified


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("netlist", help="path/to/design.sp")
    parser.add_argument("--out-json", default=None, help="also write findings/unclassified as JSON")
    parser.add_argument("--out-md", default=None, help="also write findings/unclassified as a human-readable table")
    args = parser.parse_args()

    path = args.netlist
    devices = parse_devices(path)
    if not devices:
        sys.exit(f"no MOSFET/BJT device lines found in {path}")

    findings, unclassified = detect(devices)

    print(f"=== Topology scan: {Path(path).name} ({len(devices)} devices) ===\n")
    if not findings:
        print("No elementary topology signatures matched.")
    for f in findings:
        print(f"[{f['topology']}] {', '.join(f['devices'])}")
        print(f"  {f['detail']}")
        print(f"  glayout: {f['glayout_call']}")
        print(f"  doc:     {f['doc']}\n")

    if unclassified:
        print(f"Unclassified (single-device stages, or part of a composite\n"
              f"pattern not auto-detected -- see reference.md): {', '.join(unclassified)}")

    if args.out_json:
        out_path = Path(args.out_json)
        out_path.write_text(json.dumps(
            {"netlist": str(Path(path).resolve()), "findings": findings, "unclassified": unclassified}, indent=2))
        print(f"\n  wrote {out_path}")
    if args.out_md:
        lines = [f"# Topology scan: {Path(path).name}", ""]
        for f in findings:
            lines.append(f"- **[{f['topology']}]** {', '.join(f['devices'])} -- {f['detail']}")
            lines.append(f"  - glayout: `{f['glayout_call']}`")
            lines.append(f"  - doc: {f['doc']}")
        if unclassified:
            lines.append("")
            lines.append(f"Unclassified: {', '.join(unclassified)}")
        out_path = Path(args.out_md)
        out_path.write_text("\n".join(lines) + "\n")
        print(f"  wrote {out_path}")


if __name__ == "__main__":
    main()
