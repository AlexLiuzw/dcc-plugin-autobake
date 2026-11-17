"""In-tree PEP 517 backend: mirror ``skills/`` into the package, then defer to setuptools.

The authoritative markdown lives at project-root ``skills/``; the runtime
reads ``src/dcc_plugin_autobake/skills/``. Mirrored here (rather than with a
symlink) so the same code works on Windows checkouts without developer mode.

Not a plugin installer: the sidecar's embedded Python can ``pip install .``
without importing the LAP devkit.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import build_meta as _orig

PACKAGE = "dcc_plugin_autobake"
SKILLS_SRC = "skills"
SKILLS_DST = "src/{0}/skills".format(PACKAGE)


def _sync_skills() -> None:
    root = Path(__file__).resolve().parent
    src = root / SKILLS_SRC
    dst = root / SKILLS_DST
    if not src.is_dir():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def build_sdist(*args, **kwargs):
    _sync_skills()
    return _orig.build_sdist(*args, **kwargs)


def build_wheel(*args, **kwargs):
    _sync_skills()
    return _orig.build_wheel(*args, **kwargs)


def build_editable(*args, **kwargs):
    _sync_skills()
    return _orig.build_editable(*args, **kwargs)


def prepare_metadata_for_build_wheel(*args, **kwargs):
    _sync_skills()
    return _orig.prepare_metadata_for_build_wheel(*args, **kwargs)


def prepare_metadata_for_build_editable(*args, **kwargs):
    _sync_skills()
    return _orig.prepare_metadata_for_build_editable(*args, **kwargs)


def __getattr__(name: str):
    return getattr(_orig, name)
