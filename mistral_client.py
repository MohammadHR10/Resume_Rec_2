import os
import requests
from dotenv import load_dotenv

load_dotenv()
#print("API KEY:", os.getenv("mistral_api"))

api_key = os.getenv("mistral_api")
API_URL = "https://api.mistral.ai/v1/chat/completions"


headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}

def call_mistral(prompt):
    payload = {
    "model": "mistral-small-latest",
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
