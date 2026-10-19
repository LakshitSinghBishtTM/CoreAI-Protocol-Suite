import asyncio

from coreai.cache import ResponseCache
from coreai.orchestrator import Orchestrator
from providers.base import CompletionRequest, Message, split_system
from providers.anthropic import AnthropicProvider


def test_cache_key_changes_with_every_option():
    c = ResponseCache()
    m = [{"role": "user", "content": "hi"}]
    base = {"system_prompt": None, "temperature": 0.7, "max_tokens": 50}
    k = c._make_key("p", "m", m, base)
    for opt, val in [("system_prompt", "be brief"), ("temperature", 0.1), ("max_tokens", 2000)]:
        assert c._make_key("p", "m", m, {**base, opt: val}) != k


def test_cache_miss_then_hit_respects_options():
    async def run():
        c = ResponseCache()
        m = [{"role": "user", "content": "hi"}]
        await c.set("p", "m", m, {"content": "x"}, options={"max_tokens": 50})
        assert await c.get("p", "m", m, {"max_tokens": 2000}) is None
        assert await c.get("p", "m", m, {"max_tokens": 50}) == {"content": "x"}

    asyncio.run(run())


def test_split_system_merges_prompt_and_system_messages():
    req = CompletionRequest(
        messages=[Message("system", "Answer in French"), Message("user", "hi")],
        system_prompt="Be brief",
    )
    system, rest = split_system(req)
    assert system == "Be brief\n\nAnswer in French"
    assert [m.role for m in rest] == ["user"]


def test_anthropic_keeps_system_messages():
    p = AnthropicProvider.__new__(AnthropicProvider)
    req = CompletionRequest(messages=[Message("system", "Answer in French"), Message("user", "hi")])
    assert split_system(req)[0] == "Answer in French"
    assert p._build_messages(req) == [{"role": "user", "content": "hi"}]


def test_task_priority_is_stored_and_ordered():
    o = Orchestrator()
    o.register_agent("a")
    lo = o.assign_task("a", "low", priority=0)
    hi = o.assign_task("a", "high", priority=3)
    assert hi.priority == 3
    from coreai.orchestrator import TaskStatus
    assert [t.task_id for t in o.task_store.list_by_status(TaskStatus.PENDING)] == [hi.task_id, lo.task_id]
