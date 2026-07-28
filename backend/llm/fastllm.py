"""SIS fastLLM gateway.

SIS is a UT System department hosting private AI infrastructure (Llama,
Nemotron, GPT-OSS and other open-weight models). This client follows the
contract proven by the Streamlit app's own gateway wiring on the ``VDI``
branch (`mistral_client.py` at `4cc18c9`), which is the only place the real
request shape was ever exercised:

    POST $LLM_GATEWAY_URL
    Authorization: Bearer $LLM_GATEWAY_KEY
    hl-project-id:   $HL_PROJECT_ID       (when set)
    hl-requester-id: $HL_REQUESTER_ID     (when set)
    {"model": ..., "messages": [...], "seed": 42}

So it is OpenAI chat-completions shaped, as assumed, but with three details a
generic OpenAI client would get wrong: the tenant-attribution headers, the
``seed``, and a URL that is the *endpoint* rather than a root to append to.

Two things are still worth knowing:

- ``LLM_GATEWAY_URL`` is accepted in either spelling. A URL already ending in
  ``/chat/completions`` is posted to verbatim; anything else is treated as a
  root and gets ``/v1/chat/completions`` appended.
- Model ids on this gateway are human-ish strings like ``GPT 120b`` and
  ``Nemotron 49b``, not slugs, and the old client never listed them — so
  ``/v1/models`` may 404. The config page already falls back to a free-text
  model field when listing returns nothing.
"""

from __future__ import annotations

import os
from typing import Any

from .. import db
from .base import OpenAICompatibleProvider

NAME = "fastllm"
LABEL = "SIS fastLLM gateway"

#: The old client pinned this so two runs of the same resume agreed. It matters
#: more here than it did there: a bias audit compares a baseline against a
#: variant that differs by one sentence, and unpinned sampling would show up as
#: a delta that has nothing to do with the injected attribute.
DEFAULT_SEED = 42


def _first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def base_url() -> str:
    """Gateway URL. ``LLM_GATEWAY_URL`` is the name the existing app used."""
    return db.get_connection(NAME)["base_url"] or _first_env(
        "LLM_GATEWAY_URL", "FASTLLM_BASE_URL"
    )


def api_key() -> str:
    return db.get_connection(NAME)["api_key"] or _first_env(
        "LLM_GATEWAY_KEY", "LLM_GATEWAY_API_KEY", "FASTLLM_API_KEY"
    )


class FastLLMProvider(OpenAICompatibleProvider):
    def __init__(self, default_model: str = "") -> None:
        super().__init__(
            name=NAME,
            label=LABEL,
            base_url=base_url(),
            api_key=api_key(),
            default_model=default_model or _first_env("LLM_MODEL", "FASTLLM_MODEL"),
            supports_json_schema=os.getenv("FASTLLM_JSON_SCHEMA", "1") != "0",
        )
        stored = db.get_connection(NAME)
        self.project_id = stored["project_id"] or _first_env("HL_PROJECT_ID")
        self.requester_id = stored["requester_id"] or _first_env("HL_REQUESTER_ID")
        self.seed = int(_first_env("LLM_SEED") or DEFAULT_SEED)

    def is_configured(self) -> bool:
        # Private gateways often sit behind a network ACL rather than a token,
        # so a URL alone is enough to try. The UL proxy always needs both.
        return bool(self.base_url)

    @property
    def root(self) -> str:
        """Strip the endpoint back to a root so ``/v1/models`` still resolves.

        Without this, a ``LLM_GATEWAY_URL`` naming the full endpoint would send
        model listing to ``…/chat/completions/v1/models``.
        """
        url = self.base_url.strip().rstrip("/")
        if url.endswith("/chat/completions"):
            url = url[: -len("/chat/completions")]
        if url.endswith("/v1"):
            url = url[: -len("/v1")]
        return url

    def completions_url(self) -> str:
        """Post to the configured URL when it already names the endpoint."""
        if self.base_url.strip().rstrip("/").endswith("/chat/completions"):
            return self.base_url.strip().rstrip("/")
        return super().completions_url()

    def extra_headers(self) -> dict[str, str]:
        """Tenant attribution the gateway uses for routing and quota."""
        headers: dict[str, str] = {}
        if self.project_id:
            headers["hl-project-id"] = self.project_id
        if self.requester_id:
            headers["hl-requester-id"] = self.requester_id
        return headers

    def extra_body(self) -> dict[str, Any]:
        return {"seed": self.seed}

    def harness_env(self) -> dict[str, str]:
        """Environment that points the codex CLI at this gateway.

        Only the OpenAI surface: no Anthropic-compatible endpoint is known, so
        the claude CLI harness cannot be routed here, and models served here
        default to structured chat. The ``hl-*`` headers have no equivalent in
        the CLI's environment, so a harness turn against a gateway that
        *requires* them will fail and degrade to structured — which is the
        right outcome, just not an obvious one.
        """
        if not self.is_configured():
            return {}
        return {
            "OPENAI_BASE_URL": f"{self.root}/v1",
            "OPENAI_API_KEY": self.api_key or "not-required",
        }
