#!/bin/zsh
# usage: cand.sh <tag> <seed> <w_wire> <w_ov> <w_density> <w_area> <iters>
set -e
REPO=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer
L=$REPO/example/test_vco/layout
S=$L/sweep
TAG=$1; SEED=$2; WW=$3; WOV=$4; WD=$5; WA=$6; IT=$7
python $REPO/.claude/skills/placer/script/anneal_placement.py $L/primitives/manifest.json \
  --iters $IT --seed $SEED --single-phase --no-render \
  --w-wire $WW --w-ov $WOV --w-sym 0 --w-density $WD --w-area $WA --w-aspect 10 \
  --out $L/placement_pos.json --summary-out $S/${TAG}_placing.txt > $S/${TAG}_place.log 2>&1
cp $L/placement_pos.json $S/${TAG}_placement_pos.json
python $REPO/.claude/skills/router/script/route_nets.py $L --no-gds \
  --out $S/${TAG}_routes.json --summary-out $S/${TAG}_routing.txt > $S/${TAG}_route.log 2>&1
echo "=== $TAG seed=$SEED ww=$WW wov=$WOV wd=$WD wa=$WA iters=$IT"
grep -E "overlap  |routing clearance|canvas aspect      |total HPWL|bounding box" $S/${TAG}_placing.txt
grep -E "total wirelength|path-on-path|failed nets|box-edge fallback|real glayout ports|nets routed|congestion" $S/${TAG}_routing.txt
