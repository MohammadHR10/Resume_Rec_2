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

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

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
