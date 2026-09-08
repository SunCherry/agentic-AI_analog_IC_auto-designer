#!/usr/bin/env python3
"""The annotate-and-clamp-retry loop: simulate one netlist at one finger count,
with estimated parasitics on every MOS.

RESTORED 2026-08-30. This module was imported by
`device-shaper/script/sweep_fingers.py` (four symbols) and named by
`device-shaper/script/select_shape_devices.py` (a fifth), but the
`pre-layout-extrapolation` skill folder was absent from this checkout and had
never been tracked by git -- so `sweep_fingers.py` died with
`ModuleNotFoundError: No module named 'run_extrapolation'` at import time and
could not run at all, at any argument. Same failure mode, and the same cause,
as the one `parasitic-estimation/script/estimate_parasitics.py` records in its
own restore note.

WHAT THIS RECONSTRUCTION IS BUILT FROM -- not guesswork:

  1. The call site in `sweep_fingers.check_nf()` fixes every signature and the
     4-tuple `run_nf_variant()` returns.
  2. `device-shaper/reference.md` names this module's job exactly: "the
     annotate-and-clamp-retry loop", one of four shared helpers, and defines
     `annotated` as "that same netlist re-simulated with estimated parasitics
     on every device, at one finger count".
  3. `select_shape_devices.py`'s docstring states `find_offending_device()`'s
     method: recover a device name out of ngspice's error text by matching
     against real MOS instance names in the netlist.
  4. **A complete set of surviving artifacts from a past successful run** --
     `example/test_miller_ota_8_18/device_shaping/sweep/nf_{1,2,4,8}/` -- which
     contain the annotated netlists, the variant testbenches and the sweep's
     `result.json`. Every formula below was derived from those files and then
     checked to reproduce them exactly: 8 devices x 2 finger counts, all six
     annotation parameters plus the gate resistor, to the last printed digit.

THE ANNOTATION, as recovered (all lengths in the netlist's own micron
convention -- see `estimate_parasitics._um_val` for why bare numbers here are
microns):

    w_f = w * m / nf                 width of one finger, summed over m copies
    n_d = (nf + 1) // 2              drain diffusions in an S-D-S-D-...-S row
    n_s = nf // 2 + 1                source diffusions in that same row
    ad  = n_d * w_f * ldiff          as = n_s * w_f * ldiff
    pd  = n_d * 2 * ldiff            ps = n_s * 2 * ldiff
    nrd = ldiff / (w_f * n_d)        nrs = ldiff / (w_f * n_s)
    Rg  = rg_coeff * w * m / (l * nf**2)

`nf = 1` falls out of the same formulas (n_d = n_s = 1), which is why there is
no special case. Odd `nf > 1` is the one shape the surviving artifacts do not
witness -- they sweep powers of two -- so `n_d`/`n_s` there follow the standard
alternating-diffusion count rather than a measurement.

The gate resistor is inserted as a real element: device `X`'s gate net `G`
becomes `G_rg_X`, and `RG_X G G_rg_X <value>` is emitted on the line
immediately before that device.

PROCESS NUMBERS COME FROM THE TABLE, NEVER FROM HERE. Every coefficient is
read out of `PDK_TABLES[pdk]` in
`../../parasitic-estimation/script/estimate_parasitics.py`, by these keys:

    ldiff_default_um   diffusion length (the key name `sweep_fingers.py`
                       already uses)
    rg_coeff           gate-resistance coefficient in the Rg formula above

That table is EMPTY in this checkout, on purpose (see its own docstring: the
numbers are measured process values and inventing them would produce
plausible, wrong nf decisions). So `sweep_fingers.check_nf()` still takes its
"no parasitic coefficient table for pdk=..." branch and reports the nf as never
measured -- restoring this module does not change that, and deliberately does
not try to. What it changes is that the script now IMPORTS, so `--help`, the
honest no-table refusal, and every other path work instead of dying at line 104.

For the record, and so nobody has to re-derive them: the surviving sky130A run
used `ldiff_default_um = 0.79` and `rg_coeff = 1/30` (0.03333..., readable as a
gate sheet resistance of 0.1 ohm/square divided by 3). Both are recoverable to
full precision from the artifacts named above -- they are over-determined by
16+ independent equations each -- but putting them in the table is a decision
about measured physics, so it is left to a human rather than taken here.
"""
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "..", "parasitic-estimation", "script"))

