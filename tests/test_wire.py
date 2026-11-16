"""The frame codec must be able to read what the runtime's own codec writes.

Written against the *installed* runtime so a silent protocol drift (a field
rename, a different length encoding) fails here instead of in the host.
"""

from __future__ import annotations

import io

import pytest

from dcc_plugin_autobake.agent import _wire


class FakeStream:
    """Byte stream over a buffer, with ``read``/``write`` like a pipe."""

    def __init__(self, data=b""):
        self._buffer = io.BytesIO(data)
        self.written = b""

    def read(self, size):
        return self._buffer.read(size)

    def write(self, data):
        self.written += data
        return len(data)


def test_header_layout_is_magic_seq_type_and_uint24_length():
    packed = _wire.pack_frame(0x01020304, _wire.MSG_EXEC_RESULT, b"{}")
    assert packed[:4] == b"LAIP"
    assert packed[4:8] == b"\x01\x02\x03\x04"
    assert packed[8] == 17
    assert packed[9:12] == b"\x00\x00\x02"
    assert packed[12:] == b"{}"


def test_payload_length_round_trips_at_the_boundaries():
    for size in (0, 1, 255, 256, 65535, 65536):
        stream = FakeStream()
        _wire.write_frame(stream, 1, _wire.MSG_HEARTBEAT, {"n": size, "blob": "x" * size})
        read_back = FakeStream(stream.written)
        seq, msg_type, payload = _wire.read_frame(read_back)
        assert seq == 1
        assert msg_type == _wire.MSG_HEARTBEAT
        assert payload["blob"] == "x" * size


def test_oversized_payload_is_refused_before_it_hits_the_wire():
    with pytest.raises(_wire.FrameError):
        _wire.pack_frame(1, _wire.MSG_HELLO, b"x" * (_wire.MAX_PAYLOAD_SIZE + 1))


def test_bad_magic_is_a_frame_error():
    stream = FakeStream(b"NOPE" + b"\x00" * 8)
    with pytest.raises(_wire.FrameError):
        _wire.read_frame(stream)


def test_truncated_frame_is_a_frame_error():
    packed = _wire.pack_frame(1, _wire.MSG_HELLO, b'{"a":1}')
    with pytest.raises(_wire.FrameError):
        _wire.read_frame(FakeStream(packed[:-1]))


def test_non_json_body_is_a_frame_error():
    with pytest.raises(_wire.FrameError):
        _wire.read_frame(FakeStream(b"LAIP\x00\x00\x00\x01\x01\x00\x00\x01X"))


def test_hello_payload_has_exactly_the_fields_the_sidecar_validates():
    payload = _wire.build_hello(
        agent_pid=1234,
        agent_version="0.1.0",
        dcc_version="5.2",
        main_thread_ident=99,
        tool_metadata=[
            {
                "name": "autobake.autobake.status",
                "skill_slug": "autobake",
                "requires_main_thread": True,
                "timeout_seconds": 30.0,
                "arg_schema": {"type": "object"},
            }
        ],
    )
    assert set(payload) == {
        "agent_pid",
        "agent_version",
        "agent_protocol_version",
        "dcc_kind",
        "dcc_version",
        "main_thread_ident",
        "tool_metadata",
    }
    assert payload["dcc_kind"] == "autobake"
    assert payload["agent_protocol_version"] == _wire.PROTOCOL_VERSION


def test_exec_result_success_and_error_shapes():
    ok = _wire.build_exec_result(7, True, result={"ok": True})
    assert ok == {"matches_seq": 7, "success": True, "result": {"ok": True}, "error": None}
    bad = _wire.build_exec_result(
        8, False, error=_wire.build_exec_error("no_active_object", "nope", {"a": 1})
    )
    assert bad["success"] is False
    assert bad["result"] is None
    assert bad["error"] == {"code": "no_active_object", "message": "nope", "context": {"a": 1}}


def test_shutdown_reason_must_be_one_the_sidecar_accepts():
    assert _wire.build_shutdown("dcc_quit", 2.0) == {"reason": "dcc_quit", "grace_s": 2.0}
    with pytest.raises(ValueError):
        _wire.build_shutdown("agent_shutdown", 2.0)


@pytest.mark.parametrize("msg_type", sorted(_wire._NAMES))
def test_every_message_type_has_a_human_name(msg_type):
    assert _wire.msg_type_name(msg_type) != "0x{0:02x}".format(msg_type)
