"""Phase 0: no real-looking provider keys in tracked files."""

import re
import subprocess

PATTERN = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|AIzaSy[A-Za-z0-9_-]{30,}|xai-[A-Za-z0-9]{30,})"
)


def test_no_tracked_secrets():
    try:
        files = subprocess.check_output(["git", "ls-files"], text=True).split()
    except Exception:
        return  # not a git checkout (e.g. sdist)
    bad = []
    for f in files:
        try:
            text = open(f, errors="ignore").read()
        except OSError:
            continue
        if f.startswith("tests/"):
            continue  # fixtures use fake keys
        if PATTERN.search(text):
            bad.append(f)
    assert not bad, f"possible secrets in tracked files: {bad}"
