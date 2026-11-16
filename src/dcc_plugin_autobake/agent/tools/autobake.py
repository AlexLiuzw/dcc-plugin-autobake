"""Leaf tools that drive the vendored Auto-bake Tools 1.1.5 add-on.

The capability set is upstream's: build a bake environment, bake the Bevel
Shader into a normal map, bake Ambient Occlusion. Nothing here reimplements
baking — every tool runs the same upstream helper sequence the operators run
and then calls ``bpy.ops.object.bake``. The upstream operators stay
registered, so the ``Auto-bake Tools`` panel keeps working for a human in the
viewport.

Why an adapter is needed at all
-------------------------------
Upstream is GUI code. ``at.bake`` / ``at.bake_ao`` start an
``INVOKE_DEFAULT`` modal bake and then open a new window from a timer; a
modal job outlives the RPC that started it, and a background host has no
window to open. ``at.create_environment`` reads ``bpy.context.space_data``
for a cosmetic viewport-shading step that also assumes a visible 3D View.
So each tool:

* runs the upstream helper sequence rather than the modal operator body, and
  treats the viewport-shading step as best-effort;
* calls ``bpy.ops.object.bake(...)`` *without* ``INVOKE_DEFAULT`` so the call
  returns once the map is computed;
* activates the image node itself, because Blender bakes into the active
  image node of the active material and upstream leaves that to a click;
* saves the result to disk and re-reads it, because upstream's only proof of
  success is an image window a script cannot see.

Every write tool therefore reports the resolved path, the real on-disk size
and the image dimensions, and :func:`status` reports what is actually
registered in the running host — an acceptance run can check real files and
real registry state instead of trusting a success envelope.
"""

from __future__ import annotations

import os

from ._bpy import require_bpy
from ._errors import InvalidArgs, ToolError
from ._paths import default_output_path, public_output_path, resolve_output_path
from ._registry import leaf_tool
from .. import _addon

PROVIDER_TYPE = "autobake"
SKILL_SLUG = "autobake"

#: Numeric limits, taken from the upstream property definitions so a caller
#: gets the same rejections the panel would apply.
BEVEL_SAMPLES = (2, 32)
BEVEL_RADIUS = (0.001, 0.5)
IMAGE_SIZE = (2, 32768)
AO_DISTANCE = (0.001, 10.0)
AO_SAMPLES = (2, 32)
AO_EXPONENTIATION = (0.1, 10.0)

_NORMAL_SUFFIX = "_n"
_AO_SUFFIX = "_ao"


# ---------------------------------------------------------------------------
# host accessors
# ---------------------------------------------------------------------------


def _context(bpy):
    return getattr(bpy, "context", None)


def _scene(bpy):
    scene = getattr(_context(bpy), "scene", None)
    if scene is None:
        raise ToolError(
            "bpy.context.scene is unavailable — not inside a Blender host",
            code="no_scene",
        )
    return scene


def _active_object(bpy):
    obj = getattr(_context(bpy), "active_object", None)
    if obj is None:
        raise ToolError(
            "no active object — select a mesh object in Blender, or pass object_name",
            code="no_active_object",
            context={"hint": "bpy.context.active_object is None"},
        )
    return obj


def _require_mesh(obj):
    if getattr(obj, "type", None) != "MESH":
        raise ToolError(
            "object {0!r} is {1}, not a MESH — auto-bake needs a mesh".format(
                getattr(obj, "name", "?"), getattr(obj, "type", None)
            ),
            code="unsupported_object_type",
            context={"object": getattr(obj, "name", None), "type": getattr(obj, "type", None)},
        )


def _activate(bpy, name):
    """Select *name* and make it active, mirroring a user click."""
    objects = getattr(getattr(bpy, "data", None), "objects", None)
    if objects is None:
        raise ToolError(
            "bpy.data.objects is unavailable — not inside a Blender host",
            code="no_data",
        )
    if name not in objects:
        raise ToolError(
            "object {0!r} not found in this file".format(name),
            code="object_not_found",
            context={"object": name},
        )
    for candidate in list(objects):
        try:
            candidate.select_set(False)
        except Exception:  # noqa: BLE001
            pass
    obj = objects[name]
    try:
        obj.select_set(True)
    except Exception:  # noqa: BLE001
        pass
    view_layer = getattr(_context(bpy), "view_layer", None)
    if view_layer is not None:
        try:
            view_layer.objects.active = obj
        except Exception:  # noqa: BLE001
            pass
    return obj


