"""
providers/__init__.py
"""

import os
from dataclasses import dataclass, field

from loguru import logger

# Explicit submodule imports so patch("providers.openai.OpenAIProvider") works
from . import anthropic, deepseek, gemini, grok, openai
from .anthropic import AnthropicProvider
from .catalog import BY_ID, CATALOG, EXTRA_ENV
from .compat import CompatProvider
from .pool import KeyPoolProvider
from .base import (
    BaseProvider,
    CompletionRequest,
    CompletionResponse,
    Message,
    StreamUsage,
)
from .deepseek import DeepSeekProvider
from .gemini import GeminiProvider
from .grok import GrokProvider
from .openai import OpenAIProvider

__all__ = [
    "BaseProvider",
    "CompletionRequest",
    "StreamUsage",
    "CompletionResponse",
    "Message",
    "OpenAIProvider",
    "AnthropicProvider",
    "GeminiProvider",
    "GrokProvider",
    "DeepSeekProvider",
    "ProviderLoadResult",
    "PROVIDER_MAP",
    "load_providers",
    "load_providers_or_raise",
    "fingerprint",
    "CATALOG",
]

PROVIDER_MAP: dict[str, tuple] = {
    "openai": (OpenAIProvider, "OPENAI_API_KEY"),
    "anthropic": (AnthropicProvider, "ANTHROPIC_API_KEY"),
    "gemini": (GeminiProvider, "GEMINI_API_KEY"),
    "grok": (GrokProvider, "GROK_API_KEY"),
    "deepseek": (DeepSeekProvider, "DEEPSEEK_API_KEY"),
}


@dataclass
class ProviderLoadResult:
    providers: dict[str, BaseProvider] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    # Dict-like interface so existing call sites work without modification.
    # Tests assert: result == {}, "openai" in result, result["openai"]
    def __contains__(self, item):
        return item in self.providers

    def __iter__(self):
        return iter(self.providers)

    def __getitem__(self, key):
        return self.providers[key]

    def __len__(self):
        return len(self.providers)

    def __bool__(self):
        return bool(self.providers)

    def __eq__(self, other):
        if isinstance(other, dict):
            return self.providers == other
        if isinstance(other, ProviderLoadResult):
            return (
                self.providers == other.providers
                and self.skipped == other.skipped
                and self.failed == other.failed
            )
        return NotImplemented

    def keys(self):
        return self.providers.keys()

    def values(self):
        return self.providers.values()

    def items(self):
        return self.providers.items()

    def get(self, key, default=None):
        return self.providers.get(key, default)


def fingerprint(p: BaseProvider) -> tuple:
    """Identity of a provider's credentials and endpoint, to tell whether a reload changed it."""
    return tuple(
        (m.api_key, getattr(m, "base_url", ""), m.default_model)
        for m in getattr(p, "members", [p])
    )


def _build(lab_id: str, key: str, name: str, entry: dict | None = None) -> BaseProvider:
    lab = BY_ID[lab_id]
    entry = entry or {}
    if lab.kind == "native":
        return PROVIDER_MAP[lab_id][0](api_key=key)
    return CompatProvider(
        key,
        name,
        entry.get("base_url") or lab.base_url,
        entry.get("model") or lab.model,
    )


def _load_store(result: "ProviderLoadResult") -> None:
    """Keys added at runtime (see coreai/keystore.py), merged with environment keys.

    Several keys for one lab become a KeyPoolProvider under the lab's own name,
    so model pinning, pricing and circuit breaking keep working unchanged.
    """
    try:
        from coreai.keystore import load_secrets

        entries = load_secrets()
    except Exception as e:
        logger.warning(f"  [FAIL] key store — {e}")
        result.failed["keystore"] = str(e)
        return
    by_lab: dict[str, list[dict]] = {}
    for e in entries:
        by_lab.setdefault(e.get("provider", ""), []).append(e)
    # compat labs may also come from their own environment variable
    for lab_id, env_var in EXTRA_ENV.items():
        env_key = os.getenv(env_var, "").strip()
        if env_key:
            by_lab.setdefault(lab_id, []).insert(
                0, {"label": "env", "api_key": env_key, "provider": lab_id}
            )
    for lab_id, items in by_lab.items():
        lab = BY_ID.get(lab_id)
        if lab is None:
            continue
        if lab.kind in ("native", "compat"):
            members = [result.providers[lab_id]] if lab_id in result.providers else []
            labels = ["env"] if members else []
            for e in items:
                if e["label"] == "env" and members:
                    continue  # native env key already loaded above
                try:
                    members.append(_build(lab_id, e["api_key"], lab_id, e))
                    labels.append(e["label"])
                except Exception as exc:
                    result.failed[f"{lab_id}/{e['label']}"] = str(exc)
            if not members:
                continue
            p = members[0] if len(members) == 1 else KeyPoolProvider(lab_id, members, labels)
            p.managed = True
            result.providers[lab_id] = p
            result.skipped.pop(lab_id, None)
            logger.info(f"  [OK]   {lab_id} loaded ({len(members)} key(s))")
        else:  # local / custom: one provider per entry, named by its label
            for e in items:
                name = e["label"]
                if name in result.providers:
                    result.failed[name] = "name clashes with another provider"
                    continue
                try:
                    if lab.kind == "local":
                        from .local import LocalProvider

                        p = LocalProvider(name, e["base_url"], e.get("api_key", ""), default_model=e.get("model", ""))
                    else:
                        p = CompatProvider(e["api_key"], name, e["base_url"], e.get("model", ""))
                except Exception as exc:
                    result.failed[name] = str(exc)
                    continue
                p.managed = True
                result.providers[name] = p
                logger.info(f"  [OK]   {name} ({lab.kind}, {e['base_url']})")


