#!/bin/zsh
source /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/sweep/batchP3.sh
for s in 10 11 12 13 14 15 16 17 18 19 20; do runq U$s $s 1.0 20 30 0.0005 60000 0.005 50; done
