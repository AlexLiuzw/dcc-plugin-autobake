---
name: autobake
description: Bake Bevel-Shader normal maps and Ambient Occlusion maps in Blender through the Auto-bake Tools add-on.
---

# Auto-bake Tools (`autobake`)

Four tools that prepare a mesh for baking and produce the two maps the
upstream Auto-bake Tools add-on is built for. The bake runs inside Blender
with Cycles; this plugin is the MCP surface, not a reimplementation.

Provider type is `autobake`, so the wire name of every tool is
`autobake.autobake.<tool>`.

## Tools

| Tool | Upstream | What it does |
|---|---|---|
| `create_environment` | `at.create_environment` | Replaces the material's node tree with the bevel-shader bake network, switches to Cycles, marks the object prepared. |
| `bake_normal` | `at.bake` | Bakes the Bevel Shader into a normal map, saves a PNG, returns its size and dimensions. |
| `bake_ao` | `at.bake_ao` | Bakes Ambient Occlusion into a map, saves a PNG, returns its size and dimensions. |
| `status` | — | Read-only: what is registered in the running host, current settings, skipped 4.x-only knobs. |

## Order of operations

`create_environment` must run first — the two bake tools refuse with
`environment_not_prepared` otherwise. Call `status` first if you want to know
whether a previous call already prepared the object.

```json
{ "provider_id": "<from provider.list_providers>", "tool_name": "autobake.autobake.status", "args": {} }
```

```json
{ "provider_id": "<same>", "tool_name": "autobake.autobake.create_environment", "args": { "object_name": "Cube", "image_width": 2048, "image_height": 2048 } }
```

```json
{ "provider_id": "<same>", "tool_name": "autobake.autobake.bake_normal", "args": { "output_path": "Cube_normal.png" } }
```

## Arguments

`create_environment` — all optional; each override is range-checked against the
upstream property limits and rejected with `invalid_args` outside them.

| Argument | Range / type | Default |
|---|---|---|
| `object_name` | string | the active object |
| `bevel_samples` | integer 2–32 | upstream default (8) |
| `bevel_radius` | number 0.001–0.5 | upstream default (0.02) |
| `image_width`, `image_height` | integer 2–32768 | upstream default (2048) |
| `ao_distance` | number 0.001–10.0 | upstream default (0.1) |
| `ao_samples` | integer 2–32 | upstream default (8) |
| `ao_exponentiation` | number 0.1–10.0 | upstream default (1.0) |
| `directx_normal` | boolean | derived from `render.bake.normal_g` |

`bake_normal` / `bake_ao`

| Argument | Notes |
|---|---|
| `object_name` | string, defaults to the active object |
| `output_path` | PNG path. Relative paths resolve against `LAP_AUTOBAKE_OUTPUT_DIR`, else the process working directory. Absolute paths are honoured. `..` segments are rejected. |

## What the tools return

A write tool returns `ok`, `path` (relative to the output root when it is
inside it), `absolute_path`, `file_size_bytes`, `width`, `height`, the image
datablock name and the settings that were in force. **The file is re-read
from disk before the call returns** — a success response means the PNG
exists with those dimensions, not merely that Blender reported success.

## Errors

| `code` | Meaning / action |
|---|---|
| `environment_not_prepared` | Run `create_environment` for this object first. |
| `no_active_object` | Select a mesh, or pass `object_name`. |
| `object_not_found` | The name is not in this `.blend`. |
| `unsupported_object_type` | Only `MESH` objects can be baked. |
| `invalid_args` | An argument is outside its range; the message names it. |
| `no_image_node` | The material has no image texture node — run `create_environment`. |
| `bake_failed` | Blender refused the bake; the context carries the engine and bake type. |
| `image_save_failed`, `image_not_written`, `image_empty` | The bake ran but the file did not land; check the output directory is writable. |
| `autobake_not_registered` | The vendored add-on is not registered in this host; `status` reports why. |
| `host_not_available` | The tool ran outside Blender. |
| `output_dir_failed` | The output directory could not be created. |

## Host notes

* The bake environment needs **Cycles**. The engine is switched automatically.
* Blender 5.x removed two Cycles knobs upstream reads (`feature_set`,
  `use_bake_multires`). The plugin skips exactly those and reports them as
  `legacy_knobs_skipped`; everything else the upstream helper does is
  unchanged.
* `create_environment` clears the active material's node tree — that is
  upstream behaviour, and it is destructive to existing shading. Bake on a
  copy if the original material matters.
* The upstream add-on's own panel (**Auto-bake Tools** in the 3D View
  sidebar) stays registered and usable; the tools drive the same helpers it
  does.
