* Two-stage Miller OTA — NMOS diff pair + PMOS mirror load, common-source 2nd stage.
* MODIFIED by fold_wide_devices.py netlist: folded to fit the PDK model bins.
*   XMN1: w=320 m=1 -> w=40 m=8  (total 320um unchanged, 8x copies to fit the PDK model bin)
*   XMN2: w=320 m=1 -> w=40 m=8  (total 320um unchanged, 8x copies to fit the PDK model bin)
*   XMN3: w=102.6 m=1 -> w=34.2 m=3  (total 102.6um unchanged, 3x copies to fit the PDK model bin)
*   XMN4: w=236 m=1 -> w=47.2 m=5  (total 236um unchanged, 5x copies to fit the PDK model bin)
*   XMP3: w=120 m=1 -> w=40 m=3  (total 120um unchanged, 3x copies to fit the PDK model bin)
*   XMN5: w=345 m=1 -> w=43.125 m=8  (total 345um unchanged, 8x copies to fit the PDK model bin)
* Re-expression only: same total width, same topology, same nets. `nf` untouched --
* the model bins on per-copy w and nf does not divide it. Original in user_inputs/.
* Derived from two_stage.sp (same device sizes, ideal Ibias, no self-biased XM8)
* with series nulling resistor R0 + physical MiM compensation cap from miller_ota_3.sp.
.subckt two_stage_rz Vip Vin VDD VSS vout
* PMOS mirror load (diode-connected reference + mirror)
XMP1 net4 net4 VDD VDD sky130_fd_pr__pfet_01v8 l=0.3 w=14.4 nf=1 m=1
XMP2 net5 net4 VDD VDD sky130_fd_pr__pfet_01v8 l=0.3 w=14.4 nf=1 m=1
* NMOS input differential pair
XMN1 net4 Vin net3 VSS sky130_fd_pr__nfet_01v8 l=0.15 w=40 nf=1 m=8
XMN2 net5 Vip net3 VSS sky130_fd_pr__nfet_01v8 l=0.15 w=40 nf=1 m=8
* Tail current source (mirrors bias reference MN4)
XMN3 net3 net7 VSS VSS sky130_fd_pr__nfet_01v8 l=0.3 w=70 nf=1 m=6
* Bias reference (diode-connected)
XMN4 net7 net7 VSS VSS sky130_fd_pr__nfet_01v8 l=0.3 w=70 nf=1 m=10
* Second stage — common-source gain (PMOS) + current-source load (NMOS)
XMP3 vout net5 VDD VDD sky130_fd_pr__pfet_01v8 l=0.3 w=40 nf=1 m=3
XMN5 vout net7 VSS VSS sky130_fd_pr__nfet_01v8 l=0.3 w=70 nf=1 m=14
* Miller compensation: series nulling resistor + MiM cap (~2.0 fF/um^2, 45x45um ~ 4.05pF)
* R0: generic poly resistor, ~48.2 ohm/sq * 4.15/2.0 ~ 100 ohm
R0 net5 net8 sky130_fd_pr__res_generic_po w=2 l=4.15
XC0 net8 vout sky130_fd_pr__cap_mim_m3_1 l=45 w=45
* Self-biased reference: diode-connected PMOS in series with diode-connected XMN4 (~100uA)
XMP4 net7 net7 VDD VDD sky130_fd_pr__pfet_01v8 l=0.15 w=2.3 nf=1 m=1
.ends two_stage_rz
