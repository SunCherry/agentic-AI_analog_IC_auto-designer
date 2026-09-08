#!/bin/zsh
REPO=/Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer
L=$REPO/example/test_vco/layout
S=$L/sweep
CSV=$S/results.csv
[[ -f $CSV ]] || echo "tag,seed,w_wire,w_ov,w_density,w_area,iters,overlap,clearance,aspect,hpwl,bbox,routed_um,shorts,failed,fallback" > $CSV
run() {
  TAG=$1; SEED=$2; WW=$3; WOV=$4; WD=$5; WA=$6; IT=$7
  python $REPO/.claude/skills/placer/script/anneal_placement.py $L/primitives/manifest.json \
    --iters $IT --seed $SEED --single-phase --no-render \
    --w-wire $WW --w-ov $WOV --w-sym 0 --w-density $WD --w-area $WA --w-aspect 10 \
    --out $L/placement_pos.json --summary-out $S/${TAG}_placing.txt > $S/${TAG}_place.log 2>&1
  RC=$?
  cp $L/placement_pos.json $S/${TAG}_placement_pos.json 2>/dev/null
  python $REPO/.claude/skills/router/script/route_nets.py $L --no-gds \
    --out $S/${TAG}_routes.json --summary-out $S/${TAG}_routing.txt > $S/${TAG}_route.log 2>&1
  OV=$(grep -m1 "overlap  " $S/${TAG}_placing.txt | grep -oE "PASS|FAIL")
  CL=$(grep -m1 "routing clearance" $S/${TAG}_placing.txt | grep -oE "PASS|FAIL")
  AS=$(grep -m1 "canvas aspect      " $S/${TAG}_placing.txt | grep -oE "[0-9.]+:1" | head -1)
  ASV=$(grep -m1 "canvas aspect      " $S/${TAG}_placing.txt | grep -oE "PASS|OVER")
  HP=$(grep -m1 "total HPWL" $S/${TAG}_placing.txt | grep -oE "[0-9.]+ um" | head -1 | tr -d ' um')
  BB=$(grep -m1 "bounding box" $S/${TAG}_placing.txt | sed -E 's/.*: *([0-9.]+ x [0-9.]+) um.*/\1/' | tr -d ' ')
  WL=$(grep -m1 "total wirelength" $S/${TAG}_routing.txt | grep -oE "[0-9.]+ um" | head -1 | tr -d ' um')
  SH=$(grep -m1 "path-on-path shorts" $S/${TAG}_routing.txt | grep -oE ": *[0-9]+" | tr -d ': ')
  FN=$(grep -m1 "failed nets" $S/${TAG}_routing.txt | sed -E 's/.*: *//' | cut -c1-12)
  FB=$(grep -m1 "box-edge fallback" $S/${TAG}_routing.txt | grep -oE ": *[0-9]+" | tr -d ': ')
  echo "$TAG,$SEED,$WW,$WOV,$WD,$WA,$IT,$OV,$CL,$AS/$ASV,$HP,$BB,$WL,$SH,$FN,$FB" >> $CSV
  echo "$TAG seed=$SEED ww=$WW wd=$WD wa=$WA -> aspect=$AS/$ASV hpwl=$HP routed=$WL ov=$OV cl=$CL fb=$FB"
}
