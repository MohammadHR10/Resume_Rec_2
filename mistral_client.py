import os
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("LLM_GATEWAY_KEY")
API_URL = os.getenv("LLM_GATEWAY_URL", "http://litellma01.tkg.utshare.internal:4000/v1/chat/completions")
MODEL = os.getenv("LLM_MODEL", "llama-3.2-90b-vision-instruct")
MODEL_CALIBRATOR = os.getenv("LLM_CALIBRATOR_MODEL", "GPT 120b")

SEED = 42

HL_PROJECT_ID = os.getenv("HL_PROJECT_ID", "")
HL_REQUESTER_ID = os.getenv("HL_REQUESTER_ID", "resume-modeler")

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

if HL_PROJECT_ID:
    headers["hl-project-id"] = HL_PROJECT_ID
if HL_REQUESTER_ID:
    headers["hl-requester-id"] = HL_REQUESTER_ID


def call_mistral(prompt, model=None):
    """Call the LLM Gateway. Uses MODEL by default, pass model= to override."""
    payload = {
        "model": model or MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "seed": SEED
    }

    response = requests.post(API_URL, headers=headers, json=payload)

    if response.status_code != 200:
        print(f"LLM call failed (status {response.status_code}): {response.text[:200]}")

    try:
        return response.json()
    except Exception as e:
        return {
            "error": str(e),
            "status_code": response.status_code,
            "raw": response.text
        }


def call_calibrator(prompt):
    """Call the calibrator model (GPT 120b by default) for batch comparison."""
    return call_mistral(prompt, model=MODEL_CALIBRATOR)
