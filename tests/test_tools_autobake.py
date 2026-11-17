"""Tool behaviour against the fake host: registration, guards, argument checks.

These tests are hermetic (no Blender, no files beyond ``tmp_path``) and check
the adapter's contract, not the bake maths. Real bake output is verified by
host acceptance, which this suite deliberately cannot stand in for.
"""

from __future__ import annotations

import os

import pytest

import types as _types

from dcc_plugin_autobake.agent import _addon, _ui
from dcc_plugin_autobake.agent.tools import _bpy, autobake
from dcc_plugin_autobake.agent.tools._errors import InvalidArgs, ToolError


# ---------------------------------------------------------------------------
# tool inventory / wire names
# ---------------------------------------------------------------------------


def test_the_four_documented_tools_are_registered():
    from dcc_plugin_autobake.agent._wiring import build_registry

    registry = build_registry()
    assert registry.names() == [
        "autobake.autobake.bake_ao",
        "autobake.autobake.bake_normal",
        "autobake.autobake.create_environment",
        "autobake.autobake.status",
    ]


def test_no_tool_is_registered_on_the_blender_provider():
    from dcc_plugin_autobake.agent._wiring import build_registry

    for name in build_registry().names():
        assert name.startswith("autobake.autobake."), name
        assert not name.startswith("blender."), name


def test_skill_slug_is_autobake_for_every_tool():
    from dcc_plugin_autobake.agent._wiring import build_registry

    for spec in build_registry().snapshot_metadata():
        assert spec["skill_slug"] == "autobake"


def test_every_advertised_tool_declares_a_schema():
    from dcc_plugin_autobake.agent._wiring import build_registry

    for spec in build_registry().snapshot_metadata():
        assert spec["arg_schema"], "missing arg_schema for {0}".format(spec["name"])
        assert spec["arg_schema"].get("type") == "object"


# ---------------------------------------------------------------------------
# registration: our own ids, no contest with the host provider
# ---------------------------------------------------------------------------


def test_registration_is_idempotent(host):
    _addon.ensure_registered(host)
    first = _addon.registered_ids()
    _addon.ensure_registered(host)
    assert _addon.registered_ids() == first
    assert _addon.is_registered() is True


def test_we_register_only_our_own_ids(host):
    """Every id this plugin registers is in its own namespace."""
    _addon.ensure_registered(host)
    ids = _addon.registered_ids()
    assert _ui.PANEL_IDNAME in ids
    for operator in _ui.OPERATORS:
        assert operator in ids
    # Nothing from the upstream id set may appear.
    for upstream in _addon.UPSTREAM_OPERATORS:
        assert upstream not in ids
    assert _addon.UPSTREAM_PANEL_IDNAME not in ids


def test_registered_classes_belong_to_this_plugin(host):
    """The acceptance rule: __module__ must be ours for evidence to count."""
    _addon.ensure_registered(host)
    for idname in _addon.registered_ids():
        cls = getattr(host.types, idname, None)
        assert cls is not None, idname
        assert _ui.is_ours(cls), "{0} is defined by {1}".format(
            idname, getattr(cls, "__module__", "?")
        )


def test_coexists_with_a_host_copy_that_owns_the_upstream_ids(host):
    """The Blender provider's 0.3.1 copy holds at.* and VIEW3D_PT_Atobake.

    Both must be able to register: ours under autobake.*, the host's left
    untouched. This is the coexistence decision, asserted rather than assumed.
    """
    host.types.Scene.at_tool = object()
    host.types.Object.at_objtool = object()
    host.ops.at = _types.SimpleNamespace()
    for upstream in _addon.UPSTREAM_OPERATORS:
        setattr(host.ops.at, upstream.partition(".")[2], lambda *a, **k: {"FINISHED"})
    host.types.VIEW3D_PT_Atobake = _HostPanel

    _addon.ensure_registered(host)

    # Ours registered…
    assert host.types.VIEW3D_PT_AutobakeTools is not None
    for operator in _ui.OPERATORS:
        namespace, _, name = operator.partition(".")
        assert hasattr(getattr(host.ops, namespace), name)
    # …and theirs survived, unchanged.
    assert host.types.VIEW3D_PT_Atobake is _HostPanel
    for upstream in _addon.UPSTREAM_OPERATORS:
        assert hasattr(host.ops.at, upstream.partition(".")[2])


def test_unregistering_leaves_the_host_provider_alone(host):
    host.types.Scene.at_tool = object()
    host.types.Object.at_objtool = object()
    host.types.VIEW3D_PT_Atobake = _HostPanel
    _addon.ensure_registered(host)
    assert _addon.unregister(host) is True

    assert host.types.VIEW3D_PT_Atobake is _HostPanel
    # The host's property groups are data the bake helpers need, so they stay.
    assert host.types.Scene.at_tool is not None
    assert _addon.owns_property_groups() is False


