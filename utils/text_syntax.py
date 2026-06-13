from __future__ import annotations

from typing import List


_CHARSET_RANGES = {
    "a-z": list("abcdefghijklmnopqrstuvwxyz"),
    "A-Z": list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    "0-9": list("0123456789"),
}


def parse_escaped_csv(raw: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    escaped = False

    for ch in str(raw or ""):
        if escaped:
            current.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == ",":
            token = "".join(current).strip()
            if token:
                parts.append(token)
            current = []
            continue
        current.append(ch)

    if escaped:
        current.append("\\")

    token = "".join(current).strip()
    if token:
        parts.append(token)

    return parts


def _expand_charset_token(token: str) -> List[str]:
    if token in _CHARSET_RANGES:
        return list(_CHARSET_RANGES[token])

    out: List[str] = []
    i = 0
    while i < len(token):
        matched = False
        for range_token, chars in _CHARSET_RANGES.items():
            if token.startswith(range_token, i):
                out.extend(chars)
                i += len(range_token)
                matched = True
                break
        if not matched:
            return [token]
    return out


def expand_charset_spec(spec: str) -> List[str]:
    alphabet: List[str] = []
    for token in parse_escaped_csv(spec):
        alphabet.extend(_expand_charset_token(token))
    return alphabet
