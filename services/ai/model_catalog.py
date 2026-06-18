from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .openai_compat_http import OpenAICompatHTTPClient
from .provider_config import (
    get_provider_api_key,
    get_provider_base_url,
    get_provider_default_model,
    get_provider_label,
    normalize_provider,
)

_ROOT_DIR = Path(__file__).resolve().parents[2]
_NUWA_MODEL_FILE = _ROOT_DIR / "File" / "nuwa.json"


def _format_price(value: Any, currency: str) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "待定"
    symbol = "￥" if str(currency or "").upper() == "CNY" else ""
    return f"{symbol}{num:.2f}"


def _build_price_text(model: Dict[str, Any]) -> str:
    currency = str(model.get("currency") or "CNY")
    unit = str(model.get("price_unit") or "per_1m_tokens")
    unit_text = "/ 1M tokens" if unit == "per_1m_tokens" else unit
    input_text = _format_price(model.get("input_price_per_m"), currency)
    output_text = _format_price(model.get("output_price_per_m"), currency)
    return f"输入 {input_text} / 输出 {output_text} {unit_text}"


def _normalize_remote_model(model_id: str, provider: str) -> Dict[str, Any]:
    model_id = str(model_id or "").strip()
    return {
        "id": model_id,
        "provider": provider,
        "provider_label": get_provider_label(provider),
        "vendor_name": get_provider_label(provider),
        "label": model_id,
        "description": "",
        "price_text": "价格待定",
        "input_price_per_m": None,
        "output_price_per_m": None,
        "currency": "",
        "price_unit": "",
        "supported_endpoint_types": ["openai"],
    }


def load_deepseek_models() -> List[Dict[str, Any]]:
    provider = "deepseek"
    api_key = get_provider_api_key(provider)
    if not api_key:
        default_model = get_provider_default_model(provider)
        return [_normalize_remote_model(default_model, provider)]

    client = OpenAICompatHTTPClient(
        base_url=get_provider_base_url(provider),
        api_key=api_key,
        timeout_sec=30,
    )
    response = client.list_models()
    items = response.get("data") or []
    models: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        model_id = str((item or {}).get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        models.append(_normalize_remote_model(model_id, provider))
    models.sort(key=lambda item: str(item.get("id") or "").lower())
    if models:
        return models
    default_model = get_provider_default_model(provider)
    return [_normalize_remote_model(default_model, provider)]


def load_nuwa_models() -> List[Dict[str, Any]]:
    if not _NUWA_MODEL_FILE.exists():
        return []
    payload = json.loads(_NUWA_MODEL_FILE.read_text(encoding="utf-8"))
    items = payload.get("models") or []
    models: List[Dict[str, Any]] = []
    for raw in items:
        item = dict(raw or {})
        supported = [str(x or "").strip().lower() for x in item.get("supported_endpoint_types") or []]
        if supported and "openai" not in supported:
            continue
        model_id = str(item.get("model_name") or "").strip()
        if not model_id:
            continue
        item["id"] = model_id
        item["provider"] = "nuwa"
        item["provider_label"] = get_provider_label("nuwa")
        item["label"] = model_id
        item["price_text"] = _build_price_text(item)
        models.append(item)
    models.sort(
        key=lambda item: (
            str(item.get("vendor_name") or "").lower(),
            str(item.get("id") or "").lower(),
        )
    )
    return models


def list_models(provider: str) -> List[Dict[str, Any]]:
    provider = normalize_provider(provider)
    if provider == "nuwa":
        return load_nuwa_models()
    return load_deepseek_models()
