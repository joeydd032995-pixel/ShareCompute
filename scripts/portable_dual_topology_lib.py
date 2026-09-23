#!/usr/bin/env python3
"""Shared helpers for the Phase B portable dual-topology simulation.

Reuses UDP multicast discovery + TCP framing from four_platform_pool_lib.
Adds a binary-capable data-plane framing for activation messages along shard ranks.
Uses a distinct discovery service id (`sharecompute-portable`) and data-plane magic
(`SCPT`) so this protocol is not confused with Phase A inference (`sharecompute-infer` /
`SCIN`) or the four-platform pool demo (`sharecompute-pool`).
"""
from __future__ import annotations

import hashlib
import os
import json
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
    DISCOVERY_MULTICAST_GROUP,
    DISCOVERY_PORT,
    HUB_READY_PREFIX,
    PROTOCOL_VERSION,
    HubBeacon,
    Peer,
    open_beacon_listener,
    open_beacon_sender,
    encode_msg,
    plan_shards,
    recv_line,
    send_msg,
)

# Distinct from four_platform_pool_lib.SERVICE_ID ("sharecompute-pool")
# and inference_pipeline_lib.INFER_SERVICE_ID ("sharecompute-infer").
PORTABLE_SERVICE_ID = "sharecompute-portable"
PORTABLE_SEATS: tuple[str, ...] = ("ios", "windows")
PORTABLE_DISPLAY: dict[str, str] = {
    "ios": "iPhone",
    "windows": "Windows",
}
PORTABLE_DEFAULT_USABLE_GB: dict[str, float] = {
    "ios": 6.0,
    "windows": 16.0,
}
ROLES: tuple[str, ...] = ("frontend", "worker")
TOPOLOGY_CHOICES: tuple[str, ...] = ("iphone-frontend", "windows-frontend", "both")

_TOPOLOGY_MAP = {
    "iphone-frontend": ("ios", "windows"),
    "windows-frontend": ("windows", "ios"),
}


def resolve_topology(name: str) -> Tuple[str, str]:
    if name not in _TOPOLOGY_MAP:
        raise ValueError(f"unknown or non-atomic topology: {name!r}")
    return _TOPOLOGY_MAP[name]


# Demo model sizing (fake-but-sized — not MLX). Fits ios(6)+windows(16) after overhead.
DEFAULT_LAYER_COUNT = 32
DEFAULT_TOTAL_WEIGHT_GB = 12.0
DEFAULT_OVERHEAD_GB = 0.5
DEFAULT_TOKEN_COUNT = 8
DEFAULT_ACTIVATION_BYTES = 4096  # fake activation blob size
DATA_HEADER_MAGIC = b"SCPT"  # ShareCompute PorTable data plane
DATA_HEADER_FMT = "!4sIIIIII"  # magic, epoch_hash32, token_id, rank_from, rank_to, nbytes, reserved
DATA_HEADER_SIZE = struct.calcsize(DATA_HEADER_FMT)


@dataclass(frozen=True)
class PortablePeer:
    platform: str
    node_id: str
    usable_gb: float
    role: str  # frontend | worker
    data_host: str
    data_port: int


@dataclass(frozen=True)
class PortableShard:
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


