* FLAT netlist for layout -- derived copy of
*   example/test_vco/device_shaping/vco_lc_final_shaped.sp
* The golden netlist is NOT modified.
*
* HAND-REPAIRED, and why (placer SKILL.md Step 1 permits a repair to the
* derived flat copy that equivalence still holds across):
*   flatten_netlist.py / subckt_macros.flatten() DROPPED both XL0 and XL1.
*   `netlist_devices.parse_devices()` recognises only MODEL-NAMED devices;
*   a value-form `L`/`R`/`C` line (`L1 A n1 3.9860n`) matches no regex, so
*   vco_spiral_ind's body parsed as ZERO devices and the two tank coils
*   vanished (printed as `[XL0] vco_spiral_ind: []`, 10 devices -> 8).
*
* The coils are kept here as UNEXPANDED subckt calls, which is also what the
* layout wants: the physical cell is ONE drawn spiral (src/cells/primitives/
* inductor.py), not four lumped elements, and Magic cannot extract an
* inductor at all -- the coil is black-boxed on both sides of LVS.
*
* .subckt order is deliberate: vco_lc FIRST, because flatten_netlist.py's
* top_subckt_name() takes the file's first .subckt line as the top.
.subckt vco_lc Vcont Iref VDD VSS voutp voutn
* XN0/XN1: cross-coupled NMOS pair (source N0/N1, Wn, l=45n)
XN0 voutn voutp vtail VSS sky130_fd_pr__nfet_01v8 l=0.15 w=20 nf=1 m=1
XN1 voutp voutn vtail VSS sky130_fd_pr__nfet_01v8 l=0.15 w=20 nf=1 m=1
* XN2/XN3: MOS varactors — drain and source both on Vcont, gate on the tank
* (source N2/N3, Wvar). These set TuningRange.
XN2 Vcont voutp Vcont VSS sky130_fd_pr__nfet_01v8 l=0.5 w=60 nf=1 m=1
XN3 Vcont voutn Vcont VSS sky130_fd_pr__nfet_01v8 l=0.5 w=60 nf=1 m=1
* XN4/XN5: tail current source + its diode-connected reference (source N4/N5,
* Wn6/Wn5). The W ratio sets the tail current, hence Power and OutputPower.
XN4 vtail Iref VSS VSS sky130_fd_pr__nfet_01v8 l=0.5 w=15 nf=1 m=4
XN5 Iref  Iref VSS VSS sky130_fd_pr__nfet_01v8 l=0.5 w=15 nf=1 m=1
* XC0/XC1: fixed MiM tank capacitance (source C0/C1, C1). 16x16 um ~ 512 fF.
XC0 VDD voutp sky130_fd_pr__cap_mim_m3_1 l=7.5 w=7.5 m=1
XC1 VDD voutn sky130_fd_pr__cap_mim_m3_1 l=7.5 w=7.5 m=1
* XL0/XL1: the two tank inductors (source L0/L1, L1)
XL0 VDD voutp vco_spiral_ind
XL1 VDD voutn vco_spiral_ind
.ends vco_lc

.subckt vco_spiral_ind A B
Cpa A 0 146.35f
L1  A n1 3.9860n
R1  n1 B 9.2250
Cpb B 0 146.35f
.ends vco_spiral_ind
