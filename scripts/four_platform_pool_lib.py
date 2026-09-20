#!/usr/bin/env python3
"""Shared planning, TCP framing, and UDP LAN discovery for the four-platform pool demo.

Discovery uses IPv4 multicast (239.255.77.77:37777) rather than 255.255.255.255
broadcast: on Linux loopback, subnet broadcast to 127.255.255.255 is flaky across
processes, while multicast with IP_MULTICAST_LOOP=1 reliably delivers same-host
beacons — the simulation case we care about.
"""
from __future__ import annotations

import json
import select
import socket
import struct
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

PLATFORMS = ("windows", "macos", "ios", "android")
DEFAULT_USABLE_GB = {"windows": 16, "macos": 32, "ios": 6, "android": 8}
DISPLAY = {"windows": "Windows", "macos": "macOS", "ios": "iOS", "android": "Android"}
PROTOCOL_VERSION = 1
HUB_READY_PREFIX = "HUB_READY"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_HOST = "127.0.0.1"

# LAN-style discovery (UDP multicast). Documented choice for one-machine sim.
SERVICE_ID = "sharecompute-pool"
DISCOVERY_MULTICAST_GROUP = "239.255.77.77"
DISCOVERY_PORT = 37777
BEACON_INTERVAL_S = 0.2


@dataclass(frozen=True)
class Peer:
    platform: str
    node_id: str
    usable_gb: float


@dataclass(frozen=True)
class Shard:
    rank: int
    platform: str
    node_id: str
    start_layer: int
    end_layer: int
    estimated_gb: float


@dataclass(frozen=True)
class HubBeacon:
    hub_host: str
    hub_port: int
    epoch: str
    platforms: Tuple[str, ...]


def apportion(total: int, weights: List[int]) -> List[int]:
    """Largest-remainder (Hamilton) — matches StagePlanner.apportion."""
    count = len(weights)
    assert count > 0 and total >= count
    total_weight = sum(weights)
    if total_weight <= 0:
        result = [total // count] * count
        for i in range(total % count):
            result[i] += 1
        return result
    result = [0] * count
    fractions: List[Tuple[int, float]] = []
    distributed = 0
    for index, weight in enumerate(weights):
        exact = total * weight / total_weight
        whole = int(exact)
        result[index] = whole
        distributed += whole
        fractions.append((index, exact - whole))
    leftover = total - distributed
    ordered = sorted(fractions, key=lambda item: (-item[1], item[0]))
    for offset in range(leftover):
        result[ordered[offset][0]] += 1
    while True:
        try:
            starved = next(i for i, v in enumerate(result) if v == 0)
        except StopIteration:
            break
        largest = max(range(count), key=lambda i: result[i])
        if result[largest] <= 1:
            break
        result[largest] -= 1
        result[starved] += 1
    return result


def plan_shards(peers: List[Peer], layer_count: int, total_weight_gb: float, overhead_gb: float) -> List[Shard]:
    capacities = [max(0.0, p.usable_gb - overhead_gb) for p in peers]
    weights = [max(1, int(c * 1024)) for c in capacities]
    layers = apportion(layer_count, weights)
    bytes_per_layer = total_weight_gb / layer_count
    shards: List[Shard] = []
    cursor = 0
    for rank, (peer, n_layers) in enumerate(zip(peers, layers)):
        shards.append(Shard(rank, peer.platform, peer.node_id, cursor, cursor + n_layers,
                            n_layers * bytes_per_layer + overhead_gb))
        cursor += n_layers
    assert cursor == layer_count
    return shards


def encode_msg(payload: dict) -> bytes:
    body = dict(payload)
    body.setdefault("v", PROTOCOL_VERSION)
    return (json.dumps(body, separators=(",", ":")) + "\n").encode("utf-8")


def recv_line(sock: socket.socket, buf: bytearray, deadline: float) -> Optional[dict]:
    while True:
        nl = buf.find(b"\n")
        if nl >= 0:
            raw = bytes(buf[:nl]).strip()
            del buf[: nl + 1]
            if not raw:
                continue
            return json.loads(raw.decode("utf-8"))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            return None
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf.extend(chunk)


def send_msg(sock: socket.socket, payload: dict) -> None:
    sock.sendall(encode_msg(payload))


def encode_beacon(
    hub_host: str,
    hub_port: int,
    epoch: str,
    platforms: Sequence[str],
) -> bytes:
    payload = {
        "v": PROTOCOL_VERSION,
        "service": SERVICE_ID,
        "hub_host": hub_host,
        "hub_port": int(hub_port),
        "epoch": epoch,
        "platforms": list(platforms),
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def parse_beacon(raw: bytes) -> Optional[HubBeacon]:
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("service") != SERVICE_ID:
        return None
    try:
        hub_port = int(msg["hub_port"])
        hub_host = str(msg.get("hub_host") or DEFAULT_HOST)
        epoch = str(msg.get("epoch") or "")
        platforms = tuple(str(p) for p in (msg.get("platforms") or ()))
    except (KeyError, TypeError, ValueError):
        return None
    if hub_port <= 0 or not epoch:
        return None
    return HubBeacon(hub_host=hub_host, hub_port=hub_port, epoch=epoch, platforms=platforms)


def open_beacon_sender(ttl: int = 1) -> socket.socket:
    """UDP socket that publishes discovery beacons (multicast, loopback-enabled)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    # Same-machine simulation: peers must hear the hub's own multicast.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    return sock


def open_beacon_listener(discovery_port: int = DISCOVERY_PORT, group: str = DISCOVERY_MULTICAST_GROUP) -> socket.socket:
    """UDP socket joined to the discovery multicast group."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # SO_REUSEPORT helps multiple peer processes bind the same discovery port on Linux.
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", discovery_port))
    mreq = struct.pack("=4s4s", socket.inet_aton(group), socket.inet_aton("0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    return sock


def announce_beacon(
    sock: socket.socket,
    hub_host: str,
    hub_port: int,
    epoch: str,
    platforms: Sequence[str],
    discovery_port: int = DISCOVERY_PORT,
    group: str = DISCOVERY_MULTICAST_GROUP,
) -> None:
    payload = encode_beacon(hub_host, hub_port, epoch, platforms)
    sock.sendto(payload, (group, discovery_port))


def discover_hub(
    timeout_s: float,
    discovery_port: int = DISCOVERY_PORT,
    group: str = DISCOVERY_MULTICAST_GROUP,
) -> Optional[HubBeacon]:
    """Listen for a sharecompute-pool beacon until timeout. Returns None on miss."""
    sock = open_beacon_listener(discovery_port=discovery_port, group=group)
    deadline = time.monotonic() + timeout_s
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([sock], [], [], remaining)
            if not ready:
                return None
            try:
                data, _addr = sock.recvfrom(4096)
            except BlockingIOError:
                continue
            beacon = parse_beacon(data)
            if beacon is not None:
                return beacon
    finally:
        try:
            sock.close()
        except OSError:
            pass
