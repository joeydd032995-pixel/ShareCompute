#!/usr/bin/env python3
"""Phase A — pre-device thesis simulation: shard plan → inference-like data path.

One frontend (rank 0) owns prompt / final output; worker peers supply RAM/compute
seats (fake-but-sized compute). Reuses UDP multicast discovery + TCP join from the
four-platform demo; adds a separate TCP activation pipeline along shard ranks.

    python3 scripts/inference_pipeline_demo.py
    python3 scripts/inference_pipeline_demo.py --fail-discovery
    python3 scripts/inference_pipeline_demo.py --fail-platform android
    python3 scripts/inference_pipeline_demo.py --omit-worker
    python3 scripts/inference_pipeline_demo.py --kill-worker mid

Hard fails (missing seat, discovery fail, kill worker mid-run) → non-zero exit, no hang.
This does **not** claim MLX/Metal works — compute is checksum + sleep scaled by layers.
See docs/INFERENCE-PIPELINE-SIM.md.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from inference_pipeline_orch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
