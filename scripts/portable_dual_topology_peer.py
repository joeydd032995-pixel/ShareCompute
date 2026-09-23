#!/usr/bin/env python3
"""Frontend / worker peers for Phase B portable dual-topology simulation.
Discover hub via UDP -> JOIN over TCP with role + data port -> wait for PLAN matching
epoch -> run activation data-plane along shard ranks (refuse until epoch matches).
"""
from __future__ import annotations
import os,select,socket,sys,threading,time
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
    gb = float(usable_gb if usable_gb is not None else PORTABLE_DEFAULT_USABLE_GB[platform])
    node_id = f"sim-{platform}"
    deadline = time.monotonic() + timeout_s
    data_sock = open_data_listener(DEFAULT_HOST, 0)
    data_host, data_port = data_sock.getsockname()[0] or DEFAULT_HOST, data_sock.getsockname()[1]
    if not data_host or data_host == "0.0.0.0":
        data_host = DEFAULT_HOST
    print(
        f"p {PORTABLE_DISPLAY[platform]} ({role}): listen {data_host}:{data_port}",
        flush=True
    )
    if hub_host is None or hub_port is None or hub_port <= 0:
        remaining = max(0.1, deadline - time.monotonic())
        print(
            f"p {PORTABLE_DISPLAY[platform]}: wait {PORTABLE_SERVICE_ID} beacon on "
            f"{DISCOVERY_MULTICAST_GROUP}:{discovery_port} (timeout {remaining:.1f}s)",
            flush=True
        )
        beacon = discover_portable_hub(remaining, discovery_port=discovery_port)
        if beacon is None:
            print(
                f"p {PORTABLE_DISPLAY[platform]}: discovery fail - no {PORTABLE_SERVICE_ID} beacon on "
                f"{DISCOVERY_MULTICAST_GROUP}:{discovery_port} within {timeout_s:.1f}s",
                file=sys.stderr,
                flush=True
            )
            data_sock.close()
            return 1
        hub_host = beacon.hub_host
        hub_port = beacon.hub_port
        print(
            f"p {PORTABLE_DISPLAY[platform]}: hub {hub_host}:{hub_port} "
            f"(epoch={beacon.epoch})",
            flush=True
        )
    else:
        print(
            f"p {PORTABLE_DISPLAY[platform]}: hub {hub_host}:{hub_port} (no disc)",
            flush=True
        )
    ctrl: socket.socket | None = None
    buf = bytearray()
    last_err: BaseException | None = None
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            sock.connect((hub_host, int(hub_port)))
            sock.settimeout(None)
            sock.setblocking(False)
            send_msg(
                sock,
                {
                    "type": "join",
                    "platform": platform,
                    "node_id": node_id,
                    "usable_gb": gb,
                    "role": role,
                    "data_host": data_host,
                    "data_port": data_port,
                }
            )
            ack = recv_line(sock, buf, deadline)
            if ack and ack.get("type") == "ack":
                join_epoch = str(ack.get("epoch") or "")
                print(
                    f"p {PORTABLE_DISPLAY[platform]}: ack role={role} as {node_id} epoch={join_epoch}",
                    flush=True
                )
                ctrl = sock
                break
            if ack and ack.get("type") == "error":
                last_err = RuntimeError(f"hub rejected join: {ack}")
                try:
                    sock.close()
                except OSError:
                    pass
                print(
                    f"p {PORTABLE_DISPLAY[platform]}: join rejected ({ack.get('reason')})",
                    file=sys.stderr,
                    flush=True
                )
                data_sock.close()
                return 1
            last_err = RuntimeError(f"unexpected reply: {ack!r}")
        except OSError as exc:
            last_err = exc
            try:
                sock.close()
            except OSError:
                pass
        time.sleep(0.05)
    else:
        print(
            f"p {PORTABLE_DISPLAY[platform]}: join fail {hub_host}:{hub_port} within "
            f"{timeout_s:.1f}s ({last_err})",
            file=sys.stderr,
            flush=True
        )
        data_sock.close()
        return 1
    assert ctrl is not None
    plan_msg = None
    plan_deadline = time.monotonic() + max(2.0, timeout_s)
    while time.monotonic() < plan_deadline:
        msg = recv_line(ctrl, buf, min(plan_deadline, time.monotonic() + 0.5))
        if msg is None:
            continue
        if msg.get("type") == "plan":
            plan_msg = msg
            break
        if msg.get("type") == "shutdown":
            print(f"p {PORTABLE_DISPLAY[platform]}: shutdown before plan ({msg.get('reason')})")
            _cleanup(ctrl, data_sock)
            return 1
        if msg.get("type") == "error":
            print(f"p {PORTABLE_DISPLAY[platform]}: hub error {msg}", file=sys.stderr)
            _cleanup(ctrl, data_sock)
            return 1
    if plan_msg is None:
        print(f"p {PORTABLE_DISPLAY[platform]}: no plan", file=sys.stderr)
        _cleanup(ctrl, data_sock)
        return 1
    plan_epoch, token_count, shards = plan_dict_to_shards(plan_msg)
    if plan_epoch != join_epoch:
        print(
            f"p {PORTABLE_DISPLAY[platform]}: REFUSE data - plan epoch {plan_epoch} "
            f"!= join epoch {join_epoch}",
            file=sys.stderr,
            flush=True
        )
        try:
            send_msg(ctrl, {"type": "pipeline-error", "reason": "epoch-mismatch"})
        except OSError:
            pass
        _cleanup(ctrl, data_sock)
        return 1
    my = next((s for s in shards if s.platform == platform), None)
    if my is None:
        print(f"p {PORTABLE_DISPLAY[platform]}: not in plan", file=sys.stderr)
        _cleanup(ctrl, data_sock)
        return 1
    buf_budget = buffer_budget_bytes(my.estimated_gb)
    try:
        act_nbytes = activation_nbytes_for_budget(
            buf_budget, int(plan_msg.get("activation_bytes", A))
        )
    except ValueError as exc:
        print(
            f"p {PORTABLE_DISPLAY[platform]}: budget refuse - {exc}",
            file=sys.stderr,
            flush=True
        )
        try:
            send_msg(ctrl, {"type": "pipeline-error", "reason": f"buffer-budget:{exc}"})
        except OSError:
            pass
        _cleanup(ctrl, data_sock)
        return 1
    print(
        f"p {PORTABLE_DISPLAY[platform]}: plan={plan_epoch} rank={my.rank} "
        f"layers=[{my.start_layer},{my.end_layer}) tokens={token_count} "
        f"buf_budget={buf_budget}B act={act_nbytes}B",
        flush=True
    )
    pipe_deadline = time.monotonic() + max(timeout_s, 20.0)
    if os.environ.get("SHARECOMPUTE_PORTABLE_BACKEND") == "llamacpp-rpc":
        # Wait until orch tears us down; do not run SCPT.
        print(
            f"p {PORTABLE_DISPLAY[platform]}: accepted plan (rpc; skip SCPT)",
            flush=True,
        )
        try:
            while time.monotonic() < pipe_deadline:
                msg = recv_line(ctrl, buf, min(pipe_deadline, time.monotonic() + 0.5))
                if msg and msg.get("type") in ("error", "shutdown"):
                    return 1 if msg.get("type") == "error" else 0
                time.sleep(0.05)
            return 0
        finally:
            _cleanup(ctrl, data_sock)
    try:
        if my.role == "frontend" or my.rank == 0:
            rc = _run_frontend(
                ctrl,
                buf,
                data_sock,
                shards,
                my,
                plan_epoch,
                token_count,
                prompt,
                pipe_deadline,
                buf_budget=buf_budget,
                act_nbytes=act_nbytes
            )
        else:
            rc = _run_worker(
                ctrl,
                buf,
                data_sock,
                shards,
                my,
                plan_epoch,
                token_count,
                pipe_deadline,
                buf_budget=buf_budget
            )
        return rc
    except Exception as exc:  # noqa: BLE001
        print(f"p {PORTABLE_DISPLAY[platform]}: pipe exc: {exc}", file=sys.stderr)
        try:
            send_msg(ctrl, {"type": "pipeline-error", "reason": str(exc)})
        except OSError:
            pass
        return 1
    finally:
        _cleanup(ctrl, data_sock)
