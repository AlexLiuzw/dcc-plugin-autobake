"""Static check: every ``_addon.X`` / ``_ui.X`` attribute actually exists.

The 0.1.2 boot test found that ``_orchestrator`` still imported a name
(``registration_owner``) that the UI redesign had deleted. Importing the module
did not catch it, because the reference sat inside a function body — it only
fails when that function runs, which in production means "on the user's
machine, during boot".

An import-based test cannot see that class of defect, so this walks the AST and
resolves every attribute reference against the real module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from dcc_plugin_autobake.agent import _addon, _ui

AGENT_ROOT = Path(__file__).resolve().parents[1] / "src" / "dcc_plugin_autobake"

#: Modules whose attributes are checked, and the module object to check against.
WATCHED = {
    "_addon": _addon,
    "_ui": _ui,
}


def _python_files():
    for path in AGENT_ROOT.rglob("*.py"):
        if "vendor" in path.parts:
            continue  # third-party, byte-identical, not ours to inspect
        yield path


def _references(path, module_names):
    """Yield ``(lineno, module_alias, attribute)`` for ``name.attr`` reads."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id in module_names:
            found.append((node.lineno, value.id, node.attr))
    return found


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: p.name)
def test_watched_module_attributes_exist(path):
    """Every ``_addon.foo`` / ``_ui.foo`` in the packaged source must resolve."""
    missing = []
    for lineno, alias, attribute in _references(path, set(WATCHED)):
        module = WATCHED[alias]
        if not hasattr(module, attribute):
            missing.append(
                "{0}:{1} {2}.{3} does not exist".format(
                    path.name, lineno, alias, attribute
                )
            )
    assert not missing, missing


@pytest.mark.parametrize("path", list(_python_files()), ids=lambda p: p.name)
def test_from_imports_of_watched_modules_resolve(path):
    """``from ._addon import x`` must name something the module exports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        target = WATCHED.get(node.module or "")
        if target is None:
            continue
        for alias in node.names:
            if not hasattr(target, alias.name):
                missing.append(
                    "{0}:{1} from {2} import {3} does not exist".format(
                        path.name, node.lineno, node.module, alias.name
                    )
                )
    assert not missing, missing


def test_no_module_references_the_removed_registration_owner():
    """The deleted API must not linger anywhere in the package."""
    offenders = []
    for path in _python_files():
        for lineno, alias, attribute in _references(path, set(WATCHED)):
            if attribute in ("registration_owner", "OWNER_THIS_PLUGIN", "OWNER_OTHER_COPY",
                             "OWNER_NONE", "PANEL_CLASS_NAME"):
                offenders.append("{0}:{1} {2}.{3}".format(path.name, lineno, alias, attribute))
    assert not offenders, offenders
