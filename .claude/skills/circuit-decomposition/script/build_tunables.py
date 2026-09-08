#!/usr/bin/env python3
"""Turn detected patterns into tie groups, a tunable-variable registry, and
a Jinja2 netlist template -- deterministically, from `../pattern-table.md`'s
own "Tie group" rules.

WHY THIS EXISTS. `tie_groups` used to be an empty stub that SKILL.md's
judgment steps filled in by hand, and `schematic-sizing` kept its own
parallel answer in `structure_groups.json`. Two hand-maintained sources for
one fact drift, and they did: on `test_miller_ota` a current mirror
(XMN4 ref, XMN3/XMN5 legs) was authored as `ratio_conflict` with three
INDEPENDENT free widths, because `m` was being treated as frozen. Sizing
then tuned the three legs to w=47.2 / 85 / 100 -- three different unit
devices in one mirror, which is not a mirror. `pattern-table.md` had said
`tied: [w, l]`, `ratio_carrier: m` the whole time.

So the rules move into code, and the template becomes the single artifact
sizing writes through:

  - **Tying is structural, not clerical.** Two devices that share a tunable
    share the literal `{{ VAR }}` in the template. A group cannot drift
    apart, because there is no second place to write a per-device value.
  - **`m` is a real sizing variable wherever a pattern names it
    `ratio_carrier`** (current_mirror, cascode_current_mirror,
    resistor_ladder, capacitor_bank) and is frozen everywhere else. That is
    what dissolves the "ratio conflict": a mirror whose legs are not
    expressible as one shared `w` IS expressible as one shared unit `w`
    times per-leg integer `m`, which is also how it gets laid out.

Every variable carries `tunable_by`: `sizing` (the loop may propose it) or
`fold` (templated only so `fold_wide_devices.py` can re-render a PDK-bin fold
through it -- sizing must leave it alone). `m` outside a ratio carrier is
`fold`; `w`/`l` are always `sizing`.

`nf` is never templated here -- it is `device-shaper`'s to choose, after
sizing converges.

Usage (normally called by build_decomposition.py, not directly):
  python build_tunables.py <netlist.sp> [--top NAME] [--out-j2 PATH]
      [--print-groups]

Reads the netlist only; never writes to it.
"""
import argparse
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent.parent / "reference"))
import group_devices as grp  # noqa: E402
import detect_topology as topo  # noqa: E402

# --------------------------------------------------------------------------
# The registry. One row per pattern, transcribed from pattern-table.md's
# "Tie group" field. A pattern absent here gets DEFAULT_RULE -- no tie, its
# own free w/l -- which is what that file's `none` rows mean.
#
# `tied`          parameters that become ONE shared variable across members
# `ratio_carrier` the parameter that stays per-device and is TUNABLE; the
#                 ratio between members lives here and nowhere else
# `free`          parameters that stay per-device and tunable, untied
# --------------------------------------------------------------------------
TIE_RULES = {
    'current_mirror':         {'tied': ['w', 'l'], 'ratio_carrier': 'm',  'free': []},
    'cascode_current_mirror': {'tied': ['w', 'l'], 'ratio_carrier': 'm',  'free': [], 'per_level': True},
    'differential_pair':      {'tied': ['w', 'l', 'm'], 'ratio_carrier': None, 'free': []},
    'quad_pair':              {'tied': ['w', 'l'], 'ratio_carrier': None, 'free': []},
    'cross_coupled_pair':     {'tied': ['w', 'l', 'm'], 'ratio_carrier': None, 'free': []},
    'transmission_gate':      {'tied': ['l'],      'ratio_carrier': None, 'free': ['w']},
    'resistor_ladder':        {'tied': ['w', 'l'], 'ratio_carrier': 'm',  'free': []},
    'capacitor_bank':         {'tied': ['w', 'l'], 'ratio_carrier': 'm',  'free': []},
}
DEFAULT_RULE = {'tied': [], 'ratio_carrier': None, 'free': ['w', 'l']}

# Patterns that describe a RELATIONSHIP between devices already claimed by
# another pattern, rather than a geometry group of their own. They must not
# claim devices, or they would steal a mirror's legs and dissolve its tie.
ADVISORY_PATTERNS = {
    'self_biased_reference', 'rc_compensation_network', 'diode_connected_load',
    'beta_multiplier_bias', 'push_pull_output',
}

