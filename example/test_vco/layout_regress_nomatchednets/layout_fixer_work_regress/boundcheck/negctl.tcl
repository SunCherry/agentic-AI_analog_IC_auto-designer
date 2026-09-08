gds read /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/routed.gds
load routed
select top cell
flatten -dobox -nolabels routed_neg
load routed_neg
snap internal
box values 110000 50000 110100 50100
puts "NEG_BOX1: [box values]"
paint met1
box values 110110 50000 110210 50100
puts "NEG_BOX2: [box values]"
paint met1
drc euclidean on
drc style drc(full)
drc on
select top cell
expand
drc check
drc catchup
set n [drc list count total]
puts "NEG_DRC_TOTAL: $n"
set res [drc listall why]
foreach {rule coords} $res {
  puts "NEG_RULE: ([llength $coords]) $rule"
  foreach c $coords { puts "NEG_COORD: $rule @ $c" }
}
quit -noprompt
