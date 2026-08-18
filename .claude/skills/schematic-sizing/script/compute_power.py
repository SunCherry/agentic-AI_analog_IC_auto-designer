#!/usr/bin/env python3
"""Compute a netlist's total static (DC bias) power dissipation from a
single VDD supply, given per-device operating-point data -- see
../SKILL.md's "Power budget" section.

Power = VDD * (sum of every MOS device's own `id` whose `source` net is
the VDD supply net) -- by KCL, in a single-supply circuit where every
current path that leaves VDD eventually returns to VSS through the
devices this project's op-point probe already extracts, this equals the
total DC current the supply itself delivers, without needing a separate
supply-current probe/testbench change.

Usage (standalone):
  python compute_power.py <rendered_netlist.sp> <op_points.json> --vdd 1.8
      [--vdd-net VDD]
"""
import argparse
import json
import re
import sys

from netlist_devices import parse_devices


def extract_vdd(testbench_text, vdd_net="VDD"):
    """Parse the testbench's own VDD supply value (e.g. `vdd VDD 0
    dc=1.8`) rather than hardcoding it -- the validated testbench pattern
    always has exactly one DC source tied to the VDD net. Returns the float
    value, or
    raises ValueError if no such source is found."""
    pattern = re.compile(
        rf'^\S+\s+{re.escape(vdd_net)}\s+\S+\s+dc\s*=?\s*([\d.eE+-]+)',
        re.IGNORECASE | re.MULTILINE)
    m = pattern.search(testbench_text)
    if not m:
        raise ValueError(f"no DC source tied to net {vdd_net!r} found in testbench -- "
                          f"pass --vdd explicitly")
    return float(m.group(1))


def extract_supply_volts(testbench_text, supply_nets):
    """`{NET_UPPER: volts}` for every named rail, each parsed from the
    deck's own DC source (`extract_vdd()` per net).

    **One voltage cannot stand in for several rails.** A split or
    multi-rail design has its rails at different volts by definition, so a
    single scalar multiplied by the summed current of all of them is wrong
    by whatever the rails differ -- and wrong *plausibly*, which is the
    failure mode this whole module is written against. Raises ValueError
    naming the rail whose DC source could not be parsed, rather than
    quietly reusing another rail's number for it."""
    volts = {}
    for net in supply_nets:
        volts[net.upper()] = extract_vdd(testbench_text, vdd_net=net)
    return volts


def compute_total_power_mw(netlist_text, op_points, vdd=None, vdd_net=None,
                            supply_nets=None, supply_volts=None):
    """Return (total_power_mw, per_device_ma) -- per_device_ma is
    {device_name: current_mA} for every device whose `source` net is one of
    the supply nets (case-insensitive), for transparency/debugging. op_points
    keys are looked up case-insensitively (`id_<name>`), matching this
    project's op-point probe convention regardless of case.

    `supply_nets` takes one or more net names, because **a design's supply
    net is not reliably called VDD** and a split or multi-rail design has
    more than one: summing a single net and reporting it as the design's power
    undercounts by whatever the other rails deliver, and nothing downstream
    can see that from the number alone. `vdd_net` is the older single-net
    spelling, still accepted.

    **Pass `supply_volts` ({net: volts}) whenever more than one rail is
    named** -- each device is then weighted by ITS OWN rail's voltage,
    which is the only correct sum across rails at different potentials.
    The scalar `vdd` path is the single-rail shorthand it degrades to, and
    with several rails named it is right only if they happen to sit at the
    same voltage; `extract_supply_volts()` builds the dict.

    **An empty `per_device_ma` means no device's source sat on any named
    net** -- that is a 0.0 W answer the caller must not read as "this design
    uses no power". Callers check it and say so (see
    run_sizing_iteration.py's `power_note`).
    """
    if supply_nets is None:
        supply_nets = (vdd_net,) if vdd_net else ("VDD",)
    if isinstance(supply_nets, str):
        supply_nets = (supply_nets,)
    wanted = {n.upper() for n in supply_nets}

    volts = None
    if supply_volts:
        volts = {k.upper(): float(v) for k, v in supply_volts.items()}
    elif vdd is None:
        raise ValueError("compute_total_power_mw needs either supply_volts "
                          "({net: volts}) or a scalar vdd")

    devices = parse_devices(netlist_text)
    op_lower = {k.lower(): v for k, v in op_points.items()}

    per_device_ma = {}
    per_device_net = {}
    for d in devices:
        if d["kind"] != "mos" or len(d["nets"]) < 4:
            continue
        source = d["nets"][2]
        if source.upper() not in wanted:
            continue
        id_key = f"id_{d['name'].lower()}"
        id_val = op_lower.get(id_key)
        if id_val is None:
            continue
        per_device_ma[d["name"]] = id_val * 1e3  # A -> mA
        per_device_net[d["name"]] = source.upper()

    if volts is not None:
        missing = sorted({n for n in per_device_net.values() if n not in volts})
        if missing:
            raise ValueError("no voltage supplied for rail(s) %s -- every net "
                              "a device draws through needs one" % ", ".join(missing))
        total_power_mw = sum(ma * volts[per_device_net[name]]
                              for name, ma in per_device_ma.items())
    else:
        total_power_mw = sum(per_device_ma.values()) * vdd
    return total_power_mw, per_device_ma


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("netlist")
    ap.add_argument("op_points_json")
    ap.add_argument("--vdd", required=True,
                     help="rail voltage. One value applies to every named net; "
                          "give one per --vdd-net (comma-separated, same order) "
                          "when the rails sit at different voltages")
    ap.add_argument("--vdd-net", default="VDD",
                     help="supply net(s), comma-separated for a multi-rail design")
    args = ap.parse_args()

    netlist_text = open(args.netlist).read()
    op_points = json.load(open(args.op_points_json))

    supply_nets = tuple(n.strip() for n in args.vdd_net.split(",") if n.strip())
    vals = [float(v) for v in args.vdd.split(",") if v.strip()]
    if len(vals) == 1:
        vals = vals * len(supply_nets)
    if len(vals) != len(supply_nets):
        ap.error(f"--vdd has {len(vals)} value(s) for {len(supply_nets)} net(s) -- "
                  f"give one value, or one per net in the same order")
    supply_volts = dict(zip((n.upper() for n in supply_nets), vals))

    total_mw, per_device = compute_total_power_mw(
        netlist_text, op_points, supply_nets=supply_nets,
        supply_volts=supply_volts)
    rails = ", ".join(f"{n}={v}V" for n, v in supply_volts.items())
    print(f"Total power: {total_mw:.4f} mW ({rails})")
    for name, ma in sorted(per_device.items()):
        print(f"  {name}: {ma:.4f} mA")


if __name__ == "__main__":
    main()
