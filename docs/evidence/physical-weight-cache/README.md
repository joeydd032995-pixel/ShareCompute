# Physical weight-cache evidence — reported 2026-10-08

The operator ran the standalone test kit twice in a row on **their own devices on one home Wi-Fi
network**, with the phone apps keeping their weight cache between the two runs. Same devices as
`../physical-three-device/` (F37):

| Role | Device | Reported platform |
|---|---|---|
| Laptop (coordinator and worker) | Windows laptop, AMD A9-9420e, 4 GB | `windows` |
| Android worker | Android phone, 8 GB | `android`, not a simulator |
| iPhone worker | iPhone, 8 GB, sideloaded through SideStore | `ios`, not a simulator |

- `cold/`: the first run. The phones' caches were empty.
- `warm/`: the second run, same model, same layer placement.

Both `report.json` files say **`"status": "PASS"`, `"physical_devices": true`, `"scope":
"physical-LAN"`, `"matches_baseline": true`**, with the runtime revision `4da6337…` and model sha256
`74a4da8c…`.

## What changed between the runs

| | Cold | Warm | Change |
|---|---:|---:|---:|
| Bytes sent to Android | 125,378,346 | 16,175,106 | −87.1% |
| Bytes sent to iPhone | 215,447,670 | 9,443,194 | −95.6% |
| **Both phones together** | **325.0 MiB** | **24.4 MiB** | **−92.5%** |
| Bytes sent to the laptop's worker (loopback, no cache) | 52,440,460 | 52,440,460 | 0 |
| Android cache | 109,191,936 stored, 0 hit | 0 stored, **109,191,936 hit** | |
| iPhone cache | 205,997,568 stored, 0 hit | 0 stored, **205,997,568 hit** | |
| Files rejected by the hash check | 0 | 0 | |
| Split wall time | 69,577 ms | 18,409 ms | −73.5% |

What did **not** change: the layers (laptop 0–4, Android 5–16, iPhone 17–24), the tracked buffer
peaks (59,005,184 / 133,527,296 / 257,146,368 bytes, identical in both runs and in F37), the 16
graph calls per worker, and the 16 generated token IDs, which equal the laptop-only baseline in both
runs.

Three things in these numbers are exact rather than approximate:

1. **Every cached byte was used.** Each phone's warm `cache_hit_bytes` equals its cold
   `cache_stored_bytes` to the byte.
2. **The saving matches the prediction.** F38 read the model's tensor table and predicted that 1 MiB
   would make 104.1 MiB of Android's share and 196.5 MiB of the iPhone's share cacheable. The
   phones served 109,191,936 bytes (104.1 MiB) and 205,997,568 bytes (196.5 MiB).
3. **The warm byte counts equal the Linux loopback run's.** F38 measured 16,175,106 bytes to Android
   and 9,443,194 to the iPhone on loopback. The physical Wi-Fi run produced the same two numbers.
   What is sent after a cache hit depends on the model and the placement, not on the network.

## Timing

| | Prefill | Decode (16 tokens) | Wall |
|---|---:|---:|---:|
| Laptop alone, cold pass (`cold/baseline.log`) | 2,632 ms | 9,900 ms | 13,621 ms |
| Laptop alone, warm pass (`warm/baseline.log`) | 2,593 ms | 9,281 ms | 12,982 ms |
| Split, cold (`cold/split.log`) | 780 ms | 5,212 ms | 69,577 ms |
| Split, warm (`warm/split.log`) | 740 ms | 4,913 ms | 18,409 ms |

Compute time did not depend on the cache: prefill and decode differ by 5–6% between the two split
runs, which is within what a Wi-Fi laptop run varies by. The cache removed upload time, and nothing
else.

**The split is still slower end to end than the laptop alone on this model.** Warm, it took 18.4 s
against 13.0 s for the laptop-only baseline of the same pass. Its compute phase (prefill plus
decode, 5.65 s) is about half of the laptop's (11.87 s), but the remaining 25.6 MB upload and the
three-worker start-up are not free, and this run does not break that 12.8 s down. A 0.5B model fits
on the laptop, so splitting it was never going to win on wall time; the point of the split is a model
that does not fit.

## Files

For each of `cold/` and `warm/`:

- `report.json` and `coordinator.log`: the verdict, including per-worker `cache` deltas.
- `split.log` and `baseline.log`: native probe output for the split run and the laptop-only run.
- `laptop-worker.log`: the laptop's own RPC worker. It prints `local cache : n/a`, because the kit
  starts it without a cache directory on purpose: its shard travels over loopback.

Content is unchanged except for masking, as in F37:
- Phone LAN addresses are replaced by `<android-lan-ip>` and `<iphone-lan-ip>`.
- The Windows user name in a model path is replaced by `<user>`.

The generated text is a fixed test fixture and contains a factual error made by the model. It is
not a claim by this project.

## What this does not establish

- **Which build.** The reports do not record the kit's commit. The `cache` fields in them prove the
  build includes PR #31's weight cache, and the runtime revision is `4da6337…`. Nothing more specific
  is known.
- **The damage-detection path on hardware.** No file was rejected in either run. The hash check ran
  on every hit, but the case where it finds a bad file has been exercised on loopback only (F38).
- **Persistence beyond two back-to-back runs.** The reports do not say whether either phone app was
  closed, killed, locked or left idle between the runs, or how long the gap was. Whether the cache
  survives an app kill, a reboot, or the OS purging cache storage (iOS `Library/Caches` and Android
  `cacheDir` can both be purged under storage pressure) is unobserved.
- **Platform identity:** platform and simulator flags are worker reports, not hardware attestation.
- **Capacity:** this is the same small model (Qwen2.5-0.5B Q4_K_M, about 469 MiB). It does not show
  that a model too large for any single device can run.
- **Coverage:** one prompt, 16 tokens, one pair of runs. The wall-time change is a single pair of
  samples, not a benchmark, and the Wi-Fi conditions between the runs are not recorded.
- **Disk use on the phones:** the byte counts say 104.1 MiB and 196.5 MiB were cached, but neither
  phone's free storage was recorded, and the cache is never evicted until cleared.
