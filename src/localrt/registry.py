"""Scan a models directory for .gguf files and expose capability flags."""

import json
from pathlib import Path
from typing import Any

from loguru import logger

from .gguf import GGUFError, describe


def _detect(info: dict[str, Any]) -> dict[str, bool]:
    tpl = info.get("chat_template") or ""
    return {
        "tools": "tools" in tpl,
        "vision": False,  # needs an mmproj file; set via overrides
        "json_mode": True,
        "embeddings": "bert" in str(info.get("architecture")),
    }


class ModelRegistry:
    def __init__(self, directory: str | Path, overrides: dict[str, dict] | None = None):
        self.directory = Path(directory)
        self.overrides = overrides or {}
        self._models: dict[str, dict[str, Any]] = {}

    def scan(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for p in sorted(self.directory.rglob("*.gguf")):
            try:
                info = describe(p)
            except (GGUFError, OSError) as e:
                logger.warning(f"skipping {p.name}: {e}")
                continue
            info["path"] = str(p)
            info["capabilities"] = {**_detect(info), **self.overrides.get(p.stem, {})}
            found[p.stem] = info
        self._models = found
        return found

    def list(self) -> list[dict[str, Any]]:
        return [self._public(k, v) for k, v in self._models.items()]

    def get(self, name: str) -> dict[str, Any] | None:
        return self._models.get(name)

    def delete(self, name: str) -> bool:
        m = self._models.pop(name, None)
        if not m:
            return False
        Path(m["path"]).unlink(missing_ok=True)
        return True

    @staticmethod
    def _public(name: str, info: dict[str, Any]) -> dict[str, Any]:
        d = {k: v for k, v in info.items() if k not in ("chat_template", "path")}
        d["id"] = name
        return d

    def dump(self) -> str:
        return json.dumps(self.list(), indent=2)
