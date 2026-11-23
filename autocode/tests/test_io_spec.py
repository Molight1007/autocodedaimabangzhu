"""I/O 契约：解析、校验、描述。

这些测试守的是一条线：**契约读不通的数据不能入库**。
只要这条成立，"链内自洽但评测机跑不了"的伪成功就进不来。
"""

from __future__ import annotations

import unittest

from autocode.slim2 import io_spec

SPEC = {
    "input": [
        {"name": "n", "type": "int", "min": 1, "max": 1000},
        {"name": "a", "type": "int[]", "len": "n", "min": -1000000000, "max": 1000000000},
    ],
    "output": {"type": "int", "order": "fixed"},
}

TWO_LINES = """{"input": [
  {"name": "n", "type": "int", "min": 1, "max": 10},
  {"name": "a", "type": "int[]", "len": "n"},
  {"name": "target", "type": "int"}
], "output": {"type": "int[]", "order": "any"}}"""


class TestParse(unittest.TestCase):
    def test_ok(self) -> None:
        spec, problems = io_spec.parse(SPEC)
        self.assertEqual(problems, [])
        self.assertEqual(len(spec["input"]), 2)
        self.assertTrue(spec["input"][1]["array"])
        self.assertEqual(spec["input"][1]["len_ref"], "n")

    def test_missing_input(self) -> None:
        _, problems = io_spec.parse({"output": {"type": "int"}})
        self.assertTrue(problems)

    def test_unknown_type(self) -> None:
        _, problems = io_spec.parse({"input": [{"name": "x", "type": "blob"}]})
        self.assertTrue(any("不支持" in item for item in problems))

    def test_array_without_len(self) -> None:
        _, problems = io_spec.parse({"input": [{"name": "a", "type": "int[]"}]})
        self.assertTrue(any("len" in item for item in problems))

    def test_sep_line(self) -> None:
        spec, problems = io_spec.parse(
            {"input": [{"name": "a", "type": "int[]", "len": 3, "sep": "line"}]}
        )
        self.assertEqual(problems, [])
        self.assertEqual(spec["input"][0]["sep"], "line")


class TestValidate(unittest.TestCase):
    def setUp(self) -> None:
        self.spec, _ = io_spec.parse(SPEC)

    def test_valid(self) -> None:
        ok, reason = io_spec.validate_input(self.spec, "3\n5 -2 9\n")
        self.assertTrue(ok, reason)

    def test_count_mismatch(self) -> None:
        ok, reason = io_spec.validate_input(self.spec, "5\n1 2 3\n")
        self.assertFalse(ok)
        self.assertIn("不够", reason)

    def test_out_of_range(self) -> None:
        ok, reason = io_spec.validate_input(self.spec, "1\n2000000000\n")
        self.assertFalse(ok)
        self.assertIn("上限", reason)

    def test_extra_token(self) -> None:
        """多给的 token 必须被抓住：否则"一行写两组数据"会被当成合法。"""
        ok, reason = io_spec.validate_input(self.spec, "3\n5 -2 9 100\n")
        self.assertFalse(ok)
        self.assertIn("多余", reason)

    def test_not_integer(self) -> None:
        ok, reason = io_spec.validate_input(self.spec, "2\n5 x\n")
        self.assertFalse(ok)

    def test_space_array_spans_lines(self) -> None:
        """空格分隔的数组允许换行写——输入格式常常一行放不下。"""
        ok, reason = io_spec.validate_input(self.spec, "4\n1 2\n3 4\n")
        self.assertTrue(ok, reason)

    def test_line_sep_takes_one_per_line(self) -> None:
        spec, _ = io_spec.parse(
            {"input": [{"name": "a", "type": "int[]", "len": 2, "sep": "line"}]}
        )
        self.assertTrue(io_spec.validate_input(spec, "1\n2\n")[0])
        self.assertFalse(io_spec.validate_input(spec, "1 2\n")[0])

    def test_line_array_after_scalar_on_first_line(self) -> None:
        """首行的 `q` 与后续 q 行是同一份输入，`line[]` 不能把 q 那一行算进去。

        这是最常见的输入形态（`3\\n2155895064 3\\n2 2\\n10000000000 10`）。
        旧实现从当前行切片，把 `3` 当成第一行、又少吃最后一行，
        于是正确数据被判成"读完契约规定的字段后还有多余内容"。
        """
        spec, _ = io_spec.parse(
            {
                "input": [
                    {"name": "q", "type": "int"},
                    {"name": "rows", "type": "line[]", "len": "q", "sep": "line"},
                ]
            }
        )
        ok, reason = io_spec.validate_input(spec, "3\n1 2\n3 4\n5 6\n")
        self.assertTrue(ok, reason)
        # 少一行仍然要拦住
        self.assertFalse(io_spec.validate_input(spec, "3\n1 2\n3 4\n")[0])

    def test_empty_input(self) -> None:
        self.assertFalse(io_spec.validate_input(self.spec, "")[0])


