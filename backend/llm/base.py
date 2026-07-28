"""Provider interface and the OpenAI-compatible implementation both back ends share.

Two operations are all the app needs:

- ``structured_extract(prompt, schema)`` — one strict-JSON call (JD parsing,
  candidate evaluation). Returns a dict validated against the schema shape by
  the provider where it can, and by the caller's Pydantic model after.
- ``chat(messages, schema=None)`` — a conversational turn, optionally
  strict-JSON (the structured chat loop in ``chat/structured.py``).

Retry/backoff follows the reference pipeline: exponential with jitter on
connection errors, 429s and 5xx; client errors fail fast.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

MAX_RETRIES = 4
RETRY_BASE_SECONDS = 1.0

# Idle connections get dropped by gateways between batches; closing after each
# request trades a little latency for not eating a RemoteDisconnected mid-run.
_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_maxsize=8, pool_connections=4))
_session.mount("http://", HTTPAdapter(pool_maxsize=8, pool_connections=4))
_session.headers.update({"Connection": "close"})


class LLMError(RuntimeError):
    """A call to a provider failed in a way the caller has to surface."""


class ProviderNotConfigured(LLMError):
    """The provider is selected but its URL/credentials are missing."""


def ensure_strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make a JSON Schema acceptable to OpenAI strict structured outputs.

    Strict mode demands that every object declares ``additionalProperties:
    false`` and lists *all* of its properties in ``required``. Pydantic emits
    neither by default, so walk the tree and add them. Mutates in place and
    returns the same object for convenience.
    """
    if not isinstance(schema, dict):
        return schema

    if schema.get("type") == "object" or "properties" in schema:
        props = schema.get("properties")
        if isinstance(props, dict):
            schema["additionalProperties"] = False
            schema["required"] = list(props.keys())
            for sub in props.values():
                ensure_strict_schema(sub)

    for key in ("items", "not"):
        if key in schema:
            ensure_strict_schema(schema[key])
    for key in ("anyOf", "oneOf", "allOf", "prefixItems"):
        for sub in schema.get(key, []) or []:
            ensure_strict_schema(sub)
    for sub in (schema.get("$defs") or {}).values():
        ensure_strict_schema(sub)

    return schema


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_loose(text: str) -> dict[str, Any]:
    """Pull a JSON object out of a model reply that may not be pure JSON.

    Models without strict structured-output support wrap their answer in code
    fences or a sentence of preamble. Try the whole string, then a fenced
    block, then the outermost balanced ``{...}``.
    """
    text = (text or "").strip()
    if not text:
        raise LLMError("model returned an empty response")

    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise LLMError(f"could not parse JSON from model response: {text[:300]}")


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    return candidates


