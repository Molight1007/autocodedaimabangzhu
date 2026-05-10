from __future__ import annotations

import importlib
from typing import Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_ATTR_MODULES: tuple[str, ...] = (
    "math",
    "random",
    "itertools",
    "statistics",
    "functools",
)


def collect_module_attr_metadata(module_names: Iterable[str]) -> Tuple[Dict[str, List[str]], Dict[str, Set[str]]]:
    attr_sources: Dict[str, List[str]] = {}
    callable_attr_sources: Dict[str, Set[str]] = {}
    for raw_name in module_names:
        name = str(raw_name or "").strip()
        if not name or name in attr_sources:
            continue
        try:
            module = importlib.import_module(name)
        except Exception:
            continue

        attrs: List[str] = []
        callable_attrs: Set[str] = set()
        for attr in dir(module):
            if str(attr).startswith("_"):
                continue
            attrs.append(attr)
            try:
                if callable(getattr(module, attr, None)):
                    callable_attrs.add(attr)
            except Exception:
                continue

        attr_sources[name] = sorted(set(attrs))
        callable_attr_sources[name] = callable_attrs

    return attr_sources, callable_attr_sources


def should_insert_call_parentheses(
    *,
    completion: str,
    next_char: str,
    attr_owner: Optional[str],
    callable_builtins: Set[str],
    callable_attr_sources: Dict[str, Set[str]],
) -> bool:
    token = str(completion or "").strip()
    if not token:
        return False
    if str(next_char or "") == "(":
        return False

    owner = str(attr_owner or "").strip()
    if owner:
        return token in callable_attr_sources.get(owner, set())
    return token in callable_builtins
