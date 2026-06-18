from __future__ import annotations

from typing import Dict, List, Optional

from .secure_storage import SecureSettingsStore

DEFAULT_PROVIDER = "deepseek"

PROVIDER_DEFS: Dict[str, Dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "default_model": "deepseek-reasoner",
    },
    "nuwa": {
        "label": "Nuwa",
        "base_url": "https://api.nuwaflux.com/v1",
        "default_model": "claude-sonnet-4-6",
    },
}

_ORG = "PyQtApp"
_APP = "DataGenerator"
_KEY_CURRENT_PROVIDER = "llm/current_provider"


def _store() -> SecureSettingsStore:
    return SecureSettingsStore(organization=_ORG, application=_APP)


def normalize_provider(provider: str) -> str:
    key = str(provider or "").strip().lower()
    if key in PROVIDER_DEFS:
        return key
    return DEFAULT_PROVIDER


def list_providers() -> List[str]:
    return list(PROVIDER_DEFS.keys())


def get_provider_defs() -> Dict[str, Dict[str, str]]:
    return dict(PROVIDER_DEFS)


def get_provider_label(provider: str) -> str:
    provider = normalize_provider(provider)
    return str(PROVIDER_DEFS.get(provider, {}).get("label") or provider)


def get_provider_base_url_default(provider: str) -> str:
    provider = normalize_provider(provider)
    return str(PROVIDER_DEFS.get(provider, {}).get("base_url") or "")


def get_provider_default_model(provider: str) -> str:
    provider = normalize_provider(provider)
    return str(PROVIDER_DEFS.get(provider, {}).get("default_model") or "")


def get_current_provider() -> str:
    value = _store().get_value(_KEY_CURRENT_PROVIDER, DEFAULT_PROVIDER)
    return normalize_provider(str(value or DEFAULT_PROVIDER))


def set_current_provider(provider: str) -> None:
    _store().set_value(_KEY_CURRENT_PROVIDER, normalize_provider(provider))


def _key(provider: str, field: str) -> str:
    provider = normalize_provider(provider)
    return f"llm/providers/{provider}/{field}"


def get_provider_api_key(provider: str) -> Optional[str]:
    return _store().get_secret(_key(provider, "api_key"))


def set_provider_api_key(provider: str, api_key: str) -> None:
    _store().set_secret(_key(provider, "api_key"), api_key)


def clear_provider_api_key(provider: str) -> None:
    _store().clear_secret(_key(provider, "api_key"))


def get_provider_base_url(provider: str) -> str:
    default = get_provider_base_url_default(provider)
    value = _store().get_value(_key(provider, "base_url"), default)
    return str(value or default).strip() or default


def set_provider_base_url(provider: str, base_url: str) -> None:
    default = get_provider_base_url_default(provider)
    value = str(base_url or "").strip() or default
    _store().set_value(_key(provider, "base_url"), value)


def get_provider_model(provider: str) -> str:
    default = get_provider_default_model(provider)
    value = _store().get_value(_key(provider, "model"), default)
    return str(value or default).strip() or default


def set_provider_model(provider: str, model: str) -> None:
    default = get_provider_default_model(provider)
    value = str(model or "").strip() or default
    _store().set_value(_key(provider, "model"), value)