def test_our_property_groups_are_removed_when_we_created_them(host):
    assert not hasattr(host.types.Scene, "at_tool")
    _addon.ensure_registered(host)
    assert _addon.owns_property_groups() is True
    assert hasattr(host.types.Scene, "at_tool")
    _addon.unregister(host)
    assert not hasattr(host.types.Scene, "at_tool")


def test_registration_rolls_back_when_a_class_fails(host):
    """A half-registered UI is worse than none: an operator with no panel."""
    original = host.utils.register_class
    calls = {"n": 0}

    def _fail_on_third(cls):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise RuntimeError("register_class refused")
        return original(cls)

    host.utils.register_class = _fail_on_third
    with pytest.raises(RuntimeError):
        _addon.ensure_registered(host)
    assert _addon.is_registered() is False
    # Nothing of ours may be left behind.
    assert not hasattr(host.types, _ui.PANEL_IDNAME)
    for operator in _ui.OPERATORS:
        namespace, _, name = operator.partition(".")
        holder = getattr(host.ops, namespace, None)
        assert holder is None or not hasattr(holder, name)


def test_registering_twice_then_unregistering_is_clean(host):
    _addon.ensure_registered(host)
    _addon.ensure_registered(host)
    assert _addon.unregister(host) is True
    assert _addon.is_registered() is False
    assert _addon.registered_ids() == []


class _HostPanel:
    """Stands in for the host provider's own VIEW3D_PT_Atobake class."""

    bl_idname = "VIEW3D_PT_Atobake"
    bl_label = "Auto-bake Tools"


# ---------------------------------------------------------------------------
# 5.x knob guard
# ---------------------------------------------------------------------------


def test_legacy_knobs_are_reported_per_host(host):
    assert _addon.missing_legacy_knobs(host.scene) == []


def test_guard_is_not_installed_when_upstream_already_works(host, monkeypatch):
    _addon.ensure_registered(host)
    assert _addon.guard_installed() is False


def test_guard_is_installed_on_a_host_without_the_legacy_knobs(new_host):
    _addon.ensure_registered(new_host)
    assert _addon.guard_installed() is True
    # Both entry points must be patched, or the panel button keeps the
    # unguarded original.
    from dcc_plugin_autobake.agent.vendor.autobake_tools.addon.operator import at_create_env
    from dcc_plugin_autobake.agent.vendor.autobake_tools.addon.utility import at_utils

    assert at_utils.create_render_env_n is at_create_env.create_render_env_n


def test_guard_is_removed_on_unregister(new_host):
    _addon.ensure_registered(new_host)
    assert _addon.guard_installed() is True
    _addon.unregister(new_host)
    assert _addon.guard_installed() is False


def test_render_env_survives_a_host_without_the_legacy_knobs(new_host):
    _addon.ensure_registered(new_host)
    _addon.create_render_env(new_host)
    assert new_host.scene.render.engine == "CYCLES"
    assert new_host.scene.cycles.bake_type == "NORMAL"
    assert new_host.scene.render.bake.use_selected_to_active is False


def test_render_env_sets_the_legacy_knobs_when_they_exist(host):
    _addon.ensure_registered(host)
    _addon.create_render_env(host)
    assert host.scene.cycles.feature_set == "SUPPORTED"
    assert host.scene.render.use_bake_multires is False


# ---------------------------------------------------------------------------
# create_environment
# ---------------------------------------------------------------------------


def test_create_environment_prepares_the_object(host, tmp_output):
    host.add_object("Cube")
    host.select("Cube")
    result = autobake.create_environment(image_width=64, image_height=64, bevel_samples=4)

    assert result["ok"] is True
    assert result["environment_prepared"] is True
    assert result["render_engine"] == "CYCLES"
    assert result["image_node_created"] is True
    assert result["image_node_active"] is True
    assert result["provider_type"] == "autobake"
    assert result["registration"]["registered"] is True
    assert _ui.PANEL_IDNAME in result["registration"]["ids"]


def test_create_environment_builds_the_expected_node_network(host, tmp_output):
    host.add_object("Cube")
    host.select("Cube")
    autobake.create_environment()
    nodes = list(host.context.active_object.active_material.node_tree.nodes)
    types = sorted(node.type for node in nodes)
    assert types == [
        "AMBIENT_OCCLUSION",
        "BEVEL",
        "BSDF_DIFFUSE",
        "BSDF_GLOSSY",
        "MATH",
        "OUTPUT_MATERIAL",
        "TEX_IMAGE",
    ]


