from __future__ import annotations

import importlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DistributionEntry:
    key: str
    name: str
    function_path: str
    group: Optional[str]
    params: List[Dict[str, Any]] = field(default_factory=list)
    special_ui: Optional[str] = None
    description: str = ""
    _function: Any = None

    @property
    def display_name(self) -> str:
        if self.group:
            return f"{self.group} / {self.name}"
        return self.name


class DistributionRegistry:
    def __init__(self):
        self._entries: Dict[str, DistributionEntry] = {}
        self._ordered_keys: List[str] = []

    def load(self, json_path: str) -> None:
        if not os.path.isfile(json_path):
            return

        with open(json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

        entries: List[DistributionEntry] = []

        for key, data in raw.items():
            if not isinstance(data, dict):
                continue

            function_path = str(data.get("function", "")).strip()
            if not function_path:
                continue

            fn = self._resolve_function(function_path)
            if fn is None:
                continue

            params = []
            for p in data.get("params") or []:
                if isinstance(p, dict):
                    params.append({
                        "key": str(p.get("key", "")),
                        "label": str(p.get("label", "")),
                        "placeholder": str(p.get("placeholder", "")),
                        "default": p.get("default"),
                    })

            entry = DistributionEntry(
                key=key,
                name=str(data.get("name", key)),
                function_path=function_path,
                group=data.get("group") or None,
                params=params,
                special_ui=data.get("special_ui") or None,
                description=str(data.get("description", "")),
                _function=fn,
            )
            entries.append(entry)

        self._entries = {}
        self._ordered_keys = []
        for entry in entries:
            self._entries[entry.key] = entry
            self._ordered_keys.append(entry.key)

    def get_entry(self, key: str) -> Optional[DistributionEntry]:
        return self._entries.get(key)

    def get_display_name(self, key: str) -> str:
        entry = self._entries.get(key)
        return entry.display_name if entry else key

    def get_display_name_map(self) -> Dict[str, str]:
        return {entry.key: entry.display_name for entry in self._entries.values()}

    def get_key_label_map(self) -> Dict[str, str]:
        return {entry.key: entry.name for entry in self._entries.values()}

    def get_display_name_to_key_map(self) -> Dict[str, str]:
        return {entry.display_name: entry.key for entry in self._entries.values()}

    @property
    def ordered_display_names(self) -> List[str]:
        return [entry.display_name for entry in self._entries.values()]

    def get_grouped_entries(self) -> List[Dict[str, Any]]:
        groups: Dict[str, List[DistributionEntry]] = {}
        for entry in self._entries.values():
            g = entry.group or ""
            groups.setdefault(g, []).append(entry)
        result: List[Dict[str, Any]] = []
        if "" in groups:
            for entry in groups.pop(""):
                result.append({"type": "item", "entry": entry})
        for group_name, entries in groups.items():
            result.append({"type": "group", "name": group_name, "entries": entries})
        return result

    @staticmethod
    def _resolve_function(qualified_path: str):
        try:
            parts = qualified_path.rsplit(".", 1)
            if len(parts) != 2:
                return None
            module = importlib.import_module(parts[0])
            return getattr(module, parts[1], None)
        except Exception:
            return None


_distribution_registry: Optional[DistributionRegistry] = None


def get_distribution_registry() -> DistributionRegistry:
    global _distribution_registry
    if _distribution_registry is None:
        _distribution_registry = DistributionRegistry()
        json_path = os.path.join(os.path.dirname(__file__), "..", "..", "File", "distributions.json")
        json_path = os.path.normpath(os.path.abspath(json_path))
        _distribution_registry.load(json_path)
    return _distribution_registry
