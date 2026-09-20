#!/usr/bin/env python3
"""Four-platform ShareCompute pool demo — networked multi-process TCP connect.

Spawns a hub + four platform peer OS processes that JOIN over localhost TCP.
If any required platform misses the join deadline, exits non-zero and names it.

    python3 scripts/four_platform_pool_demo.py
    python3 scripts/four_platform_pool_demo.py --fail-platform android

Platform runtimes stay simulated (stock RAM profiles); the connect path is real.
"""
from __future__ import annotations

import argparse
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Sequence, Set

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from four_platform_pool_lib import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_TIMEOUT_S,
    DEFAULT_USABLE_GB,
    DISPLAY,
    HUB_READY_PREFIX,
    PLATFORMS,
    Peer,
    plan_shards,
    recv_line,
    send_msg,
)


def run_hub(host: str, port: int, expected: Sequence[str], timeout_s: float) -> int:
    expected_set: Set[str] = set(expected)
    for p in expected_set:
        if p not in PLATFORMS:
            print(f"error: unknown platform in --expect: {p}", file=sys.stderr)
            return 2
    print("ShareCompute four-platform pool demo (Python / TCP hub)")
    print("=======================================================")
    print(f"Listening on {host}:{port}  timeout={timeout_s:.1f}s")
    print(f"Expecting platforms: {', '.join(sorted(expected_set))}\n")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(16)
    server.setblocking(False)
    print(f"{HUB_READY_PREFIX} {server.getsockname()[1]}", flush=True)
    connected: Dict[str, Peer] = {}
    lock = threading.Lock()
    deadline = time.monotonic() + timeout_s
    done = threading.Event()

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
                print(f"  ✓ {DISPLAY[platform]} joined via TCP as {peer.node_id} "
                      f"({peer.usable_gb:.0f} GB usable) from {addr[0]}:{addr[1]}", flush=True)
                send_msg(conn, {"type": "ack", "node_id": node_id, "platform": platform})
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
        print(f"\nDemo failed: {len(missing)} of {len(expected_set)} required platform(s) missing.", flush=True)
        return 1

    print("\n  All required platforms joined over TCP.\n", flush=True)
    peers = [connected[p] for p in sorted(connected)]
    total_gb = sum(p.usable_gb for p in peers)
    shards = plan_shards(peers, 48, 24.0, 0.5)
    print(f"RAM pool ready — {total_gb:.1f} GB across " + ", ".join(DISPLAY[p.platform] for p in peers))
    print("Epoch 1  model demo-48L  layers 48\n")
    print("Shard plan:")
    for s in shards:
        print(f"  rank {s.rank}  {DISPLAY[s.platform].ljust(7)}  {s.node_id}  "
              f"layers [{s.start_layer},{s.end_layer})  ~{s.estimated_gb:.2f} GB")
    print("\nSuccess: Windows, macOS, iOS, and Android joined via localhost TCP and are pooled.")
    print("(Platform runtimes are still simulated profiles — connect path is real multi-process TCP. "
          "See docs/FOUR-PLATFORM-CONNECT.md)")
    return 0


def run_peer(host: str, port: int, platform: str, timeout_s: float, usable_gb: Optional[float] = None) -> int:
    if platform not in PLATFORMS:
        print(f"error: unknown platform {platform}", file=sys.stderr)
        return 2
    gb = float(usable_gb if usable_gb is not None else DEFAULT_USABLE_GB[platform])
    node_id = f"sim-{platform}"
    deadline = time.monotonic() + timeout_s
    last_err: Optional[BaseException] = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            sock.connect((host, port))
            send_msg(sock, {"type": "join", "platform": platform, "node_id": node_id, "usable_gb": gb})
            buf = bytearray()
            sock.setblocking(False)
            ack = recv_line(sock, buf, deadline)
            if ack and ack.get("type") == "ack":
                print(f"peer {DISPLAY[platform]}: ack from hub as {node_id}", flush=True)
                _ = recv_line(sock, buf, min(deadline, time.monotonic() + 2.0))
                sock.close()
                return 0
            last_err = RuntimeError(f"unexpected reply: {ack!r}")
        except OSError as exc:
            last_err = exc
        finally:
            try:
                sock.close()
            except OSError:
                pass
        time.sleep(0.05)
    print(f"peer {DISPLAY[platform]}: failed to join {host}:{port} within {timeout_s:.1f}s ({last_err})",
          file=sys.stderr, flush=True)
    return 1


