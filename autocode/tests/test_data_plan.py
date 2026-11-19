"""数据计划、多解法与解法可信度审计的单元测试（不联网、不起进程）。

守四件事：
1. 模型给的越界计划必须被修正/丢弃，不能把非法 profile 传进生成器；
2. 有效条数不足 10 时由调用方补足（这里验计划层给出的下限口径）；
3. 解法清单不足 2 条时会追问一次，且不会把同名解法重复计入；
4. 声称更低阶却没变快的备选解必须剔除，没声称更优的解不能误杀。
"""

from __future__ import annotations

import unittest
from typing import Any, Dict, List

from autocode.app import data_plan, ref_audit
from autocode.app.prep_models import RefArtifact
from autocode.app.solutions import _clean_approaches, is_reskin
from autocode.llm.client import ChatResult, ChatUsage

SPEC = {
    "input_grammar": [
        {"kind": "scalar", "name": "n", "type": "int"},
        {"kind": "repeat", "count": "n", "sep": "space", "elem": {"type": "int", "name": "a"}},
    ],
    "constraints_exec": ["1 <= n <= 1000"],
    "checker_type": "exact",
}


def result(obj: Dict[str, Any]) -> ChatResult:
    return ChatResult(
        content="{}", usage=ChatUsage(1, 1, 2), attempts=1, elapsed_ms=1.0,
        model="stub", json_obj=obj, finish_reason="stop",
    )


class TestNormalizePlan(unittest.TestCase):
    def test_default_plan_matches_legacy_counts(self) -> None:
        plan = data_plan.default_plan(3, 30, 2)
        self.assertEqual([g["count"] for g in plan["groups"]], [3, 30, 2])
        self.assertEqual(plan["source"], "default")

    def test_invalid_profile_and_layer_are_dropped(self) -> None:
        payload = {
            "total": 12,
            "groups": [
                {"name": "坏档位", "layer": "boundary", "profile": "huge", "count": 3},
                {"name": "矛盾", "layer": "stress", "profile": "tiny", "count": 3},
                {"name": "正常", "layer": "exhaustive", "profile": "small", "count": 4},
            ],
        }
        plan = data_plan.normalize_plan(payload, data_plan.default_plan(1, 1, 1))
        self.assertEqual(plan["groups"][0]["name"], "正常", "非法档位/分层必须先被丢弃")
        self.assertEqual(plan["groups"][0]["profile"], "small")
        self.assertEqual(plan["source"], "llm")
        self.assertGreaterEqual(sum(g["count"] for g in plan["groups"]), data_plan.MIN_TOTAL)

    def test_total_is_topped_up_to_minimum(self) -> None:
        payload = {
            "total": 2,
            "groups": [{"name": "小", "layer": "exhaustive", "profile": "tiny", "count": 2}],
        }
        plan = data_plan.normalize_plan(payload, data_plan.default_plan(1, 1, 1))
        self.assertGreaterEqual(sum(g["count"] for g in plan["groups"]), data_plan.MIN_TOTAL)
        self.assertEqual(plan["source"], "llm")

    def test_hand_cases_reject_oversized_input(self) -> None:
        payload = {
            "groups": [{"name": "小", "layer": "exhaustive", "profile": "tiny", "count": 10}],
            "hand_cases": [
                {"input": "\n".join(str(i) for i in range(80)), "focus": "太长"},
                {"input": "3\n1 2 3\n", "focus": "正常"},
            ],
        }
        plan = data_plan.normalize_plan(payload, data_plan.default_plan(1, 1, 1))
        self.assertEqual(len(plan["hand_cases"]), 1)
        self.assertEqual(plan["hand_cases"][0]["focus"], "正常")

    def test_group_count_is_clamped(self) -> None:
        payload = {"groups": [{"name": "大", "layer": "exhaustive", "profile": "tiny", "count": 99}]}
        plan = data_plan.normalize_plan(payload, data_plan.default_plan(1, 1, 1))
        self.assertLessEqual(plan["groups"][0]["count"], data_plan.MAX_GROUP_COUNT)


