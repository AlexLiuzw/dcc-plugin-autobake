"""Register what this plugin needs in the host — without contesting any id.

Three separate concerns, deliberately kept apart:

1. **Data the bake helpers require.** Upstream's helpers read ``Scene.at_tool``
   and ``Object.at_objtool``. Those are property groups (data), not registered
   ids, and the helpers cannot run without them. If the host provider already
   created them we reuse that instance; otherwise we register our own copy of
   the two vendored ``PropertyGroup`` classes.

2. **Our UI.** :mod:`._ui` registers this plugin's own operators and panel
   under the ``autobake`` namespace. Upstream's ``at.*`` operators and its
   ``VIEW3D_PT_Atobake`` panel are *never* registered by this plugin, so the
   Blender provider plugin (which vendors the same add-on as 0.3.1) can hold
   them without a collision either way round.

3. **The 5.x render-environment guard.** ``at_utils.create_render_env_n`` reads
   two Cycles knobs Blender 5.0 removed. The guard routes both the helper and
   the name the upstream operator imported into a version-tolerant copy, so a
   human clicking the host's panel on 5.x does not crash either. It is only
   installed on a host that actually lacks a knob, so 4.2 / 4.5 run upstream
   code unmodified.

Consequence for acceptance: ``VIEW3D_PT_Atobake`` / ``at.*`` can be present and
perfectly functional while this plugin is dead, because they belong to the host
provider. Only ids whose ``__module__`` starts with ``dcc_plugin_autobake`` are
evidence for this plugin.
"""

from __future__ import annotations

import logging
import threading

from . import _ui

log = logging.getLogger(__name__)

ADDON_MODULE = "autobake_tools"
ADDON_VERSION = (1, 1, 5)
ADDON_LICENSE = "Apache-2.0"
ADDON_COPYRIGHT = "Copyright 2023 Igor Subachev (GarikDog)"

#: Upstream ids. This plugin does not register them; they are listed so
#: ``status`` can report what the *host* owns and so a test can assert we never
#: touch them.
UPSTREAM_OPERATORS = ("at.create_environment", "at.bake", "at.bake_ao")
UPSTREAM_PANEL_IDNAME = "VIEW3D_PT_Atobake"

#: Per-object marker the bake helpers poll for.
PREPARED_ATTR = "prepare_statement_prop_bool"

STATE_UNREGISTERED = "unregistered"
STATE_REGISTERED = "registered"

_lock = threading.Lock()
_state = STATE_UNREGISTERED
_detail = "not attempted"
_registered_classes = []
_own_properties = False
_guard_installed = False
_guard_originals = None


def _vendored():
    from .vendor import autobake_tools

    return autobake_tools


def _utils():
    from .vendor.autobake_tools.addon.utility import at_utils

    return at_utils


# ---------------------------------------------------------------------------
# state reporting
# ---------------------------------------------------------------------------


def is_registered():
    return _state == STATE_REGISTERED


def registration_detail():
    return _detail


def registered_ids():
    """The ids this plugin registered, in registration order."""
    return [_bl_idname(cls) for cls in _registered_classes]


def owns_property_groups():
    """True when this plugin created ``Scene.at_tool`` / ``Object.at_objtool``.

    False means the host provider's copy was already there and is being reused;
    unregistering must then leave it alone.
    """
    return _own_properties


def _bl_idname(cls):
    return getattr(cls, "bl_idname", None) or getattr(cls, "__name__", "?")


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def ensure_registered(bpy=None):
    """Register this plugin's ids and required data. Idempotent.

    Must run on Blender's main thread: ``register_class`` performs RNA writes.
    Returns the vendored add-on module for compatibility with the callers that
    read ``bl_info`` off it.
    """
    global _state, _detail, _registered_classes, _own_properties
    with _lock:
        if _state == STATE_REGISTERED:
            return _vendored()

        addon = _vendored()
        registered = []
        try:
            _ensure_property_groups(bpy, registered)
            for cls in _ui.build_classes(bpy).values():
                bpy.utils.register_class(cls)
                registered.append(cls)
        except Exception as exc:  # noqa: BLE001
            # Roll back rather than leave half a UI: an operator without its
            # panel, or properties without operators, is worse than nothing.
            for cls in reversed(registered):
                try:
                    bpy.utils.unregister_class(cls)
                except Exception:  # noqa: BLE001
                    log.debug("rollback of %s failed", _bl_idname(cls), exc_info=True)
            _release_property_groups(bpy)
            _state = STATE_UNREGISTERED
            _detail = "{0}: {1}".format(type(exc).__name__, exc)
            raise

        _registered_classes = registered
        _state = STATE_REGISTERED
        _detail = "registered {0} id(s)".format(len(registered))
        _install_render_env_guard(bpy)
        return addon


