"""Register the vendored Auto-bake Tools add-on in the host, safely.

The vendored tree is byte-identical to the upstream archive (see
``vendor/autobake_tools/VENDOR-MANIFEST.json``), which means two things this
module has to deal with:

1. **Upstream is GUI-era code.** Its two bake operators start an
   ``INVOKE_DEFAULT`` modal bake and open a new window from a timer, and
   ``create_render_env_n`` reads two Cycles knobs (``feature_set``,
   ``use_bake_multires``) that Blender removed in 5.0. The operators stay
   registered for a human working in the viewport; the leaf tools bypass the
   modal wrappers (see :mod:`..tools.autobake`), and
   :func:`_install_render_env_guard` routes the shared helper through a
   version-tolerant copy without touching the vendored files.
2. **Another copy may already be registered.** The Blender provider plugin
   used to vendor this same add-on, so both plugins can be loaded into one
   Blender process. ``bpy.utils.register_class`` fails on a duplicate id, so
   :func:`ensure_registered` detects that case and *adopts* the existing
   registration instead of raising — and then deliberately never unregisters
   classes it does not own.

``registration_owner()`` reports which of the two happened, so an acceptance
run can tell "this plugin registered it" from "someone else already had it".
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

ADDON_MODULE = "autobake_tools"
ADDON_VERSION = (1, 1, 5)
ADDON_LICENSE = "Apache-2.0"
ADDON_COPYRIGHT = "Copyright 2023 Igor Subachev (GarikDog)"

OPERATOR_CREATE_ENV = "at.create_environment"
OPERATOR_BAKE = "at.bake"
OPERATOR_BAKE_AO = "at.bake_ao"
OPERATORS = (OPERATOR_CREATE_ENV, OPERATOR_BAKE, OPERATOR_BAKE_AO)

PANEL_CLASS_NAME = "VIEW3D_PT_Autobake"
PANEL_IDNAME = "VIEW3D_PT_Atobake"
PANEL_LABEL = "Auto-bake Tools"

#: Per-object marker the upstream bake operators poll for.
PREPARED_ATTR = "prepare_statement_prop_bool"

OWNER_THIS_PLUGIN = "this-plugin"
OWNER_OTHER_COPY = "other-copy"
OWNER_NONE = "none"

_lock = threading.Lock()
_owner = OWNER_NONE
_detail = "not attempted"
_guard_installed = False
_guard_originals = None


def _vendored():
    from .vendor import autobake_tools

    return autobake_tools


def _utils():
    from .vendor.autobake_tools.addon.utility import at_utils

    return at_utils


def registration_owner():
    """One of ``this-plugin`` / ``other-copy`` / ``none``."""
    return _owner


def registration_detail():
    """Human-readable reason for the current state."""
    return _detail


def is_registered():
    return _owner != OWNER_NONE


def ensure_registered(bpy=None):
    """Register the vendored add-on in *bpy*'s host. Idempotent.

    Must run on Blender's main thread: ``register()`` performs RNA writes.
    Returns the vendored add-on module.
    """
    global _owner, _detail
    with _lock:
        if _owner != OWNER_NONE:
            return _vendored()

        addon = _vendored()
        try:
            addon.register()
        except Exception as exc:  # noqa: BLE001
            if _looks_like_duplicate(exc) and _host_already_has_autobake(bpy):
                # Another copy of the same add-on owns the classes (the
                # Blender provider plugin vendors an identical tree). Adopt
                # it: the operators and the panel are already the ones a
                # human sees, and re-registering would only raise again.
                _owner = OWNER_OTHER_COPY
                _detail = "adopted existing registration ({0}: {1})".format(
                    type(exc).__name__, exc
                )
                log.info("auto-bake add-on already registered in this host: %s", _detail)
                _install_render_env_guard(bpy)
                return addon
            # A genuine failure (missing host module, RNA error) must surface:
            # pretending to be registered would turn every later tool call
            # into a confusing "Scene.at_tool is missing".
            _detail = "{0}: {1}".format(type(exc).__name__, exc)
            raise

        _owner = OWNER_THIS_PLUGIN
        _detail = "registered"
        _install_render_env_guard(bpy)
        return addon


def unregister(bpy=None):
    """Undo our own registration. Never touches another copy's classes."""
    global _owner, _detail
    with _lock:
        if _owner != OWNER_THIS_PLUGIN:
            # Either nothing is registered, or someone else owns the classes
            # and removing them would break that plugin's panel.
            return False
        addon = _vendored()
        try:
            addon.unregister()
        finally:
            _remove_render_env_guard()
            _owner = OWNER_NONE
            _detail = "unregistered"
    return True