from estimate_parasitics import _MOS_RE, PDK_TABLES, _um_val  # noqa: E402

__all__ = [
    "MIN_CLAMP_RETRIES", "mos_instance_names", "make_variant_testbench",
    "run_nf_variant", "find_offending_device",
]

# Floor on the clamp-retry budget. `sweep_fingers.check_nf()` uses it as
# `max(MIN_CLAMP_RETRIES, 3 * len(mos_names))`, i.e. this only binds on a very
# small circuit, where 3-per-device would otherwise allow almost no retries.
MIN_CLAMP_RETRIES = 3

# ngspice runtime ceiling for one variant, seconds. An AC sweep of a few tens
# of devices is seconds; a hang is a broken deck, not a slow one.
NGSPICE_TIMEOUT_S = 300

_PARAM_RE = re.compile(r"\b(?P<key>[a-zA-Z_]+)\s*=\s*(?P<val>[^\s=]+)")
_NF_TOKEN_RE = re.compile(r"\s*\bnf\s*=\s*[^\s=]+", re.IGNORECASE)
_ANNOT_KEYS = ("ad", "as", "pd", "ps", "nrd", "nrs")


def _params(param_text):
    """`l=0.3 w=28.8 nf=1 m=1` -> {'l': '0.3', 'w': '28.8', ...}."""
    return {m.group("key").lower(): m.group("val")
            for m in _PARAM_RE.finditer(param_text or "")}


def _num(params, key, default=None):
    raw = params.get(key)
    if raw is None:
        return default
    v = _um_val(raw)
    return default if v is None else v


def _fmt(v):
    """Match the surviving artifacts' number format exactly: %g, 6 sig figs
    (`22.752`, `0.0274306`, `71.1111`)."""
    return f"{v:g}"


def mos_instance_names(netlist_path):
    """Every MOS instance name in the netlist, in file order.

    Uses the same `_MOS_RE.match(line.strip())` idiom as every other
    device-shaper script, so "what counts as a MOS line" has one definition
    across the skill rather than a private copy here.
    """
    names = []
    with open(netlist_path) as f:
        for line in f:
            m = _MOS_RE.match(line.strip())
            if m:
                names.append(m.group("name"))
    return names


def find_offending_device(text, mos_names):
    """Recover the device ngspice is complaining about from its error text.

    The method is the one `select_shape_devices.py`'s docstring attributes to
    this function: tokenize the message and keep only tokens that match a REAL
    MOS instance name. ngspice spells instance names inconsistently across
    messages (bare `XMN1`, lowercased, or prefixed as `xmn1.msky130_...`), so
    matching is case-insensitive and against a token's leading identifier --
    but a token that is not a real instance can never be returned, which is the
    property that matters: a wrong clamp is worse than no clamp.

    Returns the name as spelled in the NETLIST, or None.
    """
    if not text:
        return None
    by_lower = {n.lower(): n for n in mos_names}
    for token in re.findall(r"[A-Za-z_][\w.]*", text):
        head = token.split(".")[0].lower()
        if head in by_lower:
            return by_lower[head]
    return None


