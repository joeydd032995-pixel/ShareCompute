# 3B capacity test — loopback evidence, 2026-10-08

Everything here ran on **one Linux x86-64 machine** (4 vCPUs, 16 GB), with the same pinned runtime
(`4da6337…`) and the same worker budgets the physical devices use: laptop 768 MiB, Android 2048 MiB,
iPhone 1536 MiB. It prepares the physical capacity test. It is not that test.

## The model, and why it is not the one the plan named

The plan (`task_plan.md`, item 1) named "a 3B Q4 model of about 1.9 GB". Qwen2.5-3B-Instruct
Q4_K_M is 2,104,932,768 bytes, and **one worker holds all of it in 1,890 MiB**, which fits the Android
phone's 2048 MiB budget. Running that model across three devices would show that splitting works,
not that pooling is needed. `negative-controls.txt` has the run.

The same model at **Q8_0** (`qwen2.5-3b-instruct-q8_0.gguf`, 3,616,088,480 bytes, sha256
`6dcc2269…ba1`, pinned to Hugging Face commit `7dabda4d…`) needs more than any one budget:

| Whole model given to one worker with a budget of | Result |
|---|---|
| 768 MiB (laptop) | refused, `RPC buffer budget exceeded` |
| 1536 MiB (iPhone) | refused |
| 2048 MiB (Android) | refused |
| 3072 MiB | refused |
| 4096 MiB (the largest `init_cluster` accepts) | runs; peak **3,337,814,016 bytes (3,183 MiB)** |

The 4096 MiB run is the control: it shows the refusals come from the budget, not from a fault in the
model or the worker. The three real budgets add up to 4,352 MiB, so the pool can hold what no single
budget can.

## The pooled run

`scripts/verify_capacity.py` (five `VERIFIED` lines) then ran the three-worker loopback split against
the pinned reference tokens (`native/split/reference-capacity.json`).

| Worker | Layers | Peak allocation | Budget | Headroom | Sent to it |
|---|---:|---:|---:|---:|---:|
| laptop | 7 | 562.2 MiB | 768 | 206 MiB | 547 MiB |
| android | 17 | 1,348.3 MiB | 2048 | 700 MiB | 1,329 MiB |
| iphone (also holds `output.weight`) | 13 | 1,298.6 MiB | 1536 | 237 MiB | 1,253 MiB |
| **Combined** | 37 | **3,209.1 MiB** | largest: 2048 | | phones: 2,582 MiB |

`PASS`. The 16 generated tokens equal the pinned reference, and the text is " Paris. The capital of
Spain is Madrid. The capital of Italy is Rome." Prefill 1,081 ms, decode 7,233 ms, wall 32,055 ms.
Those timings are loopback with no Wi-Fi in them and say nothing about the physical run.

The reference tokens come from the same model run in one process with no workers. Every three-worker run
in this session (five) produced identical tokens.

## Memory, because the laptop has 4 GB

`process-memory-peaks-kib.json` has the peak memory of each process during one loopback run:

| Process | Real memory (anonymous) | Mapped model file |
|---|---:|---:|
| laptop worker | 570 MiB | 5 MiB |
| iPhone-role worker | 1,278 MiB | 5 MiB |
| Android-role worker | 1,352 MiB | 5 MiB |
| probe (the laptop-side client) | 391 MiB | 3,455 MiB |

The probe maps the whole 3.4 GB file to stream it to the workers. Those pages are file-backed, so the
OS can drop them and read them again; they are not a 3.4 GB allocation. **On the physical laptop the real
memory is about 570 MiB (worker) + 391 MiB (probe) + the launcher**, against the roughly 1.2 GB free the
laptop's own worker log showed in F37. That is tight, and this run cannot say how Windows behaves there.

## What this does not establish

- **Any physical device.** Nothing here touched the laptop, the Android phone or the iPhone. Wi-Fi
  upload time, the iPhone's memory limit, Android's low-memory killer, thermal throttling and the
  laptop's behaviour with about 1.2 GB free are all untested.
- **That the laptop "cannot load the model alone".** The result is about worker budgets, which are
  this project's own limits. The laptop was never asked to run the model unsplit, and a memory-mapped
  model larger than free RAM may still run slowly. The physical test makes no laptop-alone attempt.
- **Cross-architecture identity of the tokens.** The pinned reference was made on x86-64 Linux. F37
  showed the 0.5B model's tokens identical on x86-64 Windows, ARM Android and ARM iOS. That is not a
  guarantee for a model six times larger, and a legitimate numerical difference would fail the run.
- **Quality.** The text is a sanity check, not an evaluation.
- **Platform identity:** platform flags are worker reports, not hardware attestation.
