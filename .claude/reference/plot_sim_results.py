#!/usr/bin/env python3
"""Plot simulation output, and own this project's ngspice-raw parser.

`parse_ngspice_ascii_raw()` here is the single implementation every other
script imports rather than re-deriving -- `process_results_template.py` and
`compute_fidelity.py` beside this file both do. Keep it in this folder: it is
reference machinery, not a per-design artifact, and moving it back beside the
designs is what previously made deleting a design tree break every
results script.

Supports the two formats this project's testbenches produce:

  * ngspice ASCII raw files (e.g. simout_pre.out / simout_post.out),
    written via `set filetype=ascii` + `write <file> <signal>`.
  * plain CSV exports (e.g. <design_name>_pre.csv) with a header row
    naming each column, first column being the sweep variable
    (frequency/time) and the rest the signal(s).

Usage:
    python plot_sim_results.py <file> [<file2> ...] [-o out.png] [--show]

Files can be mixed .out/.csv and are overlaid on the same axes (e.g.
compare pre- vs post-layout: simout_pre.out vs simout_post.out, or
simout_pre.out vs miller_ota_3_pre.csv).
"""
import argparse
import csv
import os
import re

import numpy as np
import matplotlib.pyplot as plt

POINT_HEADER_RE = re.compile(r"^\d+$")


def parse_ngspice_ascii_raw(path):
    """Parse an ngspice ASCII raw file into (plotname, var_names, columns, is_complex)."""
    with open(path) as f:
        text = f.read()

    header, sep, values_block = text.partition("Values:")
    if not sep:
        raise ValueError(f"{path}: not an ngspice ASCII raw file (no 'Values:' section)")

    plotname = None
    flags = ""
    nvars = None
    var_names = []

    lines = header.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("Plotname:"):
            plotname = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("Flags:"):
            flags = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("No. Variables:"):
            nvars = int(stripped.split(":", 1)[1].strip())
        elif stripped.startswith("Variables:"):
            for var_line in lines[i + 1:]:
                var_line = var_line.strip()
                if not var_line:
                    continue
                parts = var_line.split()
                if parts and POINT_HEADER_RE.match(parts[0]):
                    var_names.append(parts[1])

    if nvars is None or not var_names:
        raise ValueError(f"{path}: could not parse variable list")

    is_complex = "complex" in flags.lower()
    columns = [[] for _ in range(nvars)]
    col = 0
    for raw_line in values_block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = line.split(None, 1)
        if len(tokens) == 2 and POINT_HEADER_RE.match(tokens[0]):
            value_str = tokens[1]
            col = 0
        else:
            value_str = tokens[0] if len(tokens) == 1 else line

        if is_complex:
            re_s, im_s = value_str.split(",")
            columns[col].append(complex(float(re_s), float(im_s)))
        else:
            columns[col].append(float(value_str))
        col += 1

    columns = [np.array(c) for c in columns]
    return plotname, var_names, columns, is_complex


def parse_csv(path):
    """Parse a plain CSV export (header row + numeric columns) into the
    same (plotname, var_names, columns, is_complex) shape as the ngspice
    raw parser, so both can be plotted uniformly. CSV data is always
    treated as real-valued."""
    with open(path, newline="") as f:
        reader = csv.reader(f)
        var_names = next(reader)
        rows = [row for row in reader if row]

    columns = [np.array([float(row[i]) for row in rows]) for i in range(len(var_names))]
    return None, var_names, columns, False


def load_signal_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return parse_csv(path)
    return parse_ngspice_ascii_raw(path)


def plot_files(paths, out_path=None, show=False):
    fig = None
    axes = None

    for path in paths:
        label = os.path.splitext(os.path.basename(path))[0]
        plotname, var_names, columns, is_complex = load_signal_file(path)
        x = columns[0].real if is_complex else columns[0]
        x_name = var_names[0]
        sig_names = var_names[1:]
        sig_cols = columns[1:]

        is_ac = is_complex and "ac" in (plotname or "").lower()
        log_x = "freq" in x_name.lower()

        if fig is None:
            if is_ac:
                fig, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True)
            else:
                fig, ax = plt.subplots(figsize=(8, 5))
                axes = [ax]
            fig.suptitle(plotname or "Simulation Result")

        # Real-valued CSV frequency sweeps hold amplifier gain (Vout/Vin);
        # plot that in dB like a Bode magnitude plot instead of raw volts.
        is_gain_db = log_x and not is_complex

        for name, y in zip(sig_names, sig_cols):
            if is_ac:
                mag_db = 20 * np.log10(np.abs(y))
                phase_deg = np.angle(y, deg=True)
                axes[0].semilogx(x, mag_db, label=f"{label}: {name}")
                axes[1].semilogx(x, phase_deg, label=f"{label}: {name}")
            elif is_gain_db:
                mag_db = 20 * np.log10(np.abs(y))
                axes[0].semilogx(x, mag_db, label=f"{label}: {name}")
            else:
                y_plot = y.real if is_complex else y
                plot_fn = axes[0].semilogx if log_x else axes[0].plot
                plot_fn(x, y_plot, label=f"{label}: {name}")

        if is_ac:
            axes[0].set_ylabel("Magnitude (dB)")
            axes[1].set_ylabel("Phase (deg)")
            axes[1].set_xlabel(x_name)
            axes[0].grid(True, which="both", alpha=0.3)
            axes[1].grid(True, which="both", alpha=0.3)
        else:
            axes[0].set_xlabel(x_name)
            axes[0].set_ylabel("Gain (dB)" if is_gain_db else "Value")
            axes[0].grid(True, which="both" if log_x else "major", alpha=0.3)

    for ax in axes:
        ax.legend(fontsize=8)
    fig.tight_layout()

    if out_path is None:
        out_path = os.path.splitext(paths[0])[0] + ".png"
    fig.savefig(out_path, dpi=150)
    print(f"Saved plot to {out_path}")

    if show:
        plt.show()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+", help="Simulation output file(s): ngspice ASCII raw (.out) or CSV")
    parser.add_argument("-o", "--out", default=None, help="Path to save the plot PNG (default: <first-file>.png)")
    parser.add_argument("--show", action="store_true", help="Also display the plot interactively")
    args = parser.parse_args()

    plot_files(args.files, out_path=args.out, show=args.show)


if __name__ == "__main__":
    main()
