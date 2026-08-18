#!/usr/bin/env python3
"""Group a level's devices by how strongly they share nets -- a fast,
deliberately approximate pre-pass for `../SKILL.md`'s pattern recognition.

The idea: devices that belong to the same circuit pattern are wired to each
other far more than to anything else. Two MOS sharing a gate net *and* a
source net are very likely a current mirror; two sharing only a (non-rail)
source are likely a differential or cross-coupled pair; two resistors
sharing one net are likely a ladder segment. **None of that is guaranteed**
-- this pass proves nothing, it only narrows what the next step has to
consider, and gives the manual patterns (R/C, which no detector parses at
all) an actual starting point instead of a blank page.

Affinity between two devices = the summed weight of the nets they share:

  - **Supply rails are excluded outright.** VDD/VSS/GND/0 touch nearly
    every device; counting them collapses the whole circuit into one
    cluster and says nothing.
  - **Every other net is weighted 1/(fanout-1).** A net joining exactly two
    devices scores 1.0 -- an exclusive connection, the strongest evidence
    there is. A bias net gating five devices scores 0.25 per pair: real,
    but weak, which is exactly how a fanned-out bias net should read.

Each pair also carries its *terminal signature* (`g-g`, `s-s`, `d-s`, ...),
which is what actually points at a pattern, and each device carries a
`diode_connected` flag (`gate == drain` on the same device) -- the single
highest-value hint in the file, since it marks every mirror reference and
every diode load.

Two overlapping views come out, because neither alone is enough:

  **A -- shared-net groups**: every non-rail net with the devices on it.
  This is the only view that catches a 1:N mirror *family*: N legs on one
  gate net are pairwise weak (1/(fanout-1) each), so no clustering
  threshold recovers the whole family, but the net names it exactly.

  **B -- affinity cliques**: maximal sets whose members are ALL pairwise
  linked at or above `--threshold` (Bron-Kerbosch). Connected components
  were tried first and are useless here -- an amplifier is one connected
  graph, so any threshold low enough to catch a diff pair's shared tail
  chains every device into a single cluster.

Views overlap by design; a device legitimately sits in several patterns'
neighbourhoods (a tail device is both a mirror leg and the pair's tail),
and a partition would have to pick one and hide the other. Either way a
group is a QUESTION for the pattern step ("these belong together -- as
what?"), never an answer: that step still applies each pattern's own
Structure test from `../pattern-table.md`, and may split one group across
two patterns or match a pattern this pass never grouped.

Usage:
  python group_devices.py path/to/level_slice.sp
      [--threshold 0.5] [--out-json path/to/groups.json]

`build_decomposition.py` calls this per block and folds the result into
`circuit_decomposition.yaml`'s `scan` section -- run it standalone to debug
the grouping pass on one slice. Either way it is per slice, never on a
multi-block deck: net names are block-local, so cross-block name collisions
produce affinity that does not exist. Reads only.
"""
import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent.parent / "reference"))
from scan_hierarchy import join_continuations, split_instance, model_kind  # noqa: E402
from detect_topology import SUPPLY_RAIL_NAMES  # noqa: E402

# Terminal names per device family, in netlist order. Bulk is included for
# MOS: it is normally a rail (excluded as such), so a *shared bulk that
# scores* means a real shared well/tie node, which is worth seeing.
TERMINALS = {
    'mos': ['d', 'g', 's', 'b'],
    'bjt': ['c', 'b', 'e'],
    'res': ['1', '2'],
    'cap': ['1', '2'],
    'ind': ['1', '2'],
    'diode': ['1', '2'],
}

