#!/usr/bin/env python3
"""Compute the post-layout fidelity metric between a pre-layout and a
post-layout ngspice raw (.out, ASCII) pair.

The metric is REGISTRY-DRIVEN so it can grow past the AC sweep:

  * a `Metric` names a quantity, which analysis produces it, how its
    delta is measured (absolute or relative) and its default tolerance;
  * an EXTRACTOR is one function per analysis, returning {key: value}.

**Adding a metric is a one-line `METRICS` entry.** Adding a whole new
analysis is that plus one extractor registered in `EXTRACTORS` -- no
change to `compare()`, `print_report()`, the CLI, or any caller.

Today only the AC sweep is implemented (Gain / UGBW / PM), which is what
this project has so far had a validated extraction for. See metrics.md in
this directory for the formula, the default tolerances and the required
report table.

Usage:
    python3 compute_fidelity.py <pre.out> <post.out> \
        [--gain-tol 1.0] [--ugb-rel-tol 0.15] [--pm-tol 5.0] [--iter N]
        [--tol KEY=VALUE ...] [--analysis ac]
"""
import argparse
import sys
from dataclasses import dataclass

import numpy as np


# --------------------------------------------------------------------
# Metric registry
# --------------------------------------------------------------------

@dataclass(frozen=True)
class Metric:
    """One comparable quantity.

    key        canonical name -- matches the `target_spec.json` spec key
    label      report-table label, carrying the DISPLAY unit
    analysis   which extractor produces it
    tol        default tolerance (absolute in the metric's own unit, or
               a fraction when delta == "rel")
    tol_unit   unit the tolerance prints in; ignored when delta == "rel",
               which always prints as a percentage
    delta      "abs" -> post - pre;  "rel" -> (post - pre) / pre
    scale      value / scale == the displayed number (Hz -> MHz is 1e6)
    fmt        display format for pre/post/delta
    """
    key: str
    label: str
    analysis: str
    tol: float
    tol_unit: str = ""
    delta: str = "abs"
    scale: float = 1.0
    fmt: str = "{:.2f}"

    def tol_label(self, tol=None):
        """Print the EFFECTIVE tolerance, not the default -- a `--tol`
        override that still displayed 15% would make the table lie about
        what the yes/no column was decided against."""
        t = self.tol if tol is None else tol
        if self.delta == "rel":
            return f"{t * 100:g}%"
        # :g keeps significant digits (a 0.05 tolerance must not print as
        # "0.1"), then pad to one decimal so the common whole-number case
        # reads "1.0 dB" / "5.0 deg" as metrics.md's table shows it.
        s = f"{t:g}"
        if "." not in s and "e" not in s:
            s += ".0"
        return f"{s} {self.tol_unit}".strip()


# Defaults are metrics.md's; override per-design via `tols=` / `--tol`.
METRICS = [
    Metric("Gain", "DC gain (dB)", "ac", 1.0, "dB"),
    Metric("UGBW", "UGB (MHz)", "ac", 0.15,
           delta="rel", scale=1e6, fmt="{:.3f}"),
    Metric("PM", "PM (deg)", "ac", 5.0, "deg"),
]


def metrics_for(analysis):
    """Registry entries an analysis produces, in report order."""
    return [m for m in METRICS if m.analysis == analysis]


# --------------------------------------------------------------------
# Extractors -- one per analysis, each returning {key: value or None}
# --------------------------------------------------------------------

def parse_ac_raw(path):
    with open(path) as f:
        lines = f.readlines()
    toks = "".join(lines[lines.index("Values:\n") + 1:]).split()
    freqs, vals = [], []
    for k in range(0, len(toks), 3):
        freqs.append(float(toks[k + 1].split(",")[0]))
        vr, vi = map(float, toks[k + 2].split(","))
        vals.append(complex(vr, vi))
    return np.array(freqs), np.array(vals)