def encode_portable_beacon(
    hub_host: str,
    hub_port: int,
    epoch: str,
    platforms: Sequence[str],
) -> bytes:
    payload = {
        "v": PROTOCOL_VERSION,
        "service": PORTABLE_SERVICE_ID,
        "hub_host": hub_host,
        "hub_port": int(hub_port),
        "epoch": epoch,
        "platforms": list(platforms),
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def parse_portable_beacon(raw: bytes) -> Optional[HubBeacon]:
    """Parse a beacon; only accept PORTABLE_SERVICE_ID (reject sharecompute-infer/pool)."""
    try:
        msg = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("service") != PORTABLE_SERVICE_ID:
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


def announce_portable_beacon(
    sock: socket.socket,
    hub_host: str,
    hub_port: int,
    epoch: str,
    platforms: Sequence[str],
    discovery_port: int = DISCOVERY_PORT,
    group: str = DISCOVERY_MULTICAST_GROUP,
) -> None:
    payload = encode_portable_beacon(hub_host, hub_port, epoch, platforms)
    sock.sendto(payload, (group, discovery_port))


def discover_portable_hub(
    timeout_s: float,
    discovery_port: int = DISCOVERY_PORT,
    group: str = DISCOVERY_MULTICAST_GROUP,
) -> Optional[HubBeacon]:
    """Listen for a sharecompute-portable beacon until timeout. Returns None on miss."""
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
            beacon = parse_portable_beacon(data)
            if beacon is not None:
                return beacon
    finally:
        try:
            sock.close()
        except OSError:
            pass


def plan_portable_shards(
    peers: Sequence[PortablePeer],
    layer_count: int = DEFAULT_LAYER_COUNT,
    total_weight_gb: float = DEFAULT_TOTAL_WEIGHT_GB,
    overhead_gb: float = DEFAULT_OVERHEAD_GB,
) -> List[PortableShard]:
    """Apportion layers across two seats; frontend is forced to rank 0.

    Rejects peers outside PORTABLE_SEATS, plans that cannot fit the model
    (+ overhead) in aggregate usable RAM, or where any shard's estimated_gb
    exceeds that peer's usable_gb. Requires exactly one frontend and one worker.
    """
    for p in peers:
        if p.platform not in PORTABLE_SEATS:
            raise ValueError(f"platform {p.platform!r} not in PORTABLE_SEATS {PORTABLE_SEATS}")

    frontends = [p for p in peers if p.role == "frontend"]
    workers = [p for p in peers if p.role == "worker"]
    if len(frontends) != 1:
        raise ValueError(f"expected exactly one frontend, got {len(frontends)}")
    if len(workers) != 1:
        raise ValueError(f"expected exactly one worker, got {len(workers)}")
    ordered = [frontends[0], workers[0]]

    aggregate_usable = sum(p.usable_gb for p in ordered)
    # Model must fit in the pool after reserving per-peer overhead capacity.
    aggregate_capacity = sum(max(0.0, p.usable_gb - overhead_gb) for p in ordered)
    if aggregate_capacity + 1e-9 < total_weight_gb:
        raise ValueError(
            f"insufficient aggregate RAM: usable={aggregate_usable:.3f}GB "
            f"(capacity after {overhead_gb:.2f}GB overhead/peer={aggregate_capacity:.3f}GB) "
            f"< model {total_weight_gb:.2f}GB"
        )
    if aggregate_usable + 1e-9 < total_weight_gb + overhead_gb:
        raise ValueError(
            f"insufficient aggregate RAM: usable={aggregate_usable:.3f}GB "
            f"< model+overhead {total_weight_gb + overhead_gb:.2f}GB"
        )

    # Reuse StagePlanner-equivalent apportionment from the four-platform lib.
    base = [Peer(p.platform, p.node_id, p.usable_gb) for p in ordered]
    raw = plan_shards(list(base), layer_count, total_weight_gb, overhead_gb)
    out: List[PortableShard] = []
    for shard, peer in zip(raw, ordered):
        if shard.estimated_gb > peer.usable_gb + 1e-9:
            raise ValueError(
                f"shard rank {shard.rank} ({peer.platform}) estimated "
                f"{shard.estimated_gb:.3f}GB exceeds peer usable {peer.usable_gb:.3f}GB"
            )
        out.append(
            PortableShard(
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


def shards_to_plan_dict(epoch: str, shards: Sequence[PortableShard], token_count: int) -> dict:
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


def plan_dict_to_shards(msg: dict) -> Tuple[str, int, List[PortableShard]]:
    epoch = str(msg["epoch"])
    token_count = int(msg.get("token_count", DEFAULT_TOKEN_COUNT))
    shards: List[PortableShard] = []
    for raw in msg["shards"]:
        shards.append(
            PortableShard(
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


def recv_activation(
    sock: socket.socket,
    deadline: float,
    *,
    max_payload_bytes: Optional[int] = None,
) -> Optional[ActivationMsg]:
    header = _recv_exact(sock, DATA_HEADER_SIZE, deadline)
    if header is None:
        return None
    magic, ehash, token_id, rank_from, rank_to, nbytes, _res = struct.unpack(DATA_HEADER_FMT, header)
    if magic != DATA_HEADER_MAGIC:
        raise ValueError(f"bad data-plane magic: {magic!r}")
    if nbytes < 0 or nbytes > 64 * 1024 * 1024:
        raise ValueError(f"unreasonable nbytes={nbytes}")
    if max_payload_bytes is not None and nbytes > max_payload_bytes:
        raise ValueError(
            f"activation nbytes={nbytes} exceeds buffer budget {max_payload_bytes}"
        )
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
    """Hard cap on in-flight activation payload size from shard estimated_gb.

    Demo-scale: at least one DEFAULT_ACTIVATION_BYTES slot, at most 8, scaled by
    estimated GB. Peers allocate/refuse against this limit.
    """
    n = max(1, min(8, int(estimated_gb)))
    return n * DEFAULT_ACTIVATION_BYTES


def activation_nbytes_for_budget(budget: int, requested: int = DEFAULT_ACTIVATION_BYTES) -> int:
    """Size (or refuse) an activation so it fits the advertised buffer budget."""
    if budget <= 0:
        raise ValueError(f"buffer budget must be positive, got {budget}")
    if requested > budget:
        raise ValueError(
            f"activation size {requested}B exceeds buffer budget {budget}B"
        )
    return requested


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


BACKEND_CHOICES = ("stub", "llamacpp-rpc")
DEFAULT_BACKEND = "stub"
DEFAULT_RPC_MODEL_NAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"


def resolve_llama_bin() -> Optional[str]:
    for key in ("SHARECOMPUTE_LLAMA_BIN", "BIN"):
        val = os.environ.get(key)
        if val:
            return val
    return None


def resolve_llama_model() -> Optional[str]:
    for key in ("SHARECOMPUTE_LLAMA_MODEL", "MODEL"):
        val = os.environ.get(key)
        if val:
            return val
    return None


def rpc_port_base(pid: int) -> int:
    """Spike-compatible: ports below host ephemeral range."""
    ephemeral_lo = 32768
    try:
        with open("/proc/sys/net/ipv4/ip_local_port_range", encoding="utf-8") as fh:
            ephemeral_lo = int(fh.read().split()[0])
    except OSError:
        pass
    base = 20000 + ((pid * 7) % 10000)
    if base + 64 >= ephemeral_lo:
        base = max(10000, ephemeral_lo - 5000)
    return base