def annotate_netlist_text(text, per_device_nf, coeffs, ldiff_um=None):
    """Return (annotated_text, n_annotated).

    For every MOS line: set `nf` to this device's chosen count, append the six
    geometry parameters, rename the gate net to `<gate>_rg_<device>` and append
    the matching `RG_<device>` resistor. Non-MOS lines pass through untouched
    -- a MiM cap or a poly resistor has no finger count and no junction to
    estimate, and rewriting them would be inventing geometry.

    `ldiff_um` overrides the table's `ldiff_default_um` (that is exactly what
    `sweep_fingers`' `--ldiff` is for); every other coefficient comes from
    `coeffs`.
    """
    ldiff = ldiff_um if ldiff_um is not None else coeffs.get("ldiff_default_um")
    if ldiff is None:
        raise KeyError(
            "the PDK table has no 'ldiff_default_um' -- that is the diffusion "
            "length every ad/as/pd/ps/nrd/nrs value is built from, and there "
            "is no defensible default for it")
    if "rg_coeff" not in coeffs:
        raise KeyError(
            "the PDK table has no 'rg_coeff' -- that is the gate-resistance "
            "coefficient in Rg = rg_coeff * w * m / (l * nf^2). Add it to "
            "PDK_TABLES rather than letting this fall back to a made-up number")
    rg_coeff = coeffs["rg_coeff"]

    out, n = [], 0
    for line in text.splitlines(True):
        m = _MOS_RE.match(line.strip())
        if not m:
            out.append(line)
            continue
        name = m.group("name")
        params = _params(m.group("params"))
        w = _num(params, "w")
        l = _num(params, "l")
        if w is None or l is None or w <= 0 or l <= 0:
            # No drawn geometry to estimate from. Pass it through rather than
            # annotate a device whose size we cannot read.
            out.append(line)
            continue
        mult = int(_num(params, "m", 1) or 1)
        nf = int(per_device_nf.get(name, int(_num(params, "nf", 1) or 1)))
        nf = max(1, nf)

        w_f = w * mult / nf
        n_d = (nf + 1) // 2
        n_s = nf // 2 + 1
        ad = n_d * w_f * ldiff
        a_s = n_s * w_f * ldiff
        pd = n_d * 2.0 * ldiff
        ps = n_s * 2.0 * ldiff
        nrd = ldiff / (w_f * n_d)
        nrs = ldiff / (w_f * n_s)
        rg = rg_coeff * w * mult / (l * nf * nf)

        gate = m.group("g")
        gate_rg = f"{gate}_rg_{name}"
        # The resistor is emitted IMMEDIATELY BEFORE its device, not collected
        # into a block at the end -- that is the order the surviving artifacts
        # have, and it keeps each device and its gate resistor readable as one
        # unit (and inside whatever `.subckt` the device sits in, for free).
        out.append(f"RG_{name} {gate} {gate_rg} {_fmt(rg)}\n")

        # Rebuild the device line: same tokens, gate swapped, `nf` moved to the
        # end (drop it where it sat, re-append it), then the annotation. That
        # ordering is not cosmetic -- it is what the surviving artifacts show.
        head = (f"{name} {m.group('d')} {gate_rg} {m.group('s')} "
                f"{m.group('b')} {m.group('model')}")
        rest = _NF_TOKEN_RE.sub("", m.group("params") or "").rstrip()
        annot = (f" nf={nf} ad={_fmt(ad)} as={_fmt(a_s)} pd={_fmt(pd)} "
                 f"ps={_fmt(ps)} nrd={_fmt(nrd)} nrs={_fmt(nrs)}")
        newline = line[len(line.rstrip("\r\n")):]
        out.append(head + rest + annot + newline)
        n += 1

    return "".join(out), n


def make_variant_testbench(base_tb_text, netlist_basename, out_filename=None):
    """Point a copy of the deck at the variant netlist, and at its own output.

    Two substitutions, and only two -- exactly the diff the surviving variant
    decks show against the design's own testbench:

      1. the `.include` of the netlist -> the variant's bare basename. It is
         rendered as a sibling of the deck in the variant directory and run
         with that directory as cwd, so any path component (`../netlist/...`)
         no longer resolves. Same reasoning, and same restriction to the ONE
         include naming this netlist, as
         `run_sizing_iteration.repoint_netlist_include()`.
      2. the `write <file>.out v(<node>)` target -> `simout_<stem>.out`, so
         every finger count writes its own result instead of overwriting the
         previous one in place.

    A PDK `.lib` line, or an include of anything else, is left as written.

    Returns (testbench_text, out_filename).
    """
    stem = os.path.splitext(netlist_basename)[0]
    out_filename = out_filename or f"simout_{stem}.out"

    def _sub_include(m):
        lead, q1, path, q2 = m.groups()
        if not path.lower().endswith((".sp", ".spice", ".cir")):
            return m.group(0)
        return f"{lead}{q1}{netlist_basename}{q2}"

    text = re.sub(r'^(\s*\.include\s+)(["\']?)([^"\'\s]*)(["\']?)\s*$',
                  _sub_include, base_tb_text, flags=re.MULTILINE | re.IGNORECASE)

    text, n = re.subn(r'^(\s*write\s+)(\S*?)([^/\\\s]+\.out)(\s+v\(\S+\))',
                      lambda m: f"{m.group(1)}{m.group(2)}{out_filename}{m.group(4)}",
                      text, count=1, flags=re.MULTILINE | re.IGNORECASE)
    if n == 0:
        raise ValueError(
            "no 'write <file>.out v(<node>)' line found in the testbench -- "
            "this deck writes no AC result for ac_metrics() to read")
    return text, out_filename


