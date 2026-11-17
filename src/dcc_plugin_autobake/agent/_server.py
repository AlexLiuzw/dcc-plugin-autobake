"""Agent↔sidecar server: Named Pipe listener + frame loop (stdlib only).

The read loop lives on its own daemon thread. Tool calls are handed to a
worker thread so a slow bake never blocks the pipe: if the read loop stopped
draining while Blender computed a 900-second bake, the sidecar's own
heartbeats would fill the 64 KiB pipe buffer, its ``WriteFile`` would block,
and it would stop answering the Tool Server. One worker per call keeps the
pipe drained and preserves call ordering per call (calls still run one at a
time on the main thread, which is the actual serialisation).

Shutdown is deliberately layered: send ``SHUTDOWN``, wait briefly for
``SHUTDOWN_ACK``, then close the pipe regardless. The supervisor holds the
kill-on-close Job Object as the last resort.
"""

from __future__ import annotations

import os
import threading
import time

from . import _wire
from ._dispatch import ToolNotFound
from ._logging import get_logger
from ._win_pipe import PipeAcceptTimeout, PipeClosed, PipeError, PipeServer

log = get_logger("server")

ACCEPT_TIMEOUT_S = 30.0
SHUTDOWN_ACK_TIMEOUT_S = 3.0


class AgentServer:
    """Owns the pipe endpoint and the frame state machine."""

    __slots__ = (
        "url",
        "hello",
        "execute",
        "on_connected",
        "on_shutdown",
        "_pipe",
        "_connection",
        "_read_thread",
        "_write_lock",
        "_seq",
        "_seq_lock",
        "_workers",
        "_workers_lock",
        "_connected",
        "_stopped",
        "_stop_requested",
        "_shutdown_ack",
        "_ticker",
        "_pipe_factory",
    )

    def __init__(self, url, hello, execute, ticker, pipe_factory=None):
        self.url = url
        self.hello = dict(hello)
        self.execute = execute
        self.on_connected = None
        self.on_shutdown = None
        self._pipe = None
        self._connection = None
        self._read_thread = None
        self._write_lock = threading.Lock()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._workers = []
        self._workers_lock = threading.Lock()
        self._connected = threading.Event()
        self._stopped = threading.Event()
        self._stop_requested = False
        self._shutdown_ack = threading.Event()
        self._ticker = ticker
        self._pipe_factory = pipe_factory

    # -- state ------------------------------------------------------

    @property
    def connected(self):
        return self._connected

    @property
    def stopped(self):
        return self._stopped

    # -- lifecycle --------------------------------------------------

    def start(self, accept_timeout=ACCEPT_TIMEOUT_S):
        """Open the endpoint and start the read thread (non-blocking).

        Call it *before* spawning the sidecar: the endpoint must exist before
        the child tries to connect, otherwise the connect races into
        ``ERROR_FILE_NOT_FOUND``.
        """
        if self._read_thread is not None:
            raise RuntimeError("AgentServer.start called twice")
        factory = self._pipe_factory or PipeServer
        self._pipe = factory(self.url)
        self._pipe.open()
        self._read_thread = threading.Thread(
            target=self._run,
            name="lap-autobake-ipc-{0}".format(os.getpid()),
            args=(accept_timeout,),
            daemon=True,
        )
        self._read_thread.start()

    def stop(self, wait=None):
        """Close the endpoint; optionally join the read thread."""
        self._stop_requested = True
        for resource in (self._pipe, self._connection):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception:  # noqa: BLE001
                log.debug("closing %r raised (ignored)", resource, exc_info=True)
        if wait is not None and self._read_thread is not None:
            self._read_thread.join(timeout=wait)

    def join_workers(self, timeout=5.0):
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout)
        with self._workers_lock:
            self._workers = [item for item in self._workers if item.is_alive()]

    # -- send path --------------------------------------------------

    def send(self, msg_type, payload):
        """Write one frame; returns the seq used."""
        if self._connection is None:
            raise PipeClosed("no sidecar is connected")
        with self._seq_lock:
            self._seq = (self._seq + 1) & 0xFFFFFFFF
            seq = self._seq
        with self._write_lock:
            _wire.write_frame(self._connection, seq, msg_type, payload)
        return seq

    def send_heartbeat(self):
        try:
            return self.send(
                _wire.MSG_HEARTBEAT,
                _wire.build_heartbeat(
                    ts_ms=int(time.time() * 1000.0),
                    main_thread_last_tick_ns=self._ticker.last_tick_ns(),
                ),
            )
        except Exception:  # noqa: BLE001
            log.debug("heartbeat send failed", exc_info=True)
            return None

    def send_log(self, level, message, extra=None):
        try:
            return self.send(
                _wire.MSG_AGENT_LOG,
                {
                    "level": str(level),
                    "message": str(message),
                    "extra": dict(extra or {}),
                },
            )
        except Exception:  # noqa: BLE001
            log.debug("agent log send failed", exc_info=True)
            return None

    def request_shutdown(self, reason, grace_s=SHUTDOWN_ACK_TIMEOUT_S):
        """Ask the sidecar to drain and exit; waits for its ACK."""
        if self._connection is None:
            return False
        try:
            self.send(_wire.MSG_SHUTDOWN, _wire.build_shutdown(reason, grace_s))
        except Exception:  # noqa: BLE001
            log.debug("SHUTDOWN could not be sent (peer already gone)", exc_info=True)
            return False
        return self._shutdown_ack.wait(timeout=grace_s)

    # -- read loop --------------------------------------------------

    def _run(self, accept_timeout):
        try:
            try:
                self._connection = self._pipe.accept(timeout=accept_timeout)
            except PipeAcceptTimeout:
                log.error(
                    "no sidecar connected to %s within %.0fs", self.url, accept_timeout
                )
                return
            except (PipeClosed, PipeError):
                if not self._stop_requested:
                    log.exception("accept failed")
                return

            log.info("sidecar connected on %s", self.url)
            try:
                self.send(_wire.MSG_HELLO, self.hello)
            except Exception:  # noqa: BLE001
                log.exception("HELLO could not be sent")
                return
            self._read_loop()
        finally:
            self._connected.clear()
            self._stopped.set()
            if self._connection is not None:
                try:
                    self._connection.close()
                except Exception:  # noqa: BLE001
                    log.debug("connection close raised (ignored)", exc_info=True)

    def _read_loop(self):
        while not self._stop_requested:
            try:
                seq, msg_type, payload = _wire.read_frame(self._connection)
            except _wire.FrameError as exc:
                if self._stop_requested:
                    log.info("read loop closed during shutdown")
                else:
                    log.warning("dropping connection: %s", exc)
                return
            except Exception:  # noqa: BLE001 - transport EOF and friends
                if self._stop_requested:
                    log.info("read loop closed during shutdown")
                else:
                    log.info("sidecar disconnected")
                return

            handler = self._HANDLERS.get(msg_type)
            if handler is None:
                log.warning("no handler for %s (seq=%d)", _wire.msg_type_name(msg_type), seq)
                continue
            try:
                handler(self, seq, payload)
            except Exception:  # noqa: BLE001
                log.exception("handler for %s raised", _wire.msg_type_name(msg_type))

    def _on_hello_ack(self, seq, payload):
        version = payload.get("sidecar_protocol_version")
        if version != _wire.PROTOCOL_VERSION:
            log.error(
                "protocol mismatch: agent=%s sidecar=%s — dropping", _wire.PROTOCOL_VERSION, version
            )
            self._stop_requested = True
            return
        log.info(
            "handshake complete: sidecar_pid=%s version=%s capabilities=%s",
            payload.get("sidecar_pid"),
            payload.get("sidecar_version"),
            payload.get("capabilities"),
        )
        self._connected.set()
        if self.on_connected is not None:
            try:
                self.on_connected(payload)
            except Exception:  # noqa: BLE001
                log.exception("on_connected callback raised (ignored)")

    def _on_exec_tool(self, seq, payload):
        tool_name = payload.get("tool_name")
        if not tool_name:
            self._reply_error(seq, "protocol_violation", "EXEC_TOOL without tool_name")
            return
        args = dict(payload.get("args") or {})
        timeout_s = payload.get("timeout_s")
        worker = threading.Thread(
            target=self._run_tool,
            name="lap-autobake-exec-{0}".format(seq),
            args=(seq, tool_name, args, timeout_s),
            daemon=True,
        )
        with self._workers_lock:
            self._workers = [item for item in self._workers if item.is_alive()]
            self._workers.append(worker)
        worker.start()

    def _run_tool(self, seq, tool_name, args, timeout_s):
        try:
            result = self.execute(tool_name, args, timeout_s)
        except ToolNotFound as exc:
            self._reply_error(seq, exc.code, str(exc), {"tool_name": tool_name})
            return
        except Exception as exc:  # noqa: BLE001 - every failure is a tool error
            self._reply_tool_failure(seq, exc)
            return
        try:
            self.send(
                _wire.MSG_EXEC_RESULT,
                _wire.build_exec_result(seq, True, result=result),
            )
        except Exception:  # noqa: BLE001
            log.info("result for %s dropped: the sidecar had already disconnected", tool_name)

    def _reply_tool_failure(self, seq, exc):
        code = getattr(exc, "code", None) or type(exc).__name__
        message = getattr(exc, "message", None) or str(exc)
        context = getattr(exc, "context", None) or {}
        if not context:
            import traceback

            context = {"traceback": traceback.format_exc()}
        self._reply_error(seq, code, message, context)

    def _reply_error(self, seq, code, message, context=None):
        try:
            self.send(
                _wire.MSG_EXEC_RESULT,
                _wire.build_exec_result(
                    seq,
                    False,
                    error=_wire.build_exec_error(code, message, context),
                ),
            )
        except Exception:  # noqa: BLE001
            log.info("error result could not be delivered (sidecar gone)")

    def _on_heartbeat(self, seq, payload):
        log.debug("heartbeat seq=%d ts_ms=%s", seq, payload.get("ts_ms"))

    def _on_shutdown(self, seq, payload):
        reason = payload.get("reason")
        log.info("SHUTDOWN from sidecar: reason=%s", reason)
        try:
            self.send(_wire.MSG_SHUTDOWN_ACK, {"drained_calls": 0})
        except Exception:  # noqa: BLE001
            log.debug("SHUTDOWN_ACK could not be sent", exc_info=True)
        if self.on_shutdown is not None:
            try:
                self.on_shutdown(payload)
            except Exception:  # noqa: BLE001
                log.exception("on_shutdown callback raised (ignored)")
        self._stop_requested = True

    def _on_shutdown_ack(self, seq, payload):
        log.info("SHUTDOWN_ACK: drained_calls=%s", payload.get("drained_calls"))
        self._shutdown_ack.set()
        self._stop_requested = True

    _HANDLERS = {
        _wire.MSG_HELLO_ACK: _on_hello_ack,
        _wire.MSG_EXEC_TOOL: _on_exec_tool,
        _wire.MSG_HEARTBEAT: _on_heartbeat,
        _wire.MSG_SHUTDOWN: _on_shutdown,
        _wire.MSG_SHUTDOWN_ACK: _on_shutdown_ack,
    }


__all__ = ["ACCEPT_TIMEOUT_S", "AgentServer", "SHUTDOWN_ACK_TIMEOUT_S"]
