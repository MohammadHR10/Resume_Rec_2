import os
import requests
from dotenv import load_dotenv

load_dotenv()

SEED = 42

LLM_GATEWAY_URL = os.getenv("LLM_GATEWAY_URL", "")
LLM_GATEWAY_KEY = os.getenv("LLM_GATEWAY_KEY", "")
HL_PROJECT_ID = os.getenv("HL_PROJECT_ID", "")
HL_REQUESTER_ID = os.getenv("HL_REQUESTER_ID", "")

MODEL = os.getenv("LLM_MODEL", "llama-3.2-90b-vision-instruct")
MODEL_SECOND = os.getenv("LLM_SECOND_MODEL", "Nemotron 49b")
MODEL_ARBITER = os.getenv("LLM_ARBITER_MODEL", "GPT 120b")


def _call_api(prompt, model):
    """Send a prompt to the VDI LLM gateway."""
    if not (LLM_GATEWAY_URL and LLM_GATEWAY_KEY):
        return {"error": "LLM_GATEWAY_URL / LLM_GATEWAY_KEY not configured", "choices": []}

    headers = {
        "Authorization": f"Bearer {LLM_GATEWAY_KEY}",
        "Content-Type": "application/json",
    }
    if HL_PROJECT_ID:
        headers["hl-project-id"] = HL_PROJECT_ID
    if HL_REQUESTER_ID:
        headers["hl-requester-id"] = HL_REQUESTER_ID

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "seed": SEED,
    }

    try:
        response = requests.post(LLM_GATEWAY_URL, headers=headers, json=payload, timeout=120)
    except requests.exceptions.RequestException as e:
        return {"error": str(e), "status_code": 0, "raw": ""}

    if response.status_code != 200:
        print(f"LLM call failed ({model}) status {response.status_code}: {response.text[:300]}")

    try:
        data = response.json()
    except Exception as e:
        return {"error": str(e), "status_code": response.status_code, "raw": response.text}

    return data


def call_mistral(prompt, model=None):
    """Model A — primary scorer."""
    return _call_api(prompt, model or MODEL)


def call_second_scorer(prompt):
    """Model B — second scorer."""
    return _call_api(prompt, MODEL_SECOND)


def call_arbiter(prompt):
    """Final arbiter that reconciles both scorers' judgments."""
    return _call_api(prompt, MODEL_ARBITER)


def call_extractor(prompt):
    """Dedicated bias-free resume extractor (uses the arbiter-class model)."""
    return _call_api(prompt, MODEL_ARBITER)
