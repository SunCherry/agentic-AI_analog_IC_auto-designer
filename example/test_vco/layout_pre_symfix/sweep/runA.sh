#!/bin/zsh
source /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/sweep/batch.sh
for s in 1 2 3 4 5 6 7 8; do run A$s $s 1.0 20 60 0.002 20000; done
