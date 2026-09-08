#!/bin/zsh
REPO=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer
L=$REPO/example/test_vco/layout
S=$L/sweep
TAG=$1; shift
EXTRA="$@"
cp $S/${TAG}_placement_pos.json $L/placement_pos.json
python $REPO/.claude/skills/router/script/route_nets.py $L $EXTRA \
   > $S/${TAG}_routegds.log 2>&1
cp $L/routed.gds $S/gds/${TAG}_routed.gds
cp $L/routing_summary.txt $S/gds/${TAG}_routing_gds.txt
mkdir -p $L/drc_screen/$TAG
python $REPO/.claude/skills/router/script/run_drc.py $S/gds/${TAG}_routed.gds \
   --top routed --work-dir $L/drc_screen/$TAG > $S/gds/${TAG}_drc.log 2>&1
WL=$(grep -m1 "total wirelength" $L/routing_summary.txt | grep -oE "[0-9.]+ um")
echo "### $TAG  wire=$WL  extra='$EXTRA'"
grep -E "DRC errors|distinct violated|DRC check" $S/gds/${TAG}_drc.log
