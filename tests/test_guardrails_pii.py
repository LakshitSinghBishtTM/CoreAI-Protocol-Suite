import json

from coreai.logging_setup import format_json
from coreai.logging_setup import redact as log_redact
from guardrails import detect, redact
from guardrails.pii import ALL_KINDS


def kinds(t, **kw):
    return [f.kind for f in detect(t, **kw)]


def test_detects_common_pii():
    assert kinds("mail bob@example.com now") == ["email"]
    assert kinds("ssn 123-45-6789") == ["ssn"]
    assert kinds("call +1 415 555 2671") == ["phone"]


def test_credit_card_needs_valid_luhn():
    assert kinds("card 4111 1111 1111 1111") == ["credit_card"]
    assert kinds("order 1234 5678 9012 3456") == []


def test_secrets():
    assert kinds("key sk-abcdefghijklmnop1234") == ["api_key"]
    assert kinds("AKIAABCDEFGHIJKLMNOP") == ["aws_key"]
    assert kinds("ghp_" + "a" * 30) == ["github_token"]
    assert kinds("Authorization: Bearer abcdefgh12345678") == ["bearer"]
    pk = "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----"
    assert redact(f"x {pk} y") == "x [REDACTED:private_key] y"


def test_redact_and_overlap_priority():
    assert redact("a@b.co and 123-45-6789") == "[REDACTED:email] and [REDACTED:ssn]"
    assert redact("") == "" and redact("nothing here") == "nothing here"


def test_ip_is_opt_in():
    assert kinds("host 10.0.0.1") == []
    assert kinds("host 10.0.0.1", kinds=ALL_KINDS) == ["ipv4"]


def test_logs_redact_pii_by_default_and_can_be_disabled(monkeypatch):
    assert "[REDACTED:email]" in log_redact("user bob@example.com failed")
    monkeypatch.setenv("COREAI_LOG_REDACT_PII", "0")
    assert "bob@example.com" in log_redact("user bob@example.com failed")


def test_json_log_line_is_redacted():
    import datetime as dt
    from types import SimpleNamespace

    rec = {
        "time": dt.datetime.now(dt.timezone.utc),
        "level": SimpleNamespace(name="INFO"),
        "name": "n", "function": "f", "message": "ssn 123-45-6789",
        "extra": {}, "exception": None,
    }  # fmt: skip
    assert "123-45-6789" not in format_json(rec)
    assert json.loads(format_json(rec))["message"] == "ssn [REDACTED:ssn]"
