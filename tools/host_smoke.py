"""Install the built package into an isolated Blender and exercise the real tools.

This is the answer to "why was nothing tested". The hermetic suite proves the
adapter logic, but it uses a fake host; this script installs the *shipped zip*
and drives the real add-on inside real Blender.

It is deliberately isolated: it wires nothing into the machine-wide install
trees, uses `--wire none`, points the install at a scratch directory, and
launches Blender with `--factory-startup` so the user's preferences and the
Blender provider plugin are untouched.

Usage:
    python tools/host_smoke.py --package <zip> --blender <blender.exe> [--version-tag 5.2]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

DRIVER = r'''
"""Runs inside Blender: register the vendored add-on and drive the four tools."""
import json
import os
import sys
import traceback

install_root = os.environ["AUTOBAKE_INSTALL_ROOT"]
output_dir = os.environ["AUTOBAKE_OUTPUT_DIR"]
agent_dir = os.path.join(install_root, "agent")
abi = "cp%d%d" % (sys.version_info[0], sys.version_info[1])
for candidate in (os.path.join(agent_dir, abi), agent_dir):
    if os.path.isdir(os.path.join(candidate, "dcc_plugin_autobake")):
        sys.path.insert(0, candidate)
        break

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


try:
    import bpy

    from dcc_plugin_autobake.agent import _addon
    from dcc_plugin_autobake.agent.tools import autobake

    report["blender"] = bpy.app.version_string
    report["python"] = sys.version.split()[0]

    # 1. registration
    step("register", lambda: _addon.ensure_registered(bpy))
    step("registration_owner", lambda: _addon.registration_owner())
    report["guard_installed"] = _addon.guard_installed()

    # 2. a fresh scene with a mesh that has a real material
    def _prepare_scene():
        bpy.ops.wm.read_factory_settings(use_empty=True)
        bpy.ops.mesh.primitive_cube_add(size=2.0, location=(0, 0, 0))
        obj = bpy.context.active_object
        mat = bpy.data.materials.new("smoke_material")
        mat.use_nodes = True
        obj.data.materials.append(mat)
        bpy.context.view_layer.objects.active = obj
        obj.select_set(True)
        return {"object": obj.name, "material": obj.active_material.name}

    step("scene", _prepare_scene)

    # 3. create_environment
    step("create_environment", lambda: autobake.create_environment(
        object_name="Cube", image_width=256, image_height=256,
        bevel_samples=4, bevel_radius=0.02))

    # 4. normal bake
    normal = step("bake_normal", lambda: autobake.bake_normal(
        output_path=os.path.join(output_dir, "smoke_normal.png")))

    # 5. AO bake
    ao = step("bake_ao", lambda: autobake.bake_ao(
        output_path=os.path.join(output_dir, "smoke_ao.png")))

    # 6. status
    status = step("status", lambda: autobake.status())
    report["status"] = {
        "panel_registered": status["panel"]["registered"],
        "operators": status["operators"],
        "registration_owner": status["registration"]["owner"],
        "legacy_knobs_skipped": status["legacy_knobs_skipped"],
    }

    # 7. verify the PNGs exist and match the reported dimensions
    def _check(field, expected):
        path = expected["absolute_path"]
        info = {
            "path": path,
            "exists": os.path.isfile(path),
            "bytes": os.path.getsize(path) if os.path.isfile(path) else 0,
            "width": expected["width"],
            "height": expected["height"],
            "reported_bytes": expected["file_size_bytes"],
        }
        if not info["exists"]:
            raise AssertionError("no file at " + path)
        if info["bytes"] != info["reported_bytes"]:
            raise AssertionError("size mismatch for " + path)
        if info["bytes"] <= 0:
            raise AssertionError("empty file at " + path)
        return info

    report["files"] = {
        "normal": step("verify_normal_png", lambda: _check("normal", normal)),
        "ao": step("verify_ao_png", lambda: _check("ao", ao)),
    }

    report["ok"] = True
except Exception:
    report["fatal"] = traceback.format_exc()

out = os.environ["AUTOBAKE_REPORT"]
with open(out, "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2, default=str)
print("SMOKE_REPORT=" + out)
'''


def run(cmd, **kwargs):
    print("+ " + " ".join(str(part) for part in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--blender", required=True, type=Path)
    parser.add_argument("--tag", default="smoke")
    parser.add_argument("--installer", type=Path, default=None)
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="autobake-smoke-"))
    install_root = work / "install"
    output_dir = work / "out"
    output_dir.mkdir(parents=True)

    print("work dir: {0}".format(work))

    installer = args.installer or (
        Path(os.environ["APPDATA"]) / "LAP" / "toolserver" / "lap-dcc-installer.exe"
    )

    # Install the shipped zip into a scratch prefix, wiring nothing.
    env = os.environ.copy()
    env["APPDATA"] = str(work / "appdata")

    result = run(
        [
            str(installer),
            "install",
            "plugin",
            "--provider",
            "autobake",
            "--plugin-slug",
            "dcc-plugin-blender-autobake",
            "--path",
            str(args.package),
            "--wire",
            "none",
            "--no-service",
        ],
        env=env,
    )
    print(result.stdout[-4000:])
    if result.returncode != 0:
        print(result.stderr[-4000:], file=sys.stderr)
        print("INSTALL FAILED")
        return 2

    installed = work / "appdata" / "LAP" / "autobake-provider" / "dcc-plugin-blender-autobake"
    if not installed.is_dir():
        print("expected install tree missing: {0}".format(installed), file=sys.stderr)
        return 2
    version_dirs = sorted(p.name for p in installed.iterdir() if p.is_dir())
    print("installed versions: {0}".format(version_dirs))
    install_dir = installed / version_dirs[-1]

    driver = work / "drive.py"
    driver.write_text(DRIVER, encoding="utf-8")
    report_path = work / "report.json"

    run_env = os.environ.copy()
    run_env["AUTOBAKE_INSTALL_ROOT"] = str(install_dir)
    run_env["AUTOBAKE_OUTPUT_DIR"] = str(output_dir)
    run_env["AUTOBAKE_REPORT"] = str(report_path)
    # Keep Blender out of the user's real config.
    run_env["BLENDER_USER_CONFIG"] = str(work / "blender-config")
    run_env["BLENDER_USER_SCRIPTS"] = str(work / "blender-scripts")

    result = run(
        [
            str(args.blender),
            "--background",
            "--factory-startup",
            "--python",
            str(driver),
        ],
        env=run_env,
        cwd=str(work),
    )
    print(result.stdout[-6000:])
    if result.stderr.strip():
        print("--- stderr ---")
        print(result.stderr[-4000:])

    if not report_path.is_file():
        print("NO REPORT — Blender did not reach the driver")
        return 3

    report = json.loads(report_path.read_text(encoding="utf-8"))
    print("\n=== report ===")
    print(json.dumps(report, indent=2, default=str))

    keep = Path.cwd() / "evidence"
    keep.mkdir(exist_ok=True)
    target = keep / "host-smoke-{0}.json".format(args.tag)
    shutil.copy2(report_path, target)
    print("\nsaved: {0}".format(target))

    ok = bool(report.get("ok")) and all(s["ok"] for s in report["steps"])
    print("\nRESULT: {0}".format("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
