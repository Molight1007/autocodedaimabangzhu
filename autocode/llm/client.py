"""DeepSeek 客户端（urllib 直连，不引 SDK）。

为什么不用官方 SDK：本项目只依赖 stdlib（设计 R1 的"零额外依赖"口径），
一次 chat 就是一次 JSON POST，引 SDK 只会多一层版本漂移风险。

凭据读取顺序（都不落库、不进仓库）：
1. 环境变量 `DEEPSEEK_API_KEY`（首选）
2. 环境变量为空或**服务端返回 401** 时，回退读 `File/key.txt`（`File/key.txt` 已被 .gitignore 忽略）
日志/异常里只允许出现 `key=****后4位`，禁止全量输出。

其他配置：`DEEPSEEK_API_BASE`（默认官方）、`DEEPSEEK_MODEL`（默认 deepseek-chat）。

实测坑（已内建处理）：
- **推理模型**（如 `deepseek-v4-flash`）：`max_tokens` 太小会被 reasoning 吃光，
  `content` 返回空串。故 max_tokens 下限 4096、空 content 视为失败并**加倍重试**，
  `reasoning_tokens` 单独记账（不混进 completion_tokens 解释）。
- **JSON 输出**：先试 `response_format={"type":"json_object"}`；若服务端 400 不支持，
  自动退回"围栏提取 + 括号配平截取"，并在本次进程内记住该能力，避免反复 400。

可靠性口径：
- 网络错误 / HTTP 429 / 5xx / JSON 解析失败 / 空 content → 指数退避重试（1s,2s,4s），
  总尝试次数 = 1 + `max_retries`（默认 3 次重试）。
- 4xx（除 429）= 请求本身有问题，**不重试**（避免烧钱），401 允许一次换 key 重试。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"
DEFAULT_TIMEOUT_S = 180
DEFAULT_MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
# 推理模型会把 max_tokens 先花在 reasoning 上，给小值等于必然空 content。
# 实测（deepseek-v4-flash，CSP 复杂题）：8192 可能全被 reasoning 吃掉，
# 16384 也出现过 reasoning=16384 / content 空，故上限放到 32768。
MIN_MAX_TOKENS = 4096
MAX_TOKENS_CAP = 32_768
MAX_TOKENS_DEFAULT = MIN_MAX_TOKENS

_ENV_KEY = "DEEPSEEK_API_KEY"
_ENV_BASE = "DEEPSEEK_API_BASE"
_ENV_MODEL = "DEEPSEEK_MODEL"
_ENV_TIMEOUT = "DEEPSEEK_TIMEOUT_S"


class LlmError(RuntimeError):
    """LLM 调用失败（重试耗尽 / 请求非法）。`attempts` 记录实际发了几次请求（计费用）。"""

    def __init__(self, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = int(attempts)


class LlmConfigError(LlmError):
    """配置缺失或请求参数非法（不重试；401 例外，允许换 key 重试一次）。"""

    def __init__(self, message: str, *, status: int = 0, attempts: int = 1) -> None:
        super().__init__(message, attempts=attempts)
        self.status = int(status)


@dataclass(frozen=True)
class LlmConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_s: int = DEFAULT_TIMEOUT_S
    max_retries: int = DEFAULT_MAX_RETRIES
    key_source: str = "env"

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    @property
    def masked_key(self) -> str:
        return mask_key(self.api_key)

    def redacted(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "key_source": self.key_source,
            "api_key": self.masked_key,
        }

    def with_key(self, key: str, source: str) -> "LlmConfig":
        return LlmConfig(
            api_key=key,
            base_url=self.base_url,
            model=self.model,
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
            key_source=source,
        )


def mask_key(key: str) -> str:
    """只暴露后 4 位；用于日志与异常（key 长度本身也不泄漏）。"""
    text = str(key or "")
    if len(text) <= 4:
        return "****" if text else "<missing>"
    return f"****{text[-4:]}"


def project_key_file() -> Path:
    """`<repo>/File/key.txt`（已 gitignore）。"""
    return Path(__file__).resolve().parents[2] / "File" / "key.txt"


def read_key_file(path: Optional[Path] = None) -> str:
    """读本地 key 文件；不存在/空则返回空串（不抛，交由上层决策）。"""
    target = Path(path) if path is not None else project_key_file()
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@dataclass
class ChatUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
        }

    def add(self, other: "ChatUsage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.reasoning_tokens += other.reasoning_tokens
        self.cache_hit_tokens += other.cache_hit_tokens


@dataclass
class ChatResult:
    content: str
    usage: ChatUsage
    attempts: int = 1
    elapsed_ms: float = 0.0
    model: str = ""
    json_obj: Optional[Dict[str, Any]] = None
    finish_reason: str = ""
    json_mode_used: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
            "content_chars": len(self.content),
            "has_json": self.json_obj is not None,
            "json_mode_used": self.json_mode_used,
        }


def _saved_settings() -> Dict[str, Any]:
    try:
        from .settings import load_settings
        return load_settings()
    except Exception:  # noqa: BLE001 - 设置文件损坏不应阻断默认配置
        return {}


def load_config(
    env: Optional[Mapping[str, str]] = None,
    *,
    key_file: Optional[Path] = None,
    allow_file_fallback: bool = True,
) -> LlmConfig:
    """读配置：环境变量优先，为空则回退 `File/key.txt`；两处都没有则抛 `LlmConfigError`。"""
    source = env if env is not None else os.environ
    saved = _saved_settings() if env is None else {}
    api_key = str(saved.get("api_key") or source.get(_ENV_KEY, "")).strip()
    saved_key = bool(saved.get("api_key"))
    key_source = "env"
    file_key = read_key_file(key_file) if allow_file_fallback else ""
    # 真实运行时（env 参数为 None）优先用项目内 key 文件：
    # 环境变量可能是早先遗留的失效凭据，先撞 401 每题多一次往返。
    # 显式传入 env 的调用方（测试/嵌入）仍保持"环境变量优先"的旧语义。
    if env is None and saved_key:
        pass
    elif env is None and file_key:
        api_key, key_source = file_key, "file"
    elif not api_key and file_key:
        api_key, key_source = file_key, "file"
    elif not api_key:
        key_source = "missing"
    if not api_key:
        raise LlmConfigError(
            f"未配置 {_ENV_KEY}（环境变量），且 {project_key_file()} 不存在/为空，无法调用 LLM"
        )
    base_url = str(saved.get("base_url") or source.get(_ENV_BASE, DEFAULT_BASE_URL)).strip() or DEFAULT_BASE_URL
    model = str(saved.get("model") or source.get(_ENV_MODEL, DEFAULT_MODEL)).strip() or DEFAULT_MODEL
    raw_timeout = str(source.get(_ENV_TIMEOUT, "")).strip()
    timeout_s = int(raw_timeout) if raw_timeout.isdigit() else DEFAULT_TIMEOUT_S
    return LlmConfig(
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout_s=timeout_s,
        key_source=key_source,
    )


# ── JSON 提取 ────────────────────────────────────────────────────────────────


def _balanced_object(text: str) -> Optional[str]:
    """从首个 `{` 起做括号配平截取（感知字符串与转义），失败返回 None。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里提取 JSON 对象：直解 → 围栏 → 括号配平截取。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    candidates: List[str] = [raw]
    if "```" in raw:
        for block in raw.split("```"):
            body = block.strip()
            if body.lower().startswith("json"):
                body = body[4:].strip()
            if body.startswith("{"):
                candidates.append(body)
    balanced = _balanced_object(raw)
    if balanced:
        candidates.append(balanced)
    for chunk in candidates:
        try:
            parsed = json.loads(chunk)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


