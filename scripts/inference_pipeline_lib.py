#!/usr/bin/env python3
"""Shared helpers for the Phase A inference-pipeline simulation.

Reuses UDP multicast discovery + TCP framing from four_platform_pool_lib.
Adds a binary-capable data-plane framing for activation messages along shard ranks.
"""
from __future__ import annotations

import hashlib
import select
import socket
import struct
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from four_platform_pool_lib import (  # noqa: F401 — re-export for peers/hub
    BEACON_INTERVAL_S,
    DEFAULT_HOST,
    DEFAULT_TIMEOUT_S,
    DEFAULT_USABLE_GB,
    DISCOVERY_MULTICAST_GROUP,
    DISCOVERY_PORT,
    DISPLAY,
    HUB_READY_PREFIX,
    PLATFORMS,
    PROTOCOL_VERSION,
    SERVICE_ID,
    Peer,
    Shard,
    announce_beacon,
    apportion,
    discover_hub,
    encode_msg,
    open_beacon_listener,
    open_beacon_sender,
    parse_beacon,
    plan_shards,
    recv_line,
    send_msg,
)

# Same service id as the four-platform demo; role field distinguishes seats.
INFER_SERVICE_ID = SERVICE_ID  # "sharecompute-pool"
ROLES = ("frontend", "worker")

# Demo model sizing (fake-but-sized — not MLX).
DEFAULT_LAYER_COUNT = 48
DEFAULT_TOTAL_WEIGHT_GB = 24.0
DEFAULT_OVERHEAD_GB = 0.5
DEFAULT_TOKEN_COUNT = 8
DEFAULT_ACTIVATION_BYTES = 4096  # fake activation blob size
DATA_HEADER_MAGIC = b"SCIN"  # ShareCompute INference data plane
DATA_HEADER_FMT = "!4sIIIIII"  # magic, epoch_hash32, token_id, rank_from, rank_to, nbytes, reserved
DATA_HEADER_SIZE = struct.calcsize(DATA_HEADER_FMT)


@dataclass(frozen=True)
class InferPeer:
    platform: str
    node_id: str
    usable_gb: float
    role: str  # frontend | worker
    data_host: str
    data_port: int


@dataclass(frozen=True)
class InferShard:
    rank: int
    platform: str
    node_id: str
    role: str
    start_layer: int
    end_layer: int
    estimated_gb: float
    data_host: str
    data_port: int

    @property
    def layer_count(self) -> int:
        return self.end_layer - self.start_layer


@dataclass
class ActivationMsg:
    epoch: str
    token_id: int
    rank_from: int
    rank_to: int
    payload: bytes

    @property
    def nbytes(self) -> int:
        return len(self.payload)


def epoch_hash32(epoch: str) -> int:
    digest = hashlib.sha256(epoch.encode("utf-8")).digest()
    return struct.unpack("!I", digest[:4])[0]


def plan_infer_shards(
    peers: Sequence[InferPeer],
    layer_count: int = DEFAULT_LAYER_COUNT,
    total_weight_gb: float = DEFAULT_TOTAL_WEIGHT_GB,
    overhead_gb: float = DEFAULT_OVERHEAD_GB,
) -> List[InferShard]:
    """Apportion layers across peers; frontend (role) is forced to rank 0."""
    frontends = [p for p in peers if p.role == "frontend"]
    workers = [p for p in peers if p.role == "worker"]
    if len(frontends) != 1:
        raise ValueError(f"expected exactly one frontend, got {len(frontends)}")
    ordered = [frontends[0]] + sorted(workers, key=lambda p: p.platform)
    # Reuse StagePlanner-equivalent apportionment from the four-platform lib.
    base = [Peer(p.platform, p.node_id, p.usable_gb) for p in ordered]
    raw = plan_shards(list(base), layer_count, total_weight_gb, overhead_gb)
    out: List[InferShard] = []
    for shard, peer in zip(raw, ordered):
        out.append(
            InferShard(
                rank=shard.rank,
                platform=peer.platform,
                node_id=peer.node_id,
                role=peer.role,
                start_layer=shard.start_layer,
                end_layer=shard.end_layer,
                estimated_gb=shard.estimated_gb,
                data_host=peer.data_host,
                data_port=peer.data_port,
            )
        )
    return out


def shards_to_plan_dict(epoch: str, shards: Sequence[InferShard], token_count: int) -> dict:
    return {
        "type": "plan",
        "epoch": epoch,
        "token_count": token_count,
        "activation_bytes": DEFAULT_ACTIVATION_BYTES,
        "shards": [
            {
                "rank": s.rank,
                "platform": s.platform,
                "node_id": s.node_id,
                "role": s.role,
                "start_layer": s.start_layer,
                "end_layer": s.end_layer,
                "estimated_gb": s.estimated_gb,
                "data_host": s.data_host,
                "data_port": s.data_port,
            }
            for s in shards
        ],
    }


