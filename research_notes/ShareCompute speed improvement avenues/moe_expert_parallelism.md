# MoE inference techniques and expert-aware placement for a layer-split RPC pipeline (laptop + Android + iPhone)

Date of research: 2026-10-21. Method note: GitHub pages were read through WebFetch, which returns a small model's summary of the page ("fetched, secondhand"); `mcp__github` is bound to the ShareCompute repo only, so ggml-org content could not be read directly. arXiv `/abs` pages give abstracts only; two papers were read via `/html`. Local project facts are cited to `findings.md` (F43/F46/F49/F50) and `CLAUDE.md`. Derived numbers marked "my arithmetic" were computed from cited inputs. Anything I could not source is marked UNVERIFIED or sits under Gaps.

Pin facts used throughout: llama.cpp pin `4da6337...` was first committed to `native/split/llama-revision.txt` on **2026-09-28** (`git log -S`, commit f4e677b). Probe uses `LLAMA_SPLIT_MODE_LAYER`, `n_gpu_layers=999`, `tensor_split` shares (`native/split/probe.cpp:56-59`). Worker budgets 768 / 2048 / 1536 MiB (sum 4,352 MiB); demonstrated pooled weights 3,209.1 MiB (F43).

---

## (a) What MoE support exists in llama.cpp today, and what works over ggml-rpc

