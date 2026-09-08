---
name: route-optimizer
description:
  Shorten and straighten the routing of an already-routed layout. Takes a DRC-clean `.gds`
  plus its `physical_map.json`, freezes every wire rectangle that lands on a
  device pin, throws the connective wiring away and rebuilds it as a
  minimum-length rectilinear tree over those landings, with A* on a
  coordinate-compressed grid whose cost is lexicographic -- shortest first,
  then fewest turns among the shortest. U-turns, staircases and dangling branches are not
  pattern-matched and deleted -- they vanish because the rebuilt tree has no
  reason to contain them. Bulk connections are additionally freed to land
  anywhere on the tie ring they already sit on. Re-runs Magic DRC and reverts
  any net a new violation lands on, so the output is DRC-clean or the input
  stands. Use between `../router/SKILL.md` (or a `layout-fixer` result) and
  `../../agents/verify-agent.md`, on any layout whose routes wander.
---
# route-optimizer: rip up the detours, keep the connections

Two files in, a shorter-wired GDS out. No netlist is read, no device is moved.
Process facts (layer stack, min separation, via layers) come from the active
PDK; geometry conventions (wire width, via footprint) are inherited from **the
input layout**, so new metal is drawn the way metal that already passed DRC was.

| Input (both required) | Carries |
|---|---|
| a routed `.gds`, **DRC-clean** | the drawn metal: macro placements, wire polygons, `via_stack` cells |
| its `physical_map.json` | which polygon belongs to which net (`nets[].segments` / `.vias`), and where each device sits |

Run it after routing is legal and **before PEX** -- shorter wire is less
parasitic R and C on the nets that set the spec. Not on a layout that has not
passed DRC: the method rests on inheriting geometry known to be good, and on
telling a new violation from an old one. The input is never modified.

## Run it
```
python script/shorten_routes.py <layout.gds> [--map <physical_map.json>]
    [--out-dir <dir>] [--analyze-only] [--nets a,b,c]
    [--passes 2] [--via-weight 2.0] [--bend-penalty 0.2] [--min-gain 0.1]
    [--margin 8.0] [--max-coords 180] [--extra-clearance 0.0]
    [--no-bulk-relax] [--no-prune] [--no-reroute] [--no-drc]
    [--drc-retries 2] [--drc-attrib-margin 0.5] [--skip-baseline-drc]
    [--keep-work]
```
Values shown are the real defaults; `--map` and `--out-dir` default beside the
GDS. **Start with `--analyze-only`** -- it writes nothing and prints the
per-net table saying whether there is anything to win:

- **`bound`** -- terminal-bbox half-perimeter. A *true* lower bound: no legal
  rectilinear route beats it, whatever the obstacles.
- **`mst`** -- Manhattan MST over the same terminals; the optimal rectilinear
  Steiner tree sits between the two, so this is roughly what a good route costs.
- **`excess = wirelen - bound`** -- provable detour, in um. The ranking key,
  and an amount not a ratio, because the objective is *total* wirelength: 1.1x
  on a long supply rail beats 1.5x on a short stub.

A net near 1.0x has nothing to give. One well above it may also have nothing to
give -- the detour can be the only way past a device -- which is why the
optimiser measures rather than assumes.

## Frozen landings: the whole design

A pin landing -- a wire rectangle overlapping a macro's own drawn metal on the
same layer -- is the one piece of routing LVS depends on. In a clean layout
that overlap is never an accident; it *is* the connection. So anything touching
device metal is **frozen** (never moved, never re-emitted), frozen pieces that
touch group into **terminal clusters**, everything else is **free** and thrown
away, and the rebuild connects the clusters and only the clusters. Connectivity
at every pin therefore survives *by construction*, whatever the rebuild does.
Nets with fewer than two clusters are left alone.

## Bulk landings: a tie ring is one node

