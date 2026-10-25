import asyncio

import pytest

from coreai import _version
from evals.scorers import score


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
