#!/usr/bin/env python3
"""Selftest for Phase B portable dual-topology stub (unit + subprocess matrix)."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DEMO = os.path.join(HERE, "portable_dual_topology_demo.py")


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def test_lib_constants_and_topology() -> None:
    import portable_dual_topology_lib as lib

    assert lib.PORTABLE_SERVICE_ID == "sharecompute-portable"
    assert lib.PORTABLE_SERVICE_ID not in ("sharecompute-infer", "sharecompute-pool")
    assert lib.DATA_HEADER_MAGIC == b"SCPT"
    assert lib.DATA_HEADER_MAGIC != b"SCIN"
    assert lib.PORTABLE_SEATS == ("ios", "windows")
    assert "macos" not in lib.PORTABLE_SEATS and "android" not in lib.PORTABLE_SEATS
    assert lib.resolve_topology("iphone-frontend") == ("ios", "windows")
    assert lib.resolve_topology("windows-frontend") == ("windows", "ios")
    try:
        lib.resolve_topology("both")
        _fail("resolve_topology('both') must raise")
    except ValueError:
        pass
    try:
        lib.resolve_topology("macos-frontend")
        _fail("unknown topology must raise")
    except ValueError:
        pass


def test_scpt_roundtrip_rejects_scin() -> None:
    import portable_dual_topology_lib as lib

    msg = lib.ActivationMsg(epoch="abc", token_id=1, rank_from=0, rank_to=1, payload=b"x" * 64)
    framed = lib.encode_activation(msg)
    assert framed[:4] == b"SCPT"
    bad = b"SCIN" + framed[4:]
    a, b = socket.socketpair()
    try:
        a.setblocking(False)
        b.setblocking(False)
        b.sendall(bad)
        try:
            lib.recv_activation(a, time.monotonic() + 1.0)
            _fail("SCIN magic must be rejected")
        except ValueError:
            pass
    finally:
        a.close()
        b.close()


def test_plan_fits_two_seats_rejects_tiny_ram() -> None:
    import portable_dual_topology_lib as lib

    peers = [
        lib.PortablePeer("ios", "sim-ios", 6.0, "frontend", "127.0.0.1", 9001),
        lib.PortablePeer("windows", "sim-windows", 16.0, "worker", "127.0.0.1", 9002),
    ]
    shards = lib.plan_portable_shards(peers)
    assert len(shards) == 2
    assert shards[0].role == "frontend" and shards[0].platform == "ios" and shards[0].rank == 0
    tiny = [
        lib.PortablePeer("ios", "sim-ios", 0.1, "frontend", "127.0.0.1", 9001),
        lib.PortablePeer("windows", "sim-windows", 0.1, "worker", "127.0.0.1", 9002),
    ]
    try:
        lib.plan_portable_shards(tiny)
        _fail("tiny RAM must reject plan")
    except ValueError:
        pass


def test_beacon_service_filter() -> None:
    import json
    import portable_dual_topology_lib as lib

    good = json.dumps(
        {
            "v": 1,
            "service": "sharecompute-portable",
            "hub_host": "127.0.0.1",
            "hub_port": 5555,
            "epoch": "deadbeef",
            "platforms": ["ios", "windows"],
        }
    ).encode()
    bad = json.dumps(
        {
            "v": 1,
            "service": "sharecompute-infer",
            "hub_host": "127.0.0.1",
            "hub_port": 5555,
            "epoch": "deadbeef",
            "platforms": ["ios", "windows"],
        }
    ).encode()
    assert lib.parse_portable_beacon(good) is not None
    assert lib.parse_portable_beacon(bad) is None


def run_unit_tests() -> None:
    test_lib_constants_and_topology()
    test_scpt_roundtrip_rejects_scin()
    test_plan_fits_two_seats_rejects_tiny_ram()
    test_beacon_service_filter()
    print("PASS: lib unit checks")


def main() -> int:
    run_unit_tests()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
