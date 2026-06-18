from __future__ import annotations

import re
from typing import Any, Dict, Optional

from services.dispatch_api import DispatchAPI

from .errors import MissingApiKeyError
from .openai_compat_http import OpenAICompatHTTPClient
from .provider_config import (
    get_current_provider,
    get_provider_api_key,
    get_provider_base_url,
    get_provider_model,
)


_FENCE_RE = re.compile(r"```(?:python)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _extract_code(text: str) -> str:
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    if m:
        return (m.group(1) or "").strip() + "\n"
    return text.strip() + "\n"


class DeepSeekDispatchAPI(DispatchAPI):
    def request(self, task_name: str, payload: Dict[str, Any]) -> Any:
        provider = str((payload or {}).get("provider") or get_current_provider()).strip().lower()
        api_key = get_provider_api_key(provider)
        if not api_key:
            raise MissingApiKeyError(f"{provider}_api_key_missing")

        prompt = str((payload or {}).get("prompt") or "").strip()
        if not prompt:
            raise RuntimeError("prompt_missing")

        base_url = str((payload or {}).get("base_url") or get_provider_base_url(provider)).strip()
        model = str((payload or {}).get("model") or get_provider_model(provider)).strip()

        client = OpenAICompatHTTPClient(base_url=base_url, api_key=api_key, timeout_sec=60)
        resp = client.create_chat_completion(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": prompt},
            ],
            stream=False,
            extra_body=None,
        )

        content: Optional[str] = None
        try:
            choices = resp.get("choices") or []
            if choices:
                message = (choices[0] or {}).get("message") or {}
                content = message.get("content")
        except Exception:
            content = None

        if not content:
            raise RuntimeError("empty_response")

        code = _extract_code(str(content))
        return {"code": code, "raw": resp, "task": task_name}
