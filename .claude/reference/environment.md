# EDA Toolchain Environment & Known Quirks

All of this was learned the hard way earlier in this repo's history —
follow it exactly to avoid re-discovering the same failures.

## Paths (macOS, this machine)

> **PDK paths belong to the guideline, not to this file.** The active process
> and its `pdk_root`, `magicrc`, `ngspice_lib`, `netgen_setup` and
> `klayout_lyp` live in `pdk_options.json` alongside this file; print the resolved
> values with `python .claude/reference/pdk_config.py`. The paths written out
> below are the *currently selected* PDK's, kept inline because the quirks
> attached to them are what this file is for. If they disagree with the
> guideline, the guideline wins and this section is stale.

- sky130A PDK: `~/pdk/manual` (i.e. `PDK_ROOT=$HOME/pdk/manual`).
  **Always set `PDK_ROOT=$HOME/pdk/manual` explicitly on every magic/netgen
  invocation** — non-interactive shells don't source `.zshrc`, and
  `sky130A.magicrc` silently falls back to a broken conda placeholder path
  without it.
- Magic: `/usr/local/bin/magic`. Invoke headless:
  `magic -dnull -noconsole -rcfile $HOME/pdk/manual/sky130A/libs.tech/magic/sky130A.magicrc <script.tcl>`
- netgen: `$HOME/.local/bin/netgen`.
- ngspice: on `PATH`.
- sky130 ngspice models:
  `$HOME/pdk/manual/sky130A/libs.tech/ngspice/sky130.lib.spice` (use the
  `tt` corner unless told otherwise).
- netgen LVS setup script:
  `$HOME/pdk/manual/sky130A/libs.tech/netgen/sky130A_setup.tcl`