def _target_object(bpy, object_name):
    if object_name:
        _activate(bpy, object_name)
    obj = _active_object(bpy)
    _require_mesh(obj)
    return obj


def _settings(bpy):
    scene = _scene(bpy)
    settings = getattr(scene, "at_tool", None)
    if settings is None:
        raise ToolError(
            "Scene.at_tool is missing — the auto-bake add-on is not registered "
            "in this host",
            code="autobake_not_registered",
            context={"expected": "Scene.at_tool"},
        )
    return scene, settings


def _object_settings(obj):
    tool = getattr(obj, "at_objtool", None)
    if tool is None:
        raise ToolError(
            "Object.at_objtool is missing — the auto-bake add-on is not "
            "registered in this host",
            code="autobake_not_registered",
            context={"expected": "Object.at_objtool"},
        )
    return tool


def _upstream_utils():
    from ..vendor.autobake_tools.addon.utility import at_utils

    return at_utils


# ---------------------------------------------------------------------------
# argument validation
# ---------------------------------------------------------------------------


def _int_in_range(value, name, limits):
    low, high = limits
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidArgs(
            "{0} must be an integer in [{1}, {2}]; got {3!r}".format(name, low, high, value),
            context={"argument": name, "value": repr(value)},
        )
    if number < low or number > high:
        raise InvalidArgs(
            "{0} must be in [{1}, {2}]; got {3}".format(name, low, high, number),
            context={"argument": name, "value": number, "minimum": low, "maximum": high},
        )
    return number


def _float_in_range(value, name, limits):
    low, high = limits
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise InvalidArgs(
            "{0} must be a number in [{1}, {2}]; got {3!r}".format(name, low, high, value),
            context={"argument": name, "value": repr(value)},
        )
    if number < low or number > high:
        raise InvalidArgs(
            "{0} must be in [{1}, {2}]; got {3}".format(name, low, high, number),
            context={"argument": name, "value": number, "minimum": low, "maximum": high},
        )
    return number


def _apply_settings(settings, bevel_samples, bevel_radius, image_width, image_height,
                    ao_distance, ao_samples, ao_exponentiation):
    """Push numeric overrides onto ``Scene.at_tool``.

    ``directx_normal`` is deliberately absent: upstream
    ``create_render_env_n`` re-derives it from the current ``render.bake.normal_g``,
    so a value set here would be overwritten a moment later. It is applied
    after that helper instead (see :func:`_push_to_nodes`).
    """
    applied = {}
    if bevel_samples is not None:
        settings.bevel_samples_prop_int = _int_in_range(
            bevel_samples, "bevel_samples", BEVEL_SAMPLES
        )
        applied["bevel_samples"] = settings.bevel_samples_prop_int
    if bevel_radius is not None:
        settings.bevel_radius_prop_float = _float_in_range(
            bevel_radius, "bevel_radius", BEVEL_RADIUS
        )
        applied["bevel_radius"] = settings.bevel_radius_prop_float
    if image_width is not None:
        settings.image_width_prop_int = _int_in_range(image_width, "image_width", IMAGE_SIZE)
        applied["image_width"] = settings.image_width_prop_int
    if image_height is not None:
        settings.image_height_prop_int = _int_in_range(image_height, "image_height", IMAGE_SIZE)
        applied["image_height"] = settings.image_height_prop_int
    if ao_distance is not None:
        settings.at_ao_distance = _float_in_range(ao_distance, "ao_distance", AO_DISTANCE)
        applied["ao_distance"] = settings.at_ao_distance
    if ao_samples is not None:
        settings.at_ao_samples = _int_in_range(ao_samples, "ao_samples", AO_SAMPLES)
        applied["ao_samples"] = settings.at_ao_samples
    if ao_exponentiation is not None:
        settings.at_ao_exponentiation = _float_in_range(
            ao_exponentiation, "ao_exponentiation", AO_EXPONENTIATION
        )
        applied["ao_exponentiation"] = settings.at_ao_exponentiation
    return applied


