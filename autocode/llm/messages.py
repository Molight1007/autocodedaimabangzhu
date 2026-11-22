"""多模态消息的检测与降级工具。

拆出来单独放，是因为 `client.py` 有 800 行硬线（R4 门禁）：
图片相关的两个纯函数放进去会直接把文件顶超。

用途：模型不接受图片输入时（HTTP 400 "does not support image"），
调用方把 content 数组压回纯文本重试，而不是整次调用失败。
"""

from __future__ import annotations

from typing import Any, Dict, List

#: 会被当成"图片附件"的 content 块类型。
IMAGE_PART_TYPES = ("image_url", "file", "input_image")


def has_image_blocks(messages: Any) -> bool:
    """消息里是否含图片块（多模态 content 数组中的 image_url / file）。"""
    if not isinstance(messages, list):
        return False
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in IMAGE_PART_TYPES:
                return True
    return False


def strip_image_blocks(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把多模态消息压回纯文本（幂等；非数组 content 原样保留）。"""
    out: List[Dict[str, Any]] = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            out.append(message)
            continue
        texts = [
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        out.append({**message, "content": "\n".join(text for text in texts if text)})
    return out
