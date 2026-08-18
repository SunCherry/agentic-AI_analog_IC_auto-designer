# Post-Layout Fidelity Metric

Use `compute_fidelity.py` in this directory for every calculation below —
never hand-compute or eyeball these numbers.

## Extracted quantities

From a pre-layout AC sweep (ideal schematic) and a post-layout AC sweep
(PEX-extracted, same testbench/stimulus), extract for each:

- **DC gain** (dB) — magnitude at the lowest simulated frequency.
- **UGB** (Hz) — unity-gain crossing frequency (log-interpolated).
- **Phase margin** (deg) — `180 + phase(at UGB)`, phase referenced to 0°
  at DC (matches the convention used throughout this repo's prior AC
  analyses).

## Combined scalar error

```
E = |ΔGain_dB| / GAIN_TOL
  + |UGB_post - UGB_pre| / UGB_pre / UGB_REL_TOL
  + |ΔPM_deg| / PM_TOL
```

Default tolerances (starting points — override per-design if the user
gives different spec margins):

| term | default tolerance | meaning |
|---|---|---|
| `GAIN_TOL` | 1.0 dB | acceptable DC gain drift from parasitics |
| `UGB_REL_TOL` | 0.15 | acceptable *relative* UGB degradation (15%) |
| `PM_TOL` | 5.0 deg | acceptable phase margin drift |

**`TOLERANCE` (overall pass/fail threshold) = 1.0.** `E <= 1.0` roughly
means every individual term is within its own tolerance simultaneously
(their sum is bounded by 1); it is a soft combined signal, not a strict
per-term gate — always report the per-term deltas alongside `E`, since a
single blown term (e.g. UGB) can hide inside a passing sum if the other
terms are near zero. Prefer flagging `E <= 1.0` AND no single term
individually exceeds ~1.3x its share as "clean" success; note in the
report if that stricter check fails even though `E <= 1.0`.

## Required report table

Print this table every time verify-agent completes an iteration, even
if the result is worse than the previous best:

```
Iteration <i> — post-layout fidelity report
| metric      | pre-layout | post-layout | delta        | tolerance | within tol? |
|--------------|-----------|-------------|---------------|-----------|-------------|
| DC gain (dB) | ...       | ...         | ...           | 1.0 dB    | yes/no      |
| UGB (MHz)    | ...       | ...         | ... (...%)    | 15%       | yes/no      |
| PM (deg)     | ...       | ...         | ...           | 5.0 deg   | yes/no      |
| E (combined) | -         | -           | ...           | <= 1.0    | yes/no      |
```

**`UGB` here is the same quantity `target_spec.json` calls `UGBW`** -- this
table is the post-layout fidelity report, whose rows are metric labels, while
a spec file's keys are spec names. The short form is used throughout the
post-layout side (`verify-agent`, `pre-layout-extrapolation`); the long
one
throughout the spec side. Same measurement, and `compute_fidelity.py`'s
`ac_metrics()` produces it for both.
