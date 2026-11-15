"""质量评分纯函数测试（契约 §4.1 的五条扣分规则各至少一例）。"""

from __future__ import annotations

import unittest

from autocode.app.quality import (
    ISSUE_CONFLICT_CASES,
    ISSUE_CONSTANT,
    ISSUE_DUPLICATE,
    ISSUE_LOW_TOTAL,
    ISSUE_NO_BOUNDARY,
    ISSUE_NO_STRESS,
    ISSUE_UNVERIFIED,
    ISSUE_UNVERIFIED_CASES,
    LEVEL_ERROR,
    LEVEL_WARN,
    score_problem,
    summarize_issues,
)


def _cases(count: int, layer: str, *, output: str | None = None, prefix: str = "in") -> list:
    """默认每条输出不同（避免误触发常量输出扣分）；要测常量时显式传 output。"""
    return [
        {
            "layer": layer,
            "input_data": f"{prefix}{i}",
            "expected_output": str(output) if output is not None else f"{prefix}-out-{i}",
        }
        for i in range(count)
    ]


def _solutions(total: int, verified: int) -> list:
    return [
        {"lang": "python", "verified": 1 if i < verified else 0} for i in range(total)
    ]


class TestCleanData(unittest.TestCase):
    def test_full_marks(self) -> None:
        cases = _cases(20, "exhaustive") + _cases(2, "boundary", prefix="b") + _cases(2, "stress", prefix="s")
        # 输出各不相同，避免常量输出判定
        for index, case in enumerate(cases):
            case["expected_output"] = str(index)
        result = score_problem(cases, _solutions(2, 2))
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["total"], 24)
        self.assertEqual(result["duplicate_inputs"], 0)
        self.assertEqual(result["verified_solutions"], 2)


class TestDuplicatePenalty(unittest.TestCase):
    def test_duplicates_deduct(self) -> None:
        cases = _cases(20, "exhaustive")
        for index, case in enumerate(cases):
            case["expected_output"] = str(index)
        cases.append({"layer": "stress", "input_data": "in0", "expected_output": "0"})
        cases.append(dict(_cases(1, "boundary", prefix="b")[0]))
        result = score_problem(cases, _solutions(1, 1))
        self.assertEqual(result["duplicate_inputs"], 1)
        self.assertIn(ISSUE_DUPLICATE, [i["code"] for i in result["issues"]])
        self.assertEqual(result["score"], 98)  # 100 - min(20, 1*2)

    def test_duplicate_penalty_capped(self) -> None:
        # 30 条完全相同的 input → 29 条重复 → min(20, 29*2) = 20（封顶）
        cases = [
            {"layer": "exhaustive", "input_data": "same", "expected_output": str(i)}
            for i in range(30)
        ]
        cases.append(dict(_cases(1, "boundary", prefix="b")[0]))
        cases.append(dict(_cases(1, "stress", prefix="s")[0]))
        result = score_problem(cases, _solutions(1, 1))
        self.assertEqual(result["duplicate_inputs"], 29)
        self.assertEqual(result["score"], 80)


class TestConstantOutputPenalty(unittest.TestCase):
    def test_constant_outputs(self) -> None:
        cases = _cases(20, "exhaustive", output="42") + _cases(2, "boundary", output="42", prefix="b")
        cases += _cases(2, "stress", output="42", prefix="s")
        result = score_problem(cases, _solutions(1, 1))
        self.assertGreater(result["constant_outputs"], 0)
        self.assertIn(ISSUE_CONSTANT, [i["code"] for i in result["issues"]])
        self.assertEqual(result["score"], 80)

    def test_varied_outputs_not_flagged(self) -> None:
        cases = _cases(20, "exhaustive")
        for index, case in enumerate(cases):
            case["expected_output"] = str(index)
        cases += _cases(2, "boundary", prefix="b")
        cases += _cases(2, "stress", prefix="s")
        result = score_problem(cases, _solutions(1, 1))
        self.assertEqual(result["constant_outputs"], 0)


