#!/usr/bin/env python3
"""Run every structural pass and write ONE file: `circuit_decomposition.yaml`.

Before this driver existed the skill left four kinds of generated file
behind -- `hierarchy.json`, a `.sp` slice per block, a `.groups.json` and a
`.topo.json` per slice -- and the real output had to be assembled from all
of them by hand. They are now intermediates: the slices live in a temp
directory that is deleted on exit (`--keep-work` to inspect them), and
every scan result lands in the `scan:` section of the single output file.
One design, one artifact.

The three passes, all in-process:

  1. `scan_hierarchy.analyze()` -- the `.subckt` tree, ASCII + mermaid, and
     one self-contained slice per block.
  2. `group_devices` -- per slice, the shared-net groups and affinity
     cliques that narrow what each block's patterns could be.
  3. `detect_topology.detect()` -- per slice, the exact drain/gate/source
     signature matches.
  4. `match_nets.candidates()` -- per slice, the NETS each matched group
     implies: which are twins that must be routed as mirror images, which
     are one common node the whole group shares. Matched devices with
     unmatched wiring are still mismatched, and nothing upstream of layout
     was recording that.

Passes 2 and 3 run per slice and never on the whole deck: `.subckt`
boundaries are invisible to `detect_topology.parse_devices()`, and net
names are block-local, so a whole-deck scan invents matches across blocks
that do not exist in the circuit.

**Evidence goes to stdout, conclusions go to the file.** The per-block
scan -- device table, shared-net groups, affinity cliques, detector
findings, net-match candidates -- is printed for `../SKILL.md` Step 2 to work from in the same
turn, and is NOT written into `circuit_decomposition.yaml`. That file is a
reference `schematic-agent` and `schematic-sizing` read: which patterns
exist, and which devices share a tunable parameter. Persisting the raw
scan buried those answers under several hundred lines of intermediate
data that no consumer reads. The scan is cheap and deterministic -- re-run
this command to see it again, or `--scan-json PATH` to keep a copy for
auditing.

The file comes out with `hierarchy` filled in, `tie_groups` +
`tunable_parameters` DERIVED by `build_tunables` from `../pattern-table.md`'s
own rules (plus the `.sp.j2` template rendered from them), and `patterns`,
`unmatched_devices`, `matched_nets`, `open_questions` as empty stubs for
Steps 2-4 to author. Key meanings for every
section are in `../SKILL.md` (`../output-schema.yaml` is cited there too but
does not exist -- see that file's "Files and references").

Usage:
  python build_decomposition.py path/to/top.sp --out <design_dir>/circuit_decomposition.yaml
      [--top NAME] [--threshold 0.5] [--keep-work DIR] [--scan-json PATH]

Reads the netlist only; never writes to it (`../../../../CLAUDE.md` Key Rules:
a netlist in the loop is frozen).
"""
import argparse
import datetime
import json
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SCRIPT_DIR.parent.parent.parent / "reference"))
import scan_hierarchy as hier  # noqa: E402
import group_devices as grp  # noqa: E402
import detect_topology as topo  # noqa: E402
import match_nets as nets  # noqa: E402
import build_tunables as tunables  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: python3 -m pip install pyyaml")


def _block_scalar(dumper, data):
    """Multi-line strings (the ASCII tree, the mermaid graph) as `|` block
    scalars. Without this they dump as one quoted line of `\\n` escapes,
    which throws away the readability that made YAML the right format."""
    style = '|' if '\n' in data else None
    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style=style)


yaml.add_representer(str, _block_scalar, Dumper=yaml.SafeDumper)


def tree_depth(node: dict) -> int:
    return 1 + max((tree_depth(c) for c in node['children']), default=0)


def instance_paths(node: dict, prefix: str = '') -> dict[str, list[str]]:
    """block name -> every instance path it appears at. A block
    instantiated twice is two physical copies of every pattern inside it,
    which the pattern step has to say out loud."""
    here = f"{prefix}/{node['instance']}" if node.get('instance') else "(top)"
    paths = {node['subckt']: [here]}
    for child in node['children']:
        for name, ps in instance_paths(child, '' if here == '(top)' else here).items():
            paths.setdefault(name, []).extend(ps)
    return paths


def compact(group: dict) -> dict:
    """One group, with its pairwise evidence flattened to a line per link
    (`XMN4~XMP4 1.333 g-g,d-d @net7`) instead of a nested dict per shared
    terminal. Same information, a fraction of the file: the full nested
    form ran ~70 lines for an 11-device block, which buries the authored
    sections underneath the evidence they are drawn from."""
    out = {k: group[k] for k in ('id', 'devices') if k in group}
    if 'net' in group:
        out['net'] = group['net']
        out['terminals'] = {n: '/'.join(t) for n, t in group['terminals'].items()}
    out['cohesion'] = group['cohesion']
    if group['diode_connected']:
        out['diode_connected'] = group['diode_connected']
    out['links'] = [
        f"{e['devices'][0]}~{e['devices'][1]} {e['affinity']} "
        f"{','.join(ev['signature'] for ev in e['evidence'])} "
        f"@{','.join(sorted({ev['net'] for ev in e['evidence']}))}"
        for e in group['edges']
    ]
    out['candidate_patterns'] = group['candidate_patterns']
    return out


