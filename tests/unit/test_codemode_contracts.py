"""Code Mode host contracts that do not need the native worker."""

import hashlib
import json
import math
import stat
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from qq_ai_bot.codemode import engine_monty
from qq_ai_bot.codemode.driver_types import EngineAnswer, EngineCall, HostCounters
from qq_ai_bot.codemode.engine_monty import CodeEngineUnavailable, PinnedWorker, strict_json
from qq_ai_bot.codemode.limits import CodeModeLimits
from qq_ai_bot.codemode.snapshot_binding import call_digest, expected_call, matches_saved
from qq_ai_bot.config import Settings


@pytest.mark.parametrize(
    "value",
    [object(), {1: "x"}, {"a": {1, 2}}, b"x", math.nan, math.inf, (lambda: 1)],
)
def test_strict_json_refuses_non_json(value):
    with pytest.raises(ValueError):
        strict_json(value, limit=1024)


def test_strict_json_normalizes_tuples_and_bounds_size():
    assert strict_json((1, [2, (3,)], {"a": None}), limit=1024) == [1, [2, [3]], {"a": None}]
    with pytest.raises(ValueError, match="code_value_too_large"):
        strict_json("x" * 100, limit=10)
    deep: list = []
    for _ in range(80):
        deep = [deep]
    with pytest.raises(ValueError, match="code_value_too_deep"):
        strict_json(deep, limit=10_000)


