# Physical evidence for F56 — first CPU reports from the three devices — reported 2026-11-19

Two back-to-back runs of the **`coder` profile** (Qwen2.5-Coder-1.5B-Instruct Q8_0, 1,894,532,160 B) on the
operator's own three devices on one home Wi-Fi network, using the kit built from commit
`936c69bf580e777b065e8f94f8c5c88d3a3ba96c` (the merge of PR #54, the first build that prints a CPU report; all five
components report it). The laptop is an AMD A9-9420e (2 cores), the Android phone a moto g 5G (2024), the iPhone an
iPhone17,5.

- `cold/` — first run, phone caches empty (`report.json`, coordinator, laptop-worker and split logs).
- `warm/` — second run, same model and layer placement, phone caches full.
- `phones/android-log.jsonl`, `phones/iphone-events.jsonl` — each app's exported log for the whole session (both
  runs). `*-after-cold-run.jsonl` are the earlier exports taken between the two runs; they are a prefix of the
  full exports, kept because they are the only phone logs written before the warm run began. The logs also contain
  earlier operator mis-taps (scan/stop/background) from before the runs; those are not part of the test.

The CPU reports are on `cold/laptop-worker.log:2` (`SC_CPU`), `phones/android-log.jsonl:1` (`native_cpu`) and
`phones/iphone-events.jsonl:1` (`native_cpu`).

Both reports say `"status": "PASS"`, `"matches_baseline": true`, `"baseline_source": "pinned-reference"`.

**What was changed from the originals.** LAN addresses are replaced with `<laptop-lan>`, `<android-lan>` and
`<iphone-lan>`, and the Windows account name in the model path (`C:\Users\<user>\...` in `split.log`) is replaced.
Nothing else was edited. Pin and token fields were already `redacted` in the originals.

**What is not here.** `events.jsonl` (named by each report's `event_log`) was not among the files provided. The
unsigned `.ipa` and a byte-identical duplicate of the cold laptop-worker log were left out. The cold laptop-worker log
was provided twice (identical files); the warm one is the shorter log.
