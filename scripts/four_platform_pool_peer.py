#!/usr/bin/env python3
"""UDP discovery + TCP join peer for the four-platform pool demo."""
from __future__ import annotations

import socket
import sys
import time
from typing import Optional

from four_platform_pool_lib import (
    DEFAULT_USABLE_GB,
    DISCOVERY_MULTICAST_GROUP,
    DISCOVERY_PORT,
    DISPLAY,
    PLATFORMS,
    SERVICE_ID,
    discover_hub,
    recv_line,
    send_msg,
)


def run_peer(
    platform: str,
    timeout_s: float,
    *,
    usable_gb: Optional[float] = None,
    discovery_port: int = DISCOVERY_PORT,
    hub_host: Optional[str] = None,
    hub_port: Optional[int] = None,
) -> int:
    if platform not in PLATFORMS:
        print(f"error: unknown platform {platform}", file=sys.stderr)
        return 2
    gb = float(usable_gb if usable_gb is not None else DEFAULT_USABLE_GB[platform])
    node_id = f"sim-{platform}"
    deadline = time.monotonic() + timeout_s

    if hub_host is None or hub_port is None or hub_port <= 0:
        remaining = max(0.1, deadline - time.monotonic())
        print(
            f"peer {DISPLAY[platform]}: listening for {SERVICE_ID} beacon on "
            f"{DISCOVERY_MULTICAST_GROUP}:{discovery_port} (timeout {remaining:.1f}s)",
            flush=True,
        )
        beacon = discover_hub(remaining, discovery_port=discovery_port)
        if beacon is None:
            print(
                f"peer {DISPLAY[platform]}: discovery failed — no {SERVICE_ID} beacon on "
                f"{DISCOVERY_MULTICAST_GROUP}:{discovery_port} within {timeout_s:.1f}s",
                file=sys.stderr,
                flush=True,
            )
            return 1
        hub_host = beacon.hub_host
        hub_port = beacon.hub_port
        print(
            f"peer {DISPLAY[platform]}: discovered hub {hub_host}:{hub_port} "
            f"(epoch={beacon.epoch})",
            flush=True,
        )
    else:
        print(
            f"peer {DISPLAY[platform]}: using explicit hub {hub_host}:{hub_port} (skip discovery)",
            flush=True,
        )

    last_err: Optional[BaseException] = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            sock.connect((hub_host, int(hub_port)))
            send_msg(
                sock,
                {"type": "join", "platform": platform, "node_id": node_id, "usable_gb": gb},
            )
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
    print(
        f"peer {DISPLAY[platform]}: failed to join {hub_host}:{hub_port} within "
        f"{timeout_s:.1f}s ({last_err})",
        file=sys.stderr,
        flush=True,
    )
    return 1
