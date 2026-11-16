"""End-to-end IPC round trip over a real Windows Named Pipe.

This is as close to host acceptance as a hermetic test can get: a real pipe,
a real frame exchange, a real HELLO/HELLO_ACK handshake and a real tool call
dispatched through the registry. It proves the transport, codec and
registration agree with each other — it still does **not** prove a bake works
in Blender, which only host acceptance can show.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="Named Pipes are Windows-only"
)

from dcc_plugin_autobake.agent import _wire  # noqa: E402
from dcc_plugin_autobake.agent._dispatch import (  # noqa: E402
    MainThreadDispatcher,
    MainThreadTicker,
    ToolRegistry,
    build_executor,
)
from dcc_plugin_autobake.agent._server import AgentServer  # noqa: E402
from dcc_plugin_autobake.agent._win_pipe import PipeConnection  # noqa: E402


def _pipe_url(tag):
    return r"\\.\pipe\lap-autobake-test-{0}-{1}".format(tag, int(time.time() * 1000) % 100000)


class SidecarStub:
    """Stands in for the sidecar process: connects, handshakes, calls a tool."""

    def __init__(self, url):
        self.url = url
        self.connection = None
        self.results = {}
        self.hello = None
        self.hello_ack = None

    def connect(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.connection = PipeConnection(_open_pipe_handle(self.url))
                return self
            except OSError:
                time.sleep(0.05)
        raise AssertionError("could not connect to {0}".format(self.url))

    def send(self, seq, msg_type, payload):
        _wire.write_frame(self.connection, seq, msg_type, payload)

    def read(self):
        return _wire.read_frame(self.connection)

    def close(self):
        if self.connection is not None:
            self.connection.close()


ctypes = pytest.importorskip("ctypes")


def _open_pipe_handle(url):
    """Open an existing Named Pipe for read/write and return its raw HANDLE."""
    import ctypes.wintypes as wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    GENERIC_READ_WRITE = 0xC0000000
    OPEN_EXISTING = 3
    handle = kernel32.CreateFileW(
        url, GENERIC_READ_WRITE, 0, None, OPEN_EXISTING, 0, None
    )
    if not handle or handle == ctypes.c_void_p(-1).value:
        raise OSError("CreateFileW failed for {0}".format(url))
    return int(handle)


def _start_server(url, registry):
    ticker = MainThreadTicker()
    execute = build_executor(registry, MainThreadDispatcher(ticker=ticker))
    hello = _wire.build_hello(
        agent_pid=1234,
        agent_version="0.1.0",
        dcc_version="5.2",
        main_thread_ident=1234,
        tool_metadata=registry.snapshot_metadata(),
    )
    server = AgentServer(url=url, hello=hello, execute=execute, ticker=ticker)
    events = []
    server.on_connected = lambda payload: events.append(("online", payload))
    server.start(accept_timeout=10.0)
    return server, events


def _registry_echoing_calls():
    registry = ToolRegistry()

    def _status():
        return {"ok": True, "provider_type": "autobake"}

    def _bake(fail=False):
        if fail:
            from dcc_plugin_autobake.agent.tools._errors import ToolError

            raise ToolError("bake refused", code="bake_failed", context={"why": "test"})
        return {"ok": True, "path": "Cube_n.png"}

    registry.register(name="autobake.autobake.status", fn=_status, skill_slug="autobake")
    registry.register(name="autobake.autobake.bake_normal", fn=_bake, skill_slug="autobake")
    return registry


def test_handshake_and_tool_call_round_trip():
    url = _pipe_url("roundtrip")
    server, events = _start_server(url, _registry_echoing_calls())
    stub = SidecarStub(url)
    try:
        stub.connect()
        seq, msg_type, hello = stub.read()
        assert msg_type == _wire.MSG_HELLO
        assert hello["dcc_kind"] == "autobake"
        assert hello["agent_protocol_version"] == _wire.PROTOCOL_VERSION
        assert [tool["name"] for tool in hello["tool_metadata"]] == [
            "autobake.autobake.bake_normal",
            "autobake.autobake.status",
        ]

        stub.send(
            2,
            _wire.MSG_HELLO_ACK,
            {
                "sidecar_pid": 999,
                "sidecar_version": "0.5.7",
                "sidecar_protocol_version": _wire.PROTOCOL_VERSION,
                "capabilities": [],
            },
        )
        assert server.connected.wait(timeout=5.0), "the server never saw HELLO_ACK"
        assert events and events[0][0] == "online"

        stub.send(
            3,
            _wire.MSG_EXEC_TOOL,
            {"tool_name": "autobake.autobake.status", "args": {}, "timeout_s": 5.0},
        )
        _, reply_type, reply = stub.read()
        assert reply_type == _wire.MSG_EXEC_RESULT
        assert reply["success"] is True
        assert reply["result"]["provider_type"] == "autobake"
        assert reply["error"] is None
    finally:
        stub.close()
        server.stop(wait=2.0)


def test_a_failing_tool_returns_a_structured_error():
    url = _pipe_url("error")
    server, _events = _start_server(url, _registry_echoing_calls())
    stub = SidecarStub(url)
    try:
        stub.connect()
        stub.read()  # HELLO
        stub.send(
            2,
            _wire.MSG_HELLO_ACK,
            {
                "sidecar_pid": 999,
                "sidecar_version": "0.5.7",
                "sidecar_protocol_version": _wire.PROTOCOL_VERSION,
                "capabilities": [],
            },
        )
        server.connected.wait(timeout=5.0)

        stub.send(
            3,
            _wire.MSG_EXEC_TOOL,
            {
                "tool_name": "autobake.autobake.bake_normal",
                "args": {"fail": True},
                "timeout_s": 5.0,
            },
        )
        _, reply_type, reply = stub.read()
        assert reply_type == _wire.MSG_EXEC_RESULT
        assert reply["success"] is False
        assert reply["result"] is None
        assert reply["error"]["code"] == "bake_failed"
        assert reply["error"]["context"] == {"why": "test"}
    finally:
        stub.close()
        server.stop(wait=2.0)


def test_an_unknown_tool_is_rejected_by_name():
    url = _pipe_url("unknown")
    server, _events = _start_server(url, _registry_echoing_calls())
    stub = SidecarStub(url)
    try:
        stub.connect()
        stub.read()
        stub.send(
            2,
            _wire.MSG_HELLO_ACK,
            {
                "sidecar_pid": 1,
                "sidecar_version": "0.5.7",
                "sidecar_protocol_version": _wire.PROTOCOL_VERSION,
                "capabilities": [],
            },
        )
        server.connected.wait(timeout=5.0)
        stub.send(
            3,
            _wire.MSG_EXEC_TOOL,
            {"tool_name": "autobake.autobake.nope", "args": {}, "timeout_s": 5.0},
        )
        _, _, reply = stub.read()
        assert reply["success"] is False
        assert reply["error"]["code"] == "tool_not_found"
    finally:
        stub.close()
        server.stop(wait=2.0)


def test_heartbeat_ack_and_shutdown_complete():
    url = _pipe_url("shutdown")
    server, _events = _start_server(url, _registry_echoing_calls())
    stub = SidecarStub(url)
    try:
        stub.connect()
        stub.read()
        stub.send(
            2,
            _wire.MSG_HELLO_ACK,
            {
                "sidecar_pid": 1,
                "sidecar_version": "0.5.7",
                "sidecar_protocol_version": _wire.PROTOCOL_VERSION,
                "capabilities": [],
            },
        )
        server.connected.wait(timeout=5.0)

        assert server.send_heartbeat() is not None
        _, msg_type, payload = stub.read()
        assert msg_type == _wire.MSG_HEARTBEAT
        assert payload["main_thread_last_tick_ns"] > 0

        stub.send(4, _wire.MSG_SHUTDOWN, {"reason": "sidecar_shutdown", "grace_s": 1.0})
        _, ack_type, ack = stub.read()
        assert ack_type == _wire.MSG_SHUTDOWN_ACK
        assert "drained_calls" in ack
    finally:
        stub.close()
        server.stop(wait=2.0)


def test_a_protocol_mismatch_drops_the_connection():
    url = _pipe_url("mismatch")
    server, events = _start_server(url, _registry_echoing_calls())
    stub = SidecarStub(url)
    try:
        stub.connect()
        stub.read()
        stub.send(
            2,
            _wire.MSG_HELLO_ACK,
            {
                "sidecar_pid": 1,
                "sidecar_version": "9.9.9",
                "sidecar_protocol_version": _wire.PROTOCOL_VERSION + 1,
                "capabilities": [],
            },
        )
        time.sleep(0.3)
        assert not server.connected.is_set()
        assert events == []
    finally:
        stub.close()
        server.stop(wait=2.0)


def test_server_accept_times_out_without_a_sidecar():
    url = _pipe_url("timeout")
    registry = _registry_echoing_calls()
    ticker = MainThreadTicker()
    server = AgentServer(
        url=url,
        hello=_wire.build_hello(1, "0.1.0", "5.2", 1, []),
        execute=build_executor(registry, MainThreadDispatcher(ticker=ticker)),
        ticker=ticker,
    )
    server.start(accept_timeout=0.3)
    assert server.stopped.wait(timeout=5.0), "the server never gave up"
    server.stop(wait=1.0)