# Terminal-signature -> patterns worth testing in SKILL.md Step 2. A hint, never a
# verdict: every entry here still has to pass its own Structure test in
# ../pattern-table.md, and several signatures are genuinely ambiguous
# (`s-s` alone cannot tell a diff pair from a cross-coupled pair -- only
# gate origin can, which is that step's job).
# Ordered most-specific first (two shared terminals before one), so the
# printed candidate list leads with the pattern the evidence points at
# hardest rather than with whichever rule happened to be listed first.
HINTS = [
    ({'g-g', 's-s'}, 'same', ['current_mirror', 'cascode_current_mirror']),
    ({'d-g', 's-d'}, 'same', ['flipped_voltage_follower']),
    ({'d-g', 'g-d'}, 'same', ['cross_coupled_pair']),
    ({'d-d', 's-s'}, 'compl', ['transmission_gate']),
    ({'g-g'},        'same', ['current_mirror', 'beta_multiplier_bias']),
    ({'s-s'},        'same', ['differential_pair', 'cross_coupled_pair', 'quad_pair']),
    ({'d-s'},        'same', ['cascode_stage', 'cascode_current_mirror']),
    ({'d-d'},        'compl', ['push_pull_output', 'self_biased_reference']),
]
# Passive/mixed hints, keyed on the two devices' kinds (no signature test --
# a 2-terminal device's terminals carry no role).
KIND_HINTS = {
    ('res', 'res'): ['resistor_ladder'],
    ('cap', 'cap'): ['capacitor_bank'],
    ('cap', 'res'): ['rc_compensation_network'],
    ('res', 'cap'): ['rc_compensation_network'],
}


def is_rail(net: str) -> bool:
    return net.lower() in SUPPLY_RAIL_NAMES or net in ('0', 'gnd!')


def parse_slice(path: Path) -> list[dict]:
    """Every device in one level slice, MOS/BJT *and* passives -- unlike
    `detect_topology.parse_devices()`, which returns MOS/BJT only and is
    why every passive pattern is invisible to the auto pass."""
    devices = []
    for line in join_continuations(path):
        stripped = line.strip()
        if not stripped or stripped.startswith(('*', '.', '$')):
            continue
        letter = stripped[0].upper()
        name, nets, model, params = split_instance(stripped)
        if letter == 'X':
            kind = model_kind(model)
        elif letter in ('M', 'Q', 'R', 'C', 'L', 'D'):
            kind = {'M': 'mos', 'Q': 'bjt', 'R': 'res', 'C': 'cap', 'L': 'ind', 'D': 'diode'}[letter]
        else:
            continue  # sources, controlled sources: not part of a pattern
        terms = TERMINALS.get(kind)
        if not terms or len(nets) < len(terms) - (1 if kind == 'mos' else 0):
            continue  # unknown family, or too few nets to be that family
        low = model.lower()
        devices.append({
            'name': name, 'kind': kind, 'model': model, 'params': params,
            # Polarity, NOT kind, decides same-type vs complementary: both
            # sky130 MOS models classify as kind 'mos', so comparing kinds
            # would call an nfet and a pfet the same type and no
            # complementary hint (transmission gate, push-pull) could ever
            # fire.
            'polarity': ('n' if 'nfet' in low or 'npn' in low else
                         'p' if 'pfet' in low or 'pnp' in low else None),
            'terminals': dict(zip(terms, nets)),
            'diode_connected': kind in ('mos', 'bjt') and len(nets) > 1 and nets[0] == nets[1],
        })
    return devices


def net_fanout(devices: list[dict]) -> dict[str, int]:
    fanout: dict[str, int] = {}
    for dev in devices:
        for net in set(dev['terminals'].values()):
            fanout[net] = fanout.get(net, 0) + 1
    return fanout


def pair_affinity(a: dict, b: dict, fanout: dict[str, int]) -> tuple[float, list[dict]]:
    weight = 0.0
    evidence = []
    for ta, na in a['terminals'].items():
        for tb, nb in b['terminals'].items():
            if na != nb or is_rail(na):
                continue
            w = 1.0 / max(1, fanout[na] - 1)
            weight += w
            evidence.append({'net': na, 'signature': f"{ta}-{tb}", 'fanout': fanout[na], 'weight': round(w, 3)})
    return round(weight, 3), evidence