def run_nf_variant(netlist_path, base_tb_text, work_dir, design_name, nf,
                   mos_names, pdk, ldiff_um, max_retries, base_overrides=None):
    """Annotate at finger count `nf`, simulate, and clamp-retry on failure.

    Returns `(out_path, per_device_nf, overrides, n_attempts)` -- the 4-tuple
    `sweep_fingers.check_nf()` unpacks. `out_path` is None when every attempt
    failed, which is the signal check_nf() already handles ("could not simulate
    at Nf=... even after N clamp-retry attempt(s)").

    `base_overrides` is `pinned_nf`: devices that keep the finger count the
    netlist already carries instead of being swept. `overrides` comes back
    carrying only the CLAMPS this call had to apply -- devices ngspice refused
    at `nf`, walked back toward 1 -- which is what the sweep records as
    `clamped`, distinct from `pinned`.

    Each attempt writes into `work_dir` itself -- the caller has already made
    that per-finger-count (`<base>/nf_<n>/`), so a re-run overwrites its own
    variant rather than accumulating directories.
    """
    coeffs = PDK_TABLES.get(pdk)
    if coeffs is None:
        raise KeyError(
            f"no parasitic coefficient table for pdk={pdk!r} -- available: "
            f"{sorted(PDK_TABLES)}. sweep_fingers.check_nf() screens for this "
            f"before calling here, so reaching this line means a caller "
            f"skipped that check")

    # Write DIRECTLY into the work_dir handed in -- do not append a per-nf
    # folder here. `sweep_fingers.sweep_nf()` already passes
    # `os.path.join(base_work_dir, f"nf_{nf}")`, and its own --save-artifacts
    # help documents the result as `<work_dir>/nf_<n>/`. Adding another level
    # here produced `.../nf_1/sweep/nf_1/`.
    var_dir = work_dir
    os.makedirs(var_dir, exist_ok=True)
    stem = f"{design_name}_nf{nf}"
    netlist_basename = f"{stem}.sp"
    base_text = open(netlist_path).read()

    overrides = dict(base_overrides or {})
    clamps = {}
    n_attempts = 0
    last_err = ""

    while n_attempts < max(1, int(max_retries)):
        n_attempts += 1
        per_device_nf = {}
        for name in mos_names:
            per_device_nf[name] = int(clamps.get(name, overrides.get(name, nf)))

        annotated, _ = annotate_netlist_text(base_text, per_device_nf, coeffs,
                                             ldiff_um=ldiff_um)
        with open(os.path.join(var_dir, netlist_basename), "w") as f:
            f.write(annotated)

        tb_text, out_filename = make_variant_testbench(base_tb_text, netlist_basename)
        tb_basename = f"tb_{stem}.spice"
        with open(os.path.join(var_dir, tb_basename), "w") as f:
            f.write(tb_text)

        out_path = os.path.join(var_dir, out_filename)
        if os.path.isfile(out_path):
            os.remove(out_path)     # never read a previous attempt's result

        try:
            proc = subprocess.run(["ngspice", "-b", tb_basename], cwd=var_dir,
                                  capture_output=True, text=True,
                                  timeout=NGSPICE_TIMEOUT_S)
            last_err = (proc.stderr or "") + "\n" + (proc.stdout or "")
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            # A timeout or a missing ngspice is not something a clamp fixes.
            return None, {}, clamps, n_attempts if isinstance(
                exc, subprocess.TimeoutExpired) else n_attempts

        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return out_path, per_device_nf, clamps, n_attempts

        # Failed. If ngspice named a device, halve its finger count and retry;
        # a device already at nf=1 cannot be clamped further, so stop rather
        # than spin.
        culprit = find_offending_device(last_err, mos_names)
        if culprit is None:
            break
        current = int(clamps.get(culprit, overrides.get(culprit, nf)))
        if current <= 1:
            break
        clamps[culprit] = max(1, current // 2)

    return None, {}, clamps, n_attempts
