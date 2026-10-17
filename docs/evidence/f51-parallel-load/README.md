# Evidence for F51 - loading the endpoints at the same time

Measured 2026-10-16 and 17 in the container (x86_64 Linux, 4 shared cores), loopback only, 3B Q8_0 model,
three workers at the real budgets. Nothing here ran on a phone.

| File | What it is |
|---|---|
| `set-tensor-timing.diff` | The timing wrapped around the three steps of `ggml_backend_rpc_buffer_set_tensor` (hash, hash-check round trip, send). Applied to a scratch build only; **not** part of any carried patch. It prints one `SC_LOAD` line per endpoint when the probe exits. |
| `gate-reports/ser-1`, `ser-2` | Full coordinator gate, 16 tokens, `PASS` against `pinned-reference`, **with the final patch switched off** (`SC_PARALLEL_LOAD=0`). split_generation 50.1 s and 48.0 s. |
| `gate-reports/def-1`, `def-2` | The same gate with the final patch **on by default**. split_generation 34.3 s and 33.1 s. |
| `gate-reports/experiment-serial-tls`, `experiment-parallel-tls` | The same gate with the experiment version (before the patch existed): 47.6 s serial, 31.6 s parallel. |
| `verify-proof.json` | `scripts/verify_parallel_load.py` on the 0.5B proof model with the normal build: both runs produce the same eight tokens. |
| `harness/` | The scripts used: `loadtime.py` (probe-only load timing with three workers, cold and warm), `gate.sh` and `summ.py` (the gate runs and their summary), `bridgebench.py` (the relay's byte path in isolation, asyncio against threads). They carry the scratchpad paths of the session that ran them. |

Not committed: the three-patch checkout, the build directories, the 3.6 GB model, and the ThreadSanitizer build. To
reproduce the ThreadSanitizer check, configure `native/split` with
`-DCMAKE_C_FLAGS="-fsanitize=thread -g" -DCMAKE_CXX_FLAGS="-fsanitize=thread -g" -DCMAKE_EXE_LINKER_FLAGS=-fsanitize=thread -DGGML_NATIVE=OFF`
against the patched `.deps/llama-split`, build `sc-rpc-worker` and `sc-split-probe`, and run
`TSAN_OPTIONS="halt_on_error=0 exitcode=66" python3 scripts/verify_parallel_load.py --bin-dir <that bin> --model models/split-proof.gguf`.
The negative control is the same run after changing `std::atomic<size_t> size_done{0};` back to `size_t size_done = 0;` in
`src/llama-model-loader.h`: it reports a data race at `llama-model-loader.cpp:1750` and exits 66.

Limits: two to four runs per condition; cold timings varied between runs (cause unknown); three workers and the relay
share four cores, so loopback understates what separate phones could overlap and cannot show their Wi-Fi.
