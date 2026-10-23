import io
import json

import httpx
import pytest

from caips_cli.cli import main


class Server:
    """Records requests; answers from a {(method, path): (status, json)} table."""

    def __init__(self, table=None):
        self.table, self.seen = table or {}, []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else None
        self.seen.append(
            (
                req.method,
                req.url.raw_path.decode(),
                body,
                req.headers.get("authorization"),
            )
        )
        status, data = self.table.get((req.method, req.url.path), (200, {"ok": True}))
        return httpx.Response(status, json=data)


def run(argv, srv=None, monkeypatch=None):
    srv = srv or Server()
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, transport=httpx.MockTransport(srv), out=out, err=err)
    return code, out.getvalue(), err.getvalue(), srv


@pytest.mark.parametrize(
    "argv,method,path,body",
    [
        (["health"], "GET", "/readyz", None),
        (["providers"], "GET", "/v1/providers", None),
        (["models"], "GET", "/v1/models", None),
        (
            ["explain", "hi"],
            "POST",
            "/v1/routing/explain",
            {"messages": [{"role": "user", "content": "hi"}]},
        ),
        (
            ["explain", "hi", "--model", "fast", "--policy", "cheap"],
            "POST",
            "/v1/routing/explain",
            {
                "messages": [{"role": "user", "content": "hi"}],
                "model": "fast",
                "policy": "cheap",
            },
        ),
        (["policies"], "GET", "/v1/admin/policies", None),
        (["budgets"], "GET", "/v1/admin/budgets", None),
        (
            ["budgets", "--subject", "a b"],
            "GET",
            "/v1/admin/budgets?subject=a%20b",
            None,
        ),
        (
            ["audit", "--limit", "5", "--action", "x"],
            "GET",
            "/v1/admin/audit?limit=5&action=x",
            None,
        ),
        (["local", "list"], "GET", "/v1/local/models", None),
        (
            ["local", "pull", "org/repo:Q4_K_M"],
            "POST",
            "/v1/local/models/pull",
            {"model": "org/repo:Q4_K_M"},
        ),
        (["local", "unload", "tiny"], "POST", "/v1/local/models/tiny/unload", None),
        (
            ["runs", "start", "calc", "2+2"],
            "POST",
            "/v1/runs",
            {"agent": "calc", "task": "2+2"},
        ),
        (["runs", "status", "r1"], "GET", "/v1/runs/r1", None),
        (["runs", "cancel", "r1"], "POST", "/v1/runs/r1/cancel", None),
        (["runs", "kill"], "POST", "/v1/runs/kill", {"owner": None}),
        (["runs", "kill", "--owner", "bob"], "POST", "/v1/runs/kill", {"owner": "bob"}),
        (
            ["approvals", "approve", "a1"],
            "POST",
            "/v1/tools/approvals/a1",
            {"approve": True},
        ),
        (
            ["approvals", "deny", "a1"],
            "POST",
            "/v1/tools/approvals/a1",
            {"approve": False},
        ),
        (["finetune", "status", "j1"], "GET", "/v1/finetune/jobs/j1", None),
        (["finetune", "cancel", "j1"], "DELETE", "/v1/finetune/jobs/j1", None),
    ],
)
def test_each_command_is_one_documented_request(argv, method, path, body):
    code, out, err, srv = run(["--url", "http://gw"] + argv)
    assert code == 0 and err == ""
    assert [(m, p, b) for m, p, b, _ in srv.seen] == [(method, path, body)]
    assert json.loads(out) == {"ok": True}


def test_ids_are_percent_encoded():
    _, _, _, srv = run(["local", "rm", "../admin"])
    assert srv.seen[0][1] == "/v1/local/models/..%2Fadmin"


def test_api_key_sent_as_bearer(monkeypatch):
    monkeypatch.setenv("CAIPS_API_KEY", "sekret")
    _, _, _, srv = run(["health"])
    assert srv.seen[0][3] == "Bearer sekret"


def test_http_error_goes_to_stderr_with_exit_1():
    srv = Server({("GET", "/v1/runs/zzz"): (404, {"detail": "Run not found."})})
    code, out, err, _ = run(["runs", "status", "zzz"], srv)
    assert code == 1 and out == "" and "404" in err and "Run not found." in err


