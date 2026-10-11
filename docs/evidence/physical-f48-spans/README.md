# Physical evidence for F48 — the first runs with per-node spans inside `split_generation` — reported 2026-10-11

Two back-to-back runs of the **3B capacity test** on the operator's own three devices, on one home
Wi-Fi network, using the build from CI run `38137484700` (PR #44, the `split_generation` spans, F47).
Same devices as `../physical-f45-logging/` (F45) and the earlier physical directories.

| Role | Device | Reported platform | Peer |
|---|---|---|---|
| Laptop (coordinator and worker) | Windows laptop, AMD A9-9420e, 4 GB | `windows` | `127.0.0.1` (its own worker, over loopback) |
| Android worker | motorola moto g 5G (2024), Android 15, 3,472 MiB visible | `android`, not a simulator | `<android-lan-ip>` |
| iPhone worker | iPhone17,5, iOS 26.3.1, 7,718 MiB | `ios`, not a simulator | `<iphone-lan-ip>` |

- `cold/` — **run 1**, phone caches emptied first. **Only the two phones' logs exist.** The operator did
  not save the laptop's report for this run, so there is no `report.json`, no `events.jsonl` and no
  spans for it. Everything F48 says about the cold run comes from the phones' own logs.
- `warm/` — **run 2**, same model, same layer placement, phone caches full. This is the only run with a
  laptop report, and so the only one with the new `upload:*` / `cache:*` spans.

**What is new here:** these are the first physical runs where `report.json` carries per-node child spans
of `split_generation` and the event log carries each child's own `began_ts` / `ended_ts`.

**Build.** All five components in `warm/report.json` — coordinator, probe, three workers — report
`eec404dca1d36ec64f153541d995951c4453eedf` with `"mismatch": false`, `"complete": true` and
`"unknown": []`. Both phone apps report the same commit in their own logs for both runs. That commit is
GitHub's temporary merge of PR #44 into `main` made by the CI run that built the artifacts, not a commit in
`main`'s history. `main` and the PR's last commit (`dec6ed8`) have identical file trees; the merge commit
itself could not be fetched to confirm the same.

`warm/report.json` says **`"status": "PASS"`, `"physical_devices": true`, `"scope": "physical-LAN"`,
`"matches_baseline": true`, `"baseline_source": "pinned-reference"`**, runtime revision `4da6337…`,
model sha256 `6dcc2269…` (Qwen2.5-3B-Instruct **Q8_0**).

## Files

| File | What it is |
|---|---|
| `warm/report.json` | the laptop's report for run 2, the authority for every laptop-side number |
| `warm/events.jsonl` | the coordinator's event log; the five child spans appear as `phase` events with `parent`, `began_ts` and `ended_ts` |
| `warm/coordinator.log`, `warm/laptop-worker.log`, `warm/split.log` | the laptop's text logs. `laptop-worker.log` carries one `SC_STATS` line about every 0.26 s with **no timestamps** |
| `warm/iphone-log.jsonl`, `warm/android-log.json` | the phones' exports **after run 2**; each holds **both** runs (see below) |
| `cold/iphone-log.jsonl`, `cold/android-log.json` | the phones' exports **after run 1** — a prefix of the warm files |

**The phone logs are per app launch, not per run.** The iPhone app was not relaunched between runs, so
`warm/iphone-log.jsonl` is one continuous log of both runs under launch id `5eb89332`, and its
`graph_calls` and `stored_bytes` are cumulative across them. The Android app was relaunched, so
`warm/android-log.json` holds run 1 first and run 2 from its second `app_start` (`14:42:26`).
`cold/` is therefore a strict prefix of `warm/` for each phone; it is kept because it is what was
exported after run 1. Use `report.json` for run-2 laptop numbers and `ts` / `wall` to line the logs up.

## Results (run 2)

| | |
|---|---:|
| `status` | PASS |
| wall clock (`phases.total_ms`) | 183.1 s |
| `split_generation` | 140.0 s |
| prefill / decode | 28.9 s / 11.5 s |
| bytes sent to the laptop's own worker | 573.9 MB (no cache on the laptop) |
| bytes sent to the iPhone / Android | 14.4 MB / 20.2 MB |
| cache hits, iPhone / Android | 1,299,890,176 B / 1,373,143,040 B |
| cache rejected | 0 |
| `model_checksum` | 24.7 s at 139.7 MiB/s, `exceeded_heartbeat_limit: true` |
| `phases.unaccounted_ms` | 217 ms (0.1%) |
| laptop minimum free RAM | 197.1 MiB of 3,973 |
| `loop_stalls.count` | 0 |

## Masking

LAN addresses are replaced by `<laptop-lan-ip>`, `<android-lan-ip>` and `<iphone-lan-ip>`, and the Windows user
name in two file paths by `<user>`; `127.0.0.1` is left as it is. The pairing pin and token appear as
`"redacted"` in the apps' own logs. Nothing else was altered.
