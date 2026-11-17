"""Namespaced logger for the agent.

Blender's embedded Python owns the root logger and forwards it to the
System Console; adding our own handler keeps plugin lines identifiable there
without touching anything else in the host.
"""

from __future__ import annotations

import logging
import os

ROOT = "dcc_plugin_autobake.agent"
_configured = False


def get_logger(name):
    """Return ``dcc_plugin_autobake.agent.<name>``."""
    _ensure_configured()
    if name.startswith(ROOT):
        return logging.getLogger(name)
    return logging.getLogger("{0}.{1}".format(ROOT, name))


def _ensure_configured():
    global _configured
    if _configured:
        return
    _configured = True
    logger = logging.getLogger(ROOT)
    if logger.handlers:
        return
    level_name = os.environ.get("LAP_AUTOBAKE_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False


__all__ = ["ROOT", "get_logger"]
