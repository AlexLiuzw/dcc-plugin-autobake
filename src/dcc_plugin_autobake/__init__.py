"""``dcc_plugin_autobake`` — LAP DCC provider plugin for Auto-bake Tools.

The provider identity is ``autobake``, not ``blender``: auto-bake is a plugin
of its own that happens to run inside Blender. Its tools are
``autobake.autobake.*``, its install tree is
``%APPDATA%\\LAP\\autobake-provider\\dcc-plugin-blender-autobake\\<version>``, and
nothing in it is shared with the Blender provider plugin.

Two processes import this package, and only one of them gets far enough to
matter:

* **sidecar** (``lap-autobake-sidecar.exe``) — imports nothing from here. The
  four tools are agent leaf tools, so ``plugin.toml`` declares no
  ``tool_packages`` and no ``sidecar_local_tool_packages``; the sidecar builds
  its whole registry from the agent's HELLO. That is deliberate: a sidecar-side
  tool would be one that never touches Blender, and this plugin has none.
* **agent** (Blender's embedded Python) — imports
  :mod:`dcc_plugin_autobake.agent` only. That subtree is stdlib-only, so this
  module must stay import-free at the top level: even a guarded
  ``lap_plugin_runtime`` import would make ``import dcc_plugin_autobake`` fail
  inside Blender if the guard ever mis-fired.
"""

from __future__ import annotations

__version__ = "0.1.1"

#: Provider type every wire tool of this plugin is prefixed with.
PROVIDER_TYPE = "autobake"

#: Skill slug shared by all four leaf tools.
SKILL_SLUG = "autobake"

#: Three-segment wire names, in documented order.
TOOL_NAMES = (
    "autobake.autobake.create_environment",
    "autobake.autobake.bake_normal",
    "autobake.autobake.bake_ao",
    "autobake.autobake.status",
)

__all__ = ["PROVIDER_TYPE", "SKILL_SLUG", "TOOL_NAMES", "__version__"]
