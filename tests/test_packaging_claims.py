"""Roadmap section 2 claims, enforced: dependency budget, one logging library, CI scans."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAX_RUNTIME_DEPS = 20


def _runtime_deps() -> list[str]:
    lines = (ROOT / "requirements.txt").read_text().splitlines()
    return [x.strip() for x in lines if x.strip() and not x.strip().startswith("#")]


def test_runtime_dependencies_under_budget():
    assert len(_runtime_deps()) < MAX_RUNTIME_DEPS


def test_single_logging_library():
    names = {re.split(r"[\[<>=!~ ]", d, maxsplit=1)[0].lower() for d in _runtime_deps()}
    assert "loguru" in names
    assert not names & {"structlog", "logbook", "eliot", "python-json-logger"}


def test_stdlib_logging_goes_through_the_loguru_pipeline():
    src = (ROOT / "src" / "coreai" / "logging_setup.py").read_text()
    assert "class InterceptHandler" in src and "basicConfig(handlers=[InterceptHandler()]" in src


def test_ci_runs_secret_and_dependency_scans():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "gitleaks" in ci and "pip-audit" in ci


def test_publish_workflow_uses_trusted_publishing_with_provenance():
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text()
    assert "id-token: write" in wf and "environment: pypi" in wf
    assert "pypa/gh-action-pypi-publish" in wf
    assert "password:" not in wf and "PYPI_API_TOKEN" not in wf  # no stored token
    assert "attestations: false" not in wf  # provenance stays on (the action default)
    assert "Tag must match package version" in wf


def test_ci_checks_the_built_distribution_and_all_of_src_with_mypy():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "twine check --strict dist/*" in ci
    assert "\n        run: mypy\n" in ci
    assert 'files = ["src"]' in (ROOT / "pyproject.toml").read_text()