class TestMissingLayers(unittest.TestCase):
    def test_no_boundary(self) -> None:
        cases = _cases(20, "exhaustive") + _cases(2, "stress", prefix="s")
        result = score_problem(cases, _solutions(1, 1))
        codes = [i["code"] for i in result["issues"]]
        self.assertIn(ISSUE_NO_BOUNDARY, codes)
        self.assertNotIn(ISSUE_NO_STRESS, codes)

    def test_no_stress(self) -> None:
        cases = _cases(20, "exhaustive") + _cases(2, "boundary", prefix="b")
        result = score_problem(cases, _solutions(1, 1))
        codes = [i["code"] for i in result["issues"]]
        self.assertIn(ISSUE_NO_STRESS, codes)
        self.assertNotIn(ISSUE_NO_BOUNDARY, codes)

    def test_missing_both(self) -> None:
        result = score_problem(_cases(20, "exhaustive"), _solutions(1, 1))
        codes = [i["code"] for i in result["issues"]]
        self.assertIn(ISSUE_NO_BOUNDARY, codes)
        self.assertIn(ISSUE_NO_STRESS, codes)
        self.assertEqual(result["score"], 80)


class TestLowTotal(unittest.TestCase):
    def test_below_twenty(self) -> None:
        cases = _cases(5, "exhaustive") + _cases(1, "boundary", prefix="b") + _cases(1, "stress", prefix="s")
        result = score_problem(cases, _solutions(1, 1))
        self.assertIn(ISSUE_LOW_TOTAL, [i["code"] for i in result["issues"]])
        self.assertEqual(result["score"], 85)

    def test_exactly_twenty_not_penalised(self) -> None:
        cases = _cases(18, "exhaustive") + _cases(1, "boundary", prefix="b") + _cases(1, "stress", prefix="s")
        result = score_problem(cases, _solutions(1, 1))
        self.assertNotIn(ISSUE_LOW_TOTAL, [i["code"] for i in result["issues"]])


class TestUnverifiedSolutions(unittest.TestCase):
    def test_unverified_deducts(self) -> None:
        cases = _cases(20, "exhaustive") + _cases(2, "boundary", prefix="b") + _cases(2, "stress", prefix="s")
        for index, case in enumerate(cases):
            case["expected_output"] = str(index)
        result = score_problem(cases, _solutions(3, 2))
        self.assertIn(ISSUE_UNVERIFIED, [i["code"] for i in result["issues"]])
        self.assertEqual(result["score"], 90)
        self.assertEqual(result["verified_solutions"], 2)
        self.assertEqual(result["solutions"], 3)

    def test_no_cases_at_all(self) -> None:
        """空题按规则扣：低总量 15 + 缺 boundary 10 + 缺 stress 10 = 65（契约写死，不特殊照顾）。"""
        result = score_problem([], [])
        self.assertEqual(result["score"], 65)
        codes = [i["code"] for i in result["issues"]]
        self.assertIn(ISSUE_LOW_TOTAL, codes)
        self.assertEqual(result["total"], 0)


