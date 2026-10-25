"""caips: a thin CLI over the HTTP API. Everything it does is one documented request,
so the CLI and the API cannot drift apart.

Environment: ``CAIPS_URL`` (default http://localhost:6969). The token comes from ``caips login``
(stored in ~/.config/caips, mode 0600) or, overriding it, ``CAIPS_API_KEY``.
Output is JSON on stdout (pipe to ``jq``); errors go to stderr; exit code 1 on any
HTTP error, 2 on bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Optional
from urllib.parse import quote

import httpx


class CliError(Exception):
    pass


def _jsonl(path: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError as e:
                    raise CliError(f"{path}:{n}: invalid JSON ({e})")
    return rows


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="caips", description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default=os.getenv("CAIPS_URL", "http://localhost:6969"))
    ap.add_argument("--timeout", type=float, default=60)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "login", help="save the API token for --url (hidden prompt or stdin)"
    )
    sub.add_parser("logout", help="forget the saved token for --url")
    ky = sub.add_parser("keys", help="provider API keys (admin)").add_subparsers(
        dest="sub", required=True
    )
    ky.add_parser("list", help="stored and environment keys (masked)")
    ky.add_parser("catalog", help="provider ids you can add keys for")
    p = ky.add_parser(
        "add", help="add a key; the key is read from a hidden prompt or stdin"
    )
    p.add_argument("provider")
    p.add_argument("--label", default="")
    p.add_argument("--base-url", default="")
    p.add_argument("--model", default="")
    p = ky.add_parser("rm", help="remove a stored key by id")
    p.add_argument("key_id")
    sub.add_parser("health", help="liveness and readiness")
    sub.add_parser("providers", help="configured providers and their health")
    sub.add_parser("usage", help="usage summary")
    sub.add_parser("policies", help="active routing policies (admin)")
    p = sub.add_parser(
        "policy-set",
        help="set one routing policy at runtime (admin; in memory until restart)",
    )
    p.add_argument("name")
    p.add_argument(
        "--strategy",
        default=None,
        help="cost_aware|balanced|cheapest|fastest|latency_first|round_robin|fallback|explicit",
    )
    p.add_argument("--weights", default=None, help="cost,latency weights, e.g. 0.7,0.3")
    p.add_argument("--ceiling", default=None, help="latency_ceiling_ms")
    p.add_argument("--hedge", default=None, help="hedge_after_ms")
    p.add_argument("--chain", default=None, help="fallback chain, comma separated")
    p.add_argument("--provider", default=None, help="pinned provider (explicit)")
    p.add_argument("--model", default=None)
    p.add_argument("--fallback-model", default=None, dest="fallback_model")
    p.add_argument("--allow", default=None, help="provider allow list, comma separated")
    p.add_argument("--deny", default=None, help="provider deny list, comma separated")
    p.add_argument(
        "--file",
        default=None,
        help="read the policy body from a YAML/JSON file; flags above override it",
    )
    p = sub.add_parser("policy-rm", help="remove one routing policy at runtime (admin)")
    p.add_argument("name")
    p = sub.add_parser("budgets", help="budget limits and usage (admin)")
    p.add_argument("--subject", default=None, help="key subject (default: yourself)")
    p = sub.add_parser("budget-set", help="set a subject's budget limits (admin)")
    p.add_argument("subject")
    p.add_argument("--max-cost", type=float, default=None, dest="max_cost_usd")
    p.add_argument("--warn-cost", type=float, default=None, dest="warn_cost_usd")
    p.add_argument("--max-requests", type=int, default=None, dest="max_requests")
    p.add_argument("--max-tokens", type=int, default=None, dest="max_tokens")
    p.add_argument(
        "--window", type=int, default=86400, dest="window_seconds", help="seconds"
    )
    p = sub.add_parser("budget-rm", help="remove a subject's budget override (admin)")
    p.add_argument("subject")
    p = sub.add_parser(
        "explain", help="dry run: where would this request route, and why"
    )
    p.add_argument("prompt")
    p.add_argument("--model", default=None)
    p.add_argument("--policy", default=None)
    p.add_argument("--provider", default=None)
    p = sub.add_parser("audit", help="recent audit events (admin)")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--action", default=None)
    sub.add_parser("models", help="models the gateway can route to")

    lm = sub.add_parser("local", help="managed local GGUF models").add_subparsers(
        dest="sub", required=True
    )
    lm.add_parser("list")
    p = lm.add_parser("inspect")
    p.add_argument("model")
    p = lm.add_parser("pull")
    p.add_argument("model", help="org/repo:QUANT")
    p = lm.add_parser("rm")
    p.add_argument("model")
    p = lm.add_parser("unload")
    p.add_argument("model")

    rn = sub.add_parser("runs", help="agent runs").add_subparsers(
        dest="sub", required=True
    )
    rn.add_parser("list")
    p = rn.add_parser("start")
    p.add_argument("agent")
    p.add_argument("task")
    for name in ("status", "events", "cancel", "pause", "resume", "watch"):
        p = rn.add_parser(name)
        p.add_argument("run_id")
    p = rn.add_parser("kill", help="stop all active runs (admin)")
    p.add_argument(
        "--owner", default=None, help="only this owner; default is everything"
    )

    wf = sub.add_parser("workflows", help="fixed-step workflows").add_subparsers(
        dest="sub", required=True
    )
    wf.add_parser("list")
    p = wf.add_parser("run")
    p.add_argument("name")
    p.add_argument("input", nargs="?", default="")
    p.add_argument("--approve", action="append", default=[], help="step id (admin)")
    p = wf.add_parser("runs", help="past runs of one workflow, newest first")
    p.add_argument("name")
    p.add_argument("--limit", type=int, default=50)
    p = wf.add_parser("run-detail", help="one stored workflow run")
    p.add_argument("name")
    p.add_argument("run_id")

    ap_ = sub.add_parser("approvals", help="tool-call approvals").add_subparsers(
        dest="sub", required=True
    )
    ap_.add_parser("list")
    for name in ("approve", "deny"):
        p = ap_.add_parser(name)
        p.add_argument("approval_id")

    ft = sub.add_parser("finetune", help="fine-tuning jobs").add_subparsers(
        dest="sub", required=True
    )
    for name in ("estimate", "submit"):
        p = ft.add_parser(name)
        p.add_argument("dataset", help='JSONL file, one {"messages": [...]} per line')
        p.add_argument("--base-model", required=True)
        p.add_argument("--provider", default="openai")
        p.add_argument("--preset", default="balanced")
        if name == "submit":
            p.add_argument(
                "--max-cost",
                type=float,
                default=None,
                help="refuse if the estimate is higher (USD)",
            )
    ft.add_parser("list")
    p = ft.add_parser("status")
    p.add_argument("job_id")
    p = ft.add_parser("cancel")
    p.add_argument("job_id")

    p = sub.add_parser("chat", help="stream a chat completion to stdout")
    p.add_argument("model")
    p.add_argument("prompt")

    p = sub.add_parser(
        "tui",
        help="interactive terminal UI (needs: pip install 'coreai-protocol-suite[tui]')",
    )
    p.add_argument(
        "--section",
        default=None,
        help="section to open first (see `caips-tui --help`)",
    )

    p = sub.add_parser(
        "web", help="open the web UI (signs in with SECRET_KEY, a saved token or asks)"
    )
    p.add_argument("--no-open", action="store_true", help="print the URL only")

    p = sub.add_parser("eval", help="run an eval suite (see docs/evals.md)")
    p.add_argument("tasks")
    p.add_argument("--model", default=None)
    p.add_argument("--baseline", default=None)
    p.add_argument(
        "--grader-model", default=None, help="model that grades rubric tasks"
    )
    p.add_argument("--out", default="eval-results.json")
    return ap


def _plan(a: argparse.Namespace) -> tuple[str, str, Optional[dict]]:
    """(method, path, json body) for a parsed command."""
    c, s = a.cmd, getattr(a, "sub", None)
    q = lambda v: quote(str(v), safe="")  # noqa: E731  ids go into URL paths
    simple = {
        "health": "/readyz",
        "providers": "/v1/providers",
        "usage": "/v1/usage",
        "models": "/v1/models",
    }
    if c in simple:
        return "GET", simple[c], None
    if c == "policies":
        return "GET", "/v1/admin/policies", None
    if c == "policy-set":
        body: dict[str, Any] = {}
        if a.file:
            import yaml

            try:
                with open(a.file) as fh:
                    data = yaml.safe_load(fh)
            except (OSError, yaml.YAMLError) as e:
                raise CliError(f"cannot read {a.file}: {e}")
            if not isinstance(data, dict):
                raise CliError(f"{a.file}: must be a YAML/JSON mapping")
            body = data
        if a.strategy:
            body["strategy"] = a.strategy
        if a.weights:
            parts = [w for w in a.weights.replace(" ", "").split(",") if w]
            if len(parts) != 2:
                raise CliError("--weights needs two numbers, e.g. 0.7,0.3")
            try:
                body["weights"] = {"cost": float(parts[0]), "latency": float(parts[1])}
            except ValueError:
                raise CliError("--weights needs two numbers, e.g. 0.7,0.3")
        for flag, key in (
            ("ceiling", "latency_ceiling_ms"),
            ("hedge", "hedge_after_ms"),
        ):
            raw = getattr(a, flag)
            if raw:
                try:
                    body[key] = float(raw)
                except ValueError:
                    raise CliError(f"--{flag} needs a number")
        for flag in ("chain", "allow", "deny"):
            raw = getattr(a, flag)
            if raw:
                body[flag if flag != "chain" else "fallback_chain"] = [
                    v.strip() for v in raw.split(",") if v.strip()
                ]
        for key in ("provider", "model", "fallback_model"):
            if getattr(a, key):
                body[key] = getattr(a, key)
        return "PUT", f"/v1/admin/policies/{q(a.name)}", body
    if c == "policy-rm":
        return "DELETE", f"/v1/admin/policies/{q(a.name)}", None
    if c == "budgets":
        return (
            "GET",
            "/v1/admin/budgets" + (f"?subject={q(a.subject)}" if a.subject else ""),
            None,
        )
    if c == "budget-set":
        body = {
            k: getattr(a, k)
            for k in (
                "max_cost_usd",
                "warn_cost_usd",
                "max_requests",
                "max_tokens",
                "window_seconds",
            )
        }
        return "PUT", f"/v1/admin/budgets/{q(a.subject)}", body
    if c == "budget-rm":
        return "DELETE", f"/v1/admin/budgets/{q(a.subject)}", None
    if c == "explain":
        payload: dict[str, Any] = {"messages": [{"role": "user", "content": a.prompt}]}
        for k in ("model", "policy", "provider"):
            if getattr(a, k):
                payload[k] = getattr(a, k)
        return "POST", "/v1/routing/explain", payload
    if c == "audit":
        extra = f"&action={q(a.action)}" if a.action else ""
        return "GET", f"/v1/admin/audit?limit={int(a.limit)}{extra}", None
    if c == "keys":
        if s == "list":
            return "GET", "/v1/keys", None
        if s == "catalog":
            return "GET", "/v1/keys/catalog", None
        if s == "rm":
            return "DELETE", f"/v1/keys/{q(a.key_id)}", None
        import getpass

        secret = (
            getpass.getpass(f"{a.provider} API key (blank for none): ")
            if sys.stdin.isatty()
            else sys.stdin.readline()
        ).strip()
        return (
            "POST",
            "/v1/keys",
            {
                "provider": a.provider,
                "api_key": secret,
                "label": a.label,
                "base_url": a.base_url,
                "model": a.model,
            },
        )
    if c == "local":
        return {
            "list": ("GET", "/v1/local/models", None),
            "inspect": ("GET", f"/v1/local/models/{q(getattr(a, 'model', ''))}", None),
            "pull": (
                "POST",
                "/v1/local/models/pull",
                {"model": getattr(a, "model", "")},
            ),
            "rm": ("DELETE", f"/v1/local/models/{q(getattr(a, 'model', ''))}", None),
            "unload": (
                "POST",
                f"/v1/local/models/{q(getattr(a, 'model', ''))}/unload",
                None,
            ),
        }[s or ""]
    if c == "runs":
        rid = q(getattr(a, "run_id", ""))
        if s == "list":
            return "GET", "/v1/runs", None
        if s == "start":
            return "POST", "/v1/runs", {"agent": a.agent, "task": a.task}
        if s == "kill":
            return "POST", "/v1/runs/kill", {"owner": a.owner}
        return (
            ("GET" if s in ("status", "events") else "POST"),
            f"/v1/runs/{rid}" + ("" if s == "status" else f"/{s}"),
            None,
        )
    if c == "workflows":
        if s == "list":
            return "GET", "/v1/workflows", None
        if s == "runs":
            return "GET", f"/v1/workflows/{q(a.name)}/runs?limit={int(a.limit)}", None
        if s == "run-detail":
            return "GET", f"/v1/workflows/{q(a.name)}/runs/{q(a.run_id)}", None
        return (
            "POST",
            f"/v1/workflows/{q(a.name)}/run",
            {"input": a.input, "approved": a.approve},
        )
    if c == "approvals":
        if s == "list":
            return "GET", "/v1/tools/approvals", None
        return (
            "POST",
            f"/v1/tools/approvals/{q(a.approval_id)}",
            {"approve": s == "approve"},
        )
    if c == "finetune":
        if s == "list":
            return "GET", "/v1/finetune/jobs", None
        if s in ("status", "cancel"):
            return (
                ("GET" if s == "status" else "DELETE"),
                f"/v1/finetune/jobs/{q(a.job_id)}",
                None,
            )
        ft_body: dict[str, Any] = {
            "base_model": a.base_model,
            "provider": a.provider,
            "hyperparam_preset": a.preset,
            "examples": _jsonl(a.dataset),
        }
        if s == "submit" and a.max_cost is not None:
            ft_body["max_cost_usd"] = a.max_cost
        return (
            "POST",
            "/v1/finetune/estimate" if s == "estimate" else "/v1/finetune/jobs",
            ft_body,
        )
    raise CliError(f"unhandled command {c}")


def _sse(lines):
    """Yield the decoded JSON payload of each ``data:`` line; stop at ``[DONE]``."""
    for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except ValueError:
            continue


def _stream(a, key, transport, out, err) -> int:
    """`runs watch` prints one JSON event per line; `chat` prints text deltas."""
    if a.cmd == "chat":
        method, path = "POST", "/v1/chat/completions"
        body: Optional[dict] = {
            "model": a.model,
            "stream": True,
            "messages": [{"role": "user", "content": a.prompt}],
        }
    else:
        method, path, body = "GET", f"/v1/runs/{quote(a.run_id, safe='')}/stream", None
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with httpx.Client(
            base_url=a.url, headers=headers, timeout=a.timeout, transport=transport
        ) as c:
            with c.stream(method, path, json=body) as r:
                if r.status_code >= 400:
                    r.read()
                    print(f"caips: HTTP {r.status_code}: {r.text}", file=err)
                    return 1
                for ev in _sse(r.iter_lines()):
                    if a.cmd == "chat":
                        if ev.get("error"):
                            print(f"caips: stream error: {ev['error']}", file=err)
                            return 1
                        for ch in ev.get("choices") or []:
                            out.write((ch.get("delta") or {}).get("content") or "")
                        out.flush()
                    else:
                        print(json.dumps(ev), file=out)
                        out.flush()
    except httpx.HTTPError as e:
        print(f"caips: cannot reach {a.url}: {e}", file=err)
        return 1
    if a.cmd == "chat":
        out.write("\n")
    return 0


def main(
    argv: Optional[list[str]] = None, transport: Any = None, out=None, err=None
) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    try:
        a = build_parser().parse_args(argv)
    except SystemExit as e:
        return int(e.code or 0)
    if a.cmd in ("login", "logout"):
        from caips_cli import credentials

        if a.cmd == "logout":
            print(
                "removed" if credentials.delete(a.url) else "no saved token", file=err
            )
            return 0
        import getpass

        tok = (
            getpass.getpass("API token: ")
            if sys.stdin.isatty()
            else sys.stdin.readline()
        ).strip()
        if not tok:
            print("caips: empty token", file=err)
            return 2
        print(f"saved to {credentials.save(a.url, tok)}", file=err)
        return 0
    from caips_cli import credentials

    key = credentials.resolve(a.url)
    if a.cmd == "web":
        import webbrowser
        from datetime import timedelta

        tok = key
        if not tok and os.getenv("SECRET_KEY"):
            from api.auth import create_access_token

            tok = create_access_token(
                "user:web",
                ["admin", "completions:read", "completions:write", "tools:read",
                 "runs:read", "runs:write"],
                timedelta(hours=12),
            )
        # The token goes in the URL fragment: browsers never send it to a server.
        url = a.url.rstrip("/") + "/app" + (f"#token={tok}" if tok else "")
        print(url if a.no_open else a.url.rstrip("/") + "/app", file=out)
        if not a.no_open:
            webbrowser.open(url)
        return 0
    if a.cmd == "tui":
        try:
            from caips_tui.app import main as tui_main
        except ImportError as e:  # textual is an optional extra
            print(
                f"caips: the TUI needs Textual ({e.name or e}); install with: pip install 'coreai-protocol-suite[tui]'",
                file=err,
            )
            return 2
        return tui_main(
            ["--url", a.url, "--timeout", str(a.timeout)]
            + (["--section", a.section] if a.section else [])
        )
    if a.cmd == "eval":
        import asyncio

        from evals.__main__ import main as eval_main

        args = [a.tasks, "--base-url", a.url, "--out", a.out] + (
            ["--model", a.model] if a.model else []
        )
        args += ["--baseline", a.baseline] if a.baseline else []
        args += ["--grader-model", a.grader_model] if a.grader_model else []
        return asyncio.run(eval_main(args, transport=transport))
    if a.cmd == "chat" or (a.cmd == "runs" and a.sub == "watch"):
        return _stream(a, key, transport, out, err)
    try:
        method, path, body = _plan(a)
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        with httpx.Client(
            base_url=a.url, headers=headers, timeout=a.timeout, transport=transport
        ) as c:
            r = c.request(method, path, json=body)
    except CliError as e:
        print(f"caips: {e}", file=err)
        return 2
    except httpx.HTTPError as e:
        print(f"caips: cannot reach {a.url}: {e}", file=err)
        return 1
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail") or r.json()
        except ValueError:
            detail = r.text
        print(f"caips: HTTP {r.status_code}: {detail}", file=err)
        return 1
    if r.content:
        try:
            try:
                text = json.dumps(r.json(), indent=2)
            except ValueError:
                text = r.text
            print(text, file=out)
            out.flush()
        except BrokenPipeError:  # e.g. `caips providers | head`: not an error
            try:
                sys.stdout = open(os.devnull, "w")  # keep interpreter shutdown quiet
            except OSError:
                pass
    return 0
