gds read /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/designs/test_miller_ota_0908/layout/shortening/test_miller_ota_0908_fixed_shortened.gds
load routed
select top cell
box values
puts "SRC_BBOX: [box values]"
puts "CELLNAME: [cellname list self]"
quit -noprompt
