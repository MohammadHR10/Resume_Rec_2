import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Use internal LLM Gateway instead of Mistral API
api_key = os.getenv("LLM_GATEWAY_KEY")
API_URL = os.getenv("LLM_GATEWAY_URL", "http://litellma01.tkg.utshare.internal:4000/v1/chat/completions")
MODEL = os.getenv("LLM_MODEL", "llama-3.2-90b-vision-instruct")

headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

def call_mistral(prompt):
    """
    Call the internal LLM Gateway for completions.
    Using the model specified in .env (default: llama-3.2-90b-vision-instruct)
    """
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}]
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