`../../../src/cells/primitives/fet.py` gives bulk one port, on the tie ring's
south bar, and it stays fixed -- routing needs one deterministic point per
terminal. But the ring is a **single node wrapped around the device**, so a
device on the far side of it pays twice: out to that bar, and back.

So this pass releases it. Where the netlist says a net has a `bulk` terminal on
a macro, it asks fet.py's `find_bulk_tapring()` for the ring behind the
landing, drops the fixed landing, and lets the rebuild land wherever on the
ring is nearest -- typically a via straight down onto the bar facing the net.

**Correctness comes from the trace, not from recognising a ring.** That
function traces the metal continuous with the landing already in use --
same-layer overlap, layer changes only through a real via cut of that pair,
from the PDK's own `via_links`. Everything it returns is the same node by
construction, so extraction cannot tell the difference.

**The ring shape is the discriminator.** A supply net lands on a device's
source rail as well as its tie ring and both trace to a real node, but only a
ring has metal on four sides of its bbox and a hole in the middle. Releasing
every landing that traced to *something* was tried and measured: it roughly
doubled the worst net's rebuild and cost another its rebuild outright, the net
then having to reach several sprawling regions in an order the greedy tree does
not have. Rings only. Three further restrictions, each doing real work:

- **A cluster is released only if it lands on nothing but that ring** -- one
  rectangle can touch two devices, and releasing it for one would cut the other.
- **Ring metal on a non-routable layer does not count**: a ring whose bars are
  all local interconnect gives the router nothing to land on.
- **Released ring metal is `virtual`** -- it counts for connectivity, landing
  and merging, never for wirelength, the map or the GDS. It is already drawn.

`--no-bulk-relax` turns the pass off.

## The rebuild

**Prune first**: free items of degree <= 1 dropped, repeatedly. Pure removal,
so it cannot introduce a violation, and it is the one improvement still open to
a net whose rebuild is rejected.

**Then rip up and re-route**, worst excess first, for `--passes` passes -- each
pass matters because every accepted net frees space for the next. Per net:
start from one cluster, repeatedly A* from the tree so far to the nearest
cluster not yet attached.

The search runs on a **coordinate-compressed (Hanan) graph**, not a fixed
pitch: candidate lines are every obstacle edge offset outward by that layer's
`clearance + half wire width`, plus every own-metal edge and centre line, inside
the connection's bbox grown by `--margin`. A shortest rectilinear
obstacle-avoiding path always exists on such a set, so discretising to it loses
nothing, and it adapts to the layout instead of imposing a grid.

### Shortest first, then straightest

The search cost is a **pair compared lexicographically, `(um, bends)`**.
Length decides; bends only ever break ties. With the heuristic `(manhattan,
0)`, ordinary A* on vector costs returns the fewest-bend path among the
shortest -- so on a Hanan grid, where every monotone staircase between two
points is exactly as long as the L through either corner, the straight one
wins for free. The search state carries the axis of travel, which is what
makes a turn countable at all.

Acceptance is lexicographic too, in the order the objective is stated:
**wirelength, then vias, then turns.** A rebuild that is exactly as long but
goes round fewer corners is *kept* -- it used to be discarded as "not
shorter", throwing away straightness the search had already found. Length may
never increase to buy it.

On top of that, `--bend-penalty` prices a bend in real wire (default **0.2
um**), which steers the search through NEAR-ties as well as exact ones. This
is not redundant with the tie-break and it is not a cosmetic knob: a straighter
net occupies fewer tracks and leaves cleaner channels for the nets routed after
it, so on every design tried a non-zero penalty improved **total wirelength as
well as turn count**. Sweeping it is worthwhile -- the response is not
monotonic, and too large a value makes the search buy vias to dodge corners,
since a via is then cheaper than a turn. `--bend-penalty 0` gives the strict
reading, where a turn can never cost a nanometre.

`--via-weight` (default 2.0 um) prices a via the same way; unpriced, the search
buys layer changes for free.

