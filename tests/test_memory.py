import pytest

from memory import MemoryStore


def test_remember_recall_overwrite():
    m = MemoryStore()
    m.remember("alice", "tz", "UTC+1")
    m.remember("alice", "tz", "UTC+2")
    assert m.recall("alice", "tz") == "UTC+2" and len(m.view("alice")) == 1


def test_owners_are_isolated():
    m = MemoryStore()
    m.remember("alice", "k", "secret plan")
    assert m.recall("bob", "k") is None and m.view("bob") == []
    assert m.forget("bob") == 0 and m.recall("alice", "k") == "secret plan"


def test_pii_redacted_on_write_unless_disabled():
    m = MemoryStore()
    m.remember("a", "contact", "mail bob@example.com")
    assert m.recall("a", "contact") == "mail [REDACTED:email]"
    raw = MemoryStore(redact=False)
    raw.remember("a", "contact", "mail bob@example.com")
    assert "bob@example.com" in raw.recall("a", "contact")


def test_forget_scopes():
    m = MemoryStore()
    for k in "abc":
        m.remember("u", k, "v", ns="n1")
    m.remember("u", "x", "v", ns="n2")
    assert m.forget("u", key="a", ns="n1") == 1
    assert m.forget("u", ns="n1") == 2
    assert m.forget("u") == 1 and m.view("u") == []


def test_retention_expires_old_items():
    t = [0.0]
    m = MemoryStore(retention_days=1, clock=lambda: t[0])
    m.remember("u", "old", "v")
    t[0] = 86400 * 0.5
    m.remember("u", "new", "v")
    t[0] = 86400 * 1.2
    assert m.recall("u", "old") is None and m.recall("u", "new") == "v"


def test_search_ranks_by_overlap():
    m = MemoryStore()
    m.remember("u", "diet", "vegetarian, allergic to peanuts")
    m.remember("u", "city", "lives in Lisbon")
    r = m.search("u", "what peanuts allergy diet")
    assert r[0]["key"] == "diet" and len(r) == 1


def test_limits(monkeypatch):
    import memory.store as s

    m = MemoryStore()
    with pytest.raises(ValueError):
        m.remember("u", "k", "x" * (s.MAX_VALUE_CHARS + 1))
    with pytest.raises(ValueError):
        m.remember("", "k", "v")
    monkeypatch.setattr(s, "MAX_ITEMS_PER_SCOPE", 2)
    t = [0.0]
    m.clock = lambda: t[0]
    for i, k in enumerate("abc"):
        t[0] = i
        m.remember("u", k, "v")
    assert [x["key"] for x in m.view("u")] == ["b", "c"]  # oldest evicted
