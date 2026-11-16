"""Wirer behaviour: add-on planting, isolation from the Blender provider, unwire.

The isolation assertions are the point of this file. The Blender provider
writes ``LAP_Bridge`` plus ``lap_bootstrap.py`` plus
``dcc-whitelist.d/blender.json``; if this plugin wrote any of those it would
break the other plugin's wiring or claim to be a DCC it is not.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "installer"))

import wirer as wirer_module  # noqa: E402


def _make_layout(tmp_path, monkeypatch, versions=("5.2",)):
    """Fake ``%APPDATA%\\Blender Foundation\\Blender\\<v>`` trees."""
    appdata = tmp_path / "appdata"
    blender_root = appdata / "Blender Foundation" / "Blender"
    for version in versions:
        (blender_root / version / "scripts").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "no-program-files"))
    return blender_root


def _make_install(tmp_path):
    root = tmp_path / "install" / "0.1.0"
    (root / "agent").mkdir(parents=True)
    (root / "plugin.toml").write_text(
        'plugin_slug = "dcc-plugin-autobake"\nprovider_type = "autobake"\n',
        encoding="utf-8",
    )
    return root


def test_parse_versions_auto_finds_supported_blender(tmp_path, monkeypatch):
    _make_layout(tmp_path, monkeypatch, versions=("4.2", "5.2", "9.9"))
    wirer = wirer_module.Wirer()
    assert wirer.parse_versions("auto") == ["5.2", "4.2"]


def test_parse_versions_rejects_an_unsupported_version():
    with pytest.raises(ValueError):
        wirer_module.Wirer().parse_versions("3.6")


def test_parse_versions_none_is_empty():
    assert wirer_module.Wirer().parse_versions("none") == []


def test_wire_plants_its_own_addon_and_shim(tmp_path, monkeypatch):
    blender_root = _make_layout(tmp_path, monkeypatch)
    install = _make_install(tmp_path)
    record = wirer_module.Wirer().wire(install, "5.2")

    addon_init = Path(record["addon_init"])
    shim = Path(record["shim_py"])
    assert addon_init == blender_root / "5.2" / "scripts" / "addons" / "LAP_AutoBake" / "__init__.py"
    assert shim == blender_root / "5.2" / "scripts" / "startup" / "lap_autobake_bootstrap.py"
    assert addon_init.is_file()
    assert shim.is_file()
    assert record["provider_type"] == "autobake"
    assert record["plugin_id"] == "AUTOBAKE5.2"


def test_wire_does_not_touch_the_blender_providers_files(tmp_path, monkeypatch):
    blender_root = _make_layout(tmp_path, monkeypatch)
    version_scripts = blender_root / "5.2" / "scripts"
    provider_addon = version_scripts / "addons" / "LAP_Bridge" / "__init__.py"
    provider_addon.parent.mkdir(parents=True)
    provider_addon.write_text("# the Blender provider's shell\n", encoding="utf-8")
    provider_shim = version_scripts / "startup" / "lap_bootstrap.py"
    provider_shim.parent.mkdir(parents=True, exist_ok=True)
    provider_shim.write_text("# the Blender provider's shim\n", encoding="utf-8")

    install = _make_install(tmp_path)
    wirer_module.Wirer().wire(install, "5.2")

    assert provider_addon.read_text(encoding="utf-8") == "# the Blender provider's shell\n"
    assert provider_shim.read_text(encoding="utf-8") == "# the Blender provider's shim\n"


def test_install_hook_writes_no_whitelist_dropin(tmp_path, monkeypatch):
    """The Tool Server's whitelist describes DCC executables; Blender owns it."""
    local_appdata = tmp_path / "localappdata"
    monkeypatch.setenv("LOCALAPPDATA", str(local_appdata))
    install = _make_install(tmp_path)
    wirer = wirer_module.Wirer()

    wirer.install_hook(install, [{"version": "5.2"}])
    assert not (local_appdata / "LAP" / "dcc-whitelist.d").exists()

    wirer.uninstall_hook(install, [])
    assert not (local_appdata / "LAP" / "dcc-whitelist.d").exists()


def test_unwire_removes_only_our_files(tmp_path, monkeypatch):
    blender_root = _make_layout(tmp_path, monkeypatch)
    version_scripts = blender_root / "5.2" / "scripts"
    provider_addon = version_scripts / "addons" / "LAP_Bridge" / "__init__.py"
    provider_addon.parent.mkdir(parents=True)
    provider_addon.write_text("# provider\n", encoding="utf-8")

    install = _make_install(tmp_path)
    wirer = wirer_module.Wirer()
    record = wirer.wire(install, "5.2")
    wirer.unwire(record)

    assert not Path(record["addon_target"]).exists()
    assert not Path(record["shim_py"]).exists()
    assert provider_addon.is_file(), "unwiring must not delete the Blender provider"


