from __future__ import annotations

from typing import Optional

from .deepseek_dispatch import DeepSeekDispatchAPI

_instance: Optional[DeepSeekDispatchAPI] = None


def get_default_dispatch_api() -> DeepSeekDispatchAPI:
    global _instance
    if _instance is None:
        _instance = DeepSeekDispatchAPI()
    return _instance

