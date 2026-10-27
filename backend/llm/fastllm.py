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
from contextlib import contextmanager
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

#: Gap between a retry's seed and the pinned one. Large and fixed, so a rerun of
#: the same screening still reproduces, attempt for attempt.
RETRY_SEED_STRIDE = 1000

#: Substrings that mark a model as GPT-OSS, i.e. harmony-speaking. Everything
#: in ``adjust_system`` and the ``response_format`` demotion below is scoped to
#: these: they are the only models on this gateway served without a harmony
#: parser, and the workaround actively breaks the others. gemma answers a
#: prompt-delivered schema in Markdown and ignores the instruction entirely,
#: which reads downstream as "the model's reply was not valid JSON".
HARMONY_MODEL_HINTS = ("gpt-oss", "gpt 120b", "gpt 20b")


def is_harmony_model(model: str) -> bool:
    name = (model or "").strip().lower()
    return any(hint in name for hint in HARMONY_MODEL_HINTS)


#: Appended to a GPT-OSS system prompt. See ``adjust_system``.
HARMONY_FINAL_CHANNEL = (
    "\n\nIMPORTANT: You have no callable functions available. Never emit a function "
    "call or address any recipient. The tools named above are values inside a JSON "
    "payload that you write out as ordinary text. Put your entire answer in the "
    "final channel as a single JSON object and nothing else."
)


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
        # Set before super().__init__, which assigns supports_json_schema and
        # so reaches the setter below.
        self._active_model = default_model
        self._demoted: set[str] = set()
        self._force_json_schema = os.getenv("FASTLLM_JSON_SCHEMA", "0") == "1"
        super().__init__(
            name=NAME,
            label=LABEL,
            base_url=base_url(),
            api_key=api_key(),
            default_model=default_model or _first_env("LLM_MODEL", "FASTLLM_MODEL"),
            # Replaced by the property below, which decides per model.
            supports_json_schema=False,
        )
        # super().__init__ assigns supports_json_schema, which reaches the
        # setter above and would record the default model as demoted before a
        # single request has been made. Only a real 400 should demote.
        self._demoted.clear()
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

    # -- per-model behaviour ----------------------------------------------
    #
    # The audit page benchmarks models against each other through one provider
    # instance, so "which model is this" cannot be answered at construction
    # time. Both hooks below read the model of the call in flight instead.

    @contextmanager
    def _for_model(self, model: str | None):
        previous = self._active_model
        self._active_model = model or self.default_model or ""
        try:
            yield
        finally:
            self._active_model = previous

    @property
    def supports_json_schema(self) -> bool:
        """True except for GPT-OSS, which needs the schema in the prompt.

        For GPT-OSS this gateway accepts ``response_format`` and then ignores
        it, returning byte-identical replies with and without it and never
        400ing, so the base client's demotion never fires and the schema is
        silently doing nothing. Every other model here honours it — and gemma
        *only* honours it, failing outright when the schema arrives as prose.
        """
        if self._active_model in self._demoted:
            return False
        return self._force_json_schema or not is_harmony_model(self._active_model)

    @supports_json_schema.setter
    def supports_json_schema(self, value: bool) -> None:
        # The base client demotes by assigning False after a 400 naming
        # response_format. Record that per model rather than for the instance,
        # so one model's rejection does not mute the schema for the rest.
        if value:
            self._demoted.discard(self._active_model)
        else:
            self._demoted.add(self._active_model)

    def structured_extract(self, prompt: str, schema: dict[str, Any], *,
                           model: str | None = None, system: str | None = None,
                           schema_name: str = "Response") -> dict[str, Any]:
        with self._for_model(model):
            return super().structured_extract(
                prompt, schema, model=model, system=system, schema_name=schema_name)

    def chat(self, messages: list[dict[str, str]], *, schema: dict[str, Any] | None = None,
             model: str | None = None, system: str | None = None,
             schema_name: str = "Response") -> Any:
        with self._for_model(model):
            return super().chat(
                messages, schema=schema, model=model, system=system, schema_name=schema_name)

    def extra_body(self) -> dict[str, Any]:
        return {"seed": self.seed}

    def adjust_system(self, system_text: str, *, structured: bool) -> str:
        """Talk the harmony models out of emitting a tool call we cannot read.

        The GPT-OSS models here are served by vLLM without a harmony tool-call
        parser. Asked to "use a tool", they do exactly that — end the turn on
        the harmony ``<|call|>`` token (it surfaces as ``stop_reason`` 200012)
        addressed to a recipient the gateway never declared. vLLM drops the
        unparsed call, so ``content`` comes back ``null`` with no
        ``tool_calls`` to read instead; when the parse fails louder the gateway
        500s with ``unexpected tokens remaining in message header``.

        Nothing in the response distinguishes that from a model with nothing to
        say, which is why it read as "empty completion" and burned the whole
        retry budget. Measured against the chat system prompt, this suffix took
        populated ``content`` from 2 replies in 12 to 12 in 12.

        Only the prompt can reach this: the fix belongs in the SIS deployment
        (vLLM's ``--tool-call-parser``), and this goes away when they enable it.
        """
        if not system_text.strip() or not is_harmony_model(self._active_model):
            return system_text
        return system_text + HARMONY_FINAL_CHANNEL

    def vary_on_retry(self, body: dict[str, Any], attempt: int) -> None:
        """Move the pinned seed so a retry is a new draw, not a replay.

        ``DEFAULT_SEED`` makes scoring reproducible, but it also makes failure
        reproducible: the empty-content and header-parse failures above are
        seed-dependent, so retrying the identical body reproduced them exactly,
        four times, before giving up. Deriving each retry's seed from the pinned
        one keeps a *run* reproducible while letting a retry actually differ.
        """
        body["seed"] = self.seed + RETRY_SEED_STRIDE * (attempt + 1)

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
