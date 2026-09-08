#!/usr/bin/env python3
"""Run the placer's Step 3 annealer with a PORT-AWARE wire term.

Why this exists (measured on this design, not a preference)
-----------------------------------------------------------
`anneal_placement.py`'s `hpwl_by_net()` measures wire between macro BBOX
CENTRES, and `build_net_to_macros()` drops the supply rails.  Both are
deliberate coarse choices that are fine when macros are small compared to
the distances between them.  This design breaks that assumption twice:

  * each `vco_spiral_ind` coil is 196 x 213 um, and BOTH its terminals sit
    within ~12um of one corner region of that box -- terminal A at local
    (0, +100.11) and terminal B at (+94, +87.89) relative to the bbox
    centre.  A coil's centre is therefore ~100um away from the metal the
    router actually lands on, and rotating the coil moves its real
    terminals from the top edge to the left edge while moving its centre
    not at all.  Centre-HPWL cannot see that at all.
  * `VDD` carried 36.9% of the previous run's ROUTED wirelength.  It is
    excluded from centre-HPWL entirely, so the anneal optimises a number
    that ignores the single largest routed net.

Consequence, measured: an 8-seed sweep at the previous run's own weights
produced placements with HPWL 391-453um (BETTER than the previous run's
602um) whose real routed wirelength was 1205-1658um (far WORSE than its
675um).  The cost function and the objective are anti-correlated here.

What is changed, and what is NOT
--------------------------------
Only the wire TERM of the cost function is replaced, by monkeypatching two
functions on the imported module:

  * `build_net_to_macros` -> the router's own net set: pins
    drain/gate/source/BULK, supply rails INCLUDED, nets touching >= 2
    placed macros.  Same rule as `route_nets.py::build_nets(...,
    include_supply=True)`, so the anneal optimises the nets the router
    actually routes.
  * `hpwl_by_net` -> per net, each macro contributes the MEAN of its real
    `ports[net]` landing points transformed to world coordinates by the
    module's own `local_to_world()` (bit-for-bit the transform the router
    lands wire on), falling back to the macro centre only for a macro with
    no port on that net -- which is exactly the case the router itself
    falls back to `pin_point()` for.

Everything else is the stock script: the same moves, the same annealing
schedule, the same overlap / clearance / area / canvas-aspect terms, the
SAME GATES, the same `placement_pos.json`, the same `placing_summary.txt`,
the same `--max-aspect` resolution from `design_constraints.json`.  No file
inside `.claude/` is modified; the patch lives in this process only.

Because the summary's "total HPWL" and "worst nets by HPWL" lines are
produced by the patched function, they report the PORT-AWARE number.  That
is a different quantity from the stock script's, so this script also prints
the stock centre-HPWL of the final placement, for comparison against runs
made with the unpatched script.

Usage: identical to anneal_placement.py -- every argument is forwarded.
    python port_aware_place.py primitives/manifest.json --iters N --seed S ...
"""
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / ".claude" / "skills" / "placer" / "script"))
sys.path.insert(0, str(REPO / ".claude" / "reference"))

import anneal_placement as ap  # noqa: E402

MANIFEST_PATH = Path(sys.argv[1]).resolve()
MANIFEST = json.loads(MANIFEST_PATH.read_text())

# macro -> {net: [(lx, ly), ...]} straight out of the manifest, local coords
# relative to each macro's own bbox centre (generate_primitives.py's
# near_edge_ports() convention, the same one ports_to_world() consumes).
LOCAL_PORTS = {}
for _m in MANIFEST["macros"]:
    LOCAL_PORTS[_m["name"]] = {
        net: [(p["x"], p["y"]) for p in pts]
        for net, pts in (_m.get("ports") or {}).items()
    }

_stock_net_to_macros = ap.build_net_to_macros
_stock_hpwl_by_net = ap.hpwl_by_net


def build_net_to_macros_router_rule(manifest):
    """The router's net set: bulk included, supply rails INCLUDED."""
    net_to_macros = {}
    for dev in manifest["device_index"].values():
        macro = dev.get("macro")
        if macro is None:
            continue
        for pin in ("drain", "gate", "source", "bulk"):
            net = dev.get(pin)
            if not net:
                continue
            net_to_macros.setdefault(net, set()).add(macro)
    return {n: m for n, m in net_to_macros.items() if len(m) >= 2}


def _macro_point(name, net, pos, macro_by_name):
    mx, my, mrot = pos[name]
    ew, eh = ap.effective_wh(macro_by_name[name], mrot)
    cx, cy = mx + ew / 2.0, my + eh / 2.0
    pts = LOCAL_PORTS.get(name, {}).get(net)
    if not pts:
        return cx, cy                      # no port on this net -> centre
    xs, ys = [], []
    for lx, ly in pts:
        wx, wy = ap.local_to_world(lx, ly, cx, cy, mrot)
        xs.append(wx)
        ys.append(wy)
    return sum(xs) / len(xs), sum(ys) / len(ys)


def hpwl_by_net_port_aware(macro_by_name, net_to_macros, pos):
    per_net = {}
    for net, macs in net_to_macros.items():
        xs, ys = [], []
        for name in macs:
            if name not in pos:
                continue
            px, py = _macro_point(name, net, pos, macro_by_name)
            xs.append(px)
            ys.append(py)
        if len(xs) >= 2:
            per_net[net] = ((max(xs) - min(xs)) + (max(ys) - min(ys)), len(xs))
    return per_net


ap.build_net_to_macros = build_net_to_macros_router_rule
ap.hpwl_by_net = hpwl_by_net_port_aware

if __name__ == "__main__":
    print("port_aware_place: wire term = port-aware HPWL over the ROUTER's "
          "net set (bulk pins + supply rails included). Gates unchanged.")
    ap.main()
    # Stock centre-HPWL of whatever placement was just written, for
    # comparability with runs of the unpatched script.
    out = None
    argv = sys.argv
    if "--out" in argv:
        out = Path(argv[argv.index("--out") + 1])
    else:
        out = MANIFEST_PATH.parent.parent / "placement_pos.json"
    try:
        placed = json.loads(Path(out).read_text())
        macros = ap.load_macros(MANIFEST)
        macro_by_name = {m["name"]: m for m in macros}
        pos = {n: (v["x"], v["y"], v["rotated"])
               for n, v in placed["positions"].items()}
        stock_nets = _stock_net_to_macros(MANIFEST)
        centre = sum(v[0] for v in _stock_hpwl_by_net(
            macro_by_name, stock_nets, pos).values())
        print(f"  stock centre-HPWL (unpatched metric, for comparison only): "
              f"{centre:.2f} um")
    except Exception as exc:                                  # pragma: no cover
        print(f"  (could not recompute stock centre-HPWL: {exc})")
