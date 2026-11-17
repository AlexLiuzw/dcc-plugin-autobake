"""Read-only coexistence check against whatever the machine already has wired.

The acceptance run found that with the Blender provider 0.3.1 loaded, the
`VIEW3D_PT_Atobake` panel belongs to *that* plugin, and its `at.*` operators
work whether or not this plugin exists. This script proves the fix from the
other side: load the host provider as the machine has it, then register this
plugin's UI, and show that

* both panels exist simultaneously under different ids,
* each is attributed to its own plugin by ``__module__``,
* unregistering ours leaves the host's panel and operators untouched.

It only launches Blender and reads state. It installs nothing, wires nothing,
and does not stop any process.
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
import json, os, sys, traceback

report = {"ok": False}
try:
    import bpy
    report["blender"] = bpy.app.version_string

    def panel_state(idname):
        cls = getattr(bpy.types, idname, None)
        if cls is None:
            return {"present": False}
        return {
            "present": True,
            "idname": idname,
            "module": getattr(cls, "__module__", None),
            "label": getattr(cls, "bl_label", None),
        }

    # 1. what the machine already has
    report["before"] = {
        "host_panel": panel_state("VIEW3D_PT_Atobake"),
        "ops_at": {
            name: hasattr(getattr(bpy.ops, "at", None), name)
            for name in ("create_environment", "bake", "bake_ao")
        },
        "enabled_addons": sorted(
            name for name in bpy.context.preferences.addons.keys()
            if "LAP" in name.upper()
        ),
    }

    # 2. register this plugin's UI on top
    for candidate in os.environ["AUTOBAKE_AGENT_PATHS"].split(os.pathsep):
        if candidate and os.path.isdir(candidate):
            sys.path.insert(0, candidate)
            break
    from dcc_plugin_autobake.agent import _addon, _ui

    _addon.ensure_registered(bpy)
    report["ours"] = {
        "ids": _addon.registered_ids(),
        "panel": panel_state(_ui.PANEL_IDNAME),
    }

    # 3. both must now exist, under different ids, each attributed correctly
    after_host = panel_state("VIEW3D_PT_Atobake")
    ours = panel_state(_ui.PANEL_IDNAME)
    report["after"] = {"host_panel": after_host, "our_panel": ours}

    problems = []
    if not ours["present"]:
        problems.append("our panel did not register")
    if os.environ.get("AUTOBAKE_EXPECT_HOST") == "1" and not after_host["present"]:
        problems.append("host panel disappeared when we registered")
    if ours["present"] and after_host["present"]:
        if ours["idname"] == after_host["idname"]:
            problems.append("panels share one idname")
        if ours["module"] == after_host["module"]:
            problems.append("panels resolved to the same module")
    if ours["present"] and not (ours["module"] or "").startswith("dcc_plugin_autobake"):
        problems.append("our panel is attributed to %r" % ours["module"])
    report["problems"] = problems

    # 4. unregistering must not disturb the host
    _addon.unregister(bpy)
    report["after_unregister"] = {
        "host_panel": panel_state("VIEW3D_PT_Atobake"),
        "our_panel": panel_state(_ui.PANEL_IDNAME),
        "ops_at": {
            name: hasattr(getattr(bpy.ops, "at", None), name)
            for name in ("create_environment", "bake", "bake_ao")
        },
    }
    if report["after_unregister"]["our_panel"]["present"]:
        problems.append("our panel survived unregister")
    if os.environ.get("AUTOBAKE_EXPECT_HOST") == "1":
        if not report["after_unregister"]["host_panel"]["present"]:
            problems.append("unregistering us removed the host panel")

    report["ok"] = not problems
except Exception:
    report["fatal"] = traceback.format_exc()

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
    parser.add_argument("--tag", default="coexist")
    parser.add_argument("--expect-host", action="store_true", default=True)
    parser.add_argument("--no-host", dest="expect_host", action="store_false")
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="autobake-coexist-"))
    # Install into a scratch prefix so nothing on the machine changes, but let
    # Blender use its *real* config so the host provider loads as it normally does.
    appdata = work / "appdata"
    env = os.environ.copy()
    env["APPDATA"] = str(appdata)
    installer = Path(os.environ["APPDATA"]) / "LAP" / "toolserver" / "lap-dcc-installer.exe"
    install = run(
        [str(installer), "install", "plugin", "--provider", "autobake",
         "--plugin-slug", "dcc-plugin-blender-autobake", "--path", str(args.package),
         "--wire", "none", "--no-service"],
        env=env,
    )
    if install.returncode != 0:
        print(install.stdout[-2000:])
        print(install.stderr[-2000:], file=sys.stderr)
        return 2

    root = appdata / "LAP" / "autobake-provider" / "dcc-plugin-blender-autobake"
    version = sorted(p.name for p in root.iterdir() if p.is_dir())[-1]
    agent_dir = root / version / "agent"

    driver = work / "coexist.py"
    driver.write_text(DRIVER, encoding="utf-8")
    report_path = work / "report.json"
    run_env = os.environ.copy()
    run_env.update(
        AUTOBAKE_AGENT_PATHS=os.pathsep.join(
            [str(agent_dir / "cp313"), str(agent_dir / "cp311"), str(agent_dir)]
        ),
        AUTOBAKE_REPORT=str(report_path),
        AUTOBAKE_EXPECT_HOST="1" if args.expect_host else "0",
    )

    result = run(
        [str(args.blender), "--background", "--python", str(driver)],
        env=run_env,
        cwd=str(work),
    )
    print(result.stdout[-3000:])
    if result.stderr.strip():
        print(result.stderr[-2000:], file=sys.stderr)
    if not report_path.is_file():
        print("NO REPORT")
        return 3

    report = json.loads(report_path.read_text(encoding="utf-8"))
    print(json.dumps(report, indent=2, default=str))
    evidence = Path.cwd() / "evidence"
    evidence.mkdir(exist_ok=True)
    shutil.copy2(report_path, evidence / "host-coexist-{0}.json".format(args.tag))
    print("\nRESULT: {0}".format("PASS" if report.get("ok") else "FAIL"))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
