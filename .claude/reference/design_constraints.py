#!/usr/bin/env python3
"""
Single accessor for a design's run constraints -- `design_constraints.json`.

`design-sheets-intake` interviews the user once, at the end of intake, for the
high-level choices the downstream skills and agents would otherwise each stop
and ask for -- how many sizing iterations may be spent, how many layout fix
rounds, how long the placer anneals, how elongated the canvas may get -- and
writes them to

    <design_dir>/design_constraints.json

Every consumer reads them back through this module, so the questions are asked
in one place and an agent with no `AskUserQuestion` grant (`layout-agent`,
`layout-fixer`) can still get its value. `CONSTRAINT_KEYS` below is the
registry: one row per question, naming that key's kind, default and consumer.
A new constraint starts as a row there.

Values are counts (positive integers) or ratios (`16:9`, `16/9` or `1.78` --
stored as written, read back as a float; `--raw` prints the written form).

Read one value (the common case; exit 2 means "unresolved -- ask the user"):

    python .claude/reference/design_constraints.py <design_dir> \
        --key schematic_sizing_iterations

Print everything with where each value came from:

    python .claude/reference/design_constraints.py <design_dir>

Write the file (intake; also the way to amend one later -- merges, never
clobbers the keys it wasn't given):

    python .claude/reference/design_constraints.py <design_dir> --write \
        --set schematic_sizing_iterations=15 --set max_canvas_aspect=16:9

From Python:

    import sys, os
    sys.path.insert(0, os.path.join(REPO_ROOT, '.claude', 'reference'))
    from design_constraints import constraints

    c = constraints('designs/test_miller')
    c.get('layout_fix_iterations')      # 5      -- value, or the default
    c.get('max_canvas_aspect')          # 1.7777 -- ratios come back parsed
    c.get_raw('max_canvas_aspect')      # '16:9' -- ... or as written
    c.source('layout_fix_iterations')   # 'file' | 'default' | 'unresolved'

Pure stdlib, same as `pdk_config.py`.
"""

import os
import sys
import json
import argparse

__all__ = [
    'constraints', 'DesignConstraints', 'DesignConstraintsError', 'parse_ratio',
    'CONSTRAINT_KEYS', 'FILENAME', 'SCHEMA_VERSION', 'find_constraints_file',
]

FILENAME = 'design_constraints.json'
SCHEMA_VERSION = 1


class DesignConstraintsError(Exception):
    pass


# The registry of collectable constraints. One row per question intake asks.
#
#   kind     -- 'count' (a positive integer budget) or 'ratio' (an aspect
#               ratio, written either as a float or as `W:H` / `W/H`, which is
#               how a floorplan constraint is actually spoken).
#   default  -- what a consumer uses when the file is silent; None means there
#               is no safe default and the consumer must ask the user.
#   consumer -- who reads it, so a new key's owner is obvious from this file.
CONSTRAINT_KEYS = {
    'schematic_sizing_iterations': {
        'kind': 'count',
        'default': None,
        'consumer': 'schematic-sizing (Loop budget / N)',
        'help': 'ngspice tuning iterations schematic-sizing may spend in one run',
    },
    'layout_fix_iterations': {
        'kind': 'count',
        'default': 5,
        'consumer': 'layout-fixer (MAX_SUBRETRY, DRC and LVS independently)',
        'help': 'fix-and-recheck cycles layout-fixer may spend per gate',
    },
    'placer_anneal_iters': {
        'kind': 'count',
        'default': 20000,
        'consumer': 'placer Step 3 --iters (passed by layout-agent)',
        'help': 'simulated-annealing iteration ceiling for placement',
    },
    'max_canvas_aspect': {
        'kind': 'ratio',
        'default': '16:9',
        'consumer': 'placer Step 3 --max-aspect (the anneal aspect penalty)',
        'help': ('the placement bounding box\'s long side may not exceed this '
                 'ratio of its short side; orientation-free, so it constrains '
                 'a tall canvas exactly as it does a wide one'),
    },
}


def parse_ratio(value):
    """`16:9`, `16/9`, `1.78` -> 1.7777... Ratios are spoken as W:H far more
    often than as a decimal, and a floorplan constraint the user cannot read
    back in the form they gave it is one they will not trust."""
    if isinstance(value, bool):
        raise ValueError('a bool is not a ratio')
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    for sep in (':', '/'):
        if sep in text:
            w, _, h = text.partition(sep)
            w, h = float(w), float(h)
            if h == 0:
                raise ValueError('ratio %r divides by zero' % text)
            return w / h
    return float(text)


