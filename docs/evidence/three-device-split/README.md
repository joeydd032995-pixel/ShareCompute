# Native execution evidence — 2026-09-28

These files were produced by **three Linux processes on one x86-64 host**, using
native C++ workers, real model weights, and the same pinned TLS tunnel used on the LAN.
They are not Android or iPhone hardware measurements. Node names describe intended
roles; the reports preserve each worker's actual `linux` platform and loopback address.

Reproduce with:

```sh
python scripts/build_split_runtime.py --jobs 3
python scripts/download_split_model.py
python scripts/verify_split_runtime.py --bin-dir build/desktop/bin --model models/split-proof.gguf --out split-runs/matrix
```

| Case | Expected result | Observed |
|---|---|---|
| 24-token split generation | PASS; exact baseline tokens | PASS; all three graph counts = 24 |
| Kill Android-role process after its first graph | FAIL; discard generation | FAIL; fault trigger records graph count 1 |
| Limit all RPC buffers to 64 MiB per worker | FAIL; refuse oversized allocation | FAIL; native model load exited 1 |

The successful model placement was layers 0–4, 5–16, and 17–24 (24 includes the
output layer). Recorded peak RPC buffers were 59,005,184; 133,527,296; and 257,146,368
bytes. These are **not** total-process memory measurements. The laptop also owns the
GGUF mapping and other local tensors. Timing is from this host only.

`placement-and-generation.log` selects native placement, buffer, and final-result lines
from the successful log. Report files are otherwise copied unchanged. The generated
fixture text contains model mistakes and is not a factual claim by this project.

All platform jobs and the actual Swift/native iOS simulator computation passed in the
[validated run](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/36391329414).
The [simulator artifact](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/36391329414/artifacts/10956746112)
contains its report and logs. It placed layers 0–4 / 5–16 / 17–24 on distinct
workers, recorded 24 graphs on each, and matched the local ARM baseline token IDs.
Only a physical-mode run on the operator's devices completes the hardware objective.
