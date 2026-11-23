"""一段对话一次调用：取模板、控预算、记 `llm_calls`。

刻意不做的事：
- 不拼提示词（模板在 `prompts/templates/*.md`，拼装即改提示词契约）；
- 不解析业务字段（`validators` 负责）；
- 不在重试里偷偷换参数（重试由调用方决定，输入会带 `<已驳回>`）。
"""

from __future__ import annotations

import hashlib
import time
import traceback
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from ..llm.client import LlmClient, LlmConfig, LlmError, extract_json_object, load_config
from ..prompts import loader
from .stages import StageSpec


class StageError(RuntimeError):
    """一次对话失败：HTTP、JSON、结构三种都归到这里，由上层落 job.error。"""

    def __init__(self, message: str, code: str = "LLM_FAILED") -> None:
        super().__init__(message)
        self.code = code


class BudgetExhausted(StageError):
    def __init__(self, message: str = "本题预算已用完") -> None:
        super().__init__(message, "BUDGET_EXHAUSTED")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StageRunner:
    """`responder` 注入后完全不碰网络，供离线自测使用。"""

    def __init__(
        self,
        db: Any,
        *,
        problem_id: Optional[int] = None,
        budget: int = 6,
        responder: Optional[Callable[[str, str, str], Dict[str, Any]]] = None,
        llm_config: Optional[LlmConfig] = None,
        user: str = "",
    ) -> None:
        self.db = db
        self.problem_id = problem_id
        self.budget = max(1, int(budget))
        self.responder = responder
        #: 调用者本人的模型凭据；不给才退回全局配置（离线自测/命令行仍走这条路）
        self.llm_config = llm_config
        self.user = str(user or "")
        self.calls = 0
        self.tokens = 0
        self.last_model = ""
        self._clients: Dict[str, LlmClient] = {}

    # ── 客户端 ──────────────────────────────────────────────────────────────
    def _client(self, spec: StageSpec) -> LlmClient:
        key = spec.model or "__default__"
        if key not in self._clients:
            cfg = self.llm_config if self.llm_config is not None else load_config()
            if spec.model and cfg.model != spec.model:
                cfg = replace(cfg, model=spec.model)
            self._clients[key] = LlmClient(config=cfg)
        return self._clients[key]

    @property
    def vision_supported(self) -> bool:
        """本 runner 用过的客户端是否接受图片输入。

        `False` 表示服务端报过"这个模型不收图"、客户端已剥图重试：
        那一次的回复看着成功，其实模型**没看到图**，调用方必须自己拦下来。
        """
        return all(client._vision_supported for client in self._clients.values())

    # ── 记账 ────────────────────────────────────────────────────────────────
    def _record(
        self,
        spec: StageSpec,
        *,
        attempt: int,
        ok: bool,
        error_kind: str = "",
        usage: Any = None,
        elapsed_ms: float = 0.0,
        prompt_hash: str = "",
    ) -> None:
        data = usage.to_dict() if usage is not None else {}
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO llm_calls(user, problem_id, stage, attempt, model, prompt_version,"
                " prompt_hash, ok, error_kind, prompt_tokens, completion_tokens,"
                " total_tokens, elapsed_ms, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.user,
                    self.problem_id,
                    spec.stage,
                    int(attempt),
                    spec.model or "default",
                    spec.version,
                    prompt_hash,
                    1 if ok else 0,
                    error_kind[:60],
                    int(data.get("prompt_tokens", 0)),
                    int(data.get("completion_tokens", 0)),
                    int(data.get("total_tokens", 0)),
                    float(elapsed_ms),
                    _now(),
                ),
            )
        self.tokens += int(data.get("total_tokens", 0))

    # ── 主入口 ──────────────────────────────────────────────────────────────
    def _budget_guard(self) -> None:
        if self.calls >= self.budget:
            raise BudgetExhausted(f"本题预算 {self.budget} 次对话已用完")
        self.calls += 1

    def call_text(self, spec: StageSpec, user_content: Any, *, attempt: int = 1) -> str:
        """要纯文本（参考解是代码块、图片转写是正文）；`user_content` 也可以是
        多模态 content 数组（`[{type:text}, {type:image_url}]`）。记账口径与 `call` 一致。"""
        self._budget_guard()

        system_text = loader.load(spec.template)
        prompt_hash = hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16]

        if self.responder is not None:
            started = time.perf_counter()
            try:
                obj = self.responder(spec.stage, user_content, system_text)
            except Exception as exc:  # noqa: BLE001
                self._record(spec, attempt=attempt, ok=False, error_kind="stub", prompt_hash=prompt_hash)
                raise StageError(f"离线桩失败：{exc}", "STUB_FAILED") from exc
            self._record(
                spec, attempt=attempt, ok=True,
                elapsed_ms=(time.perf_counter() - started) * 1000.0, prompt_hash=prompt_hash,
            )
            self.last_model = "stub"
            if isinstance(obj, str):
                return obj
            # 桩若返回 dict，允许用 code 字段（便于自测复用同一套桩）
            if isinstance(obj, dict) and isinstance(obj.get("code"), str):
                return str(obj["code"])
            raise StageError("离线桩未返回代码文本", "STUB_FAILED")

        started = time.perf_counter()
        try:
            result = self._client(spec).chat(
                [{"role": "system", "content": system_text},
                 {"role": "user", "content": user_content}],
                json_mode=False,
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                timeout_s=spec.timeout_s,
                thinking=getattr(spec, "thinking", "default"),
                tag=spec.stage,
                stage=spec.stage,
                problem_id=self.problem_id,
                prompt_hash=prompt_hash,
            )
        except LlmError as exc:
            self._record(
                spec, attempt=attempt, ok=False, error_kind="llm",
                elapsed_ms=(time.perf_counter() - started) * 1000.0, prompt_hash=prompt_hash,
            )
            raise StageError(f"模型调用失败：{exc}", "LLM_FAILED") from exc
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._record(
                spec, attempt=attempt, ok=False, error_kind="unknown",
                elapsed_ms=(time.perf_counter() - started) * 1000.0, prompt_hash=prompt_hash,
            )
            raise StageError(f"模型调用异常：{type(exc).__name__}", "LLM_FAILED") from exc

        self.last_model = str(result.model or "")
        text = str(result.content or "")
        self._record(
            spec,
            attempt=attempt,
            ok=bool(text.strip()),
            error_kind="" if text.strip() else "empty",
            usage=result.usage,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            prompt_hash=prompt_hash,
        )
        if not text.strip():
            raise StageError("模型返回空内容", "EMPTY")
        return text

    def call(self, spec: StageSpec, user_content: str, *, attempt: int = 1) -> Dict[str, Any]:
        self._budget_guard()

        system_text = loader.load(spec.template)
        prompt_hash = hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16]

        if self.responder is not None:
            started = time.perf_counter()
            try:
                obj = self.responder(spec.stage, user_content, system_text)
            except Exception as exc:  # noqa: BLE001
                self._record(spec, attempt=attempt, ok=False, error_kind="stub", prompt_hash=prompt_hash)
                raise StageError(f"离线桩失败：{exc}", "STUB_FAILED") from exc
            self._record(
                spec, attempt=attempt, ok=True,
                elapsed_ms=(time.perf_counter() - started) * 1000.0, prompt_hash=prompt_hash,
            )
            self.last_model = "stub"
            if not isinstance(obj, dict):
                raise StageError("模型返回的不是 JSON 对象", "BAD_JSON")
            return obj

        started = time.perf_counter()
        try:
            result = self._client(spec).chat(
                [{"role": "system", "content": system_text},
                 {"role": "user", "content": user_content}],
                json_mode=True,
                temperature=spec.temperature,
                max_tokens=spec.max_tokens,
                timeout_s=spec.timeout_s,
                thinking=getattr(spec, "thinking", "default"),
                tag=spec.stage,
                stage=spec.stage,
                problem_id=self.problem_id,
                prompt_hash=prompt_hash,
            )
        except LlmError as exc:
            self._record(
                spec, attempt=attempt, ok=False, error_kind="llm",
                elapsed_ms=(time.perf_counter() - started) * 1000.0, prompt_hash=prompt_hash,
            )
            raise StageError(f"模型调用失败：{exc}", "LLM_FAILED") from exc
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._record(
                spec, attempt=attempt, ok=False, error_kind="unknown",
                elapsed_ms=(time.perf_counter() - started) * 1000.0, prompt_hash=prompt_hash,
            )
            raise StageError(f"模型调用异常：{type(exc).__name__}", "LLM_FAILED") from exc

        obj = result.json_obj or extract_json_object(result.content)
        self.last_model = str(result.model or "")
        self._record(
            spec,
            attempt=attempt,
            ok=obj is not None,
            error_kind="" if obj is not None else "bad_json",
            usage=result.usage,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
            prompt_hash=prompt_hash,
        )
        if obj is None:
            raise StageError(
                "模型输出不是合法 JSON（可能被截断）。原文前 200 字："
                + str(result.content or "")[:200],
                "BAD_JSON",
            )
        return obj
