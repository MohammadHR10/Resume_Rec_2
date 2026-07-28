"""Azure OpenAI via the UL AI proxy.

The proxy is Bearer-authed and speaks three dialects off one root:

    POST {root}/v1/chat/completions   OpenAI-compatible (what we use)
    POST {root}/v1/messages           Anthropic-compatible (the claude CLI harness)
    GET  {root}/v1/models             deployment listing
    GET  {root}/health                health

Only the OpenAI surface is used for evaluation calls; the Anthropic surface
matters in Phase 5, where the claude CLI is pointed at it through
``ANTHROPIC_BASE_URL``/``ANTHROPIC_AUTH_TOKEN``.
"""

from __future__ import annotations

import os

from .base import OpenAICompatibleProvider

NAME = "ulproxy"
LABEL = "Azure OpenAI (UL AI proxy)"


def base_url() -> str:
    return os.getenv("ULMAIPROXY_BASE_URL", "").strip()


def auth_token() -> str:
    return os.getenv("ULMAIPROXY_AUTH_TOKEN", "").strip()


class ULProxyProvider(OpenAICompatibleProvider):
    def __init__(self, default_model: str = "") -> None:
        super().__init__(
            name=NAME,
            label=LABEL,
            base_url=base_url(),
            api_key=auth_token(),
            default_model=default_model or os.getenv("ULMAIPROXY_MODEL", ""),
        )

    def harness_env(self) -> dict[str, str]:
        """Environment that points a CLI harness at this proxy.

        The claude CLI wants the Anthropic-compatible root; the codex CLI wants
        the OpenAI one. Both are served off the same root by the same token, so
        a harness adapter can take this whole dict regardless of which CLI it
        drives (see ``backend/chat/adapters``).
        """
        if not self.is_configured():
            return {}
        return {
            "ANTHROPIC_BASE_URL": self.root,
            "ANTHROPIC_AUTH_TOKEN": self.api_key,
            "ANTHROPIC_API_KEY": self.api_key,
            "OPENAI_BASE_URL": f"{self.root}/v1",
            "OPENAI_API_KEY": self.api_key,
        }
