"""The vendored tree must stay byte-identical to the pinned upstream archive.

This is the licence-and-provenance test: if anyone edits a vendored file, the
manifest hash stops matching and this fails. Fix by changing the adapter, or
by re-running ``tools/vendor_upstream.py`` against a newly pinned archive (and
updating ``NOTICE.md`` in the same commit).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

VENDOR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "dcc_plugin_autobake"
    / "agent"
    / "vendor"
    / "autobake_tools"
)

PINNED_ARCHIVE_SHA256 = "53df706df88287aa5b82371cd08af70a7a2f7413953129a1e7dfba2faa30bf91"
UPSTREAM_COMMIT = "34f49ab8718fa7c50ef9572ef58130f69642e298"


def _manifest():
    return json.loads((VENDOR / "VENDOR-MANIFEST.json").read_text(encoding="utf-8"))


def test_manifest_pins_the_expected_archive():
    manifest = _manifest()
    assert manifest["archive_sha256"] == PINNED_ARCHIVE_SHA256
    assert manifest["commit"] == UPSTREAM_COMMIT
    assert manifest["tag"] == "v1,1,5"
    assert manifest["license"] == "Apache-2.0"
    assert "GarikDog" in manifest["copyright"]


def test_every_vendored_file_matches_the_manifest():
    manifest = _manifest()
    files = manifest["files"]
    assert files, "the manifest lists no files"
    for relative, expected in sorted(files.items()):
        path = VENDOR / relative
        assert path.is_file(), "missing vendored file: {0}".format(relative)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        assert actual == expected, "vendored file changed: {0}".format(relative)


def test_manifest_lists_no_file_that_is_missing():
    manifest = _manifest()
    on_disk = {
        str(path.relative_to(VENDOR)).replace("\\", "/")
        for path in VENDOR.rglob("*")
        if path.is_file()
        and path.name != "VENDOR-MANIFEST.json"
        and "__pycache__" not in path.parts
    }
    assert on_disk == set(manifest["files"])


def test_license_and_upstream_readme_are_redistributed():
    license_text = (VENDOR / "LICENSE.md").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0" in license_text
    readme = (VENDOR / "UPSTREAM-README.md").read_text(encoding="utf-8")
    assert readme.strip(), "the upstream README should be kept for provenance"


@pytest.mark.parametrize("relative", ["__init__.py", "addon/utility/at_utils.py"])
def test_upstream_copyright_header_is_preserved(relative):
    text = (VENDOR / relative).read_text(encoding="utf-8")
    assert "Copyright 2023 Igor Subachev (GarikDog)" in text
    assert "Apache License, Version 2.0" in text


def test_upstream_bl_info_is_what_the_plugin_documents():
    text = (VENDOR / "__init__.py").read_text(encoding="utf-8")
    assert '"name": "Auto-bake Tools"' in text
    collapsed = text.replace(" ", "")
    assert '"version":(1,1,5)' in collapsed
    assert '"blender":(3,0,1)' in collapsed