def _run_frontend(
    ctrl: socket.socket,
    buf: bytearray,
    data_sock: socket.socket,
    shards: list[PortableShard],
    my: PortableShard,
    epoch: str,
    token_count: int,
    prompt: str,
    deadline: float,
    *,
    buf_budget: int,
    act_nbytes: int
) -> int:
    if len(shards) < 2:
        payload = make_prompt_activation(prompt, act_nbytes)
        for tid in range(token_count):
            payload = fake_compute(payload, my.layer_count, tid, my.rank)
        print(f"p fe: completed {token_count} tokens (single-seat)")
        send_msg(ctrl, {"type": "done", "tokens": token_count})
        _wait_shutdown(ctrl, buf, deadline)
        return 0
    next_shard = shards[1]
    last_shard = shards[-1]
    forward = connect_with_deadline(next_shard.data_host, next_shard.data_port, deadline)
    print(
        f"p fe: connected -> rank {next_shard.rank} "
        f"{next_shard.data_host}:{next_shard.data_port}",
        flush=True
    )
    result_conn_holder: dict[str, socket.socket | None] = {"conn": None}
    accept_err: list[BaseException] = []
    def accept_results() -> None:
        try:
            while time.monotonic() < deadline and result_conn_holder["conn"] is None:
                ready, _, _ = select.select([data_sock], [], [], 0.2)
                if not ready:
                    continue
                conn, addr = data_sock.accept()
                conn.setblocking(False)
                result_conn_holder["conn"] = conn
                print(f"p fe: result from {addr[0]}:{addr[1]}")
                return
        except BaseException as exc:  # noqa: BLE001
            accept_err.append(exc)
    acceptor = threading.Thread(target=accept_results, daemon=True)
    acceptor.start()
    seed = make_prompt_activation(prompt, act_nbytes)
    completed = 0
    try:
        for tid in range(token_count):
            activation = fake_compute(seed, my.layer_count, tid, my.rank)
            if len(activation) > buf_budget:
                send_msg(
                    ctrl,
                    {
                        "type": "pipeline-error",
                        "reason": f"activation-exceeds-budget:{len(activation)}>{buf_budget}",
                    }
                )
                return 1
            seed = make_prompt_activation(f"{prompt}|tok{tid}", act_nbytes)
            msg = ActivationMsg(
                epoch=epoch,
                token_id=tid,
                rank_from=my.rank,
                rank_to=next_shard.rank,
                payload=activation
            )
            try:
                send_activation(forward, msg)
            except OSError as exc:
                print(f"p fe: send to rank 1 failed: {exc}", file=sys.stderr)
                send_msg(ctrl, {"type": "pipeline-error", "reason": f"forward-broken:{exc}"})
                return 1
            acceptor.join(timeout=max(0.0, deadline - time.monotonic()))
            result_conn = result_conn_holder["conn"]
            if result_conn is None:
                if accept_err:
                    raise accept_err[0]
                print("p fe: no result channel timeout", file=sys.stderr)
                send_msg(ctrl, {"type": "pipeline-error", "reason": "no-result-channel"})
                return 1
            try:
                result = recv_activation(result_conn, deadline, max_payload_bytes=buf_budget)
            except ValueError as exc:
                print(f"p fe: bad result frame: {exc}", file=sys.stderr)
                send_msg(ctrl, {"type": "pipeline-error", "reason": f"result-frame:{exc}"})
                return 1
            if result is None:
                print(
                    f"p fe: miss result {tid} (worker dead?)",
                    file=sys.stderr,
                    flush=True
                )
                send_msg(ctrl, {"type": "pipeline-error", "reason": f"missing-result-token-{tid}"})
                return 1
            if result.epoch != epoch:
                send_msg(ctrl, {"type": "pipeline-error", "reason": "result-epoch-mismatch"})
                return 1
            if result.token_id != tid:
                send_msg(ctrl, {"type": "pipeline-error", "reason": f"token-mismatch-{result.token_id}"})
                return 1
            if result.rank_from != last_shard.rank or result.rank_to != my.rank:
                print(
                    f"p fe: rank mismatch "
                    f"(got {result.rank_from}->{result.rank_to}, "
                    f"expected {last_shard.rank}->{my.rank})",
                    file=sys.stderr,
                    flush=True
                )
                send_msg(
                    ctrl,
                    {
                        "type": "pipeline-error",
                        "reason": (
                            f"rank-mm:{result.rank_from}->{result.rank_to}"
                            f"!={last_shard.rank}->{my.rank}"
                        ),
                    }
                )
                return 1
            completed += 1
            print(
                f"p fe: token {tid} complete "
                f"(final from rank {result.rank_from}, {result.nbytes}B)",
                flush=True
            )
        print(f"p fe: all {completed} tokens done")
        send_msg(ctrl, {"type": "done", "tokens": completed})
        _wait_shutdown(ctrl, buf, deadline)
        return 0
    finally:
        try:
            forward.close()
        except OSError:
            pass
        rc = result_conn_holder.get("conn")
        if rc is not None:
            try:
                rc.close()
            except OSError:
                pass
