#!/usr/bin/env python3
"""TCP hub + UDP beacon announcer for the four-platform pool demo."""
from __future__ import annotations

import select
import socket
import sys
import threading
import time
import uuid
from typing import Dict, Optional, Sequence, Set

from four_platform_pool_lib import (
    BEACON_INTERVAL_S,
    DEFAULT_USABLE_GB,
    DISCOVERY_MULTICAST_GROUP,
    DISCOVERY_PORT,
    DISPLAY,
    HUB_READY_PREFIX,
    PLATFORMS,
    SERVICE_ID,
    Peer,
    announce_beacon,
    open_beacon_sender,
    plan_shards,
    recv_line,
    send_msg,
)


def run_hub(
    host: str,
    port: int,
    expected: Sequence[str],
    timeout_s: float,
    *,
    enable_beacon: bool = True,
    discovery_port: int = DISCOVERY_PORT,
) -> int:
    expected_set: Set[str] = set(expected)
    for p in expected_set:
        if p not in PLATFORMS:
            print(f"error: unknown platform in --expect: {p}", file=sys.stderr)
            return 2
    epoch = uuid.uuid4().hex[:12]
    print("ShareCompute four-platform pool demo (Python / UDP discovery + TCP hub)")
    print("======================================================================")
    print(f"Listening on {host}:{port}  timeout={timeout_s:.1f}s")
    print(f"Expecting platforms: {', '.join(sorted(expected_set))}")
    if enable_beacon:
        print(
            f"Beacon: multicast {DISCOVERY_MULTICAST_GROUP}:{discovery_port} "
            f"service={SERVICE_ID} epoch={epoch}",
            flush=True,
        )
    else:
        print("Beacon: DISABLED (--no-beacon / --fail-discovery)", flush=True)
    print()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(16)
    server.setblocking(False)
    bound_port = server.getsockname()[1]
    print(f"{HUB_READY_PREFIX} {bound_port}", flush=True)

    connected: Dict[str, Peer] = {}
    lock = threading.Lock()
    deadline = time.monotonic() + timeout_s
    done = threading.Event()
    stop_beacon = threading.Event()
    beacon_thread: Optional[threading.Thread] = None

    if enable_beacon:
        sender = open_beacon_sender()

        def beacon_loop() -> None:
            try:
                while not stop_beacon.is_set() and not done.is_set():
                    try:
                        announce_beacon(
                            sender,
                            host,
                            bound_port,
                            epoch,
                            sorted(expected_set),
                            discovery_port=discovery_port,
                        )
                    except OSError as exc:
                        print(f"  ! beacon send error: {exc}", file=sys.stderr, flush=True)
                    stop_beacon.wait(BEACON_INTERVAL_S)
            finally:
                try:
                    sender.close()
                except OSError:
                    pass

        beacon_thread = threading.Thread(target=beacon_loop, name="hub-beacon", daemon=True)
        beacon_thread.start()

    def handle_client(conn: socket.socket, addr) -> None:
        buf = bytearray()
        try:
            conn.setblocking(False)
            msg = recv_line(conn, buf, deadline)
            if msg is None or msg.get("type") != "join":
                return
            platform = str(msg.get("platform", ""))
            if platform not in PLATFORMS:
                return
            node_id = str(msg.get("node_id") or f"sim-{platform}")
            usable_gb = float(msg.get("usable_gb", DEFAULT_USABLE_GB[platform]))
            peer = Peer(platform, node_id, usable_gb)
            with lock:
                connected[platform] = peer
                print(
                    f"  ✓ {DISPLAY[platform]} joined via TCP as {peer.node_id} "
                    f"({peer.usable_gb:.0f} GB usable) from {addr[0]}:{addr[1]}",
                    flush=True,
                )
                send_msg(conn, {"type": "ack", "node_id": node_id, "platform": platform, "epoch": epoch})
                if expected_set.issubset(connected.keys()):
                    done.set()
            while not done.is_set() and time.monotonic() < deadline + 1:
                time.sleep(0.05)
            try:
                send_msg(conn, {"type": "shutdown", "reason": "hub-complete"})
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001
            print(f"  ! client {addr} error: {exc}", file=sys.stderr, flush=True)
        finally:
            try:
                conn.close()
            except OSError:
                pass

    try:
        while not done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([server], [], [], min(0.2, remaining))
            if not ready:
                continue
            try:
                conn, addr = server.accept()
            except BlockingIOError:
                continue
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    finally:
        done.set()
        stop_beacon.set()
        if beacon_thread is not None:
            beacon_thread.join(timeout=1.0)
        try:
            server.close()
        except OSError:
            pass

    missing = sorted(expected_set - set(connected.keys()))
    if missing:
        print("\nFAIL: platform(s) did not join within deadline:", flush=True)
        for platform in missing:
            print(f"  ✗ {DISPLAY[platform]} — no TCP join", flush=True)
        joined = sorted(connected.keys())
        if joined:
            print("Joined OK: " + ", ".join(DISPLAY[p] for p in joined), flush=True)
        if not enable_beacon:
            print(
                "\nDiscovery note: hub beacon was disabled — peers likely failed UDP discovery "
                f"on {DISCOVERY_MULTICAST_GROUP}:{discovery_port}.",
                flush=True,
            )
        print(
            f"\nDemo failed: {len(missing)} of {len(expected_set)} required platform(s) missing.",
            flush=True,
        )
        return 1

    print("\n  All required platforms joined over TCP (after UDP discovery).\n", flush=True)
    peers = [connected[p] for p in sorted(connected)]
    total_gb = sum(p.usable_gb for p in peers)
    shards = plan_shards(peers, 48, 24.0, 0.5)
    print(f"RAM pool ready — {total_gb:.1f} GB across " + ", ".join(DISPLAY[p.platform] for p in peers))
    print(f"Epoch {epoch}  model demo-48L  layers 48\n")
    print("Shard plan:")
    for s in shards:
        print(
            f"  rank {s.rank}  {DISPLAY[s.platform].ljust(7)}  {s.node_id}  "
            f"layers [{s.start_layer},{s.end_layer})  ~{s.estimated_gb:.2f} GB"
        )
    print(
        "\nSuccess: Windows, macOS, iOS, and Android discovered the hub via UDP multicast "
        "and joined via TCP."
    )
    print(
        "(Platform runtimes are still simulated profiles — discovery is LAN-style UDP, "
        "connect is multi-process TCP. See docs/FOUR-PLATFORM-CONNECT.md)"
    )
    return 0
