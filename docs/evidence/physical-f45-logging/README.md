# Physical evidence for F45 — the first runs with build provenance — reported 2026-10-11

Two back-to-back runs of the **3B capacity test** on the operator's own three devices, on one home
Wi-Fi network, using the **F44 logging build** installed fresh from CI. Same devices as
`../physical-three-device/` (F37), `../physical-weight-cache/` (F39) and `../physical-3b-capacity/`
(F43).

| Role | Device | Reported platform | Peer |
|---|---|---|---|
| Laptop (coordinator and worker) | Windows laptop, AMD A9-9420e, 4 GB | `windows` | `127.0.0.1` (its own worker, over loopback) |
| Android worker | motorola moto g 5G (2024), Android 15, 3,472 MiB visible | `android`, not a simulator | `<android-lan-ip>` |
| iPhone worker | iPhone17,5, iOS 26.3.1, 7,718 MiB | `ios`, not a simulator | `<iphone-lan-ip>` |

- `cold/` — the first run. Both phone caches were empty, because installing the new apps wiped them.
- `warm/` — the second run, same model and the same layer placement.

**What is new here, and the reason this directory exists separately from `../physical-3b-capacity/`:**
these are the first physical runs whose reports name the commit every component was built from. All
five components — coordinator, probe and the three workers — report
`62e5b42e0b7bcd2d3642b8dde41352d8991be4c0`, with `"mismatch": false`, `"complete": true` and
`"unknown": []`. Every earlier physical run is provenance-blind, which is why F42 can never be
settled.

Both reports say **`"status": "PASS"`, `"physical_devices": true`, `"scope": "physical-LAN"`,
`"matches_baseline": true`, `"baseline_source": "pinned-reference"`**, runtime revision `4da6337…`,
model sha256 `6dcc2269…ba1` (Qwen2.5-3B-Instruct **Q8_0**).

## Files

Each of `cold/` and `warm/` holds the laptop's side plus both phones' own exported logs:

| File | What it is |
|---|---|
| `report.json` | the laptop's report, the per-run authority for every counter |
| `events.jsonl` | the coordinator's event log, one flushed JSON object per line |
| `coordinator.log`, `laptop-worker.log`, `split.log` | the laptop's text logs |
| `android-log.json` | exported from the Android app with **Export log** |
| `iphone-log.jsonl` | exported from the iPhone app's **Event log** |

**The phone logs are per app launch, not per run.** The Android app was relaunched between runs, so
`warm/android-log.json` holds both runs (run 2 begins at the second `app_start`, `06:20:43`). The
iPhone app was *not* relaunched, so `warm/iphone-log.jsonl` is one continuous log of both runs under
a single `launch` id, and its `stored_bytes` stays at the cold run's total through the warm run
because that counter is cumulative for the launch. `report.json` is the per-run authority; comparing
raw phone totals between runs will mislead you.

## Results

| | cold | warm | change |
|---|---:|---:|---:|
| `status` | PASS | PASS | |
| wall clock | 556.7 s | 165.6 s | −70.3% |
| `split_generation` | 504.1 s | 126.0 s | −75.0% |
| bytes sent to the two phones | 2,582.2 MiB | 33.0 MiB | −98.7% |
| cache hits | 0 | 2,549.2 MiB | |
| cache rejected | 0 | 0 | |
| prefill + decode | 27.5 s | 27.0 s | −1.8% |
| `phases.unaccounted_ms` | 859 ms (0.2%) | 140 ms (0.1%) | |
| laptop minimum free RAM | 89.2 MiB | 208.8 MiB | |
| `loop_stalls.count` | 0 | 0 | |
| pooled `run_allocated_bytes` | 3,364,962,304 | 3,364,962,304 | identical |

| Worker | Layers | Held during the run | Budget | Headroom |
|---|---:|---:|---:|---:|
| laptop | 0–6 | 562.2 MiB | 768 | 205.8 MiB |
| android | 7–23 | 1,348.3 MiB | 2048 | 699.7 MiB |
| iphone | 24–36 | 1,298.6 MiB | 1536 | 237.4 MiB |

Pooled 3,209.1 MiB against a 2,048 MiB largest budget, `"exceeds_largest_worker_budget": true` —
and **byte-identical to F43's figure**, in both runs.

## Masking

`docs/evidence/**` is never edited after commit except for masking. Replaced here:
`<laptop-lan-ip>`, `<android-lan-ip>`, `<iphone-lan-ip>` for the four devices' LAN addresses
(`127.0.0.1` is left as-is, it names the laptop's own loopback worker) and `<user>` for the Windows
account name in the model path. The pairing pin and token were already written as `redacted` by the
apps themselves; `"pin":"matched"` is the Android app recording that pinning succeeded, not a secret.
Session ids are per-run random identifiers and are not credentials — authentication is the separate
redacted token.
