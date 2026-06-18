from __future__ import annotations

from typing import Optional

from .provider_config import (
    get_provider_api_key,
    get_provider_base_url,
    get_provider_base_url_default,
    get_provider_default_model,
    get_provider_model,
    set_provider_api_key,
    set_provider_base_url,
    set_provider_model,
    clear_provider_api_key,
)


DEFAULT_DEEPSEEK_BASE_URL = get_provider_base_url_default("deepseek")
DEFAULT_DEEPSEEK_MODEL = get_provider_default_model("deepseek")


def get_deepseek_api_key() -> Optional[str]:
    return get_provider_api_key("deepseek")


def set_deepseek_api_key(api_key: str) -> None:
    set_provider_api_key("deepseek", api_key)


def clear_deepseek_api_key() -> None:
    clear_provider_api_key("deepseek")


def get_deepseek_base_url() -> str:
    return get_provider_base_url("deepseek")


def set_deepseek_base_url(base_url: str) -> None:
    set_provider_base_url("deepseek", base_url)


def get_deepseek_model() -> str:
    return get_provider_model("deepseek")


def set_deepseek_model(model: str) -> None:
    set_provider_model("deepseek", model)