- KLayout: `/Applications/klayout.app` (CLI: `klayout`). **A bare
  `klayout <file.gds>` shows every layer with generic/arbitrary colors --
  dense multi-finger devices (diff pairs, current mirrors) visually blur
  into what looks like a solid block at a glance, easy to mistake for "no
  real geometry, just a bounding box."** Always open with the real sky130
  layer-properties file so devices actually look like devices:
  `klayout -l "PDK/sky130_pdk/libs.tech/klayout/tech/sky130A.lyp" <file.gds>`
  (path relative to the project root; `-l` loads layer properties on open.
  This `.lyp` comes from the full open_pdks-style
  checkout vendored in this repo under `PDK/`, a *different* path than the
  `PDK_ROOT=~/pdk/manual` used for
  Magic/netgen/ngspice above -- don't conflate the two). Confirmed via
  `klayout.lay.LayoutView` (the same `klayout` Python package used for
  headless PNG snapshots) that this makes a real, visible difference:
  without it, a glayout-generated device's fingers/guard-ring/tie-ring
  structure is present in the GDS but rendered in flat, hard-to-distinguish
  default colors; with it, diffusion/poly/metal layers render in their
  standard sky130 colors and finger structure is immediately visible.

## Magic DRC (layout-fixer)

Batch-mode DRC **requires** `drc on` + `drc catchup` **before** `drc check`
and `drc list count` — without these, Magic silently reports 0 violations
regardless of actual layout state. The validated script pattern:

```tcl
gds read <file.gds>
load <TOPCELL>
select top cell
drc euclidean on
drc style drc(full)
drc on
select top cell
expand
drc check
drc catchup
set n [drc list count total]
puts "DRC_TOTAL: $n"
foreach {rule cnt} [drc listall count] { puts "CELLCOUNT: $rule $cnt" }
set res [drc listall why]
foreach {rule coords} $res { puts "RULE: ([llength $coords]) $rule" }
quit -noprompt
```

The `expand` step matters: skip it and `drc listall count` can report
spurious cell-adjacency entries that are not real rule violations — always
cross-check `DRC_TOTAL` (from `drc list count total`, the authoritative
count) against `drc listall why` (the real per-rule violation listing,
which is empty when truly clean) before trusting a "0 violations" claim.
Coordinates from `drc why`/`drc find` are in Magic internal units
(multiply by 0.005 to get µm).

`CIF file read warning: Input off lambda grid by N/M; snapped to grid`
means Magic moved geometry onto its internal grid before checking it, so
DRC ran on a snapped copy rather than on the GDS exactly as written. It is
routine for glayout output and is not itself a violation, but it does mean
a sub-grid DRC margin cannot be resolved by Magic — if a spacing result
looks impossibly exact, this is why.

GDS-read warnings like `Unknown layer/datatype in boundary, layer=64
type=44` are expected and harmless — this is glayout's internal "pwell"
glayer mapped to a GDS layer/datatype that sky130A.tech doesn't recognize;
Magic correctly drops it, and it appears identically across every glayout
GDS in this repo. Do not treat it as a DRC issue.

## Magic Extraction + netgen LVS (layout-fixer)

```tcl
gds read <file.gds>
load <TOPCELL>
select top cell
port makeall
extract path .
extract all
ext2spice lvs
ext2spice -o <TOPCELL>_extracted.spice
quit -noprompt
```

then:

```
netgen -batch lvs "<TOPCELL>_extracted.spice <TOPCELL>" \
  "<design>.spice <design_subckt_name>" \
  $HOME/pdk/manual/sky130A/libs.tech/netgen/sky130A_setup.tcl \
  <report>.out
```

- **netgen rejects `.sp`/`.cdl` file extensions** ("don't know type of
  file") — always `cp <design>.sp <design>.spice` before comparing.
- **CRITICAL — resistor element-prefix syntax must match.** Magic's
  `ext2spice` always extracts sky130 poly resistor *devices*
  (`sky130_fd_pr__res_generic_po`, `res_high_po_*`, etc.) as SPICE
  R-primitives: `R<name> <n1> <n2> <modelname> <params>` — **never** as an
  `X<name> ...` subcircuit call, regardless of layout hierarchy. netgen's
  `sky130A_setup.tcl` resistor-matching rule
  (`permute "-circuit1 $dev" end_a end_b`) only binds node names correctly
  when **both sides of the comparison use the same element prefix**. If
  the golden netlist instantiates a resistor with `X<name> ...`
  (subcircuit-call syntax) while the layout extracts it as `R<name> ...`
  (primitive syntax), netgen silently reports the device as connected to
  synthetic disconnected `dummy_N` nets on **both** terminals — even
  though the real wiring is correct — producing a persistent, misleading
  "open circuit" LVS failure that looks like a layout bug but isn't. **Any
  resistor device with both a `.model` and `.subckt` card of the same
  name in the golden netlist must be instantiated with `R<name> ...`, not
  `X<name> ...`.** If layout-fixer ever sees a resistor reported as
  disconnected on both terminals despite the raw extracted spice showing
  correct node names, check this first before touching any layout
  geometry — it has cost multiple wasted iterations before.

## PEX (verify-agent)

C-only extraction (no parasitic R — full RC extraction is far slower and
not needed for this project's AC-fidelity signal):

```tcl
gds read <file.gds>
load <TOPCELL>
select top cell
port makeall
extract path .
extract all
ext2spice lvs
ext2spice cthresh 0
ext2spice subcircuit top on
ext2spice -o <TOPCELL>_pex.spice
quit -noprompt
```

`cthresh 0` includes every extracted parasitic cap (no filtering by
minimum size) — needed since we're trying to measure exactly this effect.
Sanity-check every PEX netlist with a quick netgen LVS against the golden
netlist with `ignore class c -circuit1` in the netgen setup (see
layout-fixer's Step 2 flow) — this confirms the parasitic-annotated netlist
still represents the same circuit before trusting its simulation results.

## ngspice AC testbench pattern

Reuse the structure already validated in a reference
AC testbench: a differential AC source split ±0.5 into the
two inputs around a `vcm` common-mode bias, ideal supplies, a fixed output
load `CL`, `.ac dec 10 1 10G`, and `write ./<out>.out v(vout)` inside
`.control` with `set filetype=ascii`. The pre-layout testbench `.include`s
the golden `.sp` directly; the post-layout testbench `.include`s the PEX
netlist instead and instantiates its top subckt (check the exact port
order in the PEX file's `.subckt` line — it is not guaranteed to match the
golden netlist's port order verbatim).
