import io
import os
import shutil
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
    (tmp_path / "config" / "secrets.json").write_text('{"k":"SECRET"}')
    (tmp_path / "config" / "server.key").write_text("KEY")
    return tmp_path


def test_roundtrip_excludes_secrets_and_keeps_data(env):
    m = backup.create("b.tar.gz", models_dir="")
    assert set(m["files"]) == {"db/0-runs.db", "config/routing.yml"}
    names = tarfile.open("b.tar.gz").getnames()
    assert not any("secret" in n or n.endswith(".key") for n in names)
    out = env / "restored"
    backup.restore("b.tar.gz", str(out))
    got = (
        sqlite3.connect(out / "db" / "0-runs.db").execute("select x from t").fetchall()
    )
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


# --- Postgres (pg_dump / pg_restore), with fake client tools on PATH ----------

FAKE = """#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps({"argv": a, "pw": os.environ.get("PGPASSWORD")}) + "\\n")
if "--file" in a:
    open(a[a.index("--file") + 1], "wb").write(b"PGDMP-fake")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


@pytest.fixture
def fake_pg(env, monkeypatch):
    import json
    import stat

    bindir = env / "bin"
    bindir.mkdir()
    for tool in ("pg_dump", "pg_restore"):
        f = bindir / tool
        f.write_text(FAKE)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    log = env / "calls.log"
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    return lambda: [json.loads(x) for x in log.read_text().splitlines()]


DSN = "postgresql+asyncpg://caips:p%40ss@db.example:5433/caipsdb"


def test_pg_backup_keeps_password_out_of_argv_and_manifest(env, fake_pg):
    m = backup.create("b.tar.gz", pg=[DSN])
    call = fake_pg()[0]
    assert call["pw"] == "p@ss"
    assert "p@ss" not in " ".join(call["argv"]) and "p%40ss" not in " ".join(call["argv"])
    assert ["--host", "db.example", "--dbname", "caipsdb", "--port", "5433"] == call["argv"][-8:-2]
    entry = m["files"]["pg/0-caipsdb.pgdump"]
    assert "p@ss" not in entry["origin"] and "p%40ss" not in entry["origin"]
    assert "pg/0-caipsdb.pgdump" in backup.verify("b.tar.gz")["files"]


def test_pg_dump_failure_is_a_clean_error(env, fake_pg, monkeypatch):
    monkeypatch.setenv("FAKE_RC", "1")
    with pytest.raises(backup.BackupError, match="pg_dump failed"):
        backup.create("b.tar.gz", pg=[DSN])


def test_missing_pg_dump_is_a_clean_error(env, monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(backup.BackupError, match="pg_dump not found"):
        backup.create("b.tar.gz", pg=[DSN])


def test_database_url_is_auto_discovered(env, fake_pg, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    m = backup.create("b.tar.gz")
    assert "pg/0-caipsdb.pgdump" in m["files"]
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///x.db")
    assert "pg/0-caipsdb.pgdump" not in backup.create("c.tar.gz")["files"]


def test_pg_restore_loads_the_dump(env, fake_pg):
    backup.create("b.tar.gz", pg=[DSN])
    out = env / "restored"
    backup.restore("b.tar.gz", str(out), pg_restore_to="postgresql://u:pw@h/target")
    assert (out / "pg/0-caipsdb.pgdump").read_bytes() == b"PGDMP-fake"
    call = fake_pg()[-1]
    assert "--clean" in call["argv"] and call["argv"][-1].endswith("0-caipsdb.pgdump")
    assert call["pw"] == "pw" and "pw" not in " ".join(call["argv"])


def test_pg_restore_needs_a_dump_in_the_archive(env, fake_pg):
    backup.create("b.tar.gz")
    with pytest.raises(backup.BackupError, match="exactly one"):
        backup.restore("b.tar.gz", str(env / "r"), pg_restore_to="postgresql://u@h/t")


@pytest.mark.skipif(
    not (os.getenv("TEST_POSTGRES_URL") and shutil.which("pg_dump") and shutil.which("pg_restore")),
    reason="needs TEST_POSTGRES_URL and pg_dump/pg_restore on PATH",
)
def test_real_postgres_roundtrip(env):
    import psycopg

    dsn = os.environ["TEST_POSTGRES_URL"].replace("+asyncpg", "")
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("drop table if exists backup_probe")
        c.execute("create table backup_probe(x int)")
        c.execute("insert into backup_probe values (7), (8)")
    backup.create("pg.tar.gz", pg=[dsn])
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("delete from backup_probe")
    backup.restore("pg.tar.gz", str(env / "out"), pg_restore_to=dsn)
    with psycopg.connect(dsn) as c:
        assert sorted(r[0] for r in c.execute("select x from backup_probe")) == [7, 8]
        c.execute("drop table backup_probe")
