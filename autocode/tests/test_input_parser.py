"""输入解析兜底：LLM 写的 parser.py 与声明式 grammar 的统一入口。"""

from __future__ import annotations

import unittest

from autocode.engine.datagen import grammar, input_parser

LINE_PARSER = """
import sys, json
lines = sys.stdin.read().splitlines()
n = int(lines[0])
words = lines[1].split()
print(json.dumps({"n": n, "words": words, "raw": lines[1]}))
"""

BAD_PARSER = """
import sys
print("not json")
"""

# 只读第一行、忽略后面全部内容：样例能过，但哨兵测试必须拒绝它
LAZY_PARSER = """
import sys, json
first = sys.stdin.readline()
print(json.dumps({"n": int(first.split()[0])}))
"""

# 输出 key 与 spec 声明不一致
BAD_KEY_PARSER = """
import sys, json
tokens = sys.stdin.read().split()
if any(t.startswith("__AUTOCODE_SENTINEL__") for t in tokens):
    raise SystemExit("trailing")
print(json.dumps({"x": int(tokens[0])}))
"""

# 非确定性：每次输出不同
RANDOM_PARSER = """
import sys, json, random
sys.stdin.read()
print(json.dumps({"n": random.randint(1, 10**9)}))
"""

FULL_PARSER = """
import sys, json
lines = sys.stdin.read().splitlines()
tokens = " ".join(lines).split()
if any(t.startswith("__AUTOCODE_SENTINEL__") for t in tokens):
    raise SystemExit("unexpected trailing content")
print(json.dumps({"n": int(tokens[0]), "m": int(tokens[1])}))
"""


class TestInputParser(unittest.TestCase):
    def test_parser_reads_whole_line_with_spaces(self) -> None:
        spec = {"input_parser_code": LINE_PARSER}
        parsed = input_parser.parse_input(spec, "2\nhello world again\n")
        self.assertEqual(parsed["words"], ["hello", "world", "again"])
        self.assertEqual(parsed["raw"], "hello world again")

    def test_non_json_output_rejected(self) -> None:
        with self.assertRaises(grammar.GrammarError):
            input_parser.parse_input({"input_parser_code": BAD_PARSER}, "1\n")

    def test_grammar_fallback_when_no_parser(self) -> None:
        spec = {"input_grammar": [{"kind": "scalar", "name": "n", "type": "int"}]}
        self.assertEqual(input_parser.parse_input(spec, "7\n"), {"n": 7})

    def test_constraints_checked_on_parser_path(self) -> None:
        spec = {"input_parser_code": LINE_PARSER, "constraints_exec": ["n >= 5"]}
        parsed = input_parser.parse_input(spec, "2\nhi\n")
        self.assertTrue(input_parser.validate_input(spec, parsed))
        parsed_ok = input_parser.parse_input(spec, "9\nhi\n")
        self.assertEqual(input_parser.validate_input(spec, parsed_ok), [])

    # ── 门 V2：哨兵 / key 覆盖 / 确定性 ─────────────────────────────────────
    def _spec(self, code: str) -> dict:
        return {
            "input_parser_code": code,
            "input_grammar": [
                {"kind": "scalar", "name": "n", "type": "int"},
                {"kind": "scalar", "name": "m", "type": "int"},
            ],
            "constraints_exec": ["1 <= n <= 100"],
        }

    def test_parser_ignoring_tail_warns(self) -> None:
        # 哨兵是软门：只记录风险，不因此拒绝（避免把"容忍尾部垃圾"的正确 parser 误杀）
        warnings: list[str] = []
        errors = input_parser.check_samples(
            LAZY_PARSER, [{"input": "3 4\n1 2\n"}], self._spec(LAZY_PARSER), warnings
        )
        self.assertTrue(any("哨兵" in item for item in warnings), warnings)
        self.assertEqual(errors, [])

    def test_missing_key_rejected(self) -> None:
        errors = input_parser.check_samples(BAD_KEY_PARSER, [{"input": "3 4\n"}], self._spec(BAD_KEY_PARSER))
        self.assertTrue(any("缺少字段" in error for error in errors), errors)

    def test_nondeterministic_parser_rejected(self) -> None:
        errors = input_parser.check_samples(RANDOM_PARSER, [{"input": "3 4\n"}], self._spec(RANDOM_PARSER))
        self.assertTrue(any("非确定性" in error or "不一致" in error for error in errors), errors)

    def test_full_parser_accepted(self) -> None:
        self.assertEqual(
            input_parser.check_samples(FULL_PARSER, [{"input": "3 4\n"}], self._spec(FULL_PARSER)), []
        )

    def test_unevaluated_constraint_does_not_block(self) -> None:
        # constraints 也是 LLM 产物；变量名对不上时不能反过来判数据非法
        spec = {"input_grammar": [{"kind": "scalar", "name": "n", "type": "int"}],
                "constraints_exec": ["n <= foo"]}
        parsed = input_parser.parse_input(spec, "3\n")
        self.assertEqual(input_parser.validate_input(spec, parsed), [])


if __name__ == "__main__":
    unittest.main()
