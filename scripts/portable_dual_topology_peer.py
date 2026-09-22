#!/usr/bin/env python3
"""Frontend / worker peers for Phase B portable dual-topology simulation."""
from __future__ import annotations
import select,socket,sys,threading,time
from portable_dual_topology_lib import (
 PORTABLE_SEATS, PORTABLE_DISPLAY, PORTABLE_SERVICE_ID, PORTABLE_DEFAULT_USABLE_GB,
 ROLES, DEFAULT_ACTIVATION_BYTES, DEFAULT_HOST,
 DISCOVERY_MULTICAST_GROUP, DISCOVERY_PORT, ActivationMsg, PortableShard,
 activation_nbytes_for_budget, buffer_budget_bytes,
 connect_with_deadline, discover_portable_hub, fake_compute, make_prompt_activation,
 open_data_listener, plan_dict_to_shards, recv_activation, recv_line,
 send_activation, send_msg,
)
A = DEFAULT_ACTIVATION_BYTES
def run_peer(
    platform: str,
    role: str,
    timeout_s: float,
    *,
    usable_gb: float | None = None,
    discovery_port: int = DISCOVERY_PORT,
    hub_host: str | None = None,
    hub_port: int | None = None,
    prompt: str = "ShareCompute Phase B portable prompt"
) -> int:
    if platform not in PORTABLE_SEATS:
        print(f"error: non-product platform {platform}", file=sys.stderr)
        return 2
    if role not in ROLES:
        print(f"error: unknown role {role}", file=sys.stderr)
        return 2
    # TEMP_PARTIAL - will be replaced with full body in same commit series
    raise NotImplementedError("full peer body pending")
