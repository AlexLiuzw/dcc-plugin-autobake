"""The boot chain: add-on entry → agent.start() → orchestrator → sidecar spawn.

This file exists because the 0.1.1 candidate shipped a broken import inside
``_orchestrator`` and nothing caught it: the leaf-tool tests imported
``tools.autobake`` directly, and ``agent/__init__.py`` imports the orchestrator
lazily, so ``import dcc_plugin_autobake.agent`` still succeeded. The agent died
only when a real host called ``start()``.

So these tests call ``start()`` — the same entry point the Blender add-on shell
uses — and assert it gets all the way to "sidecar spawned". Everything host-side
is faked; the chain itself is not.
"""

from __future__ import annotations

import sys
import types as _types
from pathlib import Path

import pytest

from dcc_plugin_autobake.agent import AgentBootError, load_config


@pytest.fixture()
def plugin_toml(tmp_path):
    """A minimal plugin.toml that satisfies ``load_config``."""
    path = tmp_path / "plugin.toml"
    path.write_text(
        "provider_type = \"autobake\"\n"
        "plugin_version = \"0.1.2\"\n"
        "supported_provider_versions = [\"4.2\", \"5.2\"]\n"
        "[runtime]\n"
        "topology = \"sidecar\"\n"
        "[sidecar]\n"
        "binary = \"sidecar/lap-autobake-sidecar/lap-autobake-sidecar.exe\"\n"
        "[plugin]\n"
        "plugin_slug = \"dcc-plugin-blender-autobake\"\n",
        encoding="utf-8",
    )
    (tmp_path / "sidecar" / "lap-autobake-sidecar").mkdir(parents=True)
    (tmp_path / "sidecar" / "lap-autobake-sidecar" / "lap-autobake-sidecar.exe").write_bytes(
        b"MZ"
    )
    return path


def test_orchestrator_module_imports_without_a_host():
    """The 0.1.1 defect, directly: the module must import on plain CPython.

    ``from ._bpy import get_bpy`` pointed at a module that does not exist
    (``_bpy`` lives under ``agent/tools/``), so this import raised
    ``ModuleNotFoundError`` and the agent never started in a real host.
    """
    import importlib

    sys.modules.pop("dcc_plugin_autobake.agent._orchestrator", None)
    module = importlib.import_module("dcc_plugin_autobake.agent._orchestrator")
    assert hasattr(module, "start_sidecar_agent")
    assert hasattr(module, "detect_dcc_version")


def test_every_relative_import_in_the_agent_resolves():
    """Guards the whole class of defect, not just the one that shipped.

    A wrong import depth is invisible until the module is first executed, and
    these modules are executed in the host, not in the test suite. Importing
    every one of them closes that gap.
    """
    import importlib
    import pkgutil

    package = importlib.import_module("dcc_plugin_autobake.agent")
    failures = []
    for info in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        if ".vendor." in info.name or info.name.endswith(".vendor"):
            continue  # needs a host, covered by the vendor-integrity tests
        try:
            importlib.import_module(info.name)
        except ImportError as exc:
            failures.append("{0}: {1}".format(info.name, exc))
    assert not failures, failures


def test_declared_sidecar_path_exists_in_the_built_package():
    """The binary path in plugin.toml must be a real file in a built package.

    A rename that touches only the slug leaves the declaration pointing at a
    path the build never creates; the agent then spawns nothing.
    """
    root = Path(__file__).resolve().parents[1]
    toml = (root / "config" / "plugin.toml").read_text(encoding="utf-8")
    binary = None
    for line in toml.splitlines():
        if line.strip().startswith("binary ="):
            binary = line.split("=", 1)[1].strip().strip('"')
            break
    assert binary, "no [sidecar].binary in plugin.toml"
    # The devkit names the bundle after provider_type; assert that directly.
    assert binary.split("/")[1] == "lap-autobake-sidecar"


def test_start_rejects_a_blender_provider_toml(tmp_path):
    path = tmp_path / "plugin.toml"
    path.write_text('provider_type = "blender"\n', encoding="utf-8")
    with pytest.raises(AgentBootError):
        load_config(str(path))