class TestTrustPenalties(unittest.TestCase):
    """v2 规则：数据可信度必须计入评分（只扣规模/重复会漏掉"参考解互相矛盾"）。"""

    def _full(self) -> list:
        cases = [
            {"layer": "exhaustive", "input_data": f"in{i}", "expected_output": f"out{i}",
             "trust_level": "oracle"}
            for i in range(20)
        ]
        cases.append({"layer": "boundary", "input_data": "b", "expected_output": "bo", "trust_level": "oracle"})
        cases.append({"layer": "stress", "input_data": "s", "expected_output": "so", "trust_level": "cross_verified"})
        return cases

    def test_conflict_deducts_15_with_error_level(self) -> None:
        cases = self._full() + [
            {"layer": "user", "input_data": "u", "expected_output": "uo", "trust_level": "conflict"}
        ]
        result = score_problem(cases, [{"lang": "python", "verified": 1}])
        self.assertEqual(result["score"], 85)
        self.assertEqual(result["conflict_cases"], 1)
        issue = next(i for i in result["issues"] if i["code"] == ISSUE_CONFLICT_CASES)
        self.assertEqual(issue["level"], LEVEL_ERROR)

    def test_reference_only_deducts_5_with_warn_level(self) -> None:
        cases = self._full() + [
            {"layer": "stress", "input_data": "u", "expected_output": "uo", "trust_level": "reference_only"}
        ]
        result = score_problem(cases, [{"lang": "python", "verified": 1}])
        self.assertEqual(result["score"], 95)
        self.assertEqual(result["reference_only_cases"], 1)
        issue = next(i for i in result["issues"] if i["code"] == ISSUE_UNVERIFIED_CASES)
        self.assertEqual(issue["level"], LEVEL_WARN)

    def test_both_penalties_accumulate(self) -> None:
        cases = self._full() + [
            {"layer": "user", "input_data": "c1", "expected_output": "o", "trust_level": "conflict"},
            {"layer": "user", "input_data": "c2", "expected_output": "o", "trust_level": "conflict"},
            {"layer": "stress", "input_data": "r1", "expected_output": "o", "trust_level": "reference_only"},
        ]
        result = score_problem(cases, [{"lang": "python", "verified": 1}])
        self.assertEqual(result["score"], 80)  # 100 - 15 - 5（同类条数不重复加倍）
        codes = [i["code"] for i in result["issues"]]
        self.assertIn(ISSUE_CONFLICT_CASES, codes)
        self.assertIn(ISSUE_UNVERIFIED_CASES, codes)
        self.assertEqual(result["conflict_cases"], 2)
        self.assertEqual(result["reference_only_cases"], 1)

    def test_floor_zero(self) -> None:
        """扣满 105 分时下限为 0（重复20+常量20+缺层20+低量15+未验证解10+矛盾15+未验证数据5）。"""
        cases = [
            {"layer": "sample", "input_data": "same", "expected_output": "x", "trust_level": "conflict"}
            for _ in range(18)
        ]
        cases.append(
            {"layer": "sample", "input_data": "same", "expected_output": "x", "trust_level": "reference_only"}
        )
        result = score_problem(cases, [{"lang": "python", "verified": 0}])
        self.assertEqual(result["score"], 0)
        self.assertGreaterEqual(result["score"], 0)

    def test_clean_trust_unaffected(self) -> None:
        result = score_problem(self._full(), [{"lang": "python", "verified": 1}])
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["issues"], [])


class TestShape(unittest.TestCase):
    def test_layer_counts_stable_order(self) -> None:
        cases = (
            _cases(1, "stress", prefix="s")
            + _cases(1, "sample", prefix="x")
            + _cases(1, "boundary", prefix="b")
        )
        result = score_problem(cases, [])
        layers = [item["layer"] for item in result["layer_counts"]]
        self.assertEqual(layers, ["sample", "boundary", "stress"])

    def test_langs_and_keys(self) -> None:
        result = score_problem([], [{"lang": "cpp", "verified": 1}, {"lang": "python", "verified": 1}])
        self.assertEqual(result["langs"], ["cpp", "python"])
        for key in (
            "score",
            "total",
            "distinct_inputs",
            "duplicate_inputs",
            "constant_outputs",
            "layer_counts",
            "verified_solutions",
            "solutions",
            "langs",
            "issues",
        ):
            self.assertIn(key, result)

    def test_summarize(self) -> None:
        self.assertEqual(summarize_issues([]), "无问题")
        self.assertIn("没有", summarize_issues([{"message": "没有 stress 层"}]))


if __name__ == "__main__":
    unittest.main()
