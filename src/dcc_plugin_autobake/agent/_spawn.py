"""Spawn and supervise the sidecar process — stdlib only.

Windows only, like every sidecar-topology LAP plugin: the IPC transport is a
Named Pipe. The child is bound to a kill-on-close Job Object so a Blender
crash reaps it instead of leaving an orphan holding a Tool Server
registration.

stdout/stderr are drained on daemon threads and re-logged, so a sidecar that
dies during bootstrap leaves its traceback in Blender's console — the usual
failure mode otherwise is a silent "sidecar never appeared".
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import subprocess
import sys
import threading

from ._compat import ENV_LAP_IPC_URL, ENV_LAP_PLUGIN_SLUG, ENV_LAP_PLUGIN_TOML
from ._logging import get_logger

log = get_logger("spawn")

CREATE_NO_WINDOW = 0x08000000

#: Time the child gets to announce itself on the pipe before we call the boot
#: failed. Matches the orchestrator's accept timeout.
BOOT_TIMEOUT_S = 30.0


class SpawnError(RuntimeError):
    """The sidecar could not be started or contained."""


def mint_ipc_url(agent_pid, plugin_slug):
    """Pipe URL for this agent — mirrors the runtime's scheme exactly."""
    slug = (plugin_slug or "").strip()
    if slug:
        return r"\\.\pipe\lap-plugin-autobake-{0}-{1}".format(slug, int(agent_pid))
    return r"\\.\pipe\lap-plugin-autobake-{0}".format(int(agent_pid))


class _StreamForwarder(threading.Thread):
    """Copy a child pipe to the agent log, line by line."""

    def __init__(self, stream, level, tag):
        super().__init__(name="autobake-sidecar-{0}".format(tag), daemon=True)
        self._stream = stream
        self._level = level
        self._tag = tag

    def run(self):
        try:
            for raw in iter(self._stream.readline, b""):
                if not raw:
                    break
                text = raw.decode("utf-8", errors="replace").rstrip()
                if text:
                    log.log(self._level, "sidecar[%s] %s", self._tag, text)
        except Exception:  # noqa: BLE001
            log.debug("sidecar stream forwarder ended", exc_info=True)


class SidecarProcess:
    """One spawned sidecar, plus its Job Object binding."""

    __slots__ = ("binary", "ipc_url", "plugin_toml", "plugin_slug", "_process", "_job",
                 "_forwarders")

    def __init__(self, binary, ipc_url, plugin_toml, plugin_slug):
        self.binary = str(binary)
        self.ipc_url = str(ipc_url)
        self.plugin_toml = str(plugin_toml)
        self.plugin_slug = str(plugin_slug or "")
        self._process = None
        self._job = None
        self._forwarders = []

    @property
    def pid(self):
        return self._process.pid if self._process is not None else None

    @property
    def is_alive(self):
        return self._process is not None and self._process.poll() is None

    @property
    def returncode(self):
        return self._process.returncode if self._process is not None else None

    def start(self):
        if self._process is not None:
            raise SpawnError("SidecarProcess.start called twice")
        if not os.path.isfile(self.binary):
            raise SpawnError(
                "sidecar executable is missing: {0!r} (rebuild with "
                "`lap-plugin build-all`)".format(self.binary)
            )

        entry = os.path.join(os.path.dirname(self.binary), "entry.py")
        argv = [self.binary]
        if os.path.isfile(entry):
            argv.append(entry)

        env = os.environ.copy()
        env[ENV_LAP_IPC_URL] = self.ipc_url
        env[ENV_LAP_PLUGIN_TOML] = self.plugin_toml
        if self.plugin_slug:
            env[ENV_LAP_PLUGIN_SLUG] = self.plugin_slug

        kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "env": env,
            "close_fds": True,
            "bufsize": 0,
        }
        if sys.platform.startswith("win"):
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            self._process = subprocess.Popen(argv, **kwargs)
        except OSError as exc:
            raise SpawnError("could not start {0!r}: {1}".format(argv, exc)) from exc

        self._job = _KillOnCloseJob()
        try:
            self._job.assign(self._process._handle)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            log.warning(
                "Job-Object binding failed; the sidecar will not be reaped "
                "automatically if Blender crashes",
                exc_info=True,
            )
            self._job.close()
            self._job = None

        for stream, level, tag in (
            (self._process.stdout, 20, "out"),
            (self._process.stderr, 30, "err"),
        ):
            forwarder = _StreamForwarder(stream, level, tag)
            forwarder.start()
            self._forwarders.append(forwarder)

        log.info("sidecar started: pid=%s url=%s", self.pid, self.ipc_url)
        return self.pid

    def wait(self, timeout=None):
        if self._process is None:
            return None
        try:
            return self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self, timeout=5.0):
        if self._process is None or not self.is_alive:
            return
        try:
            self._process.terminate()
        except Exception:  # noqa: BLE001
            log.debug("terminate raised (ignored)", exc_info=True)
        self.wait(timeout=timeout)
        if self.is_alive:
            try:
                self._process.kill()
            except Exception:  # noqa: BLE001
                log.debug("kill raised (ignored)", exc_info=True)
            self.wait(timeout=timeout)

    def close(self):
        try:
            if self._process is not None:
                for name in ("stdout", "stderr", "stdin"):
                    stream = getattr(self._process, name, None)
                    if stream is None:
                        continue
                    try:
                        stream.close()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if self._job is not None:
                self._job.close()
                self._job = None


class _KillOnCloseJob:
    """Windows Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``.

    Closing the last handle (which happens when this Blender process exits,
    however it exits) kills every process in the job, so a crashed host never
    leaves a sidecar behind.
    """

    __slots__ = ("_handle",)

    def __init__(self):
        self._handle = None

    def assign(self, process_handle):
        if not sys.platform.startswith("win"):
            raise OSError("Job Objects are Windows-only")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            err = ctypes.get_last_error()
            raise OSError("CreateJobObjectW failed: {0}".format(ctypes.WinError(err)))

        class _Limits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Limits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ):
            err = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(
                "SetInformationJobObject failed: {0}".format(ctypes.WinError(err))
            )
        if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(process_handle))):
            err = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(
                "AssignProcessToJobObject failed: {0}".format(ctypes.WinError(err))
            )
        self._handle = int(job)

    def close(self):
        if self._handle is None:
            return
        try:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
                wintypes.HANDLE(self._handle)
            )
        except Exception:  # noqa: BLE001
            log.debug("CloseHandle(job) raised (ignored)", exc_info=True)
        self._handle = None


__all__ = [
    "BOOT_TIMEOUT_S",
    "CREATE_NO_WINDOW",
    "SidecarProcess",
    "SpawnError",
    "mint_ipc_url",
]
