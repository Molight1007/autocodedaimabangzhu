"""LLM 调用的公共骨架：绑题 + 记账 + 阶段策略（温度/思考）+ prompt 版本指纹。

为什么要单独成文件：`prep_service.py` 只放编排，受 R4 行数门禁（≤800 行）约束；
把"一次 chat 怎么发、怎么记账、怎么按 stage 取策略"拆出来，
`PrepService` 与补语言/补解析共用同一套实现（历史上两套实现导致成本漏记）。

本文件不含阶段编排逻辑，只有：
- `use_problem()`：绑定 problem_id + 装配 `client.recorder`；
- `_chat()`：一次调用（含重试计费、deadline/budget 熔断、温度策略、prompt_hash）；
- `_account()`：次数/token 记账；`_note()/_log()`：过程记录。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Optional

from ..prompts import loader as prompts
from .prep_models import (
    DEFAULT_TEMPERATURE,
    TEMPERATURE_POLICY,
    THINKING_POLICY,
    BudgetExhausted,
    StageFailure,
    stage_key,
)


class LlmStageMixin:
    """要求宿主类提供：`store` / `client` / `budget` / `verbose` / 预算与 deadline 字段。"""

    # 宿主在 __init__ 里赋值的属性（此处只做类型提示用的声明）
    store: Any
    client: Any
    budget: Any
    verbose: bool
    deadline_ts: Any
    call_cap: int
    token_cap: int
    _calls_this_run: int
    _tokens_this_run: int
    _problem_id: Any
    _notes: Any

    def use_problem(self, problem_id: int) -> None:
        """绑定题目并把 `client.recorder` 装好。

        历史 bug：只有在 `prepare()` 里才设置 `_problem_id` / `recorder`，而
        `add_language` / `enrich_problem` 走的是另外新建的 PrepService，导致
        method_analysis / code_notes 的调用**完全不落库**（成本审计缺一段）。
        现在任何入口先 `use_problem(pid)`，记账口径与 prepare 一致。
        """
        self._problem_id = int(problem_id)
        if self.client is None:
            from ..llm.client import LlmClient

            self.client = LlmClient(verbose=self.verbose)
        # 每次 HTTP 尝试都落库（含失败/重试），否则失败重试的 token 无法审计
        self.client.recorder = self.store.record_llm_call

    def _account(self, spent: int, before_tokens: int, usage: Any = None) -> None:
        """记一次调用（含失败重试）：次数进预算，token 按客户端累计差值算。"""
        used = max(0, int(getattr(self.client.usage, "total_tokens", 0)) - before_tokens)
        calls = max(1, int(spent))
        self.budget.record(usage, calls=calls)
        self._calls_this_run += calls
        self._tokens_this_run += used
        if self._tokens_this_run > self.token_cap:
            raise BudgetExhausted(
                f"本题 token 超上限（{self._tokens_this_run}/{self.token_cap}）"
            )

    def _chat(self, template_name: str, user_content: str, *, tag: str,
              max_tokens: int = 4096, json_mode: bool = True,
              temperature: Optional[float] = None) -> Any:
        """一次 LLM 调用（json_mode=False 用于要代码的阶段；重试同样计费，失败也记）。

        `temperature=None` 时按 `stage_key(tag)` 查 `TEMPERATURE_POLICY`；
        显式传值（如参考解重试时升温）优先，不被策略覆盖。
        """
        if self.deadline_ts is not None and time.monotonic() > self.deadline_ts:
            raise StageFailure("timeout", f"单题超过时限（deadline 已到，tag={tag}）")
        if self._calls_this_run >= self.call_cap or self._tokens_this_run >= self.token_cap:
            raise BudgetExhausted(
                f"本题预算已到（calls {self._calls_this_run}/{self.call_cap}，"
                f"tokens {self._tokens_this_run}/{self.token_cap}）"
            )
        self.budget.ensure()
        system_text = prompts.load(template_name)
        # prompt 版本可追溯：模板原文的短 hash 随 llm_calls 落库，改词后能对上账
        prompt_hash = hashlib.sha256(system_text.encode("utf-8")).hexdigest()[:16]
        messages = [{"role": "system", "content": system_text},
                    {"role": "user", "content": user_content}]
        stage = stage_key(tag)
        if temperature is None:
            temperature = TEMPERATURE_POLICY.get(stage, DEFAULT_TEMPERATURE)
        thinking, thinking_budget = THINKING_POLICY.get(stage, ("default", 0))
        before_tokens = int(getattr(self.client.usage, "total_tokens", 0))
        # 整题墙钟预算必须**穿透到单次 HTTP**：旧实现只在调用前查 deadline，
        # 而 urlopen(timeout=) 只管两次 recv 的间隔，模型持续吐字时不会触发，
        # 实测出现过单次 702s 把 180s 整题预算击穿 4 倍。单次另设 120s 上限。
        wall_clock_s: Optional[float] = None
        if self.deadline_ts is not None:
            remaining = float(self.deadline_ts) - time.monotonic()
            wall_clock_s = max(5.0, min(120.0, remaining))
        try:
            result = self.client.chat(
                messages,
                json_mode=json_mode,
                tag=tag,
                max_tokens=max_tokens,
                temperature=temperature,
                stage=stage,
                problem_id=self._problem_id,
                thinking=thinking,
                thinking_budget=thinking_budget,
                prompt_hash=prompt_hash,
                deadline_ts=self.deadline_ts,
                wall_clock_s=wall_clock_s,
            )
        except Exception as exc:  # 失败也要记账：重试同样花钱
            self._account(int(getattr(exc, "attempts", 1) or 1), before_tokens)
            raise
        self._account(int(result.attempts), before_tokens, result.usage)
        return result

    def _note(self, message: str) -> None:
        self._notes.append(message); self._log(f"[note] {message}")

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)
