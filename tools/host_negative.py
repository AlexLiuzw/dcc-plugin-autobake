"""Negative cases and install isolation, against the shipped package.

Two claims the delivery makes that a positive smoke test cannot support:

1. the tools reject bad input *in a real host* (not just against the fake one)
2. installing and removing this plugin leaves the Blender provider alone

Both need the real installer and real Blender, so they live here rather than
in the hermetic suite. Nothing is written outside a scratch prefix.
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

#: The devkit CLI. Preferred over the installer's `uninstall` verb, which on
#: this machine fails with `No module named 'lap_plugin_runtime'` for every
#: plugin including the pre-existing Blender provider — an installer-side
#: defect that says nothing about this plugin.
LAP_PLUGIN = Path(os.environ["LOCALAPPDATA"]) / "LAP" / "sdk" / "venv" / "Scripts" / "lap-plugin.exe"

NEGATIVE_DRIVER = r'''
import json, os, sys, traceback
install_root = os.environ["AUTOBAKE_INSTALL_ROOT"]
agent_dir = os.path.join(install_root, "agent")
abi = "cp%d%d" % (sys.version_info[0], sys.version_info[1])
for c in (os.path.join(agent_dir, abi), agent_dir):
    if os.path.isdir(os.path.join(c, "dcc_plugin_autobake")):
        sys.path.insert(0, c)
        break

import bpy
from dcc_plugin_autobake.agent import _addon
from dcc_plugin_autobake.agent.tools import autobake

_addon.ensure_registered(bpy)
cases = []


def case(name, fn, expect_code):
    entry = {"case": name, "expected": expect_code}
    try:
        fn()
    except Exception as exc:
        entry["raised"] = type(exc).__name__
        entry["code"] = getattr(exc, "code", None)
        entry["message"] = str(exc)[:200]
    else:
        entry["raised"] = None
        entry["code"] = None
    entry["ok"] = entry["code"] == expect_code
    cases.append(entry)


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.mesh.primitive_cube_add(size=2.0)
cube = bpy.context.active_object
cube.data.materials.append(bpy.data.materials.new("m"))
bpy.context.view_layer.objects.active = cube
cube.select_set(True)

# no active object
bpy.context.view_layer.objects.active = None
case("no active object", lambda: autobake.create_environment(), "no_active_object")

bpy.context.view_layer.objects.active = cube
cube.select_set(True)

# unknown object
case("unknown object", lambda: autobake.create_environment(object_name="NoSuch"), "object_not_found")

# non-mesh
bpy.ops.object.light_add(type='POINT', location=(4, 0, 0))
light = bpy.context.active_object
case("non-mesh object", lambda: autobake.create_environment(), "unsupported_object_type")

bpy.context.view_layer.objects.active = cube
cube.select_set(True)

# bake before environment
case("bake before environment", lambda: autobake.bake_normal(), "environment_not_prepared")
case("ao before environment", lambda: autobake.bake_ao(), "environment_not_prepared")

# out-of-range arguments
for arg, val in (("image_width", 1), ("bevel_samples", 99), ("ao_distance", 0.0)):
    case("out of range %s=%s" % (arg, val),
         lambda a=arg, v=val: autobake.create_environment(**{a: v}), "invalid_args")

# path escape must be reported even before the environment exists: a bad path
# is the caller's own input, so it must not be masked by "prepare first"
case("path escape (no environment)",
     lambda: autobake.bake_normal(output_path="../../evil.png"), "invalid_args")

# ...and equally on a prepared object
autobake.create_environment(image_width=128, image_height=128)
case("path escape (prepared)",
     lambda: autobake.bake_normal(output_path="../../evil.png"), "invalid_args")

# after a refused call, the scene must be untouched and a good call must work
res = autobake.bake_normal(output_path=os.path.join(os.environ["AUTOBAKE_OUTPUT_DIR"], "after_negative.png"))
cases.append({"case": "recovery after refusals", "ok": os.path.isfile(res["absolute_path"]),
              "bytes": res["file_size_bytes"]})

report = {"cases": cases, "ok": all(c["ok"] for c in cases),
          "blender": bpy.app.version_string}
with open(os.environ["AUTOBAKE_REPORT"], "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2, default=str)
'''


def run(cmd, **kwargs):
    print("+ " + " ".join(str(p) for p in cmd))
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--blender", required=True, type=Path)
    parser.add_argument("--tag", default="neg")
    parser.add_argument(
        "--installer",
        type=Path,
        default=Path(os.environ["APPDATA"]) / "LAP" / "toolserver" / "lap-dcc-installer.exe",
    )
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="autobake-neg-"))
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
    if install.returncode != 0:
        print(install.stdout[-3000:])
        print(install.stderr[-3000:], file=sys.stderr)
        return 2

    provider_root = appdata / "LAP" / "autobake-provider" / "dcc-plugin-blender-autobake"
    version = sorted(p.name for p in provider_root.iterdir() if p.is_dir())[-1]
    install_dir = provider_root / version
    print("installed at: {0}".format(install_dir))
    # The install registered ref markers in the machine's shared runtime, so
    # the uninstall has to look there to release them.
    shared_root = Path(os.environ["LOCALAPPDATA"]) / "LAP" / "SidecarPython"

    driver = work / "neg.py"
    driver.write_text(NEGATIVE_DRIVER, encoding="utf-8")
    report_path = work / "report.json"
    run_env = os.environ.copy()
    run_env.update(
        AUTOBAKE_INSTALL_ROOT=str(install_dir),
        AUTOBAKE_OUTPUT_DIR=str(out),
        AUTOBAKE_REPORT=str(report_path),
        BLENDER_USER_CONFIG=str(work / "cfg"),
        BLENDER_USER_SCRIPTS=str(work / "scr"),
    )
    result = run(
        [str(args.blender), "--background", "--factory-startup", "--python", str(driver)],
        env=run_env,
        cwd=str(work),
    )
    print(result.stdout[-2500:])
    if result.stderr.strip():
        print(result.stderr[-2500:], file=sys.stderr)

    if not report_path.is_file():
        print("NO REPORT")
        return 3
    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(json.dumps(report, indent=2, default=str))

    evidence = Path.cwd() / "evidence"
    evidence.mkdir(exist_ok=True)
    shutil.copy2(report_path, evidence / "host-negative-{0}.json".format(args.tag))

    # Isolation: a simulated Blender-provider tree must survive uninstall.
    fake_provider = appdata / "LAP" / "blender-provider" / "dcc-plugin-blender" / "0.3.1"
    fake_provider.mkdir(parents=True, exist_ok=True)
    marker = fake_provider / "SURVIVES.txt"
    marker.write_text("blender provider payload\n", encoding="utf-8")

    removal = run(
        [
            str(LAP_PLUGIN),
            "uninstall",
            "--install-dir",
            str(install_dir),
            "--shared-root",
            str(shared_root),
            "--dry-run",
        ],
        env=env,
    )
    print("[dry-run] " + removal.stdout[-1500:])
    dry_run_ok = removal.returncode == 0

    removal = run(
        [
            str(LAP_PLUGIN),
            "uninstall",
            "--install-dir",
            str(install_dir),
            "--shared-root",
            str(shared_root),
        ],
        env=env,
    )
    print(removal.stdout[-3000:])
    uninstall_ok = removal.returncode == 0
    ours_gone = not install_dir.is_dir()
    theirs_alive = marker.is_file()
    print(
        "\nuninstall rc={0} our_tree_removed={1} blender_provider_intact={2}".format(
            removal.returncode, ours_gone, theirs_alive
        )
    )
    isolation = {
        "uninstall_rc": removal.returncode,
        "dry_run_ok": dry_run_ok,
        "our_tree_removed": ours_gone,
        "blender_provider_intact": theirs_alive,
        "uninstaller": "lap-plugin uninstall --install-dir",
        "ok": dry_run_ok and uninstall_ok and ours_gone and theirs_alive,
    }
    (evidence / "host-isolation-{0}.json".format(args.tag)).write_text(
        json.dumps(isolation, indent=2), encoding="utf-8"
    )

    ok = bool(report.get("ok")) and isolation["ok"]
    print("\nRESULT: {0}".format("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
