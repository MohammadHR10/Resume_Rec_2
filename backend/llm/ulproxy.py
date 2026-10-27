"""Azure OpenAI via the UL AI proxy.

The proxy is Bearer-authed and speaks three dialects off one root:

    POST {root}/v1/chat/completions   OpenAI-compatible (what we use)
    POST {root}/v1/messages           Anthropic-compatible (the claude CLI harness)
    GET  {root}/v1/models             deployment listing
    GET  {root}/health                health

Only the OpenAI surface is used for evaluation calls; the Anthropic surface
matters for the claude CLI harness, pointed at it through
``ANTHROPIC_BASE_URL``/``ANTHROPIC_AUTH_TOKEN``.

**Two request-shape constraints, read from the proxy's source rather than
inferred from failures** (`azure-openai-proxy/app/proxy/chat_completions.py`):

1. Each deployment has a ``target_uri``. When it names ``/openai/responses``
   the proxy *translates* the Chat Completions body into an Azure Responses
   API call, mapping a fixed set of parameters —
   ``temperature``, ``max_tokens``→``max_output_tokens``, ``top_p``, ``stop``,
   ``presence_penalty``, ``frequency_penalty``. So an explicit ``temperature``
   is forwarded to Azure, and the gpt-5 family reject any value but their
   default. The proxy reports that only as ``Upstream API error: 400``, with
   the underlying reason discarded. Hence: send no temperature.

2. ``response_format`` appears **nowhere** in the proxy. It is absent from the
   translation map, so for a Responses-API deployment it is silently dropped
   and no schema is enforced. Asking for JSON in the prompt is therefore the
   only thing that actually constrains the reply here, so this provider does
   not claim structured-output support.

Deployments whose ``target_uri`` names ``/chat/completions`` (Kimi, gpt-oss and
other Foundry models-as-a-service) are forwarded unchanged and would accept
both parameters — but a client cannot tell the two kinds apart from the model
listing, so the safe shape is used for all of them.
"""

from __future__ import annotations

import os

from .. import db
from .base import OpenAICompatibleProvider

NAME = "ulproxy"
LABEL = "Azure OpenAI (UL AI proxy)"


def base_url() -> str:
    """Connection details entered in the UI win; the environment is the fallback."""
    return db.get_connection(NAME)["base_url"] or os.getenv("ULMAIPROXY_BASE_URL", "").strip()


def auth_token() -> str:
    return db.get_connection(NAME)["api_key"] or os.getenv("ULMAIPROXY_AUTH_TOKEN", "").strip()


class ULProxyProvider(OpenAICompatibleProvider):
    def __init__(self, default_model: str = "") -> None:
        super().__init__(
            name=NAME,
            label=LABEL,
            base_url=base_url(),
            api_key=auth_token(),
            default_model=default_model or os.getenv("ULMAIPROXY_MODEL", ""),
            # See the module docstring: both of these are what the proxy's own
            # source says a valid request looks like, not guesses at a 400.
            temperature=None,
            supports_json_schema=False,
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
