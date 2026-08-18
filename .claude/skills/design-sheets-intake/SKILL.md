---
name: design-sheets-intake
description: >-
  Front-door intake: interviews the user for the three mandatory files
  (netlist, testbench, target_spec.json) plus a PDK, resolves the netlist's
  full `.include` closure, then lays down `<design_root>/<project_name>/` with
  `user_inputs/` (verbatim backup), `netlist/`, `testbench/`, `spec/`,
  re-pointing every include the move broke (PDK `.lib` left alone). Optionally
  collects a reference layout's three tiers into `layout/` and reports
  shortfalls. Does NOT validate, simulate, size or check -- collects, files.
---

# Design Sheets Intake

Two jobs, in order: **interview** the user until every mandatory input is a real
path, then **build the folder** -- `<design_dir>/{user_inputs,netlist,testbench,
spec}` plus `layout/` (if a layout exists, with `submodules/`+`primitives/`) --
and file them into it. Validation, ERC, simulation and sizing belong to whatever
runs next. **Nothing is guessed.**

**Paths & names.** Shell commands run from the project root. `<design_dir>` =
`<design_root>/<project_name>/`; `<design_root>` is decided at Step 0 -- **you
create it**. `<project_name>` = folder; `<design_name>` = netlist `.sp` basename,
resolved off the netlist filename, never the folder.

## Step 0 — name the project, settle the root

Ask for a short identifier, sanitize to `snake_case` → `<project_name>`. Ask
where design folders go; default **`designs/`** → `<design_root>`. `mkdir -p
<design_root>` if absent; record it.

**If `<design_dir>` already exists, stop.** `ls <design_dir>` first so the user
decides with its contents in front of them, then ask via `AskUserQuestion` --
never silently overwrite, merge into, or delete an existing design folder:

| Answer | Do |
|---|---|
| **Reuse this folder** | keep it and everything in it. Files already inside are *candidates* for Step 2, confirmed one by one like any other -- never adopted because they are sitting there. Step 4 backs up into the existing `user_inputs/`; a name collision there is its own ask. |
| **Create a new one** | take a new `<project_name>` (offer `<name>_v2`/`_v3`, the first free suffix, as the default) and `mkdir` it fresh. The old folder is left untouched. |

Same question if the user's identifier collides with a folder under a
*different* `<design_root>` you were also given -- resolve which one is meant
before creating anything.

## Step 1 — is there a reference layout?

Ask **now** (it changes the tracker), via `AskUserQuestion`: "Yes — I have a
GDS" (create `layout/` + tiers, report shortfalls) vs "No — build from scratch"
(no `layout/`). Branch question only; paths are collected at Step 6.

## Step 2 — collect the three mandatory files + PDK

None has a fallback, and **no path is accepted without the user confirming it in
words.** Anything you scanned, globbed, or inferred is a *candidate* until then,
however obvious the match looks.

### 2.0 — how the paths arrive

Two modes. Pick the one the user's opening message already implies; if it
implies neither, ask which they want before collecting anything.