def build(netlist: Path, top_name: str | None, threshold: float, work: Path) -> tuple[dict, dict, dict]:
    result = hier.analyze(netlist, top_name)
    blocks, tree = result['blocks'], result['tree']
    hier.write_slices(result, work)
    paths = instance_paths(tree)

    scan: dict[str, dict] = {}
    parsed: dict[str, tuple] = {}
    for name, block in blocks.items():
        label = 'TOP_DECK' if name == '__TOP__' else name
        slice_path = work / f"{label}.sp"
        if not slice_path.exists():
            continue  # a block with no leaf devices of its own
        devices = grp.parse_slice(slice_path)
        if not devices:
            continue  # e.g. a top deck holding only sources: nothing to pattern-match
        edges = grp.all_edges(devices)
        mos = topo.parse_devices(str(slice_path))
        findings, unclassified = topo.detect(mos) if mos else ([], [])
        scan[label] = {
            'devices': [{k: d[k] for k in ('name', 'kind', 'polarity', 'model',
                                           'terminals', 'params', 'diode_connected')}
                        for d in devices],
            'net_groups': [compact(g) for g in grp.net_groups(devices, edges)],
            'cliques': [compact(g) for g in grp.cliques(devices, edges, threshold)],
            'detector': {'findings': findings, 'unclassified': unclassified},
            'net_matches': nets.candidates(devices, findings),
        }
        parsed[label] = (devices, findings)

    hierarchy = {
        # One diagram, the ASCII tree -- it reads inline for both an agent
        # and a person. The mermaid version is printed instead of stored:
        # two renderings of the same tree in one file is duplication, and
        # the printed one can still be pasted into a report.
        'diagram': '\n'.join(hier.ascii_tree(tree, blocks)),
        'levels': tree_depth(tree),
        'blocks': [{'name': 'TOP_DECK' if n == '__TOP__' else n,
                    'pins': b['pins'],
                    'instance_paths': paths.get(n, []),
                    'own_devices': len(b['leaves']),
                    'sub_instances': [{'instance': c['instance'], 'subckt': c['subckt']}
                                      for c in b['calls']]}
                   for n, b in blocks.items()],
    }
    # Only when they say something: an empty `includes`/`other_roots` on
    # every flat design is noise in a file meant to be read.
    if result['includes']:
        hierarchy['includes'] = result['includes']
    if result['other_roots']:
        hierarchy['other_roots'] = result['other_roots']

    # Tie groups and the tunable-variable registry are DERIVED, not authored:
    # `build_tunables` applies pattern-table.md's own "Tie group" rules to the
    # detector's findings. They used to be stubs an agent filled in by hand,
    # which is how a current mirror once reached sizing as three independent
    # free widths. See that module's docstring.
    # The top block by NAME -- `TOP_DECK` only when the deck has loose devices
    # outside any .subckt; a design whose top IS a .subckt is labelled with it.
    top_label = result['top'] if result['top'] in parsed else 'TOP_DECK'
    top_devices, top_findings = parsed.get(top_label, ([], []))
    tie_groups, tunable_list, binding = tunables.derive(top_devices, top_findings)

    doc = {
        'schema': 'circuit-decomposition/v1',
        'netlist': str(netlist),
        'top': result['top'],
        'generated': datetime.date.today().isoformat(),
        'confirmed_by_user': False,
        'hierarchy': hierarchy,
        # DERIVED by build_tunables.py from pattern-table.md's rules.
        'tie_groups': tie_groups,
        'tunable_parameters': tunable_list,
        # Stubs -- SKILL.md Steps 2-3 fill these in.
        'patterns': [],
        'unmatched_devices': [],
        'matched_nets': [],
        'open_questions': [],
    }
    return doc, {'blocks': scan, 'mermaid': '\n'.join(hier.mermaid(tree, blocks))}, binding


