"""plugin.toml / pyproject / package must agree on identity and versions.

A drift here is the failure mode that produced the earlier wrong-shaped
candidate: tools registered under the host provider, or a package whose
install tree collided with the Blender provider's.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOML_PATH = ROOT / "config" / "plugin.toml"
PYPROJECT = ROOT / "pyproject.toml"


def _read(path):
    return path.read_text(encoding="utf-8")


def _value(text, key):
    match = re.search(r"^{0}\s*=\s*(.+)$".format(re.escape(key)), text, re.MULTILINE)
    assert match, "missing {0}".format(key)
    return match.group(1).strip()


def test_provider_type_is_autobake_not_blender():
    text = _read(TOML_PATH)
    assert _value(text, "provider_type") == '"autobake"'


def test_plugin_slug_is_the_independent_repo_name():
    text = _read(TOML_PATH)
    assert _value(text, "plugin_slug") == '"dcc-plugin-autobake"'


def test_install_tree_cannot_collide_with_the_blender_provider():
    """The slug is what places the version dir under ``<provider>-provider``."""
    text = _read(TOML_PATH)
    slug = _value(text, "plugin_slug").strip('"')
    assert slug != "dcc-plugin-blender"
    provider = _value(text, "provider_type").strip('"')
    assert provider != "blender"


def test_no_auto_bake_tool_package_is_declared_for_the_sidecar():
    """The tools are agent leaves; listing them here would advertise them twice."""
    text = _read(TOML_PATH)
    match = re.search(r"^tool_packages\s*=\s*\[(.*?)\]", text, re.MULTILINE | re.DOTALL)
    assert match is not None
    assert match.group(1).strip() == ""


def test_supported_versions_match_the_wirer_table():
    text = _read(TOML_PATH)
    declared = re.findall(r'"(\d+\.\d+)"', _value(text, "supported_provider_versions"))
    wirer = _read(ROOT / "scripts" / "installer" / "wirer.py")
    table = re.search(r"SUPPORTED_BLENDER_VERSIONS: tuple = \(([^)]*)\)", wirer)
    assert table is not None
    wired = re.findall(r'"(\d+\.\d+)"', table.group(1))
    assert sorted(declared) == sorted(wired), (declared, wired)


def test_dangerous_capabilities_are_the_narrow_set():
    text = _read(TOML_PATH)
    capabilities = _value(text, "dangerous_capabilities")
    assert "filesystem_write" in capabilities
    assert "run_script" not in capabilities, (
        "this plugin exposes no script-execution tool; declaring run_script "
        "would widen the host's permission surface for nothing"
    )


def test_sidecar_binary_lives_under_its_own_bundle_directory():
    text = _read(TOML_PATH)
    binary = _value(text, "binary").strip('"')
    assert binary.startswith("sidecar/lap-autobake-sidecar/")
    assert binary.endswith("lap-autobake-sidecar.exe")


def test_package_name_matches_the_python_package():
    text = _read(TOML_PATH)
    assert _value(text, "package") == '"dcc_plugin_autobake"'
    assert (ROOT / "src" / "dcc_plugin_autobake" / "__init__.py").is_file()


def test_pyproject_declares_the_same_name_and_version():
    toml_text = _read(TOML_PATH)
    pyproject = _read(PYPROJECT)
    assert 'name = "dcc-plugin-autobake"' in pyproject
    version = _value(toml_text, "plugin_version").strip('"')
    assert 'version = "{0}"'.format(version) in pyproject


def test_agent_has_no_third_party_imports():
    """The agent runs in Blender's embedded Python, which has no pip packages."""
    stdlib = {
        "__future__",
        "collections",
        "ctypes",
        "importlib",
        "io",
        "json",
        "keyword",
        "logging",
        "os",
        "re",
        "struct",
        "subprocess",
        "sys",
        "threading",
        "time",
        "traceback",
        "types",
        "zlib",
    }
    host_modules = {"bpy", "gpu_extras", "addon_utils"}
    # Only module-level statements count: `import bpy` inside a function is a
    # deliberate late import and must stay allowed.
    import ast

    offenders = []
    for path in (ROOT / "src" / "dcc_plugin_autobake" / "agent").rglob("*.py"):
        if "vendor" in path.parts:
            continue
        tree = ast.parse(_read(path), filename=str(path))
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import inside the package
                    continue
                names = [node.module or ""]
            for name in names:
                root = name.split(".")[0]
                if root in stdlib or root in host_modules:
                    continue
                if root == "dcc_plugin_autobake":
                    continue
                offenders.append("{0}:{1} {2}".format(path.name, node.lineno, name))
    assert not offenders, offenders


def test_agent_entry_point_rejects_a_blender_provider_toml(tmp_path):
    """A mis-generated install must fail loudly, not register tools on BLENDER."""
    from dcc_plugin_autobake.agent import AgentBootError, load_config

    wrong = tmp_path / "plugin.toml"
    wrong.write_text('provider_type = "blender"\nplugin_version = "0.3.1"\n', encoding="utf-8")
    try:
        load_config(str(wrong))
    except AgentBootError as exc:
        assert "autobake" in str(exc)
    else:  # pragma: no cover - the assertion above is the point
        raise AssertionError("a blender-provider plugin.toml was accepted")


def test_agent_entry_point_accepts_the_real_toml():
    from dcc_plugin_autobake.agent import load_config

    config = load_config(str(TOML_PATH))
    assert config.provider_type == "autobake"
    assert config.plugin_slug == "dcc-plugin-autobake"
    assert config.topology == "sidecar"
    assert config.supported_versions
