#!/usr/bin/env python3
"""
Single accessor for the project's active PDK.

Every script that needs a process-specific fact -- a model name, a GDS layer
number, a tool path -- reads it from `.claude/reference/pdk_options.json`
through this module, so no process name is hardcoded anywhere else and
retargeting the flow is one edit to that file's "selected" key.

Usage:

    import sys, os
    sys.path.insert(0, os.path.join(REPO_ROOT, '.claude', 'reference'))
    from pdk_config import pdk

    pdk().model_prefix          # 'sky130_fd_pr__'
    pdk().routing_layers        # (67, 68, 69, 70, 71)
    pdk().device('nfet')        # 'sky130_fd_pr__nfet_01v8'
    pdk().tool('magicrc')       # path, $VARS expanded

Override for a one-off run without editing the file:

    PDK_OPTION=gf180mcuD python <script>.py ...

Pure stdlib, no imports beyond json/os, so it loads anywhere extraction does.
"""

import os
import json

__all__ = ['pdk', 'PDKConfig', 'PDKConfigError', 'OPTIONS_PATH']

# This file lives in <repo>/.claude/reference/, so the repo root is two up.
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
OPTIONS_PATH = os.path.join(_HERE, 'pdk_options.json')

# Environment override, for trying another process without editing the file.
ENV_OVERRIDE = 'PDK_OPTION'


class PDKConfigError(RuntimeError):
    """The PDK guideline is missing, malformed, or names an unknown PDK."""


class PDKConfig:
    """One PDK's entry from pdk_options.json, with typed accessors."""

    def __init__(self, name, data):
        self.name = name
        self._d = data

    # -- identity -----------------------------------------------------------
    @property
    def display_name(self):
        return self._d.get('display_name', self.name)

    @property
    def pdk_env(self):
        """Value for the PDK environment variable (magic/glayout read it)."""
        return self._d.get('pdk_env', self.name)

    @property
    def pdk_root(self):
        return _expand(self._d.get('pdk_root', ''))

    # -- netlist model names ------------------------------------------------
    @property
    def model_prefix(self):
        """'sky130_fd_pr__' -- this process's primitive-library prefix."""
        return self._d.get('model_prefix', '')

    @property
    def model_marker(self):
        """'_fd_pr__' -- the foundry-primitive marker shared across PDKs.

        Matching on this rather than the full prefix is what lets a parser
        accept any process's netlist; the prefix is for *emitting* names.
        """
        return self._d.get('model_marker', '_fd_pr__')

    def device(self, kind):
        """Model name for 'nfet' | 'pfet' | 'cap' | 'res'."""
        try:
            return self._d['devices'][kind]
        except KeyError:
            raise PDKConfigError(
                f"PDK '{self.name}' defines no '{kind}' device in "
                f"{OPTIONS_PATH}")

    # -- layout / GDS layers ------------------------------------------------
    @property
    def glayout_module(self):
        """Name of the glayout PDK module ('sky130', 'gf180')."""
        return self._d.get('glayout_module', '')

    @property
    def _layers(self):
        return self._d.get('layers', {})

    @property
    def layers_verified(self):
        """False marks an entry whose layer numbers are placeholders."""
        return self._layers.get('_verified', True)

    @property
    def routing_layers(self):
        return tuple(self._layers.get('routing_layers', ()))

    @property
    def drawing_datatype(self):
        return self._layers.get('drawing_datatype', 20)

    @property
    def label_datatype(self):
        return self._layers.get('label_datatype', 5)

    @property
    def resistor_marker_datatype(self):
        return self._layers.get('resistor_marker_datatype')

    @property
    def annotation_layer(self):
        """(layer, datatype) for the redraw's own annotation -- geometry that
        must never be mistaken for real drawn shapes."""
        return tuple(self._layers.get('annotation_layer', (236, 0)))

    @property
    def label_glayer(self):
        return self._layers.get('label_glayer')

    # -- process rules that are not per-glayer -----------------------------
    @property
    def latchup_max_tap_distance(self):
        """Furthest a diffusion may sit from a well/substrate tap, in um.

        sky130's LU.2/LU.3 ("N-diff distance to P-tap must be < 15.0um" and
        its p-diff/n-tap twin). glayout's `get_grule()` only carries per-glayer
        widths and spacings, so this rule has nowhere else to live; it is a
        process fact, so it lives here rather than at the one call site that
        needs it. None means this PDK has not had the rule confirmed, and a
        consumer must skip the check rather than invent a number."""
        return self._d.get('rules', {}).get('latchup_max_tap_distance_um')

    @property
    def via_links(self):
        """{(layer, datatype): (lower_metal, upper_metal)}.

        JSON keys are strings ('67/44') since JSON has no tuple keys; they are
        parsed back to tuples here so callers see one consistent shape.
        """
        out = {}
        for key, pair in self._layers.get('via_links', {}).items():
            lay, dt = key.split('/')
            out[(int(lay), int(dt))] = tuple(pair)
        return out

    # -- top thick-metal stack (spiral inductors) ---------------------------
    @property
    def inductor(self):
        """The `inductor` block: coil/underpass/via layers, DRC minimums and
        the physical constants a spiral generator needs, or None.

        This has to live here rather than come from glayout's `get_grule()`.
        sky130_mapped's glayer table shifts every metal name down one -- its
        "met5" resolves to GDS 71/20, the real met4 -- and never defines
        72/20 at all, so asking glayout for met5's rules silently returns
        met4's 0.3/0.4um against real minimums of 1.6/1.6um.
        """
        return self._d.get('inductor')

    def require_inductor(self, what='this operation'):
        """Return the inductor stack, or refuse if this PDK has none.

        A PDK entry may be complete for sizing and still have no confirmed
        thick-metal stack; geometry work must stop rather than borrow another
        process's layer numbers.
        """
        ind = self.inductor
        if not ind or not ind.get('_verified'):
            raise PDKConfigError(
                f"{what} needs a top thick-metal stack, but PDK "
                f"'{self.name}' defines no verified 'inductor' block in "
                f"{OPTIONS_PATH}. Add one (coil/underpass/via layers, "
                f"rules_um, physical) from the process's layer map and DRC "
                f"deck, set \"_verified\": true, and re-run.")
        return ind

    def require_layers(self, what='this operation'):
        """Raise unless the active PDK has usable layer numbers.

        A PDK entry can be added with its model names known and its layer map
        still unconfirmed. Geometry work must refuse that rather than silently
        extract nothing, which is what an empty routing stack would produce.
        """
        if not self.routing_layers or not self.layers_verified:
            raise PDKConfigError(
                f"{what} needs GDS layer numbers, but PDK '{self.name}' has "
                f"no verified layer map in {OPTIONS_PATH} "
                f"(routing_layers={list(self.routing_layers)}, "
                f"_verified={self.layers_verified}). Fill in that PDK's "
                f"'layers' block from its layer map, set \"_verified\": true, "
                f"and re-run.")

    # -- tool paths ---------------------------------------------------------
    def tool(self, key):
        """Tool path with $PDK_ROOT and $HOME expanded."""
        val = self._d.get('tools', {}).get(key)
        if val is None:
            return None
        return _expand(val.replace('$PDK_ROOT', self.pdk_root))

    @property
    def analysis_doc(self):
        """Relative path to this PDK's deep-dive notes, by the existing
        `pdk-specific/<pdk>_analysis.md` convention."""
        return self._d.get('analysis_doc')

    def __repr__(self):
        return f"<PDKConfig {self.name}>"