def hints_for(a: dict, b: dict, evidence: list[dict]) -> tuple[list[str], dict]:
    """(candidate patterns, pattern -> index of the rule that hinted it).
    The rank travels with the hint so a group can order its merged list by
    how specific the *matching rule* was -- ranking by position in the
    HINTS table instead would credit a pattern with the strongest rule it
    appears in anywhere, not the one that actually fired."""
    kind_hint = KIND_HINTS.get((a['kind'], b['kind']), [])
    flat = len(HINTS)
    if a['kind'] not in ('mos', 'bjt') or b['kind'] not in ('mos', 'bjt'):
        return kind_hint, {p: flat for p in kind_hint}
    # Order-independence WITHOUT collapsing direction: match the signature
    # set as written (a first) or as it would read with b first. Blanket
    # symmetrizing instead -- adding `g-d` for every `d-g` -- makes
    # cross_coupled_pair's reciprocity requirement vacuous, so it fires on
    # every diode connection in the circuit.
    sigs = {e['signature'] for e in evidence}
    flipped = {f"{s.split('-')[1]}-{s.split('-')[0]}" for s in sigs}
    same = (a['polarity'] == b['polarity'] if a['polarity'] and b['polarity']
            else a['kind'] == b['kind'])
    out: list[str] = []
    rank: dict[str, int] = {}
    for i, (want, kinds, patterns) in enumerate(HINTS):
        if not (want <= sigs or want <= flipped):
            continue
        if (kinds == 'same') != same:
            continue
        for j, p in enumerate(patterns):
            if p not in out:
                out.append(p)
                # Rule index, plus the pattern's position inside that rule
                # as a fraction: ties inside one rule keep the rule's own
                # order (a shared tail reads diff pair first) instead of
                # falling back to alphabetical.
                rank[p] = i + j / 100
    if (a['diode_connected'] or b['diode_connected']) and 'current_mirror' in out:
        out = ['current_mirror'] + [p for p in out if p != 'current_mirror']
        rank['current_mirror'] = -1.0
    if out:
        return out, rank
    return kind_hint, {p: flat for p in kind_hint}


def all_edges(devices: list[dict]) -> list[dict]:
    fanout = net_fanout(devices)
    edges = []
    for i, a in enumerate(devices):
        for b in devices[i + 1:]:
            weight, evidence = pair_affinity(a, b, fanout)
            if weight > 0:
                patterns, rank = hints_for(a, b, evidence)
                edges.append({'devices': [a['name'], b['name']], 'affinity': weight,
                              'evidence': evidence, 'candidate_patterns': patterns,
                              'candidate_rank': rank})
    return sorted(edges, key=lambda e: -e['affinity'])


def summarize(devices: list[dict], names: list[str], edges: list[dict], gid: str, **extra) -> dict:
    """Common shape for both views: members, cohesion, the diode-connected
    members (the highest-value single hint), and the union of the pairwise
    pattern candidates."""
    inner = [e for e in edges if set(e['devices']) <= set(names)]
    # Merged most-specific-first, so a group's list does not lead with
    # whichever edge happened to be enumerated first.
    best: dict[str, int] = {}
    for e in inner:
        for p in e['candidate_patterns']:
            r = e['candidate_rank'].get(p, len(HINTS))
            best[p] = min(best.get(p, r), r)
    candidates = sorted(best, key=lambda p: best[p])
    return {'id': gid, 'devices': names, 'size': len(names),
            'cohesion': round(sum(e['affinity'] for e in inner), 3),
            'candidate_patterns': candidates, 'edges': inner,
            'diode_connected': [d['name'] for d in devices if d['name'] in names and d['diode_connected']],
            **extra}


def net_groups(devices: list[dict], edges: list[dict]) -> list[dict]:
    """View A -- every non-rail net, with the devices on it and which
    terminal each one presents. This is the view that catches a 1:N mirror
    *family*: N legs on one gate net are not pairwise-strong (each pair
    scores only 1/(fanout-1)), so no clustering threshold recovers the
    whole family, but the net itself names it exactly."""
    by_net: dict[str, list[tuple[str, str]]] = {}
    for dev in devices:
        for term, net in dev['terminals'].items():
            if not is_rail(net):
                by_net.setdefault(net, []).append((dev['name'], term))
    groups = []
    for i, (net, touches) in enumerate(sorted(by_net.items(), key=lambda kv: -len(kv[1])), 1):
        names = sorted({name for name, _ in touches}, key=lambda n: [d['name'] for d in devices].index(n))
        if len(names) < 2:
            continue
        terminals = {name: sorted(t for n, t in touches if n == name) for name in names}
        groups.append(summarize(devices, names, edges, f"N{i}", net=net, terminals=terminals))
    return groups


