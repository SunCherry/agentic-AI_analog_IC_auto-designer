* Cross-coupled LC VCO — converted from the Cadence/Spectre design sheets in
* example/training_example/VCO/first/ (spice_netlist/netlist + oceanScript.ocn).
*
* Topology (unchanged from the source schematic, VCO_schematic.png):
*   XN0/XN1  NMOS cross-coupled pair (the -2/gm that cancels the tank loss)
*   XN2/XN3  NMOS accumulation-mode varactors, channel tied to Vcont,
*            gate on the tank — this is the frequency-tuning element
*   XN4      NMOS tail current source
*   XN5      diode-connected NMOS bias reference, driven by an external Iref
*   XC0/XC1  fixed MiM tank capacitance
*   XL0/XL1  the two tank inductors, VDD to each output
*
* Conversion notes (45nm generic bulk / Spectre -> sky130A / ngspice,
* parameterization per example/three_stage_ota/user_inputs/three_stage_ota.sp):
*   nmos -> sky130_fd_pr__nfet_01v8 ; VDD 1.6 V -> 1.8 V (sky130 1v8 rail)
*   Wn/Wn5/Wn6/Wvar/C1/L1/Rp desVar indirection -> literal l/w/nf/m per device
*   l=45n -> l=0.15 for the switching pair (sky130 Lmin), l=0.5 for the
*     varactors and the current mirror (output resistance / cap density)
*   ideal `capacitor c=C1` -> physical sky130_fd_pr__cap_mim_m3_1, ~2 fF/um^2
*   ideal `inductor l=L1`  -> the vco_spiral_ind model below
*   `isource dc=300u` I0 was ideal and internal; it is a source, not a device,
*     so it moves to the testbench and reaches the mirror through the Iref port
*     (same treatment CL got in three_stage_ota).
*   Rp (2.2k, VDD to each output) is DELETED. In the source netlist the
*     inductor was lossless and Rp stood in for the tank's finite Q. The
*     inductor model below carries its own series R, so keeping Rp would
*     double-count the loss and stop the oscillator from starting.
*   Node renaming: Vout\+ -> voutp, Vout\- -> voutn, net1 -> vtail,
*     net16 -> Iref (now a port), GND -> VSS.
*   PMOS-free design, so every bulk ties to VSS except the varactor bulks,
*     which also tie to VSS (channel at Vcont keeps the junction reverse-biased).
*
* W/L below are a STARTING POINT, not a sized design — the source carried
* Spectre sizer variables that mean nothing in sky130. Measured at this
* starting point (see the testbench): OscFreq 2.724 GHz, TuningRange 226 MHz,
* OutputPower 8.66 dBm, Power 2.876 mW — three of the four spec keys fail.
* Run schematic-sizing against target_spec.json before freezing.

* --- Tank inductor -----------------------------------------------------------
* A pi-model of the parametric spiral this project's own generator draws:
*   python src/cells/primitives/inductor.py \
*       --turns 4.5 --inner 100 --width 8 --space 2 --shape octagon
* which reports L=3.986 nH (current-sheet), R_dc=9.225 ohm, C_sub=292.7 fF,
* 196.0 x 213.2 um on met5 (72/20), Q~6.5 @ 2.4 GHz, crude f_SR 4.7 GHz.
* The generator's own spice_subckt() emits series L-R only; the substrate cap
* is split half to each terminal here so the pre-layout tank is not optimistic
* about its own loading. The VDD-side half sits on an AC ground and does
* nothing; the output-side half is real tank capacitance.
* NOTE: at 3.5 GHz this coil runs at ~0.75 x f_SR, where an L-R-C lumped model
* is optimistic. It is adequate for sizing; an EM-extracted model is not.
.subckt vco_spiral_ind A B
Cpa A 0 146.35f
L1  A n1 3.9860n
R1  n1 B 9.2250
Cpb B 0 146.35f
.ends vco_spiral_ind

* --- VCO core ----------------------------------------------------------------
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
XN4 vtail Iref VSS VSS sky130_fd_pr__nfet_01v8 l=0.5 w=15 nf=1 m=3
XN5 Iref  Iref VSS VSS sky130_fd_pr__nfet_01v8 l=0.5 w=15 nf=1 m=1
* XC0/XC1: fixed MiM tank capacitance (source C0/C1, C1). 16x16 um ~ 512 fF.
XC0 VDD voutp sky130_fd_pr__cap_mim_m3_1 l=7.5 w=7.5 m=1
XC1 VDD voutn sky130_fd_pr__cap_mim_m3_1 l=7.5 w=7.5 m=1
* XL0/XL1: the two tank inductors (source L0/L1, L1)
XL0 VDD voutp vco_spiral_ind
XL1 VDD voutn vco_spiral_ind
.ends vco_lc
