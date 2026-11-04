"""Pass 1 scratch: one DeepSeek request, printed unparsed. Throwaway.

Deliberately crude — no error classification, no response type, no abstraction.
Those are the point of model.py. This file exists so you have seen the raw
JSON once before you decide what to keep from it.

    python scratch_request.py
"""

import json
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

# API_KEY = os.environ["DEEPSEEK_APIKEY"]
API_KEY = "fp;ejf"
BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

payload = {
    "model": MODEL,
    "messages": [
        {"role": "system", "content": "You are a terse assistant."},
        {"role": "user", "content": "Say hello in five words"},
    ],
}

started = time.monotonic()
response = httpx.post(
    f"{BASE_URL}/chat/completions",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json=payload,
    timeout=httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0),
)
elapsed_ms = (time.monotonic() - started) * 1000

print(f"HTTP {response.status_code}  in {elapsed_ms:.0f} ms\n")
try:
    print(json.dumps(response.json(), indent=2))
except ValueError:
    print("body was not JSON:\n", response.text)