def _push_to_nodes(utils, settings, directx_normal=None):
    """Re-run the per-node setters that need an existing node tree.

    The ``at_tool`` setters swallow "there are no Nodes by now", so the
    numeric overrides applied before the environment exists only reach the
    shader nodes when they are re-applied here.
    """
    if settings is None:
        return {}
    applied = {}
    if directx_normal is not None:
        # After create_render_env_n: that helper seeds at_dx_normal from
        # render.bake.normal_g, so the caller's explicit choice goes last.
        try:
            settings.at_dx_normal = bool(directx_normal)
            applied["directx_normal"] = True
        except Exception:  # noqa: BLE001
            applied["directx_normal"] = False
    for name, setter in (
        ("bevel_samples", utils.bevel_samples_setting),
        ("bevel_radius", utils.bevel_radius_setting),
        ("ao_distance", utils.ao_distance_setting),
        ("ao_exponentiation", utils.ao_exponentiation_setting),
        ("ao_samples", utils.ao_samples_setting),
        ("directx_normal", utils.dx_normal_setting),
    ):
        try:
            setter()
            applied[name] = True
        except Exception:  # noqa: BLE001
            applied[name] = False
    return applied


# ---------------------------------------------------------------------------
# material / image plumbing
# ---------------------------------------------------------------------------


def _active_material(obj):
    return getattr(obj, "active_material", None)


def _image_nodes(obj):
    material = _active_material(obj)
    node_tree = getattr(material, "node_tree", None)
    nodes = getattr(node_tree, "nodes", None)
    if nodes is None:
        return None
    return [node for node in nodes if getattr(node, "type", None) == "TEX_IMAGE"]


def _image_node(obj):
    """The material's image texture node (the last one, matching upstream)."""
    nodes = _image_nodes(obj)
    if not nodes:
        return None
    return nodes[-1]


def _activate_node(obj, node):
    """Make *node* the material's active node — the bake target."""
    node_tree = getattr(_active_material(obj), "node_tree", None)
    nodes = getattr(node_tree, "nodes", None)
    if nodes is None:
        return False
    try:
        for other in nodes:
            other.select = False
        node.select = True
        nodes.active = node
    except Exception:  # noqa: BLE001
        return False
    return True


def _resolve_created_image(bpy, requested, suffix):
    """Return the image datablock ``bpy.ops.image.new`` actually created.

    Upstream links the image by the *requested* name, but Blender renames on a
    collision (``Cube_1_n`` → ``Cube_1_n.001``), which would leave the node
    pointing at nothing and the bake landing in a datablock nobody saves.
    """
    images = getattr(getattr(bpy, "data", None), "images", None)
    if images is None:
        raise ToolError(
            "bpy.data.images is unavailable — not inside a Blender host",
            code="no_data",
            context={"image": requested},
        )
    if images.get(requested) is not None:
        return requested
    candidates = [
        image.name
        for image in images
        if str(getattr(image, "name", "")).startswith(requested)
        and str(getattr(image, "name", "")).endswith(suffix)
    ]
    if candidates:
        return sorted(candidates)[-1]
    raise ToolError(
        "Blender did not create the bake target image {0!r}".format(requested),
        code="image_missing",
        context={"image": requested, "suffix": suffix},
    )


def _bind_image(bpy, obj, image_name):
    """Point the material's image node at *image_name* and make it active."""
    images = getattr(getattr(bpy, "data", None), "images", None)
    image = images.get(image_name) if images is not None else None
    if image is None:
        return False
    node = _image_node(obj)
    if node is None:
        return False
    node.image = image
    return _activate_node(obj, node)


def _image_size(image):
    size = getattr(image, "size", None) or (0, 0)
    try:
        return int(size[0]), int(size[1])
    except (TypeError, ValueError, IndexError):
        return 0, 0


def _save_image(bpy, obj, image_name, path):
    """Write *image_name* to *path* and verify the bytes landed."""
    images = getattr(getattr(bpy, "data", None), "images", None)
    image = images.get(image_name) if images is not None else None
    if image is None:
        raise ToolError(
            "baked image {0!r} is gone before it could be saved".format(image_name),
            code="image_missing",
            context={"image": image_name},
        )
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        try:
            os.makedirs(directory)
        except OSError as exc:
            raise ToolError(
                "output directory {0!r} could not be created: {1}".format(directory, exc),
                code="output_dir_failed",
                context={"path": path, "directory": directory},
            ) from exc
    try:
        image.filepath_raw = path
        image.file_format = "PNG"
        image.save()
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            "saving image {0!r} to {1!r} failed: {2}".format(image_name, path, exc),
            code="image_save_failed",
            context={"image": image_name, "path": path},
        ) from exc
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ToolError(
            "image {0!r} was saved but is not on disk at {1!r}: {2}".format(
                image_name, path, exc
            ),
            code="image_not_written",
            context={"image": image_name, "path": path},
        ) from exc
    if size <= 0:
        raise ToolError(
            "baked image file {0!r} is empty".format(path),
            code="image_empty",
            context={"image": image_name, "path": path},
        )
    width, height = _image_size(image)
    return size, width, height