### Takeaway
Upstream llama.cpp has mature *placement* flags for MoE (`-ot`, `--cpu-moe`, `--n-cpu-moe`), a merged-in-Oct-2026 *GPU* LRU expert cache (`--moe-cache-mib`, PR #29887), and an experimental tensor-parallel mode (`-sm tensor`, PR #19378). All of it is built around GPU-vs-CPU-RAM offload; **none is expert-parallel across network peers, and none helps CPU-only RPC workers**. The expert-cache PRs postdate the project's pin. Whether any of it works over ggml-rpc is mostly undocumented; MoE-over-RPC has never been run in this project.

### Cited Findings
- `-cmoe/--cpu-moe` keeps all MoE weights on CPU; `-ncmoe/--n-cpu-moe N` keeps the MoE weights of the first N layers on CPU; `-ot/--override-tensor` is the general tensor-to-buffer-type mechanism (llama-server man page) — [llama-cli man page](https://dyn.manpages.debian.org/unstable/llama.cpp-tools/llama-cli.1.en.html)
- PR #15077 "llama : add --n-cpu-moe option" (author slaren) merged 2025-08-04; it works by adding tensor overrides; it follows `--cpu-moe` (#14992) and the page mentions neither RPC nor mmap; `--override-tensor` rules given earlier take priority — [PR #15077](https://github.com/ggml-org/llama.cpp/pull/15077) (fetched)
- `-ot` takes `regex=buffer_type`, first matching rule wins; `exps=CPU` targets routed-expert tensors; `--cpu-moe` is equivalent to `-ot "exps=CPU"`; the shared expert stays with attention in the basic setup; the guide gives **only per-tensor / per-layer placement and says nothing about splitting individual experts inside a tensor**. Literal tensor names given: `blk\.(19|[2-9][0-9])\.ffn_(up|gate|down)_exps\.weight` — [Doctor-Shotgun MoE offload guide](https://huggingface.co/blog/Doctor-Shotgun/llamacpp-moe-offload-guide)
- Expert weights are stored as one fused tensor holding all experts, introduced by a 2024 rework of `mul_mat_id` (commit 08a0c020, touching CUDA, Metal and loaders for merged-expert models) — [semanticdiff view of commit 08a0c020](https://app.semanticdiff.com/gh/ggml-org/llama.cpp/commit/08a0c0206075556e82aca0feafad530dcc5f1426) (search snippet only; exact tensor shapes not fetched)
- **PR #29887 "llama : add a GPU cache for MoE experts kept in host memory"** (am17an; port of the qvac-fabric cache): **merged by ggerganov 2026-10-07** (opened 2026-10-03). `MUL_MAT_ID` on host-resident experts runs on the GPU from an LRU cache; only misses are uploaded; applies to batches of at most 32 tokens; flag `--moe-cache-mib N` (default 0; author suggests ~10% of expert size). Author benchmarks (Qwen3.8-Flash-Next Q4_0, 93.7 GiB, EPYC 7742): RTX 5090 30.8 -> 54.5 t/s (`--fit`, 77% hit) and -> 67.8 t/s (`-cmoe`, 19,000 MiB cache, 89% hit). Tested on CUDA and one HIP report; a Vulkan load crash and slowdown reported; **RPC support not confirmed by the page** (a user mentioned an RPC setup with no clean comparison, no maintainer answer) — [PR #29887](https://github.com/ggml-org/llama.cpp/pull/29887) (fetched)
- Follow-up PR #30112 (multi-GPU cache) merged 2026-10-08 per a third-party blog that also lists regressions (GLM 5.3 Flash two-GPU 33 -> 16 t/s at 43.5% hit rate; RTX 3090 cache only 1-5% faster than a tuned `--n-cpu-moe 44`; payoff threshold ~75% hit rate) — [DevelopersDigest](https://www.developersdigest.tech/blog/llama-cpp-moe-offload-n-cpu-moe-vs-moe-cache-2026) (secondary; PR #30112 page itself not read)
- **PR #26563 "expert hot store" (`-ehs/--expert-hot-s`)**: open, unmerged, CUDA-only, off by default, reported corrupt output on Vulkan; author-reported decode gains on an 8 GB card (Qwen3.6-35B-A3B 33.25 -> 56.0 t/s) — [PR #26563](https://github.com/ggml-org/llama.cpp/pull/26563), [dev.to write-up](https://dev.to/breachprotocol/a-llamacpp-patch-learns-which-experts-to-keep-in-vram-while-you-type-374n) (search-result summaries; pages not fetched)
- **PR #19378 "ggml: backend-agnostic tensor parallelism (experimental)"** (JohannesGaessler): merged 2026-04-09; `--split-mode tensor` via a "meta" backend; two AllReduces per transformer layer; needs FlashAttention on, KV quantization off, `-fit` unsupported; "not yet production ready"; NUMA-aware CPU parallelism planned, not implemented. The February PR text listed `olmoe` and `deepseek2` among architectures "definitely broken" under `-sm tensor`; later commits added Qwen3 MoE, Qwen3.5, GPT-OSS, Gemma 4 MoE. The page does not say whether expert tensors are split by expert or by FFN dimension (it mentions a Qwen-30B-A3B granularity fix for intermediate dim 768, which suggests FFN-dimension splitting — inference). The author did not address RPC — [PR #19378](https://github.com/ggml-org/llama.cpp/pull/19378) (fetched, first 100k of 133k characters)
- Project's own run: `LLAMA_SPLIT_MODE_TENSOR` works over the project's RPC workers with equal tokens (dense models only) but needs ~145 direction switches per token per link vs ~2 for layer split and is 2.1-2.8x slower on loopback, 5.6-11x slower on an emulated phone link, because `ggml_backend_rpc_buffer_cpy_tensor` returns false across servers and RPC exposes no all-reduce — [F49](file:///home/user/ShareCompute/findings.md) (section "F49", point 3)
- Issue #19825 "Managed SSD offloading for MoE" (closed by author 2026-02-26, no maintainer replies visible): on a 24 GB M4 Pro, llama.cpp falling back on OS swap with MoE models larger than RAM caused kernel panics; reporter cited ik_llama.cpp `--cpu-moe` numbers (Qwen 30B IQ4_KSS 35+ t/s) — [Issue #19825](https://github.com/ggml-org/llama.cpp/issues/19825) (fetched)
- **No upstream llama.cpp issue or PR proposing expert parallelism (experts of one layer on different network peers) turned up** in two targeted searches; results were only general EP literature (vLLM/SGLang), e.g. the [SGLang EP doc](https://www.mintlify.com/sgl-project/sglang/distributed/expert-parallelism), which defines EP generically as experts spread over devices with all-to-all dispatch and combine. Absence of a hit in a US-only keyword search is not proof that no such issue exists.
- **Fork status:** ik_llama.cpp (ikawrakow) is a fork, not upstream: fused MoE (`-fmoe`), tensor overrides, run-time repack (`-rtr`, README warns against it with CPU-resident experts), "Smart Expert Reduction" (PR 239, Mar 2025, changes which experts run, so output differs) — [ik_llama.cpp on Codeberg](https://codeberg.org/ikawrakow/illama), [Doctor-Shotgun guide](https://huggingface.co/blog/Doctor-Shotgun/llamacpp-moe-offload-guide), [SourcePulse](https://www.sourcepulse.org/projects/1843811) (search summaries)
- **Colibri** (JustVugg/colibri) is **an independent pure-C engine, not a llama.cpp fork**: reads safetensors (GGUF not mentioned), keeps dense layers in RAM and reads routed experts from disk on demand, per-layer LRU plus a pinned "hot set" learned from usage; supports OLMoE, Qwen3.x, GLM, DeepSeek, Kimi; Linux/macOS/Windows (no Android/iOS mentioned); measured: OLMoE 22-23 tok/s on CPU (hardware not stated in the fetched text), Qwen3.6-35B-A3B 6.0 tok/s CPU, GLM-5.2 0.05-0.1 tok/s cold on a 25 GB laptop — [Colibri README](https://github.com/JustVugg/colibri) (fetched; secondary description in [Pinggy](https://pinggy.io/blog/colibri_glm_5_2_744b_model_25gb_ram/))
- Colibri has an opt-in "local cluster mode" where "disk-backed expert workers execute routed FFNs on other Macs"; the coordinator keeps routing, token generation and KV local; "a layer's routed batch-union is sent as one persistent TCP request, so a token does not incur one round trip per expert" (i.e., one exchange **per layer per worker**, not per token) — [Colibri cluster doc](https://github.com/JustVugg/colibri/blob/main/docs/cluster.md) (fetched; no throughput or byte counts given there)
- The pin predates the merged expert cache: pin first committed 2026-09-28, PR #29887 opened 2026-10-03 — [git history of native/split/llama-revision.txt](file:///home/user/ShareCompute/native/split/llama-revision.txt) and [PR #29887](https://github.com/ggml-org/llama.cpp/pull/29887). `-ot`, `--cpu-moe`, `--n-cpu-moe` (2025) and `-sm tensor` (Apr 2026) are in the pin.

### Inferences
- `--moe-cache-mib` / `-ehs` do nothing for this project: they cache host-RAM experts into **GPU** memory, and every ShareCompute worker is CPU-only. They also are not in the pin.
- `--cpu-moe`, `--n-cpu-moe` decide GPU-vs-CPU placement. With CPU-only workers they are no-ops; the useful analogue is `-ot` (API form: `llama_model_params.tensor_buft_overrides`, UNVERIFIED at the pin), which could pin whole `ffn_*_exps` tensors of chosen layers to an RPC buffer type named `RPC0[endpoint]` (F46 naming). That is **per-layer** FFN offload, not per-expert placement.
- MUL_MAT_ID over ggml-rpc: the server runs the ordinary CPU backend, which implements `MUL_MAT_ID`, so a MoE model split by layer *should* work exactly like a dense one. No source in this session confirms it, and the project has never run an MoE model. Treat as **UNVERIFIED but cheap to test on loopback**.

### Gaps
- Whether MoE (MUL_MAT_ID, router top-k/argsort) graphs run correctly over ggml-rpc at the pin: no authoritative source found. A Level1Techs thread ("Running qwen 3.6 / 3.8 on 3090+3080 over RPC?") appeared in search but was not read.
- PR #30112 and PR #26563 were not fetched directly; #29887's RPC answer is "unclear".
- Whether `-sm tensor` splits experts by expert index or FFN dimension: not stated in the part of PR #19378 that was read.
- Could not read ggml-org source at the pin (github MCP bound to ShareCompute; F49/F50 already note HTTP 403 for ggml-org from the container).
- exo: no result about MoE or expert parallel support was found; treat as UNVERIFIED/no evidence.

---

## (b) MoE models small enough to matter for a ~3.2 GiB pooled budget

### Takeaway
Only a handful of MoE models fit: OLMoE-1B-7B (Q3_K_M 3,189 MiB, Q4_K_M 4,018 MiB), Granite 3.1 3B-A800M (Q8_0 3,350 MiB, Q4_K_M 1,923 MiB), Granite 4.0-H-Tiny (Q3_K_M 3,198 MiB), SmallThinker-4B-A0.6B (Q4 about 2.3-2.5 GiB). Qwen1.5-MoE-A2.7B, DeepSeek-V2-Lite and everything 8B-total or larger exceed the pool at any sensible quantization. Active-parameter counts of 0.4-1.3B mean roughly 4x fewer weight bytes read per token than the dense 3B Q8 test, but no published llama.cpp CPU number exists for these models on phone-class hardware.

### Cited Findings
GGUF file sizes (bytes from the Hugging Face file listings; MiB = my arithmetic; compare with 3,209.1 MiB demonstrated pool and 4,352 MiB budget sum):

| Model (total params; active) | Quant: size | Fits 3,209 MiB / 4,352 MiB? |
|---|---|---|
| OLMoE-1B-7B-0125-Instruct (6.92B; ~1.3B) | Q2_K 2,444 MiB; Q3_K_M 3,189; Q4_0 3,746; Q4_K_M 4,018; Q8_0 7,019 | Q3_K_M just under 3,209; Q4_0/Q4_K_M only under 4,352 |
| Granite 3.1 3B-A800M-instruct (3.30B; 0.8B) | Q4_0 1,804; Q4_K_M 1,923; Q8_0 3,350 | Q4 easily; Q8_0 is 98.7% of F43's 3B Q8_0 footprint (3,448.6 MiB) |
| Granite 3.1 1B-A400M (1.33B; 0.4B) | (not listed; smaller than the above) | yes |
| Granite 4.0-H-Tiny (6.94B hybrid Mamba+MoE; ~1B by name) | Q2_K 2,466; Q3_K_M 3,198; Q4_K_M 4,035; Q8_0 7,048 | Q3_K_M borderline |
| SmallThinker-4B-A0.6B (4.27B; 0.6B by name) | Q4_0 2,358; Q4_K 2,508; Q6_K 3,350; Q8_0 4,336 | Q4/Q6 yes |
| LFM2.5-8B-A1B (8.34B+; ~1B by name) | Q4_0 4,620; Q4_K_M 4,917; Q8_0 8,593 | no (over 4,352) |
| Qwen1.5-MoE-A2.7B-Chat (14.3B; 2.7B) | Q2_K 5,617; Q3_K_M 7,093 | no |
| DeepSeek-V2-Lite-Chat (15.7B; ~2.4B) | Q2_K 6,133; Q4_K_M 9,884; Q8_0 15,929 | no |
| Qwen3-30B-A3B (30.5B; ~3B) | Q4_K_M about 17-18 GB | no (about 5x the pool) |
| SmallThinker-21B-A3B (21.5B; ~3B) | Q4_0 11.47 GiB | no |

Sources for the table: sizes from [OLMoE GGUF](https://huggingface.co/allenai/OLMoE-1B-7B-0125-Instruct-GGUF), [Granite 3.1 3B-A800M GGUF](https://huggingface.co/bartowski/granite-3.1-3b-a800m-instruct-GGUF), [Granite 4.0-H-Tiny GGUF](https://huggingface.co/ibm-granite/granite-4.0-h-tiny-GGUF), [SmallThinker-4B GGUF](https://huggingface.co/Tiiny/SmallThinker-4BA0.6B-Instruct-GGUF), [LFM2.5-8B-A1B GGUF](https://huggingface.co/LiquidAI/LFM2.5-8B-A1B-GGUF), [Qwen1.5-MoE-A2.7B-Chat GGUF](https://huggingface.co/tensorblock/Qwen1.5-MoE-A2.7B-Chat-GGUF), [DeepSeek-V2-Lite-Chat GGUF](https://huggingface.co/mradermacher/DeepSeek-V2-Lite-Chat-GGUF); parameter totals from the Hugging Face model pages: [OLMoE](https://huggingface.co/allenai/OLMoE-1B-7B-0125-Instruct), [Qwen1.5-MoE](https://huggingface.co/Qwen/Qwen1.5-MoE-A2.7B), [DeepSeek-V2-Lite](https://huggingface.co/deepseek-ai/DeepSeek-V2-Lite), [Granite 3.1 3B-A800M](https://huggingface.co/ibm-granite/granite-3.1-3b-a800m-instruct), [Granite 3.1 1B-A400M](https://huggingface.co/ibm-granite/granite-3.1-1b-a400m-instruct), [Granite 4.0-H-Tiny](https://huggingface.co/ibm-granite/granite-4.0-h-tiny), [LFM2-8B-A1B](https://huggingface.co/LiquidAI/LFM2-8B-A1B), [SmallThinker-4B](https://huggingface.co/Tiiny/SmallThinker-4BA0.6B-Instruct), [SmallThinker-21B](https://huggingface.co/Tiiny/SmallThinker-21BA3B-Instruct), [Qwen3-30B-A3B](https://huggingface.co/Qwen/Qwen3-30B-A3B). Qwen3-30B-A3B Q4_K_M ~17-18 GB from [LM Studio](https://lmstudio.ai/models/qwen/qwen3-30b-a3b) / [LocalClaw](https://localclaw.io/models/qwen3-30b-a3b); SmallThinker-21B q4_0 11.47 GiB (developer's own table) from [SmallThinker model card](https://huggingface.co/PowerInfer/SmallThinker-21BA3B-Instruct/blob/45e458513a7e3831500b12a9e9467f317ce946b8/README.md). "Active" figures follow the model names; they are not re-verified against model cards except OLMoE (computed below).

- Architecture configs (hidden size d, layers L, experts E, top-k, expert FFN width): OLMoE d=2048, L=16, E=64, k=8, ff=1024, vocab 50,304 — [OLMoE config](https://huggingface.co/allenai/OLMoE-1B-7B-0125-Instruct/resolve/main/config.json); Granite 3.1 3B-A800M d=1536, L=32, E=40, k=8, ff=512, vocab 49,155 — [config](https://huggingface.co/ibm-granite/granite-3.1-3b-a800m-instruct/resolve/main/config.json); Qwen1.5-MoE d=2048, L=24, 60 routed experts top-4 plus a shared expert (width 5632), ff=1408 — [config](https://huggingface.co/Qwen/Qwen1.5-MoE-A2.7B/resolve/main/config.json); DeepSeek-V2-Lite d=2048, L=27 (first layer dense), 64 routed top-6 plus 2 shared, ff=1408 — [config](https://huggingface.co/deepseek-ai/DeepSeek-V2-Lite/resolve/main/config.json); Qwen3-30B-A3B d=2048, L=48, E=128, k=8, ff=768, 4 KV heads — [config](https://huggingface.co/Qwen/Qwen3-30B-A3B/resolve/main/config.json)
- Published CPU speeds (all vendor/author-reported, none on phone-class CPUs): Colibri OLMoE 22-23 tok/s and Qwen3.6-35B-A3B 6.0 tok/s on CPU (own engine, hardware not stated in fetched text) — [Colibri README](https://github.com/JustVugg/colibri); SmallThinker model card: 21B-A3B q4_0 30.2 tok/s vs Qwen3-30B-A3B 33.5 tok/s on an i9-14900 — [SmallThinker card](https://huggingface.co/PowerInfer/SmallThinker-21BA3B-Instruct/blob/45e458513a7e3831500b12a9e9467f317ce946b8/README.md); distributed-llama lists Qwen3-30B-A3B Q40 (17.0 GB) as a launch target and one secondary report gives 14.33 tok/s eval on one root plus three workers, all Raspberry Pi 5 8 GB on a switch — [distributed-llama README](https://cdn.jsdelivr.net/gh/b4rtaz/distributed-llama@main/README.md), [hada.io report](https://news.hada.io/topic?id=22984); Pi 5 8 GB dense Llama 3.2 3B CPU: 4.61 tok/s — [geerlingguy/ai-benchmarks](https://github.com/geerlingguy/ai-benchmarks)
- Project baseline for the dense 3B Q8_0 split: physical decode 11,624 ms (cold) / 13,621 ms (warm) for 16 tokens (0.73 / 0.85 s per token, about 1.2-1.4 tok/s); loopback 7,027 ms (0.44 s per token); 3,448.6 MiB model — [F43](file:///home/user/ShareCompute/findings.md); per-node compute has never been measured — [F49](file:///home/user/ShareCompute/findings.md)

### Inferences
- **OLMoE arithmetic from its config (mine):** expert params 64 x 16 x 3 x 2048 x 1024 = 6.44B (93% of 6.92B total); attention 0.27B; embeddings plus head 0.21B; active per token = 8 experts x 16 layers + dense = 1.28B, which agrees with the "1B-7B" name. Q4_K_M averages 4.87 bits per weight, so about 0.72 GB of weights are read per token (embedding lookup excluded), against about 3.3 GB for the dense 3B Q8_0: **about 4.6x fewer bytes per token**. Granite 3B-A800M Q8_0: roughly 0.85 GB/token, about 3.9x fewer, at an almost identical file size to F43's model.
- If decode were purely weight-bandwidth-bound, loopback 0.44 s/token would fall to roughly 0.11 s. Physical decode is 0.73-0.85 s, so the 0.29-0.41 s gap to loopback is unexplained overhead that an MoE would not shrink (network, relay, slower phone cores). Subtracting only the removable compute gives about 0.4-0.5 s/token, i.e. **roughly 2-2.5 tok/s versus 1.2-1.4 now, a 1.3-2x range**. If phone cores are the slow part the gain is larger; if `mul_mat_id` kernels are less efficient than dense GEMV on ARM, it is smaller. This is a derived estimate, not a measurement.
- Compute-per-token falls but the last link's logit return shrinks too: vocab 49-50k x 4 B = about 0.2 MB versus 0.608 MB for Qwen2.5-3B (F49).
- Quality per resident byte: MoE at Q3 holds 7B total parameters in the same footprint as a 3B dense Q8, but I found no benchmark comparing OLMoE-Q3 or Granite-3B-A800M to Qwen2.5-3B; whether the MoE is "better" is UNVERIFIED.
- The F37/F43 gate demands tokens equal to a pinned reference across x86 and ARM; F37/F40 already flag near-tie drift. MoE adds a discrete top-k routing decision per layer, so a tiny numeric difference can flip an expert and diverge sooner (my inference, not measured).

### Gaps
- No llama.cpp tokens/s for OLMoE, Granite MoE or SmallThinker on Android/iOS-class CPUs was found (one search for OLMoE phone/Pi benchmarks returned nothing relevant).
- Active-parameter counts for Granite, SmallThinker, LFM2.5, DeepSeek-V2-Lite were taken from names/papers and not confirmed from model cards.
- Whether llama.cpp at the pin handles `granitemoehybrid` (Mamba state) layers split across RPC devices is UNVERIFIED.
- MoE quantization sensitivity at Q2/Q3 (needed to judge OLMoE Q3_K_M): not researched.

---

## (c) Traffic and memory: expert-split versus layer-split in a star topology

### Takeaway
No evidence was found that expert placement reduces per-token network traffic relative to layer split; the evidence points the other way. Layer split costs about one 8 KB exchange per link per token. Expert parallelism needs an activation exchange **per MoE layer** with every device owning a selected expert (16-48 exchanges per token for the candidate models), which is a latency problem rather than a bandwidth problem. What expert placement does reduce is resident memory per device and, in principle, serial compute, neither of which layer split fails to deliver for capacity.

### Cited Findings
- Layer split in this project: client-to-worker 0.011 MB per token per link, worker-to-client 0.008 MB (0.608 MB on the last link, which carries F32 logits, 151,936 x 4 B); activations about 8 KB; about 2 direction switches per link per token — [F49](file:///home/user/ShareCompute/findings.md) (point 3 table)
- Tensor split (73 all-reduces per token on a 36-layer model) costs 145-289 direction switches per token per link and is 2.1-2.8x slower with zero network and 5.6-11x slower on an emulated phone link; RPC has no device-to-device path, so a phone-to-phone copy goes phone, relay, laptop probe, relay, phone — [F49](file:///home/user/ShareCompute/findings.md)
- **The only measured distributed edge-MoE system with expert placement (OD-MoE, Mixtral-8x7B, top-2, 32 layers, 10 RTX 3090 nodes, 1 Gbps LAN)**: main node runs attention, gating and normalization; workers hold experts. The embedding exchanged between main node and a worker is **about 16 KB per token per layer**, plus about 8 KB per token per layer of KV-cache alignment; expert weights are not sent over the network. No total bytes-per-token figure is given. Throughput: Transformers (all experts cached) 4.89 tok/s, OD-MoE 3.69 (75.5%), AdapMoE 3.13, Mixtral-Offloading 2.24, llama.cpp (CPU) 0.82, HOBBIT 0.79, MoE-Infinity 0.69 — [OD-MoE arXiv HTML](https://arxiv.org/html/2512.03927v1)
- Colibri cluster mode sends a layer's routed batch-union as one persistent TCP request per layer per worker — [Colibri cluster doc](https://github.com/JustVugg/colibri/blob/main/docs/cluster.md)
- Expert parallelism in general needs two all-to-all exchanges per MoE layer (dispatch tokens to experts, combine results) — [SGLang EP doc](https://www.mintlify.com/sgl-project/sglang/distributed/expert-parallelism) (search summary; generic)
- Prism (arXiv 2508.12851, "Accelerating Edge Inference for Distributed MoE Models with Latency-Optimized Expert Placement"): activation-aware expert placement across heterogeneous GPU edge servers; latency reductions up to 30.6% (DeepSeek-V2-Lite, BigBench: 6.63 s vs EPLB 9.56 s) but only 3.2% on Mixtral (BigBench: 2.09 s vs 2.16 s) and 2.0% on Mixtral (MultiData: 9.17 s vs 9.36 s); the testbed is **one machine with four A100s simulating three servers over a Docker network capped at 500 Mbps**; baselines are other expert-placement schemes (Uniform, Redundance, SmartMoE, EPLB); **pipeline/layer placement is not compared**; communication reduction is "significant" but unquantified in the text — [Prism arXiv HTML v4](https://arxiv.org/html/2508.12851v4)
- Layer-split capacity is already fully proportional to budgets: laptop 562.2 MiB, Android 1,348.3 MiB, iPhone 1,298.6 MiB held, combined 3,209.1 MiB against a 2,048 MiB largest budget — [F43](file:///home/user/ShareCompute/findings.md)
- Android visible RAM is 3,472 MiB (available 968-1,398 MiB at session start, fell to 282-439 MiB during 3B runs); the iPhone's measured link rate was about 6.1 MB/s and F49 emulated 2.5 ms one-way, 48 Mbit/s per phone; Wi-Fi round-trip time has never been measured in the project — [F49](file:///home/user/ShareCompute/findings.md), [F46](file:///home/user/ShareCompute/findings.md), [F50](file:///home/user/ShareCompute/findings.md)

### Inferences
Per-token activation traffic, star topology with the laptop as coordinator (attention, router, embeddings, head) and phones owning experts, F32 hidden state (my arithmetic: round trip = hidden state plus k expert ids and weights out, partial sum back):

| Model | d | MoE layers L | top-k | EP bytes per token per owning phone | EP sequential exchanges per token | Layer split per link |
|---|---|---|---|---|---|---|
| OLMoE-1B-7B | 2048 | 16 | 8 | 257 KiB | at least 16 | 8 KiB each way, 1 exchange |
| Granite 3B-A800M | 1536 | 32 | 8 | 386 KiB | at least 32 | 6 KiB each way, 1 exchange |
| Qwen1.5-MoE-A2.7B | 2048 | 24 | 4 | 385 KiB | at least 24 | 8 KiB, 1 |
| DeepSeek-V2-Lite | 2048 | 26 | 6 | 417 KiB | at least 26 | 8 KiB, 1 |
| Qwen3-30B-A3B | 2048 | 48 | 8 | 771 KiB | at least 48 | 8 KiB, 1 |

- Bytes are small on any link (about 0.25-0.8 MB per token), so bandwidth is not the issue; **synchronization count is**. At RTT = 5 ms (the F49 emulation's value, not a measurement) the OLMoE floor is 16 x 5 = 80 ms/token (12.5 tok/s ceiling, one RTT per layer, serial); if each layer needs set-input, compute, get-output as three blocking RPC calls (as F49's ~2 switches per all-reduce suggests, UNVERIFIED for an expert-offload graph), the ceiling falls to about 4 tok/s. At RTT = 1 ms the ceilings are 62 and 21 tok/s: expert parallelism only becomes plausible on wired, sub-millisecond links.
- Layer split on OLMoE needs about 0.72 GB of weight reads per token spread over three devices plus two hops: tens of ms of compute plus two RTTs. Expert parallelism could cut the serial expert compute by at most the number of owning devices (two phones), saving at most about a third to a half of ~70 ms, while adding at least 14 extra RTTs. At any Wi-Fi RTT of 2 ms or more it loses. This is a back-of-envelope comparison from cited inputs, not a measurement.
- Expert placement therefore reduces *resident memory per device* only in the sense that layer split already does; the pooled total is identical. Its real advantage in the literature is against **moving expert weights** (offloading) or against **tensor parallelism** (all-reduce of every matmul), not against pipeline/layer split. The source report's "far less data movement per token" is supported only versus those two; versus layer split it is **unsupported, and the activation traffic per token is higher**.
- Expert-parallel memory shape: the coordinator must hold all non-expert weights plus KV cache. For OLMoE that is about 7% of weights (about 0.3 GB at Q4_K_M, fits the laptop's 768 MiB), but the 93% expert share at Q4_K_M (about 3,740 MiB by my arithmetic) exceeds the two phones' 3,584 MiB budget sum, so the laptop would have to host experts too, or the quant drops to Q3_K_M (about 2,970 MiB of experts).
- Load imbalance: with top-8 of 64 spread across two owners (37/27 by budget), the critical path each layer is the busier owner, so realised parallelism is below 2x; Prism's whole premise is activation-skew-aware placement, which requires routing statistics.

### Gaps
- No byte or time measurement of an expert-offload graph over ggml-rpc exists; RTT over the project's relay is unmeasured.
- OD-MoE gives no total per-token bytes; Prism's per-token communication volumes are in figures, not text.
- No source compares expert-parallel to pipeline/layer split on consumer or edge hardware (searched; none found).

---

## Published and prototyped systems for MoE on edge or heterogeneous devices

### Takeaway
Nearly all published work attacks "experts do not fit in fast memory" (GPU VRAM or device RAM) by caching/prefetching/quantizing experts or by running them where they live; the few distributed ones (OD-MoE, Prism, Colibri cluster) either need fast LAN/GPU nodes or are simulated. None measured against a layer-split pipeline over Wi-Fi phones.

### Cited Findings
- **EdgeMoE**: on-device engine for mobile/edge; expert weights in external storage fetched only when activated, expert-wise bitwidth adaptation, expert preloading; abstract states memory savings and speedup without numbers — [EdgeMoE abstract](https://arxiv.org/abs/2308.14352)
- **Fiddler**: CPU-GPU orchestration that runs experts on the CPU instead of copying weights; 1.26x single-batch, 1.30x long prefill, 11.57x beam search over baselines — [Fiddler abstract](https://arxiv.org/abs/2402.07033)
- **ProMoE**: proactive expert prefetch using intermediate results; 2.20x average prefill (up to 3.21x) and 2.07x decode (up to 5.02x) speedups over offloading baselines — [ProMoE abstract](https://arxiv.org/abs/2410.22134)
- **HOBBIT**: token-level mixed-precision expert loading, swapping low-priority cache-miss experts for low-precision versions; up to 9.93x decode speedup over offloading baselines on "edge devices" (devices not named on the abstract page) — [HOBBIT abstract](https://arxiv.org/abs/2411.01433)
- **OD-MoE**: on-demand expert loading with no expert cache across distributed edge nodes; claims 99.94% expert-activation prediction accuracy several layers ahead, ~75% of fully GPU-cached decode speed with 1/3 of the GPU memory, under 1 GB GPU per worker; testbed is ten RTX 3090 nodes on 1 Gbps (see section c) — [OD-MoE abstract](https://arxiv.org/abs/2512.03927), [HTML](https://arxiv.org/html/2512.03927v1)
- **Prism / DanceMoE** (see section c) — [arXiv 2508.12851](https://arxiv.org/abs/2508.12851)
- **MoE-SpAc** (arXiv 2603.09983): speculative decoding as lookahead for expert prefetch/eviction on heterogeneous edge, low-frequency experts to CPU — [MoE-SpAc](https://arxiv.org/pdf/2603.09983) (search summary only)
- **AdapMoE** appears only as a baseline row in OD-MoE's table (3.13 tok/s, 8 GB GPU) — [OD-MoE HTML](https://arxiv.org/html/2512.03927v1)
- **prima.cpp** (ICLR 2026): dense 30-70B models on heterogeneous home clusters; "pipelined-ring" parallelism overlapping disk I/O with compute and communication; Halda scheduler; reports 674 ms/token for a 70B model on four devices and 5-17x lower time-per-output-token than llama.cpp, exo and distributed-llama (authors' claim). It is **not MoE** and not a patch; it loads each device's layers from local files — [ICLR page](https://proceedings.iclr.cc/paper_files/paper/2026/hash/e991e5587c1daa49bbf9a818b3f02f9a-Abstract-Conference.html), [arXiv](https://arxiv.org/pdf/2504.08791v2); also noted in [F50](file:///home/user/ShareCompute/findings.md)
- **distributed-llama** requires 2^n nodes with the node count capped by KV heads, and supports Qwen3-30B-A3B Q40 — [README](https://cdn.jsdelivr.net/gh/b4rtaz/distributed-llama@main/README.md)

### Inferences
- The one wired-LAN data point (distributed-llama, Pi 5 x4, ~14 tok/s on a 3B-active model, secondary) shows MoE across commodity boards can work when workers talk peer-to-peer through a switch; the project's star-through-relay Wi-Fi topology and RPC's lack of peer links (F49) are the opposite conditions.
- The offloading papers (EdgeMoE, Fiddler, ProMoE, HOBBIT) address a GPU-memory/host-memory bandwidth gap that ShareCompute's CPU-only workers do not have; their gains do not transfer.

### Gaps
- MoE-Lightning, and exo's MoE behaviour, were not retrieved. EdgeMoE/Fiddler/ProMoE/HOBBIT abstract pages omit hardware; full texts not read.
- No paper found that measures MoE on phones over Wi-Fi in a star topology.

---

## (d) Page-cache / on-demand expert streaming from phone storage

### Takeaway
Streaming experts from storage is real but is a *capacity* technique that trades speed for it: measured ~2-3 tok/s on a Jetson with 1.27 GB/s NVMe and 0.05-0.1 tok/s cold on a 25 GB laptop in the best-documented cases. It would not fit the current design: ShareCompute workers do not mmap model files, the phone cache re-reads and re-hashes whole tensors on every hit, and expert slices are about 1 MB, right at the 1 MiB cache threshold. Nothing at the pin supports expert-granular loading.

### Cited Findings
- On stock llama.cpp, mmap'd MoE experts that the router does not select are never paged in, so models larger than RAM can run from SSD; this is why MoE makes SSD-backed inference possible, but write-ups give no controlled mmap vs `--no-mmap` comparison — [search results summary](https://viblo.asia/p/running-moe-llms-larger-than-your-ram-a-practical-guide-to-mmap-expert-offloading-and-ssd-streaming-with-llamacpp-pPLkNQQNJRZ) (secondary)
- Stock llama.cpp relies on the OS swap/page cache and it misbehaves when the router jumps between experts on a RAM-starved Mac (kernel panics on 24 GB M4 Pro) — [Issue #19825](https://github.com/ggml-org/llama.cpp/issues/19825)
- Jetson Orin Nano Super 8 GB, NVMe with ~1.27 GB/s random-read ceiling, Gemma 4 26B-A4B QAT Q4 (13.3 GiB): custom llama.cpp patch using `pread` into pinned slots with a per-layer LFU cache (not mmap) gives **2.95 tok/s** versus **1.94 tok/s** for `--cpu-moe` mmap; cold prefill about a quarter of baseline speed; the author estimates an IO-bound limit of ~5 tok/s; peak RSS 6.7 GB; no hit rates reported; 64 slots (6 GB pinned) starved the system — [Jetson streaming write-up](https://dev.to/anyesh/running-a-26b-moe-on-an-8-gb-jetson-by-streaming-experts-from-ssd-2jpf)
- Colibri: dense layers resident, experts read from disk on demand, LRU plus learned pinned set; GLM-5.2 0.05-0.1 tok/s cold on a 25 GB laptop, 1.83 tok/s on 128 GB Ryzen AI Max+ — [Colibri README](https://github.com/JustVugg/colibri)
- PR #29887's cache achieves 72-89% hit rates with a cache of roughly 10-29% of total expert bytes (6.4-19 GB of 65.4 GB) on one model, but is a GPU cache — [PR #29887](https://github.com/ggml-org/llama.cpp/pull/29887)
- iOS: dirty memory counts toward the app's memory footprint and iOS terminates an app past its limit — [Apple: Reducing your app's memory use](https://developer.apple.com/documentation/xcode/reducing-your-app-s-memory-use.md); an Apple forum answer says an app can mmap a large file but that does not give significantly more usable memory, because iOS has no swap and caps address space — [Apple Developer Forums (jetsam / per-process limit)](https://developer.apple.com/forums/thread/54336) (search-result summary); a Chinese APM article classes memory-mapped files as clean memory — [Tencent Cloud article](https://cloud.tencent.cn/developer/article/1662232) (secondary)
- Project weight cache: threshold lowered to 1 MiB; every hit is hash-checked; warm runs re-read and re-hash the cached files, each phone's phase 34.9 s (iPhone) and 37.6 s (Android) one after the other; the loader uploads whole tensors through RPC buffers (not mmap, `.host_buffer = false`) — [F46](file:///home/user/ShareCompute/findings.md), [F50](file:///home/user/ShareCompute/findings.md) (point 4), [F43](file:///home/user/ShareCompute/findings.md)
- Tensor-size mix of the 3B Q8_0 model: 89.0% of RPC bytes sit in 109 tensors above 10 MiB; 1.3% below 1 MiB — [F50](file:///home/user/ShareCompute/findings.md)

### Inferences
- **Expert slice size vs the 1 MiB cache threshold (my arithmetic).** One expert's matrix is d x ff elements: OLMoE 2048 x 1024 at 4.87 bits per weight = about 1.28 MB (above 1.05 MB, cacheable per slice); Qwen3-30B-A3B 2048 x 768 at about 4.72 bits per weight = about 0.93 MB (below the threshold, uncacheable at slice granularity, as F49 saw for tensor-split row pieces: warm saving fell from -98.7% to -63%). A whole expert for one layer is ~3.8 MB (OLMoE Q4_K_M) or ~2.8 MB (Qwen3-30B).
- **Worst-case (all-miss) bytes per token** if every selected expert had to come from storage: OLMoE about 490 MB, Qwen3-30B-A3B about 1.07 GB. At a 1.27 GB/s device that is 0.4 s and 0.85 s per token; at the iPhone's measured 6.1 MB/s network rate (fetching experts from the laptop over Wi-Fi) it would be 80 s and 175 s per token. Phone flash rates were not measured or sourced, so only the network case is quantified. Realistic hit rates (70-90% in the cited cache PR, different model and hardware) divide those figures by 3-10.
- Streaming only makes sense for models much larger than the pool (Qwen3-30B-A3B at ~18 GB is 5.6x the 3.2 GiB demonstrated pool; hot-set caching would have to cover about 18% of expert bytes), and the evidence says expect low single-digit tok/s at best, achieved only after new code: expert-granular `set_tensor`/cache keys, expert-aware prefetch, and a way for the owning phone to compute its experts (which brings back the per-layer RTT problem of section c). None of that exists at the pin.
- For models that already fit (OLMoE Q3, Granite-3B), mmap/streaming adds nothing; the project's current "upload everything, hash-check, hold in RAM" path is the right shape, and F51's parallel loading is the relevant lever for load time.
- On a 4 GB laptop with a 768 MiB worker budget, mmap'd streaming of a larger model on the *host* side is conceivable (the host already maps `token_embd` as `CPU_Mapped`, 315.3 MiB, F46), but F45 measured the laptop's free RAM bottoming at 89.2 MiB, so page-cache headroom is the binding constraint (my inference).

### Gaps
- Android lmkd behaviour with large mmap'd page cache, and whether clean mapped pages count toward iOS jetsam limits: no authoritative Apple or Android doc found (the Apple results discussed dirty memory only). UNVERIFIED.
- Phone flash sequential/random read speeds for the operator's devices: not measured, not sourced.
- Whether sub-tensor `set_tensor` chunks are individually cached at the pin (the F38 hash cache works on payloads over the threshold): not confirmed from source this session.

---

## What an "expert-aware StagePlanner" would require in ggml terms

### Takeaway
At the pin, experts for a layer are three fused 3D tensors (`ffn_gate_exps`, `ffn_up_exps`, `ffn_down_exps`, expert index as the third dimension) that live in one buffer on one backend; there is no supported way to place different experts of one tensor on different backends. Per-layer FFN offload via `-ot`/tensor overrides is available; per-expert placement would need new ggml/llama.cpp code (graph decomposition into one `MUL_MAT_ID` per device with remapped ids plus a sum, or per-expert tensors), plus planner, cache and budget changes.

### Cited Findings
- Expert tensors are fused ("all experts share one tensor"), named `blk.N.ffn_{gate,up,down}_exps.weight`; `-ot` regex placement is per tensor — [commit 08a0c020](https://app.semanticdiff.com/gh/ggml-org/llama.cpp/commit/08a0c0206075556e82aca0feafad530dcc5f1426), [Doctor-Shotgun guide](https://huggingface.co/blog/Doctor-Shotgun/llamacpp-moe-offload-guide)
- Current planner: `StagePlanner` assigns contiguous layer ranges by largest-remainder shares and per-worker budgets; the native probe passes `tensor_split` shares with `LLAMA_SPLIT_MODE_LAYER` so llama.cpp itself assigns layers — [StagePlanner.swift](file:///home/user/ShareCompute/Sources/ShareComputeCore/StagePlanner.swift), [probe.cpp](file:///home/user/ShareCompute/native/split/probe.cpp)
- The RPC backend declares no all-reduce and no cross-server copy; RPC buffer types are named `RPC<dev>[endpoint]`; the loader walks contexts one endpoint at a time in endpoint-name order (F51 patch parallelizes this) — [F46](file:///home/user/ShareCompute/findings.md), [F49](file:///home/user/ShareCompute/findings.md), [F51](file:///home/user/ShareCompute/findings.md)
- `-sm tensor` already splits MoE models in some form (Qwen3 MoE, GPT-OSS) but over RPC it inherits F49's cost — [PR #19378](https://github.com/ggml-org/llama.cpp/pull/19378), [F49](file:///home/user/ShareCompute/findings.md)

### Inferences
Requirements, in rough order of size (all UNVERIFIED against pin source, which could not be read):
1. **Memory model**: split each layer's bytes into dense (attention, norms, router `ffn_gate_inp`, shared expert, KV cache, embeddings, head; replicated on the coordinator) and divisible expert bytes. Planner output becomes (layer, expert range, node), not a layer range. The current contiguous-layer logic does not express this.
2. **Placement mechanism**: either (a) per-layer only: `tensor_buft_overrides` / `-ot` to put whole `ffn_*_exps` tensors of chosen layers on an RPC buffer type — no per-expert granularity, no parallelism, one RTT-pair per offloaded layer; or (b) per-expert: a patch that builds one `MUL_MAT_ID` per owning device over that device's expert subset (ids remapped, non-owned ids masked) and sums the partial outputs, or re-splits the fused tensor at load into per-device tensors. The ggml scheduler splits by whole ops per backend, not by expert, so (b) is new graph-building code in `src/llama-graph.cpp`-equivalent plus load-time slicing.
3. **Routing profile**: hot-expert skew decides balance; Prism and Colibri both use measured activation heat. A profiling pass (not available today) is needed before placement.
4. **Budget and cache**: `llama-budget.patch` accounting works per buffer, so it extends; the 1 MiB cache threshold interacts with ~1 MB expert slices (see section d); the `tensor_split` shares vector pass is the code the F49 latent read-past-end sits in.
5. **Proof gate**: token equality across x86/ARM is more fragile with discrete top-k routing (see section b).
6. **Failure model**: an expert owner dying now stalls every layer of every token, versus one stage; the supervisor already discards failed generations (CLAUDE.md), so detection exists but each token touches every owner.

### Gaps
- Could not read the pinned `src/llama-graph.cpp`, `src/llama-model-loader.cpp` or `ggml-rpc.cpp` for MoE handling (no ggml-org access).
- Whether `tensor_buft_overrides` reaches RPC buffer types at the pin was not checked.

---

## Net verdict for ShareCompute (laptop 768 MiB, Android 2,048 MiB, iPhone 1,536 MiB budgets)

### Takeaway
MoE is **neither** a capacity nor a speed lever when used as expert-parallel placement over this RPC star: it adds 16-48 sequential synchronizations per token, matches layer split's capacity, and has no supporting measurement. Used as **plain layer-split of a small-active MoE model**, it is a *plausible, untested speed lever of roughly 1.3-2x* (not a capacity lever; 3,189-3,350 MiB models of this class hold the same bytes as the 3B Q8 dense test), because weight bytes read per token drop about 4x. Disk/page-cache expert streaming is a capacity lever only for models far above the pool, at low single-digit tok/s, and does not fit the current upload-and-hash-cache design.

### Cited Findings
- Layer split already ran a 3,209 MiB model that no worker budget could hold, with tokens equal to the reference — [F43](file:///home/user/ShareCompute/findings.md)
- Tensor-style synchronization per token is the dominant cost in this topology (2.1-2.8x slower on loopback) and more devices add memory, not speed — [F49](file:///home/user/ShareCompute/findings.md)
- Measured distributed expert placement exists only on 1 GbE RTX 3090 nodes (OD-MoE, 75.5% of cached speed with 16 KB hidden-state exchanges per layer per token), not on phones — [OD-MoE](https://arxiv.org/html/2512.03927v1)
- The only upstream expert caches are GPU caches merged after the pin (Oct 2026) — [PR #29887](https://github.com/ggml-org/llama.cpp/pull/29887)

### Inferences
- **Verdict by option:** (1) expert-parallel across devices: no (speed negative; capacity-neutral). (2) Disk/page-cache streaming (Colibri-style): capacity only, for models well above ~3-4 GiB, at about 1-3 tok/s even with fast local flash; large engineering; conflicts with RPC upload and hash-cache design. (3) Small-active MoE through the existing layer-split path: the only variant worth a cheap test.
- **Recommended cheapest experiment (not run; research only):** run Granite 3.1 3B-A800M Q8_0 (3,350 MiB, matches F43's footprint) and OLMoE Q3_K_M (3,189 MiB) through the existing loopback split gate and compare tokens to a single-process reference; confirm MUL_MAT_ID over `sc-rpc-worker` works; then one physical pair. Pair it with the per-node compute and Wi-Fi RTT measurement F49 says is missing, because that measurement alone decides whether the physical 0.73-0.85 s/token is compute (MoE helps up to ~4x) or network/stall (MoE helps ~1x).
- Watch-outs: quality of 0.8-1.3B-active models vs the 3B dense (unsourced), cross-architecture token-equality fragility from top-k routing, and the hybrid-Mamba Granite 4.0-H arch being untested over RPC.

### Gaps
- No physical or loopback MoE run exists in this project; every speed figure for the project hardware is an extrapolation from F43 timings.
- Quality comparison between candidate MoE models and the dense 3B reference: not researched.
