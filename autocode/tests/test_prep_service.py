"""预生成流水线测试：预算熔断、阶段失败落库、spec 校验、提示质检、多实现投票。

用一个"求和"小题 + 桩 LLM 跑通全链路（真起进程、真编译），所以既覆盖编排也覆盖门禁。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from autocode.app import judge
from autocode.app.prep_service import (
    Budget,
    BudgetExhausted,
    PrepService,
    STAGE_SPEC,
)
from autocode.llm.client import ChatResult, ChatUsage
from autocode.store.db import Database

PROBLEM_MD = """## 求和（sum）

### 【题目描述】

给定 n 个数，输出它们的和。

### 【输入格式】

第一行 n，第二行 n 个整数。

### 【样例输入】

3
1 2 3

### 【样例输出】

6
"""

SPEC = {
    "input_grammar": [
        {"kind": "scalar", "name": "n", "type": "int"},
        {"kind": "repeat", "count": "n", "sep": "space", "elem": {"type": "int", "name": "a"}},
    ],
    "constraints_exec": ["1 <= n <= 1000"],
    "output_format": "一个整数",
    "output_type": "single_value",
    "checker_type": "exact",
    "float_eps": 1e-6,
    "time_limit_ms": {"c": 1000, "cpp": 1000, "python": 4000},
    "memory_limit_mb": 256,
    "traps": ["n=1"],
    "tier": "A",
}

GENERATOR = '''```python
import random, sys
seed = int(sys.argv[1]); profile = sys.argv[2]
rng = random.Random(seed)
if profile == "tiny":
    n = rng.randint(1, 6)
elif profile == "edge":
    n = 1
elif profile == "max":
    n = 1000
else:
    n = 10
print(n)
print(" ".join(str(rng.randint(-100, 100)) for _ in range(n)))
```'''

REF_PY = '''```python
import sys
d = sys.stdin.read().split()
n = int(d[0])
print(sum(int(x) for x in d[1:1 + n]))
```'''

REF_CPP = '''```cpp
#include <cstdio>
int main(){int n;if(scanf("%d",&n)!=1)return 0;long long s=0,v;for(int i=0;i<n;i++){scanf("%lld",&v);s+=v;}printf("%lld\\n",s);return 0;}
```'''

BRUTE_PY = '''```python
import sys
d = sys.stdin.read().split()
n = int(d[0])
a = [int(x) for x in d[1:1 + n]]
s = 0
for x in a:
    s = s + x
print(s)
```'''

HINTS = {
    "L1": "前缀和 / 直接累加。",
    "L2": "一遍扫描累加即可，复杂度 O(n)。",
    "L3": "1) 读 n；2) 读 n 个数；3) 累加；4) 输出。",
}


class StubClient:
    """按 tag 前缀返回预置内容；可注入 spec 覆盖以测试门禁。"""

    def __init__(self, *, spec: Dict[str, Any] | None = None, hints: Dict[str, str] | None = None) -> None:
        self.calls = 0
        self.spec = spec if spec is not None else SPEC
        self.hints = hints if hints is not None else HINTS
        self.usage = ChatUsage()

    def chat(self, messages: List[Dict[str, str]], *, json_mode: bool = True, tag: str = "", **_kw: Any) -> ChatResult:
        self.calls += 1
        if tag.startswith("spec"):
            payload: Any = self.spec
        elif tag.startswith("generator"):
            payload = GENERATOR
        elif tag.startswith("brute"):
            payload = BRUTE_PY
        elif tag.startswith("reference"):
            payload = REF_CPP if "lang = cpp" in messages[1]["content"] else REF_PY
        elif tag.startswith("hints"):
            payload = self.hints
        else:
            raise AssertionError(f"未知 tag {tag}")
        content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        obj = payload if isinstance(payload, dict) else None
        usage = ChatUsage(100, 20, 120)
        self.usage.prompt_tokens += 100
        self.usage.completion_tokens += 20
        self.usage.total_tokens += 120
        return ChatResult(content=content, usage=usage, attempts=1, elapsed_ms=1.0,
                          model="stub", json_obj=obj, finish_reason="stop")


class PipelineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmp.name) / "t.db")
        self.db.init_schema()
        self.db.execute(
            "INSERT INTO problems(title, source, source_id, statement_md, status, created_at, updated_at)"
            " VALUES('求和', 'test', 'T-sum', ?, 'draft', 'now', 'now')",
            (PROBLEM_MD,),
        )
        self.problem_id = int(self.db.query_scalar("SELECT id FROM problems"))
        self.db.execute(
            "UPDATE problems SET spec_json = ? WHERE id = ?",
            (json.dumps({"samples": [{"input": "3\n1 2 3\n", "output": "6\n"}]}), self.problem_id),
        )
        self.db.execute(
            "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
            " expected_output, source_of_truth, trust_level) VALUES(?, 'sample', 1, ?, ?, ?, 'sample', 'oracle')",
            (self.problem_id, "3\n1 2 3\n", "hash1", "6\n"),
        )

    def tearDown(self) -> None:
        self.db.close()
        self._tmp.cleanup()

    def build(self, client: Any, **kwargs: Any) -> PrepService:
        params = dict(cross_cases=2, exhaustive_cases=2, stress_cases=1, boundary_cases=2)
        params.update(kwargs)
        service = PrepService(db=self.db, client=client, **params)
        # 单元测试用固定 tag 的桩客户端，不覆盖 parser 兜底路径（另有 test_input_parser 专测）
        service.allow_parser_fallback = False
        service.enable_llm_extras = False
        return service


class TestHappyPath(PipelineTestCase):
    def test_full_pipeline(self) -> None:
        report = self.build(StubClient()).prepare(self.problem_id)
        self.assertTrue(report.ok, report.error)
        self.assertEqual(report.failed_stage, "")
        self.assertTrue(report.generator_ok)
        self.assertTrue(all(ref.verified == 1 for ref in report.refs))
        self.assertIn("sample", report.layers)
        self.assertGreater(len(report.hints), 0)
        self.assertEqual(
            str(self.db.query_scalar("SELECT status FROM problems WHERE id = ?", (self.problem_id,))),
            "prepared",
        )
        stage_rows = self.db.query_all(
            "SELECT stage FROM problem_stage_runs WHERE problem_id = ?", (self.problem_id,)
        )
        self.assertGreaterEqual(len(stage_rows), 4)
        levels = [
            int(row["level"])
            for row in self.db.query_all(
                "SELECT level FROM hints WHERE problem_id = ? ORDER BY level", (self.problem_id,)
            )
        ]
        self.assertIn(4, levels)

    def test_rerun_does_not_grow_case_rows(self) -> None:
        service = self.build(StubClient())
        service.prepare(self.problem_id)
        before = int(self.db.query_scalar("SELECT COUNT(*) FROM test_cases WHERE problem_id = ?", (self.problem_id,)))
        self.build(StubClient()).prepare(self.problem_id)
        after = int(self.db.query_scalar("SELECT COUNT(*) FROM test_cases WHERE problem_id = ?", (self.problem_id,)))
        self.assertEqual(before, after)


class TestBudget(PipelineTestCase):
    def test_exhausted_before_any_call(self) -> None:
        client = StubClient()
        report = self.build(client, max_calls=0).prepare(self.problem_id)
        self.assertFalse(report.ok)
        self.assertEqual(report.failed_stage, "budget")
        self.assertEqual(client.calls, 0)
        self.assertEqual(
            str(self.db.query_scalar("SELECT failed_stage FROM problems WHERE id = ?", (self.problem_id,))),
            "budget",
        )

    def test_budget_counts_calls(self) -> None:
        report = self.build(StubClient(), max_calls=3).prepare(self.problem_id)
        self.assertFalse(report.ok)
        self.assertEqual(report.failed_stage, "budget")
        self.assertLessEqual(report.llm_calls, 3)

    def test_budget_dataclass(self) -> None:
        budget = Budget(max_calls=1)
        budget.record(ChatUsage(5, 5, 10))
        self.assertEqual(budget.calls, 1)
        self.assertEqual(budget.remaining, 0)
        with self.assertRaises(BudgetExhausted):
            budget.ensure()


class TestStageFailure(PipelineTestCase):
    def test_bad_spec_fails_and_records(self) -> None:
        bad = dict(SPEC)
        bad["input_grammar"] = [{"kind": "graph", "name": "g"}]
        report = self.build(StubClient(spec=bad)).prepare(self.problem_id)
        self.assertFalse(report.ok)
        self.assertEqual(report.failed_stage, STAGE_SPEC)
        row = self.db.query_one(
            "SELECT error_text FROM problem_stage_runs WHERE problem_id = ? AND stage = 'spec'"
            " ORDER BY id DESC LIMIT 1",
            (self.problem_id,),
        )
        self.assertTrue(row["error_text"])
        self.assertEqual(
            str(self.db.query_scalar("SELECT status FROM problems WHERE id = ?", (self.problem_id,))),
            "failed",
        )

    def test_spec_grammar_must_parse_samples(self) -> None:
        """grammar 写成 1 个 scalar 时，样例回放会失败 → spec 门拦下。"""
        bad = dict(SPEC)
        bad["input_grammar"] = [{"kind": "scalar", "name": "n", "type": "int"}]
        report = self.build(StubClient(spec=bad)).prepare(self.problem_id)
        self.assertEqual(report.failed_stage, STAGE_SPEC)
        self.assertIn("样例", report.error)


class TestSpecNormalize(PipelineTestCase):
    def test_unknown_checker_defaulted(self) -> None:
        service = self.build(StubClient())
        spec, problems = service._normalize_spec({"input_grammar": SPEC["input_grammar"],
                                                 "checker_type": "fuzzy"})
        self.assertEqual(spec["checker_type"], "exact")
        self.assertTrue(problems)

    def test_missing_grammar_rejected(self) -> None:
        service = self.build(StubClient())
        _, problems = service._normalize_spec({"output_type": "list"})
        self.assertTrue(problems)

    def test_limits_defaulted(self) -> None:
        service = self.build(StubClient())
        spec, _ = service._normalize_spec({"input_grammar": SPEC["input_grammar"]})
        self.assertEqual(spec["time_limit_ms"]["python"], 4000)


class TestVoting(PipelineTestCase):
    def test_all_agree(self) -> None:
        service = self.build(StubClient())
        result = service._agree({"a": "1\n", "b": "1", "c": "1\n"}, "exact", 1e-6)
        self.assertEqual(result["agreeing"], 3)
        self.assertTrue(result["consistent"])

    def test_disagreement_marks_inconsistent(self) -> None:
        service = self.build(StubClient())
        result = service._agree({"a": "1\n", "b": "2\n"}, "exact", 1e-6)
        self.assertFalse(result["consistent"])
        self.assertEqual(result["agreeing"], 1)

    def test_none_outputs_ignored(self) -> None:
        service = self.build(StubClient())
        result = service._agree({"a": "1", "b": None}, "exact", 1e-6)
        self.assertEqual(result["agreeing"], 1)
        self.assertTrue(result["consistent"])


class TestHintQuality(unittest.TestCase):
    def test_clean_hint(self) -> None:
        self.assertIsNone(judge.hint_quality_issue("用前缀和，复杂度 O(n)。"))

    def test_fenced_code_flagged(self) -> None:
        self.assertIn("fenced_code_block", judge.hint_quality_issue("看这个：```py\nx=1\n```"))

    def test_python_def_flagged(self) -> None:
        self.assertIn("python_def", judge.hint_quality_issue("你可以 def solve(): 然后调用"))

    def test_cpp_signature_flagged(self) -> None:
        reason = judge.hint_quality_issue("#include <vector>\nstd::vector<int> v;")
        self.assertIsNotNone(reason)
        self.assertIn("cpp_include", reason or "")


class TestExtractCode(unittest.TestCase):
    def test_fenced_preferred(self) -> None:
        text = "说明\n```cpp\nint main(){}\n```\n尾巴"
        self.assertEqual(judge.extract_code(text, "cpp"), "int main(){}")

    def test_language_match(self) -> None:
        text = "```python\nprint(1)\n```\n```cpp\nint main(){}\n```"
        self.assertIn("print(1)", judge.extract_code(text, "python"))
        self.assertIn("int main", judge.extract_code(text, "cpp"))

    def test_plain_fallback(self) -> None:
        self.assertEqual(judge.extract_code("print(1)", "python"), "print(1)")


if __name__ == "__main__":
    unittest.main()
