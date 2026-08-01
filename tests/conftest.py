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
    ROOT / "api",  # api/auth.py is the only "auth" module now
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
# Shared fixtures
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock, MagicMock

import pytest


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