# ---------------------------------------------------------------------------
# bake driver
# ---------------------------------------------------------------------------


def _environment_prepared(obj):
    return bool(getattr(_object_settings(obj), _addon.PREPARED_ATTR, False))


def _require_environment(obj, tool_name):
    if _environment_prepared(obj):
        return
    raise ToolError(
        "{0} needs a bake environment first — call "
        "{1}.{2}.create_environment for {3!r}".format(
            tool_name, PROVIDER_TYPE, SKILL_SLUG, getattr(obj, "name", "?")
        ),
        code="environment_not_prepared",
        context={
            "tool": tool_name,
            "object": getattr(obj, "name", None),
            "upstream_operator": _addon.OPERATOR_CREATE_ENV,
            "prepared_flag": _addon.PREPARED_ATTR,
        },
    )


def _bake(bpy, obj, settings, utils, tool_name, label, suffix, configure, bake_type):
    """Create the target image, bake into it, and return its name."""
    configure()
    if _image_node(obj) is None:
        # Upstream ``create_image`` dereferences the node without a guard, so
        # a cleared material would raise UnboundLocalError after Blender has
        # already created the datablock. Check first.
        raise ToolError(
            "no image texture node to bake into — "
            "{0}.{1}.create_environment builds one".format(PROVIDER_TYPE, SKILL_SLUG),
            code="no_image_node",
            context={"object": getattr(obj, "name", None), "tool": tool_name},
        )
    requested = utils.create_image(suffix)
    image_name = _resolve_created_image(bpy, requested, suffix)
    if not _bind_image(bpy, obj, image_name):
        raise ToolError(
            "the baked image could not be bound to the material's image node",
            code="no_image_node",
            context={"object": getattr(obj, "name", None), "image": image_name},
        )
    try:
        bpy.ops.object.bake(type=bake_type, use_clear=True)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            "Blender refused the {0} bake: {1}".format(label, exc),
            code="bake_failed",
            context={
                "tool": tool_name,
                "bake_type": bake_type,
                "object": getattr(obj, "name", None),
                "engine": getattr(_scene(bpy).render, "engine", None),
            },
        ) from exc
    return image_name


# ---------------------------------------------------------------------------
# leaf tools
# ---------------------------------------------------------------------------

_CREATE_ENV_SCHEMA = {
    "type": "object",
    "properties": {
        "object_name": {"type": "string", "description": "Mesh to prepare; defaults to the active object."},
        "bevel_samples": {"type": "integer", "minimum": BEVEL_SAMPLES[0], "maximum": BEVEL_SAMPLES[1]},
        "bevel_radius": {"type": "number", "minimum": BEVEL_RADIUS[0], "maximum": BEVEL_RADIUS[1]},
        "image_width": {"type": "integer", "minimum": IMAGE_SIZE[0], "maximum": IMAGE_SIZE[1]},
        "image_height": {"type": "integer", "minimum": IMAGE_SIZE[0], "maximum": IMAGE_SIZE[1]},
        "ao_distance": {"type": "number", "minimum": AO_DISTANCE[0], "maximum": AO_DISTANCE[1]},
        "ao_samples": {"type": "integer", "minimum": AO_SAMPLES[0], "maximum": AO_SAMPLES[1]},
        "ao_exponentiation": {
            "type": "number",
            "minimum": AO_EXPONENTIATION[0],
            "maximum": AO_EXPONENTIATION[1],
        },
        "directx_normal": {"type": "boolean", "description": "Write the normal map's green channel for DirectX instead of OpenGL."},
    },
    "additionalProperties": False,
}

_BAKE_SCHEMA = {
    "type": "object",
    "properties": {
        "object_name": {"type": "string", "description": "Mesh to bake; defaults to the active object."},
        "output_path": {
            "type": "string",
            "description": "PNG path. Absolute, or relative to LAP_AUTOBAKE_OUTPUT_DIR (default: the process cwd).",
        },
    },
    "additionalProperties": False,
}