def _coerce(key, value):
    """Validate against the key's kind, and return it in STORAGE form -- the
    integer for a count, the text as written for a ratio (`get()` is what
    turns that into a float)."""
    kind = CONSTRAINT_KEYS.get(key, {}).get('kind', 'count')
    if kind == 'ratio':
        try:
            ratio = parse_ratio(value)
        except (TypeError, ValueError) as exc:
            raise DesignConstraintsError(
                '%s: expected a ratio like 16:9 or 1.78, got %r (%s)'
                % (key, value, exc))
        if ratio <= 0:
            raise DesignConstraintsError(
                '%s: must be > 0, got %r' % (key, value))
        if ratio < 1:
            raise DesignConstraintsError(
                '%s: %r is %.4f, i.e. long-side/short-side < 1, which no '
                'bounding box can satisfy -- write it long side first '
                '(9:16 means the same shape as 16:9)' % (key, value, ratio))
        return value if isinstance(value, str) else float(value)
    if isinstance(value, bool):
        raise DesignConstraintsError('%s: expected an integer, got a bool' % key)
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise DesignConstraintsError(
            '%s: expected an integer, got %r' % (key, value))
    if ivalue < 1:
        raise DesignConstraintsError(
            '%s: must be >= 1, got %d' % (key, ivalue))
    return ivalue


def find_constraints_file(design_dir):
    """The constraints file governing `design_dir`: its own, else the nearest
    one above it.

    The file lives at the DESIGN ROOT, but consumers are routinely handed a
    subdirectory -- `layout-agent`'s hand-off payload names `<design>/layout`,
    because that is where `routes.json` and `manifest.json` are. Anchoring the
    lookup strictly at the given directory made that return the registry
    DEFAULT with exit 0: a silently wrong budget, no error, no warning. A run
    asked for 20 layout-fix iterations and any consumer reading from `layout/`
    would have used 5.

    Walking up stops at the repo root (a directory holding `.git`) so a design
    can never pick up constraints from outside its own checkout, and at the
    filesystem root regardless. Returns `(path, found_in)`; `found_in` is None
    when nothing was found, and the returned path is then the one the caller
    named, so writing still creates the file where it was asked to.
    """
    design_dir = os.path.abspath(design_dir)
    here = design_dir
    while True:
        candidate = os.path.join(here, FILENAME)
        if os.path.isfile(candidate):
            return candidate, here
        if os.path.isdir(os.path.join(here, '.git')):
            break                      # repo root -- do not look outside it
        parent = os.path.dirname(here)
        if parent == here:
            break                      # filesystem root
        here = parent
    return os.path.join(design_dir, FILENAME), None


class DesignConstraints(object):
    def __init__(self, design_dir):
        self.design_dir = os.path.abspath(design_dir)
        self.path, self.found_in = find_constraints_file(self.design_dir)
        self.exists = os.path.isfile(self.path)
        self.data = {}
        self._values = {}
        if self.exists:
            try:
                with open(self.path) as fh:
                    self.data = json.load(fh)
            except ValueError as exc:
                raise DesignConstraintsError(
                    '%s is not valid JSON: %s' % (self.path, exc))
            # "constraints" is the block; "budgets" was its first name and is
            # still read, so a design filed before the rename keeps working.
            values = dict(self.data.get('budgets', {}))
            values.update(self.data.get('constraints', {}))
            if not isinstance(values, dict):
                raise DesignConstraintsError(
                    '%s: "constraints" must be an object' % self.path)
            for key, value in values.items():
                if value is None:
                    continue
                self._values[key] = _coerce(key, value)

    # -- reading ---------------------------------------------------------
    def source(self, key):
        if key in self._values:
            return 'file'
        spec = CONSTRAINT_KEYS.get(key, {})
        return 'default' if spec.get('default') is not None else 'unresolved'

    def get_raw(self, key, default=None):
        """The value as WRITTEN: the file's, else the registry default, else
        `default`. `16:9` comes back as `'16:9'` -- what to print back at the
        user. Returns None when nothing resolves it -- ask the user."""
        if key in self._values:
            return self._values[key]
        registry_default = CONSTRAINT_KEYS.get(key, {}).get('default')
        if registry_default is not None:
            return registry_default
        return default

    def get(self, key, default=None):
        """The value in force, in the form a consumer computes with: an int
        for a count, a float for a ratio (`16:9` -> 1.7778)."""
        value = self.get_raw(key, default)
        if value is None:
            return None
        if CONSTRAINT_KEYS.get(key, {}).get('kind') == 'ratio':
            return parse_ratio(value)
        return value

    def require(self, key):
        value = self.get(key)
        if value is None:
            raise DesignConstraintsError(
                '%s is unresolved: %s has no "%s" and it has no default -- '
                'ask the user, then record it with --write --set %s=<value>'
                % (key, self.path if self.exists else FILENAME + ' (absent)',
                   key, key))
        return value

    def resolved(self):
        return dict((k, self.get(k)) for k in CONSTRAINT_KEYS)

    # -- writing ---------------------------------------------------------
    def write(self, updates, project_name=None):
        """Merge `updates` into the file and save it. Keys not named keep
        whatever they had; nothing outside "constraints" is disturbed."""
        if not os.path.isdir(self.design_dir):
            raise DesignConstraintsError(
                'design dir does not exist: %s' % self.design_dir)
        # Writes land on the file this dir already READS, so amending from a
        # subdirectory cannot leave a second file that shadows the real one.
        for key in updates:
            if key not in CONSTRAINT_KEYS:
                raise DesignConstraintsError(
                    'unknown constraint %r -- add it to CONSTRAINT_KEYS in %s '
                    'first, so its consumer and default are recorded'
                    % (key, os.path.basename(__file__)))
        out = dict(self.data)
        out['schema_version'] = SCHEMA_VERSION
        if project_name:
            out['project_name'] = project_name
        elif 'project_name' not in out:
            out['project_name'] = os.path.basename(self.design_dir)
        values = dict(out.pop('budgets', {}))
        values.update(out.get('constraints', {}))
        for key, value in updates.items():
            values[key] = _coerce(key, value)
        out['constraints'] = values
        with open(self.path, 'w') as fh:
            json.dump(out, fh, indent=2)
            fh.write('\n')
        self.data = out
        self._values = dict((k, v) for k, v in values.items() if v is not None)
        self.exists = True
        return self.path