def run_orchestrator(script_path: str, host: str, port: int, timeout_s: float, fail_platforms: Sequence[str]) -> int:
    fail_set = set(fail_platforms)
    for p in fail_set:
        if p not in PLATFORMS:
            print(f"error: unknown --fail-platform {p}", file=sys.stderr)
            return 2
    if port <= 0:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind((host, 0))
        port = probe.getsockname()[1]
        probe.close()
    print("ShareCompute four-platform pool demo (Python)")
    print("============================================")
    print("Mode: multi-process localhost TCP join")
    print(f"Hub {host}:{port}  join_timeout={timeout_s:.1f}s")
    if fail_set:
        print("Negative test: omitting peer process(es): " + ", ".join(DISPLAY[p] for p in sorted(fail_set)))
    print()
    py = sys.executable
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    children: List[subprocess.Popen] = []

    def terminate_all() -> None:
        for proc in children:
            if proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGTERM)
                except OSError:
                    pass
        end = time.monotonic() + 2.0
        for proc in children:
            try:
                proc.wait(timeout=max(0.0, end - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass

    try:
        hub = subprocess.Popen(
            [py, script_path, "--role", "hub", "--host", host, "--port", str(port),
             "--timeout", str(timeout_s), "--expect", ",".join(PLATFORMS)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
        )
        children.append(hub)
        hub_ready = False
        ready_deadline = time.monotonic() + min(5.0, timeout_s + 1.0)
        assert hub.stdout is not None
        while time.monotonic() < ready_deadline:
            line = hub.stdout.readline()
            if not line and hub.poll() is not None:
                break
            if line:
                sys.stdout.write(line)
                sys.stdout.flush()
                if line.startswith(HUB_READY_PREFIX):
                    hub_ready = True
                    break
        if not hub_ready:
            print("error: hub did not become ready", file=sys.stderr)
            terminate_all()
            return 1
        for platform in PLATFORMS:
            if platform in fail_set:
                print(f"  · skipping peer process for {DISPLAY[platform]} (--fail-platform)", flush=True)
                continue
            children.append(subprocess.Popen(
                [py, script_path, "--role", "peer", "--host", host, "--port", str(port),
                 "--platform", platform, "--timeout", str(timeout_s)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
            ))

        def drain(proc: subprocess.Popen, label: str) -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                sys.stdout.write(line if label == "hub" else f"[{label}] {line}")
                sys.stdout.flush()

        threads = [threading.Thread(target=drain, args=(hub, "hub"), daemon=True)]
        for proc, platform in zip(children[1:], [p for p in PLATFORMS if p not in fail_set]):
            threads.append(threading.Thread(target=drain, args=(proc, platform), daemon=True))
        for t in threads:
            t.start()
        try:
            returncode = hub.wait(timeout=timeout_s + 5.0)
        except subprocess.TimeoutExpired:
            print("error: hub hung past deadline", file=sys.stderr)
            terminate_all()
            return 1
        for proc in children[1:]:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except OSError:
                    pass
        return int(returncode)
    except KeyboardInterrupt:
        terminate_all()
        return 130
    finally:
        terminate_all()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Four-platform pool demo with localhost TCP joins.")
    p.add_argument("--role", choices=("orchestrator", "hub", "peer"), default="orchestrator")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--platform", choices=PLATFORMS)
    p.add_argument("--expect", default=",".join(PLATFORMS))
    p.add_argument("--fail-platform", action="append", default=[], choices=PLATFORMS, dest="fail_platforms")
    p.add_argument("--usable-gb", type=float, default=None)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    script_path = os.path.abspath(__file__)
    if args.role == "hub":
        if args.port <= 0:
            print("error: hub role requires --port", file=sys.stderr)
            return 2
        expected = [x.strip() for x in args.expect.split(",") if x.strip()]
        return run_hub(args.host, args.port, expected, args.timeout)
    if args.role == "peer":
        if not args.platform or args.port <= 0:
            print("error: peer role requires --platform and --port", file=sys.stderr)
            return 2
        return run_peer(args.host, args.port, args.platform, args.timeout, args.usable_gb)
    return run_orchestrator(script_path, args.host, args.port, args.timeout, args.fail_platforms)


if __name__ == "__main__":
    raise SystemExit(main())