def _host_already_has_autobake(bpy):
    """True when the operator ids and the scene property group are present."""
    if bpy is None:
        try:
            import bpy as _bpy  # type: ignore[import-not-found]
        except ImportError:
            return False
        bpy = _bpy
    ops_at = getattr(getattr(bpy, "ops", None), "at", None)
    if ops_at is None:
        return False
    if not all(hasattr(ops_at, name.partition(".")[2]) for name in OPERATORS):
        return False
    scene_type = getattr(getattr(bpy, "types", None), "Scene", None)
    return scene_type is not None and hasattr(scene_type, "at_tool")


#: Substrings Blender uses when an RNA class or operator id is already taken.
_DUPLICATE_MARKERS = (
    "already registered",
    "register class",
    "is already in use",
)


def _looks_like_duplicate(exc):
    """True when *exc* is Blender refusing a second registration.

    Only this case may be adopted. Any other failure (a missing host module,
    a malformed property) has to reach the caller: an adapter that swallows
    it would leave the plugin looking healthy while every tool failed.
    """
    text = "{0}: {1}".format(type(exc).__name__, exc).lower()
    return any(marker in text for marker in _DUPLICATE_MARKERS)


def reset_for_tests():
    """Forget cached state without touching the host (test seam)."""
    global _owner, _detail, _guard_installed, _guard_originals
    with _lock:
        _owner = OWNER_NONE
        _detail = "not attempted"
        _guard_installed = False
        _guard_originals = None


# ---------------------------------------------------------------------------
# 5.x compatibility guard for the shared render-environment helper
# ---------------------------------------------------------------------------

#: The two host attributes upstream assumes and Blender 5.x dropped. Both are
#: probed per host instead of keyed off a version number, because the exact
#: release that dropped them differs from the release that re-adds a
#: replacement.
_LEGACY_KNOBS = ("feature_set", "use_bake_multires")


def legacy_knobs_present(scene):
    """Map each legacy knob to whether *scene* still exposes it."""
    cycles = getattr(scene, "cycles", None)
    render = getattr(scene, "render", None)
    return {
        "feature_set": hasattr(cycles, "feature_set"),
        "use_bake_multires": hasattr(render, "use_bake_multires"),
    }


def missing_legacy_knobs(scene):
    """Sorted names of legacy knobs this host lacks (empty on 4.2 / 4.5)."""
    present = legacy_knobs_present(scene)
    return sorted(name for name, exists in present.items() if not exists)


