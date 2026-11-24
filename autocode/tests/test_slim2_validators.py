"""slim2 代码门单测：不调模型、不碰数据库，只测"什么能入库"。"""

from __future__ import annotations

import unittest

from autocode.slim2 import validators as v


def _case(layer="boundary", data="3\n1 2 3\n", expected="3\n", derive="1 2 3 最大是 3"):
    return {
        "layer": layer, "input": data,
        "derive": derive, "expected_output": expected, "confidence": "high",
    }


def _payload(cases=None):
    cases = cases or (
        [_case("boundary", f"{i}\n{i}\n", f"{i}\n", f"n=1，答案 {i}") for i in range(1, 5)]
        + [_case("typical", f"20\n" + " ".join(str(i) for i in range(1, 20)) + f" {i}\n",
                 f"{max(19, i)}\n", "前 19 个最大 19") for i in range(20, 24)]
        + [_case("stress", "300\n" + " ".join(["7"] * 300) + "\n", "7\n", "300 个 7")]
        + [_case("stress", "300\n" + " ".join(str(i) for i in range(1, 301)) + "\n",
                 "300\n", "递增到 300")]
        + [_case("trap", "3\n-1 -1 -1\n", "-1\n", "全负且相同"),
           _case("trap", "4\n3 9 9 2\n", "9\n", "并列仍是 9")]
    )
    return {
        "analysis": {"input_model": "n 后跟 n 个数", "answer_model": "最大值",
                     "constraint_dims": [{"name": "n", "min": "1", "max": "10000"}],
                     "risk_points": ["全负", "并列"], "unsupported_reason": ""},
        "cases": cases,
    }


def _fixed_payload(cases):
    """固定元数题（一行三个整数）的载荷。"""
    return {
        "analysis": {"input_model": "一行三个整数", "answer_model": "三者之和",
                     "constraint_dims": [{"name": "a,b,c", "min": "-10^9", "max": "10^9"}],
                     "risk_points": ["溢出", "全负"], "unsupported_reason": ""},
        "cases": cases,
    }


class TestNormalize(unittest.TestCase):
    def test_input_normalized(self) -> None:
        self.assertEqual(v.normalize_input("1 2\r\n3  \r\n\r\n"), "1 2\n3\n")

    def test_hash_ignores_trailing_blank(self) -> None:
        self.assertEqual(v.input_hash("1 2\n\n"), v.input_hash("1 2\n"))


class TestCompare(unittest.TestCase):
    def test_token_mode(self) -> None:
        self.assertTrue(v.compare_output("token", "3 4\n", "3   4 \n")[0])
        # token 模式按空白切分：换行与空格等价；要区分换行必须用 line
        self.assertTrue(v.compare_output("token", "3 4\n", "3\n4\n")[0])
        self.assertFalse(v.compare_output("line", "3 4\n", "3\n4\n")[0])

    def test_line_mode(self) -> None:
        self.assertTrue(v.compare_output("line", "a b\nc\n", "a b\nc\n")[0])
        self.assertFalse(v.compare_output("line", "a b\n", "a  b\n")[0])

    def test_float_mode(self) -> None:
        self.assertTrue(v.compare_output("float", "1.000000\n", "1.0000001\n", "1e-6")[0])
        self.assertFalse(v.compare_output("float", "1.000000\n", "1.1\n", "1e-6")[0])

    def test_unknown_mode_raises(self) -> None:
        with self.assertRaises(ValueError):
            v.compare_output("special", "1\n", "1\n")


class TestSegments(unittest.TestCase):
    def test_overlap_dropped(self) -> None:
        draft = v.check_segments(
            {"problems": [
                {"title": "A", "start_line": 1, "end_line": 20, "judge_type": "token"},
                {"title": "B", "start_line": 10, "end_line": 30, "judge_type": "token"},
            ]},
            total_lines=100, max_problems=20,
        )
        self.assertTrue(draft.ok)
        self.assertEqual([item["title"] for item in draft.items], ["A"])

    def test_out_of_range_dropped(self) -> None:
        draft = v.check_segments(
            {"problems": [{"title": "A", "start_line": 1, "end_line": 999}]},
            total_lines=100, max_problems=20,
        )
        self.assertFalse(draft.ok)

    def test_float_without_tolerance_gets_default(self) -> None:
        draft = v.check_segments(
            {"problems": [{"title": "A", "start_line": 1, "end_line": 10,
                           "judge_type": "float", "tolerance": ""}]},
            total_lines=100, max_problems=20,
        )
        self.assertEqual(draft.items[0]["tolerance"], "1e-6")

    def test_float_with_inline_tolerance_is_split_on_import(self) -> None:
        """`float(1e-6)` 入库时要拆成 judge_type=float + tolerance=1e-6，
        否则这一列永远带着括号，后续判题和展示都拿不到干净的判定方式。"""
        draft = v.check_segments(
            {"problems": [{"title": "A", "start_line": 1, "end_line": 10,
                           "judge_type": "float(1e-6)", "tolerance": ""}]},
            total_lines=100, max_problems=20,
        )
        self.assertEqual(draft.items[0]["judge_type"], "float")
        self.assertEqual(draft.items[0]["tolerance"], "1e-6")
        # 不该再报「判定方式非法」的告警
        self.assertFalse([item for item in draft.warnings if "判定方式" in item])


