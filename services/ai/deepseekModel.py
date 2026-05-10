from __future__ import annotations

from typing import List

from services.ai.deepseek_config import get_deepseek_api_key, get_deepseek_base_url
from services.ai.errors import MissingApiKeyError
from services.ai.openai_compat_http import OpenAICompatHTTPClient


def list_models() -> List[str]:
    api_key = get_deepseek_api_key()
    if not api_key:
        raise MissingApiKeyError("deepseek_api_key_missing")
    client = OpenAICompatHTTPClient(base_url=get_deepseek_base_url(), api_key=api_key, timeout_sec=30)
    resp = client.list_models()
    items = resp.get("data") or []
    ids = []
    for it in items:
        mid = (it or {}).get("id")
        if mid:
            ids.append(str(mid))
    ids = sorted(set(ids))
    return ids
