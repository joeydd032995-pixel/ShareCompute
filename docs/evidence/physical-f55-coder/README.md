# Physical evidence for F55 — Coder 1.5B Q8_0 on the three devices — reported 2026-10-26

Two back-to-back runs of the **`coder` profile** (Qwen2.5-Coder-1.5B-Instruct Q8_0, 1,894,532,160 B) on the
operator's own three devices on one home Wi-Fi network, using the kit built from commit
`06d789390f57bd2918ab1e21df8d08d436087f9a` (all five components report it). Same devices as
`../physical-f45-logging/`.

- `cold/` — first run, phone caches empty (`report.json`, `events.jsonl`, coordinator, laptop-worker and split logs).
- `warm/` — second run, same model and layer placement.
- `phones/android-log.jsonl`, `phones/iphone-events.jsonl` — the apps' exported logs for the session. They also
  contain earlier operator mis-taps (stop/start/scan) from before the two runs; those are not part of the test.

Both reports say `"status": "PASS"`, `"matches_baseline": true`, `"baseline_source": "pinned-reference"`.
LAN addresses are replaced with placeholders; pin and token fields were already `redacted` in the originals.
