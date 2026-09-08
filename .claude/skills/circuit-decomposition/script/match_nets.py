"""Net-match candidates: which NETS a detected pattern asks layout to match.

Step 1 calls `candidates()` once per block slice and prints `format_lines()`;
`../SKILL.md` Step 4 authors `matched_nets` from what comes out. A tie group
makes two devices identical, which is not the same as making them *matched*:
a differential pair drawn common-centroid still has an offset if one gate
arrives on 40um of met2 and the other on 9um of met1, because the twins then
see different series R and different coupling C. This module names the nets
that constraint lands on.

How a candidate is found
------------------------
Per detected group, its devices are compared **terminal by terminal**, in
group order (a mirror's reference first, as `detect_topology` emits it):

  * every device agrees on that terminal  -> ONE net, a common node
  * they disagree                          -> a matched net SET

Terminal by terminal, never as net sets. A cross-coupled pair's two devices
touch the *same* two nets with drain and gate swapped, so a set difference
over their nets finds nothing at all -- the disagreement only exists per
terminal.

Supply rails are dropped on both paths. Two sources both on VSS are not
twins, and "VDD is common to these four devices" is not a layout constraint
anyone needs written down.

`b` is bulk on a MOS and base on a BJT, so terminals are normalised to ROLES
(`in`/`out`/`tail`/`bulk`) before any kind is assigned; a BJT diff pair's
base nets are its inputs exactly as a MOS pair's gates are.

NET_KIND, and what a missing entry means
----------------------------------------
`NET_KIND` is keyed by pattern ID, matching `../pattern-table.md`'s **Matched
nets** field -- when that field changes, this changes with it, and
pattern-table.md item 5 tells an author to check the two agree. **A pattern
absent from NET_KIND yields common nodes only**, which is right for a
deliberately asymmetric pattern (`flipped_voltage_follower`) and wrong for
anything else, so a new `auto` pattern needs a row here as well as in the
table.

Two kinds are deliberately never emitted here:

  * `sensitive` -- a high-impedance gain node matches nothing, so no terminal
    comparison can find it. It comes from the pattern row, authored by hand.
  * `differential` promoted from `branch` -- a mirror's leg drains are twins
    only if the legs are 1:1, and `m` is what decides that. This module says
    `branch` and attaches the question; Step 4 answers it and says which it
    chose.

Manual patterns (every R/C one, and `cross_coupled_pair`) reach no detector,
so they produce no candidates at all and Step 4 authors them from the printed
device table. That is the same blind spot Step 2 has, and it costs more here,
because the passive patterns are the ones whose whole constraint is parasitic.

Standalone:
    python match_nets.py <block-slice.sp>
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent.parent / "reference"))
import group_devices as grp  # noqa: E402

# Terminal letter -> role, per device kind. `b` is the reason this exists.
ROLE = {
    'mos': {'d': 'out', 'g': 'in', 's': 'tail', 'b': 'bulk'},
    'bjt': {'c': 'out', 'b': 'in', 'e': 'tail'},
}

# Readable terminal names for the report -- pattern-table.md asks for the
# terminal a net hangs off, never a net name from one design.
TERM_NAME = {
    'mos': {'d': 'drain', 'g': 'gate', 's': 'source', 'b': 'bulk'},
    'bjt': {'c': 'collector', 'b': 'base', 'e': 'emitter'},
}

# pattern ID -> role -> net kind. Mirrors ../pattern-table.md's Matched nets
# field; see the module docstring on what absence means.
NET_KIND = {
    # Gate and source common (the gate net is the one that bites: no DC
    # current, so its resistance is invisible to simulation while each leg
    # samples Vgs where it taps). Leg drains branch, not differential --
    # only a 1:1 m makes them twins.
    'current_mirror': {'in': 'common', 'tail': 'common', 'out': 'branch'},
    # One common gate net per level, same spine-not-chain rule; output
    # drains branch. The per-branch internal node is branch too, and
    # sensitive in itself -- that half is authored, not proposed here.
    'cascode_current_mirror': {'in': 'common', 'tail': 'common', 'out': 'branch'},
    # Two differential pairs -- gates (input) and drains (output) -- routed
    # as mirror images about the pair's axis. Tail common: both halves share
    # it by construction.
    'differential_pair': {'in': 'differential', 'out': 'differential', 'tail': 'common'},
    # The same two pairs, plus one differential_pair lacks: the two tail
    # nets are separate nodes and themselves a differential pair.
    'quad_pair': {'in': 'differential', 'out': 'differential', 'tail': 'differential'},
    # Gates are clk/clkb -- match their RC, since charge injection cancels
    # only if both gates switch at the same instant with the same edge.
    # Source and drain are common by construction.
    'transmission_gate': {'in': 'complementary', 'out': 'common', 'tail': 'common'},
    # flipped_voltage_follower is deliberately ABSENT: deliberately
    # asymmetric, so common nodes only is the correct answer.
}

# A branch set is twins only when the legs are 1:1. Carried on the candidate
# so Step 4 cannot promote it to `differential` without saying why.
BRANCH_NOTE = ("twins only if the legs are 1:1 -- check the group's `m` "
               "before promoting to `differential`, and say which you chose")


def _kind_of(device: dict) -> str:
    """'mos' or 'bjt' for a detected device, or None for anything else.

    `detect_topology` only ever classifies MOS and BJT, so a finding's
    devices are always one of the two; passives reach no finding.
    """
    k = (device.get('kind') or '').lower()
    if k in ('nmos', 'pmos', 'mos'):
        return 'mos'
    if k in ('npn', 'pnp', 'bjt'):
        return 'bjt'
    return None


def candidates(devices: list[dict], findings: list[dict]) -> list[dict]:
    """Net-match candidates for every detected pattern in one block.

    `devices` is `group_devices.parse_slice()`'s output (every device in the
    slice, passives included); `findings` is `detect_topology.detect()`'s.
    Returns one entry per (pattern, terminal) worth recording.
    """
    by_name = {d['name']: d for d in devices}
    out = []

    for finding in findings or []:
        pattern = finding.get('topology')
        names = [n for n in finding.get('devices', []) if n in by_name]
        if len(names) < 2:
            continue                       # a one-device pattern twins nothing
        group = [by_name[n] for n in names]

        kinds = {_kind_of(d) for d in group}
        kind = kinds.pop() if len(kinds) == 1 else None
        if kind not in ROLE:
            continue                       # mixed or unrecognised: no vocabulary

        roles = NET_KIND.get(pattern)      # None -> common nodes only
        for term in ROLE[kind]:
            nets = [(d.get('terminals') or {}).get(term) for d in group]
            if any(n is None for n in nets):
                continue                   # terminal absent on some device
            if any(grp.is_rail(n) for n in nets):
                continue                   # rails are not a constraint
            role = ROLE[kind][term]
            agree = len(set(nets)) == 1

            if agree:
                net_kind = (roles or {}).get(role, 'common')
                # A terminal every device shares is a common node whatever
                # the table calls the disagreeing ones. Reporting it as
                # `differential` because the pattern has differential
                # terminals elsewhere would be a lie about this net.
                if net_kind in ('differential', 'complementary', 'branch'):
                    net_kind = 'common'
                out.append({
                    'pattern': pattern, 'devices': names, 'terminal': TERM_NAME[kind][term],
                    'role': role, 'nets': [nets[0]], 'kind': net_kind, 'note': '',
                })
                continue

            if roles is None:
                continue                   # unregistered pattern: common only
            net_kind = roles.get(role)
            if not net_kind or net_kind == 'common':
                # Disagreeing on a terminal the table calls common is not a
                # candidate -- it is a structural surprise, and saying so is
                # more use than inventing a kind for it.
                out.append({
                    'pattern': pattern, 'devices': names, 'terminal': TERM_NAME[kind][term],
                    'role': role, 'nets': nets, 'kind': 'unexpected', 'note':
                    f"pattern-table calls this terminal `common`, but the devices "
                    f"disagree on it -- check the match before authoring",
                })
                continue
            out.append({
                'pattern': pattern, 'devices': names, 'terminal': TERM_NAME[kind][term],
                'role': role, 'nets': nets, 'kind': net_kind,
                'note': BRANCH_NOTE if net_kind == 'branch' else '',
            })

    return out


def format_lines(cands: list[dict]) -> list[str]:
    """The `[net-match: <pattern>]` lines Step 1 prints under each block."""
    lines = []
    for c in cands or []:
        nets = ' / '.join(c['nets'])
        lines.append(f"  [net-match: {c['pattern']}] {c['terminal']}: {nets}"
                     f" -> {c['kind']}  ({', '.join(c['devices'])})")
        if c['note']:
            lines.append(f"      {c['note']}")
    return lines


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        return print(__doc__.strip().splitlines()[-1]) or 2
    sys.path.insert(0, str(SCRIPT_DIR.parent.parent.parent / "reference"))
    import detect_topology as topo
    path = Path(argv[0])
    devices = grp.parse_slice(path)
    mos = topo.parse_devices(str(path))
    findings, _ = topo.detect(mos) if mos else ([], [])
    cands = candidates(devices, findings)
    if not cands:
        print(f"  no net-match candidates in {path.name} "
              f"({len(findings)} finding(s), {len(devices)} device(s))")
        return 0
    for line in format_lines(cands):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