_STATUS_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


@leaf_tool(
    name="{0}.{1}.create_environment".format(PROVIDER_TYPE, SKILL_SLUG),
    skill_slug=SKILL_SLUG,
    timeout_s=120.0,
    arg_schema=_CREATE_ENV_SCHEMA,
)
def create_environment(object_name=None, bevel_samples=None, bevel_radius=None,
                      image_width=None, image_height=None, ao_distance=None,
                      ao_samples=None, ao_exponentiation=None, directx_normal=None,
                      **ignored):
    """Prepare a mesh for baking — upstream ``at.create_environment``.

    Replaces the material's node tree with the bevel-shader bake network
    (upstream ``create_shader_editor_env`` clears it first), switches the
    render engine to Cycles, and marks the object prepared so the two bake
    tools can run.
    """
    bpy = require_bpy()
    _addon.ensure_registered(bpy)
    obj = _target_object(bpy, object_name)
    scene, settings = _settings(bpy)
    object_settings = _object_settings(obj)
    utils = _upstream_utils()

    requested = _apply_settings(
        settings,
        bevel_samples,
        bevel_radius,
        image_width,
        image_height,
        ao_distance,
        ao_samples,
        ao_exponentiation,
    )

    # Upstream AT_OP_Create_Environment.execute, in order. The viewport step
    # is cosmetic and needs a real 3D View, which a background host lacks.
    viewport_error = None
    try:
        utils.viewport_shading_setting(1)
    except Exception as exc:  # noqa: BLE001
        viewport_error = "{0}: {1}".format(type(exc).__name__, exc)

    _addon.ensure_registered(bpy)  # installs the 5.x guard before the helper runs
    try:
        _addon.create_render_env(bpy)
        utils.create_mat_env()
        node = utils.create_shader_editor_env()
    except Exception as exc:  # noqa: BLE001
        raise ToolError(
            "building the bake environment failed: {0}".format(exc),
            code="environment_build_failed",
            context={
                "object": getattr(obj, "name", None),
                "upstream_operator": _addon.OPERATOR_CREATE_ENV,
                "hint": "a bake environment needs the Cycles engine",
            },
        ) from exc

    object_settings.prepare_statement_prop_bool = True
    node_active = False
    if node is not None:
        node_active = _activate_node(obj, node)
    pushed = _push_to_nodes(utils, settings, directx_normal)

    material = _active_material(obj)
    return {
        "ok": True,
        "provider_type": PROVIDER_TYPE,
        "object": getattr(obj, "name", None),
        "material": getattr(material, "name", None),
        "render_engine": getattr(getattr(scene, "render", None), "engine", None),
        "bake_type": getattr(getattr(scene, "cycles", None), "bake_type", None),
        "environment_prepared": _environment_prepared(obj),
        "image_node_created": node is not None,
        "image_node_active": bool(node_active),
        "settings_requested": requested,
        "settings_applied_to_nodes": pushed,
        "upstream_operator": _addon.OPERATOR_CREATE_ENV,
        "addon_version": ".".join(str(part) for part in _addon.ADDON_VERSION),
        "registration_owner": _addon.registration_owner(),
        "viewport_shading_skipped": viewport_error,
        # Names the 4.x-only Cycles knobs this host lacks (empty on 4.2 /
        # 4.5, where upstream ran unmodified). Both the leaf tool and the
        # panel button go through the same guarded helper, so this describes
        # the host, not the entry point.
        "legacy_knobs_skipped": _addon.missing_legacy_knobs(scene),
    }


