# Physical three-device evidence — reported 2026-10-07

The operator ran the standalone test kit on **their own devices on one home Wi-Fi network**:

| Role | Device | Reported platform |
|---|---|---|
| Laptop (coordinator and worker) | Windows laptop, AMD A9-9420e, 4 GB | `windows` |
| Android worker | Android phone, 8 GB | `android`, not a simulator |
| iPhone worker | iPhone, 8 GB, sideloaded through SideStore | `ios`, not a simulator |

`report.json` is the coordinator's verdict: **`"status": "PASS"`, `"physical_devices": true`, `"scope": "physical-LAN"`**.

| Gate | Observed |
|---|---|
| Disjoint, non-empty layers | laptop 0–4, Android 5–16, iPhone 17–24 |
| Native computation on every worker | 16 graph calls each |
| Tracked RPC buffer peaks | 59,005,184 / 133,527,296 / 257,146,368 bytes |
| Bidirectional tunnel traffic | non-zero both ways on all three |
| Phones at distinct non-loopback LAN addresses | yes (addresses masked below) |
| Greedy token IDs equal the laptop-only baseline | 16/16 identical |

The peak buffer sizes match the Linux loopback run in `../three-device-split/` to the byte. The
placement and allocation are therefore deterministic across x86-64, ARM Android and ARM iOS for
this model.

## Timing

All timings come from this single run.

| | Prefill | Decode (16 tokens) | Wall |
|---|---:|---:|---:|
| Laptop alone (`baseline.log`) | 3,554 ms | 10,885 ms | 15,611 ms |
| Three-device split (`split.log`) | 1,942 ms | 5,489 ms | 66,352 ms |

The split's wall time is dominated by uploading about 374 MiB of model weights to the phones over
Wi-Fi before the first token. Once loaded, prefill and decode were each about half the laptop-only
time on this weak laptop CPU. That is one sample, not a benchmark.

## Files

- `report.json` and `coordinator.log`: the verdict.
- `split.log` and `baseline.log`: native probe output for the split run and the laptop-only run.
- `laptop-worker.log`: the laptop's own RPC worker.

Content is unchanged except for masking:
- Phone LAN addresses are replaced by `<android-lan-ip>` and `<iphone-lan-ip>`.
- The Windows user name in a model path is replaced by `<user>`.

The generated text is a fixed test fixture and contains a factual error made by the model. It is
not a claim by this project.

## What this does not establish

- **Platform identity:** platform and simulator flags are worker reports, not hardware attestation.
- **Capacity:** this is a small-model splitting proof (Qwen2.5-0.5B Q4_K_M, about 469 MiB). It does
  not show that a model too large for any single device can run.
- **Coverage:** one prompt, 16 tokens, one run. It says nothing about longer generations, other
  prompts, or numerical identity in general.
- **Throughput:** no throughput claim beyond the single timing row above.
- **Failure behaviour:** the run did not exercise any failure path on hardware. Worker death and
  allocation refusal remain loopback-only results.
- **Which build:** the Android worker's survival while locked is not shown, because this report
  does not record whether the phone was locked. The build used is inferred from the runtime revision
  `4da6337…`. The kit's commit is not recorded in the report.
