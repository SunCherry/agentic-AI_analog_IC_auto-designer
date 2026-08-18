#!/usr/bin/env python3
"""Pick which MOS devices device-shaper should sweep, from
`circuit_decomposition.yaml`'s `parasitic_sensitivity` section.

The rule this implements, and nothing more: an entry whose `severity` is
`high` names devices whose behaviour layout parasitics will actually move,
so those are the devices whose finger count is worth measuring. Every other
MOS device keeps whatever `nf` the sized netlist already carries -- sweeping
it would burn simulations on a device nobody flagged.

`parasitic_sensitivity[].ref` is PROSE, not a device list (schematic-agent
writes it as free text: "net5 (stage-1 output; drains of XMP2/XMN2, gate of
XMP3)"). So device names are recovered by tokenizing that prose and keeping
only tokens that match a REAL MOS instance name in the netlist -- the same
match-against-the-netlist approach `run_extrapolation.find_offending_device()`
uses to recover a name out of ngspice's error text. A token that isn't a real
MOS instance (a net name, a resistor, an English word) can therefore never
enter the selection.

Only `ref` is scanned, deliberately. `why` routinely mentions devices as
supporting argument ("XMN1/XMN2 are 320 um") rather than as the entry's
subject, and scanning it over-selects. Pass `--devices` to override the
selection by hand when a `ref` is worded so that this misses one.
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "parasitic-estimation", "script"))

from estimate_parasitics import _MOS_RE  # noqa: E402

HIGH = "high"
# Instance-name-shaped tokens: SPICE names, so letters/digits/_ after the
# leading M or X. Split on anything else so "XMP2/XMN2," yields both names.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def mos_devices(netlist_path):
    """{instance_name: nf} for every MOS line in the netlist.

    `nf` is what the netlist already says (the value a non-selected device
    keeps). Missing `nf=` means the netlist never chose one -- recorded as
    None so a caller can tell "unset" from "explicitly 1".
    """
    devices = {}
    with open(netlist_path) as f:
        for line in f:
            m = _MOS_RE.match(line.strip())
            if not m:
                continue
            nf_m = re.search(r"\bnf\s*=\s*(\d+)", m.group("params"), re.IGNORECASE)
            devices[m.group("name")] = int(nf_m.group(1)) if nf_m else None
    return devices


def _names_in(text, known):
    """Every real MOS instance name appearing as a token in `text`.

    Case-insensitive, because a writeup may say `xmn1` where the netlist
    says `XMN1`; the netlist's own spelling is what comes back.
    """
    by_lower = {n.lower(): n for n in known}
    found = []
    for tok in _TOKEN_RE.findall(text or ""):
        real = by_lower.get(tok.lower())
        if real and real not in found:
            found.append(real)
    return found


def select(decomposition_path, netlist_path, severities=(HIGH,)):
    import yaml

    with open(decomposition_path) as f:
        doc = yaml.safe_load(f)

    entries = doc.get("parasitic_sensitivity") or []
    if not entries:
        return {"ok": False, "error":
                f"{decomposition_path} has no `parasitic_sensitivity` section. "
                f"That section is schematic-agent's step 2c, appended after "
                f"circuit-decomposition runs -- device-shaper cannot choose "
                f"which devices to sweep without it. Re-run schematic-agent's "
                f"circuit read, or pass --devices explicitly."}

    if not doc.get("confirmed_by_user"):
        # Not fatal: reported so the caller decides, the same way the
        # decomposition file's own consumers treat the flag.
        pass

    known = mos_devices(netlist_path)
    selected, matched_entries, empty_entries = [], [], []

    for i, entry in enumerate(entries):
        if str(entry.get("severity", "")).strip().lower() not in severities:
            continue
        ref = str(entry.get("ref", ""))
        names = _names_in(ref, known)
        record = {
            "index": i,
            "ref": ref,
            "severity": entry.get("severity"),
            "spec_keys": entry.get("spec_keys", []),
            "devices": names,
        }
        if names:
            matched_entries.append(record)
            for n in names:
                if n not in selected:
                    selected.append(n)
        else:
            empty_entries.append(record)

    return {
        "ok": True,
        "decomposition": os.path.abspath(decomposition_path),
        "netlist": os.path.abspath(netlist_path),
        "confirmed_by_user": bool(doc.get("confirmed_by_user")),
        "severities": list(severities),
        "shape_devices": selected,
        "pinned_devices": {n: nf for n, nf in known.items() if n not in selected},
        "matched_entries": matched_entries,
        "entries_without_devices": empty_entries,
        "spec_keys": sorted({k for e in matched_entries for k in (e["spec_keys"] or [])}),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("decomposition", help="<design_dir>/circuit_decomposition.yaml")
    ap.add_argument("netlist", help="the sized netlist the sweep will run on")
    ap.add_argument("--severity", default=HIGH,
                    help="comma-separated severities to select (default: high)")
    ap.add_argument("--devices", default=None,
                    help="comma-separated instance names; overrides the yaml "
                         "selection entirely (still validated against the netlist)")
    ap.add_argument("-o", "--out", default=None,
                    help="write the selection JSON here (default: stdout only)")
    args = ap.parse_args()

    sevs = tuple(s.strip().lower() for s in args.severity.split(",") if s.strip())
    res = select(args.decomposition, args.netlist, severities=sevs)

    if args.devices is not None:
        known = mos_devices(args.netlist)
        asked = [d.strip() for d in args.devices.split(",") if d.strip()]
        unknown = [d for d in asked if d not in known]
        if unknown:
            sys.exit(f"error: not MOS instances in {args.netlist}: {unknown}")
        res = dict(res) if res.get("ok") else {"ok": True, "netlist": os.path.abspath(args.netlist)}
        res["ok"] = True
        res["shape_devices"] = asked
        res["pinned_devices"] = {n: nf for n, nf in known.items() if n not in asked}
        res["selection_source"] = "--devices (manual override)"
        res.pop("error", None)
    elif not res["ok"]:
        sys.exit(f"error: {res['error']}")
    else:
        res["selection_source"] = f"parasitic_sensitivity severity={','.join(sevs)}"

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(res, f, indent=2)

    sel = res["shape_devices"]
    print(f"Selected {len(sel)} device(s) to sweep: {', '.join(sel) if sel else '(none)'}")
    print(f"  source: {res['selection_source']}")
    if res.get("spec_keys"):
        print(f"  specs at risk: {', '.join(res['spec_keys'])}")
    pinned = res.get("pinned_devices") or {}
    if pinned:
        print(f"  pinned at netlist nf ({len(pinned)}): "
              + ", ".join(f"{n}=nf{v if v is not None else '?'}" for n, v in pinned.items()))
    for e in res.get("entries_without_devices", []):
        print(f"  NOTE: high-severity entry names no MOS device: {e['ref'][:70]!r}")
    if not res.get("confirmed_by_user", True):
        print("  WARNING: circuit_decomposition.yaml has confirmed_by_user: false")
    if args.out:
        print(f"  -> {args.out}")
    if not sel:
        sys.exit("error: no MOS device selected -- nothing to sweep. Either no "
                 "high-severity entry names a MOS device (check the NOTEs above), "
                 "or pass --devices explicitly.")


if __name__ == "__main__":
    main()
