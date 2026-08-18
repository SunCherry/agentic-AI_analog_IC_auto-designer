#!/usr/bin/env python3
"""Read a DECOMPOSED netlist -- one where
`../../circuit-decomposition/script/decompose_netlist.py` has already
pulled recognized diff_pair / current_mirror device groups out into their
own `.subckt` blocks -- and recover, per top-level subckt INSTANCE, the
real devices inside it with their nets mapped back to top-level net names.

Why this exists: a device line parser matches raw device lines, so on a
decomposed netlist it sees ONLY the leftover top-level devices -- confirmed
against a decomposed two-stage netlist, where it returns just `XMP3`/`XMP4`
and misses every device inside the three subckts. Expanding them here keeps
the whole pipeline seeing one flat device list.

What it produces is a FLATTENED netlist (every device at top level, every
net renamed to its top-level name via the instance's own pin mapping) plus
a record of which devices came from which subckt instance. That keeps the
whole downstream pipeline unchanged -- `build_device_table()`,
`device_index`, `anneal_placement.py`, `route_nets.py` all keep seeing
real top-level net names, exactly as they do for a flat netlist.
`generate_primitives.py` writes the flattened text out as
`primitives/flattened.sp`, a real inspectable artifact, rather than
transforming the netlist invisibly in memory.

**This reader assigns no topology.** A subckt's name is a label someone
wrote; which devices form a current mirror or a differential pair is read
from `circuit_decomposition.yaml` (see `decomposition_patterns.py`), the
one user-confirmed source for it. This file used to re-derive that
structurally via `detect_topology.py`'s `detect()`; that second, unreviewed
opinion is gone. What survives here is the `instance` -> devices record,
which `generate_primitives.py` carries into `manifest.json` as provenance.

Scope, stated rather than silently assumed:
  - One level of subckt expansion. A macro subckt that itself instantiates
    another subckt is reported via `nested` and left unexpanded -- this is
    a placer input reader, not a general hierarchical SPICE flattener.
  - `.include`/`.inc` are resolved relative to the including file's own
    directory (that's how the decomposed netlists this reads are written),
    with cycle protection.
  - Device names are kept EXACTLY as written when globally unique across
    the whole flattened netlist -- so a decomposed netlist flattens back to
    the same device names its flat original had (`XMN1`, `XMP3`, ...),
    making a manifest from either path directly comparable. Only on a real
    collision (the same subckt instantiated more than once) does a device
    get an `<instance>.<device>` prefix, the standard SPICE hierarchical
    convention. Internal (non-pin) nets always get that prefix, since two
    instances' internals are genuinely different nodes.
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS_DIR = HERE.parent.parent
sys.path.insert(0, str(SKILLS_DIR / "schematic-sizing" / "script"))

import netlist_devices as devparse  # noqa: E402

_INCLUDE_RE = re.compile(r'^\s*\.inc(?:lude)?\s+["\']?([^"\'\s]+)["\']?', re.IGNORECASE)
_PARAM_TOKEN_RE = re.compile(r'^\w+\s*=')


def read_lines_with_includes(path: Path, _seen=None) -> list[str]:
    """Continuation-merged (`+`) lines, with `.include`/`.inc` resolved
    inline relative to the including file's own directory. Cycle-protected
    via `_seen` (an already-included file is skipped, not re-expanded)."""
    _seen = _seen if _seen is not None else set()
    path = path.resolve()
    if path in _seen:
        return []
    _seen.add(path)

    joined = []
    for raw in path.read_text().splitlines():
        if raw.strip().startswith('+') and joined:
            joined[-1] += ' ' + raw.strip()[1:]
        else:
            joined.append(raw)

    out = []
    for line in joined:
        m = _INCLUDE_RE.match(line)
        if m:
            inc = (path.parent / m.group(1)).resolve()
            if inc.exists():
                out.extend(read_lines_with_includes(inc, _seen))
            else:
                out.append(f"* [subckt_macros] unresolved include: {m.group(1)}")
            continue
        out.append(line)
    return out


def split_subckts(lines: list[str]) -> tuple[dict, list[str]]:
    """Returns ({name: {'pins': [...], 'body': [...]}}, free_lines) --
    `free_lines` being everything outside any `.subckt`/`.ends` block."""
    subckts = {}
    free = []
    current = None
    for line in lines:
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith('.subckt'):
            tokens = stripped.split()
            current = tokens[1]
            subckts[current] = {'pins': tokens[2:], 'body': []}
            continue
        if low.startswith('.ends'):
            current = None
            continue
        if current is not None:
            subckts[current]['body'].append(line)
        else:
            free.append(line)
    return subckts, free


def match_subckt_instance(line: str, subckts: dict):
    """If `line` is an X-instance of one of `subckts`, returns
    (instance_name, subckt_name, [net, ...]); else None. Trailing `k=v`
    parameter tokens are stripped before taking the last token as the
    subckt name, so `X0 a b c MYCELL w=1` resolves to MYCELL, not `w=1`."""
    stripped = line.strip()
    if not stripped or stripped.startswith(('*', '.')):
        return None
    tokens = stripped.split()
    if len(tokens) < 2 or not tokens[0].upper().startswith('X'):
        return None
    while len(tokens) > 2 and _PARAM_TOKEN_RE.match(tokens[-1]):
        tokens.pop()
    subckt_name = tokens[-1]
    if subckt_name not in subckts:
        return None
    return tokens[0], subckt_name, tokens[1:-1]


def _rewrite_nets(line: str, net_map: dict) -> str | None:
    """Rewrites a device line's net tokens through `net_map`, keeping the
    instance name, model, and every parameter byte-for-byte. Returns None
    if `netlist_devices.parse_devices()` doesn't recognize the line as a
    device at all (a comment, a directive, a bare source) -- reusing that
    parser rather than a fourth net/model splitter, so "where do this
    line's nets end" can't drift from how the rest of this pipeline
    already answers it."""
    parsed = devparse.parse_devices(line)
    if not parsed:
        return None
    d = parsed[0]
    nets = [net_map.get(n, n) for n in d['nets']]
    model = d.get('model') or ''
    tail = ' '.join(f"{k}={v}" for k, v in d['params'].items())
    parts = [d['name'], *nets]
    if model:
        parts.append(model)
    if tail:
        parts.append(tail)
    return ' '.join(parts)


def _device_names_in(body: list[str]) -> list[str]:
    names = []
    for line in body:
        parsed = devparse.parse_devices(line)
        if parsed:
            names.append(parsed[0]['name'])
    return names


class FlattenResult:
    def __init__(self, flat_lines, groups, top_device_names, top_subckt, top_pins, nested):
        self.flat_lines = flat_lines
        self.groups = groups                    # [{instance, subckt, devices}]
        self.top_device_names = top_device_names
        self.top_subckt = top_subckt
        self.top_pins = top_pins
        self.nested = nested                    # subckt names holding further instances

    def flat_text(self) -> str:
        header = f".subckt {self.top_subckt} {' '.join(self.top_pins)}" if self.top_subckt else None
        lines = ([header] if header else []) + self.flat_lines + ([f".ends {self.top_subckt}"] if header else [])
        return "\n".join(lines) + "\n"


def flatten(netlist_path, top_subckt: str | None = None) -> FlattenResult | None:
    """Returns None (not an error) if this netlist has no top-level subckt
    instances to expand -- i.e. it isn't decomposed, and the caller should
    take its ordinary flat path unchanged."""
    netlist_path = Path(netlist_path).resolve()
    lines = read_lines_with_includes(netlist_path)
    subckts, free = split_subckts(lines)
    if not subckts:
        return None

    # The top-level circuit: the subckt nobody else instantiates (a
    # decomposed macro is, by construction, instantiated by the top). If
    # the file's devices sit outside any subckt at all, `free` is the top.
    instantiated = set()
    for name, sub in subckts.items():
        for line in sub['body']:
            hit = match_subckt_instance(line, subckts)
            if hit:
                instantiated.add(hit[1])
    if top_subckt is None:
        candidates = [n for n in subckts if n not in instantiated]
        top_subckt = candidates[0] if len(candidates) == 1 else None
    if top_subckt is not None and top_subckt not in subckts:
        raise ValueError(f"{netlist_path}: no .subckt named {top_subckt!r}")

    if top_subckt is not None:
        top_body = subckts[top_subckt]['body']
        top_pins = subckts[top_subckt]['pins']
    else:
        top_body, top_pins = free, []

    instances = []
    passthrough = []
    for line in top_body:
        hit = match_subckt_instance(line, subckts)
        if hit:
            instances.append(hit)
        else:
            passthrough.append(line)
    if not instances:
        # Nothing to expand. Harmless for an ordinary flat netlist whose one
        # `.subckt` wraps every device (the caller's line-based parser reads
        # those directly). Not harmless when NO top could be identified and
        # every device sits inside some subckt: the caller's parser is flat,
        # so it reads all of those bodies at once with their LOCAL, unmapped
        # net names -- two blocks' internal `net1`s merge into one node, and
        # nothing downstream can tell. Say so rather than returning a quiet
        # None.
        if top_subckt is None and not _device_names_in(top_body):
            ambiguous = [n for n in subckts if n not in instantiated]
            print(f"  WARNING: {netlist_path.name}: no top-level device lines and no expandable "
                  f"subckt instance, so nothing was flattened -- every device sits inside a "
                  f".subckt and will be read with its LOCAL net names, un-mapped (same-named "
                  f"internal nets in different blocks silently merge). "
                  + (f"More than one subckt is never instantiated ({ambiguous}); pass "
                     f"--top-subckt to say which is the top-level circuit."
                     if len(ambiguous) > 1 else
                     f"No subckt is both uninstantiated and instantiating others."),
                  file=sys.stderr)
        return None

    # Name collisions only matter across the instances we actually expand.
    all_names = list(_device_names_in(passthrough))
    for _inst, sub_name, _nets in instances:
        all_names.extend(_device_names_in(subckts[sub_name]['body']))
    seen, colliding = set(), set()
    for n in all_names:
        (colliding if n in seen else seen).add(n)

    nested = []
    flat_lines = list(passthrough)
    groups = []
    for inst_name, sub_name, inst_nets in instances:
        sub = subckts[sub_name]
        if len(inst_nets) != len(sub['pins']):
            raise ValueError(
                f"{netlist_path}: instance {inst_name} passes {len(inst_nets)} net(s) "
                f"but .subckt {sub_name} declares {len(sub['pins'])} pin(s)")
        net_map = dict(zip(sub['pins'], inst_nets))

        group_lines, group_names = [], []
        for line in sub['body']:
            if match_subckt_instance(line, subckts):
                nested.append(sub_name)
                continue
            parsed = devparse.parse_devices(line)
            if not parsed:
                continue
            d = parsed[0]
            local_map = dict(net_map)
            for n in d['nets']:
                if n not in local_map:
                    local_map[n] = f"{inst_name}.{n}"   # internal node
            rewritten = _rewrite_nets(line, local_map)
            if rewritten is None:
                continue
            if d['name'] in colliding:
                rewritten = f"{inst_name}.{rewritten}"
                group_names.append(f"{inst_name}.{d['name']}")
            else:
                group_names.append(d['name'])
            group_lines.append(rewritten)

        flat_lines.extend(group_lines)
        groups.append({
            'instance': inst_name,
            'subckt': sub_name,
            'devices': group_names,
        })

    return FlattenResult(flat_lines, groups, _device_names_in(passthrough),
                          top_subckt, top_pins, sorted(set(nested)))


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("netlist")
    parser.add_argument("--top-subckt", default=None)
    parser.add_argument("--print-flat", action="store_true", help="print the flattened netlist text")
    args = parser.parse_args()

    result = flatten(args.netlist, top_subckt=args.top_subckt)
    if result is None:
        print(f"{args.netlist}: no top-level subckt instances -- not a decomposed netlist "
              f"(the flat path applies unchanged)")
        return
    print(f"=== {args.netlist} (top: {result.top_subckt}) ===")
    for g in result.groups:
        print(f"  [{g['instance']}] {g['subckt']}: {g['devices']}")
    print(f"  top-level (ungrouped) devices: {result.top_device_names}")
    if result.nested:
        print(f"  WARNING: nested subckt instance(s) inside {result.nested} left unexpanded")
    if args.print_flat:
        print()
        print(result.flat_text())


if __name__ == "__main__":
    main()