PARAM_RE = re.compile(r'\b(w|l|m|nf)\s*=\s*([^\s]+)', re.IGNORECASE)


def _var_base(instance: str) -> str:
    """`XMN4` -> `MN4`, `R0` -> `R0`. The leading X is SPICE's subcircuit-call
    marker, not part of the device's name."""
    return instance[1:] if instance[:1].upper() == 'X' and len(instance) > 1 else instance


def _params(dev: dict) -> dict:
    return {k.lower(): v for k, v in PARAM_RE.findall(dev.get('params') or '')}


def _num(text, default=None):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _total_width(dev: dict) -> float:
    """`w * m` -- the device's real drawn width. The ratio between a mirror's
    legs is a TOTAL-width ratio; per-copy `w` alone says nothing about it."""
    p = _params(dev)
    return _num(p.get('w'), 0.0) * _num(p.get('m'), 1.0)


def claim_patterns(findings: list) -> list:
    """Each device belongs to ONE geometry group. Bigger patterns claim
    first, so a cascode mirror keeps its four devices instead of losing two
    to the simple mirror inside it; advisory patterns claim nothing."""
    ordered = sorted(
        (f for f in findings if f['topology'] not in ADVISORY_PATTERNS),
        key=lambda f: (-len(f['devices']), f['devices'][0]),
    )
    claimed, kept = set(), []
    for f in ordered:
        members = [d for d in f['devices'] if d not in claimed]
        if len(members) < 2:
            continue  # every member already spoken for
        claimed.update(members)
        kept.append({**f, 'devices': members})
    return kept, claimed


def derive(devices: list, findings: list):
    """-> (tie_groups, tunables). `tunables` is an ordered list of dicts:
    {name, param, members, seed, kind}, `kind` being 'continuous' (w/l) or
    'integer' (m)."""
    by_name = {d['name']: d for d in devices}
    patterns, claimed = claim_patterns(findings)

    tie_groups, tunables = [], []
    # device name -> {param: variable} , the binding the template renders from
    binding: dict[str, dict] = {d['name']: {} for d in devices}

    def add_var(name, param, members, seed, kind, tunable_by='sizing'):
        tunables.append({'name': name, 'param': param, 'members': list(members),
                         'seed': seed, 'kind': kind, 'tunable_by': tunable_by})
        for m in members:
            binding[m][param] = name

    for idx, pat in enumerate(patterns, start=1):
        topology = pat['topology']
        rule = TIE_RULES.get(topology, DEFAULT_RULE)
        members = pat['devices']
        # The reference anchors the variable name and the unit device. For a
        # mirror it is the diode-connected leg; otherwise the first member.
        ref = next((m for m in members if by_name[m].get('diode_connected')), members[0])
        base = _var_base(ref)

        group = {'id': f'tg{idx}_{topology}', 'pattern': topology,
                 'devices': members, 'status': 'ok',
                 'tied': list(rule['tied']),
                 'ratio_carrier': rule['ratio_carrier'] or 'none',
                 'free': list(rule['free'])}

        for param in rule['tied']:
            seeds = [_num(_params(by_name[m]).get(param)) for m in members]
            seeds = [s for s in seeds if s is not None]
            seed = seeds[0] if seeds else None
            if param == 'm':
                # A tied `m` (differential pair) is one shared integer.
                seed = int(round(seed)) if seed else 1
                add_var(f'{base}_M', 'm', members, seed, 'integer')
            else:
                add_var(f'{base}_{param.upper()}', param, members, seed, 'continuous')

        carrier = rule['ratio_carrier']
        if carrier == 'm':
            # One shared UNIT device (the tied w/l above), and a per-member
            # integer count. Seeded to preserve each member's ORIGINAL TOTAL
            # width against that unit -- so the starting bias point is the
            # netlist's, not a 1-copy fraction of it.
            unit_w = _num(_params(by_name[ref]).get('w')) or 1.0
            ratios = {}
            for m in members:
                count = max(1, int(round(_total_width(by_name[m]) / unit_w))) if unit_w else 1
                add_var(f'{_var_base(m)}_M', 'm', [m], count, 'integer')
                ratios[m] = {'w_total_as_written': round(_total_width(by_name[m]), 4),
                             'm_seed': count,
                             'w_total_seeded': round(unit_w * count, 4)}
            group['unit_device'] = ref
            group['ratio_seed'] = ratios

        for param in rule['free']:
            for m in members:
                seed = _num(_params(by_name[m]).get(param))
                if seed is None:
                    continue
                add_var(f'{_var_base(m)}_{param.upper()}', param, [m], seed, 'continuous')

        tie_groups.append(group)

    # Everything no pattern claimed: its own free w/l, nothing tied.
    for d in devices:
        if d['name'] in claimed:
            continue
        for param in ('w', 'l'):
            seed = _num(_params(d).get(param))
            if seed is None:
                continue
            add_var(f'{_var_base(d["name"])}_{param.upper()}', param, [d['name']],
                    seed, 'continuous')

    # Every REMAINING `m` is templated too, as `tunable_by: fold`.
    #
    # Not so sizing can move it -- sizing must not, and `tunable_by` is what
    # says so. It is because rendering is TOTAL: the whole netlist comes from
    # the template every time, so a value that is not a variable cannot be
    # written at all. `fold_wide_devices.py` legitimately multiplies `m` to fit
    # a PDK model bin on any device, matched or not, and before this it did
    # that with a literal text edit that the next render would have thrown
    # away. Templated and frozen-for-sizing is the honest way to hold both.
    for d in devices:
        if 'm' in binding.get(d['name'], {}):
            continue
        seed = _num(_params(d).get('m'))
        if seed is None:
            continue
        add_var(f'{_var_base(d["name"])}_M', 'm', [d['name']],
                max(1, int(round(seed))), 'integer', tunable_by='fold')

    return tie_groups, tunables, binding


