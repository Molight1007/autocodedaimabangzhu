import logging
from typing import Dict, List

from PySide6.QtCore import QThread, Signal

from services.ai.openai_compat_http import OpenAICompatHTTPClient
from services.ai.deepseekModel import list_models

logger = logging.getLogger(__name__)


class StreamWorker(QThread):
    delta = Signal(int, str, str)
    done = Signal(int, str, str)
    err = Signal(int, str)

    def __init__(self, *, request_id: int, messages: List[Dict[str, str]], model: str, base_url: str, api_key: str, parent=None):
        super().__init__(parent)
        self._request_id = int(request_id)
        self._messages: List[Dict[str, str]] = []
        for m in messages or []:
            item = dict(m or {})
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "")
            if role and content:
                self._messages.append({"role": role, "content": content})
        self._model = str(model or "").strip()
        self._base_url = str(base_url or "").strip()
        self._api_key = str(api_key or "").strip()

    def run(self):
        try:
            client = OpenAICompatHTTPClient(
                base_url=self._base_url,
                api_key=self._api_key,
                timeout_sec=90,
            )
            reasoning_text = ""
            content_text = ""
            for chunk in client.stream_chat_completion(
                model=self._model,
                messages=self._messages,
            ):
                if self.isInterruptionRequested():
                    return
                try:
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0] or {}).get("delta") or {}
                    reasoning_piece = delta.get("reasoning_content") or ""
                    content_piece = delta.get("content") or ""
                    if reasoning_piece:
                        reasoning_text += str(reasoning_piece)
                    if content_piece:
                        content_text += str(content_piece)
                    if reasoning_piece or content_piece:
                        if self.isInterruptionRequested():
                            return
                        self.delta.emit(self._request_id, str(reasoning_piece), str(content_piece))
                except Exception:
                    logger.exception("解析流式响应分片失败")
                    continue
            if self.isInterruptionRequested():
                return
            self.done.emit(self._request_id, reasoning_text, content_text)
        except Exception as e:
            if self.isInterruptionRequested():
                return
            self.err.emit(self._request_id, str(e))


class ModelsWorker(QThread):
    ok = Signal(list)
    err = Signal(str)

    def run(self):
        try:
            self.ok.emit(list_models())
        except Exception as e:
            self.err.emit(str(e))
