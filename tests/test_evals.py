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


def test_cli_compare_models(tmp_path, capsys):
    import asyncio
    import json

    import httpx

    from evals.__main__ import main

    def handler(request):
        model = json.loads(request.content)["model"]
        text = "42" if model == "best" else "wrong"
        return httpx.Response(
            200,
            json={
                "model": model,
                "choices": [{"message": {"content": text}}],
                "usage": {},
            },
        )

    tasks = tmp_path / "t.yml"
    tasks.write_text(
        "tasks:\n  - id: a\n    prompt: q\n    scorer: {type: contains, expected: '42'}\n"
    )
    out = tmp_path / "o.json"
    rc = asyncio.run(
        main(
            [str(tasks), "--compare", "cheap", "best", "--out", str(out)],
            transport=httpx.MockTransport(handler),
        )
    )
    res = json.loads(out.read_text())
    assert rc == 0 and res["best"]["quality"] == 1.0 and res["cheap"]["quality"] == 0.0


# ---------------------------------------------------------- agent tasks, grader


def _gateway(state):
    """A mock gateway: chat completions, a grader reply, and the run API."""
    import json

    import httpx

    def handler(req):
        if req.url.path == "/v1/chat/completions":
            body = json.loads(req.content)
            state.setdefault("models", []).append(body["model"])
            text = "8" if body["model"] == "judge" else "a fine answer"
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "choices": [{"message": {"content": text}}],
                    "usage": {},
                },
            )
        if req.method == "POST" and req.url.path == "/v1/runs":
            state["started"] = json.loads(req.content)
            return httpx.Response(202, json={"id": "r1", "status": "running"})
        if req.url.path == "/v1/runs/r1":
            state["polls"] = state.get("polls", 0) + 1
            if state["polls"] < 2:
                return httpx.Response(200, json={"id": "r1", "status": "running"})
            return httpx.Response(
                200,
                json={"id": "r1", "status": state.get("final", "completed"),
                      "stop_reason": "max_steps", "result": "the answer is 42",
                      "cost_usd": 0.03, "input_tokens": 10, "output_tokens": 5, "step": 3},
            )  # fmt: skip
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _suite(tmp_path, body):
    f = tmp_path / "t.yml"
    f.write_text(body)
    return str(f)


@pytest.mark.asyncio
async def test_agent_task_runs_through_the_run_api_and_is_scored(tmp_path):
    import json

    from evals.__main__ import main

    state = {}
    out = tmp_path / "o.json"
    tasks = _suite(
        tmp_path,
        "tasks:\n  - id: ag\n    agent: helper\n    prompt: what is 6*7\n"
        "    scorer: {type: contains, expected: '42'}\n",
    )
    rc = await main([tasks, "--out", str(out)], transport=_gateway(state))
    rep = json.loads(out.read_text())
    r = rep["results"][0]
    assert rc == 0 and rep["quality"] == 1.0
    assert state["started"] == {"agent": "helper", "task": "what is 6*7"}
    assert (r["agent"], r["run_id"], r["steps"]) == ("helper", "r1", 3)
    assert r["cost_usd"] == pytest.approx(0.03) and r["tokens"] == 15


@pytest.mark.asyncio
async def test_agent_run_that_stops_on_a_limit_scores_zero_with_the_reason(tmp_path):
    import json

    from evals.__main__ import main

    out = tmp_path / "o.json"
    tasks = _suite(
        tmp_path,
        "tasks: [{id: ag, agent: helper, prompt: p, scorer: {type: contains, expected: '42'}}]",
    )
    await main([tasks, "--out", str(out)], transport=_gateway({"final": "failed"}))
    r = json.loads(out.read_text())["results"][0]
    assert r["score"] == 0 and "max_steps" in r["error"]


@pytest.mark.asyncio
async def test_agent_task_without_a_run_api_is_an_error_not_a_crash():
    t = Task("ag", "p", {"type": "contains", "expected": "x"}, agent="helper")
    rep = await run_suite([t], fake({}))
    assert rep["errors"] == 1 and "run API" in rep["results"][0]["error"]


@pytest.mark.asyncio
async def test_cli_grades_rubric_tasks_with_a_grader_model(tmp_path):
    import json

    from evals.__main__ import main

    state = {}
    out = tmp_path / "o.json"
    tasks = _suite(
        tmp_path,
        "tasks: [{id: r, prompt: q, scorer: {type: rubric, rubric: be polite}}]",
    )
    rc = await main(
        [tasks, "--model", "worker", "--grader-model", "judge", "--out", str(out)],
        transport=_gateway(state),
    )
    rep = json.loads(out.read_text())
    assert rc == 0 and rep["results"][0]["score"] == pytest.approx(0.8)
    assert state["models"] == ["worker", "judge"]


@pytest.mark.asyncio
async def test_rubric_task_without_a_grader_model_reports_why(tmp_path):
    import json

    from evals.__main__ import main

    out = tmp_path / "o.json"
    tasks = _suite(
        tmp_path, "tasks: [{id: r, prompt: q, scorer: {type: rubric, rubric: x}}]"
    )
    await main([tasks, "--out", str(out)], transport=_gateway({}))
    assert "grader" in json.loads(out.read_text())["results"][0]["error"]
