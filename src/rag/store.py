"""SQLite vector store with brute-force cosine search (fine up to ~100k chunks).

``embed`` is any async ``list[str] -> list[list[float]]`` (the gateway's embeddings
endpoint, cloud or local). Collections are isolated by ``(owner, collection)``.
"""

import hashlib
import html
import json
import math
import re
import sqlite3
import struct
from html.parser import HTMLParser
from pathlib import Path
from typing import Awaitable, Callable

Embed = Callable[[list[str]], Awaitable[list[list[float]]]]
MAX_CHUNKS_PER_COLLECTION = 100_000


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    """Split on paragraph boundaries into ~``size`` char chunks with overlap."""
    if size <= overlap:
        raise ValueError("size must exceed overlap")
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        while len(p) > size:  # oversize paragraph: hard split
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(p[:size])
            p = p[size - overlap :]
        if cur and len(cur) + len(p) + 2 > size:
            chunks.append(cur)
            cur = cur[-overlap:] + "\n\n" + p if overlap else p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        self.skip += tag in self.SKIP
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n\n")

    def handle_endtag(self, tag):
        self.skip -= tag in self.SKIP and self.skip > 0

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def read_document(path: str | Path) -> str:
    """Plain text from .txt/.md/.html (PDF needs the optional ``pypdf``)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext in (".html", ".htm"):
        t = _Text()
        t.feed(p.read_text(errors="replace"))
        return html.unescape("".join(t.parts))
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:
            raise RuntimeError("PDF ingestion needs `pip install pypdf`") from e
        return "\n\n".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)
    if ext in (".txt", ".md", ".markdown", ""):
        return p.read_text(errors="replace")
    raise ValueError(f"unsupported file type '{ext}'")


def _pack(v: list[float]) -> bytes:
    return struct.pack(f"{len(v)}f", *v)


def _unpack(b: bytes) -> tuple[float, ...]:
    return struct.unpack(f"{len(b) // 4}f", b)


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class VectorStore:
    def __init__(self, path: str = ":memory:", embed: Embed | None = None):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.embed = embed
        self.db.executescript(
            "CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, owner TEXT, coll TEXT,"
            " source TEXT, idx INTEGER, text TEXT, meta TEXT, vec BLOB, hash TEXT);"
            "CREATE INDEX IF NOT EXISTS ix_scope ON chunks(owner, coll, source);"
        )

    async def ingest_text(
        self,
        owner: str,
        coll: str,
        source: str,
        text: str,
        meta: dict | None = None,
        **chunk_kw,
    ) -> int:
        """(Re)index one source: replaces its old chunks. Returns chunks stored."""
        if self.embed is None:
            raise RuntimeError("no embedding function configured")
        chunks = chunk_text(text, **chunk_kw)
        digest = hashlib.sha256(text.encode()).hexdigest()
        row = self.db.execute(
            "SELECT hash FROM chunks WHERE owner=? AND coll=? AND source=? LIMIT 1",
            (owner, coll, source),
        ).fetchone()
        if row and row[0] == digest:
            return 0  # unchanged: skip paid embedding calls
        total = self.db.execute(
            "SELECT COUNT(*) FROM chunks WHERE owner=? AND coll=?", (owner, coll)
        ).fetchone()[0]
        if total + len(chunks) > MAX_CHUNKS_PER_COLLECTION:
            raise ValueError("collection size limit reached")
        vecs = await self.embed(chunks) if chunks else []
        if len(vecs) != len(chunks):
            raise RuntimeError("embedding count mismatch")
        self.db.execute(
            "DELETE FROM chunks WHERE owner=? AND coll=? AND source=?",
            (owner, coll, source),
        )
        self.db.executemany(
            "INSERT INTO chunks (owner, coll, source, idx, text, meta, vec, hash) VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    owner,
                    coll,
                    source,
                    i,
                    c,
                    json.dumps(meta or {}),
                    _pack(_norm(v)),
                    digest,
                )
                for i, (c, v) in enumerate(zip(chunks, vecs))
            ],
        )
        self.db.commit()
        return len(chunks)

    async def ingest_file(self, owner: str, coll: str, path: str | Path, **kw) -> int:
        return await self.ingest_text(
            owner, coll, Path(path).name, read_document(path), **kw
        )

    async def search(
        self, owner: str, coll: str, query: str, k: int = 4, min_score: float = 0.0
    ) -> list[dict]:
        if self.embed is None:
            raise RuntimeError("no embedding function configured")
        q = _norm((await self.embed([query]))[0])
        rows = self.db.execute(
            "SELECT source, idx, text, meta, vec FROM chunks WHERE owner=? AND coll=?",
            (owner, coll),
        ).fetchall()
        hits = []
        for source, idx, text, meta, vec in rows:
            v = _unpack(vec)
            if len(v) != len(q):
                continue  # stale index from a different embedding model
            score = sum(a * b for a, b in zip(q, v))
            if score >= min_score:
                hits.append(
                    {
                        "source": source,
                        "chunk": idx,
                        "text": text,
                        "score": score,
                        "meta": json.loads(meta),
                    }
                )
        hits.sort(key=lambda h: -h["score"])
        return hits[:k]

    def delete_source(self, owner: str, coll: str, source: str) -> int:
        n = self.db.execute(
            "DELETE FROM chunks WHERE owner=? AND coll=? AND source=?",
            (owner, coll, source),
        ).rowcount
        self.db.commit()
        return n

    def sources(self, owner: str, coll: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT source, COUNT(*) FROM chunks WHERE owner=? AND coll=? GROUP BY source ORDER BY source",
            (owner, coll),
        ).fetchall()
        return [{"source": s, "chunks": n} for s, n in rows]
