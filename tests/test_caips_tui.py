"""caips-tui: client, panes and app, against a fake gateway (httpx.MockTransport)."""

import json
import sys

import httpx
import pytest

pytest.importorskip("textual")

from textual.widgets import (
    Button,
    DataTable,
    Input,
    OptionList,
    Static,
)  # noqa: E402

from caips_tui.app import SECTIONS, CaipsTui  # noqa: E402
from caips_tui.client import ApiError, Client  # noqa: E402
from caips_tui.widgets import Confirm  # noqa: E402


class Server:
    """Records requests; answers from {(method, path): (status, body) | callable}."""

    def __init__(self, table=None):
        self.table, self.seen = table or {}, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        path = req.url.raw_path.decode().split("?")[0]  # raw: keeps %2F in ids visible
        self.seen.append(
            (
                req.method,
                path,
                dict(req.url.params),
                body,
                req.headers.get("authorization"),
            )
        )
        entry = self.table.get(
            (req.method, path), (404, {"detail": "no such route in the fake"})
        )
        if callable(entry):
            return entry(req)
        status, data = entry
        if isinstance(data, bytes):
            return httpx.Response(status, content=data)
        if isinstance(data, str):
            return httpx.Response(status, text=data)
        return httpx.Response(status, json=data)

    def calls(self, method, path):
        return [s for s in self.seen if s[0] == method and s[1] == path]


def sse(*frames, headers=None):
    """A handler answering with server-sent events. A frame is data, or (event, data)."""

    def handler(req):
        out = ""
        for f in frames:
            ev, data = f if isinstance(f, tuple) else (None, f)
            data = data if isinstance(data, str) else json.dumps(data)
            out += (f"event: {ev}\n" if ev else "") + f"data: {data}\n\n"
        return httpx.Response(
            200,
            content=out.encode(),
            headers={"content-type": "text/event-stream", **(headers or {})},
        )

    return handler


def make(table=None, section="chat"):
    srv = Server({("GET", "/health"): (200, {"status": "ok"}), **(table or {})})
    client = Client("http://gw", "tok", transport=httpx.MockTransport(srv))
    return CaipsTui(client, section), srv


async def settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


SIZE = (150, 48)


# ------------------------------------------------------------------ client
async def test_client_maps_errors_and_bodies():
    srv = Server(
        {
            ("GET", "/ok"): (200, {"a": 1}),
            ("GET", "/empty"): (204, b""),
            ("GET", "/text"): (200, "plain"),
            ("GET", "/fastapi"): (429, {"detail": "slow down"}),
            ("GET", "/openai"): (400, {"error": {"message": "bad param"}}),
            ("GET", "/html"): (502, "<html>bad gateway</html>"),
        }
    )
    c = Client("http://gw/", "secret", transport=httpx.MockTransport(srv))
    assert await c.request("GET", "/ok") == {"a": 1}
    assert await c.request("GET", "/empty") is None
    assert await c.request("GET", "/text") == "plain"
    for path, status, detail in (
        ("/fastapi", 429, "slow down"),
        ("/openai", 400, "bad param"),
        ("/html", 502, "<html>bad gateway</html>"),
    ):
        with pytest.raises(ApiError) as e:
            await c.request("GET", path)
        assert (e.value.status, e.value.detail) == (status, detail)
    assert srv.seen[0][4] == "Bearer secret"
    await c.aclose()


async def test_client_drops_blank_params_and_reports_network_failure():
    srv = Server({("GET", "/p"): (200, {})})
    c = Client("http://gw", transport=httpx.MockTransport(srv))
    await c.request("GET", "/p", params={"a": "1", "b": "", "c": None})
    assert srv.seen[0][2] == {"a": "1"} and srv.seen[0][4] is None

    def boom(req):
        raise httpx.ConnectError("refused")

    down = Client("http://gw", transport=httpx.MockTransport(boom))
    with pytest.raises(ApiError) as e:
        await down.request("GET", "/x")
    assert e.value.status == 0 and "cannot reach http://gw" in str(e.value)


async def test_client_sse_parsing_and_error_before_stream():
    srv = Server(
        {
            ("POST", "/s"): sse({"n": 1}, ("error", {"error": "x"}), "[DONE]"),
            ("POST", "/denied"): (401, {"detail": "bad token"}),
        }
    )
    c = Client("http://gw", transport=httpx.MockTransport(srv))
    got = [x async for x in c.stream("POST", "/s", json={})]
    assert got == [
        ("message", '{"n": 1}'),
        ("error", '{"error": "x"}'),
        ("message", "[DONE]"),
    ]
    with pytest.raises(ApiError) as e:
        [x async for x in c.stream("POST", "/denied", json={})]
    assert e.value.status == 401 and e.value.detail == "bad token"


