import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Use internal LLM Gateway instead of Mistral API
api_key = os.getenv("LLM_GATEWAY_KEY")
API_URL = os.getenv("LLM_GATEWAY_URL", "http://litellma01.tkg.utshare.internal:4000/v1/chat/completions")
MODEL = os.getenv("LLM_MODEL", "llama-3.2-90b-vision-instruct")

# Hardcoded seed for deterministic/repeatable outputs
SEED = 42

# HiddenLayer guardrail headers — routes to the Resume Modeler project policy
HL_PROJECT_ID = os.getenv("HL_PROJECT_ID", "")
HL_REQUESTER_ID = os.getenv("HL_REQUESTER_ID", "resume-modeler")

print(f"[mistral_client] HL_PROJECT_ID loaded: {'YES (' + HL_PROJECT_ID[:8] + '...)' if HL_PROJECT_ID else 'EMPTY — headers will NOT be sent'}")
print(f"[mistral_client] HL_REQUESTER_ID loaded: {HL_REQUESTER_ID or 'EMPTY'}")

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

if HL_PROJECT_ID:
    headers["hl-project-id"] = HL_PROJECT_ID
if HL_REQUESTER_ID:
    headers["hl-requester-id"] = HL_REQUESTER_ID

print(f"[mistral_client] Headers being sent: {[k for k in headers.keys()]}")

def call_mistral(prompt):
    """
    Call the internal LLM Gateway for completions.
    Using the model specified in .env (default: llama-3.2-90b-vision-instruct)
    
    Uses a hardcoded seed (42) for deterministic/repeatable outputs.
    Same prompt will always produce the same response.
    """
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "seed": SEED  # Always use hardcoded seed for repeatability
    }

    response = requests.post(API_URL, headers=headers, json=payload)

    print("Status Code:", response.status_code)
    print("Response Text:", response.text)

    try:
        return response.json()
    except Exception as e:
        return {
            "error": str(e),
            "status_code": response.status_code,
            "raw": response.text
        }
