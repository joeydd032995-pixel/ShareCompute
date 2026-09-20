#!/usr/bin/env python3
"""Orchestrator + CLI for Phase A inference-pipeline simulation."""
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

from inference_pipeline_lib import (
    DEFAULT_HOST,
    DEFAULT_TIMEOUT_S,
    DEFAULT_TOKEN_COUNT,
    DISCOVERY_MULTICAST_GROUP,
    DISCOVERY_PORT,
    DISPLAY,
    HUB_READY_PREFIX,
    INFER_SERVICE_ID,
    PLATFORMS,
)
from inference_pipeline_hub import run_hub
from inference_pipeline_peer import run_peer

DEFAULT_FRONTEND = "macos"


def run_orchestrator(
    script_path: str,
    host: str,
    port: int,
    timeout_s: float,
    fail_platforms: Sequence[str],
    *,
    fail_discovery: bool = False,
    omit_worker: bool = False,
    kill_worker: Optional[str] = None,
    discovery_port: int = DISCOVERY_PORT,
    frontend_platform: str = DEFAULT_FRONTEND,
    token_count: int = DEFAULT_TOKEN_COUNT,
) -> int:
    fail_set = set(fail_platforms)
    for p in fail_set:
        if p not in PLATFORMS:
            print(f"error: unknown --fail-platform {p}", file=sys.stderr)
            return 2
    if omit_worker:
        # Omit one non-frontend worker seat.
        for p in PLATFORMS:
            if p != frontend_platform and p not in fail_set:
                fail_set.add(p)
                break
    if kill_worker and kill_worker not in ("mid", "start"):
        print("error: --kill-worker expects 'mid' or 'start'", file=sys.stderr)
        return 2
    # Widen the activation window so SIGKILL lands mid-pipeline, not after success.
    if kill_worker and token_count < 64:
        token_count = 64
    if port <= 0:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind((host, 0))
        port = probe.getsockname()[1]
        probe.close()

    print("ShareCompute inference-pipeline sim (Phase A)")
    print("============================================")
    print("Mode: UDP discovery + TCP control join + TCP activation pipeline")
    print(
        f"Discovery {DISCOVERY_MULTICAST_GROUP}:{discovery_port}  "
        f"hub TCP {host}:{port}  timeout={timeout_s:.1f}s  tokens={token_count}"
    )
    print(f"Frontend seat: {DISPLAY[frontend_platform]}")
    if fail_discovery:
        print("Negative test: hub will NOT announce (--fail-discovery)")
    if fail_set:
        print(
            "Negative test: omitting peer process(es): "
            + ", ".join(DISPLAY[p] for p in sorted(fail_set))
        )
    if kill_worker:
        print(f"Negative test: will SIGKILL a worker ({kill_worker}-run)")
    print()

    py = sys.executable
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    children: List[subprocess.Popen] = []
    peer_meta: List[tuple] = []  # (proc, platform, role)
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
            "--frontend-platform",
            frontend_platform,
            "--token-count",
            str(token_count),
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

        if not fail_discovery:
            time.sleep(0.15)

        for platform in PLATFORMS:
            if platform in fail_set:
                print(
                    f"  · skipping peer process for {DISPLAY[platform]} (--fail-platform/--omit-worker)",
                    flush=True,
                )
                continue
            role = "frontend" if platform == frontend_platform else "worker"
            proc = subprocess.Popen(
                [
                    py,
                    script_path,
                    "--role",
                    "peer",
                    "--platform",
                    platform,
                    "--peer-role",
                    role,
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
            children.append(proc)
            peer_meta.append((proc, platform, role))

        progress_events: List[str] = []
        progress_lock = threading.Lock()

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
                # Markers for --kill-worker timing.
                if "accepted plan" in line or "token 0" in line or "Broadcasting shard plan" in line:
                    with progress_lock:
                        progress_events.append(line.strip())

        threads = [threading.Thread(target=drain, args=(hub, "hub"), daemon=True)]
        for proc, platform, _role in peer_meta:
            threads.append(threading.Thread(target=drain, args=(proc, platform), daemon=True))
        for t in threads:
            t.start()

        # Optional: kill a worker mid-run once pipeline is clearly underway.
        killer: Optional[threading.Thread] = None
        if kill_worker:

            def kill_loop() -> None:
                target = None
                for proc, platform, role in peer_meta:
                    if role == "worker":
                        target = (proc, platform)
                        break
                if target is None:
                    return
                proc, platform = target
                if kill_worker == "start":
                    # After joins begin: brief settle then kill.
                    time.sleep(0.25)
                else:
                    # mid: wait until plan accepted / first token, then kill immediately.
                    deadline_k = time.monotonic() + min(10.0, timeout_s + 2.0)
                    saw_progress = False
                    while time.monotonic() < deadline_k:
                        if proc.poll() is not None:
                            return
                        with progress_lock:
                            # Prefer killing after at least one token started, else after plan.
                            if any("token 0" in e for e in progress_events):
                                saw_progress = True
                                break
                            if any("accepted plan" in e for e in progress_events):
                                saw_progress = True
                                # Tiny window so rank-1 is in recv/send when killed.
                                time.sleep(0.05)
                                break
                        time.sleep(0.02)
                    if not saw_progress:
                        # Fallback: do not let the happy path finish first.
                        time.sleep(0.15)
                if proc.poll() is None:
                    print(
                        f"\n  † orchestrator: SIGKILL worker {DISPLAY[platform]} (pid {proc.pid}) "
                        f"--kill-worker {kill_worker}\n",
                        flush=True,
                    )
                    try:
                        proc.kill()
                    except OSError:
                        pass

            killer = threading.Thread(target=kill_loop, daemon=True)
            killer.start()

        overall_timeout = timeout_s + (12.0 if kill_worker else 8.0)
        try:
            returncode = hub.wait(timeout=overall_timeout)
        except subprocess.TimeoutExpired:
            print("error: hub hung past deadline (no hang tolerated)", file=sys.stderr)
            terminate_all()
            return 1

        for proc, _platform, _role in peer_meta:
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
                f"\nFAIL: discovery failed — no {INFER_SERVICE_ID} beacon heard by: {names}",
                flush=True,
            )
            print(
                f"(expected multicast {DISCOVERY_MULTICAST_GROUP}:{discovery_port}; "
                "hub beacon disabled or wrong discovery port)",
                flush=True,
            )
            return 1

        if kill_worker:
            # Hard-fail expectation: non-zero, no hang (already enforced by wait timeout).
            if int(returncode) == 0:
                print(
                    "\nFAIL: expected non-zero exit after --kill-worker, but hub returned 0",
                    flush=True,
                )
                return 1
            print(
                f"\nPASS (negative): worker killed mid-run → hub exit {returncode} (no hang)",
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
        description="Phase A inference-pipeline sim: discovery + shard plan + activation TCP path."
    )
    p.add_argument("--role", choices=("orchestrator", "hub", "peer"), default="orchestrator")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--platform", choices=PLATFORMS)
    p.add_argument("--peer-role", choices=("frontend", "worker"), dest="peer_role")
    p.add_argument("--expect", default=",".join(PLATFORMS))
    p.add_argument("--frontend-platform", default=DEFAULT_FRONTEND, choices=PLATFORMS)
    p.add_argument("--token-count", type=int, default=DEFAULT_TOKEN_COUNT)
    p.add_argument(
        "--fail-platform",
        action="append",
        default=[],
        choices=PLATFORMS,
        dest="fail_platforms",
    )
    p.add_argument(
        "--omit-worker",
        action="store_true",
        help="Omit one non-frontend worker (same hard-fail as missing seat)",
    )
    p.add_argument(
        "--fail-discovery",
        action="store_true",
        help="Hub does not announce; peers must fail discovery",
    )
    p.add_argument("--no-beacon", action="store_true", help="Hub role: disable UDP beacons")
    p.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    p.add_argument("--usable-gb", type=float, default=None)
    p.add_argument(
        "--kill-worker",
        choices=("mid", "start"),
        default=None,
        help="SIGKILL a worker mid/start run → expect exit 1, no hang",
    )
    p.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Peer role: connect with --host/--port instead of beacon listen",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    demo_entry = os.path.join(here, "inference_pipeline_demo.py")
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
            frontend_platform=args.frontend_platform,
            token_count=args.token_count,
            enable_beacon=enable_beacon,
            discovery_port=args.discovery_port,
        )

    if args.role == "peer":
        if not args.platform:
            print("error: peer role requires --platform", file=sys.stderr)
            return 2
        role = args.peer_role or (
            "frontend" if args.platform == args.frontend_platform else "worker"
        )
        if args.skip_discovery:
            if args.port <= 0:
                print("error: --skip-discovery requires --port", file=sys.stderr)
                return 2
            return run_peer(
                args.platform,
                role,
                args.timeout,
                usable_gb=args.usable_gb,
                discovery_port=args.discovery_port,
                hub_host=args.host,
                hub_port=args.port,
            )
        return run_peer(
            args.platform,
            role,
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
        omit_worker=args.omit_worker,
        kill_worker=args.kill_worker,
        discovery_port=args.discovery_port,
        frontend_platform=args.frontend_platform,
        token_count=args.token_count,
    )


if __name__ == "__main__":
    raise SystemExit(main())
