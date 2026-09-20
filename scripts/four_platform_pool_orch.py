#!/usr/bin/env python3
"""Orchestrator + CLI for the four-platform pool demo."""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import List, Optional, Sequence

from four_platform_pool_lib import (
    DEFAULT_HOST,
    DEFAULT_TIMEOUT_S,
    DISCOVERY_MULTICAST_GROUP,
    DISCOVERY_PORT,
    DISPLAY,
    HUB_READY_PREFIX,
    PLATFORMS,
    SERVICE_ID,
)
from four_platform_pool_hub import run_hub
from four_platform_pool_peer import run_peer


def run_orchestrator(
    script_path: str,
    host: str,
    port: int,
    timeout_s: float,
    fail_platforms: Sequence[str],
    *,
    fail_discovery: bool = False,
    discovery_port: int = DISCOVERY_PORT,
) -> int:
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
    print("Mode: UDP multicast discovery + multi-process TCP join")
    print(
        f"Discovery {DISCOVERY_MULTICAST_GROUP}:{discovery_port}  "
        f"hub TCP {host}:{port}  join_timeout={timeout_s:.1f}s"
    )
    if fail_discovery:
        print("Negative test: hub will NOT announce (--fail-discovery / --no-beacon)")
    if fail_set:
        print(
            "Negative test: omitting peer process(es): "
            + ", ".join(DISPLAY[p] for p in sorted(fail_set))
        )
    print()

    py = sys.executable
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    children: List[subprocess.Popen] = []
    discovery_failed_peers: List[str] = []
    discovery_lock = threading.Lock()

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
        hub_cmd = [
            py,
            script_path,
            "--role",
            "hub",
            "--host",
            host,
            "--port",
            str(port),
            "--timeout",
            str(timeout_s),
            "--expect",
            ",".join(PLATFORMS),
            "--discovery-port",
            str(discovery_port),
        ]
        if fail_discovery:
            hub_cmd.append("--no-beacon")

        hub = subprocess.Popen(
            hub_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
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

        # Brief settle so beacon thread is publishing before peers listen.
        if not fail_discovery:
            time.sleep(0.15)

        for platform in PLATFORMS:
            if platform in fail_set:
                print(
                    f"  · skipping peer process for {DISPLAY[platform]} (--fail-platform)",
                    flush=True,
                )
                continue
            children.append(
                subprocess.Popen(
                    [
                        py,
                        script_path,
                        "--role",
                        "peer",
                        "--platform",
                        platform,
                        "--timeout",
                        str(timeout_s),
                        "--discovery-port",
                        str(discovery_port),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
            )

        def drain(proc: subprocess.Popen, label: str) -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                sys.stdout.write(line if label == "hub" else f"[{label}] {line}")
                sys.stdout.flush()
                if "discovery failed" in line:
                    with discovery_lock:
                        if label not in discovery_failed_peers:
                            discovery_failed_peers.append(label)

        threads = [threading.Thread(target=drain, args=(hub, "hub"), daemon=True)]
        peer_platforms = [p for p in PLATFORMS if p not in fail_set]
        for proc, platform in zip(children[1:], peer_platforms):
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

        with discovery_lock:
            failed = list(discovery_failed_peers)

        if fail_discovery or failed:
            names = ", ".join(DISPLAY.get(p, p) for p in failed) if failed else "all peers"
            print(
                f"\nFAIL: discovery failed — no {SERVICE_ID} beacon heard by: {names}",
                flush=True,
            )
            print(
                f"(expected multicast {DISCOVERY_MULTICAST_GROUP}:{discovery_port}; "
                "hub beacon disabled or wrong discovery port)",
                flush=True,
            )
            return 1

        return int(returncode)
    except KeyboardInterrupt:
        terminate_all()
        return 130
    finally:
        terminate_all()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Four-platform pool demo with UDP LAN discovery + TCP joins."
    )
    p.add_argument("--role", choices=("orchestrator", "hub", "peer"), default="orchestrator")
    p.add_argument("--host", default=DEFAULT_HOST, help="TCP bind/connect host (hub)")
    p.add_argument("--port", type=int, default=0, help="TCP port (hub); 0 = ephemeral")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--platform", choices=PLATFORMS)
    p.add_argument("--expect", default=",".join(PLATFORMS))
    p.add_argument(
        "--fail-platform",
        action="append",
        default=[],
        choices=PLATFORMS,
        dest="fail_platforms",
    )
    p.add_argument(
        "--fail-discovery",
        action="store_true",
        help="Orchestrator: hub does not announce; peers must fail discovery",
    )
    p.add_argument(
        "--no-beacon",
        action="store_true",
        help="Hub role: do not send UDP discovery beacons",
    )
    p.add_argument(
        "--discovery-port",
        type=int,
        default=DISCOVERY_PORT,
        help=f"UDP multicast discovery port (default {DISCOVERY_PORT})",
    )
    p.add_argument(
        "--usable-gb",
        type=float,
        default=None,
    )
    p.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Peer role: connect with --host/--port instead of listening for a beacon",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    demo_entry = os.path.join(here, "four_platform_pool_demo.py")
    script_path = demo_entry if os.path.isfile(demo_entry) else os.path.abspath(__file__)
    if args.role == "hub":
        if args.port <= 0:
            print("error: hub role requires --port", file=sys.stderr)
            return 2
        expected = [x.strip() for x in args.expect.split(",") if x.strip()]
        enable_beacon = not args.no_beacon and not args.fail_discovery
        return run_hub(
            args.host,
            args.port,
            expected,
            args.timeout,
            enable_beacon=enable_beacon,
            discovery_port=args.discovery_port,
        )
    if args.role == "peer":
        if not args.platform:
            print("error: peer role requires --platform", file=sys.stderr)
            return 2
        if args.skip_discovery:
            if args.port <= 0:
                print("error: --skip-discovery requires --port", file=sys.stderr)
                return 2
            return run_peer(
                args.platform,
                args.timeout,
                usable_gb=args.usable_gb,
                discovery_port=args.discovery_port,
                hub_host=args.host,
                hub_port=args.port,
            )
        return run_peer(
            args.platform,
            args.timeout,
            usable_gb=args.usable_gb,
            discovery_port=args.discovery_port,
        )
    return run_orchestrator(
        script_path,
        args.host,
        args.port,
        args.timeout,
        args.fail_platforms,
        fail_discovery=args.fail_discovery or args.no_beacon,
        discovery_port=args.discovery_port,
    )


if __name__ == "__main__":
    raise SystemExit(main())
