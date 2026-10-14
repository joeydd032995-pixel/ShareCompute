#!/bin/bash
# like run_n.sh but through counting proxies (tools/proxy.py).
# usage: SCRATCH=dir [BUILD=build/desktop] [PROBE=path] runp.sh NAME MODEL MODE N SHARES BUDGET TOKENS OWD_MS MBIT
#   MODE is layer or tensor; SHARES and BUDGET are comma lists; PROBE defaults to $SCRATCH/bin/probe-f49 (tools/build_probes.sh).
#   Fixed ports 51011+ (workers) and 51051+ (proxies): fails if any is already taken or a worker dies at start.
HERE=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$HERE/../../../.." && pwd)
SCRATCH=${SCRATCH:?set SCRATCH to a writable directory}; BUILD=${BUILD:-$REPO/build/desktop}
NAME=$1; MODEL=$2; MODE=$3; N=$4; SHARES=$5; BUD=${6:-4096}; TOK=${7:-16}; OWD=${8:-0}; MBIT=${9:-0}
D=$SCRATCH/tp
PROBE=${PROBE:-$SCRATCH/bin/probe-f49}
mkdir -p "$D/out/$NAME"; rm -f "${D:?}/out/${NAME:?}"/*
PIDS=(); EPS=(); IFS=, read -ra BUDS <<< "$BUD"
for i in $(seq 1 $N); do for port in $((51010+i)) $((51050+i)); do
  if (echo > /dev/tcp/127.0.0.1/$port) 2>/dev/null; then echo "port $port is already in use (a leaked process?)" >&2; exit 3; fi; done; done
for i in $(seq 1 $N); do
  port=$((51010+i)); pport=$((51050+i))
  $BUILD/bin/sc-rpc-worker $port ${BUDS[$((i-1))]:-${BUDS[0]}} 2 ${CACHE:+$CACHE/w$i} > $D/out/$NAME/worker$i.log 2>&1 & PIDS+=($!)
  L=${LINKS:+$(echo $LINKS | cut -d" " -f$i)}; O=${L:+${L%%:*}}; B=${L:+${L##*:}}; python3 $HERE/proxy.py $pport $port $D/out/$NAME/proxy$i.json ${O:-$OWD} ${B:-$MBIT} & PIDS+=($!)
  EPS+=("\"127.0.0.1:$pport\"")
done
sleep 1.5
for p in "${PIDS[@]}"; do kill -0 $p 2>/dev/null || { echo "a worker or proxy exited at start (see $D/out/$NAME)" >&2; for q in "${PIDS[@]}"; do kill $q 2>/dev/null; done; exit 4; }; done
EPJ=$(IFS=,; echo "${EPS[*]}")
case "${PAD:-true}" in false|False|0) PADV=False;; *) PADV=True;; esac
python3 - <<PY
import json
open("$D/out/$NAME/cfg.json","w").write(json.dumps({"endpoints":[$EPJ],"shares":[$SHARES],"tokens":$TOK,"model":"$MODEL","prompt":"The capital of France is","split_mode":"$MODE","pad_shares":$PADV,"nseq":${NSEQ:-1}}))
PY
( (until grep -q prefill_start $D/out/$NAME/probe.err 2>/dev/null; do sleep 0.05; done; for i in $(seq 1 $N); do touch $D/out/$NAME/proxy$i.json.arm; done) & ); ( time timeout ${TMO:-900} $PROBE $D/out/$NAME/cfg.json > $D/out/$NAME/probe.out 2> $D/out/$NAME/probe.err ) 2> $D/out/$NAME/time.txt
echo "exit=$?" >> $D/out/$NAME/time.txt
sleep 1
for p in "${PIDS[@]}"; do kill $p 2>/dev/null; done
wait 2>/dev/null
grep SC_RESULT $D/out/$NAME/probe.out | head -c 700; echo
grep -c . $D/out/$NAME/probe.err >/dev/null; tail -2 $D/out/$NAME/probe.err | cut -c1-200
cat $D/out/$NAME/time.txt | tr '\n' ' '; echo
for i in $(seq 1 $N); do echo "proxy$i: $(cat $D/out/$NAME/proxy$i.json)"; done
