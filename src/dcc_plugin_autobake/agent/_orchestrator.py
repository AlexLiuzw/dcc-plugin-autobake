"""Boot sequence: registry → main-thread plumbing → pipe → sidecar.

Order matters and is documented below; :func:`start_sidecar_agent` is the one
place it is encoded.

1. Register the vendored add-on in the host (so ``Scene.at_tool`` and the
   operators exist before any tool call arrives). A failure here is logged and
   swallowed: the provider must still come up, and each tool reports the real
   error at call time rather than the plugin vanishing from ``list-providers``.
2. Build the tool registry from the ``@leaf_tool`` decorations.
3. Read the host version. It goes into HELLO and the sidecar rejects the
   handshake if ``plugin.toml`` does not list it — a wrong-host install fails
   loudly instead of baking with a Cycles API that is not there.
4. Create the main-thread invoker on the *calling* thread (which is Blender's
   main thread; the invoker captures its ident).
5. Open the pipe endpoint, then spawn the sidecar — never the reverse.
6. Start the heartbeat.
"""

from __future__ import annotations

import os

from . import AgentBootError, AgentHandle
from ._addon import ensure_registered
from ._bpy import get_bpy
from ._dispatch import MainThreadDispatcher, MainThreadTicker, build_executor, create_blender_invoker
from ._logging import get_logger
from ._server import ACCEPT_TIMEOUT_S, AgentServer
from ._spawn import SidecarProcess, SpawnError, mint_ipc_url
from ._wire import build_hello
from ._wiring import build_registry

log = get_logger("orchestrator")


def start_sidecar_agent(config):
    """Bring up the sidecar topology. Returns a populated :class:`AgentHandle`."""
    bpy = get_bpy()
    _register_host_addon(bpy)

    registry = build_registry()
    if len(registry) == 0:
        raise AgentBootError(
            "the auto-bake tool registry came up empty — check that "
            "dcc_plugin_autobake.agent.tools.autobake imports cleanly"
        )

    dcc_version = detect_dcc_version(bpy, config.supported_versions)
    invoker, is_main_thread = create_blender_invoker(bpy)
    ticker = MainThreadTicker()
    dispatcher = MainThreadDispatcher(invoker=invoker, is_main_thread=is_main_thread, ticker=ticker)
    execute = build_executor(registry, dispatcher)

    ipc_url = os.environ.get("LAP_IPC_URL") or mint_ipc_url(os.getpid(), config.plugin_slug)
    hello = build_hello(
        agent_pid=os.getpid(),
        agent_version=_agent_version(),
        dcc_version=dcc_version,
        main_thread_ident=_main_thread_ident(),
        tool_metadata=registry.snapshot_metadata(),
    )
    server = AgentServer(url=ipc_url, hello=hello, execute=execute, ticker=ticker)
    handle = AgentHandle(config)
    process = None
    try:
        server.start(accept_timeout=ACCEPT_TIMEOUT_S)
        process = SidecarProcess(
            binary=config.sidecar_binary,
            ipc_url=ipc_url,
            plugin_toml=config.plugin_toml,
            plugin_slug=config.plugin_slug,
        )
        process.start()
        server.on_connected = handle._mark_online

        heartbeat = _StartHeartbeat(server, ticker)
        heartbeat.start()

        handle._process = process
        handle._server = server
        handle._heartbeat = heartbeat
    except Exception:
        _teardown(server, process)
        raise

    log.info(
        "autobake agent up: url=%s tools=%d dcc_version=%s plugin_version=%s",
        ipc_url,
        len(registry),
        dcc_version,
        config.plugin_version,
    )
    return handle


def detect_dcc_version(bpy=None, supported=()):
    """Blender's marketing version (``"5.2"``), best effort.

    ``bpy.app.version`` is the authority. Outside a host, the newest entry in
    ``plugin.toml`` supported_provider_versions is used so a unit test still
    produces a handshake-shaped payload.
    """
    if bpy is None:
        bpy = get_bpy()
    version = getattr(getattr(bpy, "app", None), "version", None) if bpy is not None else None
    if version:
        try:
            return "{0}.{1}".format(int(version[0]), int(version[1]))
        except (TypeError, ValueError, IndexError):
            log.warning("bpy.app.version is not parseable: %r", version)
    if supported:
        return str(supported[-1])
    return "unknown"


def _agent_version():
    from ._compat import AGENT_VERSION

    return AGENT_VERSION


def _main_thread_ident():
    import threading

    return threading.get_ident()


def _register_host_addon(bpy):
    """Register the vendored add-on; never fatal.

    Returns a status dict for the boot log. A failure here is not a boot
    failure: the provider must still appear in ``list-providers`` so an
    operator can see it and read the tool-level error, instead of the plugin
    silently not existing.
    """
    if bpy is None:
        log.warning("no bpy available — the auto-bake panel will not exist here")
        return {"ok": False, "reason": "no_host"}
    try:
        ensure_registered(bpy)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "auto-bake add-on registration failed; tools will report the error "
            "when called",
            exc_info=True,
        )
        return {"ok": False, "reason": "register_failed", "detail": str(exc)}
    from ._addon import registration_owner

    status = {"ok": True, "owner": registration_owner()}
    log.info("host add-on status: %s", status)
    return status


def _teardown(server, process):
    if process is not None:
        try:
            process.terminate(timeout=2.0)
        except Exception:  # noqa: BLE001
            log.debug("sidecar terminate raised (ignored)", exc_info=True)
        try:
            process.close()
        except Exception:  # noqa: BLE001
            log.debug("sidecar close raised (ignored)", exc_info=True)
    if server is not None:
        try:
            server.stop(wait=1.0)
        except Exception:  # noqa: BLE001
            log.debug("server stop raised (ignored)", exc_info=True)


class _StartHeartbeat:
    """Periodic HEARTBEAT writer (2 s, matching the IPC session default)."""

    __slots__ = ("_server", "_ticker", "_thread", "_stop")

    def __init__(self, server, ticker, interval_s=2.0):
        import threading

        self._server = server
        self._ticker = ticker
        self._stop = threading.Event()
        self._thread = None
        self._interval = float(interval_s)

    def start(self):
        import threading

        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="autobake-heartbeat", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self):
        while not self._stop.wait(self._interval):
            if self._server.connected.is_set():
                self._server.send_heartbeat()


__all__ = ["detect_dcc_version", "start_sidecar_agent"]