class TestCheckContract(unittest.TestCase):
    def _payload(self, **over) -> dict:
        base = {
            "io": SPEC,
            "judge_type": "token",
            "samples": [{"input": "3\n5 -2 9\n", "output": "3\n"}],
        }
        base.update(over)
        return base

    def test_ok(self) -> None:
        draft = io_spec.check_contract(self._payload(), [{"input": "x", "output": "3"}])
        self.assertTrue(draft.ok, draft.fatal)
        self.assertEqual(len(draft.items), 1)

    def test_sample_count_must_match(self) -> None:
        draft = io_spec.check_contract(
            self._payload(), [{"output": "3"}, {"output": "4"}]
        )
        self.assertFalse(draft.ok)
        self.assertIn("组数", draft.fatal[0])

    def test_sample_must_fit_contract(self) -> None:
        payload = self._payload(samples=[{"input": "nums = [1,2]\n", "output": "3\n"}])
        draft = io_spec.check_contract(payload, [{"output": "3"}])
        self.assertFalse(draft.ok)
        self.assertIn("契约", draft.fatal[0])

    def test_answer_cannot_change(self) -> None:
        payload = self._payload(samples=[{"input": "3\n5 -2 9\n", "output": "9\n"}])
        draft = io_spec.check_contract(payload, [{"output": "3"}])
        self.assertFalse(draft.ok)
        self.assertIn("变了", draft.fatal[0])

    def test_order_any_forces_unordered(self) -> None:
        spec, _ = io_spec.parse(TWO_LINES)
        payload = {
            "io": spec, "judge_type": "token",
            "samples": [{"input": "2\n1 2\n3\n", "output": "0 1\n"}],
        }
        draft = io_spec.check_contract(payload, [{"output": "0 1"}])
        self.assertFalse(draft.ok)
        self.assertIn("unordered", draft.fatal[0])

    def test_unordered_accepted(self) -> None:
        spec, _ = io_spec.parse(TWO_LINES)
        payload = {
            "io": spec, "judge_type": "unordered",
            "samples": [{"input": "2\n1 2\n3\n", "output": "1 0\n"}],
        }
        draft = io_spec.check_contract(payload, [{"output": "0 1"}])
        self.assertTrue(draft.ok, draft.fatal)


class TestDescribe(unittest.TestCase):
    def test_readable(self) -> None:
        spec, _ = io_spec.parse(SPEC)
        text = io_spec.describe(spec)
        self.assertIn("n", text)
        self.assertIn("每个是 int", text)
        self.assertIn("空格分隔", text)
        self.assertIn("1000", text)

    def test_judge_of(self) -> None:
        spec, _ = io_spec.parse({"input": [{"name": "x", "type": "int"}],
                                 "output": {"type": "int[]", "order": "any"}})
        self.assertEqual(io_spec.judge_of(spec), "unordered")

    def test_loads_bad_json(self) -> None:
        self.assertEqual(io_spec.loads("{oops"), {})
        self.assertEqual(io_spec.loads(""), {})


if __name__ == "__main__":
    unittest.main()
