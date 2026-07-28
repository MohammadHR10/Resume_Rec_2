"""Resolve the active provider/model from stored config.

Providers are built fresh on each resolve rather than cached: the credentials
come from the environment and the selection from SQLite, and a config change
has to take effect on the next call without a restart.
"""

from __future__ import annotations

from typing import Any

from .. import db
from .base import LLMProvider
from .fastllm import LABEL as FASTLLM_LABEL, NAME as FASTLLM_NAME, FastLLMProvider
from .ulproxy import LABEL as ULPROXY_LABEL, NAME as ULPROXY_NAME, ULProxyProvider

PROVIDERS: dict[str, type[LLMProvider]] = {
    ULPROXY_NAME: ULProxyProvider,
    FASTLLM_NAME: FastLLMProvider,
}

PROVIDER_LABELS = {ULPROXY_NAME: ULPROXY_LABEL, FASTLLM_NAME: FASTLLM_LABEL}

#: Chat mode assumed for a model when config says nothing. The claude/codex CLI
#: harnesses are only expected to work against the UL proxy (open item #2), so
#: fastLLM models default to the in-code structured loop.
DEFAULT_CHAT_MODE = {ULPROXY_NAME: "harness", FASTLLM_NAME: "structured"}


def get_provider(name: str, model: str = "") -> LLMProvider:
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"unknown provider: {name}")
    return cls(default_model=model)  # type: ignore[call-arg]


def active() -> tuple[LLMProvider, str, str]:
    """The configured provider, its name and the selected model id."""
    cfg = db.get_config()
    name = cfg.get("provider") or ULPROXY_NAME
    model = cfg.get("model") or ""
    return get_provider(name, model), name, model


def chat_mode(provider_name: str, model: str) -> str:
    """``harness`` or ``structured`` for this model, per config then default."""
    cfg = db.get_config()
    modes: dict[str, str] = cfg.get("chat_modes") or {}
    return modes.get(f"{provider_name}:{model}") or DEFAULT_CHAT_MODE.get(
        provider_name, "structured"
    )


def describe_all() -> list[dict[str, Any]]:
    """Provider cards for the config page — never raises, never blocks on I/O."""
    out: list[dict[str, Any]] = []
    for name, cls in PROVIDERS.items():
        provider = cls()  # type: ignore[call-arg]
        out.append(
            {
                "name": name,
                "label": PROVIDER_LABELS[name],
                "configured": provider.is_configured(),
                "baseUrl": provider.base_url,
            }
        )
    return out
