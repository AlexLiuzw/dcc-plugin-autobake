"""Agent package — the DCC-side half of the sidecar topology.

Runs inside Blender's embedded Python. Two constraints shape every module
here:

* **stdlib only** — no ``lap_plugin_runtime``, no ``pydantic``, nothing from
  ``pip``. The host's interpreter is whatever Blender ships.
* **``bpy`` only where it is needed** — everything host-facing sits behind
  :mod:`._bpy` and the tool modules, so the package still imports (and its
  tests still run) on plain CPython.

:func:`start` is the entry point the Blender add-on shell calls: it reads
``plugin.toml``, builds the tool registry, opens the Named Pipe, spawns the
sidecar, and returns a handle whose :meth:`AgentHandle.shutdown` tears the
whole stack down.
"""

from __future__ import annotations

import os
import threading

from ._compat import AGENT_VERSION, ENV_LAP_IPC_URL, read_toml
from ._logging import get_logger

log = get_logger("agent")

DEFAULT_TOPOLOGY = "sidecar"
KNOWN_TOPOLOGIES = ("sidecar", "in-process")


class AgentBootError(Exception):
    """The agent could not come up."""


class MissingPluginToml(AgentBootError):
    """``plugin.toml`` was not found at the given path."""


class SidecarBinaryMissing(AgentBootError):
    """``[sidecar].binary`` is declared but the executable is not there."""


class AgentConfig:
    """Resolved startup configuration."""

    __slots__ = ("plugin_toml", "topology", "sidecar_binary", "plugin_slug", "plugin_version",
                 "provider_type", "supported_versions")

    def __init__(self, plugin_toml, topology, sidecar_binary, plugin_slug, plugin_version,
                 provider_type, supported_versions):
        self.plugin_toml = plugin_toml
        self.topology = topology
        self.sidecar_binary = sidecar_binary
        self.plugin_slug = plugin_slug
        self.plugin_version = plugin_version
        self.provider_type = provider_type
        self.supported_versions = list(supported_versions)

    def __repr__(self):
        return (
            "AgentConfig(plugin_toml={0!r}, topology={1!r}, plugin_slug={2!r}, "
            "plugin_version={3!r}, provider_type={4!r})"
        ).format(
            self.plugin_toml,
            self.topology,
            self.plugin_slug,
            self.plugin_version,
            self.provider_type,
        )


def load_config(plugin_toml):
    """Read *plugin_toml* and validate the fields the agent depends on."""
    if not plugin_toml or not os.path.isfile(plugin_toml):
        raise MissingPluginToml("plugin.toml not found at {0!r}".format(plugin_toml))
    path = os.path.abspath(plugin_toml)
    data = read_toml(path)

    provider_type = data.get("provider_type")
    if provider_type != "autobake":
        raise AgentBootError(
            "plugin.toml provider_type is {0!r}; this agent only serves 'autobake' "
            "(a mix-up here would register auto-bake tools on the Blender "
            "provider)".format(provider_type)
        )

    runtime = data.get("runtime") or {}
    sidecar = data.get("sidecar") or {}
    plugin = data.get("plugin") or {}

    topology = runtime.get("topology", DEFAULT_TOPOLOGY)
    if topology not in KNOWN_TOPOLOGIES:
        raise AgentBootError(
            "[runtime].topology = {0!r} is not one of {1}".format(
                topology, list(KNOWN_TOPOLOGIES)
            )
        )

    binary = sidecar.get("binary")
    if binary and not os.path.isabs(binary):
        binary = os.path.abspath(os.path.join(os.path.dirname(path), binary))

    return AgentConfig(
        plugin_toml=path,
        topology=topology,
        sidecar_binary=binary,
        plugin_slug=plugin.get("plugin_slug") or "",
        plugin_version=str(data.get("plugin_version") or ""),
        provider_type=provider_type,
        supported_versions=data.get("supported_provider_versions") or [],
    )


def _validate(config):
    if config.topology != "sidecar":
        return
    if not config.sidecar_binary:
        raise SidecarBinaryMissing(
            "topology = 'sidecar' but plugin.toml does not declare [sidecar].binary"
        )
    if not os.path.isfile(config.sidecar_binary):
        raise SidecarBinaryMissing(
            "sidecar executable missing: {0!r}".format(config.sidecar_binary)
        )


class AgentHandle:
    """Handle over a running agent — the add-on shell keeps one per host."""

    __slots__ = (
        "config",
        "_process",
        "_server",
        "_heartbeat",
        "_state",
        "_online_callbacks",
        "_online_event",
        "_online_payload",
    )

    def __init__(self, config):
        self.config = config
        self._process = None
        self._server = None
        self._heartbeat = None
        self._state = "init"
        self._online_callbacks = []
        self._online_event = threading.Event()
        self._online_payload = None

    @property
    def state(self):
        return self._state

    def on_online(self, callback):
        """Call *callback* once the sidecar completes the handshake."""
        if self._online_event.is_set():
            try:
                callback(self._online_payload)
            except Exception:  # noqa: BLE001
                log.exception("on_online callback raised (ignored)")
            return
        self._online_callbacks.append(callback)

    def _mark_online(self, payload):
        self._state = "online"
        self._online_payload = payload
        self._online_event.set()
        callbacks, self._online_callbacks = self._online_callbacks, []
        for callback in callbacks:
            try:
                callback(payload)
            except Exception:  # noqa: BLE001
                log.exception("on_online callback raised (ignored)")

    def shutdown(self, timeout=5.0, reason="dcc_quit"):
        """Stop the sidecar, then the listener. Idempotent."""
        if self._state == "unregistered":
            return
        self._state = "draining"
        server, process, heartbeat = self._server, self._process, self._heartbeat
        self._server = self._process = self._heartbeat = None
        try:
            if heartbeat is not None:
                heartbeat.stop()
            if server is not None:
                try:
                    server.request_shutdown(reason, grace_s=min(float(timeout), 3.0))
                except Exception:  # noqa: BLE001
                    log.debug("graceful shutdown request failed", exc_info=True)
            if process is not None:
                process.terminate(timeout=max(1.0, float(timeout) / 2.0))
            if server is not None:
                server.stop(wait=1.0)
        finally:
            if process is not None:
                process.close()
            self._state = "unregistered"


def start(plugin_toml, plugin_id=None):
    """Boot the agent for *plugin_toml*. Returns an :class:`AgentHandle`."""
    config = load_config(plugin_toml)
    _validate(config)
    log.info(
        "booting autobake agent: plugin_toml=%s plugin_slug=%s version=%s plugin_id=%s",
        config.plugin_toml,
        config.plugin_slug,
        config.plugin_version,
        plugin_id,
    )
    if config.topology == "in-process":
        raise AgentBootError(
            "topology 'in-process' is not supported by this plugin: auto-bake "
            "needs a sidecar so the Tool Server connection survives a slow bake"
        )
    from ._orchestrator import start_sidecar_agent

    return start_sidecar_agent(config)


def shutdown(handle, timeout=None):
    handle.shutdown(timeout if timeout is not None else 5.0)


__all__ = [
    "AGENT_VERSION",
    "AgentBootError",
    "AgentConfig",
    "AgentHandle",
    "MissingPluginToml",
    "SidecarBinaryMissing",
    "load_config",
    "shutdown",
    "start",
]
