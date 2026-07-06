"""
tests/conftest.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.resolve()

# Order matters: later insert(0) wins, so list least-priority first.
SEARCH_PATHS = [
    ROOT / "utils",
    ROOT / "database",
    ROOT / "runtime",
    ROOT / "neural",
    ROOT / "agents",
    ROOT / "middleware",  # lower priority than api/
    ROOT / "protocols",
    ROOT / "kernel",
    ROOT / "api",  # api/auth.py beats middleware/auth.py
    ROOT / "coreai",
    ROOT,
]

for path in SEARCH_PATHS:
    path_str = str(path)
    if path_str not in sys.path:
        sys.path.insert(0, path_str)

# Force-evict any stale module cache entries that pytest or plugin imports may
# have populated before our sys.path was in place. Without this, bare-name
# imports like "from auth import X" keep resolving to the first cached version
# regardless of what sys.path now says.
_EVICT = [
    "auth",
    "middleware",
    "cache",
    "router",
    "kernel",
    "runtime",
    "scheduler",
    "limiter",
    "retry",
    "bootloader",
    "orchestrator",
    "memory_manager",
]
for _mod in _EVICT:
    sys.modules.pop(_mod, None)

# ---------------------------------------------------------------------------
# Forced module resolution for "auth"
# ---------------------------------------------------------------------------
# Both api/auth.py and middleware/auth.py exist and are both reachable as a
# bare "auth" module once their directories are on sys.path. Search-order
# insertion alone has proven unreliable at pinning this down. Instead of
# trusting sys.path priority, install a MetaPathFinder that deterministically
# maps the bare name "auth" straight to api/auth.py, bypassing the ambiguity
# entirely. This stays lazy -- it only executes api/auth.py the first time
# something actually imports "auth", which is after each test's
# monkeypatch.setenv("SECRET_KEY", ...) has already run.
import importlib.abc
import importlib.util


class _ForcedAuthFinder(importlib.abc.MetaPathFinder):
    _TARGET = ROOT / "api" / "auth.py"

    def find_spec(self, name, path, target=None):
        if name != "auth" or path is not None:
            return None
        return importlib.util.spec_from_file_location(name, self._TARGET)


if not any(isinstance(f, _ForcedAuthFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _ForcedAuthFinder())

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.name = "mock"
    p.default_model = "mock-model"
    p.estimate_cost = MagicMock(return_value=0.0001)
    p.complete = AsyncMock(
        return_value=MagicMock(
            content="mock response",
            model="mock-model",
            provider="mock",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.0001,
            latency_ms=100.0,
            cached=False,
        )
    )
    return p


@pytest.fixture
def mock_router(mock_provider):
    r = MagicMock()
    r.providers = {"mock": mock_provider}
    r.route = AsyncMock(return_value=mock_provider.complete.return_value)
    r.stats = MagicMock(
        return_value={
            "total_requests": 0,
            "strategy": "balanced",
            "provider_stats": {},
        }
    )
    return r


@pytest.fixture
def mock_orchestrator():
    o = MagicMock()
    o.get_pending_tasks = MagicMock(return_value=[])
    o.get_active_tasks = MagicMock(return_value=[])
    o.stats = MagicMock(return_value={})
    return o


@pytest.fixture
def mock_memory():
    m = MagicMock()
    m.start = AsyncMock()
    m.stop = AsyncMock()
    m.stats = MagicMock(return_value={})
    return m


@pytest.fixture
def mock_scheduler():
    s = MagicMock()
    s.start = AsyncMock()
    s.stop = AsyncMock()
    s.stats = MagicMock(return_value={})
    return s
