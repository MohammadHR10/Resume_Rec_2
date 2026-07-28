"""SIS fastLLM gateway.

SIS is a UT System department hosting private AI infrastructure (Mistral and
other open-weight models). Connection details had not arrived when this was
written, so **every assumption about the gateway lives in this file**: it is
treated as OpenAI-compatible with Bearer auth, exactly like the UL proxy but on
its own env vars. If the real gateway differs, this is the one module that
changes.

Two assumptions worth calling out because they are the likely wrong ones:

- ``FASTLLM_BASE_URL`` roots at the host, with ``/v1`` appended by the client.
  A URL that already ends in ``/v1`` is normalized, so either spelling works.
- Structured outputs (``response_format: json_schema``) may not be supported by
  small open-weight models. ``FASTLLM_JSON_SCHEMA=0`` disables it up front; the
  base client also demotes itself automatically on the first rejection.
"""

from __future__ import annotations

import os

from .base import OpenAICompatibleProvider

NAME = "fastllm"
LABEL = "SIS fastLLM gateway"


def base_url() -> str:
    return os.getenv("FASTLLM_BASE_URL", "").strip()


def api_key() -> str:
    return os.getenv("FASTLLM_API_KEY", "").strip()


class FastLLMProvider(OpenAICompatibleProvider):
    def __init__(self, default_model: str = "") -> None:
        super().__init__(
            name=NAME,
            label=LABEL,
            base_url=base_url(),
            api_key=api_key(),
            default_model=default_model or os.getenv("FASTLLM_MODEL", ""),
            supports_json_schema=os.getenv("FASTLLM_JSON_SCHEMA", "1") != "0",
        )

    def is_configured(self) -> bool:
        # Some private gateways sit behind network ACLs rather than a token, so
        # a URL alone is enough to try. The UL proxy always needs both.
        return bool(self.base_url)

    def harness_env(self) -> dict[str, str]:
        """Environment that points the codex CLI at this gateway.

        Only the OpenAI surface: no Anthropic-compatible endpoint is assumed,
        so the claude CLI harness cannot be routed here (open item #1 in the
        implementation plan). Models served here default to structured chat.
        """
        if not self.is_configured():
            return {}
        return {
            "OPENAI_BASE_URL": f"{self.root}/v1",
            "OPENAI_API_KEY": self.api_key or "not-required",
        }
