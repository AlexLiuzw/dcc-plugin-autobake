"""Copy the pinned upstream archive into ``src/dcc_plugin_autobake/agent/vendor/``.

Dev-time helper only — not shipped in the built package. It exists so the
vendored tree can be reproduced and re-verified byte-for-byte instead of
being pasted in by hand.

Usage (from the repo root):

    python tools/vendor_upstream.py --archive <path to autobake_tools-1-1-5.zip>

The archive must hash to the value pinned in ``NOTICE.md``; the script
refuses any other archive so a wrong download cannot silently become the
vendored source. Only the Python package and the licence / readme are
copied — the demo GIFs in the archive are documentation assets and are
deliberately not redistributed here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

EXPECTED_SHA256 = (
    "53df706df88287aa5b82371cd08af70a7a2f7413953129a1e7dfba2faa30bf91"
)
EXPECTED_BYTES = 11260882
ARCHIVE_ROOT = "autobake_tools-1-1-5/"

#: Archive members copied into the vendored package. The mapping is
#: ``archive member -> vendored path`` relative to the vendor directory.
WANTED = {
    "autobake_tools-1-1-5/__init__.py": "__init__.py",
    "autobake_tools-1-1-5/LICENSE.md": "LICENSE.md",
    "autobake_tools-1-1-5/README.md": "UPSTREAM-README.md",
    "autobake_tools-1-1-5/addon/__init__.py": "addon/__init__.py",
    "autobake_tools-1-1-5/addon/menu/__init__.py": "addon/menu/__init__.py",
    "autobake_tools-1-1-5/addon/menu/at_ui_panel.py": "addon/menu/at_ui_panel.py",
    "autobake_tools-1-1-5/addon/operator/__init__.py": "addon/operator/__init__.py",
    "autobake_tools-1-1-5/addon/operator/at_bake.py": "addon/operator/at_bake.py",
    "autobake_tools-1-1-5/addon/operator/at_bake_ao.py": "addon/operator/at_bake_ao.py",
    "autobake_tools-1-1-5/addon/operator/at_create_env.py": "addon/operator/at_create_env.py",
    "autobake_tools-1-1-5/addon/property/__init__.py": "addon/property/__init__.py",
    "autobake_tools-1-1-5/addon/property/at_obj_properties.py": "addon/property/at_obj_properties.py",
    "autobake_tools-1-1-5/addon/property/at_properties.py": "addon/property/at_properties.py",
    "autobake_tools-1-1-5/addon/register/__init__.py": "addon/register/__init__.py",
    "autobake_tools-1-1-5/addon/utility/__init__.py": "addon/utility/__init__.py",
    "autobake_tools-1-1-5/addon/utility/at_utils.py": "addon/utility/at_utils.py",
}

VENDOR_DIR = Path(__file__).resolve().parents[1] / "src" / "dcc_plugin_autobake" / "agent" / "vendor" / "autobake_tools"
MANIFEST_NAME = "VENDOR-MANIFEST.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="verify only, write nothing")
    args = parser.parse_args(argv)

    raw = args.archive.read_bytes()
    digest = _sha256(raw)
    if digest != EXPECTED_SHA256 or len(raw) != EXPECTED_BYTES:
        print(
            "refusing {0}: {1} bytes sha256={2} (expected {3} bytes sha256={4})".format(
                args.archive, len(raw), digest, EXPECTED_BYTES, EXPECTED_SHA256
            ),
            file=sys.stderr,
        )
        return 2

    archive = zipfile.ZipFile(args.archive)
    missing = sorted(set(WANTED) - set(archive.namelist()))
    if missing:
        print("archive is missing expected members: {0}".format(missing), file=sys.stderr)
        return 2

    if args.check:
        ok = True
        manifest_path = VENDOR_DIR / MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for member, rel in sorted(WANTED.items()):
            want = _sha256(archive.read(member))
            local = VENDOR_DIR / rel
            got = _sha256(local.read_bytes()) if local.is_file() else "<missing>"
            if want != got:
                ok = False
                print("MISMATCH {0}: archive={1} local={2}".format(rel, want, got))
            elif manifest["files"].get(rel) != want:
                ok = False
                print("MANIFEST DRIFT {0}".format(rel))
        print("vendor tree matches the pinned archive" if ok else "vendor tree DRIFTED")
        return 0 if ok else 1

    rows = {}
    for member, rel in sorted(WANTED.items()):
        data = archive.read(member)
        target = VENDOR_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        rows[rel] = _sha256(data)

    manifest = {
        "upstream": "https://github.com/GarikDog/autobake_tools",
        "tag": "v1,1,5",
        "commit": "34f49ab8718fa7c50ef9572ef58130f69642e298",
        "archive": "autobake_tools-1-1-5.zip",
        "archive_bytes": EXPECTED_BYTES,
        "archive_sha256": EXPECTED_SHA256,
        "archive_root": ARCHIVE_ROOT,
        "license": "Apache-2.0",
        "copyright": "Copyright 2023 Igor Subachev (GarikDog)",
        "note": (
            "Vendored files are byte-identical to the archive members listed in "
            "'files'. The archive's images/ tree is documentation only and is not "
            "redistributed here; verify any member against the archive hash above."
        ),
        "files": rows,
    }
    (VENDOR_DIR / MANIFEST_NAME).write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    print("wrote {0} files into {1}".format(len(rows), VENDOR_DIR))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
