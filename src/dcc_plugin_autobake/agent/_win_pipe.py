"""Windows Named Pipe server for the agent side — stdlib only, py3.10+.

The sidecar process dials the pipe named in ``LAP_IPC_URL``; this module
owns the listening end. It is byte-mode (the frame codec carries its own
lengths, so message boundaries are unnecessary) and overlapped, so a
blocked ``accept`` or ``read`` can be cancelled from another thread by
closing the endpoint at shutdown.

Only what this plugin needs is implemented: one connected client at a
time, re-accept after the client disconnects, and hard cancellation on
close. ``ctypes`` against ``kernel32`` keeps the module importable with no
third-party dependency, which the stdlib-only agent rule requires.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import sys

log = logging.getLogger(__name__)

PIPE_ACCESS_DUPLEX = 0x00000003
PIPE_TYPE_BYTE = 0x00000000
PIPE_READMODE_BYTE = 0x00000000
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
FILE_FLAG_OVERLAPPED = 0x40000000

INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

ERROR_IO_PENDING = 997
ERROR_PIPE_CONNECTED = 535
ERROR_BROKEN_PIPE = 109
ERROR_NO_DATA = 232
ERROR_OPERATION_ABORTED = 995
ERROR_INVALID_HANDLE = 6

WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF

BUFFER_SIZE = 64 * 1024
MAX_INSTANCES = 2

_CLOSED_ERRORS = (ERROR_BROKEN_PIPE, ERROR_OPERATION_ABORTED, ERROR_NO_DATA, ERROR_INVALID_HANDLE)


class PipeError(RuntimeError):
    """A Windows API call failed."""


class PipeClosed(PipeError):
    """The peer closed, or we closed the endpoint underneath the caller."""


class PipeAcceptTimeout(PipeError):
    """``accept(timeout=...)`` elapsed without a client connecting."""


class _Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


_kernel32 = None


def _api():
    global _kernel32
    if _kernel32 is not None:
        return _kernel32
    if not sys.platform.startswith("win"):
        raise PipeError("Named Pipes are Windows-only; got platform {0!r}".format(sys.platform))
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    k.CreateNamedPipeW.restype = wintypes.HANDLE
    k.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    k.ConnectNamedPipe.restype = wintypes.BOOL
    k.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    k.DisconnectNamedPipe.restype = wintypes.BOOL
    k.ReadFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    k.ReadFile.restype = wintypes.BOOL
    k.WriteFile.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    k.WriteFile.restype = wintypes.BOOL
    k.GetOverlappedResult.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
    ]
    k.GetOverlappedResult.restype = wintypes.BOOL
    k.CancelIoEx.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    k.CancelIoEx.restype = wintypes.BOOL
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    _kernel32 = k
    return k


def _new_event():
    k = _api()
    handle = k.CreateEventW(None, wintypes.BOOL(True), wintypes.BOOL(False), None)
    if not handle:
        err = ctypes.get_last_error()
        raise PipeError("CreateEventW failed: {0}".format(ctypes.WinError(err)))
    return int(handle)


class PipeConnection:
    """A connected client endpoint. ``read`` / ``write`` block."""

    def __init__(self, handle):
        self._handle = int(handle)
        self._read_event = _new_event()
        self._write_event = _new_event()
        self._closed = False

    @property
    def closed(self):
        return self._closed

    def read(self, size):
        """Read up to *size* bytes; returns ``b""`` once the peer is gone."""
        if self._closed or size <= 0:
            return b""
        k = _api()
        buf = ctypes.create_string_buffer(size)
        overlapped = _Overlapped()
        overlapped.hEvent = self._read_event
        read = wintypes.DWORD(0)
        ok = k.ReadFile(
            wintypes.HANDLE(self._handle),
            buf,
            wintypes.DWORD(size),
            ctypes.byref(read),
            ctypes.byref(overlapped),
        )
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_IO_PENDING:
                ok = k.GetOverlappedResult(
                    wintypes.HANDLE(self._handle),
                    ctypes.byref(overlapped),
                    ctypes.byref(read),
                    wintypes.BOOL(True),
                )
                if not ok:
                    err = ctypes.get_last_error()
                    if err in _CLOSED_ERRORS:
                        return b""
                    raise PipeError(
                        "ReadFile completion failed: {0}".format(ctypes.WinError(err))
                    )
            elif err in _CLOSED_ERRORS:
                return b""
            else:
                raise PipeError("ReadFile failed: {0}".format(ctypes.WinError(err)))
        return buf.raw[: read.value]

    def write(self, data):
        """Write *data*; returns the byte count."""
        if self._closed:
            raise PipeClosed("write on a closed pipe")
        if not data:
            return 0
        k = _api()
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        overlapped = _Overlapped()
        overlapped.hEvent = self._write_event
        written = wintypes.DWORD(0)
        ok = k.WriteFile(
            wintypes.HANDLE(self._handle),
            buf,
            wintypes.DWORD(len(data)),
            ctypes.byref(written),
            ctypes.byref(overlapped),
        )
        if not ok:
            err = ctypes.get_last_error()
            if err == ERROR_IO_PENDING:
                ok = k.GetOverlappedResult(
                    wintypes.HANDLE(self._handle),
                    ctypes.byref(overlapped),
                    ctypes.byref(written),
                    wintypes.BOOL(True),
                )
                if not ok:
                    err = ctypes.get_last_error()
                    if err in _CLOSED_ERRORS:
                        raise PipeClosed("peer closed during write")
                    raise PipeError(
                        "WriteFile completion failed: {0}".format(ctypes.WinError(err))
                    )
            elif err in _CLOSED_ERRORS:
                raise PipeClosed("peer closed during write")
            else:
                raise PipeError("WriteFile failed: {0}".format(ctypes.WinError(err)))
        return written.value

    def close(self):
        if self._closed:
            return
        self._closed = True
        k = _api()
        try:
            k.CancelIoEx(wintypes.HANDLE(self._handle), None)
        except Exception:  # noqa: BLE001
            log.debug("CancelIoEx on connection raised (ignored)", exc_info=True)
        for handle in (self._handle, self._read_event, self._write_event):
            try:
                k.CloseHandle(wintypes.HANDLE(handle))
            except Exception:  # noqa: BLE001
                log.debug("CloseHandle raised (ignored)", exc_info=True)


class PipeServer:
    """Listening end. Owns the pipe instance until :meth:`close`."""

    def __init__(self, url):
        if not url or not url.lower().startswith("\\\\.\\pipe\\"):
            raise PipeError(
                "not a local Named Pipe URL: {0!r} (expected \\\\.\\pipe\\...)".format(url)
            )
        self.url = url
        self._handle = None
        self._accept_event = None
        self._closed = False

    def open(self):
        """Create the pipe instance. Idempotent."""
        if self._handle is not None or self._closed:
            return
        k = _api()
        mode = PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS
        handle = k.CreateNamedPipeW(
            wintypes.LPCWSTR(self.url),
            wintypes.DWORD(PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED),
            wintypes.DWORD(mode),
            wintypes.DWORD(MAX_INSTANCES),
            wintypes.DWORD(BUFFER_SIZE),
            wintypes.DWORD(BUFFER_SIZE),
            wintypes.DWORD(0),
            None,
        )
        if not handle or handle == INVALID_HANDLE_VALUE:
            err = ctypes.get_last_error()
            raise PipeError(
                "CreateNamedPipeW({0!r}) failed: {1}".format(self.url, ctypes.WinError(err))
            )
        self._handle = int(handle)
        self._accept_event = _new_event()
        log.info("named pipe listening: %s", self.url)

    def accept(self, timeout=None):
        """Block until a client connects; returns a :class:`PipeConnection`.

        Raises :class:`PipeAcceptTimeout` when *timeout* elapses and
        :class:`PipeClosed` when the endpoint was closed underneath us.
        """
        if self._handle is None:
            if self._closed:
                raise PipeClosed("accept on a closed pipe server")
            self.open()
        k = _api()
        overlapped = _Overlapped()
        overlapped.hEvent = self._accept_event
        ok = k.ConnectNamedPipe(wintypes.HANDLE(self._handle), ctypes.byref(overlapped))
        if ok:
            return self._detach()
        err = ctypes.get_last_error()
        if err == ERROR_PIPE_CONNECTED:
            return self._detach()
        if err != ERROR_IO_PENDING:
            if err in _CLOSED_ERRORS:
                raise PipeClosed("ConnectNamedPipe failed: {0}".format(ctypes.WinError(err)))
            raise PipeError(
                "ConnectNamedPipe failed: {0}".format(ctypes.WinError(err))
            )

        wait_ms = INFINITE if timeout is None else int(max(0.0, float(timeout)) * 1000.0)
        result = k.WaitForSingleObject(wintypes.HANDLE(self._accept_event), wintypes.DWORD(wait_ms))
        if result == WAIT_TIMEOUT:
            try:
                k.CancelIoEx(wintypes.HANDLE(self._handle), ctypes.byref(overlapped))
            except Exception:  # noqa: BLE001
                log.debug("CancelIoEx on accept raised (ignored)", exc_info=True)
            raise PipeAcceptTimeout(
                "no sidecar connected within {0:.1f}s".format(float(timeout))
            )
        if result == WAIT_FAILED:
            err = ctypes.get_last_error()
            raise PipeError("WaitForSingleObject failed: {0}".format(ctypes.WinError(err)))
        if result != WAIT_OBJECT_0:
            raise PipeError("WaitForSingleObject returned 0x{0:x}".format(result))

        moved = wintypes.DWORD(0)
        ok = k.GetOverlappedResult(
            wintypes.HANDLE(self._handle),
            ctypes.byref(overlapped),
            ctypes.byref(moved),
            wintypes.BOOL(True),
        )
        if not ok:
            err = ctypes.get_last_error()
            if err in _CLOSED_ERRORS:
                raise PipeClosed("accept aborted: {0}".format(ctypes.WinError(err)))
            raise PipeError(
                "accept completion failed: {0}".format(ctypes.WinError(err))
            )
        return self._detach()

    def close(self):
        """Close the endpoint. Idempotent; unblocks a pending ``accept``."""
        if self._closed:
            return
        self._closed = True
        k = _api()
        if self._handle is not None:
            try:
                k.CancelIoEx(wintypes.HANDLE(self._handle), None)
            except Exception:  # noqa: BLE001
                log.debug("CancelIoEx on server raised (ignored)", exc_info=True)
            try:
                k.CloseHandle(wintypes.HANDLE(self._handle))
            except Exception:  # noqa: BLE001
                log.debug("CloseHandle(server) raised (ignored)", exc_info=True)
            self._handle = None
        if self._accept_event is not None:
            try:
                k.CloseHandle(wintypes.HANDLE(self._accept_event))
            except Exception:  # noqa: BLE001
                log.debug("CloseHandle(accept event) raised (ignored)", exc_info=True)
            self._accept_event = None

    def _detach(self):
        """Hand the connected handle to a :class:`PipeConnection`.

        The endpoint is *not* re-armed: this plugin runs one sidecar per
        agent, and the supervisor rebuilds the whole stack (endpoint +
        sidecar) if that sidecar dies.
        """
        handle = self._handle
        self._handle = None
        if self._accept_event is not None:
            try:
                _api().CloseHandle(wintypes.HANDLE(self._accept_event))
            except Exception:  # noqa: BLE001
                log.debug("CloseHandle(accept event) raised (ignored)", exc_info=True)
            self._accept_event = None
        return PipeConnection(handle)


__all__ = [
    "PipeAcceptTimeout",
    "PipeClosed",
    "PipeConnection",
    "PipeError",
    "PipeServer",
]
