"""Provider clients against a mocked transport — schema strictness, retries, fallbacks."""

from __future__ import annotations

import json

import pytest
import requests

from backend.llm import base, registry
from backend.llm.fastllm import FastLLMProvider
from backend.llm.ulproxy import ULProxyProvider


class FakeResponse:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def completion(content: str) -> FakeResponse:
    return FakeResponse(200, {"choices": [{"message": {"content": content}}]})


@pytest.fixture
def transport(monkeypatch):
    """Record every request and serve queued responses in order."""

    calls: list[dict] = []
    queue: list[FakeResponse] = []

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "body": json})
        if not queue:
            raise AssertionError(f"unexpected POST to {url}")
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def get(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers})
        if not queue:
            raise AssertionError(f"unexpected GET to {url}")
        return queue.pop(0)

    monkeypatch.setattr(base._session, "post", post)
    monkeypatch.setattr(base._session, "get", get)
    monkeypatch.setattr(base.time, "sleep", lambda _s: None)
    return {"calls": calls, "queue": queue}


SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "score": {"type": "integer"}},
}


# ---------------------------------------------------------------------------
# Schema handling
# ---------------------------------------------------------------------------

def test_ensure_strict_schema_closes_objects_and_requires_every_property():
    schema = base.ensure_strict_schema(json.loads(json.dumps(SCHEMA)))
    assert schema["additionalProperties"] is False
    assert sorted(schema["required"]) == ["name", "score"]


