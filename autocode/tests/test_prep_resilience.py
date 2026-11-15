"""桩场景回归（机制层，不依赖真实 LLM 服务）：

1. 合法 JSON → spec 阶段一次通过
2. 坏 JSON → 重试后用合法 JSON 通过
3. 超时/网络错误 → 阶段失败并落库（错误原文可读）
4. 预算耗尽 → failed_stage=budget，且**不发生任何调用**

这四条是流水线的"防幻觉/防打转"底线，任何改动都必须保持全绿。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from autocode.app.prep_service import PrepService
from autocode.llm.client import ChatResult, ChatUsage, LlmError
from autocode.store.db import Database

PROBLEM_MD = """## 求和（sum）

### 【题目描述】

给定 n 个数，输出它们的和。

### 【样例输入】

3
1 2 3

### 【样例输出】

6
"""

GOOD_SPEC = {
    "input_grammar": [
        {"kind": "scalar", "name": "n", "type": "int"},
        {"kind": "repeat", "count": "n", "sep": "space", "elem": {"type": "int", "name": "a"}},
    ],
    "constraints_exec": ["1 <= n <= 1000"],
    "output_format": "一个整数",
    "output_type": "single_value",
    "checker_type": "exact",
    "time_limit_ms": {"c": 1000, "cpp": 1000, "python": 4000},
    "tier": "A",
}


class ScriptedClient:
    """按脚本返回：dict → JSON；None → 空 content；Exception → 抛出。"""

    def __init__(self, script: List[Any]) -> None:
        self.script = list(script)
        self.calls = 0
        self.usage = ChatUsage()

    def chat(self, messages: List[Dict[str, str]], *, json_mode: bool = True, tag: str = "", **_kw: Any) -> ChatResult:
        self.calls += 1
        if not self.script:
            raise AssertionError(f"脚本已耗尽（tag={tag}）")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        payload = item if isinstance(item, dict) else None
        content = json.dumps(payload, ensure_ascii=False) if payload is not None else ""
        usage = ChatUsage(100, 20, 120)
        self.usage.add(usage)
        return ChatResult(content=content, usage=usage, attempts=1, elapsed_ms=1.0,
                          model="scripted", json_obj=payload, finish_reason="stop")


class ResilienceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        self.db.init_schema()
        self.db.execute(
            "INSERT INTO problems(title, statement_md, status, created_at, updated_at)"
            " VALUES('求和', ?, 'draft', 'now', 'now')",
            (PROBLEM_MD,),
        )
        self.problem_id = int(self.db.query_scalar("SELECT id FROM problems"))
        self.db.execute(
            "UPDATE problems SET spec_json = ? WHERE id = ?",
            (json.dumps({"samples": [{"input": "3\n1 2 3\n", "output": "6\n"}]}), self.problem_id),
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def service(self, client: Any, **kwargs: Any) -> PrepService:
        params = dict(cross_cases=1, exhaustive_cases=1, stress_cases=1, boundary_cases=1)
        params.update(kwargs)
        return PrepService(db=self.db, client=client, **params)

    def spec_stage(self, client: Any):
        service = self.service(client)
        problem = dict(self.db.query_one("SELECT * FROM problems WHERE id = ?", (self.problem_id,)))
        samples = service.store.load_samples(self.problem_id)
        return service._stage_spec(problem, samples)


class TestValidJson(ResilienceCase):
    def test_single_call(self) -> None:
        client = ScriptedClient([dict(GOOD_SPEC)])
        spec, detail = self.spec_stage(client)
        self.assertEqual(client.calls, 1)
        self.assertIn("样例回放 1/1 通过", detail)
        self.assertEqual(spec["time_limit_ms"]["python"], 4000)


class TestBadJson(ResilienceCase):
    def test_retry_then_success(self) -> None:
        client = ScriptedClient([None, {"unexpected": True}, dict(GOOD_SPEC)])
        spec, _detail = self.spec_stage(client)
        self.assertEqual(client.calls, 3)
        self.assertEqual(spec["checker_type"], "exact")

    def test_all_bad_raises_stage_failure(self) -> None:
        client = ScriptedClient([None, None, None])
        with self.assertRaises(Exception) as ctx:
            self.spec_stage(client)
        self.assertIn("spec", str(ctx.exception))


class TestTimeout(ResilienceCase):
    def test_network_error_fails_stage_and_records(self) -> None:
        client = ScriptedClient([LlmError("超时（180s）")])
        report = self.service(client).prepare(self.problem_id)
        self.assertFalse(report.ok)
        self.assertEqual(report.failed_stage, "spec")
        row = self.db.query_one(
            "SELECT error_text FROM problem_stage_runs WHERE problem_id = ? AND stage = 'spec'"
            " ORDER BY id DESC LIMIT 1",
            (self.problem_id,),
        )
        self.assertIn("超时", row["error_text"] or "")
        self.assertEqual(
            str(self.db.query_scalar("SELECT failed_stage FROM problems WHERE id = ?", (self.problem_id,))),
            "spec",
        )


class TestBudgetExhausted(ResilienceCase):
    def test_no_call_happens(self) -> None:
        client = ScriptedClient([dict(GOOD_SPEC)] * 5)
        report = self.service(client, max_calls=0).prepare(self.problem_id)
        self.assertEqual(report.failed_stage, "budget")
        self.assertEqual(client.calls, 0)
        self.assertEqual(report.budget["calls"], 0)

    def test_budget_stops_midway(self) -> None:
        client = ScriptedClient([dict(GOOD_SPEC)] * 10)
        report = self.service(client, max_calls=1).prepare(self.problem_id)
        self.assertEqual(report.failed_stage, "budget")
        self.assertEqual(report.llm_calls, 1)


if __name__ == "__main__":
    unittest.main()