def plan_dict_to_shards(msg: dict) -> Tuple[str, int, List[InferShard]]:
    epoch = str(msg["epoch"])
    token_count = int(msg.get("token_count", DEFAULT_TOKEN_COUNT))
    shards: List[InferShard] = []
    for raw in msg["shards"]:
        shards.append(
            InferShard(
                rank=int(raw["rank"]),
                platform=str(raw["platform"]),
                node_id=str(raw["node_id"]),
                role=str(raw["role"]),
                start_layer=int(raw["start_layer"]),
                end_layer=int(raw["end_layer"]),
                estimated_gb=float(raw["estimated_gb"]),
                data_host=str(raw["data_host"]),
                data_port=int(raw["data_port"]),
            )
        )
    shards.sort(key=lambda s: s.rank)
    return epoch, token_count, shards


def encode_activation(msg: ActivationMsg) -> bytes:
    header = struct.pack(
        DATA_HEADER_FMT,
        DATA_HEADER_MAGIC,
        epoch_hash32(msg.epoch),
        int(msg.token_id),
        int(msg.rank_from),
        int(msg.rank_to),
        len(msg.payload),
        0,
    )
    # Append epoch string length-prefixed so receivers can verify exact epoch match.
    epoch_b = msg.epoch.encode("utf-8")
    return header + struct.pack("!H", len(epoch_b)) + epoch_b + msg.payload


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        ready, _, _ = select.select([sock], [], [], remaining)
        if not ready:
            return None
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_activation(sock: socket.socket, deadline: float) -> Optional[ActivationMsg]:
    header = _recv_exact(sock, DATA_HEADER_SIZE, deadline)
    if header is None:
        return None
    magic, ehash, token_id, rank_from, rank_to, nbytes, _res = struct.unpack(DATA_HEADER_FMT, header)
    if magic != DATA_HEADER_MAGIC:
        raise ValueError(f"bad data-plane magic: {magic!r}")
    if nbytes < 0 or nbytes > 64 * 1024 * 1024:
        raise ValueError(f"unreasonable nbytes={nbytes}")
    elen_raw = _recv_exact(sock, 2, deadline)
    if elen_raw is None:
        return None
    (elen,) = struct.unpack("!H", elen_raw)
    epoch_b = _recv_exact(sock, elen, deadline)
    if epoch_b is None:
        return None
    epoch = epoch_b.decode("utf-8")
    if epoch_hash32(epoch) != ehash:
        raise ValueError("epoch hash mismatch")
    payload = _recv_exact(sock, nbytes, deadline)
    if payload is None:
        return None
    return ActivationMsg(epoch=epoch, token_id=token_id, rank_from=rank_from, rank_to=rank_to, payload=payload)


def send_activation(sock: socket.socket, msg: ActivationMsg) -> None:
    sock.sendall(encode_activation(msg))


def fake_compute(payload: bytes, layers: int, token_id: int, rank: int) -> bytes:
    """Fake-but-sized compute: checksum fold + sleep scaled by assigned layers."""
    # ~0.5ms per layer — keeps the demo snappy while still observable.
    time.sleep(min(0.05, 0.0005 * max(1, layers)))
    digest = hashlib.sha256(payload + struct.pack("!III", token_id, rank, layers)).digest()
    # Produce a same-sized activation for the next rank.
    out = bytearray(payload)
    for i, b in enumerate(digest):
        out[i % len(out)] ^= b
    out[0] = (out[0] + rank + token_id) & 0xFF
    return bytes(out)


def make_prompt_activation(prompt: str, nbytes: int = DEFAULT_ACTIVATION_BYTES) -> bytes:
    seed = hashlib.sha256(prompt.encode("utf-8")).digest()
    out = bytearray()
    while len(out) < nbytes:
        seed = hashlib.sha256(seed).digest()
        out.extend(seed)
    return bytes(out[:nbytes])


def buffer_budget_bytes(estimated_gb: float) -> int:
    """Cap in-flight activation buffers from shard estimated size (demo-scale)."""
    # Keep small for localhost sim: min 1 activation, max 8, scaled by estimated GB.
    n = max(1, min(8, int(estimated_gb)))
    return n * DEFAULT_ACTIVATION_BYTES


def open_data_listener(host: str = DEFAULT_HOST, port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(8)
    sock.setblocking(False)
    return sock


def connect_with_deadline(host: str, port: int, deadline: float) -> socket.socket:
    last_err: Optional[BaseException] = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(max(0.05, min(0.5, deadline - time.monotonic())))
            sock.connect((host, port))
            sock.settimeout(None)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            last_err = exc
            try:
                sock.close()
            except OSError:
                pass
            time.sleep(0.05)
    raise ConnectionError(f"connect {host}:{port} failed before deadline: {last_err}")
