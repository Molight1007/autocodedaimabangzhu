from __future__ import annotations

from typing import Optional

from .secure_storage import SecureSettingsStore


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-reasoner"

_ORG = "PyQtApp"
_APP = "DataGenerator"
_KEY_API_KEY = "deepseek/api_key"
_KEY_BASE_URL = "deepseek/base_url"
_KEY_MODEL = "deepseek/model"


def _store() -> SecureSettingsStore:
    return SecureSettingsStore(organization=_ORG, application=_APP)


def get_deepseek_api_key() -> Optional[str]:
    return _store().get_secret(_KEY_API_KEY)


def set_deepseek_api_key(api_key: str) -> None:
    _store().set_secret(_KEY_API_KEY, api_key)


def clear_deepseek_api_key() -> None:
    _store().clear_secret(_KEY_API_KEY)


def get_deepseek_base_url() -> str:
    s = _store().get_value(_KEY_BASE_URL, DEFAULT_DEEPSEEK_BASE_URL)
    return str(s or DEFAULT_DEEPSEEK_BASE_URL).strip() or DEFAULT_DEEPSEEK_BASE_URL


def set_deepseek_base_url(base_url: str) -> None:
    base_url = str(base_url or "").strip() or DEFAULT_DEEPSEEK_BASE_URL
    _store().set_value(_KEY_BASE_URL, base_url)


def get_deepseek_model() -> str:
    s = _store().get_value(_KEY_MODEL, DEFAULT_DEEPSEEK_MODEL)
    return str(s or DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL


def set_deepseek_model(model: str) -> None:
    model = str(model or "").strip() or DEFAULT_DEEPSEEK_MODEL
    _store().set_value(_KEY_MODEL, model)
