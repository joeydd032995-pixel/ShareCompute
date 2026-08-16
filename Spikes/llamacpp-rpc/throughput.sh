#!/usr/bin/env bash
#
# T1b / T2 — how much does the RPC transport actually cost?
#
# T1 answered "does the topology work" and "how does it fail". Neither says whether the result is
# fast enough to use, and that is the question the whole product rests on: Apps/InferRing/README.md
# reports Mac+iPhone at only -12% token generation, but warns in the same breath that "Wi-Fi and
# pre-TB5 over RDMA connections will result in sharp performance decline" and recommends a USB3.2
# cable. An iPhone and a Windows PC have no such cable between them, so the near-term ring is the
# configuration upstream warns about — and nothing has ever measured it.
#
# This produces the loopback baseline that a later cross-machine run is compared against. Loopback
# is not a network: no MTU, no contention, no radio. That is exactly why it is the right control.
# The interesting number is not any single row, it is the DELTA between this and the same script
# run against a peer on the other side of a Wi-Fi link.
#
#   bash Spikes/llamacpp-rpc/throughput.sh                              # loopback baseline
#   RPC_ENDPOINTS=192.168.1.50:50052 bash Spikes/llamacpp-rpc/throughput.sh   # T2, remote peer
#   REPEATS=5 bash Spikes/llamacpp-rpc/throughput.sh
#
# When RPC_ENDPOINTS is set, no local servers are spawned and the "local" row is still measured as
# the control. Start the remote side with:  ggml-rpc-server -H 0.0.0.0 -p 50052
#
# Requires llama.cpp built with -DGGML_RPC=ON, and a GGUF. See README.md in this directory.

set -uo pipefail

BIN=${BIN:-/home/user/llama.cpp/build/bin}
MODEL=${MODEL:-/home/user/models/qwen2.5-0.5b-instruct-q4_k_m.gguf}
OUT=${OUT:-/tmp/t1-throughput}
REPEATS=${REPEATS:-3}
NGEN=${NGEN:-128}
RPC_ENDPOINTS=${RPC_ENDPOINTS:-}

for f in "$BIN/ggml-rpc-server" "$BIN/llama-cli"; do
    [ -x "$f" ] || { echo "missing $f — see README.md" >&2; exit 1; }
done
[ -r "$MODEL" ] || { echo "missing model $MODEL — see README.md" >&2; exit 1; }

rm -rf "$OUT" && mkdir -p "$OUT"
export LD_LIBRARY_PATH="$BIN:${LD_LIBRARY_PATH:-}"

# A long fixed prompt so prompt-processing is measurable rather than noise, and --ignore-eos with a
# fixed -n so every configuration generates exactly the same number of tokens. Without that the
# comparison is between different amounts of work. -st and closed stdin keep llama-cli out of
# conversation mode, which cost one wrong reading in T1.
PROMPT="Explain in detail how gravity works, why it is described as spacetime curvature rather than a force, and how that description differs from Newton's."
#
# -v is load-bearing, not debugging noise. Without it the logs carry no "assigned to device RPC0"
# lines, and there is then no way to tell a genuine RPC run from one that silently fell back to
# local compute -- which would produce an entirely plausible table measuring nothing. The first
# version of this script omitted it and could not answer that question afterwards (F27).
# It costs ~2800 log lines per run and does not disturb the summary line this parses.
CLI_COMMON=(-ngl 99 -c 4096 -st -no-cnv --ignore-eos -n "$NGEN" -v)

serve() { "$BIN/ggml-rpc-server" -H 127.0.0.1 -p "$1" > "$OUT/srv-$2.log" 2>&1 & echo $!; }

# llama-cli's last line is: [ Prompt: 174.3 t/s | Generation: 24.3 t/s ]
# Wall clock is captured separately because it is the only thing that reflects model upload, which
# is where a slow link hurts most and which the t/s figures deliberately exclude.
run_one() { # <label> <logfile> [--rpc <endpoints>]
    local label=$1 log=$2; shift 2
    local t0 t1
    t0=$(date +%s%3N)
    timeout 600 "$BIN/llama-cli" -m "$MODEL" "$@" "${CLI_COMMON[@]}" -p "$PROMPT" \
        < /dev/null > "$log" 2>&1
    local rc=$?
    t1=$(date +%s%3N)
    if [ $rc -ne 0 ]; then echo "FAILED(rc=$rc)|0|0|$((t1-t0))|0"; return; fi
    local pp tg devs
    pp=$(grep -oE 'Prompt: *[0-9.]+' "$log" | tail -1 | grep -oE '[0-9.]+')
    tg=$(grep -oE 'Generation: *[0-9.]+' "$log" | tail -1 | grep -oE '[0-9.]+')
    devs=$(grep -cE 'assigned to device RPC[0-9]+' "$log")
    echo "${label}|${pp:-0}|${tg:-0}|$((t1-t0))|${devs}"
}

# Median rather than mean: one scheduler hiccup on a shared runner skews a mean badly, and with
# REPEATS=3 the median is the middle sample.
median() { printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2}'; }

measure() { # <label> [--rpc <endpoints>]
    local label=$1; shift
    local pps=() tgs=() walls=() devs=0
    for r in $(seq 1 "$REPEATS"); do
        IFS='|' read -r _ pp tg wall d <<< "$(run_one "$label" "$OUT/${label// /_}-$r.log" "$@")"
        pps+=("$pp"); tgs+=("$tg"); walls+=("$wall"); devs=$d
    done
    printf '  %-26s  PP %8s t/s   TG %8s t/s   wall %6s ms   %s\n' \
        "$label" "$(median "${pps[@]}")" "$(median "${tgs[@]}")" "$(median "${walls[@]}")" \
        "$([ "$#" -gt 0 ] && echo "${devs} tensors on RPC" || echo "local")"
    # The negative control. An RPC row that offloaded nothing is measuring local compute and would
    # otherwise report a perfectly believable number -- the exact false-green shape this project has
    # been bitten by five times. Fail loudly instead.
    if [ "$#" -gt 0 ] && [ "$devs" -eq 0 ]; then
        echo "  !! '$label' requested --rpc but offloaded 0 tensors -- this row measures local" >&2
        echo "     compute, not the transport. Treat it as no result." >&2
    fi
}

echo
echo "model:   $(basename "$MODEL")"
echo "gen:     $NGEN tokens, ${REPEATS} repeats, median reported"
echo "link:    ${RPC_ENDPOINTS:-loopback (127.0.0.1)}"
echo
printf '\033[1m%s\033[0m\n' "Throughput by configuration"

# The control. Every RPC row is only meaningful relative to this one.
measure "local, no RPC"

if [ -n "$RPC_ENDPOINTS" ]; then
    measure "remote: $RPC_ENDPOINTS" --rpc "$RPC_ENDPOINTS"
else
    PA=$(serve 50320 solo); sleep 2
    measure "1 peer, loopback" --rpc 127.0.0.1:50320
    kill -9 "$PA" 2>/dev/null; sleep 1

    # Distinct tags: reusing one would overwrite the first server's log and lose the evidence that
    # both peers were actually serving.
    PA=$(serve 50322 pair-a); PB=$(serve 50323 pair-b); sleep 2
    measure "2 peers, loopback" --rpc 127.0.0.1:50322,127.0.0.1:50323
    kill -9 "$PA" "$PB" 2>/dev/null
fi

echo
echo "logs in $OUT"
echo
echo "Loopback is a control, not a network. The number that matters is this table run again with"
echo "RPC_ENDPOINTS pointed at a peer across Wi-Fi — see README.md, 'What T2 has to measure'."