@leaf_tool(
    name="{0}.{1}.bake_normal".format(PROVIDER_TYPE, SKILL_SLUG),
    skill_slug=SKILL_SLUG,
    timeout_s=900.0,
    arg_schema=_BAKE_SCHEMA,
)
def bake_normal(object_name=None, output_path=None, **ignored):
    """Bake the Bevel-Shader normal map and write it to disk (``at.bake``)."""
    bpy = require_bpy()
    _addon.ensure_registered(bpy)
    obj = _target_object(bpy, object_name)
    tool_name = "{0}.{1}.bake_normal".format(PROVIDER_TYPE, SKILL_SLUG)
    _scene_obj, settings = _settings(bpy)
    _require_environment(obj, tool_name)
    utils = _upstream_utils()
    path = _resolve_output(output_path, obj, _NORMAL_SUFFIX)

    image_name = _bake(
        bpy,
        obj,
        settings,
        utils,
        tool_name,
        "normal",
        _NORMAL_SUFFIX,
        configure=utils.change_bake_normal,
        bake_type="NORMAL",
    )
    size, width, height = _save_image(bpy, obj, image_name, path)
    return {
        "ok": True,
        "map": "normal",
        "bake_type": "NORMAL",
        "provider_type": PROVIDER_TYPE,
        "object": getattr(obj, "name", None),
        "image": image_name,
        "path": public_output_path(path),
        "absolute_path": path,
        "file_size_bytes": size,
        "width": width,
        "height": height,
        "suffix": _NORMAL_SUFFIX,
        "upstream_operator": _addon.OPERATOR_BAKE,
        "environment_prepared": _environment_prepared(obj),
        "image_width_setting": getattr(settings, "image_width_prop_int", None),
        "image_height_setting": getattr(settings, "image_height_prop_int", None),
        "directx_normal": getattr(settings, "at_dx_normal", None),
        "legacy_knobs_skipped": _addon.missing_legacy_knobs(_scene(bpy)),
    }


@leaf_tool(
    name="{0}.{1}.bake_ao".format(PROVIDER_TYPE, SKILL_SLUG),
    skill_slug=SKILL_SLUG,
    timeout_s=900.0,
    arg_schema=_BAKE_SCHEMA,
)
def bake_ao(object_name=None, output_path=None, **ignored):
    """Bake the Ambient-Occlusion map and write it to disk (``at.bake_ao``)."""
    bpy = require_bpy()
    _addon.ensure_registered(bpy)
    obj = _target_object(bpy, object_name)
    tool_name = "{0}.{1}.bake_ao".format(PROVIDER_TYPE, SKILL_SLUG)
    _scene_obj, settings = _settings(bpy)
    _require_environment(obj, tool_name)
    utils = _upstream_utils()
    path = _resolve_output(output_path, obj, _AO_SUFFIX)

    def _configure():
        # Upstream AT_OP_Bake_ao.execute drops the glossy preview first, so
        # the DIFFUSE bake sees the diffuse branch.
        if getattr(settings, "at_glossy_preview", False):
            settings.at_glossy_preview = False
        utils.change_bake_ao()

    image_name = _bake(
        bpy,
        obj,
        settings,
        utils,
        tool_name,
        "ambient occlusion",
        _AO_SUFFIX,
        configure=_configure,
        bake_type="DIFFUSE",
    )
    size, width, height = _save_image(bpy, obj, image_name, path)
    return {
        "ok": True,
        "map": "ao",
        "bake_type": "DIFFUSE",
        "provider_type": PROVIDER_TYPE,
        "object": getattr(obj, "name", None),
        "image": image_name,
        "path": public_output_path(path),
        "absolute_path": path,
        "file_size_bytes": size,
        "width": width,
        "height": height,
        "suffix": _AO_SUFFIX,
        "upstream_operator": _addon.OPERATOR_BAKE_AO,
        "environment_prepared": _environment_prepared(obj),
        "image_width_setting": getattr(settings, "image_width_prop_int", None),
        "image_height_setting": getattr(settings, "image_height_prop_int", None),
        "ao_distance": getattr(settings, "at_ao_distance", None),
        "ao_samples": getattr(settings, "at_ao_samples", None),
        "ao_exponentiation": getattr(settings, "at_ao_exponentiation", None),
        "legacy_knobs_skipped": _addon.missing_legacy_knobs(_scene(bpy)),
    }


