import tempfile
import threading

import pytest

from harness.store import RunStore
from providers.base import Message

pgserver = pytest.importorskip("pgserver")
pytest.importorskip("psycopg")


@pytest.fixture(scope="module")
def dsn():
    server = pgserver.get_server(tempfile.mkdtemp())
    yield server.get_uri()
    server.cleanup()


@pytest.fixture
def store(dsn):
    s = RunStore(dsn)
    s._x("DELETE FROM events")
    s._x("DELETE FROM runs")
    return s


def test_create_get_checkpoint_roundtrip(store):
    rid = store.create(
        "a", "alice", [Message(role="user", content="hi")], deadline=123.5
    )
    run = store.get(rid)
    assert run["status"] == "running" and run["deadline"] == 123.5
    store.checkpoint(
        rid,
        [Message(role="user", content="hi"), Message(role="assistant", content="yo")],
        step=1,
        cost_usd=0.25,
        pending={"tool": "x"},
    )
    run = store.get(rid)
    assert run["step"] == 1 and run["cost_usd"] == 0.25 and '"tool"' in run["pending"]
    assert [m.content for m in store.messages(rid)] == ["hi", "yo"]


def test_checkpoint_rolls_back_on_error(store):
    rid = store.create("a", "o", [Message(role="user", content="x")], None)
    with pytest.raises(Exception):
        store.checkpoint(
            rid, [Message(role="user", content="changed")], no_such_column=1
        )
    assert store.messages(rid)[0].content == "x"


def test_control_kill_list_interrupted(store):
    r1 = store.create("a", "alice", [], None)
    r2 = store.create("a", "bob", [], None)
    assert store.request(r1, "cancel") and store.get(r1)["control"] == "cancel"
    assert store.kill("bob") == 1 and store.get(r2)["control"] == "kill"
    assert {r["id"] for r in store.list_runs(owner="alice")} == {r1}
    store.set(r1, status="completed")
    assert store.interrupted() == [r2]
    assert store.request(r1, "cancel") is False


def test_events_are_sequential_under_threads(store):
    rid = store.create("a", "o", [], None)

    def worker():
        for _ in range(10):
            store.event(rid, "model", model="m")

    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert [e["seq"] for e in store.events(rid)] == list(range(1, 41))
    assert [e["seq"] for e in store.events(rid, after=38)] == [39, 40]


def test_two_stores_share_state_and_seq(dsn):
    a, b = RunStore(dsn), RunStore(dsn)
    rid = a.create("a", "o", [], None)
    assert a.event(rid, "x") == 1 and b.event(rid, "y") == 2
    assert b.get(rid)["agent"] == "a"
