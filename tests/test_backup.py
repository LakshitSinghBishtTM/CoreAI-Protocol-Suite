import io
import sqlite3
import tarfile

import pytest

from coreai import backup


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in backup.DB_ENVS:
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "data").mkdir()
    db = sqlite3.connect("data/runs.db")
    db.execute("create table t(x)")
    db.execute("insert into t values (42)")
    db.commit()
    db.close()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "routing.yml").write_text("a: 1\n")
    (tmp_path / "config" / "secrets.json").write_text("{\"k\":\"SECRET\"}")
    (tmp_path / "config" / "server.key").write_text("KEY")
    return tmp_path


def test_roundtrip_excludes_secrets_and_keeps_data(env):
    m = backup.create("b.tar.gz", models_dir="")
    assert set(m["files"]) == {"db/0-runs.db", "config/routing.yml"}
    names = tarfile.open("b.tar.gz").getnames()
    assert not any("secret" in n or n.endswith(".key") for n in names)
    out = env / "restored"
    backup.restore("b.tar.gz", str(out))
    got = sqlite3.connect(out / "db" / "0-runs.db").execute("select x from t").fetchall()
    assert got == [(42,)]


def test_restore_refuses_overwrite_without_force(env):
    backup.create("b.tar.gz")
    out = env / "r"
    backup.restore("b.tar.gz", str(out))
    with pytest.raises(backup.BackupError, match="overwrite"):
        backup.restore("b.tar.gz", str(out))
    assert backup.restore("b.tar.gz", str(out), force=True)


def test_tampered_archive_is_rejected_before_any_write(env):
    backup.create("b.tar.gz")
    src = tarfile.open("b.tar.gz")
    members = {m.name: src.extractfile(m).read() for m in src.getmembers()}
    members["config/routing.yml"] = b"evil: 1\n"
    with tarfile.open("bad.tar.gz", "w:gz") as t:
        for n, data in members.items():
            i = tarfile.TarInfo(n)
            i.size = len(data)
            t.addfile(i, io.BytesIO(data))
    with pytest.raises(backup.BackupError, match="checksum"):
        backup.restore("bad.tar.gz", str(env / "x"))
    assert not (env / "x").exists()


def test_path_traversal_entry_rejected(env):
    import json

    man = {"format": 1, "files": {"../evil": {"sha256": "0"}}}
    raw = json.dumps(man).encode()
    with tarfile.open("t.tar.gz", "w:gz") as t:
        i = tarfile.TarInfo("manifest.json")
        i.size = len(raw)
        t.addfile(i, io.BytesIO(raw))
    with pytest.raises(backup.BackupError, match="bad or missing"):
        backup.verify("t.tar.gz")


def test_cli_exit_codes(env, capsys):
    assert backup.main(["create", "b.tar.gz"]) == 0
    assert backup.main(["verify", "b.tar.gz"]) == 0
    assert backup.main(["verify", "missing.tar.gz"]) == 1
