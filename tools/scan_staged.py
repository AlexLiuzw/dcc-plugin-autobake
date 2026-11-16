"""Pre-publication scan over exactly what is staged for this repository.

Checked, in order:

1. internal infrastructure references (internal git host, internal monorepo
   name, Monica/automation identifiers, internal ticket keys)
2. credential-shaped strings (cloud keys, personal access tokens, private key
   headers, hard-coded passwords)
3. absolute paths from the developer machine
4. file types that should never be published (build output, caches, archives)

Reads the *git index*, so what is checked is exactly what a clone would get.
Exits non-zero on any finding.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

INTERNAL_PATTERNS = [
    ("internal git host", re.compile(r"git\.woa\.com", re.IGNORECASE)),
    (".woa.com hostname", re.compile(r"[\w.-]+\.woa\.com", re.IGNORECASE)),
    ("internal monorepo name", re.compile(r"lap-dcc-mcp-server", re.IGNORECASE)),
    ("internal group name", re.compile(r"LAP-Group", re.IGNORECASE)),
    ("monica host", re.compile(r"monica\.(woa|ag2ui)", re.IGNORECASE)),
    ("monica token", re.compile(r"\bmat_[A-Za-z0-9]{10,}")),
    ("automation id", re.compile(r"dcc-autopilo|MULTICA|autopilot run", re.IGNORECASE)),
    ("internal ticket key", re.compile(r"\b(AUTO-\d+|PUBL-\d+)\b")),
]

SECRET_PATTERNS = [
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("gitlab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}")),
    ("aws key id", re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{12,}")),
    ("aws secret", re.compile(r"(?i)aws_?secret_?access_?key\s*[:=]\s*\S+")),
    ("cos/tencent secret", re.compile(r"(?i)secret_?id\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}")),
    ("private key", re.compile(r"-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) ")),
    ("slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")),
    ("hard-coded password", re.compile(r"(?i)\bpassword\s*=\s*[\"'][^\"'\s]{6,}[\"']")),
    ("hard-coded token", re.compile(r"(?i)\b(auth_)?token\s*=\s*[\"'][A-Za-z0-9_\-]{20,}[\"']")),
]

LOCAL_PATH_PATTERNS = [
    ("developer home path", re.compile(r"[A-Za-z]:\\\\?Users\\\\?\w{2,}")),
    ("posix developer home", re.compile(r"/(?:home|Users)/[A-Za-z0-9._-]{3,}/")),
]

FORBIDDEN_SUFFIXES = {
    ".zip",
    ".whl",
    ".exe",
    ".dll",
    ".pyc",
    ".pyd",
    ".so",
    ".dylib",
    ".log",
    ".blend",
}

FORBIDDEN_PARTS = {"__pycache__", "dist", "build", ".pytest_cache", ".venv"}

#: Files that legitimately contain text the patterns above would flag.
EXEMPT = {
    # This scanner documents its own patterns.
    "tools/scan_staged.py",
}


def staged_files():
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def blob_text(path):
    result = subprocess.run(
        ["git", "cat-file", "blob", ":" + path],
        cwd=str(ROOT),
        capture_output=True,
        check=True,
    )
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None  # binary: handled by the suffix/parts checks


def main():
    files = staged_files()
    if not files:
        print("nothing staged")
        return 1

    findings = []

    for path in files:
        parts = set(path.split("/"))
        suffix = Path(path).suffix.lower()
        if parts & FORBIDDEN_PARTS:
            findings.append((path, 0, "build/cache directory in the index", path))
        if suffix in FORBIDDEN_SUFFIXES:
            findings.append((path, 0, "binary/build artefact in the index", path))
        if path in EXEMPT:
            continue

        text = blob_text(path)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in INTERNAL_PATTERNS:
                if pattern.search(line):
                    findings.append((path, number, label, line.strip()[:120]))
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append((path, number, label, line.strip()[:120]))
            for label, pattern in LOCAL_PATH_PATTERNS:
                if pattern.search(line):
                    findings.append((path, number, label, line.strip()[:120]))

    print("scanned {0} staged file(s)".format(len(files)))
    if findings:
        print("\n{0} finding(s):".format(len(findings)))
        for path, number, label, excerpt in findings:
            print("  {0}:{1}  [{2}]  {3}".format(path, number, label, excerpt))
        return 1
    print("clean: no internal references, credentials, local paths or build artefacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
