"""预生成流水线测试：预算熔断、阶段失败落库、spec 校验、提示质检、多实现投票。

用一个"求和"小题 + 桩 LLM 跑通全链路（真起进程、真编译），所以既覆盖编排也覆盖门禁。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

from autocode.app import judge
from autocode.app.prep_models import ARTIFACT_VERSION
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

def _method_names(user_text: str) -> List[str]:
    """从“已有解法”段里取出方法名，供桩客户端按解法各复制一组提示。"""
    match = re.search(r"## 已有解法.*?\n(\[.*?\])\n", user_text, re.S)
    if not match:
        return []
    try:
        items = json.loads(match.group(1))
    except json.JSONDecodeError:
        return []
    return [str(item.get("name") or "") for item in items if isinstance(item, dict) and item.get("name")]


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
        self.last_messages = messages
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
            if "approaches" not in payload:
                names = _method_names(messages[1]["content"])
                if names:
                    payload = {"root": {"L1": self.hints["L1"]},
                           "approaches": [{"name": name, "L2": self.hints["L2"], "L3": self.hints["L3"]}
                                          for name in names]}
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


class TestHintsInputs(PipelineTestCase):
    """S2：hints.md 承诺“会收到已验证的解法清单”，代码必须真的传进去。"""

    def test_hints_receives_verified_approaches(self) -> None:
        from autocode.app.prep_models import RefArtifact

        service = self.build(StubClient())
        spec = {
            "checker_type": "exact",
            "samples": [{"input": "3\n1 2 3\n", "output": "6\n"}],
            "constraints_exec": ["1 <= n <= 10"],
        }
        refs = {
            "python/reference": RefArtifact(
                lang="python", role="reference", code="print(6)", verified=1,
                approach="前缀和", complexity="O(n)",
            ),
            "python/brute": RefArtifact(
                lang="python", role="brute", code="print(6)", verified=1,
                approach="暴力枚举", complexity="O(n^2)",
            ),
        }
        from autocode.app.hint_service import run_hints_stage

        run_hints_stage(service, {"id": self.problem_id, "statement_md": PROBLEM_MD}, spec, refs)
        content = service.client.last_messages[1]["content"]
        self.assertIn("已有解法", content, "L2 必须知道真实用的是哪种算法")
        self.assertIn("前缀和", content, content[:400])
        self.assertIn("参考解已通过全部题面样例", content)
        # 每种解法各出一组；暴力解也要列出来（学生可以主动选它），但不能混进主解那组
        self.assertIn("暴力枚举", content, "按解法分组时暴力解也应作为独立一组")
        self.assertIn("逐条各出一组提示", content)
        rows = service.store.db.query_all(
            "SELECT DISTINCT approach FROM hints WHERE problem_id=? AND is_active=1"
            " AND approach<>'' ORDER BY approach",
            (self.problem_id,),
        )
        self.assertEqual([r["approach"] for r in rows], ["前缀和", "暴力枚举"])
        levels = [int(r["level"]) for r in service.store.db.query_all(
            "SELECT level FROM hints WHERE problem_id=? AND is_active=1", (self.problem_id,)
        )]
        self.assertIn(0, levels, "v4 必须落一条根节点 L1")
        self.assertNotIn(1, levels, "v4 不再按解法重复 L1")


class TestAnySolutionFastFail(PipelineTestCase):
    """G5：构造题必须第一次就停，不能烧 3 次调用才失败。"""

    def test_construction_problem_stops_after_one_call(self) -> None:
        bad_spec = dict(SPEC)
        bad_spec["output_type"] = "any_solution"
        client = StubClient(spec=bad_spec)
        report = self.build(client).prepare(self.problem_id)

        self.assertFalse(report.ok)
        self.assertEqual(report.failed_stage, "spec")
        self.assertIn("暂不支持", report.error)
        self.assertEqual(client.calls, 1, f"构造题不应重试，实际调用 {client.calls} 次")


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

    def test_any_solution_rejected_fast(self) -> None:
        """判题器没有自定义 checker：构造题必须快速失败转人工，不能静默当 list 判。"""
        service = self.build(StubClient())
        spec, problems = service._normalize_spec({
            "input_grammar": SPEC["input_grammar"],
            "output_type": "any_solution",
        })
        self.assertEqual(spec["output_type"], "any_solution", "如实选的要保留，不被偷偷改写")
        self.assertTrue(problems, "any_solution 必须被拒")
        self.assertTrue(
            any("暂不支持" in item and "人工处理" in item for item in problems),
            f"拒绝理由要明确指向'暂不支持 + 人工处理'，实际 {problems}",
        )

    def test_any_solution_skips_parser_fallback(self) -> None:
        """兜底会整份复用 output_type：必须在这里也堵住，否则构造题被"洗白"继续生成。"""
        from autocode.app.spec_fallback import parser_fallback

        service = self.build(StubClient())
        service.allow_parser_fallback = True  # 必须真进入兜底分支，否则测的是“已关闭”
        base = {"input_grammar": SPEC["input_grammar"], "output_type": "any_solution"}
        spec, detail = parser_fallback(
            service, {"id": self.problem_id, "statement_md": PROBLEM_MD}, base,
            [{"input": "3\n1 2 3\n", "output": "6\n"}], "prior",
        )
        self.assertIsNone(spec, "不得返回兜底 spec")
        self.assertIn("暂不支持", detail)
        self.assertEqual(service.client.calls, 0, "不该真的去调 parser LLM（白花钱）")

    def test_any_solution_fallback_is_case_insensitive(self) -> None:
        """A1：`Any_Solution` 这类大小写变体不能绕过兜底拦截。"""
        from autocode.app.spec_fallback import parser_fallback

        service = self.build(StubClient())
        service.allow_parser_fallback = True
        base = {"input_grammar": SPEC["input_grammar"], "output_type": "Any_Solution"}
        spec, detail = parser_fallback(
            service, {"id": self.problem_id, "statement_md": PROBLEM_MD}, base,
            [{"input": "3\n1 2 3\n", "output": "6\n"}], "prior",
        )
        self.assertIsNone(spec)
        self.assertIn("暂不支持", detail)
        self.assertEqual(service.client.calls, 0)


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

    def test_bracket_assignment_flagged(self) -> None:
        """G4：模板承诺检测“方括号赋值”，代码必须真能命中。"""
        self.assertIn("index_assign", judge.hint_quality_issue("把 a[i]=1 记下来") or "")
        self.assertIn("index_assign", judge.hint_quality_issue("dp[j] += 1") or "")

    def test_def_followed_by_newline_flagged(self) -> None:
        """`def` 后换行（没有尾随空格）也必须命中，旧实现只匹配 "def "。"""
        self.assertIn("python_def", judge.hint_quality_issue("先写\ndef\n再调用") or "")

    def test_def_with_chinese_neighbor_flagged(self) -> None:
        """`\\b` 在 Unicode 下把中文当 word，`写def定义函数` 会漏检；ASCII 边界必须命中。"""
        self.assertIn("python_def", judge.hint_quality_issue("写def定义函数") or "")

    def test_complexity_arrow_not_flagged(self) -> None:
        """`O(n)->O(1)` 是复杂度写法，不是成员访问，不能误报。"""
        self.assertIsNone(judge.hint_quality_issue("优化后复杂度 O(n)->O(1)"))


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


class TestArtifactVersionAndRollback(PipelineTestCase):
    """旧版/新版的差距必须可判定：产物版本 + 失败回滚。

    守两件事：
    1. `status='prepared'` 不等于「本版产物」。低版本必须被 `prepare --all` 自动选中升级，
       否则改完流程旧题永远不动——用户看到的就是「代码改了、界面还是旧的」。
    2. 重跑失败不能留下「新数据 + 旧解析」的错配，旧产物要原样回来。
    """

    def test_offline_run_does_not_stamp_current_version(self) -> None:
        """离线模式（不生成备选解/逐行解析/题解）产不出 v4 契约 ⇒ 不许盖章。

        盖章等于宣称"已按新版产出"，而 select_targets 据此跳过 →
        这种半成品会被永久留在库里，正是「代码改了、界面还是旧的」的根因。
        """
        report = self.build(StubClient()).prepare(self.problem_id)
        self.assertTrue(report.ok, report.error)
        version = self.db.query_scalar(
            "SELECT artifact_version FROM problems WHERE id = ?", (self.problem_id,)
        )
        self.assertEqual(int(version), 0)
        self.assertIn("离线模式", report.stages[-1].detail)

    def test_select_targets_includes_stale_prepared(self) -> None:
        from autocode.app.prep_batch import select_targets

        self.db.execute(
            "UPDATE problems SET status='prepared', artifact_version=? WHERE id=?",
            (ARTIFACT_VERSION - 1, self.problem_id),
        )
        self.assertIn(self.problem_id, [t["id"] for t in select_targets(self.db)])

        self.db.execute(
            "UPDATE problems SET artifact_version=? WHERE id=?",
            (ARTIFACT_VERSION, self.problem_id),
        )
        self.assertNotIn(self.problem_id, [t["id"] for t in select_targets(self.db)])

    def test_snapshot_restore_roundtrip(self) -> None:
        from autocode.app.prep_store import PrepStore

        store = PrepStore(self.db)
        self.db.execute(
            "UPDATE problems SET analysis_json='{\"old\":1}', data_plan_json='{\"old\":1}'"
            " WHERE id=?",
            (self.problem_id,),
        )
        before = store.snapshot_problem(self.problem_id)
        # 模拟"新流程跑到一半"：换掉题解、清掉数据
        self.db.execute(
            "UPDATE problems SET analysis_json='{\"new\":1}', data_plan_json=NULL WHERE id=?",
            (self.problem_id,),
        )
        self.db.execute("DELETE FROM test_cases WHERE problem_id=?", (self.problem_id,))
        self.assertTrue(store.restore_problem(self.problem_id, before))
        self.assertEqual(
            json.loads(
                str(
                    self.db.query_scalar(
                        "SELECT analysis_json FROM problems WHERE id=?", (self.problem_id,)
                    )
                )
            ),
            {"old": 1},
        )
        self.assertEqual(
            int(
                self.db.query_scalar(
                    "SELECT COUNT(*) FROM test_cases WHERE problem_id=?", (self.problem_id,)
                )
            ),
            1,
        )

    def test_failed_rerun_keeps_previous_artifacts(self) -> None:
        """成功一次后再以极小预算重跑：产物不能被清掉（失败 ≠ 毁掉已有成果）。"""
        self.assertTrue(self.build(StubClient()).prepare(self.problem_id).ok)
        cases_before = int(
            self.db.query_scalar(
                "SELECT COUNT(*) FROM test_cases WHERE problem_id=?", (self.problem_id,)
            )
        )
        hints_before = int(
            self.db.query_scalar(
                "SELECT COUNT(*) FROM hints WHERE problem_id=? AND is_active=1", (self.problem_id,)
            )
        )
        report = self.build(StubClient(), max_calls=1).prepare(self.problem_id)
        self.assertFalse(report.ok)
        self.assertEqual(
            int(
                self.db.query_scalar(
                    "SELECT COUNT(*) FROM test_cases WHERE problem_id=?", (self.problem_id,)
                )
            ),
            cases_before,
        )
        self.assertEqual(
            int(
                self.db.query_scalar(
                    "SELECT COUNT(*) FROM hints WHERE problem_id=? AND is_active=1",
                    (self.problem_id,),
                )
            ),
            hints_before,
        )
        self.assertEqual(
            str(self.db.query_scalar("SELECT status FROM problems WHERE id=?", (self.problem_id,))),
            "failed",
        )


if __name__ == "__main__":
    unittest.main()


class TestGeneratorProbe(PipelineTestCase):
    """生成器探针：edge 档必须对 seed 敏感。

    实测 #18 模板展开：`if profile == 'edge': out=['3 v0']; return` 写死常量，
    7 次生成同一条，去重后边界组只剩 1 条，数据不足 10 条 → 整题在产物门被回滚。
    这种错误要在生成阶段就拦下，反馈给模型改，而不是拖到 10 分钟后。
    """

    FROZEN_EDGE = '''```python
import random, sys
seed = int(sys.argv[1]); profile = sys.argv[2]
rng = random.Random(seed)
if profile == "edge":
    print(1)
    print(7)
    raise SystemExit
n = 1000 if profile == "max" else 10
print(n)
print(" ".join(str(rng.randint(-100, 100)) for _ in range(n)))
```'''

    SEED_SENSITIVE_EDGE = '''```python
import random, sys
seed = int(sys.argv[1]); profile = sys.argv[2]
rng = random.Random(seed)
if profile == "edge":
    n = 1
    value = seed % 2          # 同样是最小规模，数值随 seed 变化
    print(n)
    print(value)
    raise SystemExit
n = 1000 if profile == "max" else 10
print(n)
print(" ".join(str(rng.randint(-100, 100)) for _ in range(n)))
```'''

    def test_frozen_edge_is_rejected(self) -> None:
        service = self.build(StubClient())
        ok, detail = service._probe_generator(SPEC, judge.extract_code(self.FROZEN_EDGE, "python"))
        self.assertFalse(ok, "edge 写死常量必须被拒")
        self.assertIn("seed", detail)
        self.assertIn("edge", detail)

    def test_seed_sensitive_edge_passes(self) -> None:
        service = self.build(StubClient())
        ok, detail = service._probe_generator(
            SPEC, judge.extract_code(self.SEED_SENSITIVE_EDGE, "python")
        )
        self.assertTrue(ok, detail)
        self.assertIn("edge2", detail)
        self.assertIn("edge5", detail)
