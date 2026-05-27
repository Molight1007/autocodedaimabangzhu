from __future__ import annotations

import random
from typing import Any, Dict, Iterable, Optional


DEFAULT_DISTRIBUTION = "uniform"


def generate_range_value(
    *,
    var_type: str,
    vmin: float,
    vmax: float,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Range 类型分布生成入口。

    目前默认保持现有均匀随机行为，同时预留后续策略扩展入口，
    这样 UI 侧只需要往 params 中补充 distribution / segments 等字段即可。
    """
    params = params or {}
    if vmax < vmin:
        vmin, vmax = vmax, vmin

    distribution = str(params.get("distribution") or DEFAULT_DISTRIBUTION).strip().lower()

    if distribution == "biased_low":
        return _generate_biased_low(var_type=var_type, vmin=vmin, vmax=vmax)
    if distribution == "biased_high":
        return _generate_biased_high(var_type=var_type, vmin=vmin, vmax=vmax)
    if distribution == "edge_focused":
        return _generate_edge_focused(var_type=var_type, vmin=vmin, vmax=vmax, params=params)
    if distribution == "custom_segments":
        return _generate_custom_segments(var_type=var_type, vmin=vmin, vmax=vmax, params=params)

    return _generate_uniform(var_type=var_type, vmin=vmin, vmax=vmax)


def supported_distributions() -> Iterable[str]:
    return ("uniform", "biased_low", "biased_high", "edge_focused", "custom_segments")


def _generate_uniform(*, var_type: str, vmin: float, vmax: float) -> Any:
    if _is_float_type(var_type):
        return random.uniform(vmin, vmax)
    return random.randint(int(vmin), int(vmax))


def _generate_biased_low(*, var_type: str, vmin: float, vmax: float) -> Any:
    ratio = random.random() ** 2
    return _blend_ratio(var_type=var_type, vmin=vmin, vmax=vmax, ratio=ratio)


def _generate_biased_high(*, var_type: str, vmin: float, vmax: float) -> Any:
    ratio = 1.0 - (random.random() ** 2)
    return _blend_ratio(var_type=var_type, vmin=vmin, vmax=vmax, ratio=ratio)


def _generate_edge_focused(*, var_type: str, vmin: float, vmax: float, params: Dict[str, Any]) -> Any:
    edge_bias = _to_float(params.get("edge_bias"), default=0.6)
    edge_bias = min(1.0, max(0.0, edge_bias))
    if random.random() < edge_bias:
        if _is_float_type(var_type):
            return random.choice([float(vmin), float(vmax)])
        return random.choice([int(vmin), int(vmax)])
    return _generate_uniform(var_type=var_type, vmin=vmin, vmax=vmax)


def _generate_custom_segments(*, var_type: str, vmin: float, vmax: float, params: Dict[str, Any]) -> Any:
    segments = params.get("segments")
    if not isinstance(segments, list):
        return _generate_uniform(var_type=var_type, vmin=vmin, vmax=vmax)

    weighted_segments = []
    weights = []
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        weight = max(0.0, _to_float(segment.get("weight"), default=0.0))
        if weight <= 0:
            continue
        weighted_segments.append(segment)
        weights.append(weight)

    if not weighted_segments or sum(weights) <= 0:
        return _generate_uniform(var_type=var_type, vmin=vmin, vmax=vmax)

    picked = random.choices(weighted_segments, weights=weights, k=1)[0]
    return _generate_segment_value(var_type=var_type, vmin=vmin, vmax=vmax, segment=picked)


def _generate_segment_value(*, var_type: str, vmin: float, vmax: float, segment: Dict[str, Any]) -> Any:
    mode = str(segment.get("mode") or "range").strip().lower()
    if mode == "fixed":
        return _coerce_value_by_type(segment.get("value"), var_type=var_type, default=vmin)
    if mode == "edge":
        return _generate_edge_segment_value(var_type=var_type, vmin=vmin, vmax=vmax, segment=segment)

    seg_min = _to_float(segment.get("min"), default=vmin)
    seg_max = _to_float(segment.get("max"), default=vmax)
    if seg_max < seg_min:
        seg_min, seg_max = seg_max, seg_min
    return _generate_uniform(var_type=var_type, vmin=seg_min, vmax=seg_max)


def _generate_edge_segment_value(*, var_type: str, vmin: float, vmax: float, segment: Dict[str, Any]) -> Any:
    raw_edges = str(segment.get("edges") or "").strip()
    candidates = []
    if raw_edges:
        for part in raw_edges.replace("，", ",").split(","):
            text = part.strip()
            if not text:
                continue
            candidates.append(_coerce_value_by_type(text, var_type=var_type, default=vmin))
    if not candidates:
        candidates = _default_edge_candidates(var_type=var_type, vmin=vmin, vmax=vmax)
    return random.choice(candidates)


def _blend_ratio(*, var_type: str, vmin: float, vmax: float, ratio: float) -> Any:
    value = vmin + (vmax - vmin) * ratio
    if _is_float_type(var_type):
        return float(value)
    return int(round(value))


def _default_edge_candidates(*, var_type: str, vmin: float, vmax: float) -> list[Any]:
    if _is_float_type(var_type):
        return [float(vmin), float(vmax)]

    imin = int(vmin)
    imax = int(vmax)
    values = [imin, imax]
    if imin + 1 <= imax:
        values.append(imin + 1)
    if imax - 1 >= imin:
        values.append(imax - 1)
    return list(dict.fromkeys(values))


def _coerce_value_by_type(value: Any, *, var_type: str, default: float) -> Any:
    number = _to_float(value, default=default)
    if _is_float_type(var_type):
        return float(number)
    return int(round(number))


def _is_float_type(var_type: str) -> bool:
    return str(var_type or "").strip().lower() == "float"


def _to_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)
