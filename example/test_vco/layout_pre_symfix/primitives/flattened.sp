.subckt vco_lc Vcont Iref VDD VSS voutp voutn
XN0 voutn voutp vtail VSS sky130_fd_pr__nfet_01v8 l=0.15 w=20 nf=1 m=1
XN1 voutp voutn vtail VSS sky130_fd_pr__nfet_01v8 l=0.15 w=20 nf=1 m=1
XN2 Vcont voutp Vcont VSS sky130_fd_pr__nfet_01v8 l=0.5 w=60 nf=1 m=1
XN3 Vcont voutn Vcont VSS sky130_fd_pr__nfet_01v8 l=0.5 w=60 nf=1 m=1
XN4 vtail Iref VSS VSS sky130_fd_pr__nfet_01v8 l=0.5 w=15 nf=1 m=4
XN5 Iref Iref VSS VSS sky130_fd_pr__nfet_01v8 l=0.5 w=15 nf=1 m=1
XC0 VDD voutp sky130_fd_pr__cap_mim_m3_1 l=7.5 w=7.5 m=1
XC1 VDD voutn sky130_fd_pr__cap_mim_m3_1 l=7.5 w=7.5 m=1
.ends vco_lc
