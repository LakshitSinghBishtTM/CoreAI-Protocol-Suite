import os
import stat

from caips_cli import credentials as c
from caips_cli.cli import main


def test_roundtrip_and_mode():
    p = c.save("http://x:1/", "tok")
    assert c.load("http://x:1") == "tok"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    assert c.delete("http://x:1") and c.load("http://x:1") is None


def test_env_overrides_stored(monkeypatch):
    c.save("http://x", "stored")
    assert c.resolve("http://x") == "stored"
    monkeypatch.setenv("CAIPS_API_KEY", "env")
    assert c.resolve("http://x") == "env"


def test_cli_login_logout(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("secret\n"))
    assert main(["--url", "http://g", "login"], err=io.StringIO()) == 0
    assert c.load("http://g") == "secret"
    assert main(["--url", "http://g", "logout"], err=io.StringIO()) == 0
    assert c.load("http://g") is None
