"""Lazy ``bpy`` accessor — the only place the agent imports Blender.

Every tool module goes through :func:`get_bpy` / :func:`require_bpy` so the
rest of the agent stays importable on plain CPython (unit tests, the
packaging smoke test, and the sidecar's own process, which has no ``bpy``).
"""

from __future__ import annotations

import importlib
import sys

from ._errors import HostNotAvailable

_bpy = None
_probed = False


def get_bpy():
    """Return the host's ``bpy`` module, or ``None`` outside Blender.

    Cached after the first successful import; a failed probe is *not*
    cached as "absent" forever, because a dev loop that adds the module to
    ``sys.path`` later should not need a process restart.
    """
    global _bpy, _probed
    if _bpy is not None:
        return _bpy
    module = sys.modules.get("bpy")
    if module is not None:
        _bpy = module
        return module
    try:
        _bpy = importlib.import_module("bpy")
    except ImportError:
        _probed = True
        return None
    return _bpy


def require_bpy():
    """Like :func:`get_bpy` but raises :class:`HostNotAvailable`."""
    bpy = get_bpy()
    if bpy is None:
        raise HostNotAvailable(
            "this tool drives Blender's bake API but the agent is not running "
            "inside a Blender host (no importable bpy)",
            context={"cwd": _cwd()},
        )
    return bpy


def reset_for_tests():
    """Forget the cached module (tests swap in a fake host)."""
    global _bpy, _probed
    _bpy = None
    _probed = False


def _cwd():
    import os

    try:
        return os.getcwd()
    except OSError:  # pragma: no cover - cwd removed underneath us
        return None


def is_in_blender():
    """True when a real host is importable. Used by ``status``."""
    return get_bpy() is not None


__all__ = ["get_bpy", "is_in_blender", "reset_for_tests", "require_bpy"]