class LLMProvider(ABC):
    """What the rest of the app is allowed to know about a model back end."""

    name: str = ""
    label: str = ""

    @abstractmethod
    def is_configured(self) -> bool:
        """True when this provider has everything it needs to make a call."""

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Best-effort reachability check. Never raises."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Model ids offered by the back end. Empty list on any failure."""

    @abstractmethod
    def structured_extract(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        model: str | None = None,
        system: str | None = None,
        schema_name: str = "Response",
    ) -> dict[str, Any]:
        """One strict-JSON completion. Raises LLMError on failure."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        system: str | None = None,
        schema_name: str = "Response",
    ) -> Any:
        """A chat turn. Returns a dict when ``schema`` is given, else text."""


class OpenAICompatibleProvider(LLMProvider):
    """Chat-completions client for any OpenAI-shaped endpoint.

    Both back ends the requirements name speak this dialect (the UL AI proxy
    natively, the SIS gateway by assumption), so the wire logic lives here once
    and the subclasses only supply configuration.

    ``supports_json_schema`` is a hint, not a promise: the first 400 that
    complains about ``response_format`` demotes the provider to
    prompt-instructed JSON for the rest of the process, because small
    open-weight models behind a gateway routinely advertise the parameter and
    then reject it.
    """

    #: openai-style path segment; the UL proxy and fastLLM both root at /v1
    api_root_suffix = "/v1"

    def __init__(
        self,
        name: str,
        label: str,
        base_url: str,
        api_key: str,
        *,
        default_model: str = "",
        timeout: float = 180.0,
        supports_json_schema: bool = True,
    ) -> None:
        self.name = name
        self.label = label
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.default_model = default_model
        self.timeout = timeout
        self.supports_json_schema = supports_json_schema

    # -- configuration -----------------------------------------------------

    @property
    def root(self) -> str:
        """The bare root, with any ``/v1`` the operator typed stripped off.

        Both spellings show up in the wild (Anthropic-style bare root, Codex
        style with ``/v1``); normalizing means endpoint paths can be built
        absolutely without doubling the segment.
        """
        root = self.base_url
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        return root

    def is_configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _require_configured(self) -> None:
        if not self.is_configured():
            raise ProviderNotConfigured(
                f"{self.label} is not configured — set its base URL and token in the environment"
            )

    def _require_model(self, model: str | None) -> str:
        use = (model or self.default_model or "").strip()
        if not use:
            raise LLMError(f"no model selected for {self.label}")
        return use

    # -- reachability ------------------------------------------------------

    def health(self) -> dict[str, Any]:
        if not self.is_configured():
            return {"ok": False, "detail": "not configured"}
        try:
            resp = _session.get(f"{self.root}/health", headers=self.headers(), timeout=10)
            if resp.status_code == 200:
                return {"ok": True, "detail": "healthy"}
            # Not every gateway exposes /health; a successful model listing is
            # just as good a proof of life.
            models = self.list_models()
            if models:
                return {"ok": True, "detail": f"{len(models)} model(s) reachable"}
            return {"ok": False, "detail": f"health returned HTTP {resp.status_code}"}
        except requests.RequestException as exc:
            models = self.list_models()
            if models:
                return {"ok": True, "detail": f"{len(models)} model(s) reachable"}
            return {"ok": False, "detail": str(exc)}

    def list_models(self) -> list[str]:
        if not self.is_configured():
            return []
        try:
            resp = _session.get(
                f"{self.root}{self.api_root_suffix}/models", headers=self.headers(), timeout=15
            )
            if resp.status_code // 100 != 2:
                logger.warning("%s model listing returned HTTP %s", self.name, resp.status_code)
                return []
            data = resp.json()
            entries = data.get("data", data if isinstance(data, list) else [])
            return sorted(
                m["id"] for m in entries if isinstance(m, dict) and m.get("id")
            )
        except (requests.RequestException, ValueError, AttributeError, KeyError) as exc:
            logger.warning("%s model listing failed: %s", self.name, exc)
            return []

    # -- completions -------------------------------------------------------

    def structured_extract(
        self,
        prompt: str,
        schema: dict[str, Any],
        *,
        model: str | None = None,
        system: str | None = None,
        schema_name: str = "Response",
    ) -> dict[str, Any]:
        messages = [{"role": "user", "content": prompt}]
        return self.chat(
            messages, schema=schema, model=model, system=system, schema_name=schema_name
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        system: str | None = None,
        schema_name: str = "Response",
    ) -> Any:
        self._require_configured()
        use_model = self._require_model(model)

        strict_schema = ensure_strict_schema(json.loads(json.dumps(schema))) if schema else None
        sys_text = system or ""
        if strict_schema and not self.supports_json_schema:
            sys_text = _json_instruction(sys_text, strict_schema)

        body: dict[str, Any] = {
            "model": use_model,
            "messages": (
                [{"role": "system", "content": sys_text}] if sys_text else []
            )
            + list(messages),
            "temperature": 0,
        }
        if strict_schema and self.supports_json_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": strict_schema},
            }

        text = self._post_chat(body, strict_schema, schema_name, sys_text)
        if strict_schema is None:
            return text
        return parse_json_loose(text)

    def _post_chat(
        self,
        body: dict[str, Any],
        strict_schema: dict[str, Any] | None,
        schema_name: str,
        sys_text: str,
    ) -> str:
        url = f"{self.root}{self.api_root_suffix}/chat/completions"
        last_error = "unknown error"

        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = _session.post(url, headers=self.headers(), json=body, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = f"connection error: {exc}"
                if attempt < MAX_RETRIES:
                    self._backoff(attempt, last_error)
                    continue
                raise LLMError(f"{self.label}: {last_error}") from exc

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
                if attempt < MAX_RETRIES:
                    self._backoff(attempt, last_error)
                    continue
                raise LLMError(f"{self.label}: {last_error}")

            if resp.status_code == 400 and strict_schema is not None and self.supports_json_schema:
                # The gateway advertised structured outputs and then refused
                # them. Demote for the life of the process and ask for JSON in
                # the prompt instead — one retry, then treat it as a real error.
                if "response_format" in resp.text or "json_schema" in resp.text:
                    logger.warning(
                        "%s rejected response_format; falling back to prompt-instructed JSON",
                        self.name,
                    )
                    self.supports_json_schema = False
                    body.pop("response_format", None)
                    body["messages"] = _with_system(
                        body["messages"], _json_instruction(sys_text, strict_schema)
                    )
                    continue

            if resp.status_code // 100 != 2:
                raise LLMError(f"{self.label}: HTTP {resp.status_code}: {resp.text[:300]}")

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                last_error = f"malformed response: {exc}"
                if attempt < MAX_RETRIES:
                    self._backoff(attempt, last_error)
                    continue
                raise LLMError(f"{self.label}: {last_error}") from exc

            if not content:
                last_error = "empty completion"
                if attempt < MAX_RETRIES:
                    self._backoff(attempt, last_error)
                    continue
                raise LLMError(f"{self.label}: {last_error}")

            return content

        raise LLMError(f"{self.label}: {last_error}")

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        wait = RETRY_BASE_SECONDS * (2**attempt) + random.uniform(0, 1)
        logger.warning("Retrying in %.1fs after: %s", wait, reason)
        time.sleep(wait)


def _json_instruction(system_text: str, schema: dict[str, Any]) -> str:
    instruction = (
        "You must reply with a single JSON object and nothing else — no prose, "
        "no markdown fences. The object must conform to this JSON Schema:\n"
        f"{json.dumps(schema, indent=2)}"
    )
    return f"{system_text}\n\n{instruction}".strip()


def _with_system(messages: list[dict[str, str]], system_text: str) -> list[dict[str, str]]:
    rest = [m for m in messages if m.get("role") != "system"]
    return [{"role": "system", "content": system_text}] + rest
