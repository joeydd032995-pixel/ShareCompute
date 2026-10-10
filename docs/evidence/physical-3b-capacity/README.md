# Physical 3B capacity evidence — reported 2026-10-10

The operator ran the **3B capacity test** twice in a row on **their own three devices on one home
Wi-Fi network**. Same devices as `../physical-three-device/` (F37) and `../physical-weight-cache/`
(F39):

| Role | Device | Reported platform | Peer |
|---|---|---|---|
| Laptop (coordinator and worker) | Windows laptop, AMD A9-9420e, 4 GB | `windows` | `127.0.0.1` (its own worker, over loopback) |
| Android worker | Android phone, 8 GB | `android`, not a simulator | `<android-lan-ip>` |
| iPhone worker | iPhone, 8 GB, sideloaded through SideStore | `ios`, not a simulator | `<iphone-lan-ip>` |

- `cold/`: the first run. The phones' weight caches were empty.
- `warm/`: the second run, same model, same layer placement.

Both `report.json` files say **`"status": "PASS"`, `"physical_devices": true`, `"scope":
"physical-LAN"`, `"matches_baseline": true`, `"baseline_source": "pinned-reference"`**, with runtime
revision `4da6337…` and model sha256 `6dcc2269…ba1` (Qwen2.5-3B-Instruct **Q8_0**, 3,448.6 MiB).

## The capacity result

| Worker | Layers | Held during the run | Budget | Headroom | Sent to it (cold) |
|---|---:|---:|---:|---:|---:|
| laptop | 0–6 (7) | 562.2 MiB | 768 | 205.8 MiB | 547.3 MiB |
| android | 7–23 (17) | 1,348.3 MiB | 2048 | 699.7 MiB | 1,328.8 MiB |
| iphone (also holds `output.weight`) | 24–36 (13) | 1,298.6 MiB | 1536 | 237.4 MiB | 1,253.4 MiB |
| **Combined** | 37 | **3,209.1 MiB** | largest: 2048 | | phones: 2,582.2 MiB |

Every worker stayed inside its own budget, and the three together held **1,161 MiB more than the
largest single budget**, so no one worker budget could have held this model. 16 graph calls each,
no refusals.

"Held during the run" is the per-run `run_allocated_bytes`, sampled from worker telemetry — not the
worker's lifetime peak, which on a phone would carry an earlier run into a later one. In these two
runs `peak_bytes` happens to equal `run_allocated_bytes` for all three workers.

**The allocations are byte-identical to the Linux loopback run** (`../capacity-loopback/`, F40):
589,458,432 / 1,413,839,872 / 1,361,664,000 bytes held, the same layer split, the same cold
`bytes_to_worker`, and the same combined 3,364,962,304. Placement is deterministic across x86-64
Windows, ARM Android and ARM iOS.

## The weight cache, on a model seven times larger than F39's

| | Cold | Warm | Change |
|---|---:|---:|---:|
| Bytes sent to Android | 1,393,375,362 | 20,205,632 | −98.6% |
| Bytes sent to iPhone | 1,314,271,211 | 14,361,881 | −98.9% |
| **Both phones together** | **2,582.2 MiB** | **32.97 MiB** | **−98.7%** |
| Bytes sent to the laptop's worker (loopback, no cache) | 573,890,422 | 573,890,422 | 0 |
| Android cache | 1,373,143,040 stored, 0 hit | 0 stored, **1,373,143,040 hit** | |
| iPhone cache | 1,299,890,176 stored, 0 hit | 0 stored, **1,299,890,176 hit** | |
| Files rejected by the hash check | 0 | 0 | |
| Split wall time | 492,177 ms | 141,026 ms | −71.3% |

Each phone's warm `cache_hit_bytes` equals its cold `cache_stored_bytes` **to the byte**. Every
cached byte was used; none was rejected. The cache removed 2.55 GB of Wi-Fi upload.

## Timing

| | Prefill | Decode (16 tokens) | Compute | Wall | Outside compute |
|---|---:|---:|---:|---:|---:|
| Cold (`cold/report.json`) | 11,321 ms | 11,624 ms | 22.9 s | 492.2 s | 469.2 s |
| Warm (`warm/report.json`) | 12,419 ms | 13,621 ms | 26.0 s | 141.0 s | 115.0 s |
| Linux loopback, same model (F40) | 1,039 ms | 7,027 ms | 8.1 s | 31.1 s | 23.0 s |

About 1.4 tokens/s cold and 1.2 warm. Two things these numbers say that are easy to misread:

1. **Warm compute was 13% slower than cold**, not faster. Consistent with F38/F39: the cache removes
   upload time and nothing else. One pair of samples on thermally variable phones, so this is noise,
   not a regression.
2. **The warm run still spent 115 s outside compute** while sending only 33 MB. Nothing here
   measures where it went. Candidates are the phones hash-checking 2.6 GB of cached weights, the
   laptop's own uncached 547 MiB loopback transfer, and model load; the reports have no phase
   breakdown, so this is unexplained rather than attributed.

Generated text, both runs: `" Paris. The capital of Spain is Madrid. The capital of Italy is Rome."`,
the same 16 token ids in both, equal to the pinned reference
(`native/split/reference-capacity.json`). That text is a fixed test fixture and contains a factual
error made by the model; it is not a claim by this project.

## Files

For each of `cold/` and `warm/`: `report.json`, `coordinator.log` (the verdict, per-worker `cache`
deltas), `split.log` (native probe output for the split run) and `laptop-worker.log` (the laptop's
own RPC worker, which prints `local cache : n/a` because the kit starts it without a cache
directory on purpose — its shard travels over loopback).

There is no `baseline.log`: this test compares against **pinned reference tokens** rather than a
laptop-only run, because loading a 3.4 GB model unsplit could thrash a 4 GB machine.

Content is unchanged except for masking, as in F37 and F39:
- Phone LAN addresses are replaced by `<android-lan-ip>` and `<iphone-lan-ip>`.
- The Windows user name in the model path is replaced by `<user>`.

## What this does not establish

- **Which build.** The reports do not record the kit's commit. The `cache` fields prove PR #31's
  weight cache is in it; nothing narrows it further. No `Laptop stalled` line appears in either
  `coordinator.log`, but that line prints only when a stall happens, so these logs cannot say
  whether the kit carries PR #37's loop-lag monitor.
- **That the earlier `TimeoutError` is fixed.** Neither run disconnected, which is consistent with
  F41's fix, but a run that passes cannot explain a run that failed.
- **That the laptop "cannot load the model alone".** The result is about **worker budgets**, which
  are this project's own limits. The laptop was never asked to run the model unsplit, and a
  memory-mapped model larger than free RAM may still run slowly. No laptop-alone attempt was made.
- **Laptop memory headroom.** Nothing in these runs records free RAM on the 4 GB laptop while the
  split ran. F37 observed roughly 1.2 GB free; whether that held here is unobserved.
- **The damage-detection path on hardware.** Zero rejections in both runs, as in F39. The case where
  the hash check finds a bad file has run on loopback only (F38).
- **Cache persistence beyond two back-to-back runs,** and whether either phone app was closed,
  killed or locked between them. Not recorded.
- **Platform identity:** platform and simulator flags are worker reports, not hardware attestation.
- **Coverage:** one prompt, 5 prompt tokens, 16 generated tokens, one cold/warm pair. The timings
  are single samples, and the Wi-Fi conditions are not recorded.
