gds read /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/designs/test_miller_ota_0908/layout/shortening/test_miller_ota_0908_fixed_shortened.gds
load routed
select top cell
puts "SRC_BBOX: [box values]"
port makeall
extract path .
extract all
ext2spice lvs
ext2spice -o routed_extracted.spice
quit -noprompt