@pytest.mark.parametrize("value", [None, '中文\n"', [1, True, None], {"中文": (1, "\t")}, {}, []])
def test_result_byte_quota_matches_actual_json_encoding(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
    checked = strict_json(value, limit=len(encoded))
    assert json.dumps(checked, ensure_ascii=False, allow_nan=False).encode() == encoded
    with pytest.raises(ValueError, match="code_value_too_large"):
        strict_json(value, limit=len(encoded) - 1)


@pytest.mark.parametrize(
    "value", ["x" * (8 << 20), [0] * 1_000_000], ids=["large_text", "large_list"]
)
def test_oversized_result_is_refused_before_a_host_copy(value):
    # Allocate the input before measurement: the Host must bound its own
    # encoding/copy allocations, independently of the VM's existing heap.
    tracemalloc.start()
    try:
        with pytest.raises(ValueError, match="code_value_too_large"):
            strict_json(value, limit=4096)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 64 << 10


def test_answer_errors_are_a_closed_set():
    assert EngineAnswer.error("denied", "PermissionError").error_type == "PermissionError"
    with pytest.raises(ValueError, match="code_answer_error_type_not_allowed"):
        EngineAnswer.error("x", "SystemExit")


def test_limits_require_watchdog_to_back_engine_limit():
    with pytest.raises(ValueError, match="request_timeout_seconds"):
        CodeModeLimits(max_feed_seconds=10, request_timeout_seconds=5)
    with pytest.raises(ValueError, match="max_suspensions"):
        CodeModeLimits(max_suspensions=0)
    engine = CodeModeLimits().engine()
    # Only engine resource fields; nothing names a business budget.
    assert set(engine) == {
        "max_feed_duration_secs",
        "max_memory",
        "max_recursion_depth",
        "max_suspensions",
    }


def test_counters_round_trip_and_never_default_to_reset():
    counters = HostCounters(suspensions=7, denied_calls=2, output_bytes=99, feeds=1)
    assert HostCounters.from_dict(counters.as_dict()) == counters
    assert HostCounters.from_dict({"suspensions": 3}).suspensions == 3


def test_reannounced_call_must_match_saved_boundary():
    call = EngineCall("function", 0, 4, "lookup", ("a",), {"k": 1})
    saved = {
        "kind": "function",
        "feed_index": 0,
        "engine_call_id": 4,
        "function_name": "lookup",
        "args_digest": call_digest(call),
        "pending_call_ids": [],
    }
    assert matches_saved(saved, call)
    assert not matches_saved(saved, EngineCall("function", 0, 5, "lookup", ("a",), {"k": 1}))
    assert not matches_saved(saved, EngineCall("function", 0, 4, "lookup", ("b",), {"k": 1}))
    assert not matches_saved(saved, EngineCall("function", 0, 4, "send", ("a",), {"k": 1}))
    future = {"kind": "future", "pending_call_ids": [1, 0]}
    assert matches_saved(future, EngineCall("future", 0, None, None, pending_call_ids=(0, 1)))
    assert not matches_saved(future, EngineCall("future", 0, None, None, pending_call_ids=(0,)))
    assert expected_call({"boundary_call": saved}).engine_call_id == 4
    with pytest.raises(ValueError, match="snapshot_binding_conflict"):
        expected_call({})


def test_pinned_worker_rejects_relative_missing_and_wrong_digest(tmp_path, monkeypatch):
    # File/digest contract alone is the local Darwin development path. Linux
    # separately requires a verified immutable launcher and literal layout.
    monkeypatch.setattr(engine_monty.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(engine_monty.platform, "machine", lambda: "arm64")
    binary = tmp_path / "monty"
    binary.write_bytes(b"not the pinned worker")
    with pytest.raises(CodeEngineUnavailable, match="code_engine_binary_missing"):
        PinnedWorker(binary.relative_to(tmp_path), "0" * 64).verify()
    with pytest.raises(CodeEngineUnavailable, match="code_engine_binary_missing"):
        PinnedWorker(tmp_path / "absent", "0" * 64).verify()
    with pytest.raises(CodeEngineUnavailable, match="code_engine_binary_digest_mismatch"):
        PinnedWorker(binary, "0" * 64).verify()
    good = hashlib.sha256(binary.read_bytes()).hexdigest()
    assert PinnedWorker(binary, good).verify() == good


@pytest.mark.parametrize("mutable_path", ["/opt", "/opt/yuki-monty", "/usr", "/usr/bin"])
def test_linux_worker_refuses_a_mutable_ancestor(monkeypatch, mutable_path):
    native = Path("/opt/yuki-monty/monty")
    launcher = Path("/opt/yuki-monty/monty-isolated")
    contents = {native: b"native", launcher: b"launcher"}
    monkeypatch.setattr(engine_monty.platform, "system", lambda: "Linux")
    monkeypatch.setattr(engine_monty.platform, "machine", lambda: "aarch64")
    monkeypatch.setattr(engine_monty.os, "geteuid", lambda: 10001, raising=False)
    monkeypatch.setattr(Path, "is_absolute", lambda path: True)
    monkeypatch.setattr(Path, "is_file", lambda path: path in contents)
    monkeypatch.setattr(Path, "read_bytes", lambda path: contents[path])
    monkeypatch.setattr(Path, "resolve", lambda path, **_kwargs: path)
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda path: SimpleNamespace(
            st_uid=0,
            st_mode=stat.S_IFREG | (0o777 if path.as_posix() == mutable_path else 0o755),
        ),
    )
    with pytest.raises(CodeEngineUnavailable, match="code_engine_isolation_mutable"):
        PinnedWorker(
            native,
            hashlib.sha256(b"native").hexdigest(),
            launcher,
            hashlib.sha256(b"launcher").hexdigest(),
        ).verify()


def test_linux_launcher_cannot_verify_a_different_native_file(tmp_path, monkeypatch):
    native = tmp_path / "different-monty"
    native.write_bytes(b"different worker")
    monkeypatch.setattr(engine_monty.platform, "system", lambda: "Linux")
    monkeypatch.setattr(engine_monty.platform, "machine", lambda: "aarch64")
    with pytest.raises(CodeEngineUnavailable, match="code_engine_binary_layout_mismatch"):
        PinnedWorker(native, hashlib.sha256(native.read_bytes()).hexdigest()).verify()


def test_settings_disable_code_mode_unless_explicitly_pinned(tmp_path):
    settings = Settings(_env_file=None)
    with pytest.raises(CodeEngineUnavailable, match="code_engine_not_configured"):
        PinnedWorker.from_settings(settings)
    with pytest.raises(ValueError):
        Settings(_env_file=None, code_mode_worker_sha256="not-a-digest")
    configured = Settings(
        _env_file=None,
        code_mode_worker_path=tmp_path / "monty",
        code_mode_worker_sha256="a" * 64,
        code_mode_max_worker_processes=2,
        code_mode_foreground_reserved_processes=1,
    )
    assert PinnedWorker.from_settings(configured).sha256 == "a" * 64
    assert CodeModeLimits.from_settings(configured) == CodeModeLimits()
