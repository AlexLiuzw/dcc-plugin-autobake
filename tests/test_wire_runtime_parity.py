"""Cross-checks against the runtime's own codec, when it is importable.

These are the tests that would catch a wire-format drift. They are skipped on
a machine without the LAP runtime installed (a plain CI checkout), so the
suite still runs everywhere; the host acceptance run is where they matter.
"""

from __future__ import annotations

import io

import pytest

runtime_frame = pytest.importorskip(
    "lap_plugin_runtime.ipc.frame", reason="LAP runtime not installed"
)
runtime_protocol = pytest.importorskip("lap_plugin_runtime.ipc.protocol")

from dcc_plugin_autobake.agent import _wire


class _Stream:
    def __init__(self, data=b""):
        self._buffer = io.BytesIO(data)

    def read(self, size):
        return self._buffer.read(size)


def test_our_frames_parse_with_the_runtime_parser():
    payload = {"seq_probe": True, "tool_name": "autobake.autobake.status"}
    stream = io.BytesIO()
    _wire.write_frame(stream, 42, _wire.MSG_EXEC_TOOL, payload)
    seq, msg_type, length = runtime_frame.parse_header(
        stream.getvalue()[: runtime_frame.HEADER_SIZE]
    )
    assert seq == 42
    assert msg_type == int(runtime_protocol.MsgType.EXEC_TOOL)
    assert length == len(stream.getvalue()) - runtime_frame.HEADER_SIZE


def test_runtime_frames_parse_with_our_parser():
    payload = runtime_protocol.HeartbeatPayload(ts_ms=1234, main_thread_last_tick_ns=None)
    raw = runtime_frame.pack_frame(
        7,
        int(runtime_protocol.MsgType.HEARTBEAT),
        runtime_protocol.encode_payload(payload),
    )
    seq, msg_type, body = _wire.read_frame(_Stream(raw))
    assert (seq, msg_type) == (7, _wire.MSG_HEARTBEAT)
    assert body == {"ts_ms": 1234, "main_thread_last_tick_ns": None}


def test_our_hello_is_accepted_by_the_runtime_model():
    payload = _wire.build_hello(
        agent_pid=1,
        agent_version="0.1.0",
        dcc_version="5.2",
        main_thread_ident=2,
        tool_metadata=[
            {
                "name": "autobake.autobake.status",
                "skill_slug": "autobake",
                "requires_main_thread": True,
                "timeout_seconds": 30.0,
                "arg_schema": {"type": "object", "properties": {}},
            }
        ],
    )
    parsed = runtime_protocol.HelloPayload.model_validate(payload)
    assert parsed.dcc_kind == "autobake"
    assert parsed.tool_metadata[0].name == "autobake.autobake.status"


def test_our_heartbeat_matches_the_runtime_model():
    payload = _wire.build_heartbeat(ts_ms=555, main_thread_last_tick_ns=777)
    parsed = runtime_protocol.HeartbeatPayload.model_validate(payload)
    assert parsed.ts_ms == 555
    assert parsed.main_thread_last_tick_ns == 777


def test_our_exec_result_matches_the_runtime_model():
    payload = _wire.build_exec_result(9, True, result={"ok": True})
    parsed = runtime_protocol.ExecResultPayload.model_validate(payload)
    assert parsed.matches_seq == 9
    assert parsed.success is True

    failure = _wire.build_exec_result(
        10, False, error=_wire.build_exec_error("no_active_object", "nope")
    )
    parsed_failure = runtime_protocol.ExecResultPayload.model_validate(failure)
    assert parsed_failure.error.code == "no_active_object"


def test_our_shutdown_matches_the_runtime_literal():
    payload = _wire.build_shutdown("dcc_quit", 2.5)
    parsed = runtime_protocol.ShutdownPayload.model_validate(payload)
    assert parsed.reason == "dcc_quit"
    assert parsed.grace_s == 2.5


def test_ipc_url_scheme_matches_the_runtimes():
    endpoints = pytest.importorskip("lap_plugin_runtime.ipc.endpoints")
    ours = _wire_name = __import__(
        "dcc_plugin_autobake.agent._spawn", fromlist=["mint_ipc_url"]
    ).mint_ipc_url(4321, "dcc-plugin-blender-autobake")
    theirs = endpoints.mint_ipc_url(dcc="autobake", agent_pid=4321, plugin_slug="dcc-plugin-blender-autobake")
    assert ours == theirs
