#!/bin/zsh
source /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/sweep/batchP3.sh
for s in 1 2 3 4 5 6 7 8; do runq U$s $s 1.0 20 30 0.0005 60000 0.005 50; done
