"""样例解释反推验证（T4）业务测试。

为什么要有这组测试（2026-11-25）：题面的"样例解释"没有任何自动校验，
而探针/梯子都从题面生成——解释错了学生的反馈就会错（真实事故：解释写错
购买方案，L0 把学生的正确答案判成 held）。验证链路是「参考代码锚定 →
抽断言 → 验证程序真跑」，任何一环放水（没锚定就验证、模型输出直接当真、
验证程序不真跑）都会让"质检"本身变成假话，必须离线桩全部验证。

模型只通过 responder 桩出场：D1R 参考代码 + T4 断言验证各一次调用；
判定一律来自沙箱真跑，这条约束本身也在测试里。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autocode.slim2 import config, explain_check, problem_purge
from autocode.slim2.llm_stage import StageRunner
from autocode.store.db import Database

STU = "202600000042"

#: 公路题的题面（含一段**正确**的样例解释）与样例
STATEMENT = """小苞开车从站点 1 到站点 n。站点 i 与 i+1 距离 d_i 公里，
每升油前进 d 公里，站点 i 油价 v_i 元，只能买整数升，油箱无限。

【样例 1 解释】最优方案下：小苞在站点 1 买了 3 升油，在站点 2 购买了 5 升油，
在站点 4 购买了 2 升油。"""
SAMPLES = [
    {"input": "5 4\n10 10 10 10\n9 8 9 6 5", "output": "79"},
]

#: 真实可跑的参考代码（贪心，样例输出 79——锚定必过）
REF_CODE = """import math
import sys

data = sys.stdin.buffer.read().split()
n, d = int(data[0]), int(data[1])
dist = [int(x) for x in data[2:1 + n]]
price = [int(x) for x in data[1 + n:1 + 2 * n]]
total = 0
fuel = 0.0
for i in range(n - 1):
    nxt = n - 1
    for j in range(i + 1, n):
        if price[j] < price[i]:
            nxt = j
            break
    need = sum(dist[i:nxt]) / d
    buy = math.ceil(need - fuel)
    if buy > 0:
        total += buy * price[i]
        fuel += buy
    fuel -= dist[i] / d
print(total)
"""

#: 验证程序桩：独立重算（枚举每站购买量上限的 DP 简化为贪心重算），
#: 输出与 slim_explain_check.md 契约一致的 JSON 列表。
def _verify_code(results):
    """results: [(claim, ok, actual)] → 输出这些结果的独立程序源码。"""
    data = json.dumps([{"claim": c, "ok": ok, "actual": a} for c, ok, a in results],
                      ensure_ascii=False)
    return "import json\nprint(json.dumps(" + json.dumps(data) + ", ensure_ascii=False))\n"

#: 真正独立重算的验证程序（不是回显桩）：重算贪心方案，比对断言
REAL_VERIFY = """import json, math
n, d = 5, 4
dist = [10, 10, 10, 10]
price = [9, 8, 9, 6, 5]
fuel = 0.0
plan = {}
total = 0
for i in range(n - 1):
    nxt = n - 1
    for j in range(i + 1, n):
        if price[j] < price[i]:
            nxt = j
            break
    need = sum(dist[i:nxt]) / d
    buy = math.ceil(need - fuel)
    if buy > 0:
        plan[i + 1] = buy
        total += buy * price[i]
        fuel += buy
    fuel -= dist[i] / d
claims = ["最优方案下：小苞在站点 1 买了 3 升油", "小苞在站点 2 购买了 5 升油"]
result = [
    {"claim": claims[0], "ok": plan.get(1) == 3, "actual": f"站点1买{plan.get(1)}升"},
    {"claim": claims[1], "ok": plan.get(2) == 5, "actual": f"站点2买{plan.get(2)}升"},
]
print(json.dumps(result, ensure_ascii=False))
"""

BAD_VERIFY = """import json
print("这不是 JSON 列表")
"""

UNSAFE_VERIFY = """import os
print(json.dumps([{"claim": "x", "ok": True, "actual": "1"}]))
"""


def _stub(responses):
    def responder(stage, user_content, system_text):
        if stage not in responses:
            raise AssertionError(f"离线桩收到了计划外的阶段调用：{stage}")
        return responses[stage]
    return responder


class ExplainTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _problem(self, statement: str = STATEMENT) -> int:
        import hashlib

        digest = hashlib.sha256(statement.encode()).hexdigest()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(bank_id, title, statement_md, statement_hash,"
                " samples_json, constraints_json, judge_type, created_at, updated_at)"
                " VALUES(1, '公路', ?, ?, ?, '[]', 'token',"
                " '2026-11-25T00:00:00+00:00', '2026-11-25T00:00:00+00:00')",
                (statement, digest, json.dumps(SAMPLES, ensure_ascii=False)),
            )
        return int(cursor.lastrowid)

    def _runner(self, responses, problem_id: int) -> StageRunner:
        return StageRunner(self.db, problem_id=problem_id, budget=6,
                           responder=_stub(responses), user=STU)


class TestExplainCheck(ExplainTestBase):
    def test_correct_explanation_passes(self) -> None:
        pid = self._problem()
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["最优方案下：小苞在站点 1 买了 3 升油", "小苞在站点 2 购买了 5 升油"],
                   "verify_code": REAL_VERIFY},
        }, pid)
        result = explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "checked")
        self.assertEqual(result["ok_count"], 2)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["note"], "")

    def test_wrong_explanation_flagged(self) -> None:
        pid = self._problem()
        # 解释声称站点 1 买 20 升（真实事故里的错误），重算只买到 3 升 → 不一致
        # 独立重算得站点1买3升，claim 声称 20 升 → 从文本抽数与实际比对
        wrong_verify = """import json, math
