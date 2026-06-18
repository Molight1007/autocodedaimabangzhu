from __future__ import annotations

from typing import Dict, List

from services.ai.model_catalog import list_models as list_catalog_models


def list_models(provider: str = "deepseek") -> List[Dict[str, str]]:
    return list_catalog_models(provider)