def _run_worker(
    ctrl: socket.socket,
    buf: bytearray,
    data_sock: socket.socket,
    shards: list[PortableShard],
    my: PortableShard,
    epoch: str,
    token_count: int,
    deadline: float,
    *,
    buf_budget: int
) -> int:
    by_rank = {s.rank: s for s in shards}
    is_last = my.rank == max(by_rank)
    dest = by_rank[0] if is_last else by_rank[my.rank + 1]
    expected_from = my.rank - 1
    expected_to = my.rank
    inbound: socket.socket | None = None
    while time.monotonic() < deadline and inbound is None:
        ready, _, _ = select.select([data_sock], [], [], 0.2)
        if not ready:
            msg = recv_line(ctrl, buf, time.monotonic() + 0.01)
            if msg and msg.get("type") == "shutdown":
                print(f"p wr {my.rank}: shutdown early")
                return 1
            continue
        conn, addr = data_sock.accept()
        conn.setblocking(False)
        inbound = conn
        print(
            f"p wr {my.rank}: inbound {addr[0]}:{addr[1]}",
            flush=True
        )
    if inbound is None:
        print(f"p wr {my.rank}: no inbound", file=sys.stderr)
        send_msg(ctrl, {"type": "pipeline-error", "reason": "no-inbound"})
        return 1
    outbound = connect_with_deadline(dest.data_host, dest.data_port, deadline)
    print(
        f"p wr {my.rank}: connected -> rank {dest.rank} "
        f"{dest.data_host}:{dest.data_port}"
        + (" (to frontend)" if is_last else ""),
        flush=True
    )
    processed = 0
    try:
        while processed < token_count and time.monotonic() < deadline:
            msg = recv_line(ctrl, buf, time.monotonic() + 0.01)
            if msg and msg.get("type") == "shutdown":
                print(f"p wr {my.rank}: shutdown after {processed} tokens")
                return 0 if processed >= token_count else 1
            try:
                act = recv_activation(
                    inbound,
                    min(deadline, time.monotonic() + 5.0),
                    max_payload_bytes=buf_budget
                )
            except ValueError as exc:
                print(
                    f"p wr {my.rank}: bad activation frame: {exc}",
                    file=sys.stderr,
                    flush=True
                )
                send_msg(ctrl, {"type": "pipeline-error", "reason": f"activation-frame:{exc}"})
                return 1
            except OSError as exc:
                print(f"p wr {my.rank}: in broken: {exc}", file=sys.stderr)
                send_msg(ctrl, {"type": "pipeline-error", "reason": f"inbound-broken:{exc}"})
                return 1
            if act is None:
                print(
                    f"p wr {my.rank}: act timeout "
                    f"after {processed}/{token_count}",
                    file=sys.stderr,
                    flush=True
                )
                send_msg(ctrl, {"type": "pipeline-error", "reason": "act-timeout"})
                return 1
            if act.epoch != epoch:
                send_msg(ctrl, {"type": "pipeline-error", "reason": "epoch-mm"})
                return 1
            if act.rank_from != expected_from or act.rank_to != expected_to:
                print(
                    f"p wr {my.rank}: rank mismatch "
                    f"(got {act.rank_from}->{act.rank_to}, "
                    f"expected {expected_from}->{expected_to})",
                    file=sys.stderr,
                    flush=True
                )
                send_msg(
                    ctrl,
                    {
                        "type": "pipeline-error",
                        "reason": (
                            f"rank-mm:{act.rank_from}->{act.rank_to}"
                            f"!={expected_from}->{expected_to}"
                        ),
                    }
                )
                return 1
            out_payload = fake_compute(act.payload, my.layer_count, act.token_id, my.rank)
            if len(out_payload) > buf_budget:
                send_msg(
                    ctrl,
                    {
                        "type": "pipeline-error",
                        "reason": f"activation-exceeds-budget:{len(out_payload)}>{buf_budget}",
                    }
                )
                return 1
            out = ActivationMsg(
                epoch=epoch,
                token_id=act.token_id,
                rank_from=my.rank,
                rank_to=dest.rank,
                payload=out_payload
            )
            try:
                send_activation(outbound, out)
            except OSError as exc:
                print(f"p wr {my.rank}: out broken: {exc}", file=sys.stderr)
                send_msg(ctrl, {"type": "pipeline-error", "reason": f"outbound-broken:{exc}"})
                return 1
            processed += 1
            print(
                f"p wr {my.rank}: token {act.token_id} "
                f"layers[{my.start_layer},{my.end_layer}) -> rank {dest.rank}",
                flush=True
            )
        _wait_shutdown(ctrl, buf, deadline)
        return 0
    finally:
        try:
            inbound.close()
        except OSError:
            pass
        try:
            outbound.close()
        except OSError:
            pass
def _wait_shutdown(ctrl: socket.socket, buf: bytearray, deadline: float) -> None:
    while time.monotonic() < deadline:
        msg = recv_line(ctrl, buf, min(deadline, time.monotonic() + 0.5))
        if msg is None:
            continue
        if msg.get("type") == "shutdown":
            return
def _cleanup(ctrl: socket.socket | None, data_sock: socket.socket | None) -> None:
    for s in (ctrl, data_sock):
        if s is None:
            continue
        try:
            s.close()
        except OSError:
            pass
