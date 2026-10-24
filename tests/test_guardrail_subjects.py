import textwrap

from guardrails import config as g


def _cfg(tmp_path):
    p = tmp_path / "g.yml"
    p.write_text(textwrap.dedent("""
        input: {redact_pii: true}
        subjects:
          alice:
            allowed_models: ["gpt-*", "local-?"]
            input: {redact_pii: false, blocked_topics: ["secret plan"]}
    """))
    g.reset()
    return str(p)


def test_subject_override_replaces_section_and_falls_back(tmp_path):
    p = _cfg(tmp_path)
    ai, ao = g.for_subject("alice", p)
    bi, bo = g.for_subject("bob", p)
    assert ai.redact_pii is False and ai.blocked_topics == ("secret plan",)
    assert bi.redact_pii is True and bo is None and ao is None


def test_model_allow_list(tmp_path):
    p = _cfg(tmp_path)
    assert g.model_allowed("alice", "gpt-4o", p)
    assert g.model_allowed("alice", "local-1", p)
    assert not g.model_allowed("alice", "claude-x", p)
    assert g.model_allowed("bob", "claude-x", p)  # no entry: unrestricted
