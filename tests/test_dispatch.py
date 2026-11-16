"""Registry + main-thread dispatch: what the sidecar's calls actually reach."""

from __future__ import annotations

import threading
import time

import pytest

from dcc_plugin_autobake.agent._dispatch import (
    DispatchTimeout,
    MainThreadDispatcher,
    MainThreadTicker,
    ToolNotFound,
    ToolRegistry,
    build_executor,
    create_blender_invoker,
)


def _registry():
    registry = ToolRegistry()
    registry.register(
        name="autobake.autobake.echo",
        fn=lambda value=0: {"ok": True, "value": value},
        skill_slug="autobake",
        arg_schema={"type": "object", "properties": {"value": {"type": "integer"}}},
    )
    return registry


def test_registry_rejects_an_unknown_short_name():
    registry = _registry()
    with pytest.raises(ToolNotFound) as excinfo:
        registry.get("autobake.autobake.nope")
    assert excinfo.value.code == "tool_not_found"


def test_snapshot_metadata_matches_the_hello_shape():
    spec = _registry().snapshot_metadata()[0]
    assert set(spec) == {
        "name",
        "skill_slug",
        "requires_main_thread",
        "timeout_seconds",
        "arg_schema",
    }


def test_snapshot_metadata_is_sorted_for_a_stable_wire_payload():
    registry = _registry()
    registry.register(name="autobake.autobake.aaa", fn=lambda: None, skill_slug="autobake")
    names = [spec["name"] for spec in registry.snapshot_metadata()]
    assert names == sorted(names)


def test_executor_runs_the_tool_and_forwards_arguments():
    execute = build_executor(_registry(), MainThreadDispatcher())
    assert execute("autobake.autobake.echo", {"value": 7}, 5.0) == {"ok": True, "value": 7}


def test_executor_propagates_the_tools_own_error():
    from dcc_plugin_autobake.agent.tools._errors import ToolError

    registry = ToolRegistry()

    def _fail():
        raise ToolError("nope", code="nope", context={"detail": 1})

    registry.register(name="autobake.autobake.fail", fn=_fail, skill_slug="autobake")
    execute = build_executor(registry, MainThreadDispatcher())
    with pytest.raises(ToolError) as excinfo:
        execute("autobake.autobake.fail", {}, 5.0)
    assert excinfo.value.code == "nope"


def test_dispatcher_hops_to_the_invoker_from_another_thread():
    posted = []
    run_here = threading.Event()

    class _Invoker:
        def post(self, thunk):
            posted.append(thunk)

    dispatcher = MainThreadDispatcher(
        invoker=_Invoker(), is_main_thread=lambda: False
    )

    result = {}

    def _worker():
        result["value"] = dispatcher.dispatch(lambda: "done", {}, 5.0)

    worker = threading.Thread(target=_worker)
    worker.start()

    deadline = time.time() + 5
    while not posted and time.time() < deadline:
        time.sleep(0.01)
    assert posted, "the thunk was never posted to the invoker"
    posted[0]()  # stand in for the host's main-loop tick
    worker.join(timeout=5)
    assert result["value"] == "done"


def test_dispatcher_times_out_when_the_main_thread_never_runs_the_thunk():
    class _DeadInvoker:
        def post(self, thunk):
            return None

    dispatcher = MainThreadDispatcher(invoker=_DeadInvoker(), is_main_thread=lambda: False)
    with pytest.raises(DispatchTimeout):
        dispatcher.dispatch(lambda: None, {}, 0.05)


def test_dispatcher_reraises_the_tools_exception_on_the_caller_thread():
    class _Invoker:
        def post(self, thunk):
            thunk()

    dispatcher = MainThreadDispatcher(invoker=_Invoker(), is_main_thread=lambda: False)

    def _fail():
        raise RuntimeError("host refused")

    with pytest.raises(RuntimeError, match="host refused"):
        dispatcher.dispatch(_fail, {}, 5.0)


def test_invoker_falls_back_to_inline_without_bpy_timers():
    import types

    fake = types.SimpleNamespace(app=types.SimpleNamespace())
    invoker, is_main = create_blender_invoker(fake)
    assert invoker is None
    assert is_main() is True


def test_invoker_uses_bpy_timers_when_present(host):
    invoker, is_main = create_blender_invoker(host)
    assert invoker is not None
    assert is_main() is True

    called = []
    invoker.post(lambda: called.append("ran"))
    assert host.app.timers.run_all() == 1
    assert called == ["ran"]


def test_ticker_reports_a_recent_tick():
    ticker = MainThreadTicker()
    ticker.tick()
    assert ticker.idle_seconds() < 1.0
    assert ticker.last_tick_ns() > 0


def test_dispatch_updates_the_ticker_used_by_the_heartbeat():
    ticker = MainThreadTicker()
    before = ticker.last_tick_ns()
    MainThreadDispatcher(ticker=ticker).dispatch(lambda: None, {}, 1.0)
    assert ticker.last_tick_ns() >= before
