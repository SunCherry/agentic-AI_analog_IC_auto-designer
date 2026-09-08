gds read /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/routed.gds
load routed
select top cell
puts "EXT_BBOX: [box values]"
port makeall
extract path .
extract all
ext2spice lvs
ext2spice -o routed_extracted.spice
quit -noprompt
