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
# When RPC_ENDPOINTS is set the script measures three rows: the local no-RPC control, the remote peer
# alone, and -- the one T2 actually needs -- the model split between a local worker and the remote
# peer, which is the only configuration where per-token traffic crosses the link.
#
# SECURITY: ggml-rpc-server has no authentication, no token and no TLS (F15), and upstream warns
# never to expose it on an open network. Bind the remote side to one interface and firewall it to
# the benchmark client only -- NOT 0.0.0.0. See README.md, "Read this before starting a server on
# your network". The local worker this script spawns is bound to 127.0.0.1 for the same reason.
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

# Ports are derived from the PID so two runs -- or a run following one whose sockets are still in
# TIME_WAIT -- cannot collide. A fixed port range caused a silent single-peer measurement that was
# reported as a two-peer result; see the correction in F27.
PORT_BASE=$(( 50000 + (($$ * 7) % 9000) ))

# Start a server AND prove it is listening. `sleep 2` and hope is what produced the bad row: the
# process starts, prints its banner, fails to bind, and stays alive -- so a PID check says nothing.
# ggml-rpc-server prints "Failed to create server socket" in that case and keeps running.
serve() { # <port> <tag> -> pid on stdout, or exits the script
    local port=$1 tag=$2 log="$OUT/srv-$2.log"
    "$BIN/ggml-rpc-server" -H 127.0.0.1 -p "$port" > "$log" 2>&1 &
    local pid=$!
    for _ in $(seq 1 40); do
        if grep -q "Failed to create server socket" "$log" 2>/dev/null; then
            echo "server $tag failed to bind 127.0.0.1:$port -- see $log" >&2
            kill -9 "$pid" 2>/dev/null; exit 1
        fi
        # /dev/tcp is a bash builtin, so this needs no extra tooling on the runner.
        (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null && { exec 3<&- 3>&-; echo "$pid"; return; }
        sleep 0.25
    done
    echo "server $tag never listened on 127.0.0.1:$port -- see $log" >&2
    kill -9 "$pid" 2>/dev/null; exit 1
}

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
    if [ $rc -ne 0 ]; then echo "FAILED(rc=$rc)|0|0|$((t1-t0))|0|0"; return; fi
    local pp tg devs ndev
    pp=$(grep -oE 'Prompt: *[0-9.]+' "$log" | tail -1 | grep -oE '[0-9.]+')
    tg=$(grep -oE 'Generation: *[0-9.]+' "$log" | tail -1 | grep -oE '[0-9.]+')
    devs=$(grep -cE 'assigned to device RPC[0-9]+' "$log")
    # DISTINCT devices, not just the tensor count. With two endpoints and one of them dead, every
    # tensor lands on the survivor and the total still looks healthy -- so the total cannot tell a
    # split ring from a single node. Only the distinct count can, and the split row is the entire
    # point of T2.
    ndev=$(grep -oE 'assigned to device RPC[0-9]+' "$log" | sort -u | wc -l | tr -d ' ')
    echo "${label}|${pp:-0}|${tg:-0}|$((t1-t0))|${devs}|${ndev}"
}

# Median rather than mean: one scheduler hiccup on a shared runner skews a mean badly, and with
# REPEATS=3 the median is the middle sample.
median() { printf '%s\n' "$@" | sort -n | awk '{a[NR]=$1} END{print (NR%2)?a[(NR+1)/2]:(a[NR/2]+a[NR/2+1])/2}'; }

