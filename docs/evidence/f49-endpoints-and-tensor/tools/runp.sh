#!/bin/bash
# like run.sh but with proxies. usage: runp.sh NAME MODEL MODE N SHARES BUDGET TOKENS OWD_MS MBIT
NAME=$1; MODEL=$2; MODE=$3; N=$4; SHARES=$5; BUD=${6:-4096}; TOK=${7:-16}; OWD=${8:-0}; MBIT=${9:-0}
D=$SCRATCH/tp
R=/home/user/ShareCompute
mkdir -p $D/out/$NAME; rm -f $D/out/$NAME/*
PIDS=(); EPS=(); IFS=, read -ra BUDS <<< "$BUD"
for i in $(seq 1 $N); do
  port=$((51010+i)); pport=$((51050+i))
  $R/build/desktop/bin/sc-rpc-worker $port ${BUDS[$((i-1))]:-${BUDS[0]}} 2 ${CACHE:+$CACHE/w$i} > $D/out/$NAME/worker$i.log 2>&1 & PIDS+=($!)
  L=${LINKS:+$(echo $LINKS | cut -d" " -f$i)}; O=${L:+${L%%:*}}; B=${L:+${L##*:}}; python3 $D/proxy.py $pport $port $D/out/$NAME/proxy$i.json ${O:-$OWD} ${B:-$MBIT} & PIDS+=($!)
  EPS+=("\"127.0.0.1:$pport\"")
done
sleep 1.5
EPJ=$(IFS=,; echo "${EPS[*]}")
python3 - <<PY
import json
open("$D/out/$NAME/cfg.json","w").write(json.dumps({"endpoints":[$EPJ],"shares":[$SHARES],"tokens":$TOK,"model":"$MODEL","prompt":"The capital of France is","split_mode":"$MODE","nseq":${NSEQ:-1}}))
PY
( (until grep -q prefill_start $D/out/$NAME/probe.err 2>/dev/null; do sleep 0.05; done; for i in $(seq 1 $N); do touch $D/out/$NAME/proxy$i.json.arm; done) & ); ( time timeout ${TMO:-900} $D/${PROBE:-probe-tp} $D/out/$NAME/cfg.json > $D/out/$NAME/probe.out 2> $D/out/$NAME/probe.err ) 2> $D/out/$NAME/time.txt
echo "exit=$?" >> $D/out/$NAME/time.txt
sleep 1
for p in "${PIDS[@]}"; do kill $p 2>/dev/null; done
wait 2>/dev/null
grep SC_RESULT $D/out/$NAME/probe.out | head -c 700; echo
grep -c . $D/out/$NAME/probe.err >/dev/null; tail -2 $D/out/$NAME/probe.err | cut -c1-200
cat $D/out/$NAME/time.txt | tr '\n' ' '; echo
for i in $(seq 1 $N); do echo "proxy$i: $(cat $D/out/$NAME/proxy$i.json)"; done