def constraints(design_dir):
    return DesignConstraints(design_dir)


def _display(c, key):
    raw = c.get_raw(key)
    if raw is None:
        return '--'
    if CONSTRAINT_KEYS.get(key, {}).get('kind') == 'ratio':
        return '%s (%.3f)' % (raw, c.get(key))
    return str(raw)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Read or write a design\'s design_constraints.json.')
    ap.add_argument('design_dir', help='<design_root>/<project_name>/')
    ap.add_argument('--key', help='print one value; exit 2 if unresolved')
    ap.add_argument('--raw', action='store_true',
                    help='with --key: print a ratio as written (16:9) rather '
                         'than parsed (1.7778)')
    ap.add_argument('--write', action='store_true',
                    help='write/merge the values given with --set')
    ap.add_argument('--set', action='append', default=[], metavar='KEY=VALUE',
                    help='a constraint to write (repeatable)')
    ap.add_argument('--project-name', help='recorded in the file on --write')
    args = ap.parse_args(argv)

    try:
        c = constraints(args.design_dir)
    except DesignConstraintsError as exc:
        print('ERROR: %s' % exc)
        return 1

    if args.write:
        updates = {}
        for item in args.set:
            if '=' not in item:
                print('ERROR: --set expects KEY=VALUE, got %r' % item)
                return 1
            key, _, value = item.partition('=')
            updates[key.strip()] = value.strip()
        try:
            path = c.write(updates, project_name=args.project_name)
        except DesignConstraintsError as exc:
            print('ERROR: %s' % exc)
            return 1
        print('WROTE %s' % path)
        for key in CONSTRAINT_KEYS:
            print('  %-30s %-14s (%s)'
                  % (key, _display(c, key), c.source(key)))
        return 0

    if args.key:
        try:
            c.require(args.key)          # raises when unresolved
        except DesignConstraintsError as exc:
            print('UNRESOLVED: %s' % exc, file=sys.stderr)
            return 2
        print(c.get_raw(args.key) if args.raw else c.get(args.key))
        return 0

    print('DESIGN CONSTRAINTS -- %s' % c.design_dir)
    if c.found_in and os.path.abspath(c.found_in) != c.design_dir:
        print('  (constraints found at the design root above it: %s)'
              % c.found_in)
    print('  file: %s' % (c.path if c.exists else '%s  (ABSENT)' % c.path))
    for key, spec in CONSTRAINT_KEYS.items():
        print('  %-30s %-14s %-11s %s'
              % (key, _display(c, key), '(%s)' % c.source(key),
                 spec['consumer']))
    unresolved = [k for k in CONSTRAINT_KEYS if c.source(k) == 'unresolved']
    if unresolved:
        print('\nUNRESOLVED (no default -- ask the user): %s'
              % ', '.join(unresolved))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
