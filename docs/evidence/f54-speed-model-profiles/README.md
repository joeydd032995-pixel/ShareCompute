# F54 — Coder 1.5B Q8_0 and 3B Q4_K_M kit profiles, three-worker loopback rehearsal

Evidence for [F54](../../../findings.md). Linux x86-64 container, pinned runtime built by
`scripts/build_split_runtime.py --platform desktop` (all three carried patches), three `sc-rpc-worker` processes on
loopback, budgets 768 / 2048 / 1536 MiB. **Nothing here ran on a physical device.**

| | Coder 1.5B Q8_0 (`coder`) | Qwen2.5-3B Q4_K_M (`qwen3b-q4`) |
|---|---|---|
| Report | `report-coder-loopback.json` | `report-qwen3b-q4-loopback.json` |
| Verdict | PASS, `baseline_source: pinned-reference`, 16 tokens equal | PASS, `baseline_source: pinned-reference`, 16 tokens equal |
| Layers laptop / Android / iPhone | 6 / 13 / 10 | 7 / 17 / 13 |
| Allocated MiB (of budget) | 297 (768) / 633 (2048) / 707 (1536) | 328 (768) / 757 (2048) / 831 (1536) |
| `exceeds_largest_worker_budget` | false | false |
| `prefill_ms` / `decode_ms` (16 tokens) | 517.7 / 3,912.7 | 1,174.2 / 7,112.4 |
| Reference | `native/split/reference-coder.json` | `native/split/reference-qwen3b-q4.json` |

The decode times are loopback, so they contain no network and no phone CPU; they are a floor for the physical split,
not an estimate of it. Reproduce:

```bash
python3 scripts/split_cluster.py loopback --worker-binary build/desktop/bin/sc-rpc-worker \
  --probe-binary build/desktop/bin/sc-split-probe --model models/split-coder.gguf --out split-runs/coder \
  --tokens 16 --timeout 1800 --reference native/split/reference-coder.json
```

(`python3 scripts/download_split_model.py --profile coder` fetches the model; the other profile is `qwen3b-q4`.)