def _clean(obj, **kw):
    """check_dataset 的最小参数集：本文件只测清洗逻辑，不测条数配额。"""
    kw.setdefault("min_cases", 1)
    kw.setdefault("max_cases", 40)
    return v.check_dataset(obj, **kw)


class TestDatasetClean(unittest.TestCase):
    """D2 只做清洗：拆包、逐条去坏、与样例对账。不做任何配额判定。

    2026-11-21 之前这里还有四层下限、压力层规模、单一输出占比三道 fail，
    它们没有 ground truth，把一道合法的题连续判死两轮。
    """

    def test_happy_path(self) -> None:
        draft = _clean(_payload())
        self.assertTrue(draft.ok, draft.fatal)
        self.assertEqual(len(draft.items), 12)

    def test_duplicates_are_dropped_not_failed(self) -> None:
        obj = _payload()
        obj["cases"] = [dict(obj["cases"][0]) for _ in range(12)]
        draft = _clean(obj)
        self.assertTrue(draft.ok, draft.fatal)
        self.assertEqual(len(draft.items), 1)

    def test_sample_like_case_is_kept(self) -> None:
        # 与样例同输入不同输出，代码不判——交给 D3 与标程真跑裁决
        obj = _payload()
        obj["cases"][0] = _case("boundary", "3\n1 2 3\n", "999\n")
        draft = _clean(obj)
        self.assertIn("3\n1 2 3\n", [item["input"] for item in draft.items])

    def test_oversize_input_is_dropped(self) -> None:
        """单条输入上限 20 KB：放宽了，但仍然要有上限。"""
        data = "1 " * (v.MAX_INPUT_CHARS // 2 + 1)  # 刚好超过当前上限
        draft = _clean(_payload(cases=[_case("typical", data, "", "覆盖大输入")]))
        self.assertFalse(draft.ok)
        self.assertIn(str(v.MAX_INPUT_CHARS), "；".join(draft.warnings + draft.fatal))

    def test_too_many_lines_is_dropped(self) -> None:
        from autocode.slim2 import config

        data = "1\n" * (config.MAX_INPUT_LINES + 1)  # 行数超、字符数没超
        draft = _clean(_payload(cases=[_case("typical", data, "", "覆盖多行")]))
        self.assertFalse(draft.ok)
        self.assertIn("行", "；".join(draft.warnings + draft.fatal))

    def test_missing_expected_output_is_usable(self) -> None:
        """v6 起 D2 只出输入；答案由代码执行补上，校验器不能把这种批次判死。"""
        draft = _clean(_payload(cases=[
            {"layer": "boundary", "input": "3\n1 2 3\n", "why": "检查最大值"}
        ]))
        self.assertTrue(draft.ok, draft.fatal)
        self.assertEqual(draft.items[0]["expected_output"], "")
        self.assertEqual(draft.items[0]["input"], "3\n1 2 3\n")

    def test_missing_layers_is_still_usable(self) -> None:
        # 只有 typical：以前会被"分层覆盖未达标"判死，现在照常入库
        cases = [_case("typical", f"{i}\n{i}\n", f"{i}\n") for i in range(1, 8)]
        draft = _clean(_payload(cases=cases))
        self.assertTrue(draft.ok, draft.fatal)
        self.assertEqual(len(draft.items), 7)

    def test_constant_output_is_not_a_failure(self) -> None:
        # 有些题答案本来就恒定，这不是数据错
        cases = [_case("boundary", f"{i}\n{i}\n", "0\n") for i in range(1, 8)]
        draft = _clean(_payload(cases=cases))
        self.assertTrue(draft.ok, draft.fatal)

    def test_all_invalid_fails(self) -> None:
        obj = _payload()
        obj["cases"] = [{"layer": "nope", "input": "", "expected_output": ""} for _ in range(3)]
        draft = _clean(obj)
        self.assertFalse(draft.ok)
        self.assertIn("少于下限", "；".join(draft.fatal))


class TestDatasetReview(unittest.TestCase):
    def test_pass_requires_same_value(self) -> None:
        draft = v.check_dataset_review(
            {"reviews": [{"index": 0, "input_ok": True, "recompute": "3 比 1 大",
                          "recomputed_output": "9\n", "verdict": "pass"}]},
            [{"index": 0, "expected_output": "3\n"}], "token", "",
        )
        self.assertTrue(draft.ok)
        self.assertEqual(draft.items[0]["verdict"], "mismatch")
        self.assertEqual(draft.items[0]["corrected_output"], "9\n")

    def test_vague_recompute_becomes_uncertain(self) -> None:
        draft = v.check_dataset_review(
            {"reviews": [{"index": 0, "input_ok": True, "recompute": "与期望一致",
                          "recomputed_output": "3\n", "verdict": "pass"}]},
            [{"index": 0, "expected_output": "3\n"}], "token", "",
        )
        self.assertEqual(draft.items[0]["verdict"], "uncertain")

    def test_missing_index_rejected(self) -> None:
        draft = v.check_dataset_review(
            {"reviews": [{"index": 0, "input_ok": True, "recompute": "算了",
                          "recomputed_output": "1\n", "verdict": "pass"}]},
            [{"index": 0, "expected_output": "1\n"}, {"index": 1, "expected_output": "2\n"}],
            "token", "",
        )
        self.assertFalse(draft.ok)


class TestAdversarial(unittest.TestCase):
    def test_none_found_is_legal(self) -> None:
        draft = v.check_adversarial(
            {"diagnosis": {"divergence": "没找到", "trigger_condition": "",
                           "failure_mode": "none_found", "confidence": "low"}, "cases": []},
            limit=8,
        )
        self.assertTrue(draft.ok)
        self.assertEqual(draft.items, [])

    def test_dedup_against_existing(self) -> None:
        payload = {
            "diagnosis": {"divergence": "初始值为 0", "trigger_condition": "全负",
                          "failure_mode": "WA", "confidence": "high"},
            "cases": [{"input": "3\n-5 -2 -9\n", "derive": "最大是 -2",
                       "expected_output": "-2\n", "predicted_student_output": "0\n",
                       "targets": "全负", "confidence": "high"}],
        }
        first = v.check_adversarial(payload, limit=8)
        self.assertEqual(len(first.items), 1)
        again = v.check_adversarial(
            payload, limit=8, existing_hashes={first.items[0]["input_hash"]}
        )
        self.assertEqual(again.items, [])


class TestAdversarialReview(unittest.TestCase):
    def test_ac_is_rejected_even_if_expected_right(self) -> None:
        draft = v.check_adversarial_review(
            {"reviews": [{"index": 0, "input_ok": True, "recompute": "最大是 3",
                          "recomputed_output": "3\n", "verdict": "accept",
                          "reason_code": "ok", "reason": "", "corrected_output": None}]},
            [{"index": 0, "expected_output": "3\n", "run_verdict": "AC"}], "token", "",
        )
        self.assertEqual(draft.items[0]["verdict"], "reject")
        self.assertEqual(draft.items[0]["reason_code"], "trivial_pass")

    def test_expected_wrong_requires_correction(self) -> None:
        draft = v.check_adversarial_review(
            {"reviews": [{"index": 0, "input_ok": True, "recompute": "最大是 4",
                          "recomputed_output": "4\n", "verdict": "accept",
                          "reason_code": "ok", "reason": "", "corrected_output": None}]},
            [{"index": 0, "expected_output": "3\n", "run_verdict": "WA"}], "token", "",
        )
        self.assertEqual(draft.items[0]["verdict"], "reject")
        self.assertEqual(draft.items[0]["reason_code"], "expected_wrong")
        self.assertEqual(draft.items[0]["corrected_output"], "4\n")


class TestSupport(unittest.TestCase):
    def test_special_not_supported(self) -> None:
        self.assertTrue(v.judge_supported("token"))
        self.assertFalse(v.judge_supported("special"))
        self.assertFalse(v.judge_supported("interactive"))

    def test_judge_with_inline_tolerance_is_supported(self) -> None:
        """模型爱写 float(1e-6)。旧版按精确匹配判成不支持：浮点题要么被拒收，
        要么被降级成 token 比对，末位差一点就把正确数据判错（线上失败过 2 次）。"""
        self.assertTrue(v.judge_supported("float(1e-6)"))
        self.assertTrue(v.judge_supported("float（1e-6）"))
        self.assertTrue(v.judge_supported("Float(eps=1e-6)"))
        self.assertTrue(v.judge_supported("float 1e-6"))
        self.assertEqual(v.normalize_judge("float(1e-6)"), ("float", "1e-6"))
        self.assertEqual(v.normalize_judge("float"), ("float", ""))
        # 不认识的基础判定不能被括号里的参数蒙混过关
        self.assertFalse(v.judge_supported("special(1e-6)"))

    def test_inline_tolerance_applies_to_compare(self) -> None:
        self.assertTrue(v.compare_output("float(1e-6)", "1.000000\n", "1.0000001\n")[0])
        self.assertFalse(v.compare_output("float(1e-6)", "1.0\n", "1.1\n")[0])
        # 调用方显式给的容差优先于判定方式里写的
        self.assertTrue(v.compare_output("float(1e-9)", "1.0\n", "1.001\n", "0.01")[0])


class TestFixedArityDataset(unittest.TestCase):
    """可满足性：固定元数的题（一行三个整数）不该要求 medium/large 的压力层。

    2026-11-21 三数之和连续两轮整单失败，根因就是压力层被写死成 medium/large：
    3 个数只能是 tiny，想凑 101 个元素必须违反输入格式，模型怎么答都错。
    """

    def _cases(self):
        return [
            _case("boundary", "1 1 1\n", "3\n", "1+1+1=3"),
            _case("boundary", "0 0 0\n", "0\n", "0+0+0=0"),
            _case("boundary", "-1000000000 -1000000000 -1000000000\n", "-3000000000\n",
                  "三个下界相加"),
            _case("typical", "1 2 3\n", "6\n", "1+2+3=6"),
            _case("typical", "10 20 30\n", "60\n", "10+20+30=60"),
            _case("typical", "-5 -6 -7\n", "-18\n", "-5-6-7=-18"),
            _case("typical", "999 1 -1\n", "999\n", "999+1-1=999"),
            _case("stress", "1000000000 1000000000 1000000000\n", "3000000000\n",
                  "上界相加溢出 32 位"),
            _case("trap", "-1000000000 999999999 1\n", "0\n", "正负抵消"),
            _case("trap", "0 0 1\n", "1\n", "只有最后一位非零"),
        ]

    def test_tiny_stress_is_accepted_when_input_size_is_fixed(self) -> None:
        draft = _clean(_fixed_payload(self._cases()))
        self.assertTrue(draft.ok, draft.fatal)
        stress = [item for item in draft.items if item["layer"] == "stress"]
        self.assertEqual(len(stress), 1)
        self.assertEqual(stress[0]["size_class"], "tiny")

    def test_size_is_measured_not_taken_from_model(self) -> None:
        # 模型就算硬塞一个 size_class 也不会被采信，存的是实测值
        cases = self._cases()
        cases[7]["size_class"] = "large"
        draft = _clean(_fixed_payload(cases))
        self.assertTrue(draft.ok, draft.fatal)
        self.assertEqual(draft.items[7]["size_class"], "tiny")

    def test_no_layer_quota(self) -> None:
        # 只给 typical 也能入库：覆盖是建议，不是门槛
        cases = [_case("typical", f"{i} 1 1\n", f"{i + 2}\n") for i in range(1, 6)]
        draft = _clean(_fixed_payload(cases))
        self.assertTrue(draft.ok, draft.fatal)


class TestMeasuredSize(unittest.TestCase):
    def test_boundaries(self) -> None:
        self.assertEqual(v.measured_size(" ".join(["0"] * 10))[0], "tiny")
        self.assertEqual(v.measured_size(" ".join(["0"] * 11))[0], "small")
        self.assertEqual(v.measured_size(" ".join(["0"] * 100))[0], "small")
        self.assertEqual(v.measured_size(" ".join(["0"] * 101))[0], "medium")
        self.assertEqual(v.measured_size(" ".join(["0"] * 1000))[0], "medium")
        self.assertEqual(v.measured_size(" ".join(["0"] * 1001))[0], "large")

    def test_tokens_counted_not_chars(self) -> None:
        # 一行很长但没有空格：元素数按 1 算，不按字符数放大
        self.assertEqual(v.measured_size("x" * 5000), ("tiny", 1))


if __name__ == "__main__":
    unittest.main()
