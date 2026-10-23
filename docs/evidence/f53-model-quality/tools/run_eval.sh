#!/bin/bash
# Quality comparison harness. Usage: run_eval.sh <label> <model.gguf> [kld-mode]
# Everything is CPU, 4 threads, one model at a time so timings are not polluted.
set -u
S=${QLAB:-$(cd "$(dirname "$0")" && pwd)}
B=${LLAMA_BIN:-$S/build-avx512/bin}; D=$S/data; O=$S/out; mkdir -p $O
label=$1; model=$2; kld=${3:-}
T=4
MMLU_N=${MMLU_N:-300}; ARC_N=${ARC_N:-300}; HS_N=${HS_N:-200}; CH=${CH:-60}
log() { echo "[$(date +%H:%M:%S)] $label: $*" | tee -a $O/progress.log; }

# wikitext slice shared by every model: same text, so bits-per-byte is comparable
head -c 200000 $D/wikitext-2-raw/wiki.test.raw > $D/wt_slice.txt

log "tokens in slice"
$B/llama-tokenize -m $model -f $D/wt_slice.txt --show-count --no-bos --log-disable > $O/$label.tok 2>&1

log "perplexity (c=512, chunks=$CH)"
if [ "$kld" = "base" ]; then
  $B/llama-perplexity -m $model -f $D/wt_slice.txt -c 512 --chunks $CH -t $T --kl-divergence-base $O/ref.kld > $O/$label.ppl.log 2>&1
elif [ "$kld" = "vs" ]; then
  $B/llama-perplexity -m $model -f $D/wt_slice.txt -c 512 --chunks $CH -t $T --kl-divergence-base $O/ref.kld --kl-divergence > $O/$label.ppl.log 2>&1
else
  $B/llama-perplexity -m $model -f $D/wt_slice.txt -c 512 --chunks $CH -t $T > $O/$label.ppl.log 2>&1
fi

log "mmlu ($MMLU_N)"
$B/llama-perplexity -m $model -bf $D/mmlu.bin --multiple-choice --multiple-choice-tasks $MMLU_N -c 1024 -t $T > $O/$label.mmlu.log 2>&1
log "arc-challenge ($ARC_N)"
$B/llama-perplexity -m $model -bf $D/arc_c.bin --multiple-choice --multiple-choice-tasks $ARC_N -c 2560 -np 5 -b 512 -ub 512 -t $T > $O/$label.arc.log 2>&1
log "hellaswag ($HS_N)"
$B/llama-perplexity -m $model -f $D/hellaswag.txt --hellaswag --hellaswag-tasks $HS_N -c 1024 -t $T > $O/$label.hs.log 2>&1
log "done"
