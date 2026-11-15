"""自治循环行为回归（守住 P0 对 run_autonomous_loop 的 150 行拆分重构）。

设计 §4「防打转」的判定顺序很容易在重构中改错，这里用脚本化 LLM 固定场景：
1. 正常路径：modify -> run -> self_test -> finalize 必须 passed=True。
2. 跑满轮次：只读动作不推进，必须以 max_attempts_reached 结束。
3. 协议错误：连续不可解析输出，必须熔断在 protocol_error_limit_reached。
4. 重复动作：同一动作连续 5 轮，必须 same_action_limit_reached。
5. should_stop：外部中断立即生效，不产生任何 step。
6. trace 组装：带 code 的 action_payload 能正常压缩（旧代码调用未定义的 _sha256_text，
   异常被 try/except 吞掉，导致该类日志静默丢失；此处守住修复）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from autocode.agent.contracts.protocol import RESULT_RIGHT
from autocode.agent.controller.autonomous_loop import (
    LoopConfig,
    _build_trace_message_item,
    run_autonomous_loop,
)
from autocode.agent.controller.dispatcher import ActionDispatcher
from autocode.agent.session.state import AgentSessionState

CODE_V1 = "def solve(problem, data):\n    return {'stdout': '1'}\n"


class _ScriptedLlm:
    """按脚本吐动作的假 LLM；脚本耗尽即失败（防止循环比预期多跑）。"""

    def __init__(self, actions: list[dict | str]) -> None:
        self._actions = list(actions)
        self.calls = 0

    def __call__(self, messages):  # noqa: ANN001 - 只需满足 LlmCaller 形状
        self.calls += 1
        if not self._actions:
            raise AssertionError("循环请求了超出脚本的轮次")
        item = self._actions.pop(0)
        return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)


def _always(result_type: str):
    def executor(code: str, state: AgentSessionState):  # noqa: ANN001
        return {"type": result_type, "code": "ok\n"}

    return executor


def _make_state(code: str = "") -> AgentSessionState:
    return AgentSessionState(session_id="loop-test", current_code=code)


class TestLoopHappyPath(unittest.TestCase):
    def test_modify_run_self_test_finalize_passes(self) -> None:
        state = _make_state()
        llm = _ScriptedLlm(
            [
                {"action": "modify", "code": CODE_V1},
                {"action": "run", "use_current": True},
                {"action": "self_test", "cases": [{"stdin": "1 2\n"}]},
                {"action": "finalize"},
            ]
        )
        dispatcher = ActionDispatcher(run_executor=_always(RESULT_RIGHT))
        result = run_autonomous_loop(
            state=state,
            problem={"title": "sum", "description": "a+b"},
            llm_caller=llm,
            dispatcher=dispatcher,
            config=LoopConfig(max_attempts=10),
        )

        self.assertTrue(result["passed"], result)
        self.assertEqual(result["attempts"], 4)
        self.assertEqual(result["summary"]["status"], "ok")
        self.assertEqual(len(result["summary"]["steps"]), 4)
        self.assertEqual([step["action"] for step in result["summary"]["steps"]],
                         ["modify", "run", "self_test", "finalize"])
        self.assertEqual(result["final_code"], CODE_V1)
        self.assertEqual(llm.calls, 4)
        self.assertEqual(state.status, "finished")


class TestLoopStopConditions(unittest.TestCase):
    def test_reaches_max_attempts_with_read_only_actions(self) -> None:
        llm = _ScriptedLlm([{"action": "read"}] * 3)
        result = run_autonomous_loop(
            state=_make_state(CODE_V1),
            problem={},
            llm_caller=llm,
            dispatcher=ActionDispatcher(run_executor=_always(RESULT_RIGHT)),
            config=LoopConfig(max_attempts=3),
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(result["summary"]["reason"], "max_attempts_reached")

    def test_protocol_error_limit(self) -> None:
        llm = _ScriptedLlm(["not a json action"] * 3)
        result = run_autonomous_loop(
            state=_make_state(),
            problem={},
            llm_caller=llm,
            dispatcher=ActionDispatcher(run_executor=_always(RESULT_RIGHT)),
            config=LoopConfig(max_attempts=10, max_protocol_errors=3),
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["summary"]["reason"], "protocol_error_limit_reached")
        self.assertEqual(result["attempts"], 3)

    def test_same_action_limit(self) -> None:
        llm = _ScriptedLlm([{"action": "modify", "code": CODE_V1}] * 5)
        result = run_autonomous_loop(
            state=_make_state(),
            problem={},
            llm_caller=llm,
            dispatcher=ActionDispatcher(run_executor=_always(RESULT_RIGHT)),
            config=LoopConfig(max_attempts=10, max_no_progress_rounds=10),
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["summary"]["reason"], "same_action_limit_reached")

    def test_should_stop_breaks_before_first_attempt(self) -> None:
        llm = _ScriptedLlm([])
        result = run_autonomous_loop(
            state=_make_state(),
            problem={},
            llm_caller=llm,
            dispatcher=ActionDispatcher(run_executor=_always(RESULT_RIGHT)),
            config=LoopConfig(max_attempts=5, should_stop=lambda: True),
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(llm.calls, 0)


class TestTraceMessageCompaction(unittest.TestCase):
    def test_action_payload_with_code_is_compacted(self) -> None:
        state = _make_state(CODE_V1)
        item = _build_trace_message_item(
            state=state,
            event="action_payload",
            payload={"attempt": 2, "payload": {"action": "modify", "code": CODE_V1}},
        )
        self.assertEqual(item["stage"], "action_payload")
        compact_code = item["details"]["payload"]["code"]
        self.assertTrue(compact_code["stored_in_code_versions"])
        self.assertTrue(str(compact_code["hash"]).startswith("sha256:"))
        self.assertEqual(compact_code["chars"], len(CODE_V1))
        self.assertEqual(compact_code["lines"], len(CODE_V1.splitlines()))


class _DuckSession:
    """只实现 push_event 的最小会话对象（M7：dispatcher 不得 isinstance 死绑）。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def push_event(self, *, action, result_type, note="", extra=None):  # noqa: ANN001
        event = {"action": action, "result_type": result_type, "note": note}
        if extra:
            event["extra"] = dict(extra)
        self.events.append(event)


class TestDispatcherDuckTyping(unittest.TestCase):
    def test_protocol_error_recorded_on_duck_typed_state(self) -> None:
        duck = _DuckSession()
        dispatcher = ActionDispatcher(run_executor=_always(RESULT_RIGHT))
        result = dispatcher.dispatch({"action": "not_an_action"}, duck)

        self.assertEqual(result["type"], "protocol_error")
        self.assertEqual(result["error_code"], "invalid_action")
        self.assertEqual(len(duck.events), 1)
        self.assertEqual(duck.events[0]["note"], "invalid_action")
        self.assertEqual(duck.events[0]["action"], "not_an_action")
        self.assertIn("protocol_message", duck.events[0]["extra"])

    def test_state_without_push_event_does_not_crash(self) -> None:
        class _Bare:
            pass

        dispatcher = ActionDispatcher(run_executor=_always(RESULT_RIGHT))
        result = dispatcher.dispatch({"action": "not_an_action"}, _Bare())  # type: ignore[arg-type]
        self.assertEqual(result["type"], "protocol_error")


if __name__ == "__main__":
    unittest.main()