def create_render_env(bpy):
    """``at_utils.create_render_env_n``, tolerant of missing 5.x knobs.

    Same sequence, same order as upstream. Only the setters whose attribute
    is absent on this host are skipped, so on 4.2 / 4.5 the effect is
    identical to the vendored helper.
    """
    scene = bpy.context.scene
    settings = getattr(scene, "at_tool", None)
    render = scene.render
    cycles = getattr(scene, "cycles", None)
    present = legacy_knobs_present(scene)

    if getattr(render, "engine", None) != "CYCLES":
        render.engine = "CYCLES"
    if present["feature_set"]:
        if cycles.feature_set != "SUPPORTED":
            cycles.feature_set = "SUPPORTED"
    if cycles is not None:
        try:
            if getattr(cycles, "device", None) != "GPU":
                cycles.device = "GPU"
        except Exception:  # noqa: BLE001 - device enum differs per build
            log.debug("could not switch Cycles to GPU", exc_info=True)
    if present["use_bake_multires"]:
        if render.use_bake_multires is not False:
            render.use_bake_multires = False
    if cycles is not None and getattr(cycles, "bake_type", None) != "NORMAL":
        cycles.bake_type = "NORMAL"
    bake = getattr(render, "bake", None)
    if bake is not None and getattr(bake, "use_selected_to_active", None) is not False:
        bake.use_selected_to_active = False
    # Upstream re-derives the DirectX flag from the current normal_g; keep
    # that behaviour so a caller's explicit choice is applied afterwards.
    if settings is not None and bake is not None:
        normal_g = getattr(bake, "normal_g", None)
        if normal_g == "NEG_Y":
            settings.at_dx_normal = True
        elif normal_g == "POS_Y":
            settings.at_dx_normal = False


def _guard_targets():
    """Modules holding their own reference to ``create_render_env_n``.

    ``at_utils`` defines it, but ``at_create_env`` (the operator behind the
    panel button) imported it by name, so patching only ``at_utils`` would
    leave the button running the unguarded original.
    """
    from .vendor.autobake_tools.addon.operator import at_create_env
    from .vendor.autobake_tools.addon.utility import at_utils

    return ((at_utils, "create_render_env_n"), (at_create_env, "create_render_env_n"))


def _install_render_env_guard(bpy=None):
    """Route both entry points through :func:`create_render_env`.

    A no-op on hosts that still have both legacy knobs: upstream is correct
    there and is left byte-identical and untouched. Returns whether the
    guard is installed.
    """
    global _guard_installed, _guard_originals
    if _guard_installed:
        return True
    if bpy is None:
        try:
            import bpy as _bpy  # type: ignore[import-not-found]
        except ImportError:
            return False
        bpy = _bpy
    try:
        present = legacy_knobs_present(bpy.context.scene)
    except Exception:  # noqa: BLE001 - no scene yet during early boot
        return False
    if all(present.values()):
        return False

    def _guarded():
        return create_render_env(bpy)

    originals = []
    try:
        for module, attribute in _guard_targets():
            originals.append((module, attribute, getattr(module, attribute)))
            setattr(module, attribute, _guarded)
    except Exception:  # noqa: BLE001
        for module, attribute, original in originals:
            try:
                setattr(module, attribute, original)
            except Exception:  # noqa: BLE001
                log.debug("guard rollback failed", exc_info=True)
        return False
    _guard_originals = originals
    _guard_installed = True
    return True


def _remove_render_env_guard():
    global _guard_installed, _guard_originals
    if not _guard_installed:
        return False
    for module, attribute, original in _guard_originals or ():
        try:
            setattr(module, attribute, original)
        except Exception:  # noqa: BLE001
            log.debug("guard removal failed", exc_info=True)
    _guard_originals = None
    _guard_installed = False
    return True


def guard_installed():
    return _guard_installed


__all__ = [
    "ADDON_COPYRIGHT",
    "ADDON_LICENSE",
    "ADDON_MODULE",
    "ADDON_VERSION",
    "OPERATORS",
    "OPERATOR_BAKE",
    "OPERATOR_BAKE_AO",
    "OPERATOR_CREATE_ENV",
    "OWNER_NONE",
    "OWNER_OTHER_COPY",
    "OWNER_THIS_PLUGIN",
    "PANEL_CLASS_NAME",
    "PANEL_IDNAME",
    "PANEL_LABEL",
    "PREPARED_ATTR",
    "create_render_env",
    "ensure_registered",
    "guard_installed",
    "is_registered",
    "legacy_knobs_present",
    "missing_legacy_knobs",
    "registration_detail",
    "registration_owner",
    "reset_for_tests",
    "unregister",
]
