"""Output-path handling for the bake tools — stdlib only.

Bake results are files on disk, so the path comes from the caller. Two
rules:

* a bare filename lands in the *output root* (``LAP_AUTOBAKE_OUTPUT_DIR``
  when set, otherwise the current working directory), so a tool call never
  silently writes into Blender's install tree;
* an absolute path is honoured as given.

``..`` segments are rejected outright rather than normalised: an API caller
that asks to walk up is either confused or probing, and neither should
succeed silently.
"""

from __future__ import annotations

import os

#: Environment override for where relative bake outputs land.
OUTPUT_DIR_ENV = "LAP_AUTOBAKE_OUTPUT_DIR"


def output_root():
    """Directory relative output paths resolve against."""
    override = os.environ.get(OUTPUT_DIR_ENV)
    if override and override.strip():
        return os.path.abspath(os.path.expanduser(override.strip()))
    return os.getcwd()


def resolve_output_path(raw):
    """Resolve a caller-supplied path; raise ``ValueError`` on rejection."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path must be a non-empty string")
    text = raw.strip()
    if "\x00" in text:
        raise ValueError("path contains a NUL byte")
    if ".." in text.replace("\\", "/").split("/"):
        raise ValueError("path must not contain '..' segments")
    expanded = os.path.expanduser(text)
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(output_root(), expanded))


def default_output_path(object_name, suffix):
    """``<object><suffix>.png`` under the output root."""
    safe = "".join(
        char if char.isalnum() or char in "-_." else "_"
        for char in str(object_name or "autobake")
    ) or "autobake"
    return os.path.join(output_root(), "{0}{1}.png".format(safe, suffix))


def public_output_path(path):
    """Path as reported back to a caller.

    Absolute paths are returned unchanged; a path under the output root is
    returned relative to it so a report stays readable and portable.
    """
    absolute = os.path.abspath(path)
    root = output_root()
    try:
        common = os.path.commonpath([absolute, root])
    except ValueError:  # different drives on Windows
        return absolute
    if common == root:
        relative = os.path.relpath(absolute, root)
        if not relative.startswith(".."):
            return relative.replace(os.sep, "/")
    return absolute


__all__ = [
    "OUTPUT_DIR_ENV",
    "default_output_path",
    "output_root",
    "public_output_path",
    "resolve_output_path",
]