def test_create_environment_activates_a_named_object(host, tmp_output):
    host.add_object("Cube")
    host.add_object("Sphere")
    host.select("Cube")
    result = autobake.create_environment(object_name="Sphere")
    assert result["object"] == "Sphere"
    assert host.context.active_object.name == "Sphere"


def test_create_environment_rejects_an_unknown_object(host):
    host.add_object("Cube")
    host.select("Cube")
    with pytest.raises(ToolError) as excinfo:
        autobake.create_environment(object_name="Nope")
    assert excinfo.value.code == "object_not_found"


def test_create_environment_rejects_a_non_mesh(host):
    host.add_object("Lamp", obj_type="LIGHT")
    host.select("Lamp")
    with pytest.raises(ToolError) as excinfo:
        autobake.create_environment()
    assert excinfo.value.code == "unsupported_object_type"


def test_create_environment_with_no_active_object(host):
    with pytest.raises(ToolError) as excinfo:
        autobake.create_environment()
    assert excinfo.value.code == "no_active_object"


@pytest.mark.parametrize(
    "argument,value",
    [
        ("image_width", 1),
        ("image_width", 40000),
        ("image_height", 1),
        ("bevel_samples", 1),
        ("bevel_samples", 99),
        ("bevel_radius", 0.0),
        ("bevel_radius", 3.0),
        ("ao_distance", 0.0),
        ("ao_samples", 99),
        ("ao_exponentiation", 0.0),
        ("image_width", "wide"),
    ],
)
def test_create_environment_rejects_out_of_range_values(host, tmp_output, argument, value):
    host.add_object("Cube")
    host.select("Cube")
    with pytest.raises(InvalidArgs):
        autobake.create_environment(**{argument: value})


def test_directx_normal_is_applied_after_the_render_env_helper(host, tmp_output):
    host.add_object("Cube")
    host.select("Cube")
    result = autobake.create_environment(directx_normal=True)
    assert result["settings_applied_to_nodes"].get("directx_normal") is True
    assert host.scene.at_tool.at_dx_normal is True
    assert host.scene.render.bake.normal_g == "NEG_Y"


# ---------------------------------------------------------------------------
# bake tools
# ---------------------------------------------------------------------------


def _prepared(host, **kwargs):
    host.add_object("Cube")
    host.select("Cube")
    autobake.create_environment(**kwargs)
    return host.context.active_object