class TestDiversity(unittest.TestCase):
    def test_same_shape_is_deduplicated(self) -> None:
        cases = [
            {"input": "3\n1 2 3\n"},
            {"input": "3\n4 5 6\n"},
            {"input": "1000\n" + " ".join(str(i) for i in range(1000)) + "\n"},
        ]
        picked = data_plan.diverse_select(cases, 3, set())
        signatures = {data_plan.signature(c["input"]) for c in picked}
        self.assertGreaterEqual(len(signatures), 2, "两条同形数据不该都占名额")

    def test_limit_is_respected(self) -> None:
        cases = [{"input": f"{n}\n" + " ".join(["1"] * n) + "\n"} for n in range(1, 20)]
        self.assertEqual(len(data_plan.diverse_select(cases, 4, set())), 4)

    def test_hand_case_checks_grammar_and_constraints(self) -> None:
        self.assertTrue(data_plan.hand_case_ok(SPEC, "3\n1 2 3\n"))
        self.assertFalse(data_plan.hand_case_ok(SPEC, "2000\n" + " ".join(["1"] * 2000) + "\n"))


class StubPlanService:
    def __init__(self, replies: List[Dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.tags: List[str] = []
        self.notes: List[str] = []
        self.enable_llm_extras = True
        self.call_cap = 40
        self._calls_this_run = 0

    def _chat(self, template: str, user: str, *, tag: str, **kwargs: Any) -> ChatResult:
        self.tags.append(tag)
        self._calls_this_run += 1
        return result(self.replies.pop(0))

    def _note(self, message: str) -> None:
        self.notes.append(message)


class TestSolutionPlan(unittest.TestCase):
    PROB = {"id": 1, "statement_md": "给 n 个数求和"}
    SPEC = {"samples": [{"input": "3\n1 2 3\n", "output": "6\n"}]}

    def test_second_thought_is_requested_when_only_one(self) -> None:
        from autocode.app import solutions

        service = StubPlanService([
            {"approaches": [{"name": "模拟", "idea": "直接扫", "complexity": "O(n)"}]},
            {"approaches": [
                {"name": "模拟", "idea": "直接扫", "complexity": "O(n)"},
                {"name": "分治", "idea": "两半求和再合并", "complexity": "O(n)"},
            ]},
        ])
        approaches = solutions.plan(service, self.PROB, self.SPEC)
        self.assertEqual([a["name"] for a in approaches], ["模拟", "分治"])
        self.assertEqual(service.tags, ["plan#1", "plan#2"])

    def test_single_solution_is_recorded_honestly(self) -> None:
        from autocode.app import solutions

        same = {"approaches": [{"name": "模拟", "idea": "直接扫", "complexity": "O(n)"}],
                "single_reason": "题面规定了固定流程"}
        service = StubPlanService([same, same])
        approaches = solutions.plan(service, self.PROB, self.SPEC)
        self.assertEqual(len(approaches), 1)
        self.assertTrue(any("固定流程" in note for note in service.notes))


class TestNoveltyGate(unittest.TestCase):
    """结构查重：模型把主解改变量名交上来，不能算作第二种解法。"""

    BRUTE = (
        "import sys\n"
        "d=sys.stdin.read().split()\n"
        "n=int(d[0]); a=[int(x) for x in d[1:1+n]]\n"
        "out=[]\n"
        "for i in range(n):\n"
        "    ans=0\n"
        "    for j in range(n):\n"
        "        if a[j]>a[i]:\n"
        "            ans=j+1; break\n"
        "    out.append(str(ans))\n"
        "print('\\n'.join(out))\n"
    )
    RESKIN = (
        "import sys\n"
        "data=sys.stdin.read().split()\n"
        "cnt=int(data[0]); vals=[int(v) for v in data[1:1+cnt]]\n"
        "res=[]\n"
        "for i in range(cnt):\n"
        "    pos=0\n"
        "    for j in range(cnt):\n"
        "        if vals[j]>vals[i]:\n"
        "            pos=j+1; break\n"
        "    res.append(str(pos))\n"
        "sys.stdout.write('\\n'.join(res))\n"
    )
    DIFFERENT = (
        "import sys\n"
        "d=list(map(int,sys.stdin.read().split()))\n"
        "n=d[0]; a=sorted((v,i) for i,v in enumerate(d[1:1+n]))\n"
        "ans=[0]*n\n"
        "for k in range(n-1):\n"
        "    if a[k+1][0]>a[k][0]:\n"
        "        ans[a[k][1]]=a[k+1][1]+1\n"
        "print('\\n'.join(map(str,ans)))\n"
    )

    def test_reskin_is_detected(self) -> None:
        from autocode.app.solutions import same_structure

        self.assertTrue(same_structure(self.BRUTE, self.RESKIN))

    def test_different_algorithm_passes(self) -> None:
        from autocode.app.solutions import same_structure

        self.assertFalse(same_structure(self.BRUTE, self.DIFFERENT))


class TestCleanApproaches(unittest.TestCase):
    def test_unknown_methods_are_dropped_and_order_follows_plan(self) -> None:
        methods = [{"name": "模拟", "complexity": "O(n)"}, {"name": "哈希", "complexity": "O(1)"}]
        items = [
            {"name": "哈希", "idea": "下标即键", "correctness": "一一对应"},
            {"name": "乱入", "idea": "x", "correctness": "y"},
            {"name": "模拟", "idea": "", "correctness": ""},
        ]
        cleaned = _clean_approaches(items, methods)
        self.assertEqual([x["name"] for x in cleaned], ["哈希"])


class TestRefAudit(unittest.TestCase):
    '''复杂度声明审计：纯 AST 判据，不跑代码、不联网、无计时噪声。'''

    # 三层循环：典型 O(n^2 m) 暴力
    TRIPLE = chr(10).join([
        "import sys",
        "d = sys.stdin.read().split()",
        "n = int(d[0]); m = int(d[1])",
        "out = []",
        "for i in range(n):",
        "    best = 0",
        "    for j in range(n):",
        "        ok = True",
        "        for k in range(m):",
        "            if not ok:",
        "                break",
        "        if ok:",
        "            best = j + 1",
        "    out.append(str(best))",
    ])
    # 两层循环 + 排序：声称降阶时嵌套确实少了
    DOUBLE = chr(10).join([
        "import sys",
        "d = sys.stdin.read().split()",
        "n = int(d[0])",
        "a = sorted(int(x) for x in d[1:1+n])",
        "out = []",
        "for i in range(n):",
        "    best = 0",
        "    for j in range(n):",
        "        if a[j] > a[i]:",
        "            best = j",
        "            break",
        "    out.append(str(best))",
    ])

    @staticmethod
    def _artifact(code, role, complexity, *, primary=False, lang="python"):
        return RefArtifact(
            lang=lang, role=role, code=code, verified=1,
            approach=role, complexity=complexity, is_primary=int(primary),
        )

    def _refs(self, *, alt2_code=None, alt2_complexity="O(n m log n)"):
        return {
            "python/reference": self._artifact(self.TRIPLE, "reference", "O(n^2 m)", primary=True),
            "python/alt1": self._artifact(self.DOUBLE, "alt1", "O(n^2 m) 最坏，平均更快"),
            "python/alt2": self._artifact(alt2_code or self.TRIPLE, "alt2", alt2_complexity),
        }

    def test_claimed_degree_parses_only_big_o(self):
        self.assertEqual(ref_audit.claimed_degree("O(n m log n)"), 1)
        self.assertEqual(ref_audit.claimed_degree("O(n^2 m)"), 2)
        self.assertEqual(ref_audit.claimed_degree("O(n²)"), 2)
        self.assertEqual(ref_audit.claimed_degree("O(n^3)"), 3)
        self.assertIsNone(ref_audit.claimed_degree("O(1)"))
        self.assertIsNone(ref_audit.claimed_degree("未声明"))
        self.assertIsNone(ref_audit.claimed_degree("平均更快"))

    def test_loop_depth_counts_nested_loops(self):
        self.assertEqual(ref_audit.loop_depth(self.TRIPLE), 3)
        self.assertEqual(ref_audit.loop_depth(self.DOUBLE), 2)
        self.assertIsNone(ref_audit.loop_depth("def (:"))

    def test_no_audit_when_primary_has_no_complexity(self):
        refs = self._refs()
        refs["python/reference"].complexity = ""
        self.assertEqual(ref_audit.audit_claims(StubPlanService([]), refs), [])

    def test_honest_same_order_alt_is_not_touched(self):
        '''「排序+剪枝」标 O(n^2 m) 没声称更优，绝不能被误杀。'''
        refs = self._refs(alt2_complexity="O(n^2 m) 最坏，平均更快")
        self.assertEqual(ref_audit.audit_claims(StubPlanService([]), refs), [])
        self.assertEqual(len(refs), 3)

    def test_fake_lower_order_claim_is_dropped(self):
        '''#26 回归：三重循环却标 O(n m log n)，必须剔除。'''
        refs = self._refs()
        service = StubPlanService([])
        dropped = ref_audit.audit_claims(service, refs)
        self.assertEqual(dropped, ["python/alt2"])
        self.assertNotIn("python/alt2", refs)
        self.assertIn("python/alt1", refs)
        self.assertTrue(any("已剔除" in note for note in service.notes))

    def test_reduced_nesting_is_kept(self):
        refs = self._refs(alt2_code=self.DOUBLE)
        service = StubPlanService([])
        self.assertEqual(ref_audit.audit_claims(service, refs), [])
        self.assertIn("python/alt2", refs)
        self.assertTrue(any("保留" in note for note in service.notes))

    def test_unparseable_alt_is_kept(self):
        '''解析不了就不下结论，保守保留，不误杀。'''
        refs = self._refs(alt2_code="def (:")
        self.assertEqual(ref_audit.audit_claims(StubPlanService([]), refs), [])
        self.assertIn("python/alt2", refs)

    def test_cpp_primary_still_uses_python_reference_as_baseline(self):
        refs = self._refs()
        refs["cpp/reference"] = self._artifact(
            "#include <bits/stdc++.h>", "reference", "O(n^2 m)", primary=True, lang="cpp"
        )
        refs["python/reference"].is_primary = 0
        self.assertEqual(ref_audit.audit_claims(StubPlanService([]), refs), ["python/alt2"])

    def test_no_same_lang_reference_skips_audit(self):
        refs = self._refs()
        refs.pop("python/reference")
        service = StubPlanService([])
        self.assertEqual(ref_audit.audit_claims(service, refs), [])
        self.assertTrue(any("同语言" in note for note in service.notes))


class TestReskinGate(unittest.TestCase):
    '''查重时机：生成阶段 verified 全为 0，也必须能查出换壳。'''

    def test_verified_zero_still_detects_reskin(self):
        refs = {
            "python/reference": RefArtifact(
                lang="python", role="reference", code=TestNoveltyGate.BRUTE, verified=0
            )
        }
        self.assertTrue(is_reskin(TestNoveltyGate.RESKIN, refs, "python"))

    def test_different_algorithm_passes(self):
        refs = {
            "python/reference": RefArtifact(
                lang="python", role="reference", code=TestNoveltyGate.BRUTE, verified=0
            )
        }
        self.assertFalse(is_reskin(TestNoveltyGate.DIFFERENT, refs, "python"))

    def test_other_language_is_ignored(self):
        refs = {
            "cpp/reference": RefArtifact(
                lang="cpp", role="reference", code=TestNoveltyGate.BRUTE, verified=0
            )
        }
        self.assertFalse(is_reskin(TestNoveltyGate.RESKIN, refs, "python"))


if __name__ == "__main__":
    unittest.main()