n, d = 5, 4
dist = [10, 10, 10, 10]
price = [9, 8, 9, 6, 5]
fuel = 0.0
plan = {}
total = 0
for i in range(n - 1):
    nxt = n - 1
    for j in range(i + 1, n):
        if price[j] < price[i]:
            nxt = j
            break
    need = sum(dist[i:nxt]) / d
    buy = math.ceil(need - fuel)
    if buy > 0:
        plan[i + 1] = buy
        total += buy * price[i]
        fuel += buy
    fuel -= dist[i] / d
claims = ["最优方案下：小苞在站点 1 买了 20 升油", "小苞在站点 2 购买了 5 升油"]
result = [
    {"claim": claims[0], "ok": plan.get(1) == 20, "actual": f"站点1实际买{plan.get(1)}升"},
    {"claim": claims[1], "ok": plan.get(2) == 5, "actual": f"站点2实际买{plan.get(2)}升"},
]
print(json.dumps(result, ensure_ascii=False))
"""
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["最优方案下：小苞在站点 1 买了 20 升油", "小苞在站点 2 购买了 5 升油"],
                   "verify_code": wrong_verify},
        }, pid)
        result = explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(result["ok_count"], 1)   # 第二条一致，第一条不一致
        self.assertNotEqual(result["note"], "")
        first = result["report_json"] and json.loads(result["report_json"])[0]
        self.assertFalse(first["ok"])

    def test_no_claims_is_reported_not_failed(self) -> None:
        pid = self._problem()
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": [], "verify_code": ""},
        }, pid)
        result = explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(result["status"], "no_claims")
        self.assertEqual(result["total"], 0)

    def test_unsafe_verify_code_rejected(self) -> None:
        pid = self._problem()
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["站点 1 买 3 升"], "verify_code": UNSAFE_VERIFY},
        }, pid)
        with self.assertRaises(explain_check.ExplainError) as ctx:
            explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(ctx.exception.code, "UNSAFE_CODE")

    def test_duplicate_report_entries_deduped(self) -> None:
        """模型写的验证程序偶尔重复输出判定块——按 claim 去重，以首次判定为准。"""
        pid = self._problem()
        dup_verify = (
            "import json\n"
            "entry = {'claim': '小苞在站点 1 买了 3 升油', 'ok': True, 'actual': '站点1买3升'}\n"
            "print(json.dumps([entry, entry, entry], ensure_ascii=False))\n"
        )
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["小苞在站点 1 买了 3 升油"],
                                   "verify_code": dup_verify},
        }, pid)
        result = explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(result["status"], "checked")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["ok_count"], 1)

    def test_verify_output_not_json_rejected(self) -> None:
        pid = self._problem()
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["站点 1 买 3 升"], "verify_code": BAD_VERIFY},
        }, pid)
        with self.assertRaises(explain_check.ExplainError) as ctx:
            explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(ctx.exception.code, "BAD_VERIFY_OUTPUT")

    def test_ref_code_failing_samples_blocks_check(self) -> None:
        pid = self._problem()
        bad_ref = REF_CODE.replace("print(total)", "print(total + 1)")
        runner = self._runner({
            "slim_reference": {"code": bad_ref},
        }, pid)
        with self.assertRaises(explain_check.ExplainError) as ctx:
            explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(ctx.exception.code, "REF_UNAVAILABLE")

    def test_no_samples_refused(self) -> None:
        import hashlib

        statement = "没有样例的题面。"
        digest = hashlib.sha256(statement.encode()).hexdigest()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(bank_id, title, statement_md, statement_hash,"
                " samples_json, constraints_json, created_at, updated_at)"
                " VALUES(1, '无样例', ?, ?, '[]', '[]',"
                " '2026-11-25T00:00:00+00:00', '2026-11-25T00:00:00+00:00')",
                (statement, digest),
            )
        pid = int(cursor.lastrowid)
        runner = self._runner({}, pid)
        with self.assertRaises(explain_check.ExplainError) as ctx:
            explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(ctx.exception.code, "NO_SAMPLES")

    def test_anchor_self_check_failed(self) -> None:
        """验证程序连样例输出都算不对时，一切断言判定都不可信——必须标记 self_check_failed。"""
        pid = self._problem()
        broken_verify = """import json
