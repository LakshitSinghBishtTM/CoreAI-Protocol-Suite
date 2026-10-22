# Tests import the installed package (pip install -e .[dev]). A few older
# tests import modules by bare name (auth, secure_protocol, ...), so the
# package directories are also put on sys.path.
import os
import sys

src = os.path.join(os.path.dirname(__file__), "src")
for sub in ("", "api", "protocols", "agents"):
    sys.path.insert(0, os.path.join(src, sub))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_caips_config(tmp_path, monkeypatch):
    """Never read or write the developer's real ~/.config/caips in tests."""
    monkeypatch.setenv("CAIPS_CONFIG_DIR", str(tmp_path / "caips-config"))