def cliques(devices: list[dict], edges: list[dict], threshold: float) -> list[dict]:
    """View B -- maximal sets whose members are ALL pairwise linked at or
    above the threshold (Bron-Kerbosch). Overlapping by design: a device
    legitimately sits in several patterns' neighbourhoods (a tail device is
    both a mirror leg and the pair's tail), and forcing a partition would
    have to pick one and hide the other."""
    adj: dict[str, set[str]] = {d['name']: set() for d in devices}
    for e in edges:
        if e['affinity'] >= threshold:
            a, b = e['devices']
            adj[a].add(b)
            adj[b].add(a)

    found: list[set[str]] = []

    def expand(r: set[str], p: set[str], x: set[str]):
        if not p and not x:
            if len(r) > 1:
                found.append(set(r))
            return
        pivot = max(p | x, key=lambda v: len(adj[v]))
        for v in list(p - adj[pivot]):
            expand(r | {v}, p & adj[v], x & adj[v])
            p.discard(v)
            x.add(v)

    expand(set(), set(adj), set())
    order = [d['name'] for d in devices]
    out = [summarize(devices, sorted(c, key=order.index), edges, 'C?') for c in found]
    out.sort(key=lambda g: (-g['cohesion'], -g['size']))
    for i, g in enumerate(out, 1):
        g['id'] = f"C{i}"
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("slice", help="one level slice from scan_hierarchy.py --split-dir")
    parser.add_argument("--threshold", type=float, default=0.5,
                        help="minimum pair affinity that joins a group (default 0.5)")
    parser.add_argument("--out-json", default=None)
    args = parser.parse_args()

    path = Path(args.slice).resolve()
    devices = parse_slice(path)
    if not devices:
        sys.exit(f"no devices parsed from {path}")
    edges = all_edges(devices)
    nets = net_groups(devices, edges)
    cls = cliques(devices, edges, args.threshold)

    print(f"=== Affinity grouping: {path.name} ({len(devices)} devices, threshold {args.threshold}) ===")
    print("Candidate groups -- a QUESTION for the pattern step, not an answer.")
    diodes = [d['name'] for d in devices if d['diode_connected']]
    print(f"Diode-connected (gate==drain): {', '.join(diodes) if diodes else 'none'}\n")

    print("--- View A: shared-net groups (every device on one net) ---")
    for g in nets:
        terms = ', '.join(f"{n}.{'/'.join(g['terminals'][n])}" for n in g['devices'])
        print(f"[{g['id']}] net {g['net']} (fanout {g['size']}): {terms}")
        if g['candidate_patterns']:
            print(f"     test: {', '.join(g['candidate_patterns'])}")

    print(f"\n--- View B: affinity cliques (all members pairwise >= {args.threshold}) ---")
    for g in cls:
        print(f"[{g['id']}] {', '.join(g['devices'])}   cohesion={g['cohesion']}")
        for e in g['edges']:
            sig = ', '.join(f"{ev['signature']} on {ev['net']}" for ev in e['evidence'])
            print(f"     {e['devices'][0]}~{e['devices'][1]}  {e['affinity']}  [{sig}]")
        if g['candidate_patterns']:
            print(f"     test: {', '.join(g['candidate_patterns'])}")

    grouped = {n for g in cls for n in g['devices']} | {n for g in nets for n in g['devices']}
    loners = [d['name'] for d in devices if d['name'] not in grouped]
    if loners:
        print(f"\nIn no group (rail-only connections): {', '.join(loners)}")

    if args.out_json:
        out = Path(args.out_json)
        out.write_text(json.dumps({'slice': str(path), 'threshold': args.threshold, 'devices': devices,
                                   'net_groups': nets, 'cliques': cls, 'edges': edges}, indent=2))
        print(f"\n  wrote {out}")


if __name__ == "__main__":
    main()
