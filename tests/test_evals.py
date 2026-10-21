import pytest

from evals import Task, compare, load_tasks, run_suite, score
from evals.runner import regressions
from providers.base import CompletionResponse


def fake(answers, cost=0.01, lat=100.0):
    async def route(req):
        a = answers[req.messages[0].content]
        if isinstance(a, Exception):
            raise a
        return CompletionResponse(a, "m", "p", 5, 5, cost, lat)

    return route


@pytest.mark.asyncio
async def test_scorers():
    assert await score({"type": "exact", "expected": "Paris"}, " paris\n") == 1
    assert await score({"type": "contains", "expected": ["a", "b"]}, "xa") == 0.5
    assert await score({"type": "regex", "pattern": r"\d{3}"}, "id 123") == 1
    assert await score({"type": "json", "expected": {"ok": True}}, '{"ok": true}') == 1
    assert await score({"type": "json"}, "not json") == 0

    async def grader(prompt):
        return "Score: 8"

    assert await score({"type": "rubric", "rubric": "r"}, "x", grader) == 0.8
    with pytest.raises(ValueError):
        await score({"type": "rubric", "rubric": "r"}, "x")


def test_load_smoke_suite():
    tasks = load_tasks("evals")
    assert {t.id for t in tasks} >= {"arith", "capital", "json"}


@pytest.mark.asyncio
async def test_run_records_quality_cost_latency_and_errors():
    tasks = [
        Task("a", "q1", {"type": "exact", "expected": "x"}),
        Task("b", "q2", {"type": "exact", "expected": "y"}),
        Task("c", "q3", {"type": "exact", "expected": "z"}),
    ]
    r = await run_suite(
        tasks, fake({"q1": "x", "q2": "nope", "q3": RuntimeError("boom")})
    )
    assert r["n"] == 3 and r["errors"] == 1
    assert r["quality"] == pytest.approx(1 / 3)
    assert r["cost_usd"] == pytest.approx(0.02) and r["p50_ms"] == 100.0
    assert [x["error"] for x in r["results"]] == [None, None, "boom"]


@pytest.mark.asyncio
async def test_tag_filter_and_compare_and_regressions():
    tasks = [
        Task("a", "q1", {"type": "exact", "expected": "x"}, tags=["fast"]),
        Task("b", "q2", {"type": "exact", "expected": "y"}),
    ]
    good = fake({"q1": "x", "q2": "y"}, cost=0.05)
    cheap = fake({"q1": "x", "q2": "wrong"}, cost=0.0)
    out = await compare(tasks, {"cloud": good, "local": cheap})
    assert out["cloud"]["quality"] == 1 and out["local"]["quality"] == 0.5
    assert out["local"]["cost_usd"] == 0
    assert regressions(out["cloud"], out["local"]) == ["b"]
    only = await run_suite(tasks, good, tags=["fast"])
    assert only["n"] == 1


def test_duplicate_ids_rejected(tmp_path):
    (tmp_path / "a.yml").write_text("tasks: [{id: x, prompt: p, scorer: {}}]")
    (tmp_path / "b.yml").write_text("tasks: [{id: x, prompt: p, scorer: {}}]")
    with pytest.raises(ValueError, match="duplicate"):
        load_tasks(tmp_path)


@pytest.mark.asyncio
async def test_cli_runs_and_flags_regression(tmp_path):
    import json

    import httpx

    from evals.__main__ import main

    state = {"answer": "42"}

    def handler(req):
        return httpx.Response(
            200,
            json={"model": "m", "choices": [{"message": {"content": state["answer"]}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
            headers={"x-caips-cost-usd": "0.002", "x-caips-provider": "p"},
        )  # fmt: skip

    (tmp_path / "t.yml").write_text(
        "tasks: [{id: arith, prompt: q, scorer: {type: contains, expected: '42'}}]"
    )
    base, out = str(tmp_path / "base.json"), str(tmp_path / "out.json")
    tr = httpx.MockTransport(handler)
    assert await main([str(tmp_path / "t.yml"), "--out", base], transport=tr) == 0
    assert json.load(open(base))["cost_usd"] == pytest.approx(0.002)
    state["answer"] = "41"
    argv = [str(tmp_path / "t.yml"), "--out", out, "--baseline", base]
    assert await main(argv, transport=tr) == 1
