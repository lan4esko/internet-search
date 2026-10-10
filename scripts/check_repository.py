#!/usr/bin/env python3
"""Reject sensitive runtime artifacts in the Git index without printing contents."""
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DIRECTORIES = {"backups", "artifacts", "results", "captures", "secrets", ".aws", ".codex", ".agents"}
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(rb"\bAKIA[A-Z0-9]{16}\b"),
)


def forbidden_path(name):
    path = PurePosixPath(name)
    return (bool(set(path.parts).intersection(FORBIDDEN_DIRECTORIES))
            or (path.name.startswith('.env') and path.name != '.env.example')
            or path.suffix.lower() in {'.pcap', '.pcapng', '.pem', '.key', '.crt', '.dump', '.sql', '.sqlite', '.sqlite3'}
            or path.name == 'Pasted text.txt')


def main():
    paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
    findings = []
    for name in filter(None, paths):
        if forbidden_path(name):
            findings.append(f'{name}: runtime artifact must not be tracked')
        data = subprocess.check_output(['git', 'show', ':' + name], cwd=ROOT)
        if any(pattern.search(data) for pattern in SECRET_PATTERNS):
            findings.append(f'{name}: potential secret; inspect privately')
    if findings:
        print('\n'.join(findings), file=sys.stderr)
        return 1
    print('Git index: no forbidden runtime files or recognized credential patterns. Manual review still required.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
