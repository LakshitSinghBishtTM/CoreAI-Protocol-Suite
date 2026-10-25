"""Thin Python client for a CaiPS gateway (chat, tools, agent runs, approvals)."""

from . import types
from .client import CaipsClient, CaipsError

__all__ = ["CaipsClient", "CaipsError", "types"]