@leaf_tool(
    name="{0}.{1}.status".format(PROVIDER_TYPE, SKILL_SLUG),
    skill_slug=SKILL_SLUG,
    timeout_s=30.0,
    arg_schema=_STATUS_SCHEMA,
)
def status(**ignored):
    """Read-only report of what the auto-bake add-on registered in this host."""
    bpy = require_bpy()
    addon = _addon.ensure_registered(bpy)
    info = dict(getattr(addon, "bl_info", {}) or {})

    operators = {}
    ops = getattr(bpy, "ops", None)
    for full_id in _addon.OPERATORS:
        namespace, _, name = full_id.partition(".")
        operators[full_id] = hasattr(getattr(ops, namespace, None), name)

    panel_class = _panel_class()
    types_module = getattr(bpy, "types", None)
    panel_registered = None
    if types_module is not None:
        # Blender files a registered class under its bl_idname, not its
        # __name__: this add-on defines VIEW3D_PT_Autobake but registers
        # VIEW3D_PT_Atobake, so a lookup by class name always misses.
        panel_registered = False
        for key in (_addon.PANEL_IDNAME, _addon.PANEL_CLASS_NAME):
            candidate = getattr(types_module, key, None)
            if candidate is None:
                continue
            if panel_class is not None and candidate is not panel_class:
                continue
            panel_registered = True
            break

    scene_type = getattr(types_module, "Scene", None) if types_module is not None else None
    object_type = getattr(types_module, "Object", None) if types_module is not None else None
    active = getattr(_context(bpy), "active_object", None)
    settings = getattr(_context(bpy).scene, "at_tool", None) if _context(bpy).scene else None

    prepared = None
    if active is not None:
        try:
            prepared = _environment_prepared(active)
        except ToolError:
            prepared = None

    return {
        "ok": True,
        "provider_type": PROVIDER_TYPE,
        "skill_slug": SKILL_SLUG,
        "addon_package": _addon.ADDON_MODULE,
        "addon_name": info.get("name"),
        "addon_version": list(info.get("version") or _addon.ADDON_VERSION),
        "addon_author": info.get("author"),
        "addon_blender_floor": list(info.get("blender") or ()),
        "license": _addon.ADDON_LICENSE,
        "copyright": _addon.ADDON_COPYRIGHT,
        "registration": {
            "registered": _addon.is_registered(),
            "owner": _addon.registration_owner(),
            "detail": _addon.registration_detail(),
            "render_env_guard_installed": _addon.guard_installed(),
        },
        "operators": operators,
        "panel": {
            "class": _addon.PANEL_CLASS_NAME,
            "idname": getattr(panel_class, "bl_idname", _addon.PANEL_IDNAME),
            "label": getattr(panel_class, "bl_label", _addon.PANEL_LABEL),
            "space_type": getattr(panel_class, "bl_space_type", "VIEW_3D"),
            "region_type": getattr(panel_class, "bl_region_type", "UI"),
            "category": getattr(panel_class, "bl_category", _addon.PANEL_LABEL),
            "registered": panel_registered,
            "lookup_key": _addon.PANEL_IDNAME,
        },
        "properties": {
            "Scene.at_tool": (
                hasattr(scene_type, "at_tool") if scene_type is not None else None
            ),
            "Object.at_objtool": (
                hasattr(object_type, "at_objtool") if object_type is not None else None
            ),
        },
        "active_object": {
            "name": getattr(active, "name", None),
            "type": getattr(active, "type", None),
            "environment_prepared": prepared,
        },
        "settings": {
            "image_width": getattr(settings, "image_width_prop_int", None),
            "image_height": getattr(settings, "image_height_prop_int", None),
            "bevel_samples": getattr(settings, "bevel_samples_prop_int", None),
            "bevel_radius": getattr(settings, "bevel_radius_prop_float", None),
            "ao_distance": getattr(settings, "at_ao_distance", None),
            "ao_samples": getattr(settings, "at_ao_samples", None),
            "ao_exponentiation": getattr(settings, "at_ao_exponentiation", None),
            "directx_normal": getattr(settings, "at_dx_normal", None),
            "last_image": getattr(settings, "at_image_name", None),
        },
        # Reported so an acceptance run can see whether the host still has
        # the two 4.x-only Cycles knobs upstream assumes.
        "legacy_knobs_skipped": _addon.missing_legacy_knobs(_scene(bpy)),
    }


# ---------------------------------------------------------------------------
# helpers that need a resolved object
# ---------------------------------------------------------------------------


def _panel_class():
    """The upstream panel class object (its definition, not its state)."""
    try:
        from ..vendor.autobake_tools.addon.menu.at_ui_panel import VIEW3D_PT_Autobake
    except Exception:  # noqa: BLE001
        return None
    return VIEW3D_PT_Autobake


def _resolve_output(output_path, obj, suffix):
    if output_path is None or (isinstance(output_path, str) and not output_path.strip()):
        return default_output_path(getattr(obj, "name", "autobake"), suffix)
    try:
        return resolve_output_path(output_path)
    except ValueError as exc:
        raise InvalidArgs(
            "output_path is invalid: {0}".format(exc),
            context={"output_path": output_path},
        ) from exc


__all__ = ["bake_ao", "bake_normal", "create_environment", "status"]
