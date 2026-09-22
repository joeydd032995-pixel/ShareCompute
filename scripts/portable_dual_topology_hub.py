#!/usr/bin/env python3
"""Control-plane hub for Phase B portable dual-topology simulation.

UDP beacon + TCP JOIN (with role) → StagePlanner-equivalent shard plan → broadcast
PLAN/epoch to all peers. Does not carry activations (data plane is peer-to-peer).
Product seats only: ios + windows.
"""
from __future__ import annotations

import select
import socket
import sys
import threading
import time
import uuid
from typing import Dict, List, Optional, Sequence, Set, Tuple

from portable_dual_topology_lib import (
    BEACON_INTERVAL_S,
    DEFAULT_HOST,
    DEFAULT_TOKEN_COUNT,
    DISCOVERY_MULTICAST_GROUP,
    DISCOVERY_PORT,
    HUB_READY_PREFIX,
    PORTABLE_DEFAULT_USABLE_GB,
    PORTABLE_DISPLAY,
    PORTABLE_SEATS,
    PORTABLE_SERVICE_ID,
    PortablePeer,
    announce_portable_beacon,
    open_beacon_sender,
    plan_portable_shards,
    recv_line,
    send_msg,
    shards_to_plan_dict,
)


def run_hub(
    host: str,
    port: int,
    expected: Sequence[str],
    timeout_s: float,
    *,
    frontend_platform: str,
    token_count: int = DEFAULT_TOKEN_COUNT,
    enable_beacon: bool = True,
    discovery_port: int = DISCOVERY_PORT,
) -> int:
    expected_set: Set[str] = set(expected)
    if expected_set != set(PORTABLE_SEATS):
        print(
            f"error: expected seats must equal {PORTABLE_SEATS}, got {sorted(expected_set)}",
            file=sys.stderr,
        )
        return 2
    if frontend_platform not in expected_set:
        print(f"error: frontend platform {frontend_platform} not in expected set", file=sys.stderr)
        return 2

    epoch = uuid.uuid4().hex[:12]
    print("ShareCompute portable dual-topology sim (Phase B) — control-plane hub")
    print("=====================================================================")
    print(f"Listening on {host}:{port}  timeout={timeout_s:.1f}s")
    print(f"Expecting platforms: {', '.join(sorted(expected_set))}")
    print(f"Frontend seat: {PORTABLE_DISPLAY[frontend_platform]}  tokens={token_count}")
    if enable_beacon:
        print(
            f"Beacon: multicast {DISCOVERY_MULTICAST_GROUP}:{discovery_port} "
            f"service={PORTABLE_SERVICE_ID} epoch={epoch}",
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

    connected: Dict[str, PortablePeer] = {}
    client_conns: Dict[str, Tuple[socket.socket, bytearray]] = {}
    lock = threading.Lock()
    deadline = time.monotonic() + timeout_s
    all_joined = threading.Event()
    pipeline_done = threading.Event()
    fail_reason: List[str] = []
    stop_beacon = threading.Event()
    beacon_thread: Optional[threading.Thread] = None

    if enable_beacon:
        sender = open_beacon_sender()

        def beacon_loop() -> None:
            try:
                while not stop_beacon.is_set():
                    try:
                        announce_portable_beacon(
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
        platform: Optional[str] = None
        try:
            conn.setblocking(False)
            msg = recv_line(conn, buf, deadline)
            if msg is None or msg.get("type") != "join":
                return
            platform = str(msg.get("platform", ""))
            if platform not in PORTABLE_SEATS:
                print(
                    f"  \u2717 reject JOIN: non-product platform {platform!r}",
                    flush=True,
                )
                send_msg(conn, {"type": "error", "reason": "non-product-platform"})
                return
            role = str(msg.get("role") or ("frontend" if platform == frontend_platform else "worker"))
            if role not in ("frontend", "worker"):
                send_msg(conn, {"type": "error", "reason": "bad-role"})
                return
            # Only the configured frontend platform may JOIN as frontend; that
            # platform must JOIN as frontend (reject role/platform mismatches).
            if role == "frontend" and platform != frontend_platform:
                print(
                    f"  \u2717 reject JOIN: {PORTABLE_DISPLAY[platform]} claimed frontend "
                    f"(reserved for {PORTABLE_DISPLAY[frontend_platform]})",
                    flush=True,
                )
                send_msg(
                    conn,
                    {
                        "type": "error",
                        "reason": "frontend-role-reserved",
                        "frontend_platform": frontend_platform,
                    },
                )
                return
            if platform == frontend_platform and role != "frontend":
                print(
                    f"  \u2717 reject JOIN: {PORTABLE_DISPLAY[platform]} must JOIN as frontend "
                    f"(got role={role})",
                    flush=True,
                )
                send_msg(
                    conn,
                    {
                        "type": "error",
                        "reason": "frontend-must-join-as-frontend",
                        "frontend_platform": frontend_platform,
                    },
                )
                return
            node_id = str(msg.get("node_id") or f"sim-{platform}")
            usable_gb = float(msg.get("usable_gb", PORTABLE_DEFAULT_USABLE_GB[platform]))
            data_host = str(msg.get("data_host") or DEFAULT_HOST)
            data_port = int(msg.get("data_port") or 0)
            if data_port <= 0:
                send_msg(conn, {"type": "error", "reason": "missing-data-port"})
                return
            peer = PortablePeer(platform, node_id, usable_gb, role, data_host, data_port)
            with lock:
                # One seat per platform for this demo.
                connected[platform] = peer
                client_conns[platform] = (conn, buf)
                print(
                    f"  \u2713 {PORTABLE_DISPLAY[platform]} JOIN role={role} node={node_id} "
                    f"usable={usable_gb:.0f}GB data={data_host}:{data_port} from {addr[0]}:{addr[1]}",
                    flush=True,
                )
                send_msg(
                    conn,
                    {
                        "type": "ack",
                        "node_id": node_id,
                        "platform": platform,
                        "role": role,
                        "epoch": epoch,
                    },
                )
                if expected_set.issubset(connected.keys()):
                    all_joined.set()

            # Stay attached until pipeline completes or deadline.
            while not pipeline_done.is_set() and time.monotonic() < deadline + 30:
                # Frontend may send "done" / workers may send "pipeline-error".
                peek_deadline = min(time.monotonic() + 0.2, deadline + 30)
                msg2 = recv_line(conn, buf, peek_deadline)
                if msg2 is None:
                    # Distinguish timeout vs peer hang-up: try a zero-length readiness check.
                    try:
                        ready, _, _ = select.select([conn], [], [], 0)
                        if ready:
                            probe = conn.recv(1, socket.MSG_PEEK)
                            if not probe:
                                with lock:
                                    fail_reason.append(f"peer-{platform}-disconnected")
                                pipeline_done.set()
                                return
                    except OSError:
                        with lock:
                            fail_reason.append(f"peer-{platform}-disconnected")
                        pipeline_done.set()
                        return
                    continue
                mtype = msg2.get("type")
                if mtype == "done":
                    print(f"  \u2713 frontend reported pipeline done ({msg2.get('tokens')} tokens)", flush=True)
                    pipeline_done.set()
                    return
                if mtype == "pipeline-error":
                    reason = str(msg2.get("reason") or "peer-error")
                    print(f"  \u2717 peer {platform} pipeline-error: {reason}", flush=True)
                    with lock:
                        fail_reason.append(reason)
                    pipeline_done.set()
                    return
        except Exception as exc:  # noqa: BLE001
            print(f"  ! client {addr} error: {exc}", file=sys.stderr, flush=True)
            with lock:
                fail_reason.append(f"client-error:{exc}")
            pipeline_done.set()
        finally:
            # Connection kept open until shutdown broadcast below.
            pass

    accept_threads: List[threading.Thread] = []
    try:
        while not all_joined.is_set():
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
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            accept_threads.append(t)
            t.start()

        if not all_joined.is_set():
            missing = sorted(expected_set - set(connected.keys()))
            print("\nFAIL: platform(s) did not join within deadline:", flush=True)
            for platform in missing:
                print(f"  \u2717 {PORTABLE_DISPLAY[platform]} \u2014 no TCP join", flush=True)
            joined = sorted(connected.keys())
            if joined:
                print("Joined OK: " + ", ".join(PORTABLE_DISPLAY[p] for p in joined), flush=True)
            if not enable_beacon:
                print(
                    "\nDiscovery note: hub beacon was disabled \u2014 peers likely failed UDP discovery "
                    f"on {DISCOVERY_MULTICAST_GROUP}:{discovery_port}.",
                    flush=True,
                )
            print(
                f"\nDemo failed: {len(missing)} of {len(expected_set)} required seat(s) missing.",
                flush=True,
            )
            return 1

        # Require exactly one frontend among joined peers.
        frontends = [p for p in connected.values() if p.role == "frontend"]
        if len(frontends) != 1:
            print(f"\nFAIL: expected 1 frontend, got {len(frontends)}", flush=True)
            return 1
        if frontends[0].platform != frontend_platform:
            print(
                f"\nFAIL: frontend seat is {frontends[0].platform}, "
                f"expected {frontend_platform}",
                flush=True,
            )
            return 1

        peers = list(connected.values())
        try:
            shards = plan_portable_shards(peers)
        except ValueError as exc:
            print(f"\nFAIL: shard plan rejected \u2014 {exc}", flush=True)
            with lock:
                conns_snapshot = list(client_conns.items())
            _broadcast_shutdown(conns_snapshot, "plan-rejected")
            return 1
        plan = shards_to_plan_dict(epoch, shards, token_count)

        print("\n  All seats joined. Broadcasting shard plan + epoch.\n", flush=True)
        total_gb = sum(p.usable_gb for p in peers)
        print(f"RAM pool ready \u2014 {total_gb:.1f} GB across " + ", ".join(PORTABLE_DISPLAY[p.platform] for p in peers))
        print(f"Epoch {epoch}  model demo-{sum(s.end_layer - s.start_layer for s in shards)}L  tokens {token_count}\n")
        print("Shard plan (inference pipeline ranks):")
        for s in shards:
            print(
                f"  rank {s.rank}  {PORTABLE_DISPLAY[s.platform].ljust(7)}  role={s.role.ljust(8)}  "
                f"{s.node_id}  layers [{s.start_layer},{s.end_layer})  "
                f"~{s.estimated_gb:.2f} GB  data={s.data_host}:{s.data_port}"
            )
        print(flush=True)

        with lock:
            conns_snapshot = list(client_conns.items())
        for platform, (conn, _buf) in conns_snapshot:
            try:
                send_msg(conn, plan)
            except OSError as exc:
                print(f"  ! failed to send plan to {platform}: {exc}", file=sys.stderr, flush=True)
                return 1

        # Wait for frontend done or peer error / disconnect / overall deadline.
        # Allow extra time beyond join timeout for the token pipeline.
        pipe_deadline = time.monotonic() + max(timeout_s, 15.0)
        while not pipeline_done.is_set() and time.monotonic() < pipe_deadline:
            time.sleep(0.05)

        with lock:
            reasons = list(fail_reason)

        if reasons:
            print(f"\nFAIL: pipeline error \u2014 {reasons[0]}", flush=True)
            _broadcast_shutdown(conns_snapshot, "pipeline-error")
            return 1

        if not pipeline_done.is_set():
            print("\nFAIL: pipeline timed out waiting for frontend done / worker progress", flush=True)
            _broadcast_shutdown(conns_snapshot, "pipeline-timeout")
            return 1

        _broadcast_shutdown(conns_snapshot, "hub-complete")
        print(
            "\nSuccess: discovery \u2192 join \u2192 shard plan \u2192 activation pipeline completed "
            f"({token_count} tokens on frontend).",
            flush=True,
        )
        print(
            "(Compute is fake-but-sized checksum+sleep \u2014 not MLX/Metal. "
            "Phase B portable dual-topology stub.)",
            flush=True,
        )
        return 0
    finally:
        pipeline_done.set()
        stop_beacon.set()
        if beacon_thread is not None:
            beacon_thread.join(timeout=1.0)
        for _p, (conn, _) in list(client_conns.items()):
            try:
                conn.close()
            except OSError:
                pass
        try:
            server.close()
        except OSError:
            pass


def _broadcast_shutdown(conns: List[Tuple[str, Tuple[socket.socket, bytearray]]], reason: str) -> None:
    for _platform, (conn, _) in conns:
        try:
            send_msg(conn, {"type": "shutdown", "reason": reason})
        except OSError:
            pass
