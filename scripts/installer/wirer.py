"""Blender wirer for the Auto-bake provider plugin.

Two integration points, both inside Blender's per-user config directory:

* ``scripts/addons/LAP_AutoBake/__init__.py`` — our own add-on shell. The
  name is **not** ``LAP_Bridge``: that module belongs to the Blender provider
  plugin, and two plugins sharing one add-on would overwrite each other's
  ``register()`` and collapse to a single agent.
* ``scripts/startup/lap_autobake_bootstrap.py`` — defers
  ``addon_utils.enable`` until after Blender has finished booting. The name is
  distinct from the provider's ``lap_bootstrap.py`` for the same reason.

Deliberately **not** written: ``%LOCALAPPDATA%\\LAP\\dcc-whitelist.d\\*.json``.
That drop-in is the Tool Server's per-DCC inventory of installed DCC
*executables* (``blender.exe`` and its registry keys), keyed by
``provider_type``. This plugin is not a DCC — Blender is — so its entries
belong to whoever owns ``blender.json``, which is the Blender provider plugin
and rewrites that file from its own wired versions. Writing a second file
here would either be dead weight or, worse, a conflicting duplicate of the
same Blender installs under a provider type that has no executable. Not
touching it is the only option that cannot corrupt the other plugin's state.

Contract version 2: ``install_hook`` / ``uninstall_hook`` accept
``wired_startups`` so a partial unwire is idempotent. stdlib-only; no import
of ``lap_plugin_devkit`` and none of this plugin's own modules.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

PROVIDER_TYPE: str = "autobake"
CONTRACT_VERSION: int = 2

#: Blender releases this plugin is built and tested against. Kept in step
#: with ``config/plugin.toml`` ``supported_provider_versions`` — a version
#: that is missing here can be requested with ``--wire`` but is refused,
#: rather than being wired into a host whose Cycles bake API differs.
SUPPORTED_BLENDER_VERSIONS: tuple = ("4.2", "4.5", "5.0", "5.2", "5.3")

_ADDON_MODULE_NAME = "LAP_AutoBake"
_STARTUP_SHIM_NAME = "lap_autobake_bootstrap.py"
_USERPREF_CLEANUP_NAME = "lap_autobake_unwire_userpref.py"

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_ADDON_TEMPLATE = _TEMPLATE_DIR / "addon" / "__init__.py.tmpl"
_STARTUP_TEMPLATE = _TEMPLATE_DIR / "startup" / "lap_bootstrap.py.tmpl"
_USERPREF_TEMPLATE = _TEMPLATE_DIR / "startup" / "lap_unwire_userpref.py.tmpl"

_SENTINEL_INSTALL_ROOT = "__LAP_INSTALL_ROOT__"
_SENTINEL_PLUGIN_SLUG = "__LAP_PLUGIN_SLUG__"
_SENTINEL_PLUGIN_ID = "__LAP_PLUGIN_ID__"

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")
_PROGRAM_DIR_RE = re.compile(r"^Blender\s+(\d+\.\d+)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# version helpers
# ---------------------------------------------------------------------------


def _normalize_version(token):
    match = _VERSION_RE.match(str(token).strip())
    if not match:
        raise ValueError(
            "--wire: {0!r} is not a Blender marketing version (expected e.g. 4.2)".format(
                token
            )
        )
    return "{0}.{1}".format(int(match.group(1)), int(match.group(2)))


def _supported(version):
    return version in SUPPORTED_BLENDER_VERSIONS


def _sort_key(version):
    return tuple(int(part) for part in version.split("."))


# ---------------------------------------------------------------------------
# host discovery
# ---------------------------------------------------------------------------


def _user_root():
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "Blender Foundation" / "Blender" if appdata else None


def _program_root():
    program_files = os.environ.get("PROGRAMFILES") or os.environ.get("ProgramFiles")
    return Path(program_files) / "Blender Foundation" if program_files else None


def _installed_versions():
    """Blender versions this machine has been launched at least once."""
    found = set()
    user_root = _user_root()
    if user_root is not None and user_root.is_dir():
        for child in user_root.iterdir():
            if not child.is_dir():
                continue
            try:
                version = _normalize_version(child.name)
            except ValueError:
                continue
            if _supported(version):
                found.add(version)
    program_root = _program_root()
    if program_root is not None and program_root.is_dir():
        for child in program_root.iterdir():
            match = _PROGRAM_DIR_RE.match(child.name)
            if not match:
                continue
            try:
                version = _normalize_version(match.group(1))
            except ValueError:
                continue
            if _supported(version):
                found.add(version)
    return sorted(found, key=_sort_key, reverse=True)


def _user_version_dir(version):
    root = _user_root()
    if root is None:
        return None
    candidate = root / version
    return candidate if candidate.is_dir() else None


def _blender_executable(version):
    program_root = _program_root()
    if program_root is None:
        return None
    exe_name = "blender.exe" if sys.platform == "win32" else "blender"
    candidate = program_root / "Blender {0}".format(version) / exe_name
    return candidate if candidate.is_file() else None


def _plugin_slug_from_install_dir(install_dir):
    """Read ``plugin_slug`` out of ``<install_dir>/plugin.toml``."""
    toml = Path(install_dir) / "plugin.toml"
    if not toml.is_file():
        return ""
    try:
        for raw in toml.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() != "plugin_slug":
                continue
            value = value.strip()
            if value[:1] in ("'", '"') and value.endswith(value[0]) and len(value) >= 2:
                return value[1:-1]
            return value
    except OSError:
        return ""
    return ""


def _render(template_path, install_dir, plugin_id):
    text = template_path.read_text(encoding="utf-8")
    return (
        text.replace(_SENTINEL_INSTALL_ROOT, str(install_dir))
        .replace(_SENTINEL_PLUGIN_SLUG, _plugin_slug_from_install_dir(install_dir))
        .replace(_SENTINEL_PLUGIN_ID, plugin_id)
    )


# ---------------------------------------------------------------------------
# userpref hygiene
# ---------------------------------------------------------------------------


def _write_userpref_cleanup(startup_dir):
    startup_dir.mkdir(parents=True, exist_ok=True)
    (startup_dir / _USERPREF_CLEANUP_NAME).write_text(
        _USERPREF_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )


def _remove_userpref_cleanup(startup_dir):
    try:
        (startup_dir / _USERPREF_CLEANUP_NAME).unlink()
    except OSError:
        pass


def _clear_userpref(version):
    """Best effort: drop our add-on from userpref so the next launch is clean."""
    if not version:
        return
    exe = _blender_executable(version)
    if exe is None:
        return
    expr = (
        "import addon_utils, bpy\n"
        "addon_utils.disable({0!r}, default_set=True)\n"
        "bpy.ops.wm.save_userpref()\n".format(_ADDON_MODULE_NAME)
    )
    kwargs = {"timeout": 90, "capture_output": True, "check": False}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run([str(exe), "--background", "--python-expr", expr], **kwargs)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# wirer
# ---------------------------------------------------------------------------


class Wirer:
    provider_type: str = PROVIDER_TYPE
    contract_version: int = CONTRACT_VERSION

    def parse_versions(self, arg):
        if arg is None:
            return []
        token = str(arg).strip()
        if not token or token.lower() == "none":
            return []
        if token.lower() == "auto":
            detected = _installed_versions()
            if not detected:
                raise ValueError(
                    "--wire auto found no supported Blender under {0} or {1}".format(
                        _user_root(), _program_root()
                    )
                )
            return list(detected)
        versions = []
        for chunk in token.split(","):
            piece = chunk.strip()
            if not piece:
                continue
            version = _normalize_version(piece)
            if not _supported(version):
                raise ValueError(
                    "--wire: {0} is not supported (supported: {1})".format(
                        version, ", ".join(SUPPORTED_BLENDER_VERSIONS)
                    )
                )
            versions.append(version)
        return sorted(set(versions), key=_sort_key, reverse=True)

    def wire(self, install_dir, version, *, force=False):
        """Plant this plugin's add-on module and startup shim for *version*."""
        del force
        install_dir = Path(install_dir)
        version = _normalize_version(str(version))
        if not _supported(version):
            raise ValueError("wire(): {0} is not a supported Blender version".format(version))
        user_dir = _user_version_dir(version)
        if user_dir is None:
            raise FileNotFoundError(
                "Blender {0} has no user config directory yet — launch that "
                "Blender once so %APPDATA%\\Blender Foundation\\Blender\\{0}\\ "
                "exists, then wire again".format(version)
            )

        addon_dir = user_dir / "scripts" / "addons" / _ADDON_MODULE_NAME
        startup_dir = user_dir / "scripts" / "startup"
        plugin_id = "AUTOBAKE{0}".format(version)

        addon_dir.mkdir(parents=True, exist_ok=True)
        startup_dir.mkdir(parents=True, exist_ok=True)
        (addon_dir / "__init__.py").write_text(
            _render(_ADDON_TEMPLATE, install_dir, plugin_id), encoding="utf-8"
        )
        shim = startup_dir / _STARTUP_SHIM_NAME
        shim.write_text(_render(_STARTUP_TEMPLATE, install_dir, plugin_id), encoding="utf-8")
        _remove_userpref_cleanup(startup_dir)

        return {
            "provider_type": PROVIDER_TYPE,
            "version": version,
            "plugin_id": plugin_id,
            "plugin_slug": _plugin_slug_from_install_dir(install_dir),
            "integration": "addon",
            "addons_dir": str(user_dir / "scripts" / "addons"),
            "addon_target": str(addon_dir),
            "startup_dir": str(startup_dir),
            "shim_py": str(shim),
            "addon_init": str(addon_dir / "__init__.py"),
        }

    def unwire(self, record):
        """Remove what :meth:`wire` planted. Never touches another plugin."""
        if not isinstance(record, dict):
            return
        target = record.get("addon_target")
        if target:
            shutil.rmtree(target, ignore_errors=True)
        for key in ("shim_py", "addon_init"):
            raw = record.get(key)
            if not raw:
                continue
            try:
                Path(raw).unlink()
            except OSError:
                pass
        startup_dir = record.get("startup_dir")
        if startup_dir:
            try:
                _write_userpref_cleanup(Path(startup_dir))
            except OSError:
                pass
        _clear_userpref(record.get("version"))

    def install_hook(self, install_root, wired_startups=None):
        """No per-DCC inventory is written — see the module docstring.

        ``wired_startups`` is accepted for contract v2 parity and ignored: the
        Tool Server's ``dcc-whitelist.d`` describes DCC executables, and
        Blender's entry is owned by the Blender provider plugin.
        """
        del install_root, wired_startups
        return None

    def uninstall_hook(self, install_root, wired_startups=None):
        """Also a no-op: the whitelist drop-in is not ours to shrink."""
        del install_root, wired_startups
        return None

    def agent_abi_tag(self, version):
        """PEP 425 tag of this Blender's embedded CPython.

        Matches ``config/plugin.toml`` ``harden_python_versions``:
        4.2–5.0 → cp311, 5.1+ → cp313.
        """
        try:
            normalized = _normalize_version(str(version).strip())
        except ValueError:
            return None
        if not _supported(normalized):
            return None
        major, minor = _sort_key(normalized)
        if (major, minor) <= (5, 0):
            return "cp311"
        return "cp313"


__all__ = ["SUPPORTED_BLENDER_VERSIONS", "Wirer"]