HEADER = """\
# circuit_decomposition.yaml -- the circuit read that schematic-agent and
# schematic-sizing work from. Conclusions only: which patterns the netlist
# contains, and which devices must share a tunable parameter. The scan
# evidence behind them is printed by build_decomposition.py, not stored
# here. Key meanings: .claude/skills/circuit-decomposition/SKILL.md
#
#   `hierarchy` is machine-written -- re-run the script rather than editing.
#   `tie_groups` and `tunable_parameters` are DERIVED by build_tunables.py
#   from pattern-table.md's own "Tie group" rules -- machine-written, like
#   `hierarchy`. Re-run the script rather than editing them, and do not
#   "correct" a mirror into per-leg free widths: one shared unit w/l with a
#   per-leg integer m IS the mirror rule, and `tunable_template` is rendered
#   straight from them.
#
#   `patterns`, `unmatched_devices`, `matched_nets`, `open_questions` are
#   AUTHORED against pattern-table.md, and are empty until SKILL.md's
#   judgment steps have run. Empty means "not done yet", never "none found".
#   `matched_nets` is the layout half of a tie group: the wiring that must
#   match for the matched devices to stay matched.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("netlist")
    parser.add_argument("--out", required=True, help="<design_dir>/circuit_decomposition.yaml")
    parser.add_argument("--top", default=None, help="root block, if the deck has more than one candidate")
    parser.add_argument("--threshold", type=float, default=0.5, help="affinity threshold for cliques")
    parser.add_argument("--keep-work", default=None, help="keep the per-block .sp slices here instead of a temp dir")
    parser.add_argument("--scan-json", default=None,
                        help="also save the printed scan evidence as JSON (auditing; not a deliverable)")
    parser.add_argument("--out-j2", default=None,
                        help="the tunable-parameter netlist template "
                             "(default: <design_dir>/<top>_tunable.sp.j2, beside --out)")
    args = parser.parse_args()

    netlist = Path(args.netlist).resolve()
    out = Path(args.out).resolve()
    if out.exists():
        prior = yaml.safe_load(out.read_text()) or {}
        if prior.get('patterns') or prior.get('matched_nets') or prior.get('open_questions'):
            sys.exit(f"{out} already holds authored patterns/matched_nets/open_questions -- refusing to "
                     f"overwrite the judgment steps' work. Move it aside first if you really mean to "
                     f"re-scan. (`tie_groups` and `tunable_parameters` are DERIVED and are safe to "
                     f"regenerate -- they are not what this guard protects.)")

    work = Path(args.keep_work).resolve() if args.keep_work else Path(tempfile.mkdtemp(prefix="decomp_"))
    try:
        doc, scan, binding = build(netlist, args.top, args.threshold, work)
    finally:
        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)

    out.parent.mkdir(parents=True, exist_ok=True)

    # The template is the OTHER half of this skill's output: the netlist with
    # every tunable replaced by its variable. schematic-sizing renders it
    # instead of writing values into a literal netlist through a side-car map.
    j2_path = Path(args.out_j2).resolve() if args.out_j2 else \
        out.parent / f"{doc['top']}_tunable.sp.j2"
    j2_path.parent.mkdir(parents=True, exist_ok=True)
    j2_path.write_text(tunables.J2_HEADER + tunables.render_template(netlist, binding))
    doc['tunable_template'] = str(j2_path)
    print(f"tunable template -> {j2_path}")
    for g in doc['tie_groups']:
        print(f"  {g['id']:28s} {g['devices']} tied={g['tied']} carrier={g['ratio_carrier']}")

    # default_flow_style=None keeps leaf lists inline (`devices: [XMN3, XMN4]`)
    # while anything holding a nested collection stays block-style. Purely
    # block style ran the file ~2x longer with one device name per line.
    out.write_text(HEADER + yaml.safe_dump(doc, sort_keys=False, default_flow_style=None, width=100))

    # The scan report -- printed, not stored. This is what Step 2 reads.
    print(doc['hierarchy']['diagram'])
    print(f"\n--- mermaid (for a report; not stored in the file) ---\n{scan['mermaid']}")
    for label, block in scan['blocks'].items():
        diodes = [d['name'] for d in block['devices'] if d['diode_connected']]
        print(f"\n=== {label} === {len(block['devices'])} devices"
              + (f" | diode-connected: {', '.join(diodes)}" if diodes else ""))
        for d in block['devices']:
            terms = ' '.join(f"{t}={n}" for t, n in d['terminals'].items())
            print(f"  {d['name']:<8} {d['model']:<28} {terms}  {d['params']}")
        for f in block['detector']['findings']:
            print(f"  [detector: {f['topology']}] {', '.join(f['devices'])} -- {f['detail']}")
        if block['detector']['unclassified']:
            print(f"  [detector: unclassified] {', '.join(block['detector']['unclassified'])}")
        for g in block['net_groups'] + block['cliques']:
            where = f"net {g['net']}" if 'net' in g else f"clique {g['id']}"
            tests = ', '.join(g['candidate_patterns']) or '(no hint)'
            print(f"  [{where}] {', '.join(g['devices'])} (cohesion {g['cohesion']}) -> test {tests}")
            for link in g['links']:
                print(f"      {link}")
        for line in nets.format_lines(block['net_matches']):
            print(line)

    if args.scan_json:
        Path(args.scan_json).write_text(json.dumps(scan, indent=2))
        print(f"\n  wrote {args.scan_json} (scan evidence, for auditing only)")
    print(f"\n  wrote {out}")
    print("  patterns / unmatched_devices / matched_nets are empty stubs -- "
          "SKILL.md Steps 2-4 author them from the scan above.")


if __name__ == "__main__":
    main()