measure() { # <label> [--rpc <endpoints>]
    local label=$1; shift
    local pps=() tgs=() walls=() min_devs=-1 min_ndev=-1 failed=0 expect=1
    # How many distinct RPC devices this row should be using: one per endpoint in --rpc.
    if [ "${1:-}" = "--rpc" ]; then expect=$(awk -F, '{print NF}' <<< "$2"); fi
    for r in $(seq 1 "$REPEATS"); do
        IFS='|' read -r status pp tg wall d nd \
            <<< "$(run_one "$label" "$OUT/${label// /_}-$r.log" "$@")"
        case "$status" in
            FAILED*) failed=1; echo "  !! $label, repetition $r: $status" >&2 ;;
        esac
        pps+=("$pp"); tgs+=("$tg"); walls+=("$wall")
        # Minimum, not last. Keeping only the final repetition's count would let an earlier run that
        # offloaded nothing hide behind a good last one.
        { [ "$min_devs" -lt 0 ] || [ "$d"  -lt "$min_devs" ]; } && min_devs=$d
        { [ "$min_ndev" -lt 0 ] || [ "$nd" -lt "$min_ndev" ]; } && min_ndev=$nd
    done

    # A timed-out or non-zero repetition yields pp=tg=0, and a zero folded into the median produces a
    # number that looks entirely plausible -- with REPEATS=2 a single failure halves the result. That
    # is the false-green shape this project has been bitten by repeatedly, so invalidate the whole
    # row rather than reporting a corrupted average. Failures are most likely exactly where the
    # answer matters most: the remote Wi-Fi run.
    if [ "$failed" -eq 1 ]; then
        printf '  %-26s  NO RESULT -- a repetition failed; row invalidated\n' "$label"
        return 1
    fi

    printf '  %-26s  PP %8s t/s   TG %8s t/s   wall %6s ms   %s\n' \
        "$label" "$(median "${pps[@]}")" "$(median "${tgs[@]}")" "$(median "${walls[@]}")" \
        "$([ "$#" -gt 0 ] && echo "${min_devs} tensors on ${min_ndev}/${expect} peers" || echo "local")"

    # Two negative controls, and the second is the one that matters for T2.
    #
    # llama-cli does NOT fail when an RPC endpoint is unreachable: it exits 0, silently computes
    # locally, and reports an entirely believable throughput. Measured directly -- an unreachable
    # peer produced PP 199 t/s with zero tensors offloaded, indistinguishable from a good result
    # except by this check.
    if [ "$#" -gt 0 ] && [ "$min_devs" -eq 0 ]; then
        echo "  !! '$label' requested --rpc but a repetition offloaded 0 tensors -- it fell back" >&2
        echo "     to local compute. That row measures nothing. Treat it as no result." >&2
        return 1
    fi
    # And a tensor total cannot distinguish "split across both peers" from "all on the survivor",
    # because a dead peer just means everything lands on the live one and the total looks fine.
    if [ "$#" -gt 0 ] && [ "$min_ndev" -lt "$expect" ]; then
        echo "  !! '$label' expected $expect RPC peers but a repetition used only $min_ndev." >&2
        echo "     Layers were not split as intended -- this does not measure the pooling" >&2
        echo "     topology. Check every endpoint is reachable. Treat it as no result." >&2
        return 1
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
    # Two rows, and the distinction is the whole point of T2.
    #
    # "remote only" puts EVERY layer on the peer, because -ngl 99 offloads all of them and there is
    # only one device to take them. That is ordinary remote inference -- the peer does the compute
    # and this machine just drives it -- so it conflates the peer's CPU speed with the network cost.
    # Useful for decomposing the two, useless on its own for the pooling question.
    #
    # "split" is the topology T2 actually has to validate: a local worker AND the remote peer, so
    # the scheduler divides the layers between them and the per-token traffic genuinely crosses the
    # link. This is the row that answers "can two machines pool memory over Wi-Fi and stay usable".
    measure "remote only" --rpc "$RPC_ENDPOINTS"

    # Bound to 127.0.0.1 deliberately: the local worker only ever needs to be reachable by the
    # client in this same process tree, and ggml-rpc-server has no authentication of any kind.
    PL=$(serve $((PORT_BASE+4)) local-worker)
    measure "split: local + remote" --rpc "127.0.0.1:$((PORT_BASE+4)),$RPC_ENDPOINTS"
    kill -9 "$PL" 2>/dev/null
else
    PA=$(serve $((PORT_BASE+0)) solo)
    measure "1 peer, loopback" --rpc "127.0.0.1:$((PORT_BASE+0))"
    kill -9 "$PA" 2>/dev/null; sleep 1

    # Distinct tags: reusing one would overwrite the first server's log and lose the evidence that
    # both peers were actually serving.
    PA=$(serve $((PORT_BASE+2)) pair-a); PB=$(serve $((PORT_BASE+3)) pair-b)
    measure "2 peers, loopback" --rpc "127.0.0.1:$((PORT_BASE+2)),127.0.0.1:$((PORT_BASE+3))"
    kill -9 "$PA" "$PB" 2>/dev/null
fi

echo
echo "logs in $OUT"
echo
echo "Loopback is a control, not a network. The number that matters is this table run again with"
echo "RPC_ENDPOINTS pointed at a peer across Wi-Fi — see README.md, 'What T2 has to measure'."
