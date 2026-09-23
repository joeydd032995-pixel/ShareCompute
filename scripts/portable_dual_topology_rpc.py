#!/usr/bin/env python3
"""Same-host llama.cpp RPC adapter: probe, serve, generate, status."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from subprocess import Popen
from typing import Optional

from portable_dual_topology_lib import (
    DEFAULT_RPC_MODEL_NAME,
    resolve_llama_bin,
    resolve_llama_model,
    rpc_port_base,
)

# ggml-org/llama.cpp#26724 capability marker (must appear in BIN bytes).
_RPC_PATCH_MARKER = b"the device is now unusable"
_DISCOVERY_PORT = 37777
_CLI_FAIL_MARKERS = (
    "llama_decode: failed to decode, ret = -3",
    "ret = -3",
    "Compute error",
    "graph computation failed",
    "crashed or returned a malformed response",
)
_DEFAULT_PROMPT = "Explain gravity briefly."


class RpcProbeError(Exception):
    """BIN / model / capability probe failed for llamacpp-rpc backend."""


@dataclass
class RpcRunResult:
    ok: bool
    detail: str
    client_rc: Optional[int]
    decode_failed: bool
    http_status: Optional[int]


def _is_executable(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _file_contains_marker(path: str, needle: bytes, chunk_size: int = 1 << 20) -> bool:
    try:
        with open(path, "rb") as fh:
            prev = b""
            while True:
                block = fh.read(chunk_size)
                if not block:
                    return False
                if needle in prev + block:
                    return True
                keep = len(needle) - 1
                prev = block[-keep:] if keep > 0 else b""
    except OSError:
        return False


def _bin_has_patch_marker(bin_dir: str) -> bool:
    try:
        names = os.listdir(bin_dir)
    except OSError:
        return False
    for name in names:
        path = os.path.join(bin_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            if os.path.getsize(path) > 256 * 1024 * 1024:
                continue
        except OSError:
            continue
        if _file_contains_marker(path, _RPC_PATCH_MARKER):
            return True
    return False


def probe_llama_bin(bin_dir: str) -> None:
    """Require ggml-rpc-server + client and #26724 marker in BIN bytes."""
    if not bin_dir or not os.path.isdir(bin_dir):
        raise RpcProbeError(f"llama bin dir missing or not a directory: {bin_dir!r}")

    rpc_server = os.path.join(bin_dir, "ggml-rpc-server")
    if not _is_executable(rpc_server):
        raise RpcProbeError(f"missing executable ggml-rpc-server in {bin_dir}")

    llama_server = os.path.join(bin_dir, "llama-server")
    llama_cli = os.path.join(bin_dir, "llama-cli")
    if not (_is_executable(llama_server) or _is_executable(llama_cli)):
        raise RpcProbeError(
            f"need executable llama-server and/or llama-cli in {bin_dir}"
        )

    if not _bin_has_patch_marker(bin_dir):
        raise RpcProbeError(
            "BIN lacks #26724 marker 'the device is now unusable' "
            f"(searched under {bin_dir})"
        )


def _rpc_env(bin_dir: str) -> dict:
    env = os.environ.copy()
    prev = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = bin_dir if not prev else f"{bin_dir}:{prev}"
    return env


def _port_candidates(start: int, count: int = 5) -> list[int]:
    out: list[int] = []
    port = start
    while len(out) < count and port <= 65535:
        if port != _DISCOVERY_PORT and 1 <= port <= 65535:
            out.append(port)
        port += 1
    if len(out) < count:
        raise ValueError(
            f"not enough valid ports from start {start} for count {count}"
        )
    return out


