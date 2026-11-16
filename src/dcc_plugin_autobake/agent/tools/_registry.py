"""``@leaf_tool`` decorator + enumerator — stdlib only.

A leaf tool is a plain callable the sidecar reaches over IPC. The decorator
only records metadata (wire name, skill slug, main-thread requirement,
declared schema); it does not import anything from the host, so test code
can import every tool module on plain CPython.
"""

from __future__ import annotations

_LEAF_TOOLS = []

#: Tools advertised in HELLO. ``name`` is the full three-segment wire name
#: ``<provider_type>.<skill_slug>.<tool>`` — the sidecar resolves tool calls
#: by exactly this string.
def leaf_tool(name, skill_slug, requires_main_thread=True, timeout_s=60.0, arg_schema=None, advertise=True):
    def _decorator(fn):
        _LEAF_TOOLS.append(
            {
                "name": str(name),
                "fn": fn,
                "skill_slug": str(skill_slug),
                "requires_main_thread": bool(requires_main_thread),
                "timeout_seconds": float(timeout_s),
                "arg_schema": dict(arg_schema) if arg_schema else {},
                "advertise": bool(advertise),
            }
        )
        return fn

    return _decorator


def registered_tools():
    """Return a copy of every registration made so far, in import order."""
    return [dict(entry) for entry in _LEAF_TOOLS]


def _reset_for_tests():
    del _LEAF_TOOLS[:]


__all__ = ["leaf_tool", "registered_tools"]
