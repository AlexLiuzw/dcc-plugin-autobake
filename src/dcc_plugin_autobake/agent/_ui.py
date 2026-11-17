"""This plugin's own Blender UI: panel + operators under its own ids.

Why this module exists
----------------------
The vendored upstream add-on registers ``at.create_environment`` / ``at.bake``
/ ``at.bake_ao`` and the panel ``VIEW3D_PT_Atobake``. The Blender provider
plugin (``dcc-plugin-blender`` 0.3.1) vendors the *same* add-on, so if both
plugins are installed they compete for one ``bl_idname`` and one operator set.
Whichever registers first wins, and the human then sees a panel whose
``__module__`` belongs to the other plugin — evidence that looks like ours but
is not.

Two providers cannot share one id namespace. So this plugin never registers the
upstream ids at all:

* ``autobake.create_environment`` / ``autobake.bake_normal`` / ``autobake.bake_ao``
  — our operators, in our own ``autobake`` namespace
* ``VIEW3D_PT_AutobakeTools`` — our panel

Both coexist with the host's ``at.*`` / ``VIEW3D_PT_Atobake`` because nothing
collides. The acceptance rule this enables: a panel or operator only counts as
this plugin's when its ``__module__`` starts with ``dcc_plugin_autobake``.

The panel's buttons call :mod:`..tools.autobake` — the exact functions the MCP
tools call. That is deliberate: one implementation, so a button and an RPC can
never drift, and the guarded 5.x render-environment path is used by both.

Shared state, not shared ids
----------------------------
The upstream helpers read ``Scene.at_tool`` and ``Object.at_objtool``. Those
property groups are *data*, not ids, and the bake helpers require them. If the
host already created them we reuse that; otherwise we register our own copy.
Reusing them is safe precisely because no id is contested: two plugins reading
the same settings object is the same situation as a user editing the panel and
an API call reading the values.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: Our operator ids. Distinct from upstream's ``at.*`` on purpose.
OPERATOR_CREATE_ENV = "autobake.create_environment"
OPERATOR_BAKE_NORMAL = "autobake.bake_normal"
OPERATOR_BAKE_AO = "autobake.bake_ao"
OPERATORS = (OPERATOR_CREATE_ENV, OPERATOR_BAKE_NORMAL, OPERATOR_BAKE_AO)

PANEL_IDNAME = "VIEW3D_PT_AutobakeTools"
PANEL_LABEL = "Auto-bake Tools"

#: Every id this module registers, for status reporting and collision checks.
OWNED_IDS = OPERATORS + (PANEL_IDNAME,)


def _tool_error_text(exc):
    """Operator-safe message from a tool exception."""
    message = getattr(exc, "message", None) or str(exc)
    code = getattr(exc, "code", None)
    return "[{0}] {1}".format(code, message) if code else message


def build_classes(bpy):
    """Create the operator and panel classes bound to *bpy*'s base classes.

    Built per host rather than at import time: the agent package has to stay
    importable on plain CPython (unit tests, the sidecar), where ``bpy`` does
    not exist.
    """
    types = bpy.types
    Operator = types.Operator
    Panel = types.Panel

    class AUTOBAKE_OT_CreateEnvironment(Operator):
        bl_idname = OPERATOR_CREATE_ENV
        bl_label = "Create Bake Environment"
        bl_description = "Build the bevel-shader bake environment for the active mesh"

        @classmethod
        def poll(cls, context):
            obj = getattr(context, "active_object", None)
            return obj is not None and getattr(obj, "type", None) == "MESH"

        def execute(self, context):
            from .tools import autobake

            try:
                autobake.create_environment()
            except Exception as exc:  # noqa: BLE001 - operators report, never raise
                self.report({"ERROR"}, _tool_error_text(exc))
                return {"CANCELLED"}
            return {"FINISHED"}

    class AUTOBAKE_OT_BakeNormal(Operator):
        bl_idname = OPERATOR_BAKE_NORMAL
        bl_label = "Bake Normal and Show"
        bl_description = "Bake the Bevel Shader into a normal map and save it"

        @classmethod
        def poll(cls, context):
            return _prepared(context)

        def execute(self, context):
            from .tools import autobake

            try:
                result = autobake.bake_normal()
            except Exception as exc:  # noqa: BLE001
                self.report({"ERROR"}, _tool_error_text(exc))
                return {"CANCELLED"}
            self.report({"INFO"}, "normal map: {0}".format(result["absolute_path"]))
            return {"FINISHED"}

    class AUTOBAKE_OT_BakeAO(Operator):
        bl_idname = OPERATOR_BAKE_AO
        bl_label = "Bake AO and Show"
        bl_description = "Bake Ambient Occlusion and save it"

        @classmethod
        def poll(cls, context):
            return _prepared(context)

        def execute(self, context):
            from .tools import autobake

            try:
                result = autobake.bake_ao()
            except Exception as exc:  # noqa: BLE001
                self.report({"ERROR"}, _tool_error_text(exc))
                return {"CANCELLED"}
            self.report({"INFO"}, "AO map: {0}".format(result["absolute_path"]))
            return {"FINISHED"}

    class VIEW3D_PT_AutobakeTools(Panel):
        bl_idname = PANEL_IDNAME
        bl_label = PANEL_LABEL
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = PANEL_LABEL

        @classmethod
        def poll(cls, context):
            obj = getattr(context, "active_object", None)
            return obj is not None and getattr(obj, "type", None) == "MESH"

        def draw(self, context):
            layout = self.layout
            settings = getattr(getattr(context, "scene", None), "at_tool", None)
            if settings is None:
                layout.label(text="Auto-bake settings unavailable", icon="ERROR")
                return

            box = layout.box()
            box.operator(OPERATOR_CREATE_ENV)
            layout.column().prop(settings, "image_width_prop_int", text="Image Width")
            layout.column().prop(settings, "image_height_prop_int", text="Image Height")

            layout.label(text="Bevel Shader:")
            layout.column().prop(settings, "bevel_samples_prop_int", text="Bevel Samples")
            layout.column().prop(settings, "bevel_radius_prop_float", text="Bevel Radius")
            layout.column().prop(settings, "at_dx_normal", text="DirectX Normal")

            box = layout.box()
            box.operator(OPERATOR_BAKE_NORMAL)

            layout.label(text="Ambient Occlusion:")
            layout.column().prop(settings, "at_ao_distance", text="AO Distance")
            layout.column().prop(settings, "at_ao_samples", text="AO Samples")
            layout.column().prop(settings, "at_ao_exponentiation", text="AO Exponentiation")

            box = layout.box()
            box.operator(OPERATOR_BAKE_AO)

    return {
        OPERATOR_CREATE_ENV: AUTOBAKE_OT_CreateEnvironment,
        OPERATOR_BAKE_NORMAL: AUTOBAKE_OT_BakeNormal,
        OPERATOR_BAKE_AO: AUTOBAKE_OT_BakeAO,
        PANEL_IDNAME: VIEW3D_PT_AutobakeTools,
    }


def _prepared(context):
    obj = getattr(context, "active_object", None)
    if obj is None or getattr(obj, "type", None) != "MESH":
        return False
    tool = getattr(obj, "at_objtool", None)
    return bool(getattr(tool, "prepare_statement_prop_bool", False))


def is_ours(class_object):
    """True when *class_object* was defined by this plugin.

    The acceptance rule for panel/operator evidence: the host provider vendors
    the same add-on, so a class under a shared id can belong to either plugin.
    """
    module = getattr(class_object, "__module__", "") or ""
    return module.startswith("dcc_plugin_autobake")


__all__ = [
    "OPERATORS",
    "OPERATOR_BAKE_AO",
    "OPERATOR_BAKE_NORMAL",
    "OPERATOR_CREATE_ENV",
    "OWNED_IDS",
    "PANEL_IDNAME",
    "PANEL_LABEL",
    "build_classes",
    "is_ours",
]
