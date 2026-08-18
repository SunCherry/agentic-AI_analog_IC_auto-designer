#!/usr/bin/env python3
"""Shared rendering engine for a converged run's figure.

This file is NOT the entry point a sizing run uses: `generate_plot_script.py`
writes a per-design `plot_<design_name>.py` into the project folder, and that
script imports `plot_ac` / `plot_generic` / `_spec_keys` from here. That keeps
the drawing logic shared (a rendering fix reaches every design) while the
per-design facts -- analysis, spec file, title, output name -- live beside the
design, not in a flag list.

**Driven by the spec, not by a circuit class.** Nothing here knows what an
amplifier is. What gets drawn is decided by two things the design already
states:

  * **the analysis** -- which raw file was written, and therefore what the
    x-axis is. `ac` gives a Bode pair (magnitude dB, phase deg, log frequency);
    any other analysis is drawn as the saved signal against its own sweep
    column, with no interpretation layered on top.
  * **the spec keys** -- every key `target_spec.json` carries that this run can
    locate on the plot is marked, with its target drawn beside the achieved
    value. A key the extractor does not produce is simply not marked; it is
    never guessed at, and never invented from the curve.

So a design specified on `Gain`/`UGBW`/`PM` gets those three marked, and a
design specified on something else gets whatever its own registered extractor
produces. Adding an analysis to `compute_fidelity.EXTRACTORS` is what teaches
this engine a new circuit class.

Standalone (ad-hoc / debug only -- prefer the generated per-design script):
  python plot_results.py <raw.out> -o <design>_ac.png
      [--analysis ac] [--spec target_spec.json] [--title "..."]
      [--compare <other_raw.out> --labels "before,after"]
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "..", "..", "reference"))

POINT_RE = re.compile(r"^\d+$")


def parse_ngspice_ascii_raw(path):
    """(var_names, columns, is_complex) from an ngspice ASCII raw file."""
    import numpy as np
    text = open(path).read()
    header, sep, values = text.partition("Values:")
    if not sep:
        raise ValueError(f"{path}: not an ngspice ASCII raw file "
                         "(no 'Values:' section -- was `set filetype=ascii` set?)")
    names, nvars, flags = [], None, ""
    lines = header.splitlines()
    for i, line in enumerate(lines):
        t = line.strip()
        if t.startswith("Flags:"):
            flags = t.split(":", 1)[1].strip()
        elif t.startswith("No. Variables:"):
            nvars = int(t.split(":", 1)[1].strip())
        elif t.startswith("Variables:"):
            for vl in lines[i + 1:]:
                p = vl.strip().split()
                if len(p) >= 2 and p[0].isdigit():
                    names.append(p[1])
            break
    is_complex = "complex" in flags.lower()
    nums = []
    for tok in values.split():
        if POINT_RE.match(tok) and len(nums) % (nvars or 1) == 0:
            continue                      # the per-point index column
        nums.append(tok)
    cols = [[] for _ in names]
    per = len(names)
    for i, tok in enumerate(nums):
        col = i % per
        if is_complex:
            re_s, _, im_s = tok.partition(",")
            cols[col].append(complex(float(re_s), float(im_s or 0.0)))
        else:
            cols[col].append(float(tok))
    return names, [np.array(c) for c in cols], is_complex


def _spec_keys(spec_path):
    """{key: (direction, value, units)} -- tolerant of both spec forms."""
    if not spec_path or not os.path.isfile(spec_path):
        return {}
    from run_sizing_iteration import parse_spec_entry
    out = {}
    for k, entry in json.load(open(spec_path)).items():
        try:
            out[k] = parse_spec_entry(k, entry)
        except ValueError:
            continue
    return out


def plot_ac(paths, labels, out_path, spec, title):
    """A Bode pair, with every locatable spec key marked."""
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from compute_fidelity import metrics_from

    fig, (ax_m, ax_p) = plt.subplots(2, 1, figsize=(8, 6.5), sharex=True,
                                     gridspec_kw={"height_ratios": [2, 1]})
    for path, label in zip(paths, labels):
        names, cols, is_complex = parse_ngspice_ascii_raw(path)
        f = np.real(cols[0])
        sig = cols[-1]
        mag = 20 * np.log10(np.abs(sig) + 1e-30)
        ph = np.degrees(np.unwrap(np.angle(sig))) if is_complex else np.zeros_like(f)
        ax_m.semilogx(f, mag, label=label, lw=1.6)
        ax_p.semilogx(f, ph, lw=1.2)

        m = metrics_from(path, analysis="ac")
        # Mark only what this run actually produced -- a key that came back
        # None is a measurement the sweep did not support, not a zero.
        if m.get("UGBW"):
            ax_m.axvline(m["UGBW"], ls=":", lw=1, color="0.5")
            ax_m.annotate(f"UGBW {m['UGBW']/1e6:.2f} MHz",
                          xy=(m["UGBW"], 0), xytext=(4, 8),
                          textcoords="offset points", fontsize=8)
        if m.get("Gain") is not None:
            ax_m.annotate(f"Gain {m['Gain']:.2f} dB", xy=(f[0], m["Gain"]),
                          xytext=(6, -12), textcoords="offset points", fontsize=8)
        if m.get("PM") is not None and m.get("UGBW"):
            ax_p.annotate(f"PM {m['PM']:.2f}°", xy=(m["UGBW"], -180 + m["PM"]),
                          xytext=(6, 6), textcoords="offset points", fontsize=8)

    # The target beside the achieved value -- the whole point of the figure.
    for key, (direction, value, units) in spec.items():
        u = f" {units}" if units else ""
        if key.upper() in ("GAIN",):
            _hline(ax_m, value, direction, f"{key} target", u)
        elif key.upper() in ("PM",):
            base = -180
            if direction == "RANGE":
                ax_p.axhspan(base + value[0], base + value[1], color="tab:green",
                             alpha=0.10, label=f"{key} target {value[0]}-{value[1]}{u}")
            else:
                _hline(ax_p, base + value, direction, f"{key} target", u)
        elif key.upper() in ("UGBW",):
            scale = 1e6 if (units or "MHz").lower() == "mhz" else 1.0
            if direction != "RANGE":
                ax_m.axvline(value * scale, ls="--", lw=1, color="tab:red",
                             label=f"{key} target {value:g}{u}")

    ax_m.axhline(0, color="0.7", lw=0.8)
    ax_m.set_ylabel("magnitude (dB)")
    ax_m.grid(True, which="both", alpha=0.25)
    ax_m.legend(fontsize=8, loc="best")
    ax_p.axhline(-180, color="0.7", lw=0.8)
    ax_p.set_ylabel("phase (deg)")
    ax_p.set_xlabel("frequency (Hz)")
    ax_p.grid(True, which="both", alpha=0.25)
    if ax_p.get_legend_handles_labels()[0]:
        ax_p.legend(fontsize=8, loc="best")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    return out_path


def _hline(ax, value, direction, label, units=""):
    if direction == "RANGE":
        ax.axhspan(value[0], value[1], color="tab:green", alpha=0.10,
                   label=f"{label} {value[0]:g}-{value[1]:g}{units}")
    else:
        ax.axhline(value, ls="--", lw=1, color="tab:red",
                   label=f"{label} {value:g}{units}")


def plot_generic(paths, labels, out_path, title):
    """Any analysis with no registered interpretation: the saved signal against
    its own sweep column, and nothing inferred on top of it."""
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(8, 5))
    for path, label in zip(paths, labels):
        names, cols, is_complex = parse_ngspice_ascii_raw(path)
        x = np.real(cols[0])
        y = np.abs(cols[-1]) if is_complex else np.real(cols[-1])
        ax.plot(x, y, label=label, lw=1.6)
        ax.set_xlabel(names[0] if names else "sweep")
        ax.set_ylabel(names[-1] if names else "signal")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("raw", help="the ngspice ASCII raw file the deck wrote")
    ap.add_argument("--compare", default=None,
                    help="a second raw file to overlay (e.g. the seed iteration)")
    ap.add_argument("--labels", default=None,
                    help="comma-separated curve labels, in order")
    ap.add_argument("--analysis", default="ac",
                    help="which extractor interprets the file, keyed into "
                         "compute_fidelity.EXTRACTORS. Anything without one is "
                         "drawn as a plain signal-vs-sweep curve.")
    ap.add_argument("--spec", default=None,
                    help="target_spec.json -- every key that can be located on "
                         "the plot is marked with its target")
    ap.add_argument("--title", default=None)
    ap.add_argument("-o", "--out", required=True, help="output .png")
    args = ap.parse_args()

    paths = [args.raw] + ([args.compare] if args.compare else [])
    labels = ([s.strip() for s in args.labels.split(",")] if args.labels
              else [os.path.basename(p) for p in paths])
    labels += [os.path.basename(p) for p in paths[len(labels):]]
    spec = _spec_keys(args.spec)
    title = args.title or os.path.splitext(os.path.basename(args.raw))[0]

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    try:
        if args.analysis == "ac":
            out = plot_ac(paths, labels, args.out, spec, title)
        else:
            out = plot_generic(paths, labels, args.out, title)
    except (ValueError, IndexError) as e:
        sys.exit(f"error: {e}")
    print(f"Wrote {out}")
    if spec:
        print("Marked spec keys: " + ", ".join(sorted(spec)))


if __name__ == "__main__":
    main()