# 重算策略错了（逐段恰好买够，非全局最优），总价 82 != 79
total = 82
claims = ["最优方案下：小苞在站点 1 买了 3 升油"]
result = [{"claim": "（锚点）重算最终答案等于样例输出", "ok": False, "actual": "重算=82，样例=79"}]
print(json.dumps(result, ensure_ascii=False))
"""
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["最优方案下：小苞在站点 1 买了 3 升油"],
                                   "verify_code": broken_verify},
        }, pid)
        result = explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(result["status"], "self_check_failed")
        self.assertIn("不可信", result["note"])

    def test_detail_invariant_failed_downgrades_claims(self) -> None:
        """锚点过但明细不变量不过（总账对、分项明细错）→ 数量断言整体降级为不可信。"""
        pid = self._problem()
        # 复刻 2026-11-26 真机活案例：程序把第 3 段的 3 升记到站点 3，
        # 报"站点2买2升"→ 断言"站点2买5升"被误判 false；但总花费 79 锚点通过。
        buggy_verify = """import json
claims = ["最优方案下：小苞在站点 2 购买了 5 升油"]
result = [
    {"claim": "（锚点）重算最终答案等于样例输出", "ok": True, "actual": "重算=79，样例=79"},
    {"claim": "（明细自检）数量明细加权合计等于锚点答案", "ok": False, "actual": "合计=55，锚点=79"},
    {"claim": claims[0], "ok": False, "actual": "站点2购买2升"},
]
print(json.dumps(result, ensure_ascii=False))
"""
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["最优方案下：小苞在站点 2 购买了 5 升油"],
                                   "verify_code": buggy_verify},
        }, pid)
        result = explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(result["status"], "self_check_failed")
        self.assertIn("明细", result["note"])
        report = json.loads(result["report_json"])
        quantity = [r for r in report if "站点 2" in r["claim"]][0]
        self.assertIsNone(quantity["ok"])
        self.assertIn("不可信", quantity["actual"])

    def test_enumeration_selfcheck_failed_downgrades_claims(self) -> None:
        """v3 枚举自检：贪心明细没出现在枚举集合中 → 枚举漏解，数量断言整体不可信。"""
        pid = self._problem()
        # 复刻 2026-11-26 第二次真机活案例：贪心明细对（合计=79），但枚举漏掉真方案
        buggy_verify = """import json
claims = ["最优方案下：小苞在站点 2 购买了 5 升油"]
result = [
    {"claim": "（锚点）重算最终答案等于样例输出", "ok": True, "actual": "重算=79，样例=79"},
    {"claim": "（明细自检）数量明细加权合计等于锚点答案", "ok": True, "actual": "合计=79，锚点=79"},
    {"claim": "（枚举自检）贪心明细出现在枚举的最优方案集合中", "ok": False, "actual": "枚举出2个最优方案，贪心方案不在其中"},
    {"claim": claims[0], "ok": False, "actual": "所有最优方案中站点2购买量均不为5"},
]
print(json.dumps(result, ensure_ascii=False))
"""
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["最优方案下：小苞在站点 2 购买了 5 升油"],
                                   "verify_code": buggy_verify},
        }, pid)
        result = explain_check.run_explain_check(self.db, runner, pid, STU)
        self.assertEqual(result["status"], "self_check_failed")
        self.assertIn("枚举", result["note"])
        report = json.loads(result["report_json"])
        quantity = [r for r in report if "站点 2" in r["claim"]][0]
        self.assertIsNone(quantity["ok"])

    def test_report_is_stored_and_readable(self) -> None:
        pid = self._problem()
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["最优方案下：小苞在站点 1 买了 3 升油", "小苞在站点 2 购买了 5 升油"],
                   "verify_code": REAL_VERIFY},
        }, pid)
        explain_check.run_explain_check(self.db, runner, pid, STU)
        status = explain_check.explain_status(self.db, pid)
        self.assertTrue(status["checked"])
        self.assertEqual(status["ok_count"], 2)
        self.assertEqual(len(status["report"]), 2)
        self.assertEqual(status["report"][0]["claim"], "最优方案下：小苞在站点 1 买了 3 升油")

    def test_second_check_overwrites_first(self) -> None:
        pid = self._problem()
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": [], "verify_code": ""},
        }, pid)
        explain_check.run_explain_check(self.db, runner, pid, STU)
        simple_verify = """import json
print(json.dumps([{"claim": "站点 1 买 3 升", "ok": True, "actual": "3 升"}], ensure_ascii=False))
"""
        runner2 = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["站点 1 买 3 升"], "verify_code": simple_verify},
        }, pid)
        explain_check.run_explain_check(self.db, runner2, pid, STU)
        status = explain_check.explain_status(self.db, pid)
        self.assertEqual(len(status["report"]), 1)  # 覆盖写，不是追加

    def test_purge_cascades(self) -> None:
        pid = self._problem()
        runner = self._runner({
            "slim_reference": {"code": REF_CODE},
            "slim_explain_check": {"claims": ["最优方案下：小苞在站点 1 买了 3 升油", "小苞在站点 2 购买了 5 升油"],
                   "verify_code": REAL_VERIFY},
        }, pid)
        explain_check.run_explain_check(self.db, runner, pid, STU)
        problem_purge.purge_problem(self.db, pid)
        status = explain_check.explain_status(self.db, pid)
        self.assertFalse(status["checked"])


if __name__ == "__main__":
    unittest.main()
