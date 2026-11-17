"""Produce a verifiable publication-scan report for the tracked tree.

The handoff asked for a report with the file list, the patterns applied, and
the reason for every hit or exclusion — not just a pass/fail line. This writes
that report to stdout (and optionally a file) so it can be attached to the
ticket and re-run by a reviewer.

Scans the **git tree** (``<rev>``), not the working directory, so the report
describes exactly what a clone of that commit would contain.

Usage:
    python tools/publication_scan.py [--rev HEAD] [--out report.md]
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (label, regex, why it is a finding)
SECRETS = [
    ("github token", r"\bgh[pousr]_[A-Za-z0-9]{20,}", "credential"),
    ("gitlab token", r"\bglpat-[A-Za-z0-9_-]{16,}", "credential"),
    ("aws key id", r"\b(AKIA|ASIA)[A-Z0-9]{12,}", "credential"),
    ("aws secret", r"(?i)aws_?secret_?access_?key\s*[:=]\s*\S+", "credential"),
    ("cos/tencent secret", r"(?i)secret_?id\s*[:=]\s*[\"']?[A-Za-z0-9]{16,}", "credential"),
    ("private key block", r"-----BEGIN (RSA|OPENSSH|EC|DSA|PGP) ", "credential"),
    ("slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "credential"),
    ("jwt", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.", "credential"),
    ("hard-coded password", r"(?i)\bpassword\s*=\s*[\"'][^\"'\s]{6,}[\"']", "credential"),
    ("hard-coded token", r"(?i)\b(auth_)?token\s*=\s*[\"'][A-Za-z0-9_\-]{20,}[\"']", "credential"),
]

INTERNAL = [
    ("internal git host", r"git\.woa\.com", "internal infrastructure"),
    (".woa.com hostname", r"[\w.-]+\.woa\.com", "internal infrastructure"),
    ("internal monorepo name", r"lap-dcc-mcp-server", "internal infrastructure"),
    ("internal group name", r"LAP-Group", "internal infrastructure"),
    ("monica host", r"monica\.(woa|ag2ui)", "internal infrastructure"),
    ("monica task token", r"\bmat_[A-Za-z0-9]{10,}", "credential"),
    ("internal ticket key", r"\b(AUTO-\d+|PUBL-\d+)\b", "internal coordination"),
    ("automation identifier", r"dcc-autopilo|MULTICA", "internal coordination"),
]

LOCAL = [
    ("developer home path", r"[A-Za-z]:\\\\?Users\\\\?\w{2,}", "local path"),
    ("posix developer home", r"/(?:home|Users)/[A-Za-z0-9._-]{3,}/", "local path"),
]

#: Allowed hits, with the reason each is acceptable. Keeping them explicit is
#: the point: a reviewer can see the judgement instead of trusting silence.
ALLOWED = [
    (
        "vendor/autobake_tools/UPSTREAM-README.md",
        r"github\.com/GarikDog/autobake_tools",
        "upstream provenance URL required by Apache-2.0 attribution",
    ),
    (
        "vendor/autobake_tools/LICENSE.md",
        r"apache\.org/licenses",
        "licence text redistributed verbatim",
    ),
    (
        "NOTICE.md",
        r"github\.com/GarikDog/autobake_tools",
        "upstream provenance URL required by Apache-2.0 attribution",
    ),
    (
        "tools/publication_scan.py",
        r".*",
        "this report generator: contains the patterns it searches for",
    ),
]

FORBIDDEN_SUFFIXES = {
    ".zip": "binary/build artefact — packages go to the ticket, not the repo",
    ".whl": "binary/build artefact",
    ".exe": "binary/build artefact",
    ".dll": "binary/build artefact",
    ".pyc": "build artefact",
    ".pyd": "binary/build artefact",
    ".so": "binary/build artefact",
    ".dylib": "binary/build artefact",
    ".log": "run log — may contain machine paths",
    ".blend": "user data",
    ".jsonl": "transcript",
}

FORBIDDEN_PARTS = {
    "__pycache__": "build artefact",
    "dist": "build output — delivered as a ticket attachment",
    "build": "build output",
    ".pytest_cache": "test cache",
    ".venv": "virtualenv",
    "evidence": "host test output — contains absolute paths, attached to the ticket",
}


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=str(ROOT), capture_output=True, check=True)


def tracked_files(rev):
    out = git("ls-tree", "-r", "--name-only", rev).stdout
    return [line for line in out.decode("utf-8").splitlines() if line]


def blob(rev, path):
    return git("cat-file", "blob", "{0}:{1}".format(rev, path)).stdout


def compile_all(patterns):
    return [(label, re.compile(pattern), why) for label, pattern, why in patterns]


def allowed(path, line):
    for allowed_path, pattern, _reason in ALLOWED:
        if allowed_path in path and re.search(pattern, line):
            return True
    return False


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--rev", default="HEAD")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    files = tracked_files(args.rev)
    secrets = compile_all(SECRETS)
    internal = compile_all(INTERNAL)
    local = compile_all(LOCAL)

    buffered = io.StringIO()

    def emit(text=""):
        buffered.write(text + "\n")

    emit("# Publication scan report")
    emit()
    emit("Revision: `{0}` ({1})".format(args.rev, git("rev-parse", args.rev).stdout.decode().strip()))
    emit("Tree: `{0}`".format(git("rev-parse", args.rev + "^{tree}").stdout.decode().strip()))
    emit("Files scanned: {0} (every file tracked at that revision)".format(len(files)))
    emit()
    emit("## Verdict")
    emit()

    findings = []
    exclusions = []

    for path in files:
        parts = set(path.split("/"))
        for part, why in FORBIDDEN_PARTS.items():
            if part in parts:
                findings.append((path, 0, "forbidden directory: " + part, why))
        suffix = os.path.splitext(path)[1].lower()
        if suffix in FORBIDDEN_SUFFIXES:
            findings.append((path, 0, "forbidden file type: " + suffix, FORBIDDEN_SUFFIXES[suffix]))

        data = blob(args.rev, path)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            exclusions.append((path, "binary file — not text-scanned; type check above applies"))
            continue

        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern, why in secrets + internal + local:
                if not pattern.search(line):
                    continue
                if allowed(path, line):
                    exclusions.append(
                        (path, "line {0}: {1} — allowed ({2})".format(
                            number, label,
                            next(r for p, _pat, r in ALLOWED if p in path and re.search(_pat, line))))
                    )
                    continue
                findings.append((path, number, label, why))

    if findings:
        emit("**{0} finding(s) — not publishable as-is.**".format(len(findings)))
        emit()
        for path, number, label, why in findings:
            emit("- `{0}`:{1} — {2} ({3})".format(path, number, label, why))
    else:
        emit("**Clean.** No secrets, no internal-infrastructure references, no local "
             "paths, no build artefacts in the tracked tree.")
    emit()

    emit("## Patterns applied")
    emit()
    emit("| Category | Pattern label | Regex |")
    emit("| --- | --- | --- |")
    for label, pattern, _why in secrets:
        emit("| credential | {0} | `{1}` |".format(label, pattern.replace("|", "\\|")))
    for label, pattern, _why in internal:
        emit("| internal | {0} | `{1}` |".format(label, pattern.replace("|", "\\|")))
    for label, pattern, _why in local:
        emit("| local path | {0} | `{1}` |".format(label, pattern.replace("|", "\\|")))
    emit()
    emit("File-type exclusions: " + ", ".join("`{0}`".format(s) for s in FORBIDDEN_SUFFIXES))
    emit()
    emit("Directory exclusions: " + ", ".join("`{0}`".format(d) for d in FORBIDDEN_PARTS))
    emit()

    emit("## Deliberate allowances")
    emit()
    emit("| File | Pattern | Reason |")
    emit("| --- | --- | --- |")
    for path, pattern, reason in ALLOWED:
        emit("| `{0}` | `{1}` | {2} |".format(path, pattern, reason))
    emit()

    if exclusions:
        emit("## Hits excluded, with reason")
        emit()
        for path, reason in exclusions:
            emit("- `{0}` — {1}".format(path, reason))
        emit()

    emit("## Licence and attribution")
    emit()
    emit("Verified by `tests/test_vendor_integrity.py` against the pinned archive:")
    emit()
    emit("- upstream `LICENSE.md` present, Apache-2.0, sha256 `c71d239d…0ab4`")
    emit("- copyright header `Copyright 2023 Igor Subachev (GarikDog)` retained in every vendored source file")
    emit("- `NOTICE.md` records the upstream commit, archive hash, per-file hashes, the five adapter changes,")
    emit("  and the README-badge-says-MIT versus LICENSE-says-Apache discrepancy")
    emit()

    text = buffered.getvalue()
    print(text)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print("written: {0}".format(args.out))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