def render_template(netlist: Path, binding: dict) -> str:
    """The netlist verbatim, with every bound parameter replaced by its
    `{{ VAR }}`. Unbound parameters keep their literal value -- `nf` above
    all, which belongs to device-shaper."""
    out = []
    for line in netlist.read_text().splitlines():
        stripped = line.lstrip()
        name = stripped.split()[0] if stripped.split() else ''
        vars_here = binding.get(name)
        if not vars_here or stripped.startswith('*'):
            out.append(line)
            continue

        def sub(match):
            param, value = match.group(1).lower(), match.group(2)
            var = vars_here.get(param)
            return f'{match.group(1)}={{{{ {var} }}}}' if var else match.group(0)

        out.append(PARAM_RE.sub(sub, line))
    return '\n'.join(out) + '\n'


J2_HEADER = """\
{#- AUTO-GENERATED by circuit-decomposition/script/build_tunables.py.
    Do not hand-edit: re-run the skill instead.

    This is the netlist with every TUNABLE parameter replaced by a Jinja2
    variable. Devices that must share a parameter share the variable, so a
    tie group moves as one BY CONSTRUCTION -- there is no per-device value
    to forget to update. The variable registry (seed, kind, members, which
    tie group each came from) is the `tunable_parameters` section of
    circuit_decomposition.yaml.

    `nf` is deliberately NOT templated -- device-shaper chooses it after
    sizing converges. Any parameter left as a literal below is frozen.
-#}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('netlist')
    ap.add_argument('--out-j2', default=None, help='where to write the .sp.j2 template')
    ap.add_argument('--print-groups', action='store_true')
    args = ap.parse_args()

    netlist = Path(args.netlist).resolve()
    devices = grp.parse_slice(netlist)
    mos = topo.parse_devices(str(netlist))
    findings, _ = topo.detect(mos) if mos else ([], [])
    tie_groups, tunables, binding = derive(devices, findings)

    if args.print_groups:
        for g in tie_groups:
            print(f"{g['id']:32s} {g['devices']} tied={g['tied']} "
                  f"carrier={g['ratio_carrier']} free={g['free']}")
        print()
        for t in tunables:
            print(f"  {t['name']:12s} {t['param']:3s} seed={t['seed']:<10} "
                  f"{t['kind']:10s} <- {t['members']}")

    if args.out_j2:
        out = Path(args.out_j2).resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(J2_HEADER + render_template(netlist, binding))
        print(f"wrote {out}")


if __name__ == '__main__':
    main()
