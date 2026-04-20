import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

SEED = 42
MAX_RETRIES = 5

# --- Provider configs ---
# Model A: Groq (primary scorer)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_URL = os.getenv("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")
MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")

# Model B: Groq (second scorer, different model family)
MODEL_SECOND = os.getenv("LLM_SECOND_MODEL", "qwen/qwen3-32b")

# Arbiter: Gemini (strongest free model, large context)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_URL = os.getenv("GEMINI_URL", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
MODEL_ARBITER = os.getenv("LLM_ARBITER_MODEL", "gemini-2.0-flash")

# Internal gateway (VDI fallback)
GATEWAY_KEY = os.getenv("LLM_GATEWAY_KEY", "")
GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "")
HL_PROJECT_ID = os.getenv("HL_PROJECT_ID", "")
HL_REQUESTER_ID = os.getenv("HL_REQUESTER_ID", "")


def _call_api(prompt, model, api_url, api_key, extra_headers=None, use_seed=True):
    """Generic LLM call to any OpenAI-compatible endpoint."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    if extra_headers:
        headers.update(extra_headers)

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if use_seed:
        payload["seed"] = SEED

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=120)
        except requests.exceptions.RequestException as e:
            return {"error": str(e), "status_code": 0, "raw": ""}

        if response.status_code == 429:
            wait = 2 ** attempt + 1
            print(f"Rate limited ({model}), waiting {wait}s before retry {attempt+1}/{MAX_RETRIES}")
            time.sleep(wait)
            continue

        if response.status_code != 200:
            print(f"LLM call failed ({model} @ {api_url}) status {response.status_code}: {response.text[:300]}")

        break
    else:
        return {"error": "Rate limited after max retries", "status_code": 429, "raw": ""}

    try:
        data = response.json()
    except Exception as e:
        return {"error": str(e), "status_code": response.status_code, "raw": response.text}

    # Strip <think>...</think> blocks from reasoning models (e.g. Qwen3)
    if isinstance(data, dict) and "choices" in data:
        for choice in data["choices"]:
            content = choice.get("message", {}).get("content", "")
            if "<think>" in content:
                import re
                content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
                choice["message"]["content"] = content

    return data


def call_mistral(prompt, model=None):
    """Call Model A (primary scorer) via Groq. Falls back to internal gateway if configured."""
    m = model or MODEL
    if GROQ_API_KEY:
        return _call_api(prompt, m, GROQ_URL, GROQ_API_KEY)
    if GATEWAY_KEY and GATEWAY_URL:
        extra = {}
        if HL_PROJECT_ID:
            extra["hl-project-id"] = HL_PROJECT_ID
        if HL_REQUESTER_ID:
            extra["hl-requester-id"] = HL_REQUESTER_ID
        return _call_api(prompt, m, GATEWAY_URL, GATEWAY_KEY, extra)
    return {"error": "No API key configured for primary scorer", "choices": []}


def call_second_scorer(prompt):
    """Call Model B (second scorer) via Groq with a different model."""
    if GROQ_API_KEY:
        return _call_api(prompt, MODEL_SECOND, GROQ_URL, GROQ_API_KEY)
    if GATEWAY_KEY and GATEWAY_URL:
        return _call_api(prompt, MODEL_SECOND, GATEWAY_URL, GATEWAY_KEY)
    return {"error": "No API key configured for second scorer", "choices": []}


def call_arbiter(prompt):
    """Call the arbiter model for final judgment.
    Tries Gemini first, then Groq, then internal gateway."""
    if GEMINI_API_KEY:
        return _call_api(prompt, MODEL_ARBITER, GEMINI_URL, GEMINI_API_KEY, use_seed=False)
    if GROQ_API_KEY:
        return _call_api(prompt, MODEL_ARBITER, GROQ_URL, GROQ_API_KEY)
    if GATEWAY_KEY and GATEWAY_URL:
        return _call_api(prompt, MODEL_ARBITER, GATEWAY_URL, GATEWAY_KEY)
    return {"error": "No API key configured for arbiter", "choices": []}
