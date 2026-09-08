gds read routed_lvsview.gds
load routed
select top cell
port makeall
extract path .
extract all
ext2spice lvs
ext2spice -o routed_extracted.spice
quit -noprompt
