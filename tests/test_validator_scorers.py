import asyncio
import importlib.util
from pathlib import Path

import pytest

from coreai import _version
from evals.scorers import score

# api/middleware.py shadows the middleware package on the test path, so load by file.
_spec = importlib.util.spec_from_file_location(
    "_validator", Path(__file__).parent.parent / "src" / "middleware" / "validator.py"
)
_v = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_v)
MAX_MAX_TOKENS, MAX_MESSAGES = _v.MAX_MAX_TOKENS, _v.MAX_MESSAGES
ValidationError = _v.ValidationError
validate_max_tokens = _v.validate_max_tokens
validate_messages = _v.validate_messages
validate_temperature = _v.validate_temperature


def test_validate_messages_ok_and_rejects():
    msgs = [{"role": "user", "content": "hi"}]
    assert validate_messages(msgs) == msgs
    for bad in (
        [],
        [{"role": "tool", "content": "x"}],
        [{"role": "user", "content": "  "}],
        ["notadict"],
        msgs * (MAX_MESSAGES + 1),
    ):
        with pytest.raises(ValidationError):
            validate_messages(bad)


def test_validate_max_tokens_and_temperature():
    assert validate_max_tokens(10) == 10
    for bad in (0, -1, MAX_MAX_TOKENS + 1):
        with pytest.raises(ValidationError):
            validate_max_tokens(bad)
    with pytest.raises(ValidationError):
        validate_temperature("hot")


def run(spec, out, grader=None):
    return asyncio.run(score(spec, out, grader))


def test_scorers():
    assert run({"expected": " Foo  Bar"}, "foo bar") == 1.0
    assert run({"type": "contains", "expected": ["a", "z"]}, "abc") == 0.5
    assert run({"type": "regex", "pattern": r"\d+"}, "x 42") == 1.0
    assert run({"type": "json", "expected": {"a": 1}}, '{"a": 1}') == 1.0
    assert run({"type": "json", "expected": {"a": 1}}, "not json") == 0.0
    with pytest.raises(ValueError):
        run({"type": "rubric", "rubric": "r"}, "x")
    with pytest.raises(ValueError):
        run({"type": "nope"}, "x")

    async def grader(_):
        return "8"

    assert run({"type": "rubric", "rubric": "r"}, "x", grader) == 0.8


def test_version_consistent():
    assert ".".join(map(str, _version.__version_info__)) == _version.__version__
