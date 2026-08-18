#!/usr/bin/env python3
"""Read a top-level SPICE netlist and report its `.subckt` hierarchy.

This is the FIRST pass of `../SKILL.md`: before any pattern recognition
happens, the design has to be split into its own levels, because a
netlist's `.subckt` blocks are the schematic's natural decomposition --
each block is one level of the hierarchy, drawn by whoever wrote the
schematic, not a grouping this tool invents.

Two things this pass exists to get right, both of which a naive scan gets
wrong:

  1. **An `X` line is not necessarily a sub-circuit call.** In sky130 (and
     gf180) every primitive device is ALSO an `X` instance --
     `XMN1 net4 Vin net3 VSS sky130_fd_pr__nfet_01v8 l=0.15 w=40`. An `X`
     line is a hierarchy edge only if its model token names a `.subckt`
     actually defined in this deck; otherwise it is a leaf device. Keying
     on the `X` prefix alone reports a flat 10-transistor netlist as a
     10-level-deep hierarchy.

  2. **`detect_topology.py` does not respect `.subckt` boundaries.** Its
     `parse_devices()` skips every line starting with `.`, so `.subckt` /
     `.ends` are invisible to it and the devices of ALL blocks land in one
     flat pool. Net names are LOCAL to a block, so two different blocks
     that both call a node `net1` can produce a match across blocks that
     does not exist in the circuit. `--split-dir` therefore writes one
     self-contained `.sp` slice per block, and `build_decomposition.py`
     runs the detector once per slice -- never once on the whole file.

`.include` / `.lib` lines are RECORDED but never followed. A PDK model
library defines hundreds of `.subckt`s that are not part of the design's
hierarchy; expanding them would bury the schematic in process models.
Primitives are leaves -- that is the point at which decomposition stops.

Usage:
  python scan_hierarchy.py path/to/top.sp
      [--split-dir path/to/slices] [--out-json path/to/hierarchy.json]
      [--top NAME]

`build_decomposition.py` calls `analyze()`/`write_slices()` directly and
folds the result into `circuit_decomposition.yaml`'s `hierarchy` section --
run this script standalone (with `--out-json`) to debug the hierarchy pass
on its own.
`--top` picks the root when the deck defines more than one uninstantiated
block. Reads only -- never writes to the input netlist (`../../../../CLAUDE.md`
Key Rules: a netlist in the loop is frozen).
"""
import argparse
import json
import re
from pathlib import Path

# A parameter token: `w=40`, `l=0.15`, `m=8`. Everything on an instance
# line before the first one of these is nets + the model/subckt name.
_PARAM_RE = re.compile(r'^[A-Za-z_][\w.]*\s*=')

# Leaf-device letter -> kind, for the non-X SPICE instance letters. `X` is
# deliberately absent: an X line's kind depends on its model token, which
# is what resolve_instance() is for.
_LETTER_KIND = {
    'M': 'mos', 'R': 'res', 'C': 'cap', 'L': 'ind',
    'D': 'diode', 'Q': 'bjt', 'J': 'jfet',
}
# Sources/controlled sources: present in testbenches, never a design's own
# hierarchy. Counted separately so a stray V/I in a netlist is visible
# rather than silently dropped.
_SOURCE_LETTERS = set('VIEGFH')


def join_continuations(path: Path) -> list[str]:
    """`+`-prefixed continuation lines folded into the line above -- the
    same convention `detect_topology.parse_devices()` uses."""
    joined: list[str] = []
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith('+') and joined:
            joined[-1] += ' ' + stripped[1:].strip()
        else:
            joined.append(line)
    return joined


def model_kind(model: str) -> str:
    """Classify a leaf device by its model name (sky130/gf180 both encode
    the device family in the model string: ...__nfet_01v8, ...__res_generic_po,
    ...__cap_mim_m3_1)."""
    m = model.lower()
    if 'fet' in m:
        return 'mos'
    if 'npn' in m or 'pnp' in m:
        return 'bjt'
    if 'res' in m:
        return 'res'
    if 'cap' in m:
        return 'cap'
    if 'diode' in m:
        return 'diode'
    return 'unknown'


def split_instance(line: str) -> tuple[str, list[str], str, str]:
    """(instance_name, nets, model_or_subckt, params_text) for one instance
    line. The model token is the last token before the first `k=v`
    parameter (or the last token on the line if there are none)."""
    tokens = line.split()
    name, rest = tokens[0], tokens[1:]
    first_param = len(rest)
    for i, tok in enumerate(rest):
        if _PARAM_RE.match(tok):
            first_param = i
            break
    head = rest[:first_param]
    params = ' '.join(rest[first_param:])
    if not head:
        return name, [], '', params
    return name, head[:-1], head[-1], params


