gds read /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/designs/test_miller_ota_0908/layout/placement_visualization.gds
load placement_visualization
select top cell
drc euclidean on
drc style drc(full)
drc on
select top cell
expand
drc check
drc catchup
set n [drc list count total]
puts "DRC_TOTAL: $n"
foreach pair [drc listall count] { puts "CELLCOUNT: [lindex $pair 0] [lindex $pair 1]" }
set res [drc listall why]
foreach {rule coords} $res {
  puts "RULE: ([llength $coords]) $rule"
  foreach c $coords { puts "COORD: $rule @ $c" }
}
quit -noprompt
