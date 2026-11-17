"""Frame codec for the agent↔sidecar channel — stdlib only.

Wire shape (v1), verified against the installed runtime's own codec:

===  =======  =====  ==========================================
off  size     field  notes
===  =======  =====  ==========================================
0    4        magic  ``b"LAIP"``
4    4        seq    uint32 big-endian, sender-monotonic
8    1        type   uint8 message type
9    3        len    uint24 big-endian payload length (max 16 MiB - 1)
12   len      body   UTF-8 JSON object
===  =======  =====  ==========================================

Payload bodies are plain JSON objects; the sidecar's pydantic models are
permissive about field order and reject unknown fields, so every builder
here emits exactly the documented field set.
"""

from __future__ import annotations

import json
import struct

MAGIC = b"LAIP"
HEADER_SIZE = 12
MAX_PAYLOAD_SIZE = (1 << 24) - 1
PROTOCOL_VERSION = 1

#: Message types. Values are wire-visible; they mirror the runtime's enum.
MSG_HELLO = 1
MSG_HELLO_ACK = 2
MSG_EXEC_TOOL = 16
MSG_EXEC_RESULT = 17
MSG_AGENT_LOG = 32
MSG_HEARTBEAT = 48
MSG_SHUTDOWN = 64
MSG_SHUTDOWN_ACK = 65

_NAMES = {
    MSG_HELLO: "HELLO",
    MSG_HELLO_ACK: "HELLO_ACK",
    MSG_EXEC_TOOL: "EXEC_TOOL",
    MSG_EXEC_RESULT: "EXEC_RESULT",
    MSG_AGENT_LOG: "AGENT_LOG",
    MSG_HEARTBEAT: "HEARTBEAT",
    MSG_SHUTDOWN: "SHUTDOWN",
    MSG_SHUTDOWN_ACK: "SHUTDOWN_ACK",
}

#: SHUTDOWN reasons the sidecar's schema accepts.
SHUTDOWN_REASONS = (
    "dcc_quit",
    "user_cancel",
    "sidecar_shutdown",
    "server_shutdown",
    "agent_restart",
)

_HEADER = struct.Struct(">4sIB3s")


class FrameError(Exception):
    """Byte-level framing failure — the stream can no longer be trusted."""


def msg_type_name(msg_type):
    return _NAMES.get(int(msg_type), "0x{0:02x}".format(int(msg_type) & 0xFF))


def pack_frame(seq, msg_type, payload_bytes):
    """Return one framed message."""
    if len(payload_bytes) > MAX_PAYLOAD_SIZE:
        raise FrameError(
            "payload of {0} bytes exceeds the {1}-byte limit".format(
                len(payload_bytes), MAX_PAYLOAD_SIZE
            )
        )
    header = _HEADER.pack(
        MAGIC,
        int(seq) & 0xFFFFFFFF,
        int(msg_type) & 0xFF,
        len(payload_bytes).to_bytes(3, "big"),
    )
    return header + payload_bytes


def write_frame(stream, seq, msg_type, payload):
    """Serialise *payload* as JSON and write one frame to *stream*."""
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    stream.write(pack_frame(seq, msg_type, body))


def read_frame(stream):
    """Read one frame. Returns ``(seq, msg_type, payload_dict)``.

    Raises :class:`FrameError` on a malformed header and re-raises the
    stream's own EOF/closed exception (whatever the transport uses) so the
    caller can distinguish "peer went away" from "peer sent garbage".
    """
    header = _read_exactly(stream, HEADER_SIZE)
    magic, seq, msg_type, raw_len = _HEADER.unpack(header)
    if magic != MAGIC:
        raise FrameError("bad magic {0!r} (peer is not speaking LAIP v1)".format(magic))
    length = int.from_bytes(raw_len, "big")
    body = _read_exactly(stream, length) if length else b""
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FrameError(
            "payload of {0} is not UTF-8 JSON: {1}".format(msg_type_name(msg_type), exc)
        ) from exc
    if not isinstance(payload, dict):
        raise FrameError("payload of {0} is not a JSON object".format(msg_type_name(msg_type)))
    return seq, msg_type, payload


def _read_exactly(stream, count):
    chunks = []
    remaining = count
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            raise FrameError(
                "stream closed after {0} of {1} bytes".format(count - remaining, count)
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


# ---------------------------------------------------------------------------
# payload builders
# ---------------------------------------------------------------------------


def build_hello(agent_pid, agent_version, dcc_version, main_thread_ident, tool_metadata):
    return {
        "agent_pid": int(agent_pid),
        "agent_version": str(agent_version),
        "agent_protocol_version": PROTOCOL_VERSION,
        "dcc_kind": "autobake",
        # The host DCC is Blender; its marketing version is what the sidecar
        # matches against plugin.toml supported_provider_versions.
        "dcc_version": str(dcc_version),
        "main_thread_ident": int(main_thread_ident),
        "tool_metadata": list(tool_metadata),
    }


def build_exec_result(matches_seq, success, result=None, error=None):
    return {
        "matches_seq": int(matches_seq),
        "success": bool(success),
        "result": result,
        "error": error,
    }


def build_exec_error(code, message, context=None):
    return {
        "code": str(code),
        "message": str(message),
        "context": dict(context or {}),
    }


def build_heartbeat(ts_ms, main_thread_last_tick_ns=None):
    return {
        "ts_ms": int(ts_ms),
        "main_thread_last_tick_ns": (
            None if main_thread_last_tick_ns is None else int(main_thread_last_tick_ns)
        ),
    }


def build_shutdown(reason, grace_s):
    if reason not in SHUTDOWN_REASONS:
        raise ValueError(
            "shutdown reason {0!r} is not one of {1}".format(reason, list(SHUTDOWN_REASONS))
        )
    return {"reason": str(reason), "grace_s": float(grace_s)}


__all__ = [
    "FrameError",
    "HEADER_SIZE",
    "MAGIC",
    "MAX_PAYLOAD_SIZE",
    "MSG_AGENT_LOG",
    "MSG_EXEC_RESULT",
    "MSG_EXEC_TOOL",
    "MSG_HEARTBEAT",
    "MSG_HELLO",
    "MSG_HELLO_ACK",
    "MSG_SHUTDOWN",
    "MSG_SHUTDOWN_ACK",
    "PROTOCOL_VERSION",
    "SHUTDOWN_REASONS",
    "build_exec_error",
    "build_exec_result",
    "build_heartbeat",
    "build_hello",
    "build_shutdown",
    "msg_type_name",
    "pack_frame",
    "read_frame",
    "write_frame",
]