def test_bake_normal_writes_a_real_png(host, tmp_output):
    _prepared(host, image_width=64, image_height=64)
    result = autobake.bake_normal()

    assert result["ok"] is True
    assert result["map"] == "normal"
    assert result["bake_type"] == "NORMAL"
    assert result["width"] == 64 and result["height"] == 64
    assert result["file_size_bytes"] > 0
    path = result["absolute_path"]
    assert os.path.isfile(path)
    assert os.path.getsize(path) == result["file_size_bytes"]
    with open(path, "rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n"


def test_bake_ao_writes_a_different_file_than_the_normal_map(host, tmp_output):
    _prepared(host, image_width=64, image_height=64)
    normal = autobake.bake_normal()
    ao = autobake.bake_ao()

    assert ao["map"] == "ao"
    assert ao["bake_type"] == "DIFFUSE"
    assert normal["absolute_path"] != ao["absolute_path"]
    assert os.path.isfile(normal["absolute_path"])
    assert os.path.isfile(ao["absolute_path"])


def test_bake_normal_requires_an_environment(host, tmp_output):
    host.add_object("Cube")
    host.select("Cube")
    with pytest.raises(ToolError) as excinfo:
        autobake.bake_normal()
    assert excinfo.value.code == "environment_not_prepared"
    assert excinfo.value.context["upstream_operator"] == "at.create_environment"


def test_bake_ao_requires_an_environment(host, tmp_output):
    host.add_object("Cube")
    host.select("Cube")
    with pytest.raises(ToolError) as excinfo:
        autobake.bake_ao()
    assert excinfo.value.code == "environment_not_prepared"


def test_bake_honours_an_explicit_output_path(host, tmp_path):
    _prepared(host, image_width=32, image_height=32)
    target = tmp_path / "explicit" / "normal.png"
    result = autobake.bake_normal(output_path=str(target))
    assert result["absolute_path"] == os.path.normpath(str(target))
    assert target.is_file()


def test_bake_rejects_a_path_that_escapes(host, tmp_output):
    _prepared(host, image_width=32, image_height=32)
    with pytest.raises(InvalidArgs):
        autobake.bake_normal(output_path="../../escape.png")


def test_bad_output_path_is_reported_before_the_environment_check(host, tmp_output):
    """Argument errors must not be masked by a later precondition.

    The path is the caller's own input, so it is validated first; otherwise a
    typo in output_path reports "environment not prepared" and sends the caller
    looking in the wrong place.
    """
    host.add_object("Cube")
    host.select("Cube")  # deliberately not prepared
    with pytest.raises(InvalidArgs):
        autobake.bake_normal(output_path="../../escape.png")


def test_bake_fails_cleanly_when_the_bake_operator_raises(host, tmp_output):
    _prepared(host, image_width=32, image_height=32)

    def _boom(**_kwargs):
        raise RuntimeError("bake refused")

    host.ops.object.bake = _boom
    with pytest.raises(ToolError) as excinfo:
        autobake.bake_normal()
    assert excinfo.value.code == "bake_failed"


def test_bake_isolates_the_target_selection(host, tmp_output):
    """A leftover non-mesh selection must not abort the bake.

    Real Blender bakes every selected object and refuses the whole call when
    one of them is not a mesh, so a bake started from a viewport with a light
    also selected used to fail even though the target was valid.
    """
    host.add_object("Cube")
    host.add_object("Lamp", obj_type="LIGHT")
    host.select("Cube")
    obj = host.context.active_object
    autobake.create_environment()

    # Simulate the user having a light selected as well.
    host.data.objects["Lamp"].select_set(True)
    assert host.data.objects["Lamp"].selected is True

    result = autobake.bake_normal(output_path=str(tmp_output / "isolated.png"))
    assert result["ok"] is True
    assert os.path.isfile(result["absolute_path"])
    assert host.data.objects["Lamp"].selected is False
    assert host.data.objects["Cube"].selected is True


def test_bake_reports_a_missing_image_node_rather_than_crashing(host, tmp_output):
    obj = _prepared(host, image_width=32, image_height=32)
    obj.active_material.node_tree.nodes.clear()
    with pytest.raises(ToolError) as excinfo:
        autobake.bake_normal()
    assert excinfo.value.code == "no_image_node"


def test_repeated_bakes_get_distinct_images(host, tmp_output):
    _prepared(host, image_width=32, image_height=32)
    first = autobake.bake_normal()
    second = autobake.bake_normal()
    assert first["image"] != second["image"]


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_registration_and_the_panel(new_host):
    new_host.add_object("Cube")
    new_host.select("Cube")
    result = autobake.status()

    assert result["ok"] is True
    assert result["provider_type"] == "autobake"
    assert result["addon_version"] == [1, 1, 5]
    assert result["license"] == "Apache-2.0"
    assert result["registration"]["registered"] is True
    assert result["panel"]["idname"] == _ui.PANEL_IDNAME
    assert result["panel"]["registered"] is True
    assert result["panel"]["owned_by_this_plugin"] is True
    assert result["panel"]["module"].startswith("dcc_plugin_autobake")
    for operator in _ui.OPERATORS:
        assert result["operators"][operator]["owned_by_this_plugin"] is True


def test_status_reports_the_skipped_legacy_knobs_on_5x(new_host):
    result = autobake.status()
    assert result["legacy_knobs_skipped"] == ["feature_set", "use_bake_multires"]


def test_status_reports_no_skipped_knobs_on_4x(host):
    result = autobake.status()
    assert result["legacy_knobs_skipped"] == []


def test_status_does_not_credit_the_host_providers_panel(new_host):
    """The trap: the host's panel looks identical and works while we are dead.

    Status must report it as present but *not ours*, so no acceptance run can
    mistake the Blender provider's panel for this plugin's.
    """
    new_host.types.VIEW3D_PT_Atobake = _HostPanel
    new_host.add_object("Cube")
    new_host.select("Cube")
    result = autobake.status()

    host_panel = result["host_registrations"]["panel"]
    assert host_panel["registered"] is True
    assert host_panel["owned_by_this_plugin"] is False
    assert host_panel["module"] == _HostPanel.__module__
    # And ours is separately reported and attributable.
    assert result["panel"]["owned_by_this_plugin"] is True


def test_status_marks_a_host_operator_it_cannot_verify_as_not_ours(new_host):
    """``at.*`` belongs to the host; it is never evidence for us."""
    import types as _types

    new_host.ops.at = _types.SimpleNamespace(
        create_environment=lambda *a, **k: {"FINISHED"},
        bake=lambda *a, **k: {"FINISHED"},
        bake_ao=lambda *a, **k: {"FINISHED"},
    )
    result = autobake.status()
    for upstream, report in result["host_registrations"]["operators"].items():
        assert report["registered"] is True
        assert report["owned_by_this_plugin"] is False


# ---------------------------------------------------------------------------
# no host at all
# ---------------------------------------------------------------------------


def test_tools_report_host_not_available_outside_blender():
    from dcc_plugin_autobake.agent.tools import _bpy as bpy_module

    bpy_module.reset_for_tests()
    with pytest.raises(ToolError) as excinfo:
        autobake.status()
    assert excinfo.value.code == "host_not_available"
