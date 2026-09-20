#!/usr/bin/env python3
"""Four-platform ShareCompute pool demo (simulated peers).

Connects in-process stand-ins labeled Windows, macOS, iOS, and Android to one
membership hub, then apportions model layers proportional to usable RAM
(largest-remainder), mirroring ShareComputeCore.StagePlanner.

Requires only Python 3. No Swift toolchain, no devices, no network.

    python3 scripts/four_platform_pool_demo.py
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


PLATFORMS = ("windows", "macos", "ios", "android")

# Default usable RAM (GB) — same ballpark as SimulatedPlatformPeer in Swift.
DEFAULT_USABLE_GB = {
    "windows": 16,
    "macos": 32,
    "ios": 6,
    "android": 8,
}

DISPLAY = {
    "windows": "Windows",
    "macos": "macOS",
    "ios": "iOS",
    "android": "Android",
}


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
    """Largest-remainder (Hamilton) apportionment — matches StagePlanner.apportion."""
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
        whole = int(exact)  # floor
        result[index] = whole
        distributed += whole
        fractions.append((index, exact - whole))

    leftover = total - distributed
    ordered = sorted(fractions, key=lambda item: (-item[1], item[0]))
    for offset in range(leftover):
        result[ordered[offset][0]] += 1

    # Every node at least one layer.
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


def plan_shards(
    peers: List[Peer],
    layer_count: int,
    total_weight_gb: float,
    overhead_gb: float,
) -> List[Shard]:
    capacities = [max(0.0, p.usable_gb - overhead_gb) for p in peers]
    # Scale to ints for apportionment (MB).
    weights = [max(1, int(c * 1024)) for c in capacities]
    layers = apportion(layer_count, weights)
    bytes_per_layer = total_weight_gb / layer_count

    shards: List[Shard] = []
    cursor = 0
    for rank, (peer, n_layers) in enumerate(zip(peers, layers)):
        estimated = n_layers * bytes_per_layer + overhead_gb
        shards.append(
            Shard(
                rank=rank,
                platform=peer.platform,
                node_id=peer.node_id,
                start_layer=cursor,
                end_layer=cursor + n_layers,
                estimated_gb=estimated,
            )
        )
        cursor += n_layers
    assert cursor == layer_count
    return shards


def main() -> int:
    print("ShareCompute four-platform pool demo (Python)")
    print("============================================")
    print("Connecting simulated peers…\n")

    connected: Dict[str, Peer] = {}
    for platform in PLATFORMS:
        peer = Peer(
            platform=platform,
            node_id=f"sim-{platform}",
            usable_gb=float(DEFAULT_USABLE_GB[platform]),
        )
        connected[platform] = peer
        print(
            f"  ✓ {DISPLAY[platform]} connected as {peer.node_id} "
            f"({peer.usable_gb:.0f} GB usable)"
        )

    missing = [p for p in PLATFORMS if p not in connected]
    if missing:
        print(f"error: missing platforms: {missing}")
        return 1

    print("\n  All four platforms are in the pool.\n")

    peers = [connected[p] for p in sorted(connected)]
    total_gb = sum(p.usable_gb for p in peers)
    shards = plan_shards(
        peers,
        layer_count=48,
        total_weight_gb=24.0,
        overhead_gb=0.5,
    )

    print(
        f"RAM pool ready — {total_gb:.1f} GB across "
        + ", ".join(DISPLAY[p.platform] for p in peers)
    )
    print("Epoch 1  model demo-48L  layers 48\n")
    print("Shard plan:")
    for s in shards:
        label = DISPLAY[s.platform].ljust(7)
        print(
            f"  rank {s.rank}  {label}  {s.node_id}  "
            f"layers [{s.start_layer},{s.end_layer})  ~{s.estimated_gb:.2f} GB"
        )

    print("\nSuccess: Windows, macOS, iOS, and Android are connected and pooled.")
    print(
        "(Peers are in-process simulations — see docs/FOUR-PLATFORM-CONNECT.md)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