def extract_ac(path):
    """Gain/UGBW/PM from an AC sweep raw file.

    UGBW and PM are None when the response never crosses 0 dB inside the
    swept band -- a real result, not an error: it means the sweep stopped
    short or the circuit has no unity-gain crossing. Callers must treat
    None as 'not measured' rather than substituting a number.
    """
    f, v = parse_ac_raw(path)
    mag = 20 * np.log10(np.abs(v))
    ph = np.unwrap(np.angle(v)) * 180 / np.pi
    ph -= ph[0]
    below = np.where(mag < 0)[0]
    if not len(below):
        return {"Gain": mag[0], "UGBW": None, "PM": None}
    j = below[0]
    ugb = 10 ** np.interp(0, [mag[j], mag[j - 1]],
                          [np.log10(f[j]), np.log10(f[j - 1])])
    pm = 180 + np.interp(np.log10(ugb), np.log10(f), ph)
    return {"Gain": mag[0], "UGBW": ugb, "PM": pm}


# Register a new analysis here -- e.g. "tran": extract_tran -- and add its
# Metric entries above. Nothing below this line needs to change.
EXTRACTORS = {
    "ac": extract_ac,
}


def metrics_from(path, analysis="ac"):
    """Every metric one raw file yields, as {key: value or None}."""
    if analysis not in EXTRACTORS:
        raise NotImplementedError(
            f"no extractor for analysis {analysis!r}; "
            f"available: {sorted(EXTRACTORS)}. Add one to EXTRACTORS and "
            f"its Metric entries to METRICS."
        )
    return EXTRACTORS[analysis](path)


def ac_metrics(path):
    """Return (dc_gain_dB, ugb_Hz, phase_margin_deg).

    The long-standing 3-tuple several scripts unpack directly
    (run_sizing_iteration.py, sweep_fingers.py, run_extrapolation.py,
    calibrate_strap_parasitics.py). Kept as-is on purpose: it is a
    published signature, not an internal one. New code should prefer
    `metrics_from(path)`, which is keyed and extensible.
    """
    m = extract_ac(path)
    return m["Gain"], m["UGBW"], m["PM"]


# --------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------

def compare(pre_path, post_path, tols=None, analysis="ac"):
    """Pre-vs-post for every metric `analysis` produces.

    `tols` overrides defaults per key: {"Gain": 0.5, "UGBW": 0.10}.
    A metric missing from either side is listed under "missing" and
    excluded from E -- never silently treated as zero drift.
    """
    tols = dict(tols or {})
    pre_vals = metrics_from(pre_path, analysis)
    post_vals = metrics_from(post_path, analysis)

    out, missing, E = {}, [], 0.0
    for m in metrics_for(analysis):
        a, b = pre_vals.get(m.key), post_vals.get(m.key)
        if a is None or b is None:
            missing.append(m.key)
            continue
        tol = tols.get(m.key, m.tol)
        if m.delta == "rel":
            d = (b - a) / a if a else float("inf")
        else:
            d = b - a
        e = abs(d) / tol
        E += e
        out[m.key] = {
            "pre": a, "post": b, "delta": d, "delta_kind": m.delta,
            "tol": tol, "within_tol": abs(d) <= tol, "e_term": e,
        }

    return {
        "analysis": analysis,
        "metrics": out,
        "missing": missing,
        "E": E,
        # A missing metric cannot pass: E would be a sum over fewer terms
        # and would read as better than a complete comparison.
        "pass": E <= 1.0 and not missing,
    }