Obstacles are **per layer**, rasterised from the input GDS: a layer a macro
fills is blocked, one it leaves empty is open, so a wire may pass above or
below a device on a layer that device does not use.

**Both the nodes and the moves between them are blocked.** Node blocking alone
is sound only if every obstacle edge reached the coordinate set, and
`--max-coords` can thin it -- after which two legal nodes sit either side of an
obstacle and the search steps across. Found the hard way: with node blocking
only, the largest net's rebuild was rejected by the final geometric check every
single time.

## The four gates

1. **Exact legality** -- every new rectangle re-checked against every obstacle
   at the PDK's min separation, rectangle against rectangle, with no reference
   to the grid. Surviving this is legality whether or not the discretisation
   was complete.
2. **No same-net notch** -- new metal either merges with metal it belongs to or
   clears it by the full min separation, never lands between. The trap:
   same-net metal is not an obstacle, so a rebuilt wire may graze its own
   landing by a few nanometres, and that gap is a real spacing violation no
   cross-net check looks for. Wires coming up short are **grown until they
   touch**; vias, which cannot be grown, are barred from those positions.
3. **Spans every landing** -- all terminal clusters in one connected component,
   a released bulk ring being one such cluster (so the net must still reach it,
   just not at a fixed point on it).
4. **Shorter, or straighter at the same length** -- lexicographic on
   (wirelength, vias, turns): a clear length win of at least `--min-gain`, or
   an unchanged length with fewer vias or fewer turns. Length never increases.

Then **Magic DRC** via `../router/script/run_drc.py`. A baseline DRC on the
*input* first, so inherited violations are not blamed on the rebuild; any
genuinely new violation box is attributed to rebuilt nets whose new metal lies
within `--drc-attrib-margin`, **those nets revert**, and the layout is
rewritten and re-checked up to `--drc-retries` times. If everything reverts the
output is deleted and the input stands. **The output is DRC-clean, or there is
no output.**

**LVS is not run here.** Frozen landings make pin connectivity safe by
construction and gate 3 proves each net still spans its landings, but the
authority is `../../agents/layout-fixer.md`.

## What it writes

- **`<stem>_shortened.gds`** -- the input with only changed nets' metal
  rewritten. Surviving rectangles keep their identity and are never re-emitted:
  map and GDS coordinates round a nanometre or two apart, and perturbing clean
  geometry for no reason is how a "safe" rewrite breaks DRC. Unreferenced cells
  are dropped. Net labels move onto the net's new metal if the wire they sat on
  is gone -- a label off its own metal stops LVS promoting the port.
- **`physical_map.json`** -- changed nets rewritten, the rest verbatim, plus a
  `route_shortening` block recording what moved.
- **`shortening_summary.txt`** (every run, success or not) and
  **`shortening_report.json`** (same numbers, machine-readable).
- **`drc_violations.json`** -- final DRC violations in um; empty means clean.
- **`drc.log`** -- Magic's raw log, and **only if DRC did not come out clean**.

### Rule: this skill leaves files, not a directory tree

**At most seven files. No subdirectories.** Magic and netgen are messy -- a
`.tcl` and a log per invocation, a `.ext` per cell, a copy of every GDS they
touch -- and none of it is the answer. It runs in one scratch directory
**deleted as soon as its verdict is recorded**, keeping only the structured
result. Enforcing this cut a run from over a hundred files in five directories
to a handful, nearly all the remaining bytes being the GDS.

This binds anything added later: a new tool invocation gets a subdirectory
under the scratch root, never under `--out-dir`, and what it produces reaches
the output as one named file or a section of an existing report. `--keep-work`
and `verify_shortening.py --work-dir <dir>` opt back in, for when a tool run is
itself what you are debugging.

## Verification discipline

