# dcc-plugin-blender-autobake

LAP DCC provider plugin that exposes **Auto-bake Tools 1.1.5** over MCP:
prepare a Blender mesh for baking, bake the Bevel Shader into a normal map,
bake Ambient Occlusion, and write both to disk.

The plugin is its own provider — `provider_type = "autobake"`, tools at
`autobake.autobake.*`, installed under its own tree. Blender is the host DCC
that performs the bake; it is not the provider identity, and this plugin is
not part of `dcc-plugin-blender`.

## What it provides

| Tool | Upstream operator | Purpose |
|---|---|---|
| `create_environment` | `at.create_environment` | Build the bevel-shader bake environment |
| `bake_normal` | `at.bake` | Bake the normal map and save a PNG |
| `bake_ao` | `at.bake_ao` | Bake Ambient Occlusion and save a PNG |
| `status` | — | Read-only registration / settings report |

Tool semantics, argument ranges and error codes: [`skills/autobake/SKILL.md`](skills/autobake/SKILL.md).

## Supported hosts

Blender **4.2, 4.5, 5.0, 5.2, 5.3** on Windows. The sidecar topology needs
Named Pipes, which is why Windows is the only listed platform.

Each supported release is also listed in the wirer's version table — adding a
version to `config/plugin.toml` alone is not enough, because `--wire auto`
consults the table.

## Install

```powershell
lap-plugin build-all --plugin-dir <this repo>
lap-dcc-installer install plugin --provider autobake --path <the built zip> --wire auto
```

`--wire auto` finds every supported Blender that has been launched at least
once, and plants this plugin's own add-on (`LAP_AutoBake`) plus a startup
shim. It does not touch `LAP_Bridge`, `lap_bootstrap.py`, or
`dcc-whitelist.d/blender.json` — those belong to the Blender provider plugin.

Uninstall:

```powershell
lap-dcc-installer uninstall plugin --provider autobake --plugin-slug dcc-plugin-blender-autobake
```

The install tree is `%APPDATA%\LAP\autobake-provider\dcc-plugin-blender-autobake\<version>\`,
separate from `blender-provider\dcc-plugin-blender`. Removing this plugin
cannot remove the Blender provider.

## Upstream and licensing

Upstream: [GarikDog/autobake_tools](https://github.com/GarikDog/autobake_tools),
tag `v1,1,5` (commit `34f49ab8718fa7c50ef9572ef58130f69642e298`),
**Apache-2.0**, Copyright 2023 Igor Subachev (GarikDog).

The add-on is vendored byte-for-byte at
`src/dcc_plugin_autobake/agent/vendor/autobake_tools/`, together with its
`LICENSE.md` and a `VENDOR-MANIFEST.json` recording the archive hash and the
sha256 of every copied file. `tests/test_vendor_integrity.py` re-hashes the
tree, so an edit to a vendored file fails the suite.

> **Licence note:** the upstream README badge says MIT. `LICENSE.md` and the
> copyright header in every source file say **Apache License 2.0**, and this
> plugin follows those — not the badge. Both are permissive, so the practical
> difference is attribution and the patent grant, not the right to adapt.

Modifications are confined to the adapter layer (`agent/_addon.py`,
`agent/tools/`), which registers the add-on, guards two Cycles knobs Blender
5.x removed, and drives the bake without the add-on's modal operators.

Full attribution and change list: [`NOTICE.md`](NOTICE.md).

## Development

```powershell
python -m pip install -e .[dev]
python -m pytest tests -q
```

The suite is hermetic: no Blender, no network, no installed LAP runtime
required (the runtime-parity tests skip themselves when it is absent, and the
Named Pipe round trip runs only on Windows).

Re-vendoring after an upstream bump:

```powershell
python tools/vendor_upstream.py --archive <path to autobake_tools-1-1-5.zip>
python tools/vendor_upstream.py --archive <path> --check
```

The script refuses an archive whose sha256 is not the pinned one.

## Layout

```text
config/plugin.toml                       provider identity, sidecar pins
scripts/installer/wirer.py               Blender add-on w/ startup shim
scripts/installer/templates/             rendered into each Blender version
skills/autobake/SKILL.md                 tool contract (shipped in the package)
src/dcc_plugin_autobake/agent/           stdlib-only DCC-side agent
src/dcc_plugin_autobake/agent/tools/     the four leaf tools
src/dcc_plugin_autobake/agent/vendor/    byte-identical upstream add-on
tests/                                   hermetic suite (132 tests)
tools/vendor_upstream.py                 reproducible vendoring + verification
```
