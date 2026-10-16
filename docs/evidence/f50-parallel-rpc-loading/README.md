# Evidence for F50 — parallel loading across RPC endpoints: what exists upstream and in forks

Research of 2026-10-15. **Nothing here ran a patch**; this directory holds what could be checked in the container.

| File | What it is |
|---|---|
| `apply_check.txt` | `git apply --check` of the diffs of upstream PR #26291 and PR #20062 against the pinned llama.cpp tree (`4da6337`, with the two `native/split` patches applied in the working tree). #26291 applies cleanly; #20062 fails on `common/common.h` and the loader files. The check changes nothing. The diffs are identified by size and sha256 and are **not committed** |
| `tensor_size_bands.py`, `tensor_size_bands.out` | the share of the 3B model's weight bytes by tensor size, against RPC's hash threshold (10 MiB upstream, 1 MiB in our `llama-cache.patch`). 89.0% of bytes are in tensors above 10 MiB, 9.8% in 1-10 MiB, 1.3% at or below 1 MiB; `token_embd` (315.3 MiB) stays on the host |

**How the third-party material was obtained.** The container cannot read `github.com/ggml-org` pages (HTTP 403) or
`raw.githubusercontent.com`. Issue, PR and fork pages were read through the page-fetch tool, which returns a
summary written by a small model: treat every statement about those pages as **secondhand**. The two PR diffs
were downloaded from `patch-diff.githubusercontent.com/raw/ggml-org/llama.cpp/pull/<n>.diff` and read directly. The
Turboload fork's `ggml-rpc.cpp` (3,060 lines) was shown truncated after about 1,000 lines, so its server side
was **not** seen. The links to the Turboload archive and branch were supplied by the operator.

Pages read: `ggml-org/llama.cpp` PRs #20062, #26291, #18626, #16276 and issues #25890, #16434, #10095;
`luxemque/turboload.llama.cpp` (`README.md`, `PARALLEL_RPC_LOADING.md`) and `luxemque/llama.cpp` branch
`parallel-rpc-loading` (compare view, `ggml-rpc.h`, the first part of `ggml-rpc.cpp`); the prima.cpp paper
(arXiv 2504.08791).
