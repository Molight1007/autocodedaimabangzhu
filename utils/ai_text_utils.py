import re


_THINK_BLOCK_RE = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)
_TRIPLE_QUOTE_THINK_RE = re.compile(r"(?:'''|\"\"\")\s*think[\s\S]*?(?:'''|\"\"\")", re.IGNORECASE)
_FENCE_THINK_RE = re.compile(r"```(?:think|thinking)[\s\S]*?```", re.IGNORECASE)
_FENCE_CODE_RE = re.compile(r"```(?:python)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def cleanup_partial_text(text: str) -> str:
    t = text or ""
    t = _THINK_BLOCK_RE.sub("", t)
    t = _TRIPLE_QUOTE_THINK_RE.sub("", t)
    t = _FENCE_THINK_RE.sub("", t)
    return t


def cleanup_final_code(text: str) -> str:
    t = cleanup_partial_text(text)
    m = _FENCE_CODE_RE.search(t)
    if m:
        t = m.group(1) or ""
    t = t.strip()
    if t:
        t += "\n"
    return t


def clip_text(text: str, max_chars: int) -> str:
    t = str(text or "")
    lim = max(256, int(max_chars or 0))
    if len(t) <= lim:
        return t
    keep_head = int(lim * 0.7)
    keep_tail = lim - keep_head
    return t[:keep_head] + "\n...\n(内容过长，已截断)\n...\n" + t[-keep_tail:]
