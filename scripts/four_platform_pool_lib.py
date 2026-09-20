#!/usr/bin/env python3
"""Shared planning + TCP framing for the four-platform pool demo."""
from __future__ import annotations

import json
import select
import socket
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

PLATFORMS = ("windows", "macos", "ios", "android")
DEFAULT_USABLE_GB = {"windows": 16, "macos": 32, "ios": 6, "android": 8}
DISPLAY = {"windows": "Windows", "macos": "macOS", "ios": "iOS", "android": "Android"}
PROTOCOL_VERSION = 1
HUB_READY_PREFIX = "HUB_READY"
DEFAULT_TIMEOUT_S = 5.0
DEFAULT_HOST = "127.0.0.1"


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
