#!/usr/bin/env python3
"""Orchestrator + CLI for Phase B portable dual-topology simulation."""
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

from portable_dual_topology_lib import (
    DEFAULT_HOST,
    DEFAULT_TIMEOUT_S,
    DEFAULT_TOKEN_COUNT,
    DISCOVERY_MULTICAST_GROUP,
    DISCOVERY_PORT,
    HUB_READY_PREFIX,
    PORTABLE_DISPLAY,
    PORTABLE_SEATS,
    PORTABLE_SERVICE_ID,
    TOPOLOGY_CHOICES,
    resolve_topology,
)
from portable_dual_topology_hub import run_hub
from portable_dual_topology_peer import run_peer


def run_one_topology(
    script_path: str,
    topology: str,
    host: str,
    port: int,
    timeout_s: float,
    *,
    fail_platforms: Sequence[str] = (),
    fail_discovery: bool = False,
    kill_worker: Optional[str] = None,
    discovery_port: int = DISCOVERY_PORT,
    token_count: int = DEFAULT_TOKEN_COUNT,
    usable_gb: Optional[float] = None,
    fail_role_mismatch: bool = False,
) -> int:
    fail_set = set(fail_platforms)
    for p in fail_set:
        if p not in PORTABLE_SEATS:
            print(f"error: unknown --fail-platform {p}", file=sys.stderr)
            return 2
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

    frontend_platform, worker_platform = resolve_topology(topology)

    print("ShareCompute portable dual-topology sim (Phase B)")
    print("================================================")
    print(f"Topology: {topology}  (frontend={frontend_platform}, worker={worker_platform})")
    print("Mode: UDP discovery + TCP control join + TCP activation pipeline")
    print(
        f"Discovery {DISCOVERY_MULTICAST_GROUP}:{discovery_port}  "
        f"service={PORTABLE_SERVICE_ID}  "
        f"hub TCP {host}:{port}  timeout={timeout_s:.1f}s  tokens={token_count}"
    )
    print(f"Frontend seat: {PORTABLE_DISPLAY[frontend_platform]}")
    if usable_gb is not None:
        print(f"Peer usable GB override: {usable_gb}")
    if fail_discovery:
        print("Negative test: hub will NOT announce (--fail-discovery)")
    if fail_set:
        print(
            "Negative test: omitting peer process(es): "
            + ", ".join(PORTABLE_DISPLAY[p] for p in sorted(fail_set))
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
    kill_evidence: List[str] = []
    kill_lock = threading.Lock()

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
            "ios,windows",
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

        seats = [(frontend_platform, "frontend"), (worker_platform, "worker")]
        if fail_role_mismatch:
            seats = [(frontend_platform, "worker"), (worker_platform, "frontend")]
            print("Negative test: --fail-role-mismatch (swapped JOIN roles)", flush=True)
        for platform, role in seats:
            if platform in fail_set:
                print(
                    f"  * skipping peer for {PORTABLE_DISPLAY[platform]} (--fail-platform)",
                    flush=True,
                )
                continue
            peer_cmd = [
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
                "--frontend-platform",
                frontend_platform,
            ]
            if usable_gb is not None:
                peer_cmd.extend(["--usable-gb", str(usable_gb)])
            proc = subprocess.Popen(
                peer_cmd,
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
                if "discovery failed" in line or "discovery fail" in line:
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
                    with kill_lock:
                        kill_evidence.append(
                            "no-worker-target: all workers omitted or already gone"
                        )
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
                            with kill_lock:
                                kill_evidence.append(
                                    f"worker-{platform}-already-exited-before-kill "
                                    f"(exit={proc.returncode})"
                                )
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
                if proc.poll() is not None:
                    with kill_lock:
                        kill_evidence.append(
                            f"worker-{platform}-already-exited-before-kill "
                            f"(exit={proc.returncode})"
                        )
                    return
                print(
                    f"\n  + orchestrator: SIGKILL worker {PORTABLE_DISPLAY[platform]} "
                    f"(pid {proc.pid}) --kill-worker {kill_worker}\n",
                    flush=True,
                )
                try:
                    proc.kill()  # SIGKILL on POSIX
                except OSError as exc:
                    with kill_lock:
                        kill_evidence.append(f"kill-failed:{platform}:{exc}")
                    return
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    with kill_lock:
                        kill_evidence.append(
                            f"kill-sent-but-still-alive:{platform}:pid={proc.pid}"
                        )
                    return
                if proc.poll() is None:
                    with kill_lock:
                        kill_evidence.append(
                            f"kill-sent-but-still-alive:{platform}:pid={proc.pid}"
                        )
                    return
                with kill_lock:
                    kill_evidence.append(
                        f"SIGKILL-ok:{platform}:pid={proc.pid}:exit={proc.returncode}"
                    )
                print(
                    f"  + orchestrator: kill evidence recorded for {PORTABLE_DISPLAY[platform]} "
                    f"(exit={proc.returncode})",
                    flush=True,
                )

            killer = threading.Thread(target=kill_loop, daemon=True)
            killer.start()

        # Hub may spend timeout_s on joins, then max(timeout_s, 15) on the pipeline.
        # Cover the full join + pipeline budget plus a small margin (and kill margin).
        pipe_budget = max(timeout_s, 15.0)
        overall_timeout = timeout_s + pipe_budget + (12.0 if kill_worker else 5.0)
        try:
            returncode = hub.wait(timeout=overall_timeout)
        except subprocess.TimeoutExpired:
            print("error: hub hung past deadline (no hang tolerated)", file=sys.stderr)
            terminate_all()
            return 1

        if killer is not None:
            killer.join(timeout=2.0)

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
            names = (
                ", ".join(PORTABLE_DISPLAY.get(p, p) for p in failed) if failed else "all peers"
            )
            print(
                f"\nFAIL: discovery failed - no {PORTABLE_SERVICE_ID} beacon heard by: {names}",
                flush=True,
            )
            print(
                f"(expected multicast {DISCOVERY_MULTICAST_GROUP}:{discovery_port}; "
                "hub beacon disabled or wrong discovery port)",
                flush=True,
            )
            return 1

        if kill_worker:
            with kill_lock:
                evidence = list(kill_evidence)
            ok_kill = any(e.startswith("SIGKILL-ok:") for e in evidence)
            if not ok_kill:
                print(
                    "\nFAIL: --kill-worker did not record successful SIGKILL evidence "
                    f"(got: {evidence or ['none']})",
                    flush=True,
                )
                return 1
            # Hard-fail expectation: non-zero, no hang (already enforced by wait timeout).
            if int(returncode) == 0:
                print(
                    "\nFAIL: expected non-zero exit after --kill-worker, but hub returned 0 "
                    f"(kill evidence: {evidence})",
                    flush=True,
                )
                return 1
            print(
                f"\nPASS (negative): worker killed mid-run -> hub exit {returncode} "
                f"(kill evidence: {evidence[0]}; no hang)",
                flush=True,
            )
            return 1

        return int(returncode)
    except KeyboardInterrupt:
        terminate_all()
        return 130
    finally:
        terminate_all()


def run_orchestrator(
    script_path: str,
    host: str,
    port: int,
    timeout_s: float,
    fail_platforms: Sequence[str],
    *,
    topology: str = "both",
    fail_discovery: bool = False,
    kill_worker: Optional[str] = None,
    discovery_port: int = DISCOVERY_PORT,
    token_count: int = DEFAULT_TOKEN_COUNT,
    usable_gb: Optional[float] = None,
    fail_role_mismatch: bool = False,
) -> int:
    """Run one topology, or both sequentially with aggregated exit codes.

    For ``--topology both``, exit 0 only if BOTH topology runs exit 0
    (never last-wins mask).
    """
    if topology == "both":
        print("=== Topology A: iphone-frontend ===", flush=True)
        rc_a = run_one_topology(
            script_path,
            "iphone-frontend",
            host,
            port,
            timeout_s,
            fail_platforms=fail_platforms,
            fail_discovery=fail_discovery,
            kill_worker=kill_worker,
            discovery_port=discovery_port,
            token_count=token_count,
            usable_gb=usable_gb,
            fail_role_mismatch=fail_role_mismatch,
        )
        print(f"\n=== Topology A result: {rc_a} ===\n", flush=True)
        # Fresh ephemeral port for second topology if auto-assigned.
        port_b = 0 if port <= 0 else port
        print("=== Topology B: windows-frontend ===", flush=True)
        rc_b = run_one_topology(
            script_path,
            "windows-frontend",
            host,
            port_b,
            timeout_s,
            fail_platforms=fail_platforms,
            fail_discovery=fail_discovery,
            kill_worker=kill_worker,
            discovery_port=discovery_port,
            token_count=token_count,
            usable_gb=usable_gb,
            fail_role_mismatch=fail_role_mismatch,
        )
        print(f"\n=== Topology B result: {rc_b} ===", flush=True)
        # Aggregate: exit 0 only if both exit 0 (never last-wins).
        if rc_a == 0 and rc_b == 0:
            print("\nBOTH topologies PASS", flush=True)
            return 0
        print(
            f"\nFAIL: --topology both requires both runs exit 0 "
            f"(iphone-frontend={rc_a}, windows-frontend={rc_b})",
            flush=True,
        )
        return rc_a if rc_a != 0 else rc_b

    return run_one_topology(
        script_path,
        topology,
        host,
        port,
        timeout_s,
        fail_platforms=fail_platforms,
        fail_discovery=fail_discovery,
        kill_worker=kill_worker,
        discovery_port=discovery_port,
        token_count=token_count,
        usable_gb=usable_gb,
        fail_role_mismatch=fail_role_mismatch,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Phase B portable dual-topology sim: discovery + shard plan + activation TCP path."
        )
    )
    p.add_argument("--role", choices=("orchestrator", "hub", "peer"), default="orchestrator")
    p.add_argument("--topology", choices=TOPOLOGY_CHOICES, default="both")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--platform", choices=PORTABLE_SEATS)
    p.add_argument("--peer-role", choices=("frontend", "worker"), dest="peer_role")
    p.add_argument("--expect", default=",".join(PORTABLE_SEATS))
    p.add_argument(
        "--frontend-platform",
        default="ios",
        choices=PORTABLE_SEATS,
    )
    p.add_argument("--token-count", type=int, default=DEFAULT_TOKEN_COUNT)
    p.add_argument(
        "--fail-platform",
        action="append",
        default=[],
        choices=PORTABLE_SEATS,
        dest="fail_platforms",
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
        help="SIGKILL a worker mid/start run -> expect exit 1, no hang",
    )
    p.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Peer role: connect with --host/--port instead of beacon listen",
    )
    p.add_argument(
        "--fail-role-mismatch",
        action="store_true",
        help="Swap JOIN roles (frontend<->worker) for negative role tests",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    here = os.path.dirname(os.path.abspath(__file__))
    demo_entry = os.path.join(here, "portable_dual_topology_demo.py")
    script_path = demo_entry if os.path.isfile(demo_entry) else os.path.abspath(__file__)

    if args.token_count <= 0:
        print(
            f"error: --token-count must be a positive integer (got {args.token_count})",
            file=sys.stderr,
        )
        return 1

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
        topology=args.topology,
        fail_discovery=args.fail_discovery or args.no_beacon,
        kill_worker=args.kill_worker,
        discovery_port=args.discovery_port,
        token_count=args.token_count,
        usable_gb=args.usable_gb,
        fail_role_mismatch=args.fail_role_mismatch,
    )


if __name__ == "__main__":
    raise SystemExit(main())
