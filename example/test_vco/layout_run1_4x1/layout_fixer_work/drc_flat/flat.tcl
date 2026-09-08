gds read /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/routed.gds
load routed
select top cell
puts "SRC_BBOX: [box values]"
flatten -dobox -nolabels routed_flat
load routed_flat
select top cell
puts "FLAT_BBOX: [box values]"
drc euclidean on
drc style drc(full)
drc on
select top cell
expand
drc check
drc catchup
set n [drc list count total]
puts "FLAT_DRC_TOTAL: $n"
foreach pair [drc listall count] { puts "FLATCELLCOUNT: [lindex $pair 0] [lindex $pair 1]" }
set res [drc listall why]
foreach {rule coords} $res {
  puts "FLATRULE: ([llength $coords]) $rule"
  foreach c $coords { puts "FLATCOORD: $rule @ $c" }
}
quit -noprompt
