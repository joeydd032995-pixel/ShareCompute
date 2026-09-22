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


def test_hub_rejects_non_product_and_role_mismatch() -> None:
    import socket
    import threading
    from portable_dual_topology_hub import run_hub
    from portable_dual_topology_lib import (
        DEFAULT_HOST,
        PORTABLE_DEFAULT_USABLE_GB,
        recv_line,
        send_msg,
    )

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((DEFAULT_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()

    result = {"rc": None}

    def hub_thread() -> None:
        result["rc"] = run_hub(
            DEFAULT_HOST,
            port,
            ("ios", "windows"),
            timeout_s=3.0,
            frontend_platform="ios",
            token_count=2,
            enable_beacon=False,
        )

    t = threading.Thread(target=hub_thread, daemon=True)
    t.start()
    time.sleep(0.2)

    def join_once(platform: str, role: str, data_port: int) -> dict:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect((DEFAULT_HOST, port))
        s.setblocking(False)
        buf = bytearray()
        send_msg(
            s,
            {
                "type": "join",
                "platform": platform,
                "node_id": f"sim-{platform}",
                "usable_gb": PORTABLE_DEFAULT_USABLE_GB.get(platform, 8.0),
                "role": role,
                "data_host": DEFAULT_HOST,
                "data_port": data_port,
            },
        )
        msg = recv_line(s, buf, time.monotonic() + 2.0)
        try:
            s.close()
        except OSError:
            pass
        return msg or {}

    bad = join_once("macos", "worker", 19001)
    assert bad.get("type") == "error", f"expected error for macos, got {bad}"
    assert bad.get("reason") == "non-product-platform"

    mm = join_once("windows", "frontend", 19002)
    assert mm.get("type") == "error", f"expected role reject, got {mm}"
    assert mm.get("reason") == "frontend-role-reserved"

    mm2 = join_once("ios", "worker", 19003)
    assert mm2.get("type") == "error"
    assert mm2.get("reason") == "frontend-must-join-as-frontend"

    t.join(timeout=5.0)
    print("PASS: hub reject checks")



def test_hub_rejects_worker_done() -> None:
    import threading
    from portable_dual_topology_hub import run_hub
    from portable_dual_topology_lib import (
        DEFAULT_HOST,
        PORTABLE_DEFAULT_USABLE_GB,
        recv_line,
        send_msg,
    )

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind((DEFAULT_HOST, 0))
    port = probe.getsockname()[1]
    probe.close()

    result = {"rc": None}

    def hub_thread() -> None:
        result["rc"] = run_hub(
            DEFAULT_HOST,
            port,
            ("ios", "windows"),
            timeout_s=2.0,
            frontend_platform="ios",
            token_count=1,
            enable_beacon=False,
        )

    t = threading.Thread(target=hub_thread, daemon=True)
    t.start()
    sockets = []
    buffers = []
    try:
        time.sleep(0.1)
        for platform, role, data_port in (
            ("ios", "frontend", 19101),
            ("windows", "worker", 19102),
        ):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((DEFAULT_HOST, port))
            s.setblocking(False)
            sockets.append(s)
            buf = bytearray()
            buffers.append(buf)
            send_msg(
                s,
                {
                    "type": "join",
                    "platform": platform,
                    "node_id": f"sim-{platform}",
                    "usable_gb": PORTABLE_DEFAULT_USABLE_GB[platform],
                    "role": role,
                    "data_host": DEFAULT_HOST,
                    "data_port": data_port,
                },
            )
            ack = recv_line(s, buf, time.monotonic() + 2.0)
            assert ack and ack.get("type") == "ack", f"expected {platform} ack, got {ack}"

        for platform, s, buf in zip(("ios", "windows"), sockets, buffers):
            plan = recv_line(s, buf, time.monotonic() + 2.0)
            assert plan and plan.get("type") == "plan", f"expected {platform} plan, got {plan}"

        send_msg(sockets[1], {"type": "done", "tokens": 1})
        t.join(timeout=5.0)
        assert not t.is_alive(), "hub did not stop after worker done"
        assert result["rc"] != 0, f"worker done incorrectly succeeded with rc={result['rc']}"
    finally:
        for s in sockets:
            try:
                s.close()
            except OSError:
                pass
        if t.is_alive():
            t.join(timeout=5.0)



def test_peer_module_importable() -> None:
    from portable_dual_topology_peer import run_peer
    assert callable(run_peer)
    print("PASS: peer module import")


def run_unit_tests() -> None:
    test_lib_constants_and_topology()
    test_scpt_roundtrip_rejects_scin()
    test_plan_fits_two_seats_rejects_tiny_ram()
    test_beacon_service_filter()
    test_hub_rejects_non_product_and_role_mismatch()
    test_hub_rejects_worker_done()
    test_peer_module_importable()
    print("PASS: lib unit checks")


def _run_demo(args: list[str], timeout: float = 60.0) -> subprocess.CompletedProcess:
    cmd = [sys.executable, DEMO] + args
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=os.path.dirname(HERE)
    )


def test_happy_iphone_frontend() -> None:
    if not os.path.isfile(DEMO):
        _fail(f"missing demo entry {DEMO}")
    cp = _run_demo(["--topology", "iphone-frontend", "--timeout", "8", "--token-count", "4"])
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout + "\n" + cp.stderr)
        _fail(f"iphone-frontend expected exit 0, got {cp.returncode}")
    out = cp.stdout + cp.stderr
    assert "iPhone" in out or "ios" in out
    assert "Windows" in out or "windows" in out
    print("PASS: happy iphone-frontend")


def test_happy_windows_frontend() -> None:
    cp = _run_demo(["--topology", "windows-frontend", "--timeout", "8", "--token-count", "4"])
    if cp.returncode != 0:
        sys.stderr.write(cp.stdout + "\n" + cp.stderr)
        _fail(f"windows-frontend expected exit 0, got {cp.returncode}")
    print("PASS: happy windows-frontend")


def run_matrix() -> None:
    test_happy_iphone_frontend()
    test_happy_windows_frontend()


def main() -> int:
    run_unit_tests()
    if os.path.isfile(DEMO):
        run_matrix()
    print("ALL PASS: portable_dual_topology_selftest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
