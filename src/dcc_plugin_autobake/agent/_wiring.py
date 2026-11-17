"""Wire the agent's tool modules into one registry — stdlib only.

Only this plugin's own tools are loaded. There is deliberately no
``composition.json`` peer merge: auto-bake is a separate provider, and
pulling another plugin's tools into this HELLO would advertise them under
``autobake.*`` names that no skill document describes.

``[plugin].tool_packages`` in ``plugin.toml`` is empty by design (the
auto-bake bodies are agent leaf tools, not contract packages); the package
list below is the authoritative inventory.
"""

from __future__ import annotations

import importlib

from ._logging import get_logger
from ._dispatch import ToolRegistry

log = get_logger("wiring")

#: Agent tool modules, in load order. Each one registers its tools at import
#: time through ``@leaf_tool``.
TOOL_MODULES = ("dcc_plugin_autobake.agent.tools.autobake",)


def build_registry(extra_modules=()):
    """Return a :class:`ToolRegistry` with every leaf tool registered."""
    registry = ToolRegistry()
    for module_name in list(TOOL_MODULES) + list(extra_modules):
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001
            log.exception("tool module %r failed to import (skipped)", module_name)
            continue
        _absorb(module, registry)
    log.info("tool registry ready: %d tool(s) %s", len(registry), registry.names())
    return registry


def _absorb(module, registry):
    enumerator = getattr(module, "registered_tools", None)
    if enumerator is None:
        # ``_registry`` holds the process-wide list; every tool module that
        # imported it shares that list, so one enumerator covers all of them.
        fallback = importlib.import_module("dcc_plugin_autobake.agent.tools._registry")
        enumerator = fallback.registered_tools
    for spec in enumerator():
        if spec["name"] in registry:
            continue
        registry.register(
            name=spec["name"],
            fn=spec["fn"],
            skill_slug=spec["skill_slug"],
            requires_main_thread=spec.get("requires_main_thread", True),
            timeout_seconds=spec.get("timeout_seconds", 60.0),
            arg_schema=spec.get("arg_schema"),
            advertise=spec.get("advertise", True),
        )


def tool_inventory(registry):
    """Wire names grouped by skill slug — used by ``status``-style reports."""
    inventory = {}
    for spec in registry.snapshot_metadata():
        inventory.setdefault(spec["skill_slug"], []).append(spec["name"])
    return {slug: sorted(names) for slug, names in sorted(inventory.items())}


__all__ = ["TOOL_MODULES", "build_registry", "tool_inventory"]