def parse_blocks(joined: list[str]) -> tuple[dict, list[str], list[str]]:
    """Returns (blocks, top_deck_lines, includes).

    blocks: name -> {pins, lines, start, end}. Nested `.subckt` blocks are
    handled with a stack; a nested block's lines belong to the nested
    block, not to its parent."""
    blocks: dict[str, dict] = {}
    top_deck: list[str] = []
    includes: list[str] = []
    stack: list[str] = []
    for i, line in enumerate(joined):
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith('.subckt'):
            tokens = stripped.split()
            name = tokens[1]
            blocks[name] = {'pins': tokens[2:], 'lines': [], 'start': i, 'end': None}
            stack.append(name)
            continue
        if low.startswith('.ends'):
            if stack:
                blocks[stack.pop()]['end'] = i
            continue
        if low.startswith(('.include', '.inc ', '.lib')):
            includes.append(stripped)
            continue
        if stack:
            blocks[stack[-1]]['lines'].append(line)
        else:
            top_deck.append(line)
    unterminated = [n for n, b in blocks.items() if b['end'] is None]
    if unterminated:
        raise ValueError(f".subckt without a matching .ends: {', '.join(unterminated)}")
    return blocks, top_deck, includes


def scan_instances(lines: list[str], subckt_names: set[str]) -> tuple[list[dict], list[dict]]:
    """Split one block's body into (hierarchy calls, leaf devices)."""
    calls: list[dict] = []
    leaves: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(('*', '.', '$')):
            continue
        letter = stripped[0].upper()
        name, nets, model, params = split_instance(stripped)
        if letter == 'X':
            if model in subckt_names:
                calls.append({'instance': name, 'subckt': model, 'nets': nets})
            else:
                leaves.append({'instance': name, 'model': model, 'kind': model_kind(model),
                               'nets': nets, 'params': params, 'line': stripped})
        elif letter in _LETTER_KIND:
            leaves.append({'instance': name, 'model': model, 'kind': _LETTER_KIND[letter],
                           'nets': nets, 'params': params, 'line': stripped})
        elif letter in _SOURCE_LETTERS:
            leaves.append({'instance': name, 'model': model, 'kind': 'source',
                           'nets': nets, 'params': params, 'line': stripped})
    return calls, leaves


def build_tree(root: str, blocks: dict, path: tuple[str, ...] = ()) -> dict:
    """Recursive instance tree. A block already on the current path is a
    recursive definition (illegal in SPICE, but a corrupted/merged deck can
    produce one) -- marked and not descended into."""
    node = {'subckt': root, 'children': []}
    if root in path:
        node['recursive'] = True
        return node
    for call in blocks[root]['calls']:
        child = build_tree(call['subckt'], blocks, path + (root,))
        child['instance'] = call['instance']
        node['children'].append(child)
    return node


def ascii_tree(node: dict, blocks: dict, prefix: str = '', is_last: bool = True, is_root: bool = True) -> list[str]:
    counts = blocks[node['subckt']]['device_counts']
    tally = ', '.join(f"{n} {k}" for k, n in sorted(counts.items())) or 'no leaf devices'
    label = node['subckt'] if is_root else f"{node['instance']} : {node['subckt']}"
    if node.get('recursive'):
        label += '   [RECURSIVE -- not expanded]'
        tally = ''
    connector = '' if is_root else ('`-- ' if is_last else '|-- ')
    out = [f"{prefix}{connector}{label}" + (f"   ({tally})" if tally else '')]
    child_prefix = prefix if is_root else prefix + ('    ' if is_last else '|   ')
    for i, child in enumerate(node['children']):
        out += ascii_tree(child, blocks, child_prefix, i == len(node['children']) - 1, False)
    return out


def mermaid(node: dict, blocks: dict, lines: list[str] | None = None, parent_id: str | None = None,
            counter: list[int] | None = None) -> list[str]:
    lines = ['graph TD'] if lines is None else lines
    counter = [0] if counter is None else counter
    counter[0] += 1
    node_id = f"n{counter[0]}"
    counts = blocks[node['subckt']]['device_counts']
    tally = ', '.join(f"{n} {k}" for k, n in sorted(counts.items()))
    inst = node.get('instance')
    label = f"{inst}<br/>{node['subckt']}" if inst else node['subckt']
    if tally:
        label += f"<br/><i>{tally}</i>"
    lines.append(f'    {node_id}["{label}"]')
    if parent_id:
        lines.append(f"    {parent_id} --> {node_id}")
    for child in node['children']:
        mermaid(child, blocks, lines, node_id, counter)
    return lines