The optimiser reports on its own work, so do not take its word for it:
```
python script/verify_shortening.py <original.gds> <shortened.gds>
    [--map-before ...] [--map-after ...] [--out <verification.txt>]
    [--work-dir <dir>] [--no-lvs]
```
It re-reads both layouts and both maps from disk and checks four things the
optimiser cannot honestly check about itself: (1) the output map and GDS
describe the same metal **in both directions** -- a stale polygon left by an
incomplete edit is a short nothing else looks for; (2) every net is one
connected piece and every pin connection the original made is still made; (3)
no two nets overlap on one layer; (4) **Magic extracts both layouts and netgen
compares them to each other** -- "Circuits match uniquely" is the real proof
that only geometry changed, and layout-vs-layout makes it a check this script
can carry out alone.

Checks (1)-(3) are **node-aware**, and had to become so once bulk landings
could move: two pieces of wire landing on one tie ring are connected through
macro-internal metal no top-level rectangle test can see, and a landing is a
connection to a *node*, not a claim on one rectangle. So it partitions each
macro's metal into electrical nodes (the same fet.py machinery) and asks its
questions against that. Before it did, it called a correct layout broken --
nets "split into more than one piece" on a layout netgen matched uniquely.

Writes **one file**, `verification.txt`, beside the shortened GDS.

## Honest scope

- **Multi-terminal nets grow a tree terminal by terminal**, each A*'d to the
  nearest point of the tree so far. Not a Steiner solver: a good rectilinear
  tree, not a provably optimal one. `bound` shows what is left on the table.
- **A layer pair with no `via_stack` template in the input is a transition this
  skill will not use.** Via geometry is a *reference* to a cell the input
  already contains -- which is why no via rule is looked up and the footprint
  matches one that passed DRC -- but it cannot draw a via it has never seen.
- **Wire width is measured off the input's own wires** per layer, floored at
  the PDK `min_width`. A layout drawn wider than minimum keeps it.
- **Macro obstacles are per-polygon bounding boxes** -- over-approximate for
  rotated or non-rectangular shapes; conservative in the safe direction.
- **Bulk relaxation is bulk only, rings only.** The mechanism generalises to
  any terminal whose node is bigger than its port, and deliberately is not:
  measurement says the unrestricted version is worse.
- **Turns are minimised per connection, not globally.** A* returns the
  straightest of the shortest paths for each connection; the tree is still
  grown connection by connection, so a later attachment can add a corner an
  all-at-once solver would not.
- **Rip-up is one net at a time**, everything else frozen. No negotiated
  congestion; two nets that could only improve together will not.
- **A released net can finish under its own `bound`** -- that bound is measured
  against the landings it started with, and it no longer has to reach them.
- **DRC is the authority and is checked. LVS is not run here.**

Re-measure on your own design; any figure quoted anywhere describes one layout
on one process at one moment.

## Files
`SKILL.md` · `script/shorten_routes.py` (analysis, bulk relaxation,
rip-up/re-route, rewrite, DRC revert loop) · `script/verify_shortening.py` ·
`script/special-tracing-pattern.md` -- the registry of routing shapes worth
more than the shortest wire (a matched pair's twin nets carried as one
river-routed bundle, and so on; circuit- and process-agnostic, like the rest of
this skill), naming the flags, inputs and gate changes its first entry would add
here · `script/river_route.py` -- that entry's geometry kernel, with the
offset/reflection laws under `--self-test`. **The routing passes above
implement neither yet.**
External: `../../../src/cells/primitives/fet.py` (`find_bulk_tapring()` and the
metal-tracing helpers under it -- what a bulk tie ring *is*, kept next to the
code that draws one) · `../../reference/pdk_config.py` + `pdk_options.json`
(the active process -- the only place a process name, layer number, spacing
rule or via layer is written down) · `../../reference/environment.md` (the
Magic extraction and netgen invocation patterns, reused verbatim) ·
`../router/script/run_drc.py` (the DRC gate, reused not re-derived) ·
`../router/SKILL.md` (upstream) · `../../agents/layout-fixer.md` (owns the LVS
gate this hands back to).
