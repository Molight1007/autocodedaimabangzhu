"""图片导入：让多模态模型把截图/拍照**逐行转写**成纯文本。

为什么不自己 OCR：
- 本地 OCR（tesseract / paddleocr）要额外装引擎与中文模型，服务器上体积大、安装易失败；
- 题面里的公式、下标、`10^9` 这类写法，通用 OCR 错得比模型多；
- 项目里所有"读题"的活本来就交给模型做，这里只是多一道"看图"的入口。

转写结果不直接进库：它和 PDF 抽出来的文字走同一条路（切题 → 人工勾选 → 入库），
所以图片与 PDF 的行为完全一致，除了"字是从哪来的"。

模型不收图片时（DeepSeek 只有多模态型号收）必须给人话：说清改哪个设置，
而不是让用户看着"转写结果为空"猜。
"""

from __future__ import annotations

import base64
from typing import Any, List

from . import files
from .llm_stage import StageRunner
from .stages import VISION

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}

#: 各模型厂商对图片格式的支持不一致（DeepSeek 只收 JPEG/PNG/GIF/WebP），
#: BMP 一律先在本地转成 PNG，免得白花一次调用。
_CONVERT_TO_PNG = (".bmp",)

#: 文件头 → 真实格式。前端在浏览器里把大图重编码成 JPEG 后，文件名还是 .png，
#: 声明成 image/png 但内容是 JPEG；模型按字节判格式能过，但报文本身不该撒谎。
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF8", ".gif"),
    (b"BM", ".bmp"),
)

#: 认不出字时的下限：一张题面截图不可能只有几十个字。
MIN_CHARS = 50

_NO_VISION_HINT = (
    "当前模型不接受图片输入。图片导入需要多模态模型："
    "DeepSeek 请把模型名填成 deepseek-flash（V4.1 Flash 原生支持图片），"
    "其他服务商填各自的视觉模型（如 gpt-4o、qwen-vl-max、glm-4v）。"
    "改完在「模型设置」里先「测试连接」再重试。"
)


def suffix_of(filename: str) -> str:
    name = str(filename or "").strip().lower()
    for suffix in _MIME:
        if name.endswith(suffix):
            return suffix
    return ".png"


def _to_png(raw: bytes) -> bytes:
    """BMP 之类不在支持列表里的格式，用 pymupdf 转成 PNG。失败原样返回。"""
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            return raw
    try:
        return pymupdf.Pixmap(raw).tobytes("png")
    except Exception:  # noqa: BLE001 - 转不动就让模型自己判，不在这里拦住
        return raw


def sniff(raw: bytes) -> str:
    """按文件头认格式；认不出返回空串。"""
    for magic, suffix in _MAGIC:
        if raw.startswith(magic):
            return suffix
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ""


def data_url(raw: bytes, suffix: str) -> str:
    suffix = sniff(raw) or suffix
    if suffix in _CONVERT_TO_PNG:
        raw = _to_png(raw)
        suffix = ".png"
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{_MIME.get(suffix, 'image/png')};base64,{encoded}"


def strip_fences(text: str) -> str:
    """模型偶尔还是会套一层 ``` 围栏：剥掉，别让围栏变成题面的第一行。"""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and lines[0].strip().startswith("```"):
        lines.pop(0)
    while lines and not lines[-1].strip():  # 尾部空行先去掉，否则围栏挡住了也看不见
        lines.pop()
    while lines and lines[-1].strip().startswith("```"):
        lines.pop()
        while lines and not lines[-1].strip():
            lines.pop()
    return "\n".join(lines).strip("\n")


def _one(runner: StageRunner, raw: bytes, prompt: str, suffix: str) -> str:
    """转写一页/一张。失败抛人话；不在这里判字数（整份文档才判）。"""
    content: List[Any] = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": data_url(raw, suffix)}},
    ]
    try:
        text = runner.call_text(VISION, content, attempt=1)
    except Exception as exc:  # noqa: BLE001 - 上层统一转成人话
        raise files.FileError(f"图片识别失败：{exc}", "VISION_FAILED") from exc

    if not runner.vision_supported:
        # 客户端在服务端返回 400 时会剥掉图片重试一次，那次回复看着"成功"其实没看图
        raise files.FileError(_NO_VISION_HINT, "NO_VISION_MODEL")
    return strip_fences(text)


def _require_text(text: str) -> str:
    if len(text.strip()) < MIN_CHARS:
        raise files.FileError(
            f"图片里只识别出 {len(text.strip())} 个字，可能是拍得太糊、字太小或被裁掉。"
            "请重新截图/拍照（整页、正面、光线均匀），或用带文字层的 PDF",
            "EMPTY_IMAGE",
        )
    return text


def transcribe(runner: StageRunner, filename: str, raw: bytes) -> str:
    """把一张图片转成题面文本；失败抛 `files.FileError`（带人话与错误码）。"""
    text = _one(runner, raw, "请把这张图片里的题目原文逐行照抄成纯文本。", suffix_of(filename))
    return _require_text(text)


def transcribe_pages(runner: StageRunner, filename: str, pages: List[bytes]) -> str:
    """多页文档（扫描版 PDF）逐页转写后拼接。

    扫描件和截图本质是同一种输入：都是"只有像素、没有文字层"。这里不复用
    `transcribe` 的整段判字数，否则只有一两行字的封面页会被当成识别失败。
    """
    parts: List[str] = []
    total = len(pages)
    for index, page in enumerate(pages, start=1):
        prompt = (
            f"这是同一份文档的第 {index}/{total} 页。"
            "请把这一页里的题目原文逐行照抄成纯文本，不要补其他页的内容。"
            if total > 1
            else "请把这张图片里的题目原文逐行照抄成纯文本。"
        )
        parts.append(_one(runner, page, prompt, suffix_of(filename)))
    return _require_text(strip_fences("\n".join(part for part in parts if part.strip())))
