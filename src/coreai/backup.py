"""coreai/backup.py: backup and restore of SQLite state and config.

    python -m coreai.backup create backup.tar.gz [--db PATH ...] [--config config]
    python -m coreai.backup verify backup.tar.gz
    python -m coreai.backup restore backup.tar.gz --to DIR [--force]

What is saved: every SQLite database found (the standard env vars and default
paths, plus ``--db``), copied with SQLite's online backup API so a running
gateway gives a consistent snapshot, and the ``config/`` directory. Files that
look like secrets (``secrets*``, ``*.pem``, ``*.key``, ``.env*``) are never
included. GGUF model files are not copied (they are large and re-downloadable);
a list with sizes is recorded in the manifest instead.

Every file's SHA-256 is in ``manifest.json``; ``verify`` and ``restore`` check
them. Restore refuses absolute paths and ``..``, and will not overwrite an
existing file without ``--force``.

Postgres: ``--pg DSN`` (repeatable; ``DATABASE_URL`` is used when it points at
Postgres) runs ``pg_dump --format=custom`` and stores the dump as
``pg/N-<dbname>.pgdump`` with its checksum. ``pg_dump`` and ``pg_restore`` must
be on PATH and match the server's major version. The password never appears on
a command line (it goes in ``PGPASSWORD``) or in the manifest. ``restore`` writes
the dump file; add ``--pg-restore-to DSN`` to load it with ``pg_restore
--clean --if-exists --no-owner`` (this replaces objects in that database).
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ._version import __version__

FORMAT = 1
DB_ENVS = {
    "COREAI_FEEDBACK_DB": "data/feedback.db",
    "COREAI_FINETUNE_DB": None,
    "COREAI_MEMORY_DB": "data/memory.db",
    "COREAI_RAG_DB": "data/rag.db",
}
DEFAULT_DBS = ["data/runs.db", "data/audit.db", "data/usage.db"]
SECRET_PARTS = ("secrets", ".env", ".pem", ".key")


class BackupError(Exception):
    pass


def _is_secret(name: str) -> bool:
    n = name.lower()
    return n.startswith(("secrets", ".env")) or n.endswith((".pem", ".key"))


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_dbs(extra=()) -> list[Path]:
    found = []
    for env, default in DB_ENVS.items():
        v = os.environ.get(env) or default
        if v and v != ":memory:":
            found.append(v)
    found += DEFAULT_DBS + list(extra)
    out, seen = [], set()
    for v in found:
        p = Path(v)
        if p.is_file() and p.resolve() not in seen:
            seen.add(p.resolve())
            out.append(p)
    return out


def _is_pg(dsn: str) -> bool:
    return dsn.startswith(("postgres://", "postgresql://", "postgresql+"))


def _pg_parts(dsn: str) -> tuple[list[str], dict, str]:
    """(connection args, env with PGPASSWORD, redacted DSN). No secret in argv."""
    scheme, _, rest = dsn.partition("://")
    u = urlsplit("postgresql://" + rest)
    host = u.hostname or parse_qs(u.query).get("host", [""])[0]  # ?host=/socket/dir
    if not host or not u.path.strip("/"):
        raise BackupError("Postgres DSN needs a host and a database name")
    args = ["--host", host, "--dbname", unquote(u.path.lstrip("/"))]
    if u.port:
        args += ["--port", str(u.port)]
    if u.username:
        args += ["--username", unquote(u.username)]
    env = dict(os.environ)
    if u.password:
        env["PGPASSWORD"] = unquote(u.password)
    host = host + (f":{u.port}" if u.port else "")
    user = f"{unquote(u.username)}@" if u.username else ""
    return args, env, f"postgresql://{user}{host}{u.path}"


def _need(tool: str) -> str:
    exe = shutil.which(tool)
    if not exe:
        raise BackupError(f"{tool} not found on PATH; install the PostgreSQL client tools")
    return exe


def _pg_dump(dsn: str, dst: Path) -> str:
    args, env, redacted = _pg_parts(dsn)
    cmd = [_need("pg_dump"), "--format=custom", "--no-owner", "--file", str(dst), *args]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise BackupError(f"pg_dump failed for {redacted}: {r.stderr.strip()[:300]}")
    return redacted


def _pg_restore(dsn: str, dump: Path) -> None:
    args, env, redacted = _pg_parts(dsn)
    cmd = [_need("pg_restore"), "--clean", "--if-exists", "--no-owner", *args, str(dump)]
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise BackupError(f"pg_restore failed for {redacted}: {r.stderr.strip()[:300]}")


def discover_pg(extra=()) -> list[str]:
    found = [d for d in extra if d]
    env = os.environ.get("DATABASE_URL", "")
    if _is_pg(env):
        found.append(env)
    out: list[str] = []
    for d in found:
        if d not in out:
            out.append(d)
    return out


def _snapshot(src: Path, dst: Path) -> None:
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=10)
    b = sqlite3.connect(dst)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()


def create(
    out: str, dbs=(), config_dir: str = "config", models_dir: str = "", pg=()
) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p, files = Path(tmp), {}
        for i, dsn in enumerate(discover_pg(pg)):
            (tmp_p / "pg").mkdir(exist_ok=True)
            name = "".join(c if c.isalnum() or c in "-_" else "_" for c in _pg_parts(dsn)[0][3])
            rel = f"pg/{i}-{name}.pgdump"
            origin = _pg_dump(dsn, tmp_p / rel)
            files[rel] = {"sha256": _sha(tmp_p / rel), "origin": origin}
        for i, src in enumerate(discover_dbs(dbs)):
            rel = f"db/{i}-{src.name}"
            (tmp_p / "db").mkdir(exist_ok=True)
            _snapshot(src, tmp_p / rel)
            files[rel] = {"sha256": _sha(tmp_p / rel), "origin": str(src)}
        cfg = Path(config_dir)
        if cfg.is_dir():
            for f in sorted(cfg.rglob("*")):
                if f.is_file() and not _is_secret(f.name):
                    rel = f"config/{f.relative_to(cfg).as_posix()}"
                    (tmp_p / rel).parent.mkdir(parents=True, exist_ok=True)
                    (tmp_p / rel).write_bytes(f.read_bytes())
                    files[rel] = {"sha256": _sha(tmp_p / rel)}
        models = []
        if models_dir and Path(models_dir).is_dir():
            models = [
                {"name": m.name, "bytes": m.stat().st_size}
                for m in sorted(Path(models_dir).rglob("*.gguf"))
            ]
        manifest = {
            "format": FORMAT, "version": __version__, "created": int(time.time()),
            "files": files, "models_not_included": models,
        }  # fmt: skip
        raw = json.dumps(manifest, indent=2, sort_keys=True).encode()
        with tarfile.open(out, "w:gz") as tar:
            info = tarfile.TarInfo("manifest.json")
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
            for rel in files:
                tar.add(tmp_p / rel, arcname=rel)
    return manifest


def _safe(name: str) -> bool:
    p = Path(name)
    return not p.is_absolute() and ".." not in p.parts


def _read(tar: tarfile.TarFile, rel: str) -> bytes:
    fh = tar.extractfile(rel)
    if fh is None:
        raise BackupError(f"not a regular file: {rel}")
    return fh.read()


def verify(archive: str) -> dict:
    """Raises BackupError on any mismatch; returns the manifest."""
    with tarfile.open(archive, "r:gz") as tar:
        names = tar.getnames()
        if "manifest.json" not in names:
            raise BackupError("not a CaiPS backup: manifest.json missing")
        manifest = json.loads(_read(tar, "manifest.json"))
        if manifest.get("format") != FORMAT:
            raise BackupError(f"unsupported backup format {manifest.get('format')}")
        for rel, meta in manifest["files"].items():
            if not _safe(rel) or rel not in names:
                raise BackupError(f"bad or missing entry: {rel}")
            h = hashlib.sha256(_read(tar, rel)).hexdigest()
            if h != meta["sha256"]:
                raise BackupError(f"checksum mismatch: {rel}")
    return manifest


def restore(
    archive: str, to: str, force: bool = False, pg_restore_to: str = ""
) -> list[str]:
    manifest = verify(archive)  # nothing is written before this passes
    target, written = Path(to), []
    clashes = [r for r in manifest["files"] if (target / r).exists()]
    if clashes and not force:
        raise BackupError(
            f"would overwrite {len(clashes)} file(s), e.g. {clashes[0]}; use --force"
        )
    with tarfile.open(archive, "r:gz") as tar:
        for rel in manifest["files"]:
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(_read(tar, rel))
            written.append(rel)
    if pg_restore_to:
        dumps = [r for r in written if r.startswith("pg/")]
        if len(dumps) != 1:
            raise BackupError(
                f"--pg-restore-to needs exactly one Postgres dump in the archive, found {len(dumps)}"
            )
        _pg_restore(pg_restore_to, target / dumps[0])
    return written


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m coreai.backup")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("out")
    c.add_argument("--db", action="append", default=[])
    c.add_argument("--config", default="config")
    c.add_argument("--models", default="")
    c.add_argument("--pg", action="append", default=[], help="Postgres DSN to dump")
    sub.add_parser("verify").add_argument("archive")
    r = sub.add_parser("restore")
    r.add_argument("archive")
    r.add_argument("--to", required=True)
    r.add_argument("--force", action="store_true")
    r.add_argument("--pg-restore-to", default="", help="DSN to load the dump into")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "create":
            m = create(a.out, a.db, a.config, a.models, a.pg)
            print(
                f"wrote {a.out}: {len(m['files'])} file(s), {len(m['models_not_included'])} model(s) listed, not copied"
            )
        elif a.cmd == "verify":
            print(f"ok: {len(verify(a.archive)['files'])} file(s)")
        else:
            print(
                f"restored {len(restore(a.archive, a.to, a.force, a.pg_restore_to))} file(s) to {a.to}"
            )
        return 0
    except (BackupError, OSError, tarfile.TarError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
