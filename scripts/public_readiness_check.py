#!/usr/bin/env python3
"""Public repository hygiene checks for Shadow.

The check scans Git-tracked and unignored files only. It intentionally ignores
local `.env`, `data/`, logs, and other ignored runtime files so contributors can
run it on real installations without printing secrets.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DISALLOWED_EXACT = {".env"}
DISALLOWED_PREFIXES = (
    "data/",
    "logs/",
    "reports/",
    "research_data/",
    ".playwright-mcp/",
)

SKIP_EXACT = {
    "package-lock.json",
    "scripts/public_readiness_check.py",
}

SKIP_PREFIXES = (
    ".git/",
    "node_modules/",
    "services/node_modules/",
    "static/lib/",
)

SECRET_PATTERNS = [
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b|\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{5,12}:[A-Za-z0-9_-]{20,}\b")),
    ("bearer_token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{30,}")),
]


def run_git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL)


def git_files() -> list[Path]:
    raw = run_git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    return [ROOT / item for item in raw.split("\0") if item]


def is_binary_or_large(path: Path) -> bool:
    try:
        if path.stat().st_size > 2_000_000:
            return True
        data = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\0" in data


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def check_disallowed(paths: list[Path]) -> list[str]:
    problems = []
    for path in paths:
        name = rel(path)
        if name in DISALLOWED_EXACT or any(name.startswith(prefix) for prefix in DISALLOWED_PREFIXES):
            problems.append(f"tracked_runtime_file: {name}")
    return problems


def check_ignored_contract() -> list[str]:
    required = [".env", "data/app.db", "data/auth.json", "logs/shadow.log"]
    problems = []
    for item in required:
        proc = subprocess.run(["git", "check-ignore", item], cwd=ROOT, text=True, capture_output=True)
        if proc.returncode != 0:
            problems.append(f"not_gitignored: {item}")
    return problems


def check_secrets(paths: list[Path]) -> list[str]:
    problems = []
    for path in paths:
        name = rel(path)
        if name in SKIP_EXACT or any(name.startswith(prefix) for prefix in SKIP_PREFIXES):
            continue
        if is_binary_or_large(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for label, pattern in SECRET_PATTERNS:
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                problems.append(f"possible_{label}: {name}:{line_no}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that Shadow is safe to publish.")
    parser.add_argument("--strict", action="store_true", help="Reserved for CI; currently same checks with stricter exit behavior.")
    args = parser.parse_args()

    if not (ROOT / ".git").exists():
        print("public readiness check must run from a Git checkout", file=sys.stderr)
        return 2

    files = git_files()
    problems = []
    problems.extend(check_disallowed(files))
    problems.extend(check_ignored_contract())
    problems.extend(check_secrets(files))

    if problems:
        print("Public readiness check failed:")
        for problem in problems:
            print(f"- {problem}")
        print("\nFix the files above or add a narrow false-positive exception in scripts/public_readiness_check.py.")
        return 1

    mode = "strict" if args.strict else "normal"
    print(f"Public readiness check passed ({mode}).")
    print(f"Scanned {len(files)} tracked/unignored files; ignored local runtime files were not read.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