def unregister(bpy=None):
    """Remove exactly what :func:`ensure_registered` added. Idempotent."""
    global _state, _detail, _registered_classes
    with _lock:
        if _state != STATE_REGISTERED:
            return False
        for cls in reversed(_registered_classes):
            try:
                bpy.utils.unregister_class(cls)
            except Exception:  # noqa: BLE001
                log.warning("could not unregister %s", _bl_idname(cls), exc_info=True)
        _registered_classes = []
        _release_property_groups(bpy)
        _remove_render_env_guard()
        _state = STATE_UNREGISTERED
        _detail = "unregistered"
    return True


def _ensure_property_groups(bpy, registered):
    """Create ``Scene.at_tool`` / ``Object.at_objtool`` only if absent.

    Reuses the host's instance when it exists: the bake helpers need the values,
    not ownership of the class, and re-registering an identical PropertyGroup
    would replace the user's current settings mid-session.
    """
    global _own_properties
    types = bpy.types
    if hasattr(types.Scene, "at_tool") and hasattr(types.Object, "at_objtool"):
        _own_properties = False
        return

    from .vendor.autobake_tools.addon.property import (
        AT_Obj_Properties,
        AT_Properties,
    )

    for cls in (AT_Properties, AT_Obj_Properties):
        bpy.utils.register_class(cls)
        registered.append(cls)
    types.Scene.at_tool = bpy.props.PointerProperty(type=AT_Properties)
    types.Object.at_objtool = bpy.props.PointerProperty(type=AT_Obj_Properties)
    _own_properties = True


def _release_property_groups(bpy):
    """Drop the property groups, but only the ones this plugin created."""
    global _own_properties
    if not _own_properties:
        return
    types = bpy.types
    for owner, attr in ((types.Scene, "at_tool"), (types.Object, "at_objtool")):
        try:
            delattr(owner, attr)
        except Exception:  # noqa: BLE001
            log.debug("could not delete %s", attr, exc_info=True)
    from .vendor.autobake_tools.addon.property import (
        AT_Obj_Properties,
        AT_Properties,
    )

    for cls in (AT_Obj_Properties, AT_Properties):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:  # noqa: BLE001
            log.debug("could not unregister %s", cls.__name__, exc_info=True)
    _own_properties = False


def reset_for_tests():
    """Forget cached state without touching the host (test seam)."""
    global _state, _detail, _registered_classes, _own_properties
    global _guard_installed, _guard_originals
    with _lock:
        _state = STATE_UNREGISTERED
        _detail = "not attempted"
        _registered_classes = []
        _own_properties = False
        _guard_installed = False
        _guard_originals = None


# ---------------------------------------------------------------------------
# 5.x compatibility guard for the shared render-environment helper
# ---------------------------------------------------------------------------

#: The two host attributes upstream assumes and Blender 5.x dropped. Probed per
#: host instead of keyed off a version number, because the exact release that
#: dropped them is not the release that re-adds a replacement.
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

    Same sequence, same order as upstream. Only the setters whose attribute is
    absent on this host are skipped, so on 4.2 / 4.5 the effect is identical to
    the vendored helper.
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
    # Upstream re-derives the DirectX flag from the current normal_g; keep that
    # behaviour so a caller's explicit choice is applied afterwards.
    if settings is not None and bake is not None:
        normal_g = getattr(bake, "normal_g", None)
        if normal_g == "NEG_Y":
            settings.at_dx_normal = True
        elif normal_g == "POS_Y":
            settings.at_dx_normal = False


def _guard_targets():
    """Modules holding their own reference to ``create_render_env_n``.

    ``at_utils`` defines it, but ``at_create_env`` (the operator behind the
    host's panel button) imported it by name, so patching only ``at_utils``
    would leave that button running the unguarded original.
    """
    from .vendor.autobake_tools.addon.operator import at_create_env
    from .vendor.autobake_tools.addon.utility import at_utils

    return ((at_utils, "create_render_env_n"), (at_create_env, "create_render_env_n"))


def _install_render_env_guard(bpy=None):
    """Route both entry points through :func:`create_render_env`.

    A no-op on hosts that still have both legacy knobs: upstream is correct
    there and is left byte-identical and untouched. Returns whether the guard is
    installed.
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
    "PREPARED_ATTR",
    "UPSTREAM_OPERATORS",
    "UPSTREAM_PANEL_IDNAME",
    "create_render_env",
    "ensure_registered",
    "guard_installed",
    "is_registered",
    "legacy_knobs_present",
    "missing_legacy_knobs",
    "owns_property_groups",
    "registered_ids",
    "registration_detail",
    "reset_for_tests",
    "unregister",
]