def test_unreachable_server():
    def boom(req):
        raise httpx.ConnectError("refused")

    err = io.StringIO()
    code = main(
        ["health"], transport=httpx.MockTransport(boom), out=io.StringIO(), err=err
    )
    assert code == 1 and "cannot reach" in err.getvalue()


def test_bad_usage_exits_2_without_traceback():
    assert run(["runs"])[0] == 2
    assert run(["nope"])[0] == 2


def test_finetune_reads_jsonl_and_passes_cost_limit(tmp_path):
    f = tmp_path / "d.jsonl"
    row = {
        "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
    }
    f.write_text(json.dumps(row) + "\n\n" + json.dumps(row) + "\n")
    _, _, _, srv = run(
        ["finetune", "submit", str(f), "--base-model", "gpt-4o-mini", "--max-cost", "5"]
    )
    m, p, body, _ = srv.seen[0]
    assert (m, p) == ("POST", "/v1/finetune/jobs") and len(body["examples"]) == 2
    assert body["max_cost_usd"] == 5.0 and body["hyperparam_preset"] == "balanced"
    _, _, _, srv = run(["finetune", "estimate", str(f), "--base-model", "gpt-4o-mini"])
    assert (
        srv.seen[0][1] == "/v1/finetune/estimate"
        and "max_cost_usd" not in srv.seen[0][2]
    )


def test_bad_jsonl_is_a_usage_error(tmp_path):
    f = tmp_path / "bad.jsonl"
    f.write_text('{"ok": 1}\n{not json}\n')
    code, _, err, srv = run(["finetune", "estimate", str(f), "--base-model", "m"])
    assert code == 2 and "bad.jsonl:2" in err and srv.seen == []


def test_eval_delegates(tmp_path):
    t = tmp_path / "t.yml"
    t.write_text(
        "tasks: [{id: a, prompt: q, scorer: {type: contains, expected: '42'}}]"
    )

    def chat(req):
        return httpx.Response(
            200, json={"model": "m", "choices": [{"message": {"content": "42"}}]}
        )

    out = tmp_path / "o.json"
    code = main(
        ["eval", str(t), "--out", str(out)], transport=httpx.MockTransport(chat)
    )
    assert code == 0 and json.loads(out.read_text())["quality"] == 1.0


def test_closed_pipe_is_quiet():
    class Closed(io.StringIO):
        def write(self, s):
            raise BrokenPipeError

    code = main(
        ["health"],
        transport=httpx.MockTransport(Server()),
        out=Closed(),
        err=io.StringIO(),
    )
    assert code == 0


def _sse_transport(text, status=200):
    return httpx.MockTransport(lambda req: httpx.Response(status, text=text))


def test_chat_streams_deltas_and_stops_at_done():
    sse = (
        'data: {"choices":[{"delta":{"content":"Hel"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"lo"}}]}\n\n'
        "data: [DONE]\n\n"
        'data: {"choices":[{"delta":{"content":"IGNORED"}}]}\n\n'
    )
    out, err = io.StringIO(), io.StringIO()
    code = main(["chat", "fast", "hi"], transport=_sse_transport(sse), out=out, err=err)
    assert code == 0 and out.getvalue() == "Hello\n"


def test_chat_stream_error_event_exits_1():
    sse = 'data: {"error":{"message":"boom"}}\n\n'
    out, err = io.StringIO(), io.StringIO()
    code = main(["chat", "fast", "hi"], transport=_sse_transport(sse), out=out, err=err)
    assert code == 1 and "boom" in err.getvalue()


def test_runs_watch_prints_one_event_per_line():
    sse = 'data: {"type":"step","n":1}\n\ndata: {"type":"done"}\n\n'
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["runs", "watch", "r1"], transport=_sse_transport(sse), out=out, err=err
    )
    assert code == 0
    assert [json.loads(x)["type"] for x in out.getvalue().splitlines()] == [
        "step",
        "done",
    ]


def test_watch_http_error():
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["runs", "watch", "r1"], transport=_sse_transport("nope", 404), out=out, err=err
    )
    assert code == 1 and "404" in err.getvalue()
