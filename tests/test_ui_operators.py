"""The panel/operator buttons must run the same code as the MCP tools.

The handoff required that the UI not be a second implementation. These tests
drive the registered operators the way Blender's UI does — ``execute(context)``
— and assert they produce the same effect as calling the leaf tool directly.
"""

from __future__ import annotations

import os

import pytest

from dcc_plugin_autobake.agent import _addon, _ui
from dcc_plugin_autobake.agent.tools import autobake


@pytest.fixture()
def registered(host):
    _addon.ensure_registered(host)
    host.add_object("Cube")
    host.select("Cube")
    return host


def _run_operator(host, full_id, expect=("FINISHED",)):
    """Invoke a registered operator exactly as the UI would."""
    namespace, _, name = full_id.partition(".")
    holder = getattr(host.ops, namespace)
    result = getattr(holder, name)()
    assert result in [set(expect), expect] or result == set(expect), result
    return result


def test_panel_offers_all_three_operators(registered):
    """Every operator button must be reachable from the panel's ``draw``.

    Driven by rendering the layout rather than by grepping source: a renamed
    constant would still read fine in source but produce a dead button.
    """
    calls = []

    class _Column:
        def prop(self, *_args, **_kwargs):
            pass

    class _Box(_Column):
        def __init__(self):
            pass

        def operator(self, idname):
            calls.append(idname)

        def label(self, **_kwargs):
            pass

        def column(self):
            return _Column()

        def box(self):
            return _Box()

        def split(self, **_kwargs):
            return _Box()

    cls = getattr(registered.types, _ui.PANEL_IDNAME)
    panel = cls()
    panel.layout = _Box()
    panel.draw(registered.context)

    for operator in _ui.OPERATORS:
        assert operator in calls, "{0} has no button: {1}".format(operator, calls)


def test_create_environment_button_matches_the_tool(registered, tmp_output):
    _run_operator(registered, _ui.OPERATOR_CREATE_ENV)
    obj = registered.context.active_object
    assert obj.at_objtool.prepare_statement_prop_bool is True
    assert registered.scene.render.engine == "CYCLES"


def test_bake_normal_button_produces_the_same_result_as_the_tool(registered, tmp_output):
    _run_operator(registered, _ui.OPERATOR_CREATE_ENV)

    _run_operator(registered, _ui.OPERATOR_BAKE_NORMAL)
    via_button = registered.bake_calls[-1]
    assert via_button["type"] == "NORMAL"

    # The tool call must reach the same host state.
    result = autobake.bake_normal(output_path=str(tmp_output / "tool.png"))
    via_tool = registered.bake_calls[-1]
    assert via_tool["type"] == via_button["type"]
    assert os.path.isfile(result["absolute_path"])


def test_bake_ao_button_produces_the_same_result_as_the_tool(registered, tmp_output):
    _run_operator(registered, _ui.OPERATOR_CREATE_ENV)
    _run_operator(registered, _ui.OPERATOR_BAKE_AO)
    assert registered.bake_calls[-1]["type"] == "DIFFUSE"

    result = autobake.bake_ao(output_path=str(tmp_output / "ao.png"))
    assert registered.bake_calls[-1]["type"] == "DIFFUSE"
    assert os.path.isfile(result["absolute_path"])


def test_a_button_reports_an_error_instead_of_raising(registered):
    """Blender operators must not raise: they report and cancel."""
    # No environment prepared, so bake_normal refuses.
    result = _run_operator(registered, _ui.OPERATOR_BAKE_NORMAL, expect=("CANCELLED",))
    assert result == {"CANCELLED"}
    assert any(entry["level"] == "ERROR" for entry in registered.reports)
    assert any(
        "environment_not_prepared" in entry["message"] for entry in registered.reports
    )


def test_bake_buttons_poll_for_a_prepared_environment(registered):
    cls = getattr(registered.types, _ui.OPERATOR_BAKE_NORMAL)
    # Not prepared yet.
    assert cls.poll(registered.context) is False
    _run_operator(registered, _ui.OPERATOR_CREATE_ENV)
    assert cls.poll(registered.context) is True


def test_create_environment_polls_for_a_mesh(registered):
    cls = getattr(registered.types, _ui.OPERATOR_CREATE_ENV)
    assert cls.poll(registered.context) is True
    registered.add_object("Lamp", obj_type="LIGHT")
    registered.select("Lamp")
    assert cls.poll(registered.context) is False


def test_panel_poll_requires_a_mesh(registered):
    cls = getattr(registered.types, _ui.PANEL_IDNAME)
    assert cls.poll(registered.context) is True
    registered.add_object("Lamp", obj_type="LIGHT")
    registered.select("Lamp")
    assert cls.poll(registered.context) is False


def test_all_ui_classes_are_attributed_to_this_plugin(registered):
    """``__module__`` is the acceptance evidence; it must be ours."""
    for idname in (_ui.PANEL_IDNAME,) + _ui.OPERATORS:
        cls = getattr(registered.types, idname)
        assert _ui.is_ours(cls), "{0} came from {1}".format(
            idname, getattr(cls, "__module__", "?")
        )
