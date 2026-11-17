# NOTICE — third-party components and modifications

## Auto-bake Tools 1.1.5

This plugin adapts an existing Blender add-on. The add-on is redistributed
here unmodified, inside `src/dcc_plugin_autobake/agent/vendor/autobake_tools/`.

| Field | Value |
|---|---|
| Project | Auto-bake Tools |
| Author | Igor Subachev (GarikDog) |
| Copyright | Copyright 2023 Igor Subachev (GarikDog) |
| Upstream | https://github.com/GarikDog/autobake_tools |
| Tag | `v1,1,5` |
| Commit | `34f49ab8718fa7c50ef9572ef58130f69642e298` |
| Licence | **Apache License, Version 2.0** (see `agent/vendor/autobake_tools/LICENSE.md`) |
| Archive | `autobake_tools-1-1-5.zip`, 11260882 bytes |
| Archive sha256 | `53df706df88287aa5b82371cd08af70a7a2f7413953129a1e7dfba2faa30bf91` |

### Licence discrepancy (upstream, not introduced here)

The upstream `README.md` carries an **MIT** badge. The `LICENSE.md` in the same
archive, and the copyright header on every source file, state **Apache
License 2.0**. This plugin follows `LICENSE.md` and the source headers: the
vendored copy is Apache-2.0, `LICENSE.md` is redistributed unchanged, and no
file is relicensed. The badge is recorded in the vendored
`UPSTREAM-README.md` exactly as upstream ships it, so the discrepancy stays
visible rather than being silently resolved.

Both licences permit adaptation and redistribution; Apache-2.0 additionally
requires the licence and notices to travel with the code (done here, and in
`VENDOR-MANIFEST.json`) and grants an express patent licence.

### What was vendored

Every Python source file of the add-on, plus `LICENSE.md` and the upstream
`README.md`. The archive's `images/` tree (about 11 MB of demo GIFs) is
documentation only and is **not** redistributed; nothing in the add-on loads
it. `VENDOR-MANIFEST.json` lists the sha256 of each copied file so any
deviation is detectable — `tests/test_vendor_integrity.py` enforces it, and
`tools/vendor_upstream.py --check` reproduces it.

### Modifications

No vendored file was edited. All changes live in the adapter layer
(`src/dcc_plugin_autobake/agent/_addon.py` and
`src/dcc_plugin_autobake/agent/tools/autobake.py`):

1. **Registration.** The adapter calls the add-on's own `register()` /
   `unregister()` and, if another copy of the same add-on already owns the
   operator ids, adopts that registration instead of raising. It never
   unregisters classes it does not own.
2. **Blender 5.x compatibility.** `at_utils.create_render_env_n` (and the copy
   `at_create_env` imported by name) reads `CyclesRenderSettings.feature_set`
   and `RenderSettings.use_bake_multires`, both removed in Blender 5.x. The
   adapter routes both entry points through `_addon.create_render_env`, which
   runs the same sequence and skips exactly the setters whose attribute is
   absent. On 4.2 / 4.5, where both exist, the guard is not installed at all.
3. **Non-modal baking.** Upstream's bake operators start an `INVOKE_DEFAULT`
   modal job and then open a new window from a timer. The leaf tools run the
   same helper sequence and call `bpy.ops.object.bake(...)` synchronously, then
   save the result and verify the file on disk. The upstream operators remain
   registered for interactive use.
4. **Arguments.** Numeric overrides are validated against the upstream property
   ranges before being applied, and re-applied to the shader nodes after the
   node tree exists (upstream's setters silently no-op before that point).
5. **Output paths.** Bake output goes to a caller-supplied path (or the output
   root), not to whatever the image window would have shown.

## LAP platform components

`lap-plugin-runtime` and `lap-plugin-dcc-contract-base` are consumed as
build-time dependencies from the internal LAP monorepo. They are not vendored
or redistributed here.