**Mode A -- one folder.** The user names a directory ("my design is in
`~/work/ota`"), or Step 0's reused `<design_dir>` already holds files:

1. **Scan it** -- e.g. `find <dir> -maxdepth 2 -type f \( -name '*.sp' -o -name
   '*.spice' -o -name '*.cir' -o -name '*.json' -o -name '*.gds' \)`. A folder
   the user mentions in passing still gets scanned -- detect it, don't wait to
   be told to look.
2. **Classify each hit** into a role: netlist / testbench / spec / layout /
   sub-netlist / unknown. Heuristics only, and they are evidence to *show*, not
   a decision: an analysis or `.control` block (`.ac`/`.tran`/`.dc`) or a
   `_tb`/`_pre`/`_test` name → testbench; `.subckt` with no analysis → netlist;
   a JSON with spec-shaped keys → spec; a file some other `.sp` `.include`s →
   sub-netlist.
3. **Confirm one file at a time, in the table's order below** -- one
   `AskUserQuestion` per role, showing that role's candidate(s) with the
   evidence that classified them, plus a standing "none of these -- I'll give a
   path" option. **Never batch the whole set into one question**, and never take
   a role's file just because it was the only match.
4. **Nothing is dropped silently**: extra same-role matches (two top-level
   `.sp`s) and `unknown` files are named in that role's question or listed after
   the scan. Sub-netlists reached via `.include` are 2a's job, not a question
   each.

**Mode B -- a path per file.** No folder, or the scan came up short on a role:
ask for one path per unresolved row, **one question at a time**, in table order.
`ls` each answer; a path that doesn't exist is re-asked, never assumed.

**If the user gave neither a folder nor any paths**, ask exactly that choice --
point at one folder holding all the inputs, or give a path for each file -- and
say which files are still needed. Do not guess a directory, and do not go
hunting through the project root for something that looks like a netlist.

A scan is typing relief only: **it resolves nothing.** Every `[x]` in Step 3's
tracker means *the user confirmed that path*, and 2a's include closure runs on
whatever was confirmed, in both modes.

| # | Input | If missing |
|---|---|---|
| 1 | Top-level netlist `.sp` | ask |
| 2 | Testbench `.spice`/`.sp` | ask — stop intake (2b) |
| 3 | `target_spec.json` | ask — stop intake (2c) |
| 4 | PDK | read from `.claude/reference/pdk_options.json` (`"selected"`) |

### 2a — the netlist and its include closure

Resolve the **full** closure, not just the named file: read the `.sp`, collect
every `.include`/`.lib`, resolve each relative to the **including file's own
directory**, recurse with a visited set (a cycle is a user bug -- report it).
**Separate PDK from design includes**: a `.lib` into `$PDK_ROOT` is the process
-- record it for 2d, do not copy. **Report any unresolved include and ask**; do
not proceed with a hole. Per include line record **three things** (5b needs all):
which file it is in, its raw text, and its class (design/PDK/unresolved).

### 2b — the testbench

A user input on the same footing as the netlist: it encodes the bias point, the
load, and the analysis -- none recoverable from the netlist. **No testbench
stops intake**: mark the row `[~]`, say what you're waiting for.

- **Netlist and testbench must be separate files**; a netlist carrying its own
  analysis/`.control` must be split (ask, don't silently accept).
- **Confirm it belongs to this netlist** (filename or a `.subckt` from 2a, and
  DUT port count matches). Run 2a's include resolution over the testbench too.

### 2c — target_spec.json

Normative form is `../design-sheets-checker/spec_form_template.md`: one object
per key with `Direction` (`FLOOR`/`CEILING`/`RANGE`), `Value`, `Units` (non-empty,
`"-"` if dimensionless). The flat form (`{"Gain": 40}`) does **not** conform --
ask for each key's `Direction`/`Units` rather than converting. **Units are not
guessable**: `Gain` dB, `UGBW` **MHz**, `PM` degree, `Power` **mW**; wrong units
→ wrong verdicts, not errors. Those four are an example, not the permitted set.
**A missing spec stops intake** -- mark `[~]` and ask; this skill authors none.
Validate only that it parses as JSON.

### 2d — the PDK

**The PDK is not free text -- it is chosen from the project guideline.** Run
`python .claude/reference/pdk_config.py` to print the active PDK, and read
`.claude/reference/pdk_options.json` for the full list of selectable ones. The
design uses whatever `"selected"` names; offer the other entries only as a
switch the user confirms, and record that choice by editing `"selected"`
there rather than noting a name in the design folder. A PDK not in that file
is not selectable -- add an entry first.

Confirm its `pdk_root` resolves (`ls <pdk_root>/<pdk>`); absent → ask for the
path and fix it in the guideline. **Cross-check the netlist's model names**
against the selected PDK's `model_prefix`: models from a different process
are a contradiction -- ask. Nothing about the PDK is copied into the design.

## Step 3 — progress tracker (print after every resolved item)

```
INPUTS SETUP -- bg_ref_a                              [3/6 resolved]
  [x] project name    bg_ref_a
  [x] netlist         ~/designs/bg_ref_a.sp  (+2 sub-netlists)
  [~] testbench       waiting -- no .spice yet
  [x] PDK             sky130A  (~/pdk/manual/sky130A)
  [ ] target spec     not asked yet
  [-] reference layout  none -> build from scratch
```

Markers: `[x]` resolved (always with the path); `[ ]` pending; `[~]` waiting on
the user (only after you actually asked); `[-]` skipped (only the layout row may
be `[-]`). Count = `[x]` rows only; a short set is `[x]` + 6c's counts.

## Step 4 — create the folder and back everything up

Once every mandatory row is `[x]`:
`mkdir -p <design_dir>/{user_inputs,netlist,testbench,spec}` (no `layout/`; Step
6 owns it).

**Back up to `user_inputs/` before anything moves** -- the netlist closure, the
testbench + its includes, and `target_spec.json`; it is the verbatim record of
"what did the user hand over", and **nothing writes to it after this step.**
Files **outside** `<design_dir>` are **copied**; files already **inside** are
**moved** (verify byte-for-byte with `cmp` before unlinking). Flatten the closure
unless two files collide (then use a subfolder, carry into Step 5).

## Step 5 — file each input, then fix what the move broke

Copy **from `user_inputs/`** into: netlist closure → `netlist/`; testbench (+ its
non-netlist includes) → `testbench/`; spec → `spec/`. Every closure file goes
into `netlist/`, not just the top level; count against 2a.

### 5b — re-point every include, at every level

Walk 2a's recorded include lines across the top-level netlist, each sub-netlist,
and the testbench. Four shapes:

| Include line | After move | Do |
|---|---|---|
| `.include "<sub>.sp"` (bare sibling) | resolves if flat | leave (rewrite if a collision moved it) |
| `.include "../cells/<sub>.sp"` (relative+dirs) | broken | rewrite to `"<sub>.sp"` |
| `.include "/abs/.../<sub>.sp"` (absolute) | resolves to the *original*, not your copy | rewrite to `"<sub>.sp"` |
| `.lib "$PDK_ROOT/.../<pdk>.lib.spice" tt` (PDK) | resolves correctly | **leave it** |

Every rewrite is **declared**: a header comment naming what changed + a
`MODIFIED` line in the hand-off. **Put that comment AFTER line 1** -- SPICE
treats line 1 of a deck as the title and never parses it, so prepending shifts a
real statement into the title. Included sub-netlists have no title line, so a
top-of-file header is safe there; if line 1 isn't a comment, say so.

### 5c — verify the closure resolves from its new home

Re-run 2a's resolution from `<design_dir>/netlist/<design_name>.sp` (and from
the testbench). Confirm: every include resolves; every resolved design file is
inside `netlist/`; the file set matches 2a exactly; PDK lines still resolve
unmodified. Report a closure tree. **A failure is a stop** -- fix and re-verify.

## Step 6 — the layout branch

**No reference layout** → do nothing, confirm the `[-]` row.

**Reference layout** → three tiers in subfolders (only the top level loose in
`layout/`; `design-sheets-checker` globs `layout/*.gds` and reports "ambiguous"
on more than one):

| Tier | Goes to |
|---|---|
| top level | `layout/<top>.gds` |
| sub-modules | `layout/submodules/` (per sub-`.subckt`) |
| primitive cells | `layout/primitives/` (one GDS per device, e.g. `M0.gds`) |

**6a** ask what the set contains (full hierarchy / primitives only / top GDS
only / generator script) -- its own question, not inferred from a folder.

**6b** create only the tiers 6a named; ask one path per tier, copy in. Any
`.gds` 2.0's scan turned up is a **candidate** here -- offer it against the tier
it looks like and get the same one-at-a-time confirmation; never file a GDS just
because the scan found it. Confirm
each is really GDSII (`xxd -l 16 <f>` → `00 06 00 02` HEADER); report non-GDS,
don't file/count it. Back up to `user_inputs/layout/`; if declined, say so.

**6c** report every tier against 2a's closure, naming absent items. A missing
top-level GDS after "yes" is the one stop-and-ask case. Report, not a gate --
LVS is `design-sheets-checker` Step 7b. File what exists; a partial set is `[x]`.

## Step 7 — hand off

Print the final tracker with every row settled (no `[ ]`/`[~]`), then the
hand-off block -- downstream works from **these** paths, not what was typed:

```
INPUTS SET UP -- test_miller
  project name  : test_miller          (<project_name>)
  netlist name  : two_stage_rz         (<design_name>)
  PDK           : sky130A  /Users/.../pdk/manual/sky130A
  netlist       : designs/test_miller/netlist/two_stage_rz.sp  (1 subckt, 9 MOS+1R+1C)
  testbench     : .../testbench/two_stage_rz_pre.spice  MODIFIED -- .include -> ../netlist/
  target spec   : .../spec/target_spec.json  (Gain/UGBW/PM/Power)
  closure       : 1 of 1 file, 0 unresolved, verified  |  backup: user_inputs/ (3 files)
```

**`MODIFIED` lines are the most important and easiest to drop** -- name every
file edited and what each line became (`unmodified` if nothing changed). The
`closure` line is 5c's receipt.

## What this skill does NOT do

Validate (no ERC/rescale/port-audit beyond 2b); simulate or judge viability;
author/correct a testbench or spec (a missing one stops, not a prompt to invent);
read the circuit; size; DRC/LVS a GDS; check the toolchain launches.