# --------------------------------------------------------------------- app
async def test_app_boots_with_every_section_and_switches():
    app, srv = make(
        {
            ("GET", "/v1/providers"): (
                200,
                [
                    {
                        "name": "openai",
                        "available": True,
                        "circuit_state": "closed",
                        "health": 1.0,
                        "latency_ms": 120.5,
                        "model_list": ["a", "b"],
                        "error_kinds": {},
                    }
                ],
            )
        },
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert len(SECTIONS) == 18
        for sid, _, _ in SECTIONS:
            assert app.pane(sid) is not None
        assert "online" in str(app.query_one("#conn", Static).render())
        app.query_one("#nav", OptionList).highlighted = 1  # Providers
        await settle(app, pilot)
        assert app.section == "providers"
        table = app.pane("providers").query_one(DataTable)
        assert table.row_count == 1
        assert [str(c.label) for c in table.columns.values()][:3] == [
            "Provider",
            "Up",
            "Circuit",
        ]
        assert table.get_row_at(0)[5] == "2"  # number of models
    assert all(s[4] == "Bearer tok" for s in srv.seen)


async def test_app_shows_offline_when_gateway_down():
    def boom(req):
        raise httpx.ConnectError("refused")

    app = CaipsTui(Client("http://gw", transport=httpx.MockTransport(boom)))
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert "offline" in str(app.query_one("#conn", Static).render())
        assert "no API key" in str(app.query_one("#conn", Static).render())


async def test_opening_a_section_shows_http_errors_in_the_status_line():
    app, _ = make(
        {("GET", "/v1/tools/approvals"): (403, {"detail": "Scope required: admin"})},
        section="approvals",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        status = str(app.pane("approvals").query_one(".status", Static).render())
        assert "403" in status and "Scope required: admin" in status


# ------------------------------------------------------------------- panes
async def test_runs_actions_send_the_documented_requests():
    runs = [
        {
            "id": "r/1",
            "agent": "calc",
            "status": "waiting_approval",
            "owner": "u",
            "step": 2,
            "cost_usd": 0.0012,
            "stop_reason": None,
        }
    ]
    app, srv = make(
        {
            ("GET", "/v1/runs"): (200, {"object": "list", "data": runs}),
            ("POST", "/v1/runs"): (202, {"id": "r2"}),
            ("POST", "/v1/runs/r%2F1/resume"): (200, {"id": "r/1"}),
            ("POST", "/v1/runs/r%2F1/cancel"): (200, {"cancelled": True}),
            ("POST", "/v1/runs/r%2F1/pause"): (200, {"pausing": True}),
            ("POST", "/v1/runs/kill"): (200, {"killed": 3, "owner": None}),
            ("GET", "/v1/runs/r%2F1/events"): (
                200,
                {"data": [{"seq": 1, "type": "step"}]},
            ),
            ("GET", "/v1/runs/r%2F1/stream"): sse({"seq": 1}, {"seq": 2}, "[DONE]"),
        },
        section="runs",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("runs")
        assert pane.selected()["id"] == "r/1"

        pane.query_one("#fld-agent", Input).value = "calc"
        pane.query_one("#fld-task", Input).value = "17*23"
        await pane.perform("start")
        assert srv.calls("POST", "/v1/runs")[0][3] == {"agent": "calc", "task": "17*23"}

        await pane.perform("approve")
        await pane.perform("deny")
        await pane.perform("resume")
        bodies = [c[3] for c in srv.calls("POST", "/v1/runs/r%2F1/resume")]
        assert bodies == [
            {"approve": True},
            {"approve": False},
            {},
        ]  # ids are percent-encoded in the path

        await pane.perform("cancel")
        await pane.perform("pause")
        assert srv.calls("POST", "/v1/runs/r%2F1/cancel") and srv.calls(
            "POST", "/v1/runs/r%2F1/pause"
        )

        await pane.perform("events")
        assert "1 event" in str(pane.query_one(".status", Static).render())
        await pane.perform("follow")
        assert "2 event" in str(pane.query_one(".status", Static).render())

        pane.query_one("#fld-owner", Input).value = "alice"
        await pane.perform("kill")
        assert srv.calls("POST", "/v1/runs/kill")[0][3] == {"owner": "alice"}


async def test_run_start_needs_agent_and_task():
    app, srv = make({("GET", "/v1/runs"): (200, {"data": []})}, section="runs")
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await app.pane("runs").perform("start")
        assert not srv.calls("POST", "/v1/runs")
        assert "agent and task" in str(
            app.pane("runs").query_one(".status", Static).render()
        )


async def test_destructive_actions_ask_first_and_do_nothing_on_no():
    app, srv = make(
        {
            ("GET", "/v1/runs"): (200, {"data": []}),
            ("POST", "/v1/runs/kill"): (200, {"killed": 1}),
        },
        section="runs",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("runs")
        pane.trigger("kill")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        await pilot.press("n")
        await settle(app, pilot)
        assert not srv.calls("POST", "/v1/runs/kill")
        pane.trigger("kill")
        await pilot.pause()
        await pilot.press("y")
        await settle(app, pilot)
        assert len(srv.calls("POST", "/v1/runs/kill")) == 1


async def test_action_needing_a_row_says_so_when_table_empty():
    app, srv = make({("GET", "/v1/runs"): (200, {"data": []})}, section="runs")
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.pane("runs").trigger("cancel")
        await pilot.pause()
        assert "Select a row" in str(
            app.pane("runs").query_one(".status", Static).render()
        )
        assert not isinstance(app.screen, Confirm)


async def test_approvals_and_tools():
    app, srv = make(
        {
            ("GET", "/v1/tools/approvals"): (
                200,
                {
                    "data": [
                        {
                            "id": "a1",
                            "caller": "user:me",
                            "tool": "shell",
                            "arguments": {"cmd": "ls"},
                            "status": "pending",
                            "decided_by": None,
                        }
                    ]
                },
            ),
            ("POST", "/v1/tools/approvals/a1"): (
                200,
                {"tool": "shell", "status": "approved"},
            ),
            ("GET", "/v1/tools"): (
                200,
                {
                    "data": [
                        {
                            "name": "calc",
                            "description": "math",
                            "version": "1",
                            "owner": "core",
                            "permission": "read",
                            "parameters": {},
                        }
                    ]
                },
            ),
            ("POST", "/mcp"): lambda req: httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": "391"}]},
                },
            ),
        },
        section="approvals",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await app.pane("approvals").perform("approve")
        assert srv.calls("POST", "/v1/tools/approvals/a1")[0][3] == {"approve": True}

        tools = app.pane("tools")
        await tools.activate()
        tools.query_one("#fld-args", Input).value = '{"expression": "17*23"}'
        await tools.perform("call")
        mcp = srv.calls("POST", "/mcp")[0][3]
        assert mcp["method"] == "tools/call" and mcp["params"] == {
            "name": "calc",
            "arguments": {"expression": "17*23"},
        }
        tools.query_one("#fld-args", Input).value = "[1]"
        await tools.perform("call")
        assert len(srv.calls("POST", "/mcp")) == 1  # a non-object was rejected locally
        assert "object" in str(tools.query_one(".status", Static).render())


async def test_tools_show_mcp_errors():
    app, _ = make(
        {
            ("GET", "/v1/tools"): (
                200,
                {
                    "data": [
                        {
                            "name": "x",
                            "description": "",
                            "version": "1",
                            "owner": "o",
                            "permission": "read",
                        }
                    ]
                },
            ),
            ("POST", "/mcp"): (
                200,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32000, "message": "denied"},
                },
            ),
        },
        section="tools",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await app.pane("tools").perform("call")
        assert "denied" in str(app.pane("tools").query_one(".status", Static).render())


async def test_tasks_pane_follows_submitted_ids():
    task = {
        "task_id": "t1",
        "agent_id": "a",
        "objective": "do",
        "status": "running",
        "iterations": 1,
        "created_at": "now",
    }
    app, srv = make(
        {
            ("POST", "/v1/tasks"): (202, {**task, "status": "queued"}),
            ("GET", "/v1/tasks/t1"): (200, task),
            ("DELETE", "/v1/tasks/t1"): (200, {}),
        },
        section="tasks",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("tasks")
        assert pane.query_one(DataTable).row_count == 0
        pane.query_one("#fld-agent", Input).value = "a"
        pane.query_one("#fld-objective", Input).value = "do"
        await pane.perform("submit")
        assert srv.calls("POST", "/v1/tasks")[0][3] == {
            "agent_id": "a",
            "objective": "do",
            "max_iterations": 10,
            "priority": 2,
        }
        assert pane.query_one(DataTable).row_count == 1
        await pane.perform("cancel")
        assert srv.calls("DELETE", "/v1/tasks/t1")
        pane.query_one("#fld-max_iter", Input).value = "ten"
        await pane.perform("submit")
        assert len(srv.calls("POST", "/v1/tasks")) == 1


async def test_agents_register_and_terminate():
    app, srv = make(
        {
            ("GET", "/v1/agents"): (
                200,
                [
                    {
                        "agent_id": "ag1",
                        "status": "idle",
                        "task_count": 0,
                        "error_count": 0,
                    }
                ],
            ),
            ("POST", "/v1/agents"): (201, {"agent_id": "ag2", "status": "registered"}),
            ("DELETE", "/v1/agents/ag1"): (204, b""),
        },
        section="agents",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("agents")
        pane.query_one("#fld-name", Input).value = "w"
        pane.query_one("#fld-caps", Input).value = "a, b ,"
        await pane.perform("register")
        assert srv.calls("POST", "/v1/agents")[0][3] == {
            "name": "w",
            "capabilities": ["a", "b"],
        }
        await pane.perform("terminate")
        assert srv.calls("DELETE", "/v1/agents/ag1")


async def test_keys_pane_add_clears_secret_and_remove():
    app, srv = make(
        {
            ("GET", "/v1/keys"): (
                200,
                {
                    "data": [
                        {
                            "id": "ab12",
                            "provider": "openai",
                            "label": "1",
                            "key": "…1234",
                            "source": "store",
                            "loaded": True,
                        }
                    ]
                },
            ),
            ("POST", "/v1/keys"): (
                201,
                {"provider": "qwen", "label": "1", "loaded": True},
            ),
            ("DELETE", "/v1/keys/ab12"): (200, {"deleted": "ab12"}),
        },
        section="keys",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("keys")
        pane.query_one("#fld-provider", Input).value = "qwen"
        pane.query_one("#fld-key", Input).value = "q-secret-1234"
        assert pane.query_one("#fld-key", Input).password
        await pane.perform("add")
        assert srv.calls("POST", "/v1/keys")[0][3]["api_key"] == "q-secret-1234"
        assert pane.query_one("#fld-key", Input).value == ""
        await pane.perform("remove")
        assert srv.calls("DELETE", "/v1/keys/ab12")


async def test_memory_pane():
    app, srv = make(
        {
            ("GET", "/v1/memory"): (
                200,
                {
                    "data": [
                        {"ns": "default", "key": "k/1", "value": "v", "updated": 1.0},
                        {"ns": "work", "key": "k/1", "value": "w", "updated": 2.0},
                    ]
                },
            ),
            ("PUT", "/v1/memory/k%2F2"): (200, {}),
            ("GET", "/v1/memory/search"): (
                200,
                {"data": [{"ns": "work", "key": "k/1", "value": "w", "updated": 2.0}]},
            ),
            ("DELETE", "/v1/memory/k%2F1"): (200, {"deleted": 1}),
            ("DELETE", "/v1/memory"): (200, {"deleted": 5}),
        },
        section="memory",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("memory")
        assert (
            pane.query_one(DataTable).row_count == 2
        )  # same key in two namespaces stays two rows
        pane.query_one("#fld-key", Input).value = "k/2"
        pane.query_one("#fld-value", Input).value = "hello"
        await pane.perform("put")
        put = srv.calls("PUT", "/v1/memory/k%2F2")[0]
        assert put[2] == {"ns": "default"} and put[3] == {"value": "hello"}

        pane.query_one("#fld-query", Input).value = "world"
        await pane.perform("search")
        assert pane.query_one(DataTable).row_count == 1
        assert srv.calls("GET", "/v1/memory/search")[0][2] == {
            "q": "world",
            "limit": "20",
        }

        await pane.perform("delete", {"ns": "work", "key": "k/1"})
        assert srv.calls("DELETE", "/v1/memory/k%2F1")[0][2] == {"ns": "work"}
        pane.query_one("#fld-ns", Input).value = "work"
        await pane.perform("forget")
        assert srv.calls("DELETE", "/v1/memory")[0][2] == {"ns": "work"}


async def test_rag_pane_ingest_search_delete(tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("hello rag")
    app, srv = make(
        {
            ("GET", "/v1/rag/default/documents"): (
                200,
                {"data": [{"source": "a/b.txt", "chunks": 2}]},
            ),
            ("POST", "/v1/rag/default/documents"): (201, {"chunks": 1}),
            ("POST", "/v1/rag/default/search"): (
                200,
                {
                    "data": [
                        {
                            "source": "a/b.txt",
                            "idx": 0,
                            "text": "x" * 200,
                            "score": 0.91234,
                        }
                    ]
                },
            ),
            ("DELETE", "/v1/rag/default/documents/a/b.txt"): (
                200,
                {"deleted": "a/b.txt", "chunks": 2},
            ),
        },
        section="rag",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("rag")
        pane.query_one("#fld-file", Input).value = str(doc)
        await pane.perform("ingest")
        assert srv.calls("POST", "/v1/rag/default/documents")[0][3] == {
            "source": str(doc),
            "text": "hello rag",
        }
        pane.query_one("#fld-file", Input).value = str(tmp_path / "missing.txt")
        await pane.perform("ingest")
        assert len(srv.calls("POST", "/v1/rag/default/documents")) == 1
        assert "cannot read" in str(pane.query_one(".status", Static).render())

        pane.query_one("#fld-query", Input).value = "rag?"
        pane.query_one("#fld-k", Input).value = "99"
        await pane.perform("search")
        assert srv.calls("POST", "/v1/rag/default/search")[0][3] == {
            "query": "rag?",
            "k": 20,
        }  # clamped to the server's limit
        await pane.perform("delete", {"source": "a/b.txt"})
        assert srv.calls(
            "DELETE", "/v1/rag/default/documents/a/b.txt"
        )  # slashes kept in the path


async def test_feedback_pane(tmp_path):
    app, srv = make(
        {
            ("GET", "/v1/feedback"): (
                200,
                {
                    "summary": {"up": 3, "down": 1},
                    "data": [
                        {
                            "request_id": "r1",
                            "rating": -1,
                            "model": "m",
                            "comment": "",
                            "correction": "better",
                        }
                    ],
                },
            ),
            ("POST", "/v1/feedback"): (201, {"request_id": "r2"}),
            ("GET", "/v1/feedback/eval-tasks"): (
                200,
                {"tasks": [{"prompt": "p", "expected": "e"}]},
            ),
            ("DELETE", "/v1/feedback/r1"): (200, {"deleted": "r1"}),
        },
        section="feedback",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("feedback")
        assert "3 up / 1 down" in str(pane.query_one(".status", Static).render())
        await pane.perform("submit")  # nothing filled in
        assert not srv.calls("POST", "/v1/feedback")
        pane.query_one("#fld-rating", Input).value = "-1"
        pane.query_one("#fld-request", Input).value = "r2"
        pane.query_one("#fld-correction", Input).value = "fix"
        await pane.perform("submit")
        body = srv.calls("POST", "/v1/feedback")[0][3]
        assert (
            body["rating"] == -1
            and body["request_id"] == "r2"
            and body["correction"] == "fix"
        )
        # the rating field also filters the list
        assert srv.calls("GET", "/v1/feedback")[-1][2] == {"rating": "-1"}
        pane.query_one("#fld-out", Input).value = str(tmp_path / "fb.jsonl")
        await pane.perform("export")
        assert json.loads((tmp_path / "fb.jsonl").read_text()) == {
            "prompt": "p",
            "expected": "e",
        }


async def test_local_models_pull_has_no_timeout():
    app, srv = make(
        {
            ("GET", "/v1/local/models"): (
                200,
                {"data": [{"id": "m1", "loaded": True, "size_gb": 4.1}]},
            ),
            ("POST", "/v1/local/models/pull"): (202, {"id": "m2", "file": "m2.gguf"}),
            ("POST", "/v1/local/models/m1/unload"): (200, {"unloaded": True}),
            ("DELETE", "/v1/local/models/m1"): (200, {"deleted": "m1"}),
            ("GET", "/v1/local/models/m1"): (200, {"id": "m1", "loaded": True}),
        },
        section="local",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("local")
        await pane.perform("pull")
        assert not srv.calls("POST", "/v1/local/models/pull")
        pane.query_one("#fld-model", Input).value = "org/repo:Q4_K_M"
        await pane.perform("pull")
        assert srv.calls("POST", "/v1/local/models/pull")[0][3] == {
            "model": "org/repo:Q4_K_M"
        }
        await pane.perform("unload")
        await pane.perform("rm")
        await pane.perform("inspect")
        assert srv.calls("DELETE", "/v1/local/models/m1") and srv.calls(
            "GET", "/v1/local/models/m1"
        )


async def test_finetune_estimate_submit_and_dataset_errors(tmp_path):
    data = tmp_path / "d.jsonl"
    data.write_text(
        '{"messages": [{"role": "user", "content": "hi"}]}\n\n{"messages": []}\n'
    )
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{nope\n")
    app, srv = make(
        {
            ("GET", "/v1/finetune/jobs"): (
                200,
                [
                    {
                        "job_id": "j1",
                        "status": "running",
                        "base_model": "gpt-4o-mini",
                        "provider": "openai",
                    }
                ],
            ),
            ("POST", "/v1/finetune/estimate"): (
                200,
                {"validation": {}, "estimated_usd": 1.5},
            ),
            ("POST", "/v1/finetune/jobs"): (201, {"job_id": "j2"}),
            ("DELETE", "/v1/finetune/jobs/j1"): (200, {}),
            ("GET", "/v1/finetune/jobs/j1"): (200, {"job_id": "j1"}),
        },
        section="finetune",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("finetune")
        await pane.perform("estimate")
        assert not srv.calls(
            "POST", "/v1/finetune/estimate"
        )  # needs a dataset and base model
        pane.query_one("#fld-dataset", Input).value = str(bad)
        pane.query_one("#fld-base", Input).value = "gpt-4o-mini"
        await pane.perform("estimate")
        assert "invalid JSON" in str(pane.query_one(".status", Static).render())
        pane.query_one("#fld-dataset", Input).value = str(data)
        await pane.perform("estimate")
        body = srv.calls("POST", "/v1/finetune/estimate")[0][3]
        assert (
            body["base_model"] == "gpt-4o-mini"
            and body["provider"] == "openai"
            and len(body["examples"]) == 2
        )
        pane.query_one("#fld-max_cost", Input).value = "5"
        await pane.perform("submit")
        assert srv.calls("POST", "/v1/finetune/jobs")[0][3]["max_cost_usd"] == 5.0
        await pane.perform("status")
        await pane.perform("cancel")
        assert srv.calls("DELETE", "/v1/finetune/jobs/j1")


async def test_admin_shutdown_validates_mode_and_sends_query():
    app, srv = make(
        {
            ("GET", "/v1/admin/audit"): (
                200,
                {
                    "events": [
                        {
                            "ts": 1.0,
                            "action": "tool.approval",
                            "actor": "root",
                            "outcome": "ok",
                        }
                    ]
                },
            ),
            ("POST", "/v1/admin/shutdown"): (
                200,
                {"status": "shutdown_initiated", "mode": "graceful"},
            ),
        },
        section="admin",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("admin")
        assert pane.query_one(DataTable).row_count == 1
        assert srv.calls("GET", "/v1/admin/audit")[0][2] == {"limit": "100"}
        pane.query_one("#fld-mode", Input).value = "now"
        await pane.perform("shutdown")
        assert not srv.calls("POST", "/v1/admin/shutdown")
        pane.query_one("#fld-mode", Input).value = "graceful"
        pane.query_one("#fld-message", Input).value = "maintenance"
        await pane.perform("shutdown")
        assert srv.calls("POST", "/v1/admin/shutdown")[0][2] == {
            "mode": "graceful",
            "message": "maintenance",
        }


async def test_models_usage_and_status_panes(tmp_path):
    app, srv = make(
        {
            ("GET", "/v1/models/details"): (
                200,
                [
                    {
                        "model_id": "gpt-x",
                        "provider": "openai",
                        "tier": "fast",
                        "context_window": 128000,
                        "max_output_tokens": 4096,
                        "modalities": ["text"],
                        "supports_function_calling": True,
                        "input_per_million_usd": 0.15,
                        "output_per_million_usd": 0.6,
                    }
                ],
            ),
            ("GET", "/v1/models"): (
                200,
                {
                    "data": [
                        {"id": "gpt-x", "owned_by": "openai"},
                        {"id": "fast", "owned_by": "caips"},
                    ]
                },
            ),
            ("GET", "/v1/usage/report"): lambda req: (
                httpx.Response(200, text="a,b\n1,2\n")
                if req.url.params.get("format") == "csv"
                else httpx.Response(
                    200,
                    json={
                        "bucket": "day",
                        "rows": [
                            {
                                "bucket": 1,
                                "provider": "openai",
                                "requests": 3,
                                "cost_usd": 0.01,
                            }
                        ],
                    },
                )
            ),
            ("GET", "/v1/usage"): (200, {"subject": "user:me", "budget": None}),
            ("GET", "/readyz"): (200, {"ready": True}),
            ("GET", "/metrics"): (200, "# HELP x\nx 1\n"),
            ("GET", "/v1/stats"): (403, {"detail": "Scope required: admin"}),
        },
        section="models",
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        models = app.pane("models")
        models.query_one("#fld-provider", Input).value = "openai"
        models.query_one("#fld-min_context", Input).value = "100000"
        await models.refresh_data()
        assert srv.calls("GET", "/v1/models/details")[-1][2] == {
            "provider": "openai",
            "min_context": "100000",
        }
        await models.perform("gateway")
        assert "OpenAI-compatible" in str(models.query_one(".status", Static).render())

        usage = app.pane("usage")
        await usage.activate()
        assert usage.query_one(DataTable).row_count == 1
        assert srv.calls("GET", "/v1/usage/report")[0][2] == {
            "bucket": "day",
            "group_by": "provider,model",
        }
        usage.query_one("#fld-csv", Input).value = str(tmp_path / "u.csv")
        await usage.perform("export")
        assert (tmp_path / "u.csv").read_text() == "a,b\n1,2\n"
        await usage.perform("budget")

        status = app.pane("status")
        await status.activate()
        assert srv.calls("GET", "/readyz")
        await status.on_button_pressed(
            Button.Pressed(status.query_one("#view-metrics", Button))
        )
        assert srv.calls("GET", "/metrics")
        await status.on_button_pressed(
            Button.Pressed(status.query_one("#view-stats", Button))
        )
        assert "403" in str(status.query_one(".status", Static).render())


async def test_evals_command_keeps_the_key_off_argv(monkeypatch):
    monkeypatch.setenv("CAIPS_API_KEY", "s3cret-token")
    app, _ = make(section="evals")
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("evals")
        pane.query_one("#fld-model", Input).value = "fast"
        pane.query_one("#fld-baseline", Input).value = "old.json"
        cmd = pane.command()
        assert cmd[1:3] == ["-m", "evals"] and "--model" in cmd and "--baseline" in cmd
        assert cmd[cmd.index("--base-url") + 1] == "http://gw"
        assert "s3cret-token" not in " ".join(cmd)


async def test_evals_streams_subprocess_output_and_exit_code(monkeypatch):
    app, _ = make(section="evals")
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        pane = app.pane("evals")
        monkeypatch.setattr(
            pane,
            "command",
            lambda: [sys.executable, "-c", "print('suite ok'); raise SystemExit(1)"],
        )
        assert await pane.run_suite() == 0 + 1
        assert "exit code 1" in str(pane.query_one(".status", Static).render())
        assert any("suite ok" in str(line) for line in pane.detail_log().lines)


# -------------------------------------------------------------------- chat
OPENAI_STREAM = sse(
    {
        "id": "chatcmpl-1",
        "model": "m1",
        "choices": [{"delta": {"role": "assistant", "content": ""}}],
    },
    {"id": "chatcmpl-1", "model": "m1", "choices": [{"delta": {"content": "Hel"}}]},
    {"id": "chatcmpl-1", "model": "m1", "choices": [{"delta": {"content": "lo"}}]},
    {
        "id": "chatcmpl-1",
        "model": "m1",
        "choices": [],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    },
    "[DONE]",
    headers={"x-caips-provider": "openai", "x-caips-cost-usd": "0.0002"},
)


def transcript(app):
    return [str(w.render()) for w in app.pane("chat").query(".msg")]


async def say(app, pilot, text):
    box = app.pane("chat").query_one("#prompt", Input)
    box.value = text
    (
        await pilot.press("enter")
        if app.focused is box
        else await app.pane("chat").on_input_submitted(Input.Submitted(box, box.value))
    )
    await settle(app, pilot)


async def test_chat_streams_openai_replies_and_shows_usage():
    app, srv = make({("POST", "/v1/chat/completions"): OPENAI_STREAM})
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        chat = app.pane("chat")
        chat.query_one("#fld-model", Input).value = "fast"
        chat.query_one("#fld-system", Input).value = "be brief"
        await say(app, pilot, "hi")
        body = srv.calls("POST", "/v1/chat/completions")[0][3]
        assert body["stream"] is True and body["stream_options"] == {
            "include_usage": True
        }
        assert (
            body["model"] == "fast"
            and body["temperature"] == 0.7
            and body["max_tokens"] == 1024
        )
        assert body["messages"] == [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ]
        t = transcript(app)
        assert t[0] == "hi" and t[1] == "Hello"
        assert "openai" in t[2] and "5 in / 2 out" in t[2] and "$0.000200" in t[2]
        assert chat.history[-1] == {"role": "assistant", "content": "Hello"}
        await say(app, pilot, "again")  # history is sent back
        assert (
            len(srv.calls("POST", "/v1/chat/completions")[1][3]["messages"]) == 4
        )  # system + 3 turns


async def test_chat_without_streaming_and_with_server_tools():
    app, srv = make(
        {
            ("POST", "/v1/chat/completions"): lambda req: httpx.Response(
                200,
                json={
                    "id": "c9",
                    "model": "m",
                    "choices": [{"message": {"role": "assistant", "content": "plain"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
                headers={"x-caips-provider": "grok"},
            )
        }
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        chat = app.pane("chat")
        chat.set_flag("stream", False)
        chat.set_flag("tools", True)
        await say(app, pilot, "hello")
        body = srv.calls("POST", "/v1/chat/completions")[0][3]
        assert (
            body["stream"] is False
            and body["run_tools"] is True
            and "stream_options" not in body
        )
        assert transcript(app)[1] == "plain" and "grok" in transcript(app)[2]


async def test_chat_native_and_anthropic_dialects():
    app, srv = make(
        {
            ("POST", "/v1/completions/stream"): sse(
                {"content": "na"}, {"content": "tive"}, "[DONE]"
            ),
            ("POST", "/v1/completions"): (
                200,
                {
                    "content": "whole",
                    "model": "m",
                    "provider": "gemini",
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "cost_usd": 0.5,
                    "latency_ms": 9.0,
                    "cached": True,
                    "request_id": "req-1",
                },
            ),
            ("POST", "/v1/messages"): sse(
                (
                    "message_start",
                    {
                        "type": "message_start",
                        "message": {
                            "id": "msg_1",
                            "model": "claude-x",
                            "usage": {"input_tokens": 7},
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Hi "},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "there"},
                    },
                ),
                (
                    "message_delta",
                    {"type": "message_delta", "usage": {"output_tokens": 2}},
                ),
            ),
        }
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        chat = app.pane("chat")
        chat.query_one("#fld-policy", Input).value = "cheapest"
        chat.query_one("#fld-system", Input).value = "sys"
        await say(app, pilot, "/mode native")
        await say(app, pilot, "one")
        sent = srv.calls("POST", "/v1/completions/stream")[0][3]
        assert (
            sent["stream"] is True
            and sent["policy"] == "cheapest"
            and sent["system_prompt"] == "sys"
        )
        assert "native" in transcript(app)

        chat.set_flag("stream", False)
        await say(app, pilot, "two")
        assert "whole" in transcript(app) and any(
            "cached" in line and "gemini" in line for line in transcript(app)
        )
        assert chat.last["request_id"] == "req-1"

        await say(app, pilot, "/clear")
        chat.set_flag("stream", True)
        await say(app, pilot, "/mode anthropic")
        chat.query_one("#fld-temperature", Input).value = "1.7"
        await say(app, pilot, "three")
        body = srv.calls("POST", "/v1/messages")[0][3]
        assert (
            body["system"] == "sys"
            and body["temperature"] == 1.0
            and body["max_tokens"] == 1024
        )  # clamped to Anthropic's range
        assert "Hi there" in transcript(app) and any(
            "7 in / 2 out" in line for line in transcript(app)
        )


async def test_chat_error_leaves_history_clean_and_can_retry():
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                503, json={"detail": {"error": "all_providers_unavailable"}}
            )
        return OPENAI_STREAM(req)

    app, srv = make({("POST", "/v1/chat/completions"): flaky})
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        chat = app.pane("chat")
        await say(app, pilot, "hello")
        assert chat.history == []  # the failed turn is not remembered
        assert any("503" in line for line in transcript(app))
        await say(app, pilot, "/retry")
        assert [m["content"] for m in chat.history] == ["hello", "Hello"]
        assert [
            m["content"]
            for m in srv.calls("POST", "/v1/chat/completions")[1][3]["messages"]
        ] == ["hello"]


async def test_chat_stream_error_event_is_reported():
    app, _ = make(
        {
            ("POST", "/v1/chat/completions"): sse(
                {"id": "c", "choices": [{"delta": {"content": "part"}}]},
                ("error", {"error": {"message": "upstream died"}}),
            )
        }
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await say(app, pilot, "hi")
        assert any(
            "part" in line and "upstream died" in line for line in transcript(app)
        )
        assert app.pane("chat").history == []


async def test_chat_rejects_bad_numbers_without_calling_the_api():
    app, srv = make({("POST", "/v1/chat/completions"): OPENAI_STREAM})
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.pane("chat").query_one("#fld-max_tokens", Input).value = "lots"
        await say(app, pilot, "hi")
        assert not srv.calls("POST", "/v1/chat/completions")
        assert "whole number" in str(
            app.pane("chat").query_one(".status", Static).render()
        )


async def test_chat_explain_feedback_save_and_commands(tmp_path):
    app, srv = make(
        {
            ("POST", "/v1/chat/completions"): OPENAI_STREAM,
            ("POST", "/v1/routing/explain"): (
                200,
                {"chosen": {"provider": "openai"}, "candidates": []},
            ),
            ("POST", "/v1/feedback"): (201, {"request_id": "chatcmpl-1"}),
        }
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        chat = app.pane("chat")
        await say(app, pilot, "/good")
        assert any("Nothing to rate" in line for line in transcript(app))
        await say(app, pilot, "/explain")
        assert not srv.calls("POST", "/v1/routing/explain")
        await say(app, pilot, "/explain what about this")
        assert srv.calls("POST", "/v1/routing/explain")[0][3]["messages"] == [
            {"role": "user", "content": "what about this"}
        ]
        assert any(
            "Routing (dry run)" in line and "openai" in line for line in transcript(app)
        )

        await say(app, pilot, "capital of France?")
        await say(app, pilot, "/bad Paris")
        fb = srv.calls("POST", "/v1/feedback")[0][3]
        assert fb == {
            "request_id": "chatcmpl-1",
            "rating": -1,
            "comment": "",
            "correction": "Paris",
            "prompt": "capital of France?",
            "model": "m1",
        }
        await say(app, pilot, "/good thanks")
        assert srv.calls("POST", "/v1/feedback")[1][3]["comment"] == "thanks"

        out = tmp_path / "t.md"
        await say(app, pilot, f"/save {out}")
        assert "capital of France?" in out.read_text() and "Hello" in out.read_text()
        await say(app, pilot, "/model fast")
        assert chat.val("model") == "fast"
        await say(app, pilot, "/nonsense")
        assert any("Unknown command" in line for line in transcript(app))
        await say(app, pilot, "/clear")
        assert chat.history == [] and transcript(app) == []


async def test_chat_stop_cancels_a_slow_reply():
    import asyncio

    started = asyncio.Event()

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"c","choices":[{"delta":{"content":"tick"}}]}\n\n'
            started.set()
            await asyncio.sleep(30)
            yield b"data: [DONE]\n\n"

    def handler(req):
        if req.url.path == "/health":
            return httpx.Response(200, json={})
        return httpx.Response(
            200, stream=Slow(), headers={"content-type": "text/event-stream"}
        )

    app = CaipsTui(Client("http://gw", transport=httpx.MockTransport(handler)))
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        chat = app.pane("chat")
        chat.query_one("#prompt", Input).value = "go"
        await chat.on_input_submitted(
            Input.Submitted(chat.query_one("#prompt", Input), "go")
        )
        await asyncio.wait_for(started.wait(), 5)
        chat.action_stop()
        await settle(app, pilot)
        assert chat.busy is False and chat.history == []
        assert any("(stopped)" in line for line in transcript(app))


# ---------------------------------------------------------------- entry points
def test_parser_and_main_read_key_from_env_and_file(monkeypatch, tmp_path):
    import caips_tui.app as mod

    seen = {}
    monkeypatch.setattr(
        mod.CaipsTui,
        "run",
        lambda self, *a, **k: seen.update(
            client=self.client, section=self.start_section
        ),
    )
    monkeypatch.setenv("CAIPS_API_KEY", "from-env")
    assert mod.main(["--url", "http://x:1", "--section", "runs"]) == 0
    assert (
        seen["client"].base_url == "http://x:1"
        and seen["client"].has_key
        and seen["section"] == "runs"
    )
    keyfile = tmp_path / "k"
    keyfile.write_text("tok\n")
    monkeypatch.delenv("CAIPS_API_KEY")
    assert mod.main(["--api-key-file", str(keyfile)]) == 0 and seen["client"].has_key
    assert mod.main(["--api-key-file", str(tmp_path / "nope")]) == 2
    with pytest.raises(SystemExit):
        mod.main(["--api-key", "visible-in-ps"])  # deliberately not an option


def test_caips_tui_subcommand(monkeypatch):
    import io

    import caips_tui.app as mod
    from caips_cli.cli import main

    got = {}
    monkeypatch.setattr(
        mod, "main", lambda argv=None, transport=None: got.update(argv=argv) or 0
    )
    assert main(["--url", "http://gw:9", "tui", "--section", "memory"]) == 0
    assert got["argv"] == [
        "--url",
        "http://gw:9",
        "--timeout",
        "60",
        "--section",
        "memory",
    ]
    assert (
        main(["tui", "--bogus"], err=io.StringIO()) == 2
    )  # argparse usage error, same as every other command


def test_caips_tui_subcommand_explains_missing_textual(monkeypatch):
    import builtins
    import io

    from caips_cli.cli import main

    real = builtins.__import__

    def no_textual(name, *a, **k):
        if name.startswith("caips_tui"):
            raise ImportError("No module named 'textual'", name="textual")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_textual)
    err = io.StringIO()
    assert main(["tui"], err=err) == 2
    assert "pip install" in err.getvalue() and "[tui]" in err.getvalue()


async def test_theme_toggle_and_latency_readout():
    app = CaipsTui(
        Client(
            "http://gw",
            api_key="tok",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        )
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.theme == "caips-dark"
        assert " ms" in str(app.query_one("#conn", Static).render())
        await pilot.press("ctrl+t")
        assert app.theme == "caips-light"
        await pilot.press("ctrl+t")
        assert app.theme == "caips-dark"


async def test_command_palette_lists_every_section_and_jumps():
    from caips_tui.app import SectionCommands

    app = CaipsTui(
        Client(
            "http://gw",
            api_key="tok",
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
        )
    )
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        provider = SectionCommands(app.screen)
        names = [e[0] for e in provider._entries()]
        assert all(f"Go to {label}" in names for _, label, _ in SECTIONS)
        assert {"Refresh section", "Toggle light/dark theme"} <= set(names)
        await app.goto("usage")
        await settle(app, pilot)
        assert app.section == "usage"
