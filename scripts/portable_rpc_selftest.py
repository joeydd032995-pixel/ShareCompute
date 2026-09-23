#!/usr/bin/env python3
"""Unit + optional BIN-gated tests for portable llamacpp-rpc backend."""
from __future__ import annotations

import os
import sys
from unittest.mock import mock_open, patch

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def test_backend_constants() -> None:
    import portable_dual_topology_lib as lib

    assert lib.BACKEND_CHOICES == ("stub", "llamacpp-rpc")
    assert lib.DEFAULT_BACKEND == "stub"
    assert lib.DEFAULT_RPC_MODEL_NAME == "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    print("PASS: backend constants")


def test_resolve_llama_bin_and_model() -> None:
    import portable_dual_topology_lib as lib

    keys = (
        "SHARECOMPUTE_LLAMA_BIN",
        "BIN",
        "SHARECOMPUTE_LLAMA_MODEL",
        "MODEL",
    )
    saved = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        assert lib.resolve_llama_bin() is None
        assert lib.resolve_llama_model() is None
        os.environ["BIN"] = "/tmp/fake-bin"
        os.environ["MODEL"] = "/tmp/fake.gguf"
        assert lib.resolve_llama_bin() == "/tmp/fake-bin"
        assert lib.resolve_llama_model() == "/tmp/fake.gguf"
        os.environ["SHARECOMPUTE_LLAMA_BIN"] = "/tmp/sc-bin"
        os.environ["SHARECOMPUTE_LLAMA_MODEL"] = "/tmp/sc.gguf"
        assert lib.resolve_llama_bin() == "/tmp/sc-bin"
        assert lib.resolve_llama_model() == "/tmp/sc.gguf"
        print("PASS: resolve bin/model")
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_rpc_port_base_below_ephemeral() -> None:
    import portable_dual_topology_lib as lib

    base = lib.rpc_port_base(12345)
    assert 10000 <= base <= 30000
    assert base + 64 < 32768
    with patch("builtins.open", mock_open(read_data="10064 60999\n")):
        low_ephemeral_base = lib.rpc_port_base(12345)
    assert low_ephemeral_base + 64 < 10064
    assert not (low_ephemeral_base <= 37777 <= low_ephemeral_base + 64)
    print("PASS: rpc_port_base")


def test_rpc_port_base_rejects_too_low_ephemeral() -> None:
    import portable_dual_topology_lib as lib

    for proc_value in ("65 60999\n", "1 60999\n", "0 60999\n", "-1 60999\n"):
        with patch("builtins.open", mock_open(read_data=proc_value)):
            try:
                lib.rpc_port_base(12345)
            except ValueError as exc:
                assert "65-port" in str(exc)
            else:
                raise AssertionError(f"expected ValueError for {proc_value!r}")
    with patch("builtins.open", mock_open(read_data="not-a-port-range\n")):
        try:
            lib.rpc_port_base(12345)
        except ValueError as exc:
            assert "invalid ephemeral" in str(exc)
        else:
            raise AssertionError("expected ValueError for malformed ephemeral range")
    print("PASS: rpc_port_base rejects too-low ephemeral")


def run_unit_tests() -> None:
    test_backend_constants()
    test_resolve_llama_bin_and_model()
    test_rpc_port_base_below_ephemeral()
    test_rpc_port_base_rejects_too_low_ephemeral()


def main() -> int:
    run_unit_tests()
    print("ALL PASS: portable_rpc_selftest (units)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
