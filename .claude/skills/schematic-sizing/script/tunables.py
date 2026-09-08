#!/usr/bin/env python3
"""The tunable-variable registry and netlist template, loaded from the circuit
read. The replacement for `structure_groups.json`.

WHY THIS REPLACED structure_groups.json. That file was a SECOND answer to a
question `circuit_decomposition.yaml` already answers -- which devices share a
tunable parameter -- written by a different script, from a different detector,
at a different time. Two answers drift, and they did: on `test_miller_ota` the
circuit read said the nfet mirror was one group (`tied: [w, l]`,
`ratio_carrier: m`) while `structure_groups.json` gave `XMN3`/`XMN4`/`XMN5`
six independent variables. Sizing believed the JSON and tuned three different
unit devices onto one gate net, which is not a mirror and cannot be laid out
as one.

Now there is one source. `circuit-decomposition` derives `tie_groups` and
`tunable_parameters` from `pattern-table.md`'s own rules and renders
`<top>_tunable.sp.j2` -- the netlist with each tunable as `{{ VAR }}`. Devices
that must share a parameter **share the variable in the template**, so a group
cannot be half-written: there is no per-device value to forget.

WHAT IS KEPT from the old design: **the netlist is still the state.** There is
no value store beside it. `read_values()` reports what the rendered `.sp`
currently says; `write_values()` merges a change into that and re-renders. A
value that is in the netlist is the value that simulates, always.

  registry:  {var: {"param": "w", "members": [...], "seed": 47.2,
                    "kind": "continuous"|"integer",
                    "tunable_by": "sizing"|"fold"}}
  template:  <design_dir>/<top>_tunable.sp.j2

`kind` matters: `integer` variables (`m`, a ratio carrier) are rounded and
floored at 1 on the way out; `continuous` ones (`w`, `l`) are not.

Usage (standalone; the loop calls these as functions):
  python tunables.py <design_dir>                       # print the registry
  python tunables.py <design_dir> --netlist tuning.sp   # print current values
"""
import argparse
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from netlist_devices import parse_devices  # noqa: E402

sys.path.insert(0, os.path.join(_HERE, "..", "..", "parasitic-estimation", "script"))
from estimate_parasitics import _um_val  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: python3 -m pip install pyyaml")
try:
    import jinja2
except ImportError:  # pragma: no cover
    sys.exit("Jinja2 is required: python3 -m pip install jinja2")

DECOMP_NAME = "circuit_decomposition.yaml"


class TunablesError(Exception):
    pass


def find_decomposition(where) -> Path:
    """Accept a design dir, the yaml itself, or a dir containing it."""
    p = Path(where).resolve()
    if p.is_file():
        return p
    cand = p / DECOMP_NAME
    if cand.exists():
        return cand
    raise TunablesError(
        f"no {DECOMP_NAME} at {p}. The tunable registry and the .sp.j2 template "
        f"come from circuit-decomposition -- run that skill before sizing. "
        f"(structure_groups.json is gone: see this module's docstring.)")


def load(where):
    """-> (registry, template_path). Raises if the read has not been run, or
    was run by a version that predates `tunable_parameters`."""
    doc_path = find_decomposition(where)
    doc = yaml.safe_load(doc_path.read_text()) or {}

    raw = doc.get("tunable_parameters")
    if not raw:
        raise TunablesError(
            f"{doc_path} has no `tunable_parameters` section. It was written by a "
            f"circuit-decomposition older than the template change -- re-run the "
            f"skill on this design to regenerate the registry and the .sp.j2.")

    registry = {}
    for entry in raw:
        registry[entry["name"]] = {
            "param": entry["param"].lower(),
            "members": list(entry["members"]),
            "seed": entry.get("seed"),
            "kind": entry.get("kind", "continuous"),
            # `sizing` -- the loop may propose it. `fold` -- templated only so
            # fold_wide_devices.py can re-render a PDK-bin fold through it;
            # the sizing loop must leave it alone.
            "tunable_by": entry.get("tunable_by", "sizing"),
        }

    tmpl = doc.get("tunable_template")
    tmpl_path = Path(tmpl) if tmpl else doc_path.parent / f"{doc.get('top')}_tunable.sp.j2"
    if not tmpl_path.exists():
        raise TunablesError(
            f"registry found but its template is missing: {tmpl_path}. Re-run "
            f"circuit-decomposition -- the two are written together and must match.")
    return registry, tmpl_path


def fmt(value, kind="continuous"):
    """A netlist reads `4`, not `4.0`. An `integer` variable is a device COUNT:
    it is rounded and floored at 1, because `m=0` is not a device and `m=2.5`
    is not a thing the PDK can draw."""
    if kind == "integer":
        return str(max(1, int(round(float(value)))))
    v = float(value)
    return f"{int(round(v))}" if abs(v - round(v)) < 1e-9 else f"{v:g}"


def seeds(registry):
    return {var: spec["seed"] for var, spec in registry.items() if spec["seed"] is not None}


