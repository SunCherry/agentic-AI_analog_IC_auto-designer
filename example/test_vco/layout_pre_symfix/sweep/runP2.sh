#!/bin/zsh
source /Users/cherrysun/Documents/agentic-AI_analog_IC_auto-designer/example/test_vco/layout/sweep/batchP.sh
for s in 1 2 3 4 5 6 7 8; do runp Q$s $s 1.0 20 60 0.0005 20000; done
for s in 1 2 3 4 5 6 7 8; do runp R$s $s 1.0 20 60 0.0002 20000; done
for s in 1 2 3 4 5 6 7 8; do runp S$s $s 1.0 20 30 0.0005 20000; done
for s in 1 2 3 4 5 6 7 8; do runp T$s $s 1.0 20 60 0.005  20000; done
