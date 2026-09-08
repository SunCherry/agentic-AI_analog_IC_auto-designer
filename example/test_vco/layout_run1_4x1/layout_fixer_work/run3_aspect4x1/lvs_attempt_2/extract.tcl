gds read /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/layout_fixer_work/run3_aspect4x1/lvs_attempt_2/routed_lvsview.gds
load routed
select top cell
puts "EXT_BBOX: [box values]"
port makeall
extract path .
extract all
ext2spice lvs
ext2spice -o routed_extracted.spice
quit -noprompt
