# primitives manifest

- **current_mirror_XN5_XN4** (current_mirror, auto): devices=['XN5', 'XN4'], size=14.86x20.75, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/modules/current_mirror_XN5_XN4.gds
- **XN2** (single_nfet, auto): devices=['XN2'], size=9.64x66.28, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/primitives/XN2.gds
- **XN3** (single_nfet, auto): devices=['XN3'], size=9.64x66.28, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/primitives/XN3.gds
- **XC0** (single_cap, auto): devices=['XC0'], size=12.04x7.78, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/primitives/XC0.gds
- **XC1** (single_cap, auto): devices=['XC1'], size=12.04x7.78, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/primitives/XC1.gds
- **XN1** (single_nfet, auto): devices=['XN1'], size=9.1x26.27, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/primitives/XN1.gds
- **XN0** (single_nfet, auto): devices=['XN0'], size=9.1x26.27, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/primitives/XN0.gds
- **XL0** (inductor, manual): devices=['XL0'], size=196.0x213.22, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/modules/vco_spiral_ind.gds
- **XL1** (inductor, manual): devices=['XL1'], size=196.0x213.22, gds=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/modules/vco_spiral_ind.gds

## Manual composition needed (no dedicated glayout module)
- [cross_coupled_pair] ['XN0', 'XN1']: circuit_decomposition.yaml pattern cross_coupled_pair (confidence: certain) -- Structure test PASSES exactly: XN0.gate == XN1.drain (voutp) and
XN1.gate == XN0.drain (voutn), sources shared at vtail, same type
(both sky130_fd_pr__nfet_01v8). Presents -2/gm at the drains. -- see ../../circuit-decomposition/pattern-table.md
- [capacitor_bank] ['XC0', 'XC1']: circuit_decomposition.yaml pattern capacitor_bank (confidence: likely) -- Manual (the detector parses MOS/BJT only, so no C ever reaches it).
Structure test: two capacitors sharing one plate net (VDD) with their other
plates on distinct nodes (voutp / voutn). Test passes as written. -- see ../../circuit-decomposition/pattern-table.md
