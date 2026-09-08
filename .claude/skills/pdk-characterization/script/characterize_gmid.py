#!/usr/bin/env python3
"""PDK_CONFIGS -- per-process facts the op-probe generator needs.

RESTORED 2026-08-26 by schematic-agent. The `pdk-characterization` skill folder
was absent from this checkout while
`schematic-sizing/script/generate_op_probe.py` imports `PDK_CONFIGS` from it,
which made the op-probe generator -- and therefore
`run_sizing_iteration.py`, the whole sizing loop -- unimportable.

Only the ONE key any surviving consumer actually reads is restored:
`mos_instance_name(model)`, the name of the intrinsic MOS device *inside* the
PDK's subcircuit-wrapped FET, used to build the ngspice save path
`@m.<hier>.<instance>.<mos_instance_name>[gm]`.

This is NOT guessed. It was verified empirically on this machine against
sky130A before being written down, by probing the real design under ngspice
45.2:

    let g1 = @m.xthree_stage_amp.x0.xm1.msky130_fd_pr__pfet_01v8[gm]
    print g1   ->  g1 = 2.311142e-05          (resolves; no "unknown" error)

i.e. sky130's subckt-wrapped FETs name their internal primitive `m` + the
model name. gf180mcu follows the same open_pdks convention, but it was NOT
verified here (no gf180 install on this machine), so it is marked unverified
rather than presented as fact -- the loud way to fail if this file is ever
used on that process.

Anything else the original module held -- gm/Id characterization sweeps and
their lookup tables -- is measured process data and is deliberately absent
rather than reconstructed from memory.
"""


def _sky130_mos_instance_name(model):
    """sky130A: `sky130_fd_pr__nfet_01v8` -> `msky130_fd_pr__nfet_01v8`."""
    return "m" + model.strip().lower()


def _gf180_mos_instance_name(model):
    """gf180mcuD: same open_pdks convention -- UNVERIFIED on this machine."""
    return "m" + model.strip().lower()


PDK_CONFIGS = {
    "sky130A": {
        "mos_instance_name": _sky130_mos_instance_name,
        "nfet": "sky130_fd_pr__nfet_01v8",
        "pfet": "sky130_fd_pr__pfet_01v8",
        "verified": True,
    },
    "gf180mcuD": {
        "mos_instance_name": _gf180_mos_instance_name,
        "nfet": "gf180mcu_fd_pr__nfet_03v3",
        "pfet": "gf180mcu_fd_pr__pfet_03v3",
        "verified": False,
    },
}