def fidelity(pre_path, post_path, gain_tol=1.0, ugb_rel_tol=0.15, pm_tol=5.0):
    """AC fidelity, with the flat legacy keys alongside the general ones.

    `pre`/`post`/`delta`/`within_tol`/`E_terms` are the original shape and
    stay for existing readers; `metrics`/`missing` are the extensible view.
    """
    res = compare(pre_path, post_path,
                  {"Gain": gain_tol, "UGBW": ugb_rel_tol, "PM": pm_tol},
                  analysis="ac")
    m = res["metrics"]
    if all(k in m for k in ("Gain", "UGBW", "PM")):
        res.update({
            "pre": {"gain_dB": m["Gain"]["pre"], "ugb_Hz": m["UGBW"]["pre"],
                    "pm_deg": m["PM"]["pre"]},
            "post": {"gain_dB": m["Gain"]["post"], "ugb_Hz": m["UGBW"]["post"],
                     "pm_deg": m["PM"]["post"]},
            "delta": {"gain_dB": m["Gain"]["delta"],
                      "ugb_rel": m["UGBW"]["delta"],
                      "pm_deg": m["PM"]["delta"]},
            "within_tol": {"gain": m["Gain"]["within_tol"],
                           "ugb": m["UGBW"]["within_tol"],
                           "pm": m["PM"]["within_tol"]},
            "E_terms": {"gain": m["Gain"]["e_term"], "ugb": m["UGBW"]["e_term"],
                        "pm": m["PM"]["e_term"]},
        })
    return res


# --------------------------------------------------------------------
# Report -- metrics.md's table, built from the registry
# --------------------------------------------------------------------

def print_report(result, iteration=None):
    header = f"Iteration {iteration} — " if iteration is not None else ""
    print(f"{header}post-layout fidelity report")
    print(f"{'metric':<14}{'pre-layout':>14}{'post-layout':>14}"
          f"{'delta':>16}{'tolerance':>12}{'within tol?':>13}")

    for m in metrics_for(result.get("analysis", "ac")):
        r = result["metrics"].get(m.key)
        if r is None:
            print(f"{m.label:<14}{'-':>14}{'-':>14}"
                  f"{'NOT MEASURED':>16}{m.tol_label():>12}{'n/a':>13}")
            continue
        pre = m.fmt.format(r["pre"] / m.scale)
        post = m.fmt.format(r["post"] / m.scale)
        if m.delta == "rel":
            delta = f"{r['delta'] * 100:.1f}%"
        else:
            delta = m.fmt.format(r["delta"])
        ok = "yes" if r["within_tol"] else "no"
        print(f"{m.label:<14}{pre:>14}{post:>14}{delta:>16}"
              f"{m.tol_label(r['tol']):>12}{ok:>13}")

    print(f"{'E (combined)':<14}{'-':>14}{'-':>14}{result['E']:>16.3f}"
          f"{'<=1.0':>12}{'yes' if result['pass'] else 'no':>13}")
    if result["missing"]:
        print(f"\nNOT MEASURED: {', '.join(result['missing'])} -- absent from "
              f"one or both raw files, so excluded from E. This is not a pass.")


def _parse_tol(pairs):
    tols = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"--tol expects KEY=VALUE, got {p!r}")
        k, v = p.split("=", 1)
        tols[k] = float(v)
    return tols


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pre", help="pre-layout raw .out (ASCII)")
    ap.add_argument("post", help="post-layout raw .out (ASCII)")
    ap.add_argument("--analysis", default="ac", choices=sorted(EXTRACTORS))
    ap.add_argument("--gain-tol", type=float, default=1.0)
    ap.add_argument("--ugb-rel-tol", type=float, default=0.15)
    ap.add_argument("--pm-tol", type=float, default=5.0)
    ap.add_argument("--tol", action="append", metavar="KEY=VALUE",
                    help="tolerance for any metric, repeatable; "
                         "wins over the three named flags")
    ap.add_argument("--iter", type=int, default=None)
    args = ap.parse_args()

    tols = {"Gain": args.gain_tol, "UGBW": args.ugb_rel_tol, "PM": args.pm_tol}
    tols.update(_parse_tol(args.tol))

    result = compare(args.pre, args.post, tols, analysis=args.analysis)
    print_report(result, args.iter)
    sys.exit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
