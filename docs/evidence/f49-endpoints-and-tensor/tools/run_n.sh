#!/bin/bash
# usage: run_n.sh NAME PROBE_BIN "budget1 budget2 ..." [timeout]
NAME=$1; PROBE=$2; BUDGETS=($3); TO=${4:-240}
S=$SCRATCH/nep
W=/home/user/ShareCompute/build/desktop/bin/sc-rpc-worker
M=${MODEL:-/home/user/ShareCompute/models/split-proof.gguf}
mkdir -p $S/$NAME; cd $S/$NAME
PIDS=(); cleanup(){ for pid in "${PIDS[@]}"; do kill -9 $pid 2>/dev/null; done; }; trap cleanup EXIT; EPS=(); SH=(); i=0
for b in "${BUDGETS[@]}"; do
  port=$((${BASEPORT:-52000}+i)); $W $port $b 2 > worker$i.log 2>&1 & PIDS+=($!)
  EPS+=("\"127.0.0.1:$port\""); SH+=("$b"); i=$((i+1))
done
for pp in $(seq 0 $((i-1))); do for t in $(seq 1 100); do (echo > /dev/tcp/127.0.0.1/$((${BASEPORT:-52000}+pp))) 2>/dev/null && break; sleep 0.1; done; done
EP=$(IFS=,; echo "${EPS[*]}"); SHS=$(IFS=,; echo "${SH[*]}")
cat > cfg.json <<J
{"model":"$M","prompt":"${PROMPT:-The capital of France is}","tokens":${TOKENS:-16},"endpoints":[$EP],"shares":[$SHS]}
J
start=$(date +%s.%N)
timeout -k 5 $TO $PROBE cfg.json > probe.out 2> probe.err; rc=$?
end=$(date +%s.%N)
echo "$NAME rc=$rc wall=$(echo "$end - $start" | bc)"
grep -o 'SC_RESULT.*' probe.out | head -c 600; echo
grep -c "assigned to device RPC" probe.err
for p in $(seq 0 $((i-1))); do echo -n "RPC$p layers: "; grep -oE "layer +[0-9]+ assigned to device RPC$p\b" probe.err | grep -oE "^layer +[0-9]+" | grep -oE "[0-9]+" | head -n 100 | tr '\n' ' '; echo; done
for p in $(seq 0 $((i-1))); do echo -n "worker$p last stats: "; grep SC_STATS worker$p.log | tail -1 | cut -c1-140; done
echo "$NAME rc=$rc" > rc.txt
# on timeout, snapshot before killing
for pid in "${PIDS[@]}"; do kill $pid 2>/dev/null; done
sleep 0.5; for pid in "${PIDS[@]}"; do kill -9 $pid 2>/dev/null; done
wait 2>/dev/null
