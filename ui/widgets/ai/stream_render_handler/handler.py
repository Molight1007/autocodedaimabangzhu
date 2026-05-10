"""流式渲染适配层 Handler。

核心职责：
- 管理 SSE 流式数据的累积缓冲区（reasoning / content）和 UI 刷新缓冲区。
- 通过 QTimer（120ms 防抖）批量刷新双面板编辑器，避免每收到一个 chunk 都触发重绘。
- 实时清理：cleanup_partial_text 去除 iesa 标签，用于右侧面板增量显示。
- 完成清理：cleanup_final_code 提取 ```python 代码块，用于右侧面板最终结果。

不负责：
- 不管理 request_id 校验（由调用方在回调入口过滤旧请求）。
- 不管理 ChatSession.commit_turn（由 Dialog 在 on_done 后调用）。
- 不管理 Worker 生命周期或 UI 忙闲状态。

主要数据流：
1. 入口：Dialog 收到 StreamWorker.delta Signal → 校验 request_id → 调用 Handler.on_delta。
2. on_delta：累积 reasoning/content → 拼接原始文本 + 清理文本 → 写入 pending 缓冲区 → 启动防抖定时器。
3. flush：定时器触发 → 将 pending 缓冲区内容写入 left_editor / right_editor → 光标移至末尾。
4. 入口：Dialog 收到 StreamWorker.done Signal → 校验 request_id → 调用 Handler.on_done。
5. on_done：用最终 reasoning/content 重写编辑器（非增量，全量替换）→ 返回清理后代码供 Dialog 提交历史。
6. reset：清空所有缓冲区 + 停止定时器，供新一轮请求前调用。

约束：
- flush 间隔 120ms，setSingleShot(True)，每次 on_delta 检查定时器未激活才启动。
- on_done 后必须调用 stop_flush_timer，防止残留定时器覆盖最终结果。
"""

from __future__ import annotations

from typing import Optional, Tuple

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QTextCursor

from utils.ai_text_utils import cleanup_partial_text, cleanup_final_code


class StreamRenderHandler(QObject):
    """流式渲染适配层，隔离缓冲区管理与防抖刷新逻辑。"""

    def __init__(self, *, left_editor, right_editor, parent=None):
        super().__init__(parent)
        self._left_editor = left_editor
        self._right_editor = right_editor
        self._reasoning_accum = ""
        self._content_accum = ""
        self._pending_raw_text = ""
        self._pending_refined_text = ""
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(120)
        self._flush_timer.timeout.connect(self._flush)

    def reset(self) -> None:
        """清空所有缓冲区并停止定时器，供新一轮请求前调用。"""
        self._reasoning_accum = ""
        self._content_accum = ""
        self._pending_raw_text = ""
        self._pending_refined_text = ""
        self._flush_timer.stop()

    def stop_flush_timer(self) -> None:
        """停止防抖定时器，供流式完成或取消时调用。"""
        self._flush_timer.stop()

    def on_delta(self, reasoning_piece: str, content_piece: str) -> None:
        """处理流式增量数据，累积到缓冲区并启动防抖刷新。

        数据流：
        - 累积 reasoning_piece / content_piece → 拼接原始文本（含 iesa 块）→
        - cleanup_partial_text 生成实时清理文本 → 写入 pending 缓冲区 →
        - 若定时器未激活则启动（120ms 后 flush）。
        """
        if reasoning_piece:
            self._reasoning_accum += str(reasoning_piece)
        if content_piece:
            self._content_accum += str(content_piece)

        raw = ""
        if self._reasoning_accum.strip():
            raw += "iesa\n" + self._reasoning_accum.rstrip() + "\niesa\n\n"
        raw += self._content_accum

        refined_live = cleanup_partial_text(self._content_accum)
        self._pending_raw_text = raw
        self._pending_refined_text = refined_live
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def on_done(self, reasoning_text: str, content_text: str) -> str:
        """处理流式完成，全量刷新编辑器并返回清理后的代码。

        数据流：
        - 用最终 reasoning/content 全量替换编辑器内容（非增量）。
        - 左侧面板显示完整原始输出（含 iesa 块）。
        - 右侧面板显示 cleanup_final_code 清理后的代码。
        - 返回清理后代码，供 Dialog 提交到 ChatSession。

        返回值：cleanup_final_code 清理后的代码字符串。
        """
        self._reasoning_accum = str(reasoning_text or "")
        self._content_accum = str(content_text or "")
        self._flush_timer.stop()

        raw = ""
        if self._reasoning_accum.strip():
            raw += "iesa\n" + self._reasoning_accum.rstrip() + "\niesa\n\n"
        raw += self._content_accum
        self._left_editor.set_text(raw)
        self._left_editor.editor.moveCursor(QTextCursor.End)

        cleaned = cleanup_final_code(self._content_accum)
        if cleaned.strip():
            self._right_editor.set_text(cleaned)
            self._right_editor.editor.moveCursor(QTextCursor.End)

        return cleaned

    def _flush(self) -> None:
        """防抖定时器回调，批量刷新双面板内容。"""
        raw = self._pending_raw_text
        refined = self._pending_refined_text
        if raw:
            self._left_editor.set_text(raw)
            self._left_editor.editor.moveCursor(QTextCursor.End)
        if refined:
            self._right_editor.set_text(refined)
            self._right_editor.editor.moveCursor(QTextCursor.End)