def load_providers(
    enabled: list[str] | None = None,
    *,
    include_local_yml: bool = True,
) -> ProviderLoadResult:
    """
    Load all providers that have API keys set in env.
    Returns ProviderLoadResult with .providers, .skipped, .failed.
    """
    result = ProviderLoadResult()
    from utils.plugins import register_provider_plugins

    register_provider_plugins(PROVIDER_MAP)
    targets = list(enabled) if enabled is not None else list(PROVIDER_MAP.keys())

    for name in targets:
        if name not in PROVIDER_MAP:
            result.skipped[name] = f"unknown provider '{name}'"
            continue

        cls, env_var = PROVIDER_MAP[name]
        api_key = os.getenv(str(env_var), "").strip()

        if not api_key:
            reason = f"{env_var} not set"
            logger.debug(f"  [SKIP] {name} — {reason}")
            result.skipped[name] = reason
            continue

        try:
            result.providers[name] = cls(api_key=api_key)
            logger.info(f"  [OK]   {name} loaded")
        except Exception as e:
            reason = str(e)
            logger.warning(f"  [FAIL] {name} — {reason}")
            result.failed[name] = reason

    if enabled is None:
        for p in result.providers.values():
            p.managed = True
        _load_store(result)
        if include_local_yml:
            _load_local(result)
    return result


def _load_local(result: "ProviderLoadResult") -> None:
    """Add self-hosted servers from config/local.yml (or $COREAI_LOCAL_FILE)."""
    path = os.getenv("COREAI_LOCAL_FILE", "config/local.yml")
    if not os.path.exists(path):
        return
    try:
        import yaml

        from .local import build_local_providers

        with open(path) as fh:
            built = build_local_providers(yaml.safe_load(fh) or {})
    except Exception as e:
        logger.warning(f"  [FAIL] local servers ({path}) — {e}")
        result.failed["local"] = str(e)
        return
    built.update(_managed(path))
    for name, provider in built.items():
        if name in result.providers:
            result.failed[name] = "name clashes with a cloud provider"
            continue
        result.providers[name] = provider
        logger.info(f"  [OK]   {name} (local, {provider.base_url})")


def _managed(path: str) -> dict:
    """Optional ``managed:`` block in local.yml: a models dir run via llama-server."""
    import yaml

    with open(path) as fh:
        cfg = (yaml.safe_load(fh) or {}).get("managed")
    if not cfg:
        return {}
    from localrt.provider import ManagedLocalProvider
    from localrt.registry import ModelRegistry
    from localrt.supervisor import Supervisor

    name = cfg.get("name", "gguf")
    sup = Supervisor(
        max_loaded=cfg.get("max_loaded", 1),
        idle_timeout_s=cfg.get("idle_timeout_s", 600),
    )
    prov = ManagedLocalProvider(
        name,
        ModelRegistry(cfg.get("models_dir", "models"), cfg.get("capabilities")),
        sup,
        binary=cfg.get("binary", ""),
        max_queue=cfg.get("max_queue", 0),
    )
    return {name: prov}


def load_providers_or_raise(
    enabled: list[str] | None = None,
    required: list[str] | None = None,
) -> dict[str, BaseProvider]:
    """
    Like load_providers() but raises RuntimeError if no providers loaded
    or any provider in `required` is missing. Returns plain providers dict.
    """
    result = load_providers(enabled)

    if required:
        missing = [r for r in required if r not in result.providers]
        if missing:
            raise RuntimeError(
                f"Required provider(s) not available: {', '.join(missing)}. "
                "Check API keys: "
                + ", ".join(PROVIDER_MAP[r][1] for r in missing if r in PROVIDER_MAP)
            )

    if not result.providers:
        raise RuntimeError(
            "No AI providers could be loaded. "
            f"Skipped: {result.skipped}. Failed: {result.failed}."
        )

    return result.providers
