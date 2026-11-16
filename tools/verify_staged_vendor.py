"""Verify the *staged* vendored files (git index blobs) against the manifest.

The worktree can be correct while the index holds CRLF-converted bytes, which
is what a fresh clone would then receive. This script compares the blob Git
actually stores, so it catches that case.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_REL = "src/dcc_plugin_autobake/agent/vendor/autobake_tools"
MANIFEST = ROOT / VENDOR_REL / "VENDOR-MANIFEST.json"


def blob_for(path):
    result = subprocess.run(
        ["git", "cat-file", "blob", ":" + path],
        cwd=str(ROOT),
        capture_output=True,
        check=True,
    )
    return result.stdout


def main():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    failures = []
    for relative, expected in sorted(manifest["files"].items()):
        staged = "{0}/{1}".format(VENDOR_REL, relative)
        data = blob_for(staged)
        actual = hashlib.sha256(data).hexdigest()
        status = "OK" if actual == expected else "MISMATCH"
        if actual != expected:
            failures.append((relative, expected, actual, len(data)))
        print("{0:9} {1:46} {2} bytes {3}".format(status, relative, len(data), actual[:16]))
    if failures:
        print("\n{0} staged vendored file(s) DO NOT match the pinned archive:".format(len(failures)))
        for relative, expected, actual, size in failures:
            print("  {0}: expected {1} got {2} ({3} bytes)".format(relative, expected, actual, size))
        return 1
    print("\nall {0} staged vendored files match the pinned archive".format(len(manifest["files"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