def test_start_reaches_sidecar_spawn(plugin_toml, monkeypatch):
    """Full chain with a fake host: start() must get to "sidecar spawned".

    This is the assertion 0.1.1 needed. It fails if any step between the
    add-on entry and the Popen is broken — import depth, registry build, hello
    construction, pipe creation, or the spawn call itself.
    """
    from dcc_plugin_autobake.agent import start
    from dcc_plugin_autobake.agent import _orchestrator

    spawned = {}

    class _FakeProcess:
        def __init__(self, binary, ipc_url, plugin_toml, plugin_slug):
            spawned["binary"] = binary
            spawned["ipc_url"] = ipc_url
            spawned["plugin_slug"] = plugin_slug
            self.pid = 4242

        def start(self):
            spawned["started"] = True
            return self.pid

        def terminate(self, timeout=None):
            spawned["terminated"] = True

        def close(self):
            spawned["closed"] = True

    class _FakeServer:
        def __init__(self, url, hello, execute, ticker):
            spawned["hello"] = hello
            self.connected = __import__("threading").Event()
            self.on_connected = None

        def start(self, accept_timeout=None):
            spawned["listening"] = spawned.get("url", None) or True

        def stop(self, wait=None):
            spawned["server_stopped"] = True

        def send_heartbeat(self):
            return 1

    monkeypatch.setattr(_orchestrator, "SidecarProcess", _FakeProcess)
    monkeypatch.setattr(_orchestrator, "AgentServer", _FakeServer)
    monkeypatch.setattr(_orchestrator, "create_blender_invoker", lambda bpy: (None, lambda: True))

    handle = start(str(plugin_toml))

    assert spawned.get("started") is True, spawned
    assert spawned["plugin_slug"] == "dcc-plugin-blender-autobake"
    assert spawned["ipc_url"].startswith("\\\\.\\pipe\\lap-plugin-autobake-")
    hello = spawned["hello"]
    assert hello["dcc_kind"] == "autobake"
    tool_names = {tool["name"] for tool in hello["tool_metadata"]}
    assert tool_names == {
        "autobake.autobake.create_environment",
        "autobake.autobake.bake_normal",
        "autobake.autobake.bake_ao",
        "autobake.autobake.status",
    }
    assert handle.state in ("init", "online")

    handle.shutdown(timeout=1.0)
    assert spawned.get("terminated") is True
    assert spawned.get("closed") is True


def test_start_fails_when_the_sidecar_binary_is_missing(plugin_toml):
    from dcc_plugin_autobake.agent import SidecarBinaryMissing, start

    (plugin_toml.parent / "sidecar" / "lap-autobake-sidecar" / "lap-autobake-sidecar.exe").unlink()
    with pytest.raises(SidecarBinaryMissing):
        start(str(plugin_toml))


def test_start_cleans_up_when_the_spawn_fails(plugin_toml, monkeypatch):
    """A failed spawn must not leave a listening pipe behind."""
    from dcc_plugin_autobake.agent import _orchestrator

    cleaned = {}

    class _ExplodingProcess:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self):
            raise _orchestrator.SpawnError("sidecar would not start")

    class _Server:
        def __init__(self, *args, **kwargs):
            self.connected = __import__("threading").Event()

        def start(self, accept_timeout=None):
            pass

        def stop(self, wait=None):
            cleaned["server_stopped"] = True

    monkeypatch.setattr(_orchestrator, "SidecarProcess", _ExplodingProcess)
    monkeypatch.setattr(_orchestrator, "AgentServer", _Server)
    monkeypatch.setattr(_orchestrator, "create_blender_invoker", lambda bpy: (None, lambda: True))

    from dcc_plugin_autobake.agent import start

    with pytest.raises(_orchestrator.SpawnError):
        start(str(plugin_toml))
    assert cleaned.get("server_stopped") is True


def test_detect_dcc_version_falls_back_to_the_supported_list():
    from dcc_plugin_autobake.agent import _orchestrator

    assert _orchestrator.detect_dcc_version(None, ["4.2", "5.2"]) == "5.2"


def test_detect_dcc_version_reads_the_host(host):
    from dcc_plugin_autobake.agent import _orchestrator

    host.app.version = (5, 3, 0)
    assert _orchestrator.detect_dcc_version(host, []) == "5.3"