def sizing_vars(registry):
    """The variables the sizing loop may propose. Everything else in the
    registry is templated for a different writer -- `m` outside a ratio
    carrier belongs to the PDK-bin fold, not to the loop."""
    return {v: s for v, s in registry.items() if s.get("tunable_by", "sizing") == "sizing"}


def read_values(netlist_text, registry):
    """`{var: value}` read out of the netlist itself -- microns for `w`/`l`, a
    plain count for `m`.

    A variable's value is its FIRST member's. Members cannot disagree: they are
    rendered from one template placeholder. `check_consistency()` still exists
    for a netlist someone edited by hand."""
    by_name = {d["name"].lower(): d for d in parse_devices(netlist_text)}
    out = {}
    for var, spec in registry.items():
        param = spec["param"]
        for inst in spec["members"]:
            d = by_name.get(inst.lower())
            if not d:
                continue
            raw = next((v for k, v in d["params"].items() if k.lower() == param), None)
            if raw is None:
                continue
            out[var] = float(raw) if param in ("m", "nf") else _um_val(raw)
            break
    return out


def check_consistency(netlist_text, registry):
    """Every variable whose members do NOT agree, as `[(var, {inst: value})]`.

    Empty for anything this module rendered. Non-empty means the `.sp` was
    hand-edited away from its template -- re-render rather than patching it."""
    by_name = {d["name"].lower(): d for d in parse_devices(netlist_text)}
    bad = []
    for var, spec in registry.items():
        param, seen = spec["param"], {}
        for inst in spec["members"]:
            d = by_name.get(inst.lower())
            if not d:
                continue
            raw = next((v for k, v in d["params"].items() if k.lower() == param), None)
            if raw is None:
                continue
            seen[inst] = float(raw) if param in ("m", "nf") else _um_val(raw)
        if len({round(v, 9) for v in seen.values()}) > 1:
            bad.append((var, seen))
    return bad


def render(template_path, values, registry):
    """The netlist text for `values`. Unknown variable -> KeyError, so a typo
    costs an error rather than an iteration that silently measured nothing.
    A missing one -> jinja2.UndefinedError, never a blank in the netlist."""
    unknown = [v for v in values if v not in registry]
    if unknown:
        raise KeyError(
            "no such tunable: %s -- known variables are %s (from "
            "circuit_decomposition.yaml's tunable_parameters)"
            % (", ".join(sorted(unknown)), ", ".join(sorted(registry))))
    text = Path(template_path).read_text()
    env = jinja2.Environment(undefined=jinja2.StrictUndefined,
                             keep_trailing_newline=True)
    ctx = {var: fmt(val, registry[var]["kind"]) for var, val in values.items()}
    return env.from_string(text).render(**ctx)


def write_values(netlist_path, template_path, registry, updates):
    """Merge `updates` into whatever the netlist currently says and re-render.
    Returns `(new_text, changes)` where `changes` is `[(var, old, new)]`.

    Re-rendering, rather than patching the lines in place, is what makes the
    tie structural: every member of a variable is rewritten from one value
    every single time, so they cannot come apart."""
    netlist_path = Path(netlist_path)
    current = read_values(netlist_path.read_text(), registry) if netlist_path.exists() \
        else dict(seeds(registry))
    merged = dict(seeds(registry))
    merged.update(current)

    changes = []
    for var, val in updates.items():
        if var not in registry:
            raise KeyError(
                "no such tunable: %s -- known variables are %s"
                % (var, ", ".join(sorted(registry))))
        old = merged.get(var)
        new = float(val)
        if old is None or abs(float(old) - new) > 1e-12:
            changes.append((var, old, new))
        merged[var] = new

    missing = [v for v in registry if v not in merged]
    if missing:
        raise TunablesError(
            f"no value for {', '.join(sorted(missing))} -- the netlist does not "
            f"carry them and they have no seed. Re-run circuit-decomposition.")
    return render(template_path, merged, registry), changes


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("design_dir", help="design dir, or circuit_decomposition.yaml itself")
    ap.add_argument("--netlist", default=None, help="print this netlist's current values")
    args = ap.parse_args()

    try:
        registry, tmpl = load(args.design_dir)
    except TunablesError as exc:
        raise SystemExit(str(exc))
    print(f"template: {tmpl}")
    print(f"{len(registry)} tunable variable(s):")
    values = read_values(Path(args.netlist).read_text(), registry) if args.netlist else {}
    for var, spec in registry.items():
        now = f"  now={values[var]:g}" if var in values else ""
        print(f"  {var:12s} {spec['param']:3s} {spec['kind']:10s} "
              f"{spec['tunable_by']:6s} seed={spec['seed']}{now}  <- {spec['members']}")
    if args.netlist:
        bad = check_consistency(Path(args.netlist).read_text(), registry)
        for var, seen in bad:
            print(f"  INCONSISTENT {var}: {seen}")


if __name__ == "__main__":
    main()