def test_unwire_writes_a_userpref_cleanup_for_our_addon(tmp_path, monkeypatch):
    blender_root = _make_layout(tmp_path, monkeypatch)
    install = _make_install(tmp_path)
    wirer = wirer_module.Wirer()
    record = wirer.wire(install, "5.2")
    wirer.unwire(record)

    cleanup = blender_root / "5.2" / "scripts" / "startup" / "lap_autobake_unwire_userpref.py"
    assert cleanup.is_file()
    text = cleanup.read_text(encoding="utf-8")
    assert '_ADDON_MODULE = "LAP_AutoBake"' in text
    # The cleanup must only disable *our* add-on. A `LAP_Bridge` string in the
    # executable code would risk disabling the Blender provider.
    import ast

    code = "\n".join(
        ast.unparse(node)
        for node in ast.parse(text).body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    )
    assert "LAP_Bridge" not in code


def test_wire_is_idempotent(tmp_path, monkeypatch):
    _make_layout(tmp_path, monkeypatch)
    install = _make_install(tmp_path)
    wirer = wirer_module.Wirer()
    first = wirer.wire(install, "5.2")
    second = wirer.wire(install, "5.2")
    assert Path(first["addon_target"]).is_dir()
    assert first["plugin_id"] == second["plugin_id"]


def test_wire_requires_a_launched_blender(tmp_path, monkeypatch):
    _make_layout(tmp_path, monkeypatch, versions=())
    install = _make_install(tmp_path)
    with pytest.raises(FileNotFoundError):
        wirer_module.Wirer().wire(install, "5.2")


def test_rendered_addon_points_at_this_install(tmp_path, monkeypatch):
    _make_layout(tmp_path, monkeypatch)
    install = _make_install(tmp_path)
    record = wirer_module.Wirer().wire(install, "5.2")
    text = Path(record["addon_init"]).read_text(encoding="utf-8")

    assert str(install) in text
    assert "dcc-plugin-autobake" in text
    assert 'bl_info = {' in text
    assert '"name": "LAP_AutoBake"' in text
    assert "__LAP_INSTALL_ROOT__" not in text
    assert "__LAP_PLUGIN_SLUG__" not in text
    assert "__LAP_PLUGIN_ID__" not in text


def test_rendered_addon_has_no_reference_to_the_provider_addon():
    """Only executable references matter; the docstring explains the choice."""
    import ast

    template = (
        ROOT / "scripts" / "installer" / "templates" / "addon" / "__init__.py.tmpl"
    )
    source = template.read_text(encoding="utf-8")
    module = ast.parse(source)
    # Drop the module docstring, then re-serialise what is left.
    body = [
        node
        for node in module.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    ]
    code = "\n".join(ast.unparse(node) for node in body)
    assert "LAP_Bridge" not in code
    assert "_ADDON_MODULE_NAME" not in code


def test_addon_shell_starts_our_agent_not_the_providers(tmp_path, monkeypatch):
    _make_layout(tmp_path, monkeypatch)
    install = _make_install(tmp_path)
    record = wirer_module.Wirer().wire(install, "5.2")
    text = Path(record["addon_init"]).read_text(encoding="utf-8")
    assert "from dcc_plugin_autobake.agent import start" in text
    assert "dcc_plugin_blender" not in text


def test_agent_abi_tags_match_the_build_matrix():
    wirer = wirer_module.Wirer()
    assert wirer.agent_abi_tag("4.2") == "cp311"
    assert wirer.agent_abi_tag("5.0") == "cp311"
    assert wirer.agent_abi_tag("5.2") == "cp313"
    assert wirer.agent_abi_tag("5.3") == "cp313"
    assert wirer.agent_abi_tag("9.9") is None


def test_wirer_declares_autobake_and_contract_v2():
    wirer = wirer_module.Wirer()
    assert wirer.provider_type == "autobake"
    assert wirer.contract_version == 2


def test_wirer_is_stdlib_only():
    """No third-party import in the executable part of the wirer."""
    import ast

    source = (ROOT / "scripts" / "installer" / "wirer.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    allowed = {
        "__future__",
        "os",
        "re",
        "shutil",
        "subprocess",
        "sys",
        "pathlib",
        "typing",
    }
    imported = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= allowed, imported - allowed

    code = "\n".join(
        ast.unparse(node)
        for node in tree.body
        if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
    )
    assert "dcc_plugin_autobake" not in code
    assert "import bpy" not in code
    assert "lap_plugin_devkit" not in code