def test_ensure_strict_schema_recurses_into_arrays_and_defs():
    schema = base.ensure_strict_schema(
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "object", "properties": {"a": {}}}}
            },
        }
    )
    item = schema["properties"]["items"]["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["a"]


def test_parse_json_loose_handles_fences_and_preamble():
    assert base.parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert base.parse_json_loose('Sure! {"a": 1}') == {"a": 1}
    assert base.parse_json_loose('{"a": 1}') == {"a": 1}


def test_parse_json_loose_raises_on_garbage():
    with pytest.raises(base.LLMError):
        base.parse_json_loose("no json here at all")


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def test_structured_extract_posts_a_strict_json_schema(transport):
    transport["queue"].append(completion('{"name": "Ada", "score": 3}'))
    provider = ULProxyProvider(default_model="gpt-5.2")
    result = provider.structured_extract("prompt", SCHEMA)

    assert result == {"name": "Ada", "score": 3}
    body = transport["calls"][0]["body"]
    assert transport["calls"][0]["url"] == "https://proxy.example/v1/chat/completions"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert body["temperature"] == 0
    assert transport["calls"][0]["headers"]["Authorization"] == "Bearer test-token"


def test_a_url_typed_with_v1_is_not_doubled(monkeypatch, transport):
    monkeypatch.setenv("ULMAIPROXY_BASE_URL", "https://proxy.example/v1")
    transport["queue"].append(completion('{"name": "x", "score": 1}'))
    ULProxyProvider(default_model="m").structured_extract("p", SCHEMA)
    assert transport["calls"][0]["url"] == "https://proxy.example/v1/chat/completions"


def test_rate_limits_are_retried_then_succeed(transport):
    transport["queue"].extend([FakeResponse(429, text="slow down"), completion('{"name":"a","score":1}')])
    result = ULProxyProvider(default_model="m").structured_extract("p", SCHEMA)
    assert result["name"] == "a"
    assert len(transport["calls"]) == 2


def test_connection_errors_are_retried_and_eventually_raise(transport):
    transport["queue"].extend([requests.ConnectionError("boom")] * (base.MAX_RETRIES + 1))
    with pytest.raises(base.LLMError, match="connection error"):
        ULProxyProvider(default_model="m").structured_extract("p", SCHEMA)


def test_client_errors_fail_fast(transport):
    transport["queue"].append(FakeResponse(401, text="bad token"))
    with pytest.raises(base.LLMError, match="401"):
        ULProxyProvider(default_model="m").structured_extract("p", SCHEMA)
    assert len(transport["calls"]) == 1


# ---------------------------------------------------------------------------
# The SIS gateway's own contract, as proven by the Streamlit app's VDI-branch
# client: LLM_GATEWAY_* env names, hl-* attribution headers, a pinned seed, and
# a URL that may be the endpoint rather than a root.
# ---------------------------------------------------------------------------

@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_URL", "https://gateway.example/v1")
    monkeypatch.setenv("LLM_GATEWAY_KEY", "gw-key")
    monkeypatch.setenv("HL_PROJECT_ID", "proj-7")
    monkeypatch.setenv("HL_REQUESTER_ID", "dastewart")
    monkeypatch.setenv("LLM_MODEL", "llama-3.2-90b-vision-instruct")
    for stale in ("FASTLLM_BASE_URL", "FASTLLM_API_KEY", "FASTLLM_MODEL", "LLM_SEED"):
        monkeypatch.delenv(stale, raising=False)


def test_gateway_reads_the_env_names_the_existing_app_uses(gateway):
    provider = FastLLMProvider()
    assert provider.base_url == "https://gateway.example/v1"
    assert provider.api_key == "gw-key"
    assert provider.default_model == "llama-3.2-90b-vision-instruct"
    assert provider.is_configured() is True


def test_gateway_sends_the_attribution_headers_and_a_pinned_seed(gateway, transport):
    transport["queue"].append(completion('{"name": "a", "score": 1}'))
    FastLLMProvider().structured_extract("p", SCHEMA)

    call = transport["calls"][0]
    assert call["headers"]["hl-project-id"] == "proj-7"
    assert call["headers"]["hl-requester-id"] == "dastewart"
    assert call["headers"]["Authorization"] == "Bearer gw-key"
    # Determinism matters most for the bias audit, where an unpinned sample
    # would read as a delta caused by the injected attribute.
    assert call["body"]["seed"] == 42
    assert call["url"] == "https://gateway.example/v1/chat/completions"


def test_the_seed_is_overridable(gateway, monkeypatch, transport):
    monkeypatch.setenv("LLM_SEED", "1234")
    transport["queue"].append(completion('{"name": "a", "score": 1}'))
    FastLLMProvider().structured_extract("p", SCHEMA)
    assert transport["calls"][0]["body"]["seed"] == 1234


def test_attribution_headers_are_omitted_when_unset(gateway, monkeypatch, transport):
    monkeypatch.delenv("HL_PROJECT_ID")
    monkeypatch.delenv("HL_REQUESTER_ID")
    transport["queue"].append(completion('{"name": "a", "score": 1}'))
    FastLLMProvider().structured_extract("p", SCHEMA)
    assert "hl-project-id" not in transport["calls"][0]["headers"]


def test_a_url_naming_the_full_endpoint_is_posted_to_verbatim(gateway, monkeypatch, transport):
    monkeypatch.setenv("LLM_GATEWAY_URL", "https://gateway.example/v1/chat/completions")
    transport["queue"].append(completion('{"name": "a", "score": 1}'))
    FastLLMProvider().structured_extract("p", SCHEMA)
    assert transport["calls"][0]["url"] == "https://gateway.example/v1/chat/completions"


def test_model_listing_still_resolves_from_a_full_endpoint_url(gateway, monkeypatch, transport):
    monkeypatch.setenv("LLM_GATEWAY_URL", "https://gateway.example/v1/chat/completions")
    transport["queue"].append(FakeResponse(200, {"data": [{"id": "GPT 120b"}]}))
    assert FastLLMProvider().list_models() == ["GPT 120b"]
    assert transport["calls"][0]["url"] == "https://gateway.example/v1/models"


def test_gateway_harness_env_points_codex_at_the_root_not_the_endpoint(gateway, monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_URL", "https://gateway.example/v1/chat/completions")
    assert FastLLMProvider().harness_env()["OPENAI_BASE_URL"] == "https://gateway.example/v1"


def test_the_ul_proxy_sends_no_gateway_specific_extras(transport):
    transport["queue"].append(completion('{"name": "a", "score": 1}'))
    ULProxyProvider(default_model="m").structured_extract("p", SCHEMA)
    call = transport["calls"][0]
    assert "seed" not in call["body"]
    assert "hl-project-id" not in call["headers"]


def test_a_gateway_that_rejects_response_format_falls_back_to_prompted_json(transport):
    transport["queue"].extend(
        [
            FakeResponse(400, text='{"error":"response_format is not supported"}'),
            completion('{"name": "Ada", "score": 2}'),
        ]
    )
    provider = FastLLMProvider(default_model="mistral-small")
    provider.base_url = "https://fastllm.example"
    provider.api_key = "k"
    result = provider.structured_extract("p", SCHEMA)

    assert result == {"name": "Ada", "score": 2}
    assert provider.supports_json_schema is False
    retried = transport["calls"][1]["body"]
    assert "response_format" not in retried
    assert "JSON Schema" in retried["messages"][0]["content"]


def test_chat_without_a_schema_returns_plain_text(transport):
    transport["queue"].append(completion("Because Bob meets more required quals."))
    answer = ULProxyProvider(default_model="m").chat([{"role": "user", "content": "why?"}])
    assert answer.startswith("Because Bob")


def test_a_missing_model_is_refused_before_any_request():
    with pytest.raises(base.LLMError, match="no model selected"):
        ULProxyProvider().structured_extract("p", SCHEMA)


def test_an_unconfigured_provider_is_refused_before_any_request(monkeypatch):
    monkeypatch.setenv("ULMAIPROXY_BASE_URL", "")
    monkeypatch.setenv("ULMAIPROXY_AUTH_TOKEN", "")
    with pytest.raises(base.ProviderNotConfigured):
        ULProxyProvider(default_model="m").structured_extract("p", SCHEMA)


# ---------------------------------------------------------------------------
# Listing, health, harness routing
# ---------------------------------------------------------------------------

def test_list_models_reads_the_openai_shaped_listing(transport):
    transport["queue"].append(FakeResponse(200, {"data": [{"id": "b"}, {"id": "a"}]}))
    assert ULProxyProvider().list_models() == ["a", "b"]


def test_list_models_swallows_failures(transport):
    transport["queue"].append(FakeResponse(500, text="down"))
    assert ULProxyProvider().list_models() == []


def test_health_falls_back_to_a_model_listing_when_there_is_no_health_route(transport):
    transport["queue"].extend([FakeResponse(404, text="nope"), FakeResponse(200, {"data": [{"id": "a"}]})])
    assert ULProxyProvider().health()["ok"] is True


def test_ulproxy_harness_env_routes_both_clis():
    env = ULProxyProvider().harness_env()
    assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example"
    assert env["OPENAI_BASE_URL"] == "https://proxy.example/v1"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "test-token"


def test_fastllm_harness_env_offers_no_anthropic_route(monkeypatch):
    monkeypatch.setenv("FASTLLM_BASE_URL", "https://fastllm.example")
    env = FastLLMProvider().harness_env()
    assert "ANTHROPIC_BASE_URL" not in env
    assert env["OPENAI_BASE_URL"] == "https://fastllm.example/v1"


def test_unconfigured_fastllm_yields_no_harness_env(monkeypatch):
    monkeypatch.setenv("FASTLLM_BASE_URL", "")
    assert FastLLMProvider().harness_env() == {}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_defaults_to_the_ul_proxy():
    provider, name, model = registry.active()
    assert name == "ulproxy"
    assert isinstance(provider, ULProxyProvider)
    assert model == ""


def test_registry_follows_stored_config():
    from backend import db

    db.set_config({"provider": "fastllm", "model": "mistral-small"})
    _, name, model = registry.active()
    assert (name, model) == ("fastllm", "mistral-small")


def test_chat_mode_defaults_per_provider_and_is_overridable():
    from backend import db

    assert registry.chat_mode("ulproxy", "gpt-5.2") == "harness"
    assert registry.chat_mode("fastllm", "mistral-small") == "structured"
    db.set_config({"chat_modes": {"fastllm:mistral-small": "harness"}})
    assert registry.chat_mode("fastllm", "mistral-small") == "harness"


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        registry.get_provider("nope")
