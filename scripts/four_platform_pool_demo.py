#!/usr/bin/env python3
"""Four-platform ShareCompute pool demo — LAN discovery + TCP multi-process join.

Hub announces via UDP multicast (239.255.77.77:37777); peers discover the hub,
then JOIN over TCP. Missing beacon or missing peer → non-zero exit.

    python3 scripts/four_platform_pool_demo.py
    python3 scripts/four_platform_pool_demo.py --fail-platform android
    python3 scripts/four_platform_pool_demo.py --fail-discovery

Platform runtimes stay simulated (stock RAM profiles); discovery + connect paths
are real enough that wrong network / no announcement fails the join.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from four_platform_pool_orch import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
