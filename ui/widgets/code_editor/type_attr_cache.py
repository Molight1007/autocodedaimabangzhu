"""类型属性缓存。

职责：
- 对 Python 内置/标准库类型执行 dir() 并缓存其公共属性。
- 区分可调用属性与普通属性，供补全器决定是否自动插入 ()。
- 预热常用内置类型，避免首次补全时的 dir() 延迟。

不负责：
- 不处理用户自定义类（仅覆盖内置类型 + TextIOWrapper）。
- 不持久化缓存（进程生命周期内有效）。

数据流：
TypeAttrCache 初始化 → 预热 str/int/list/dict 等 →
运行时首次遇到新类型 → _get_type_by_name() → _cache_type() → 缓存
"""

from __future__ import annotations

from typing import Dict, List, Set


# ── 名称 → 类型对象 ──────────────────────────────────────

def _get_type_by_name(name: str):
    """按名称获取类型对象。"""
    _BUILTIN_TYPE_MAP: Dict[str, type] = {
        "str": str,
        "int": int,
        "float": float,
        "list": list,
        "dict": dict,
        "set": set,
        "tuple": tuple,
        "bool": bool,
        "bytes": bytes,
        "bytearray": bytearray,
        "range": range,
        "frozenset": frozenset,
        "complex": complex,
        "enumerate": enumerate,
        "zip": zip,
        "map": map,
        "filter": filter,
        "reversed": reversed,
        "NoneType": type(None),
    }
    if name in _BUILTIN_TYPE_MAP:
        return _BUILTIN_TYPE_MAP[name]

    if name == "TextIOWrapper":
        import io
        return io.TextIOWrapper

    return None


class TypeAttrCache:
    """dir() 结果缓存，区分可调用/非可调用属性。

    典型用法:
        cache = TypeAttrCache()
        attrs = cache.get_attrs("str")         # → ["capitalize", "find", ...]
        callables = cache.get_callables("str") # → {"capitalize", "find", ...}
    """

    def __init__(self):
        self._attr_cache: Dict[str, List[str]] = {}
        self._callable_cache: Dict[str, Set[str]] = {}
        self._warmup()

    def _warmup(self):
        """预热常用内置类型，避免首次 dir() 延迟。"""
        warmup_types = [
            str, int, float, list, dict, set, tuple, bool,
            bytes, bytearray, range, frozenset,
        ]
        for t in warmup_types:
            self._cache_type(t)

    def _cache_type(self, typ: type):
        """执行 dir(typ) 并缓存结果。"""
        name = typ.__name__
        if name in self._attr_cache:
            return
        attrs: List[str] = []
        callables: Set[str] = set()
        for attr in dir(typ):
            if attr.startswith("_"):
                continue
            attrs.append(attr)
            try:
                if callable(getattr(typ, attr, None)):
                    callables.add(attr)
            except Exception:
                continue
        self._attr_cache[name] = sorted(attrs)
        self._callable_cache[name] = callables

    def get_attrs(self, type_name: str) -> List[str]:
        """获取类型的公共属性列表（不以下划线开头，已排序）。"""
        if type_name not in self._attr_cache:
            typ = _get_type_by_name(type_name)
            if typ is not None:
                self._cache_type(typ)
            else:
                return []
        return self._attr_cache.get(type_name, [])

    def get_callables(self, type_name: str) -> Set[str]:
        """获取类型的可调用属性集合。"""
        if type_name not in self._callable_cache:
            typ = _get_type_by_name(type_name)
            if typ is not None:
                self._cache_type(typ)
            else:
                return set()
        return self._callable_cache.get(type_name, set())
