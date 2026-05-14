"""自治修复 Worker。

核心职责：
- 根据 enable_cross_validation 标志选择执行路径，避免 UI 卡死：
  * enable_cross_validation=False（默认）：执行 solve_with_agent 单 Agent 自治修复。
  * enable_cross_validation=True：直接执行 verify_with_cross_agent 双算法对拍验证，
    不再事先调用 solve_with_agent，两条路径互斥独立。
- 通过 Signal 实时通知 UI 进度、代码更新、完成状态。

不负责：
- 不实现 LLM 调用细节（由 llm_caller / llm_caller_factory 注入）。
- 不管理会话持久化。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from PySide6.QtCore import QThread, Signal

from services.isolated_agent.facade import solve_with_agent, verify_with_cross_agent


class AutonomousFixWorker(QThread):
    progress = Signal(str, int, int)
    step = Signal(str, dict)
    code_update = Signal(str)
    done = Signal(dict)
    err = Signal(str)

    def __init__(
        self,
        *,
        problem: Dict[str, Any],
        candidate_code: str,
        llm_caller: Callable[[list[dict[str, str]]], Any],
        llm_caller_factory: Optional[Callable[[str], Callable[[list[dict[str, str]]], Any]]] = None,
        runtime_context: Optional[Dict[str, Any]] = None,
        enable_cross_validation: bool = False,
        max_attempts: int = 15,
        context_rounds: int = 15,
        session_id: str = "autonomous-fix-session",
        all_test_cases: Optional[list[dict]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self._problem = dict(problem or {})
        self._candidate_code = str(candidate_code or "")
        self._llm_caller = llm_caller
        self._llm_caller_factory = llm_caller_factory
        self._runtime_context = dict(runtime_context or {})
        self._enable_cross_validation = bool(enable_cross_validation)
        self._max_attempts = int(max_attempts or 15)
        self._context_rounds = int(context_rounds or 15)
        self._session_id = str(session_id or "autonomous-fix-session").strip() or "autonomous-fix-session"
        self._all_test_cases = list(all_test_cases or [])

    def run(self):
        try:
            # 双算法验证模式：直接调用 verify_with_cross_agent，不经过 solve_with_agent
            if self._enable_cross_validation:
                result = self._run_cross_validation_only()
            else:
                result = solve_with_agent(
                    problem=self._problem,
                    candidate_code=self._candidate_code,
                    llm_caller=self._llm_caller,
                    runtime_context=self._runtime_context,
                    max_attempts=self._max_attempts,
                    context_rounds=self._context_rounds,
                    session_id=self._session_id,
                    on_step_callback=self._on_step,
                    should_stop=self.isInterruptionRequested,
                )
            if self.isInterruptionRequested():
                return
            self.done.emit(dict(result or {}))
        except Exception as e:
            if self.isInterruptionRequested():
                return
            self.err.emit(str(e))

    def _on_step(self, step_info: Dict[str, Any]) -> None:
        if self.isInterruptionRequested():
            return
        action = str((step_info or {}).get("action") or "")
        attempt = int((step_info or {}).get("attempt") or 0)
        max_attempts = int((step_info or {}).get("max_attempts") or 0)
        code = str((step_info or {}).get("code") or "")
        result_type = str((step_info or {}).get("type") or "")

        stage_desc = self._build_stage_desc(action, result_type)
        self.progress.emit(stage_desc, attempt, max_attempts)
        self.step.emit(action, dict(step_info or {}))

        if action == "modify" and code:
            self.code_update.emit(code)

    def _build_stage_desc(self, action: str, result_type: str) -> str:
        action_names = {
            "read": "读取代码/题目",
            "run": "运行测试",
            "modify": "修改代码",
            "self_test": "自测验证",
            "finalize": "提交结果",
            "invalid": "解析动作",
        }
        type_names = {
            "right": "通过",
            "wrong": "错误",
            "runtime_error": "运行异常",
            "protocol_error": "协议错误",
            "finalize_ok": "完成",
        }
        action_text = action_names.get(action, action)
        type_text = type_names.get(result_type, result_type)
        if result_type and result_type not in ("right", "finalize_ok"):
            return f"{action_text} → {type_text}"
        return action_text

    def _run_cross_validation_only(self) -> Dict[str, Any]:
        """双算法验证独立路径：直接调用 verify_with_cross_agent，不经过 solve_with_agent。"""
        if not callable(self._llm_caller_factory):
            return {
                "passed": False,
                "final_code": self._candidate_code,
                "attempts": 0,
                "summary": {
                    "cross_validation": {
                        "enabled": True,
                        "verification_passed": False,
                        "skip_reason": "llm_caller_factory_missing",
                    }
                },
            }

        self.progress.emit("双算法对拍验证", 0, self._max_attempts)

        test_cases = self._collect_test_cases()

        def _on_verify_progress(payload: Dict[str, Any]) -> None:
            if self.isInterruptionRequested():
                return
            stage = str((payload or {}).get("stage") or "")
            message = str((payload or {}).get("message") or "").strip()
            if not message:
                return
            if stage == "agent_step":
                self.progress.emit(f"{message}", self._max_attempts, self._max_attempts)
                step_payload = dict(payload or {})
                step_payload.setdefault("type", step_payload.get("result_type", ""))
                self.step.emit(f"Agent-{step_payload.get('slot', '').upper()}/{step_payload.get('action', 'verify')}", step_payload)
            elif stage in ("retry_start", "compare_progress"):
                self.progress.emit(f"对拍: {message}", self._max_attempts, self._max_attempts)
            else:
                self.progress.emit(f"双算法验证: {message}", self._max_attempts, self._max_attempts)

        verdict = verify_with_cross_agent(
            problem=dict(self._problem or {}),
            candidate_code=self._candidate_code,
            test_cases=test_cases,
            llm_caller_factory=self._llm_caller_factory,
            enable_cross_validation=True,
            on_progress=_on_verify_progress,
        )

        verdict_dict = verdict.to_dict()
        final_code = str(verdict_dict.get("final_code") or self._candidate_code)
        cross_summary = dict(verdict_dict.get("summary") or {})
        cross_reason = str(cross_summary.get("reason") or verdict_dict.get("skip_reason") or "")

        return {
            "passed": bool(verdict_dict.get("verification_passed")),
            "final_code": final_code,
            "attempts": 0,
            "summary": {
                "reason": cross_reason or "cross_validation_degraded",
                "cross_validation": {
                    "enabled": True,
                    "verification_passed": bool(verdict_dict.get("verification_passed")),
                    "strategy_used": str(verdict_dict.get("strategy_used") or ""),
                    "skip_reason": str(verdict_dict.get("skip_reason") or ""),
                    "mismatch_cases": list(verdict_dict.get("mismatch_cases") or []),
                    "summary": dict(verdict_dict.get("summary") or {}),
                }
            },
        }

    def _collect_test_cases(self) -> list[dict]:
        # 优先使用外部传入的全量测试用例（problem_set.cases）
        if self._all_test_cases:
            return [dict(item) for item in self._all_test_cases if isinstance(item, dict)]

        # 回退：从 runtime_context 的 data_samples 取（通常仅 1 个样例）
        runtime_cases = self._runtime_context.get("data_samples")
        if isinstance(runtime_cases, list):
            return [dict(item) for item in runtime_cases if isinstance(item, dict)]

        # 再回退：从 problem 的 data_samples 取
        problem_cases = (self._problem or {}).get("data_samples")
        if isinstance(problem_cases, list):
            return [dict(item) for item in problem_cases if isinstance(item, dict)]

        return []