class LlmClient:
    """薄封装：一次 `chat()` 一次 HTTP 调用（含重试），并累计用量。"""

    def __init__(
        self,
        config: Optional[LlmConfig] = None,
        *,
        sleep: Any = time.sleep,
        verbose: bool = False,
        key_file: Optional[Path] = None,
    ) -> None:
        self.config = config or load_config(key_file=key_file)
        self._sleep = sleep
        self.verbose = verbose
        self._key_file = key_file
        self._json_mode_supported = True  # 进程内记住服务端能力，避免反复 400
        self._thinking_supported = True  # 服务端不认 thinking 时置 False，后续不再发
        self.recorder: Any = None  # 可注入回调：每次尝试落库（含失败）
        self._file_fallback_done = self.config.key_source != "env"
        self.calls = 0
        self.failures = 0
        self.usage = ChatUsage()
        self.history: List[Dict[str, Any]] = []

    # ── 统计 ────────────────────────────────────────────────────────────────
    def stats(self) -> Dict[str, Any]:
        return {
            "calls": self.calls,
            "failures": self.failures,
            "usage": self.usage.to_dict(),
            "model": self.config.model,
            "key_source": self.config.key_source,
            "key": self.config.masked_key,
            "json_mode": self._json_mode_supported,
        }

    # ── 主入口 ──────────────────────────────────────────────────────────────
    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        json_mode: bool = True,
        temperature: float = 0.2,
        max_tokens: int = MAX_TOKENS_DEFAULT,
        timeout_s: Optional[int] = None,
        tag: str = "",
        stage: str = "",
        problem_id: Optional[int] = None,
        thinking: str = "default",
        thinking_budget: int = 0,
    ) -> ChatResult:
        """发一次对话请求。`json_mode=True` 时要求返回可解析 JSON 对象。

        `thinking`：`disabled` / `enabled` / `default`。推理模型把大量 token 花在
        reasoning 上，结构化抽取类阶段（spec/hints/brute）关掉它可省 60%+ 输出；
        代码生成类阶段保留但设 `budget_tokens` 上限。服务端不认该字段时自动降级。
        """
        if not messages:
            raise LlmConfigError("messages 不能为空")
        self.calls += 1
        budget_tokens = max(MIN_MAX_TOKENS, int(max_tokens))
        attempts_allowed = 1 + max(0, int(self.config.max_retries))
        last_error = ""
        attempt_messages = list(messages)
        empty_retry_done = False
        truncated_retry_done = False
        for attempt in range(1, attempts_allowed + 1):
            payload: Dict[str, Any] = {
                "model": self.config.model,
                "messages": attempt_messages,
                "temperature": temperature,
                "max_tokens": budget_tokens,
                "stream": False,
            }
            if thinking in ("disabled", "enabled") and self._thinking_supported:
                payload["thinking"] = {"type": thinking}
                if thinking == "enabled" and thinking_budget > 0:
                    payload["thinking"]["budget_tokens"] = int(thinking_budget)
            use_json_mode = bool(json_mode and self._json_mode_supported)
            if use_json_mode:
                payload["response_format"] = {"type": "json_object"}
            started = time.perf_counter()
            try:
                body = self._post(payload, timeout_s=timeout_s or self.config.timeout_s)
            except LlmConfigError as exc:
                self._record_attempt(stage, problem_id, attempt, tag, False, f"config:{exc.status}",
                                     ChatUsage(), 0.0)
                if self._handle_config_error(exc, json_mode=json_mode, tag=tag):
                    continue  # 已降级（关 JSON 模式 / 换 key / 去 thinking），立刻重试
                self.failures += 1
                raise
            except LlmError as exc:
                self._record_attempt(stage, problem_id, attempt, tag, False,
                                     f"http:{type(exc).__name__}", ChatUsage(), 0.0)
                last_error = str(exc)
                self._backoff(attempt, attempts_allowed, last_error, tag)
                continue
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 1)
            result = self._build_result(
                body, elapsed_ms=elapsed_ms, attempt=attempt, json_mode_used=use_json_mode
            )
            # 每次尝试都计入用量：重试/空返回/解析失败同样计费，不能只记成功
            self._accumulate(result)
            truncated = result.finish_reason == "length"
            ok = (
                bool(result.content.strip())
                and not truncated
                and (not json_mode or result.json_obj is not None)
            )
            self._record_attempt(
                stage, problem_id, attempt, tag, ok,
                "" if ok else (
                    "truncated" if truncated else
                    ("empty_content" if not result.content.strip() else "json_invalid")
                ),
                result.usage, elapsed_ms,
            )
            if truncated:
                # 被 max_tokens 截断的代码是残缺的，不能当成功；重发一次并要求精简
                last_error = f"输出被 max_tokens={budget_tokens} 截断"
                if truncated_retry_done:
                    break
                truncated_retry_done = True
                attempt_messages = list(messages) + [
                    {"role": "assistant", "content": result.content[:1500]},
                    {
                        "role": "user",
                        "content": "上一次输出因长度上限被截断。请精简到最小可用实现后重出完整代码，"
                                   "不要省略关键逻辑，不要重复注释。",
                    },
                ]
                self._backoff(attempt, attempts_allowed, last_error, tag)
                # 截断说明"代码确实比上限长"：先加倍上限；到顶后再要求精简
                budget_tokens = min(MAX_TOKENS_CAP, max(budget_tokens * 2, 16384))
                continue
            if not result.content.strip():
                # 推理模型把 token 全花在 reasoning 上的典型表现：content 空
                last_error = (
                    f"content 为空（finish_reason={result.finish_reason or '?'}，"
                    f"reasoning_tokens={result.usage.reasoning_tokens}）"
                )
                if empty_retry_done:
                    break
                empty_retry_done = True
                # 推理把预算吃光时，继续加 max_tokens 只会再烧一次（实测一次 16k 全空）。
                # 正确做法是关掉思考重试，而不是加倍预算。
                if thinking == "enabled":
                    thinking = "disabled"
                    self._log(f"[llm] {tag or 'chat'} content 为空，重试时关闭 thinking")
                budget_tokens = min(MAX_TOKENS_CAP, max(budget_tokens, 8192))
                self._backoff(attempt, attempts_allowed, last_error, tag)
                continue
            if json_mode and result.json_obj is None:
                last_error = "JSON 解析失败（模型未按 JSON 输出）"
                # 带纠正消息重试：只重发相同提示往往还会错，白烧一次
                attempt_messages = list(messages) + [
                    {"role": "assistant", "content": result.content[:2000]},
                    {
                        "role": "user",
                        "content": "上一次输出不是合法 JSON。只输出一个 JSON 对象："
                                   "不要 markdown 围栏、不要解释、不要前后缀。",
                    },
                ]
                self._backoff(attempt, attempts_allowed, last_error, tag, content=result.content)
                continue
            if self.verbose:
                print(
                    f"[llm] {tag or 'chat'} ok attempt={attempt} {elapsed_ms:.0f}ms "
                    f"tokens={result.usage.total_tokens}(reasoning={result.usage.reasoning_tokens})"
                )
            return result
        self.failures += 1
        raise LlmError(
            f"LLM 调用失败（重试 {attempts_allowed - 1} 次后放弃）：{last_error}",
            attempts=attempts_allowed,
        )

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _handle_config_error(self, exc: LlmConfigError, *, json_mode: bool, tag: str) -> bool:
        """把"可自愈"的 4xx 处理掉并返回 True（调用方立即重试）。

        - 400 + 用了 response_format：服务端不支持 JSON mode → 关掉该能力重试。
        - 401 + 当前用的是环境变量 key：回退 `File/key.txt`（仅一次）。
        """
        status = int(getattr(exc, "status", 0) or 0)
        if status == 400 and self._thinking_supported and "thinking" in str(exc).lower():
            self._thinking_supported = False
            self._log(f"[llm] {tag or 'chat'} 服务端不支持 thinking 参数，后续不再发送")
            return True
        if status == 400 and json_mode and self._json_mode_supported:
            self._json_mode_supported = False
            self._log(f"[llm] {tag or 'chat'} 服务端不支持 response_format，退回围栏/配平提取")
            return True
        if status == 401 and not self._file_fallback_done:
            self._file_fallback_done = True
            file_key = read_key_file(self._key_file)
            if file_key and file_key != self.config.api_key:
                self.config = self.config.with_key(file_key, "file")
                self._log(
                    f"[llm] {tag or 'chat'} 环境变量 key 401，改用 File/key.txt（key={self.config.masked_key}）"
                )
                return True
        return False

    def _accumulate(self, result: ChatResult) -> None:
        """每次尝试都累计（失败的重试同样计费，账必须对上）。"""
        self.usage.add(result.usage)
        self.history.append(result.to_dict())

    def _record_attempt(
        self,
        stage: str,
        problem_id: Optional[int],
        attempt: int,
        tag: str,
        ok: bool,
        error_kind: str,
        usage: ChatUsage,
        elapsed_ms: float,
    ) -> None:
        """把单次 HTTP 尝试交给注入的 recorder（落库）；未注入则忽略。"""
        if self.recorder is None:
            return
        try:
            self.recorder(
                {
                    "problem_id": problem_id,
                    "stage": stage or (tag.split("#")[0] if tag else ""),
                    "attempt": int(attempt),
                    "ok": 1 if ok else 0,
                    "error_kind": error_kind,
                    "prompt_tokens": int(usage.prompt_tokens),
                    "completion_tokens": int(usage.completion_tokens),
                    "reasoning_tokens": int(usage.reasoning_tokens),
                    "cache_hit_tokens": int(usage.cache_hit_tokens),
                    "total_tokens": int(usage.total_tokens),
                    "elapsed_ms": float(elapsed_ms),
                    "tag": tag,
                }
            )
        except Exception:  # noqa: BLE001 - 记账失败不能影响主流程
            pass

    def _backoff(
        self, attempt: int, allowed: int, reason: str, tag: str, content: str = ""
    ) -> None:
        if attempt >= allowed:
            if content:
                print(f"[llm] {tag or 'chat'} JSON 解析失败，首 200 字：{content[:200]}")
            return
        delay = BACKOFF_BASE_S * (2 ** (attempt - 1))
        self._log(f"[llm] {tag or 'chat'} 第 {attempt} 次失败（{reason}），{delay:.0f}s 后重试")
        self._sleep(delay)

    def _post(self, payload: Mapping[str, Any], *, timeout_s: int) -> Dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.config.chat_url,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.config.api_key}",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001 - 读错误体失败不影响分类
                detail = "<no body>"
            message = f"HTTP {exc.code}（key={self.config.masked_key}）：{detail}"
            if exc.code in (400, 401, 403, 404):
                raise LlmConfigError(message, status=int(exc.code)) from exc
            raise LlmError(message) from exc
        except urllib.error.URLError as exc:
            raise LlmError(f"网络错误：{exc.reason}") from exc
        except TimeoutError as exc:
            raise LlmError(f"超时（{timeout_s}s）") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LlmError(f"响应不是 JSON：{raw[:200]}") from exc

    def _build_result(
        self, body: Mapping[str, Any], *, elapsed_ms: float, attempt: int, json_mode_used: bool
    ) -> ChatResult:
        choices = body.get("choices") or []
        content = ""
        finish_reason = ""
        if choices:
            first = choices[0] or {}
            message = first.get("message") or {}
            content = str(message.get("content") or "")
            finish_reason = str(first.get("finish_reason") or "")
        usage_raw = body.get("usage") or {}
        usage = ChatUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
            total_tokens=int(usage_raw.get("total_tokens") or 0),
            reasoning_tokens=_reasoning_tokens(usage_raw),
            cache_hit_tokens=_cache_hit_tokens(usage_raw),
        )
        return ChatResult(
            content=content,
            usage=usage,
            attempts=attempt,
            elapsed_ms=elapsed_ms,
            model=str(body.get("model") or self.config.model),
            json_obj=extract_json_object(content),
            finish_reason=finish_reason,
            json_mode_used=json_mode_used,
        )

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)


def _reasoning_tokens(usage_raw: Mapping[str, Any]) -> int:
    """reasoning token 可能的三种落点，取到即用（不同版本字段名不一致）。"""
    direct = usage_raw.get("reasoning_tokens")
    if isinstance(direct, int):
        return direct
    details = usage_raw.get("completion_tokens_details")
    if isinstance(details, Mapping):
        value = details.get("reasoning_tokens")
        if isinstance(value, int):
            return value
    return 0


def _cache_hit_tokens(usage_raw: Mapping[str, Any]) -> int:
    """前缀缓存命中数（DeepSeek 自动缓存；命中部分输入按折扣计费）。"""
    for key in ("prompt_cache_hit_tokens", "cache_hit_tokens"):
        value = usage_raw.get(key)
        if isinstance(value, int):
            return value
    details = usage_raw.get("prompt_tokens_details")
    if isinstance(details, Mapping):
        value = details.get("cached_tokens")
        if isinstance(value, int):
            return value
    return 0
