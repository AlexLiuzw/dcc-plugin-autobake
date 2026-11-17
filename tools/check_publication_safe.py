"""Check that nothing in this repository should stay private.

Run before publishing. It looks for credentials, internal-infrastructure
references, developer paths and build artefacts in the *tracked* tree (not the
working directory), so the answer describes exactly what a clone would get.

The internal-reference patterns are supplied by the operator rather than being
written here: a scanner that hard-codes the names of the internal systems it
looks for would itself leak them into a public repository.

Usage:
    python tools/check_publication_safe.py --patterns my-patterns.txt [--rev HEAD]

``my-patterns.txt`` is one ``label<TAB>regex`` per line. Keep it outside the
repository.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Credential shapes. These are generic enough to live in the repository: they
#: describe public token formats, not internal systems.
SECRETS = [
    ("github token", r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    ("gitlab token", r"\bglpat-[A-Za-z0-9_-]{16,}"),
    ("aws key id", r"\b(AKIA|ASIA)[A-Z0-9]{12,}"),
    ("aws secret", r"(?i)aws_?secret_?access_?key\s*[:=]\s*\S+"),
    ("cloud api secret", r"(?i)secret_?id\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}"),
    ("private key block", r"-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) "),
    ("slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    ("hard-coded password", r"(?i)\bpassword\s*=\s*[\"'][^\"'\s]{6,}[\"']"),
    ("hard-coded token", r"(?i)\b(auth_)?token\s*=\s*[\"'][A-Za-z0-9_\-]{20,}[\"']"),
]

#: Machine-specific paths. Public documentation should use placeholders.
LOCAL_PATHS = [
    ("developer home path", r"[A-Za-z]:\\\\?Users\\\\?\w{2,}"),
    ("posix developer home", r"/(?:home|Users)/[A-Za-z0-9._-]{3,}/"),
]

FORBIDDEN_SUFFIXES = {
    ".zip", ".whl", ".exe", ".dll", ".pyc", ".pyd", ".so", ".dylib",
    ".log", ".blend", ".jsonl",
}

FORBIDDEN_DIRS = {"__pycache__", "dist", "build", ".pytest_cache", ".venv", "evidence"}

#: Files that legitimately contain a matched pattern, with the reason.
ALLOWED = {
    "tools/check_publication_safe.py": "this checker: lists the patterns it applies",
}


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=str(ROOT), capture_output=True, check=True)


def load_operator_patterns(path):
    patterns = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label, _, regex = line.partition("\t")
        if not regex:
            raise SystemExit("bad line in {0}: {1!r}".format(path, line))
        patterns.append((label.strip(), re.compile(regex)))
    return patterns


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--patterns", type=Path, default=None,
                        help="operator-supplied internal-reference patterns")
    parser.add_argument("--rev", default="HEAD")
    args = parser.parse_args(argv)

    files = [
        line
        for line in git("ls-tree", "-r", "--name-only", args.rev).stdout.decode().splitlines()
        if line
    ]
    patterns = [(label, re.compile(regex)) for label, regex in SECRETS]
    patterns += [(label, re.compile(regex)) for label, regex in LOCAL_PATHS]
    if args.patterns:
        patterns += load_operator_patterns(args.patterns)

    findings = []
    for path in files:
        parts = set(path.split("/"))
        if parts & FORBIDDEN_DIRS:
            findings.append((path, 0, "build/cache directory", ""))
        if os.path.splitext(path)[1].lower() in FORBIDDEN_SUFFIXES:
            findings.append((path, 0, "binary/build artefact", path))

        data = git("cat-file", "blob", "{0}:{1}".format(args.rev, path)).stdout
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if path in ALLOWED:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in patterns:
                if pattern.search(line):
                    findings.append((path, number, label, line.strip()[:110]))

    print("scanned {0} tracked file(s) at {1}".format(len(files), args.rev))
    if findings:
        print("\n{0} finding(s):".format(len(findings)))
        for path, number, label, excerpt in findings:
            print("  {0}:{1} [{2}] {3}".format(path, number, label, excerpt))
        return 1
    print("clean: no credentials, internal references, local paths or build artefacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
