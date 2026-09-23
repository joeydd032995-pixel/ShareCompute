#!/usr/bin/env python3
"""Phase B - portable dual-topology stub sim entry point.

    python3 scripts/portable_dual_topology_demo.py
    python3 scripts/portable_dual_topology_demo.py --topology iphone-frontend
    python3 scripts/portable_dual_topology_demo.py --topology windows-frontend
    python3 scripts/portable_dual_topology_demo.py --backend stub
    python3 scripts/portable_dual_topology_demo.py --backend llamacpp-rpc
    python3 scripts/portable_dual_topology_demo.py --fail-discovery
    python3 scripts/portable_dual_topology_demo.py --fail-platform ios
    python3 scripts/portable_dual_topology_demo.py --kill-worker mid
    python3 scripts/portable_dual_topology_demo.py --usable-gb 0.1
    python3 scripts/portable_dual_topology_demo.py --fail-role-mismatch

See docs/PORTABLE-DUAL-TOPOLOGY-SIM.md.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from portable_dual_topology_orch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
