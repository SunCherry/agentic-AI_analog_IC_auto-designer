# `target_spec.json` — required form

**This is the normative shape of a design's `spec/target_spec.json`.**
`design-sheets-checker`'s Step 3 validates every spec file against it and
returns `INCOMPLETE` when a file does not conform, naming the exact key
and the exact correction. The worked reference on disk is
`example/test_miller_ota/spec/target_spec.json`.

A target spec states **four things per spec**, and a spec that states
fewer cannot be checked: **what the spec is** (the key), **which way it
has to go** (`Direction`), **the number** (`Value`), and **what that
number is written in** (`Units`).

## The form

```json
{
  "<SpecName>": {
    "Direction": "FLOOR" | "CEILING" | "RANGE",
    "Value": <number>  |  [<min>, <max>],
    "Units": "<unit string>"
  }
}
```

Every key of the top-level object is one spec. Every value is an object
carrying exactly the three fields `Direction`, `Value`, `Units` — spelled
and capitalized that way.

## Worked example

```json
{
  "PM": {
    "Direction": "RANGE",
    "Value": [50, 70],
    "Units": "degree"
  },
  "UGBW": {
    "Direction": "FLOOR",
    "Value": 15,
    "Units": "MHz"
  },
  "Gain": {
    "Direction": "FLOOR",
    "Value": 40,
    "Units": "dB"
  },
  "Power": {
    "Direction": "CEILING",
    "Value": 0.5813,
    "Units": "mW"
  }
}
```

## `Direction` — the three values, and what each means

| `Direction` | Met when | Stricter means | Typical of |
|---|---|---|---|
| `FLOOR` | `sim >= Value` | a **higher** `Value` | Gain, UGBW, SNR, slew rate |
| `CEILING` | `sim <= Value` | a **lower** `Value` | Power, area, noise, offset, THD |
| `RANGE` | `Value[0] <= sim <= Value[1]` | a **narrower** interval | PM, common-mode level, duty cycle |

`Direction` is spelled in **upper case**, exactly one of those three
strings. It is per-key: no spec key has a direction this project assumes
on its own, and none is inferred from the key's name.

**Direction is a property of the spec, not of the number's sign.** A
phase-noise target of `-100` dBc/Hz is a `CEILING` — more negative is
better — even though the number is negative and "smaller" reads
backwards at a glance. Write the `Direction` the requirement actually
has; the sign of `Value` never decides it.

## `Value`

- **`FLOOR` / `CEILING`** — a single JSON number (int or float).
  Negatives are fine. `true`/`false` are not numbers.
- **`RANGE`** — a JSON array of **exactly two** numbers, `[min, max]`,
  with `min < max`. JSON has no tuple type, so a Python `(50, 70)` is
  written `[50, 70]`.
- Never a string. `"15"` is not `15`, and `"15MHz"` puts the unit in the
  wrong field.

## `Units`

A non-empty string naming the unit **the `Value` is written in** — not
the unit the simulator reports. This field exists because those two
routinely differ, and the mismatch produces a plausible number instead
of an error: ngspice reports Hz, W, A in SI base units, while spec files
are written in MHz, mW, mA. Every downstream comparison converts using
this field, so an absent or wrong `Units` silently corrupts every
verdict made against that key.

Use `"-"` for a genuinely dimensionless spec (a ratio, a count).

This project's standing conventions, checked as INFO where the key is
recognized (`.claude/reference/metrics.md`):

| Key | Units |
|---|---|
| `Gain` | `dB` |
| `UGBW` | `MHz` (**not** Hz — `15` means 15 MHz) |
| `PM` | `degree` |
| `Power` | `mW` |

A key outside that table is accepted with whatever unit it declares —
**any key set is accepted**, since the circuit class decides which specs
exist. Only the *shape* is mandatory.

## What a conforming file must satisfy

1. Valid JSON, a non-empty object at the top level.
2. Every value is an object — **not a bare number**. The older flat form
   (`{"Gain": 40}`) does not conform: it declares no direction and no
   unit, which is precisely what this form exists to fix.
3. Every entry has all three of `Direction`, `Value`, `Units`, and no
   entry is missing one.
4. `Direction` ∈ {`FLOOR`, `CEILING`, `RANGE`}, upper case.
5. `Value` matches the `Direction`: scalar for `FLOOR`/`CEILING`,
   two-element ascending array for `RANGE`.
6. `Units` is a non-empty string.

Anything else is reported per key with the correction, and the file goes
back to the user to complete or correct. Nothing in this flow invents a
`Value`, a `Direction` or a `Units` the user did not state — a target
nobody set is a requirement every later iteration reports against as
though they had.

## Checking a file against this template

```
python .claude/skills/design-sheets-checker/script/check_design_sheets.py <design_dir>
```

The `TARGET SPEC` section lists every violation, keyed by spec name. It
is also callable on its own:

```
python -c "import sys; sys.path.insert(0,'.claude/skills/design-sheets-checker/script'); \
from check_design_sheets import check_target_spec; print(check_target_spec('<design_dir>'))"
```
