"""Long-running servers must not grow without bound."""

import pytest

from coreai.cache import MemoryCache, ResponseCache
from coreai.orchestrator import Orchestrator, TaskStatus, TaskStore


@pytest.mark.asyncio
async def test_memory_cache_never_exceeds_max_entries():
    cache = MemoryCache(max_entries=100)
    for i in range(10_000):
        await cache.set(f"k{i}", {"v": i}, ttl_seconds=3600)
    assert len(cache.store) == 100
    assert cache.evictions == 9_900


@pytest.mark.asyncio
async def test_memory_cache_evicts_least_recently_used():
    cache = MemoryCache(max_entries=3)
    for k in ("a", "b", "c"):
        await cache.set(k, {"k": k})
    await cache.get("a")  # a is now the most recently used
    await cache.set("d", {"k": "d"})  # evicts b
    assert await cache.get("b") is None
    assert await cache.get("a") == {"k": "a"}
    assert await cache.get("c") == {"k": "c"}


@pytest.mark.asyncio
async def test_expired_entries_are_dropped_before_live_ones_are_evicted():
    now = {"value": 10.0}
    cache = MemoryCache(max_entries=3, clock=lambda: now["value"])
    await cache.set("live1", {"k": 1})
    await cache.set("live2", {"k": 2})
    cache.store["stale"] = ({"k": 0}, 9.0)
    cache.store.move_to_end("stale", last=False)  # make it the oldest
    await cache.set("new", {"k": 3})  # over limit: purge expired first
    assert "stale" not in cache.store
    assert {"live1", "live2", "new"} <= set(cache.store)
    assert cache.evictions == 0


@pytest.mark.asyncio
async def test_purge_expired_reports_how_many():
    cache = MemoryCache(clock=lambda: 10.0)
    for i in range(5):
        cache.store[f"s{i}"] = ({}, 9.0)
    await cache.set("live", {})
    assert cache.purge_expired() == 5 and list(cache.store) == ["live"]


def test_memory_cache_rejects_nonsense_size():
    with pytest.raises(ValueError):
        MemoryCache(max_entries=0)


def test_response_cache_passes_size_to_its_default_backend():
    assert ResponseCache(max_entries=7).backend.max_entries == 7


# ------------------------------------------------------------------ task store


def finish(store, task, status=TaskStatus.COMPLETED):
    task.status = status
    store.update(task)


def test_task_store_keeps_only_the_most_recent_finished_tasks():
    store = TaskStore(max_finished=10)
    for i in range(50):
        finish(store, store.create("agent", f"task {i}"))
    assert len(store.tasks) == 10
    assert {t.objective for t in store.tasks.values()} == {
        f"task {i}" for i in range(40, 50)
    }


def test_task_store_never_drops_unfinished_tasks():
    store = TaskStore(max_finished=2)
    running = [store.create("agent", f"live {i}") for i in range(5)]
    for t in running:
        t.status = TaskStatus.RUNNING
        store.update(t)
    for i in range(20):
        finish(store, store.create("agent", f"done {i}"))
    assert all(store.get(t.task_id) is not None for t in running)
    assert len(store.tasks) == 5 + 2


def test_failed_and_cancelled_tasks_count_as_finished():
    store = TaskStore(max_finished=1)
    a, b = store.create("x", "a"), store.create("x", "b")
    finish(store, a, TaskStatus.FAILED)
    finish(store, b, TaskStatus.CANCELLED)
    assert store.get(a.task_id) is None and store.get(b.task_id) is not None


def test_deleting_a_task_also_forgets_it_from_retention():
    store = TaskStore(max_finished=2)
    a = store.create("x", "a")
    finish(store, a)
    store.delete(a.task_id)
    assert a.task_id not in store._finished


def test_orchestrator_uses_a_bounded_store():
    orch = Orchestrator()
    orch.register_agent("a1")
    for i in range(1500):
        task = orch.assign_task("a1", f"t{i}")
        orch.start_task(task.task_id)
        orch.complete_task(task.task_id, result={"ok": True})
    assert len(orch.task_store.tasks) <= 1000
