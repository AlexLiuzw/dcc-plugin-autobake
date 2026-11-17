"""Install the built package into real Blender and drive the real boot chain.

This replaces the earlier smoke test, which imported ``tools.autobake``
directly and therefore passed while ``agent.start()`` was broken. Here the
driver only does what the shipped add-on shell does:

    addon module -> register() -> agent.start(plugin_toml) -> orchestrator
                 -> Named Pipe listener -> sidecar Popen

and only *after* the handle exists does it call the tools through the agent's
own registry, i.e. the path a sidecar ``tool.invoke`` takes. Nothing is
imported from ``dcc_plugin_autobake`` before ``start()`` except the add-on
module itself.

Isolation: ``--wire none``, a scratch APPDATA prefix, and ``--factory-startup``
so the machine's real install trees, the Blender provider plugin and the user's
preferences are never touched.

Usage:
    python tools/host_boot_check.py --package <zip> --blender <blender.exe> --tag 52
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DRIVER = r'''
"""Runs inside Blender. Boots the plugin the way the shipped shell does."""
import json
import os
import sys
import threading
import time
import traceback

install_root = os.environ["AUTOBAKE_INSTALL_ROOT"]
output_dir = os.environ["AUTOBAKE_OUTPUT_DIR"]
agent_dir = os.path.join(install_root, "agent")
abi = "cp%d%d" % (sys.version_info[0], sys.version_info[1])
for candidate in (os.path.join(agent_dir, abi), agent_dir):
    if os.path.isdir(os.path.join(candidate, "dcc_plugin_autobake")):
        sys.path.insert(0, candidate)
        break

plugin_toml = os.path.join(install_root, "plugin.toml")
report = {"steps": [], "ok": False}


def step(name, fn):
    try:
        value = fn()
        report["steps"].append({"step": name, "ok": True, "value": value})
        return value
    except Exception as exc:
        report["steps"].append({
            "step": name,
            "ok": False,
            "error": "%s: %s" % (type(exc).__name__, exc),
            "traceback": traceback.format_exc(),
        })
        raise


handle = None
try:
    import bpy
    report["blender"] = bpy.app.version_string
    report["python"] = sys.version.split()[0]

    # 1. the add-on shell's own entry point: agent.start(plugin_toml)
    from dcc_plugin_autobake.agent import start

    def _boot():
        global handle
        handle = start(plugin_toml, plugin_id=os.environ.get("AUTOBAKE_PLUGIN_ID"))
        return {"state": handle.state, "slug": handle.config.plugin_slug}

    step("agent.start", _boot)

    # 2. wait for the sidecar handshake, which is what proves the sidecar was
    #    spawned AND connected — the two things 0.1.1 never reached.
    def _await_online(timeout=45.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(handle, "state", None) == "online":
                return "online"
            time.sleep(0.25)
        raise AssertionError(
            "agent state stayed %r for %.0fs (sidecar never completed HELLO)"
            % (getattr(handle, "state", None), timeout)
        )

    step("sidecar handshake", _await_online)

    # 3. children of this process: a running sidecar must be visible in the OS
    def _sidecar_process():
        import subprocess as sp

        out = sp.run(
            ["wmic", "process", "where", "name='lap-autobake-sidecar.exe'",
             "get", "ProcessId,CommandLine", "/format:list"],
            capture_output=True, text=True,
        ).stdout
        pids = [line.split("=", 1)[1].strip() for line in out.splitlines()
                if line.startswith("ProcessId=") and line.split("=", 1)[1].strip()]
        if not pids:
            raise AssertionError("no lap-autobake-sidecar.exe process is running")
        return {"pids": pids}

    step("sidecar process alive", _sidecar_process)

    # 4. drive the tools through the agent's own registry — the same executor a
    #    sidecar tool.invoke lands on, NOT a direct import of the tool module.
    from dcc_plugin_autobake.agent._dispatch import (
        MainThreadDispatcher, MainThreadTicker, build_executor, create_blender_invoker,
    )
    from dcc_plugin_autobake.agent._wiring import build_registry as _build

    registry = _build()
    invoker, is_main = create_blender_invoker(bpy)
    execute = build_executor(
        registry, MainThreadDispatcher(invoker=invoker, is_main_thread=is_main, ticker=MainThreadTicker())
    )
    report["registry_ids"] = registry.names()

    def _call(tool, args=None):
        return execute(tool, args or {}, 300.0)

    step("registry has the four tools", lambda: report["registry_ids"])

    # 5. a real bake through the registry
    def _scene():
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.mesh.primitive_cube_add(size=2.0)
        obj = bpy.context.active_object
        mat = bpy.data.materials.new("boot_material")
        mat.use_nodes = True
        obj.data.materials.append(mat)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        return obj.name

    step("scene", _scene)
    step("create_environment", lambda: _call(
        "autobake.autobake.create_environment",
        {"object_name": "Cube", "image_width": 256, "image_height": 256},
    ))
    normal = step("bake_normal", lambda: _call(
        "autobake.autobake.bake_normal",
        {"output_path": os.path.join(output_dir, "boot_normal.png")},
    ))
    ao = step("bake_ao", lambda: _call(
        "autobake.autobake.bake_ao",
        {"output_path": os.path.join(output_dir, "boot_ao.png")},
    ))

    # 6. attribution: our own ids, by __module__
    status = step("status", lambda: _call("autobake.autobake.status"))

    def _attribution():
        problems = []
        if not status["panel"]["owned_by_this_plugin"]:
            problems.append("panel not attributed to this plugin")
        for op, info in status["operators"].items():
            if not info["owned_by_this_plugin"]:
                problems.append("%s not attributed" % op)
        if status["host_registrations"]["panel"]["owned_by_this_plugin"]:
            problems.append("host panel wrongly claimed as ours")
        return {"problems": problems, "panel_module": status["panel"]["module"],
                "panel_idname": status["panel"]["idname"],
                "host_panel_module": status["host_registrations"]["panel"]["module"]}

    attribution = step("attribution", _attribution)
    if attribution["problems"]:
        raise AssertionError("; ".join(attribution["problems"]))

    # 7. the PNGs are real files matching what the tools reported
    def _verify(result, name):
        path = result["absolute_path"]
        info = {
            "path": name,
            "exists": os.path.isfile(path),
            "bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
            "reported_bytes": result["file_size_bytes"],
            "width": result["width"],
            "height": result["height"],
        }
        if not info["exists"] or info["bytes"] != info["reported_bytes"] or info["bytes"] <= 0:
            raise AssertionError("bad file for %s: %r" % (name, info))
        return info

    report["files"] = {
        "normal": step("verify_normal", lambda: _verify(normal, "boot_normal.png")),
        "ao": step("verify_ao", lambda: _verify(ao, "boot_ao.png")),
    }

    report["ok"] = True
except Exception:
    report["fatal"] = traceback.format_exc()
finally:
    if handle is not None:
        try:
            handle.shutdown(timeout=8.0)
            report["shutdown"] = "clean"
        except Exception:
            report["shutdown"] = traceback.format_exc()

with open(os.environ["AUTOBAKE_REPORT"], "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2, default=str)
print("BOOT_REPORT=" + os.environ["AUTOBAKE_REPORT"])
'''


def run(cmd, **kwargs):
    print("+ " + " ".join(str(p) for p in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--blender", required=True, type=Path)
    parser.add_argument("--tag", default="boot")
    parser.add_argument(
        "--installer",
        type=Path,
        default=Path(os.environ["APPDATA"]) / "LAP" / "toolserver" / "lap-dcc-installer.exe",
    )
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="autobake-boot-"))
    appdata = work / "appdata"
    out = work / "out"
    out.mkdir(parents=True)
    env = os.environ.copy()
    env["APPDATA"] = str(appdata)

    install = run(
        [str(args.installer), "install", "plugin", "--provider", "autobake",
         "--plugin-slug", "dcc-plugin-blender-autobake", "--path", str(args.package),
         "--wire", "none", "--no-service"],
        env=env,
    )
    print(install.stdout[-2500:])
    if install.returncode != 0:
        print(install.stderr[-2500:], file=sys.stderr)
        return 2

    provider_root = appdata / "LAP" / "autobake-provider" / "dcc-plugin-blender-autobake"
    version = sorted(p.name for p in provider_root.iterdir() if p.is_dir())[-1]
    install_dir = provider_root / version
    print("installed: {0}".format(install_dir))

    driver = work / "boot.py"
    driver.write_text(DRIVER, encoding="utf-8")
    report_path = work / "report.json"

    run_env = os.environ.copy()
    run_env.update(
        AUTOBAKE_INSTALL_ROOT=str(install_dir),
        AUTOBAKE_OUTPUT_DIR=str(out),
        AUTOBAKE_REPORT=str(report_path),
        AUTOBAKE_PLUGIN_ID="AUTOBAKE" + version,
        BLENDER_USER_CONFIG=str(work / "cfg"),
        BLENDER_USER_SCRIPTS=str(work / "scr"),
        # The sidecar must be able to reach the Tool Server; keep the real one.
    )

    result = run(
        [str(args.blender), "--background", "--factory-startup", "--python", str(driver)],
        env=run_env,
        cwd=str(work),
    )
    print(result.stdout[-6000:])
    if result.stderr.strip():
        print("--- stderr ---")
        print(result.stderr[-4000:])

    if not report_path.is_file():
        print("NO REPORT")
        return 3
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print("\n=== report ===")
    print(json.dumps(report, indent=2, default=str))

    evidence = Path.cwd() / "evidence"
    evidence.mkdir(exist_ok=True)
    shutil.copy2(report_path, evidence / "host-boot-{0}.json".format(args.tag))

    ok = bool(report.get("ok")) and all(s["ok"] for s in report["steps"])
    print("\nRESULT: {0}".format("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
