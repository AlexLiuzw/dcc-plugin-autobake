"""Tool registry + main-thread marshalling — stdlib only.

Blender's RNA is not thread-safe: the sidecar's tool calls arrive on the IPC
thread, but every ``bpy`` access must happen on Blender's main thread. Two
pieces cooperate:

* :class:`ToolRegistry` — wire name → callable + declared metadata. Filled at
  boot from the ``@leaf_tool`` decorations.
* :func:`create_blender_invoker` — posts a thunk to ``bpy.app.timers`` and
  returns ``(invoker, is_main_thread)`` so :class:`MainThreadDispatcher` can
  run inline when it is already on the right thread and hop otherwise.

The ticker is what makes a stuck host observable: it records the last time a
tool actually completed on the main thread, and the heartbeat carries that
timestamp upward.
"""

from __future__ import annotations

import threading
import time
import traceback

from ._logging import get_logger

log = get_logger("dispatch")


class ToolNotFound(LookupError):
    """The requested wire name is not registered on this agent."""

    code = "tool_not_found"

    def __init__(self, name):
        super().__init__("no such tool: {0!r}".format(name))
        self.tool_name = str(name)


class DispatchTimeout(RuntimeError):
    """The main thread did not run the thunk in time."""

    code = "main_thread_timeout"


class ToolRegistry:
    """Thread-safe wire-name → spec mapping."""

    __slots__ = ("_specs", "_lock")

    def __init__(self):
        self._specs = {}
        self._lock = threading.Lock()

    def register(self, name, fn, skill_slug, requires_main_thread=True,
                 timeout_seconds=60.0, arg_schema=None, advertise=True):
        if not name or not isinstance(name, str):
            raise ValueError("tool name must be a non-empty string")
        if not callable(fn):
            raise ValueError("tool fn must be callable")
        spec = {
            "name": name,
            "fn": fn,
            "skill_slug": str(skill_slug),
            "requires_main_thread": bool(requires_main_thread),
            "timeout_seconds": float(timeout_seconds),
            "arg_schema": dict(arg_schema or {}),
            "advertise": bool(advertise),
        }
        with self._lock:
            self._specs[name] = spec

    def get(self, name):
        with self._lock:
            spec = self._specs.get(name)
        if spec is None:
            raise ToolNotFound(name)
        return spec

    def __contains__(self, name):
        with self._lock:
            return name in self._specs

    def __len__(self):
        with self._lock:
            return len(self._specs)

    def names(self):
        with self._lock:
            return sorted(self._specs)

    def snapshot_metadata(self):
        """HELLO ``tool_metadata`` — exactly the fields the sidecar validates."""
        with self._lock:
            specs = [spec for spec in self._specs.values() if spec["advertise"]]
        return [
            {
                "name": spec["name"],
                "skill_slug": spec["skill_slug"],
                "requires_main_thread": spec["requires_main_thread"],
                "timeout_seconds": spec["timeout_seconds"],
                "arg_schema": dict(spec["arg_schema"]),
            }
            for spec in sorted(specs, key=lambda item: item["name"])
        ]


class MainThreadTicker:
    """Records when the main thread last completed a tool thunk."""

    __slots__ = ("_last_tick_ns", "_lock")

    def __init__(self):
        self._lock = threading.Lock()
        self._last_tick_ns = time.time_ns()

    def tick(self):
        with self._lock:
            self._last_tick_ns = time.time_ns()

    def last_tick_ns(self):
        with self._lock:
            return self._last_tick_ns

    def idle_seconds(self):
        return (time.time_ns() - self.last_tick_ns()) / 1e9


class MainThreadDispatcher:
    """Run a thunk on Blender's main thread and block until it returns."""

    __slots__ = ("_invoker", "_is_main_thread", "_ticker")

    def __init__(self, invoker=None, is_main_thread=None, ticker=None):
        self._invoker = invoker
        self._is_main_thread = is_main_thread or (lambda: False)
        self._ticker = ticker or MainThreadTicker()

    def dispatch(self, fn, kwargs, timeout):
        if self._invoker is None or self._is_main_thread():
            result = fn(**(kwargs or {}))
            self._ticker.tick()
            return result

        done = threading.Event()
        holder = {}

        def _thunk():
            try:
                holder["result"] = fn(**(kwargs or {}))
            except BaseException as exc:  # noqa: BLE001 - re-raised below
                holder["error"] = exc
                holder["traceback"] = traceback.format_exc()
            finally:
                done.set()

        self._invoker.post(_thunk)
        if not done.wait(timeout=timeout):
            raise DispatchTimeout(
                "the main thread did not finish the tool within {0:.0f}s — "
                "Blender may be blocked by a modal dialog".format(timeout)
            )
        self._ticker.tick()
        if "error" in holder:
            raise holder["error"]
        return holder.get("result")


def create_blender_invoker(bpy):
    """``(invoker, is_main_thread)`` backed by ``bpy.app.timers``.

    Returns ``(None, always-true)`` when the host has no timer API (unit
    tests, or a host so old it predates ``bpy.app.timers``), which makes the
    dispatcher run thunks inline.
    """
    timers = getattr(getattr(bpy, "app", None), "timers", None)
    if timers is None or not hasattr(timers, "register"):
        return None, (lambda: True)

    main_thread_ident = threading.get_ident()

    class _TimerInvoker:
        __slots__ = ()

        def post(self, thunk):
            def _callback():
                try:
                    thunk()
                except BaseException:  # noqa: BLE001
                    log.exception("uncaught exception in a main-thread thunk")
                return None

            timers.register(_callback, first_interval=0.0, persistent=False)

    return _TimerInvoker(), (lambda: threading.get_ident() == main_thread_ident)


def build_executor(registry, dispatcher):
    """Return ``execute(tool_name, args, timeout_s) -> result``."""

    def _execute(tool_name, args, timeout_s):
        spec = registry.get(tool_name)
        started = time.perf_counter()
        outcome = "error"
        try:
            result = dispatcher.dispatch(
                spec["fn"], dict(args or {}), float(timeout_s or spec["timeout_seconds"])
            )
            outcome = "ok"
            return result
        finally:
            log.info(
                "exec_tool tool=%s outcome=%s elapsed_ms=%d",
                tool_name,
                outcome,
                int((time.perf_counter() - started) * 1000),
            )

    return _execute


__all__ = [
    "DispatchTimeout",
    "MainThreadDispatcher",
    "MainThreadTicker",
    "ToolNotFound",
    "ToolRegistry",
    "build_executor",
    "create_blender_invoker",
]
