"""Stdlib-only helpers shared by every agent module.

The agent runs inside Blender's embedded Python, so this module may only
import the standard library. It centralises the three things the rest of
the agent needs from the environment:

* :data:`AGENT_VERSION` — the agent↔sidecar wire version. Bumped whenever
  the frame handling changes, so a mixed agent/sidecar pair fails at the
  HELLO handshake instead of mis-parsing frames later.
* :data:`ENV_LAP_IPC_URL` / :data:`ENV_LAP_PLUGIN_TOML` — the two env vars
  the sidecar process reads at boot. Duplicated here (rather than imported
  from ``lap_plugin_runtime``) because the agent must stay importable in a
  host that has no LAP packages on ``sys.path``.
* :func:`read_toml` — ``plugin.toml`` parsing.
"""

from __future__ import annotations

import sys

#: Agent↔sidecar wire version (not the plugin version).
AGENT_VERSION = "0.1.0"

#: The sidecar connects to the pipe named by this env var.
ENV_LAP_IPC_URL = "LAP_IPC_URL"

#: Path to ``plugin.toml`` handed to the sidecar process.
ENV_LAP_PLUGIN_TOML = "LAP_PLUGIN_TOML"

#: Business-plugin slug handed to the sidecar process.
ENV_LAP_PLUGIN_SLUG = "LAP_PLUGIN_SLUG"


class TomlError(ValueError):
    """``plugin.toml`` could not be parsed by any available reader."""


def read_toml(path):
    """Load *path* and return a nested ``dict``.

    Blender 4.2 and newer embed CPython 3.11+, which ships :mod:`tomllib`,
    so the fallback parser is only a safety net for older embed builds and
    for the plain-CPython unit tests. The fallback understands exactly the
    TOML subset ``plugin.toml`` uses: comments, ``[table]`` / ``[a.b]``
    headers, and string / int / float / bool / string-array values.
    """
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11 embed builds only
        tomllib = None

    if tomllib is not None:
        with open(path, "rb") as handle:
            return tomllib.load(handle)

    with open(path, "r", encoding="utf-8") as handle:
        return _parse_toml_subset(handle.read())


def _parse_toml_subset(text):
    root = {}
    current = root
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = root
            for segment in line[1:-1].split("."):
                name = segment.strip()
                if not name:
                    raise TomlError("line {0}: empty table segment".format(lineno))
                node = current.get(name)
                if node is None:
                    node = current[name] = {}
                elif not isinstance(node, dict):
                    raise TomlError(
                        "line {0}: table {1!r} collides with a scalar".format(
                            lineno, name
                        )
                    )
                current = node
            continue
        if "=" not in line:
            raise TomlError("line {0}: expected 'key = value'".format(lineno))
        key, _, value = line.partition("=")
        current[key.strip()] = _parse_value(value.strip(), lineno)
    return root


def _strip_comment(line):
    quote = None
    for index, char in enumerate(line):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _parse_value(token, lineno):
    if not token:
        raise TomlError("line {0}: empty value".format(lineno))
    if token[0] in ("'", '"') and token[-1] == token[0] and len(token) >= 2:
        return token[1:-1]
    if token == "true":
        return True
    if token == "false":
        return False
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].strip()
        if not inner:
            return []
        items = []
        for chunk in inner.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if chunk[0] in ("'", '"') and chunk[-1] == chunk[0] and len(chunk) >= 2:
                items.append(chunk[1:-1])
            else:
                raise TomlError(
                    "line {0}: only arrays of strings are supported".format(lineno)
                )
        return items
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    raise TomlError("line {0}: cannot parse value {1!r}".format(lineno, token))


def host_python_abi():
    """``cp311`` / ``cp313`` style tag for the interpreter we are running in."""
    return "cp{0}{1}".format(sys.version_info[0], sys.version_info[1])


__all__ = [
    "AGENT_VERSION",
    "ENV_LAP_IPC_URL",
    "ENV_LAP_PLUGIN_SLUG",
    "ENV_LAP_PLUGIN_TOML",
    "TomlError",
    "host_python_abi",
    "read_toml",
]