def _prove_listening(host: str, port: int, timeout_s: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def start_rpc_server(bin_dir: str, host: str = "127.0.0.1") -> tuple[Popen, int]:
    """Start one loopback ggml-rpc-server; retry up to 5 ports; prove listen."""
    rpc_bin = os.path.join(bin_dir, "ggml-rpc-server")
    if not _is_executable(rpc_bin):
        raise RpcProbeError(f"missing executable ggml-rpc-server in {bin_dir}")

    env = _rpc_env(bin_dir)
    base = rpc_port_base(os.getpid())
    last_err: Optional[str] = None

    for port in _port_candidates(base, 5):
        log_path = f"/tmp/sharecompute-rpc-srv-{os.getpid()}-{port}.log"
        try:
            log_fh = open(log_path, "wb")
        except OSError as exc:
            last_err = str(exc)
            continue
        proc = Popen(
            [rpc_bin, "-H", host, "-p", str(port)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
        )
        log_fh.close()
        listened = False
        for _ in range(40):
            if proc.poll() is not None:
                break
            # Bind failure surfaces in the log; also detect via listen probe.
            try:
                with open(log_path, "rb") as fh:
                    blob = fh.read()
                if b"Failed to create server socket" in blob:
                    break
            except OSError:
                pass
            if _prove_listening(host, port):
                listened = True
                break
            time.sleep(0.25)
        if listened:
            return proc, port
        stop_proc(proc)
        last_err = f"never listened on {host}:{port} (log {log_path})"

    raise RpcProbeError(
        f"ggml-rpc-server failed to listen after 5 ports from base {base}: {last_err}"
    )


def stop_proc(proc: Optional[Popen]) -> None:
    """SIGTERM then kill a child process."""
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass


def parse_cli_logs(
    stderr_text: str,
    stdout_text: str,
    client_rc: Optional[int],
) -> RpcRunResult:
    """Judge generate success from logs — never from client_rc==0 alone."""
    combined = f"{stderr_text}\n{stdout_text}"
    hit = next((m for m in _CLI_FAIL_MARKERS if m in combined), None)
    decode_failed = hit is not None
    empty_stdout = not (stdout_text or "").strip()

    ok = True
    details: list[str] = []
    if decode_failed:
        ok = False
        details.append(f"fail marker: {hit}")
    if empty_stdout:
        ok = False
        details.append("empty stdout")
    if client_rc is not None and client_rc != 0:
        ok = False
        details.append(f"client_rc={client_rc}")
    if ok:
        details.append("ok")

    return RpcRunResult(
        ok=ok,
        detail="; ".join(details),
        client_rc=client_rc,
        decode_failed=decode_failed,
        http_status=None,
    )


def _resolve_model_path(model: Optional[str]) -> str:
    path = model or resolve_llama_model()
    if not path:
        raise FileNotFoundError(
            "model path not set (SHARECOMPUTE_LLAMA_MODEL / MODEL)"
        )
    if os.path.isdir(path):
        path = os.path.join(path, DEFAULT_RPC_MODEL_NAME)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"model file missing: {path}")
    return path


def _wait_listening(host: str, port: int, proc: Popen, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        if _prove_listening(host, port):
            return True
        time.sleep(0.25)
    return False


def _start_llama_server(
    bin_dir: str,
    model_path: str,
    rpc_port: int,
    host: str = "127.0.0.1",
) -> tuple[Popen, int]:
    server_bin = os.path.join(bin_dir, "llama-server")
    env = _rpc_env(bin_dir)
    base = rpc_port_base(os.getpid())
    # Prefer ports after the RPC port, then walk the block.
    starts = [rpc_port + 1, base, base + 2]
    tried: set[int] = set()
    last_err: Optional[str] = None

    for start in starts:
        for port in _port_candidates(start, 5):
            if port in tried or port == rpc_port:
                continue
            tried.add(port)
            log_path = f"/tmp/sharecompute-llama-server-{os.getpid()}-{port}.log"
            try:
                log_fh = open(log_path, "wb")
            except OSError as exc:
                last_err = str(exc)
                continue
            proc = Popen(
                [
                    server_bin,
                    "-m",
                    model_path,
                    "--host",
                    host,
                    "--port",
                    str(port),
                    "--rpc",
                    f"127.0.0.1:{rpc_port}",
                    "-ngl",
                    "99",
                    "-c",
                    "8192",
                    "-v",
                ],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
            )
            log_fh.close()
            if _wait_listening(host, port, proc, timeout_s=120.0):
                return proc, port
            stop_proc(proc)
            last_err = f"llama-server never listened on {host}:{port} ({log_path})"

    raise RpcProbeError(f"llama-server failed to start: {last_err}")


def _post_completion(
    http_port: int,
    prompt: str,
    n_tokens: int,
    timeout_s: float,
) -> tuple[int, str]:
    payload = json.dumps(
        {"prompt": prompt, "n_predict": int(n_tokens), "stream": False}
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{http_port}/completion",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return int(resp.status), body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return int(exc.code), body
    except Exception as exc:  # noqa: BLE001 — surface as failed generate
        return 0, f"request failed: {exc}"


def _run_via_llama_server(
    *,
    bin_dir: str,
    model_path: str,
    prompt: str,
    n_tokens: int,
    timeout_s: float,
    rpc_proc: Popen,
    rpc_port: int,
) -> RpcRunResult:
    client_proc: Optional[Popen] = None
    try:
        client_proc, http_port = _start_llama_server(
            bin_dir, model_path, rpc_port
        )
        status, body = _post_completion(http_port, prompt, n_tokens, timeout_s)
        combined = body
        hit = next((m for m in _CLI_FAIL_MARKERS if m in combined), None)
        decode_failed = hit is not None or status >= 500
        ok = status > 0 and status < 500 and not decode_failed and bool(body.strip())
        detail_parts = [f"http={status}"]
        if hit:
            detail_parts.append(f"fail marker: {hit}")
        if status >= 500:
            detail_parts.append("HTTP >= 500")
        if not body.strip():
            detail_parts.append("empty body")
            ok = False
        return RpcRunResult(
            ok=ok,
            detail="; ".join(detail_parts),
            client_rc=None,
            decode_failed=decode_failed,
            http_status=status if status > 0 else None,
        )
    finally:
        stop_proc(client_proc)


def _run_via_llama_cli(
    *,
    bin_dir: str,
    model_path: str,
    prompt: str,
    n_tokens: int,
    timeout_s: float,
    rpc_port: int,
) -> RpcRunResult:
    cli_bin = os.path.join(bin_dir, "llama-cli")
    env = _rpc_env(bin_dir)
    cmd = [
        cli_bin,
        "-m",
        model_path,
        "--rpc",
        f"127.0.0.1:{rpc_port}",
        "-ngl",
        "99",
        "-c",
        "8192",
        "-st",
        "-no-cnv",
        "-v",
        "-n",
        str(int(n_tokens)),
        "-p",
        prompt,
    ]
    try:
        completed = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout_text = (exc.stdout or b"").decode("utf-8", errors="replace")
        stderr_text = (exc.stderr or b"").decode("utf-8", errors="replace")
        result = parse_cli_logs(stderr_text, stdout_text, client_rc=124)
        result.ok = False
        result.detail = f"timeout; {result.detail}"
        return result

    stdout_text = completed.stdout.decode("utf-8", errors="replace")
    stderr_text = completed.stderr.decode("utf-8", errors="replace")
    return parse_cli_logs(stderr_text, stdout_text, client_rc=completed.returncode)


def run_rpc_generate(
    *,
    bin_dir: Optional[str] = None,
    model: Optional[str] = None,
    prompt: str = _DEFAULT_PROMPT,
    n_tokens: int = 32,
    timeout_s: float = 180.0,
    rpc_server_proc: Optional[Popen] = None,
    rpc_port: Optional[int] = None,
) -> tuple[RpcRunResult, Popen, int]:
    """Probe, ensure one RPC server, run llama-server or llama-cli generate."""
    if (rpc_server_proc is None) != (rpc_port is None):
        raise ValueError(
            "rpc_server_proc and rpc_port must be provided together or neither"
        )

    resolved_bin = bin_dir or resolve_llama_bin()
    if not resolved_bin:
        raise RpcProbeError("BIN not set (SHARECOMPUTE_LLAMA_BIN / BIN)")
    probe_llama_bin(resolved_bin)
    model_path = _resolve_model_path(model)

    started_here = False
    proc = rpc_server_proc
    port = rpc_port
    if proc is None or port is None:
        proc, port = start_rpc_server(resolved_bin)
        started_here = True

    assert proc is not None and port is not None

    try:
        llama_server = os.path.join(resolved_bin, "llama-server")
        if _is_executable(llama_server):
            result = _run_via_llama_server(
                bin_dir=resolved_bin,
                model_path=model_path,
                prompt=prompt,
                n_tokens=n_tokens,
                timeout_s=timeout_s,
                rpc_proc=proc,
                rpc_port=port,
            )
        else:
            result = _run_via_llama_cli(
                bin_dir=resolved_bin,
                model_path=model_path,
                prompt=prompt,
                n_tokens=n_tokens,
                timeout_s=timeout_s,
                rpc_port=port,
            )
        return result, proc, port
    except Exception:
        if started_here:
            stop_proc(proc)
        raise