def _expand(path):
    return os.path.expandvars(os.path.expanduser(path)) if path else path


_cache = {}


def load_options(path=None):
    """Return the whole guideline file as a dict."""
    path = path or OPTIONS_PATH
    if not os.path.exists(path):
        raise PDKConfigError(
            f"No PDK guideline at {path}. This project selects its PDK there; "
            f"create it before running anything process-specific.")
    with open(path) as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise PDKConfigError(f"{path} is not valid JSON: {e}")


def pdk(name=None, path=None):
    """The active PDK: `name`, else $PDK_OPTION, else the file's "selected"."""
    key = (name, path)
    if key in _cache:
        return _cache[key]

    opts = load_options(path)
    pdks = opts.get('pdks') or {}
    chosen = name or os.environ.get(ENV_OVERRIDE) or opts.get('selected')
    if not chosen:
        raise PDKConfigError(
            f"{path or OPTIONS_PATH} names no 'selected' PDK.")
    if chosen not in pdks:
        raise PDKConfigError(
            f"PDK '{chosen}' is not in {path or OPTIONS_PATH}. "
            f"Available: {', '.join(sorted(pdks)) or '(none)'}. "
            f"Only a PDK defined there may be selected.")

    cfg = PDKConfig(chosen, pdks[chosen])
    _cache[key] = cfg
    return cfg


if __name__ == '__main__':
    p = pdk()
    print(f"selected PDK   : {p.name}  ({p.display_name})")
    print(f"model prefix   : {p.model_prefix}   marker: {p.model_marker}")
    print(f"glayout module : {p.glayout_module}")
    print(f"devices        : " + ', '.join(
        f"{k}={p.device(k)}" for k in ('nfet', 'pfet', 'cap', 'res')))
    print(f"routing layers : {list(p.routing_layers)} "
          f"(verified={p.layers_verified})")
    print(f"via links      : {p.via_links}")
    print(f"annotation     : {p.annotation_layer}   "
          f"res marker dt: {p.resistor_marker_datatype}")
    ind = p.inductor
    if ind:
        print(f"inductor stack : coil={tuple(ind['coil_layer'])} "
              f"underpass={tuple(ind['underpass_layer'])} "
              f"via={tuple(ind['via_layer'])}  "
              f"w/s>={ind['rules_um']['coil_min_width']}/"
              f"{ind['rules_um']['coil_min_space']}um")
    else:
        print("inductor stack : (none defined for this PDK)")
    for t in ('magicrc', 'ngspice_lib', 'netgen_setup', 'klayout_lyp'):
        print(f"  {t:<13}: {p.tool(t)}")
