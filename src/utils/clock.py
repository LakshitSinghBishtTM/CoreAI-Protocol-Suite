"""Naive-UTC clock helper replacing the deprecated ``datetime.utcnow()``."""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (same value ``utcnow()`` returned)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