def analyze(path: Path, top_name: str | None) -> dict:
    joined = join_continuations(path)
    blocks, top_deck, includes = parse_blocks(joined)
    subckt_names = set(blocks)

    for name, block in blocks.items():
        calls, leaves = scan_instances(block['lines'], subckt_names)
        block['calls'], block['leaves'] = calls, leaves
        counts: dict[str, int] = {}
        for leaf in leaves:
            counts[leaf['kind']] = counts.get(leaf['kind'], 0) + 1
        block['device_counts'] = counts

    top_calls, top_leaves = scan_instances(top_deck, subckt_names)

    instantiated = {c['subckt'] for b in blocks.values() for c in b['calls']}
    instantiated |= {c['subckt'] for c in top_calls}
    roots = [n for n in blocks if n not in instantiated]

    if top_calls or top_leaves:
        # The deck has device/call lines outside any .subckt -- that IS the
        # top level; every otherwise-uninstantiated block hangs under it.
        blocks['__TOP__'] = {'pins': [], 'lines': top_deck, 'calls': top_calls, 'leaves': top_leaves,
                             'device_counts': {}, 'synthetic': True}
        for leaf in top_leaves:
            blocks['__TOP__']['device_counts'][leaf['kind']] = \
                blocks['__TOP__']['device_counts'].get(leaf['kind'], 0) + 1
        top = '__TOP__'
    elif top_name:
        if top_name not in blocks:
            raise ValueError(f"--top {top_name} is not a .subckt defined in {path.name}")
        top = top_name
    elif len(roots) == 1:
        top = roots[0]
    elif not roots:
        raise ValueError("every .subckt is instantiated by another -- no root; pass --top NAME")
    else:
        raise ValueError(f"deck defines {len(roots)} uninstantiated blocks ({', '.join(sorted(roots))}); "
                         f"pass --top NAME to pick the root")

    tree = build_tree(top, blocks)
    return {'netlist': str(path.resolve()), 'top': top, 'blocks': blocks, 'tree': tree,
            'includes': includes, 'other_roots': [r for r in roots if r != top]}


def write_slices(result: dict, split_dir: Path) -> list[Path]:
    """One self-contained `.sp` per block, holding ONLY that block's own
    leaf devices -- what `detect_topology.py` must be run against, one
    block at a time (see this module's docstring, point 2). Hierarchy calls
    are dropped: a child block's devices belong to the child's own slice."""
    split_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, block in result['blocks'].items():
        if not block['leaves']:
            continue
        label = 'TOP_DECK' if name == '__TOP__' else name
        body = ''.join(f"{leaf['line']}\n" for leaf in block['leaves'])
        out = split_dir / f"{label}.sp"
        out.write_text(
            f"* Level slice of {Path(result['netlist']).name} -- block '{label}' own devices only.\n"
            f"* Written by scan_hierarchy.py; run detect_topology.py on THIS file, not on the full deck.\n"
            f".subckt {label} {' '.join(block['pins'])}\n{body}.ends {label}\n"
        )
        written.append(out)
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("netlist")
    parser.add_argument("--top", default=None, help="root block, if the deck has more than one candidate")
    parser.add_argument("--split-dir", default=None, help="write one per-block .sp slice here for detect_topology.py")
    parser.add_argument("--out-json", default=None, help="also write the hierarchy structured (standalone debugging)")
    args = parser.parse_args()

    path = Path(args.netlist).resolve()
    result = analyze(path, args.top)
    blocks = result['blocks']

    print(f"=== Hierarchy: {path.name} ===\n")
    print('\n'.join(ascii_tree(result['tree'], blocks)))

    print(f"\n--- blocks ({len(blocks)}) ---")
    for name, block in sorted(blocks.items()):
        pins = ' '.join(block['pins']) or '(none)'
        print(f"  {name}: pins={pins}, own devices={len(block['leaves'])}, sub-instances={len(block['calls'])}")

    if result['other_roots']:
        print(f"\nDefined but never instantiated (not part of {result['top']}'s hierarchy): "
              f"{', '.join(result['other_roots'])}")
    if result['includes']:
        print("\nIncludes (recorded, NOT followed -- PDK models are leaves):")
        for inc in result['includes']:
            print(f"  {inc}")

    print("\n--- mermaid ---")
    print('\n'.join(mermaid(result['tree'], blocks)))

    if args.split_dir:
        written = write_slices(result, Path(args.split_dir))
        print(f"\nWrote {len(written)} level slice(s) for detect_topology.py:")
        for w in written:
            print(f"  {w}")

    if args.out_json:
        out = Path(args.out_json)
        payload = {
            'netlist': result['netlist'],
            'top': result['top'],
            'tree': result['tree'],
            'includes': result['includes'],
            'other_roots': result['other_roots'],
            'blocks': {n: {'pins': b['pins'], 'calls': b['calls'], 'leaves': b['leaves'],
                           'device_counts': b['device_counts']} for n, b in blocks.items()},
            'ascii_tree': '\n'.join(ascii_tree(result['tree'], blocks)),
            'mermaid': '\n'.join(mermaid(result['tree'], blocks)),
        }
        out.write_text(json.dumps(payload, indent=2))
        print(f"\n  wrote {out}")


if __name__ == "__main__":
    main()
