"""教学层（建模探针 + 五步提示）业务测试。

为什么要有这组测试（2026-11-25）：探针判定与梯子推进都是状态机，规则来自
《建模助手-探针与询问规范》与《编程题助教的五步提示法》——
判错层号/错型要精确匹配；追问恰好三轮；连续两题找出才免检；
核对型答错不放行；L3 挖空服务端校验；L4 没有提交不给。
这些规则一旦写错，学生看到的就是"助教乱放行/乱卡人"，必须全部离线桩验证。

模型只通过 responder 桩出场：探针包/提示包各一次生成调用，
回答字段的判定（L0 判断型/L1/L2/L3 兜底）走 slim_judge 桩，
反应走 slim_react 桩，L4 走 slim_ladder_l4 桩——判定失败必须降级回查表规则，
这条约束本身也在测试里。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autocode.slim2 import config, problem_purge, tutor
from autocode.slim2.llm_stage import StageRunner
from autocode.store.db import Database

STU = "202600000042"
OTHER = "202600000043"

#: 四栏表 + 四层探针的标准桩（与 slim_probe.md 契约一致）
GOOD_PROBE = {
    "state_type": "积累",
    "state_type_note": "转移单线且历史可丢：只留计数",
    "table": {"1": "输入：n 个数（范围 1~10⁴）", "2": "输出：出现最多的数",
              "3": "转移：逐个读入，计数加一；读完即停",
              "4": "cnt[v]（出现次数；读入累加；被②读）"},
    "probes": [
        {"layer": 1, "planted": "输入：n 个数", "error_type": "missing",
         "correct": "输入：n 个数（范围 1~10⁴）", "should_find": "规模上限没写",
         "followups": ["如果数最大是十亿，做法还一样吗？", "①里少了一个数字，哪个？", "这张①缺规模上限。"],
         "explain": "规模决定数组开多大。"},
        {"layer": 2, "planted": "输出：出现次数最多的数", "error_type": "missing",
         "correct": "输出：出现次数最多的数；多个取最小", "should_find": "漏了并列取最小的半句",
         "followups": ["如果最多出现了两次，输出哪个？", "②少了一句话，题面里找", "②漏了「多个输出最小」。"],
         "explain": "并列时取最小是题面半句话。"},
        {"layer": 3, "planted": "转移：逐个读入，计数加一", "error_type": "missing",
         "correct": "转移：逐个读入，计数加一；读完即停", "should_find": "停止条件缺了",
         "followups": ["什么时候停？", "③少了一句控制循环的话", "③缺停止条件。"],
         "explain": "没有停止条件循环不结束。"},
        {"layer": 4, "planted": "cnt[v]（出现次数；读入累加；被②读）＋ sum（已读总和；只写不读）",
         "error_type": "redundant", "correct": "cnt[v]（出现次数；读入累加；被②读）",
         "should_find": "sum 只写不读且可由 cnt 推出", "followups": ["sum 被谁读？",
         "④多了一行，删掉还能算吗？", "sum 是冗余行。"], "explain": "sum 可由 v×cnt[v] 推出。"},
    ],
}

#: L0~L3 提示包的标准桩（与 slim_ladder.md 契约一致）
GOOD_LADDER = {
    "tag": "计数最值",
    "attribution": "建模",
    "L0": {"kind": "check", "question": "输出的 3 是由什么决定的？",
           "expected": ["出现次数", "计数", "cnt"]},
    "L1": {"quote": "如果这样的数有多个，请输出其中最小的一个",
           "content": "先确认多个并列时输出哪一个，再想计数怎么统计"},
    "L2": {"fact": "出现次数由 cnt 数组统计，答案遍历 cnt 取最大"},
    "L3": {"lead": "三步骨架：",
           "blanks": [
               {"hint": "读入一个数 v，执行 ____", "answer": "cnt[v]++", "accept": ["cnt[v]++", "cnt[v]+=1", "cnt[v]=cnt[v]+1"]},
               {"hint": "全部读完 ____", "answer": "即停", "accept": ["即停", "停止"]},
               {"hint": "遍历 cnt 取 ____", "answer": "最大值", "accept": ["最大值", "最大", "max"]},
           ]},
}

GOOD_L4 = {"line": 5, "message": "第 5 行循环边界少了一，最后元素没被处理"}


def _stub(responses):
    """按阶段名取响应的离线桩；没给的阶段直接失败（不允许意外调用）。"""
    def responder(stage, user_content, system_text):
        if stage not in responses:
            raise AssertionError(f"离线桩收到了计划外的阶段调用：{stage}")
        return responses[stage]
    return responder


class TutorTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Database(Path(self._tmp.name) / "slim2.db")
        self.db.connect().executescript(config.SCHEMA_FILE.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _problem(self, title: str = "出现次数最多的数") -> int:
        import hashlib

        # statement 进 hash：多道题必须题面不同，否则撞 UNIQUE(bank_id, statement_hash)
        statement = f"{title}：给定 n 个正整数，找出出现次数最多的数。"
        digest = hashlib.sha256(statement.encode()).hexdigest()
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(bank_id, title, statement_md, statement_hash,"
                " samples_json, constraints_json, created_at, updated_at)"
                " VALUES(1, ?, ?, ?, '[]', '[]', '2026-11-25T00:00:00+00:00',"
                " '2026-11-25T00:00:00+00:00')",
                (title, statement, digest),
            )
        return int(cursor.lastrowid)

    def _runner(self, responses, problem_id: int) -> StageRunner:
        return StageRunner(self.db, problem_id=problem_id, budget=6,
                           responder=_stub(responses), user=STU)


class TestProbe(TutorTestBase):
    def test_generation_validates_and_stores(self) -> None:
        pid = self._problem()
        # 第一次给坏包（错型含糊），第二次给好包：反馈重试必须生效
        bad = {**GOOD_PROBE, "probes": [{**p, "error_type": "vague"} for p in GOOD_PROBE["probes"]]}
        runner = self._runner({"slim_probe": bad}, pid)
        try:
            tutor.ensure_probe_pack(self.db, runner, pid)
        except tutor.TutorError:
            pass  # 桩固定返回坏包，重试同样失败——下面换桩再验一次成功路径
        runner2 = self._runner({"slim_probe": GOOD_PROBE}, pid)
        pack = tutor.ensure_probe_pack(self.db, runner2, pid)
        self.assertEqual(pack["state_type"], "积累")
        self.assertEqual(len(pack["probes"]), 4)
        # 落库后第二次取不再调模型：桩没配 slim_probe，收到即断言失败
        again = tutor.ensure_probe_pack(self.db, self._runner({}, pid), pid)
        self.assertEqual(again["state_type"], "积累")

    def test_find_miss_reteach_cycle(self) -> None:
        pid = self._problem()
        tutor.ensure_probe_pack(self.db, self._runner({"slim_probe": GOOD_PROBE}, pid), pid)
        # 层 1 指错层 → 追问第 1 轮（反后果问）
        result = tutor.probe_answer(self.db, pid, STU, {"layer": 2, "error_type": "missing"})
        self.assertEqual(result["verdict"], "missed")
        self.assertEqual(result["round"], 1)
        self.assertIn("十亿", result["followup"])
        # 层对了、错型错了 → 追问第 2 轮（指位置）
        result = tutor.probe_answer(self.db, pid, STU, {"layer": 1, "error_type": "redundant"})
        self.assertEqual(result["verdict"], "missed")
        self.assertEqual(result["round"], 2)
        # 第三次没找出 → 代填 + 讲解，层推进到 2
        result = tutor.probe_answer(self.db, pid, STU, {"layer": 4, "error_type": "redundant"})
        self.assertEqual(result["verdict"], "reteach")
        self.assertIn("范围", result["correct"])
        status = tutor.probe_status(self.db, pid, STU)
        self.assertEqual(status["probe"]["active_layer"], 2)
        self.assertEqual(status["probe"]["passed"][0], "reteach")
        # 记录落了 (层 × 错因) 向量
        row = self.db.query_one(
            "SELECT * FROM probe_records WHERE user=? AND layer=1", (STU,)
        )
        self.assertEqual(row["outcome"], "reteach")
        self.assertEqual(row["error_type"], "missing")

    def test_found_advances_and_done(self) -> None:
        pid = self._problem()
        tutor.ensure_probe_pack(self.db, self._runner({"slim_probe": GOOD_PROBE}, pid), pid)
        correct = [
            {"layer": 1, "error_type": "missing"},
            {"layer": 2, "error_type": "missing"},
            {"layer": 3, "error_type": "missing"},
            {"layer": 4, "error_type": "redundant"},
        ]
        verdicts = [tutor.probe_answer(self.db, pid, STU, item)["verdict"] for item in correct]
        self.assertEqual(verdicts, ["found"] * 4)
        status = tutor.probe_status(self.db, pid, STU)
        self.assertTrue(status["probe"]["done"])
        again = tutor.probe_answer(self.db, pid, STU, {"layer": 1, "error_type": "missing"})
        self.assertEqual(again.get("code"), "ALREADY_DONE")
        # 一道题每层只判一次：连击 = 1，还不到免检线（免检要"连续两题"）
        self.assertEqual(
            self.db.query_one("SELECT tier FROM probe_tiers WHERE user=? AND layer=1", (STU,))["tier"],
            "detail",
        )

    def test_exempt_layer_skipped_on_next_problem(self) -> None:
        # 规范口径：连续两题通过 → 免检。两道题全找出后，第三道题的对应层直接放行
        for name in ("题 A", "题 B"):
            pid = self._problem(name)
            tutor.ensure_probe_pack(self.db, self._runner({"slim_probe": GOOD_PROBE}, pid), pid)
            for item in ({"layer": 1, "error_type": "missing"}, {"layer": 2, "error_type": "missing"},
                         {"layer": 3, "error_type": "missing"}, {"layer": 4, "error_type": "redundant"}):
                result = tutor.probe_answer(self.db, pid, STU, item)
                self.assertEqual(result["verdict"], "found", result)
        third = self._problem("题 C")
        result = tutor.start_probe(self.db, self._runner({"slim_probe": GOOD_PROBE}, third), third, STU)
        # 四层全部免检：发题即全过
        self.assertTrue(result["probe"]["done"])
        self.assertEqual(result["probe"]["passed"], ["exempt", "exempt", "exempt", "exempt"])
        # 免检放行也留痕
        outcomes = self.db.query_all(
            "SELECT outcome FROM probe_records WHERE user=? AND problem_id=?", (STU, third)
        )
        self.assertEqual([r["outcome"] for r in outcomes], ["exempt"] * 4)

    def test_planted_content_hidden_from_other_fields(self) -> None:
        pid = self._problem()
        tutor.ensure_probe_pack(self.db, self._runner({"slim_probe": GOOD_PROBE}, pid), pid)
        view = tutor.probe_status(self.db, pid, STU)["probe"]
        flat = str(view)
        # 正确答案、追问文案、讲解只存在 pack 里，绝不出接口
        self.assertNotIn("correct", flat)
        self.assertNotIn("followup", flat)
        self.assertNotIn("explain", flat)
        # 活跃层（第 1 层）显示埋错后的内容
        active = next(row for row in view["rows"] if row["status"] == "active")
        self.assertEqual(active["content"], "输入：n 个数")
        self.assertEqual(view["active_layer"], 1)


class TestLadder(TutorTestBase):
    def _start(self, pid: int, ladder=GOOD_LADDER):
        tutor.ensure_ladder_pack(self.db, self._runner({"slim_ladder": ladder}, pid), pid, STU)
        return tutor.ladder_present(self.db, pid, STU, "self")

    def _walk_to_l3(self, pid: int, runner: "StageRunner | None" = None) -> None:
        """L0→L2 走完，停在 L3 待答（调用方自己 present 后作答三个空）。"""
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "我觉得是由出现次数决定的"}, runner)
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "并列时应该取最小的一个输出"}, runner)
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "遍历 cnt 数组取最大"}, runner)

    def _walk_to_l4(self, pid: int, runner: "StageRunner | None" = None) -> None:
        """L0→L3 全部走完。每一级都要先「要提示」再作答（规范：上一问已答才能再点）。"""
        self._walk_to_l3(pid, runner)
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answers": ["cnt[v]++", "即停", "最大值"]}, runner)

    def test_pack_and_first_question(self) -> None:
        pid = self._problem()
        view = self._start(pid)
        self.assertEqual(view["level"], 0)
        self.assertFalse(view["answered"])
        self.assertEqual(view["content"]["question"], GOOD_LADDER["L0"]["question"])
        self.assertNotIn("expected", str(view["content"]))  # 关键词绝不外泄

    def test_check_type_held_then_passed(self) -> None:
        pid = self._problem()
        self._start(pid)
        held = tutor.ladder_answer(self.db, pid, STU, {"answer": "我不知道"})
        self.assertEqual(held["verdict"], "held")
        # 出现任一可接受说法即放行
        passed = tutor.ladder_answer(self.db, pid, STU, {"answer": "我觉得是由出现次数决定的"})
        self.assertEqual(passed["verdict"], "passed")
        self.assertEqual(passed["level"], 1)

    def test_blanks_validated_server_side(self) -> None:
        pid = self._problem()
        self._start(pid)
        tutor.ladder_answer(self.db, pid, STU, {"answer": "出现次数"})   # L0
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "并列时取最小的一个"})  # L1
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "遍历 cnt 取最大"})     # L2
        tutor.ladder_present(self.db, pid, STU, "self")
        wrong = tutor.ladder_answer(self.db, pid, STU, {"answers": ["cnt[v]--", "即停", "最大值"]})
        self.assertEqual(wrong["verdict"], "held")
        self.assertEqual(wrong["wrong_blank"], 1)
        right = tutor.ladder_answer(
            self.db, pid, STU, {"answers": ["cnt[v] = cnt[v] + 1", "停止", "最大"]}
        )
        self.assertEqual(right["verdict"], "passed")
        self.assertEqual(right["level"], 4)

    def test_react_generated_persisted_and_cleared(self) -> None:
        """判断级回答 → AI 反应生成、持久化到状态表、下一次 present 清空。"""
        pid = self._problem()
        runner = self._runner({
            "slim_ladder": GOOD_LADDER,
            "slim_react": {"reaction": "你抓住的是并列取最小这一点"},
        }, pid)
        tutor.ensure_ladder_pack(self.db, runner, pid, STU)
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "出现次数"})   # L0 核对型：无反应
        tutor.ladder_present(self.db, pid, STU, "self")
        result = tutor.ladder_answer(
            self.db, pid, STU, {"answer": "并列时输出最小的一个"}, runner)  # L1
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["reaction"], "你抓住的是并列取最小这一点")
        view = tutor.ladder_status(self.db, pid, STU)
        self.assertEqual(view["reaction"], "你抓住的是并列取最小这一点")
        tutor.ladder_present(self.db, pid, STU, "self")                 # 新一问：旧反应清空
        view = tutor.ladder_status(self.db, pid, STU)
        self.assertEqual(view["reaction"], "")

    def test_react_failure_does_not_break_verdict(self) -> None:
        """反应调用失败必须静默降级：判定照常返回，reaction 为空。"""
        pid = self._problem()
        tutor.ensure_ladder_pack(self.db, self._runner({"slim_ladder": GOOD_LADDER}, pid), pid, STU)
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "出现次数"})
        tutor.ladder_present(self.db, pid, STU, "self")
        # 桩没配 slim_react：一收到反应调用就断言失败——但外层必须吞掉
        result = tutor.ladder_answer(
            self.db, pid, STU, {"answer": "并列时输出最小的一个"},
            self._runner({"slim_ladder": GOOD_LADDER}, pid),
        )
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["reaction"], "")
        self.assertEqual(result["level"], 2)

    def test_judge_held_blocks_perfunctory_answer(self) -> None:
        """AI 判定 held：长但敷衍/跑题的回答不再放行（旧门槛 ≥4 字会放过）。"""
        pid = self._problem()
        runner = self._runner({
            "slim_ladder": GOOD_LADDER,
            "slim_judge": {"verdict": "held", "reaction": "想想多个并列时输出哪一个"},
        }, pid)
        tutor.ensure_ladder_pack(self.db, runner, pid, STU)
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "出现次数"})   # L0 核对型精确命中
        tutor.ladder_present(self.db, pid, STU, "self")
        result = tutor.ladder_answer(
            self.db, pid, STU, {"answer": "我不知道啊随便说说"}, runner)  # L1：够长但敷衍
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(result["level"], 1)
        self.assertEqual(result["reaction"], "想想多个并列时输出哪一个")
        view = tutor.ladder_status(self.db, pid, STU)
        self.assertEqual(view["reaction"], "想想多个并列时输出哪一个")

    def test_judge_passed_advances_with_reaction(self) -> None:
        """AI 判定 passed → 状态机推进到下一级；反应来自判定调用本身（不再二次调反应）。"""
        pid = self._problem()
        runner = self._runner({
            "slim_ladder": GOOD_LADDER,
            "slim_judge": {"verdict": "passed", "reaction": "你抓住的是并列取最小"},
        }, pid)
        tutor.ensure_ladder_pack(self.db, runner, pid, STU)
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "出现次数"})
        tutor.ladder_present(self.db, pid, STU, "self")
        result = tutor.ladder_answer(
            self.db, pid, STU, {"answer": "并列时输出最小的一个"}, runner)  # L1
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["level"], 2)
        self.assertEqual(result["reaction"], "你抓住的是并列取最小")

    def test_judge_failure_degrades_to_table_rules(self) -> None:
        """判定调用失败（输出不合格）→ 降级回查表规则，旧行为不变。"""
        pid = self._problem()
        runner = self._runner({
            "slim_ladder": GOOD_LADDER,
            "slim_judge": {"verdict": "bogus"},   # 非法判定 → 视为不可用
            "slim_react": {"reaction": "降级路径的反应"},
        }, pid)
        tutor.ensure_ladder_pack(self.db, runner, pid, STU)
        tutor.ladder_present(self.db, pid, STU, "self")
        tutor.ladder_answer(self.db, pid, STU, {"answer": "出现次数"})
        tutor.ladder_present(self.db, pid, STU, "self")
        result = tutor.ladder_answer(
            self.db, pid, STU, {"answer": "并列时输出最小的一个"}, runner)
        self.assertEqual(result["verdict"], "passed")   # ≥4 字放行（旧规则）
        self.assertEqual(result["reaction"], "降级路径的反应")

    def test_l3_ai_fallback_accepts_equivalent(self) -> None:
        """L3 快路径没中（表述不在 accept 清单但语义等价）→ AI 兜底放行。"""
        pid = self._problem()
        runner = self._runner({
            "slim_ladder": GOOD_LADDER,
            "slim_judge": {"verdict": "passed", "reaction": "三个空都对"},
        }, pid)
        tutor.ensure_ladder_pack(self.db, runner, pid, STU)
        self._walk_to_l3(pid, runner)
        tutor.ladder_present(self.db, pid, STU, "self")
        # 空2 "停下来吧" 不在 accept 清单，但语义等价 → AI 兜底放行
        result = tutor.ladder_answer(
            self.db, pid, STU, {"answers": ["cnt[v]++", "停下来吧", "最大值"]}, runner)
        self.assertEqual(result["verdict"], "passed")
        self.assertEqual(result["level"], 4)

    def test_l3_ai_fallback_held_gives_wrong_blank(self) -> None:
        """L3 AI 判 held 时必须带回第一个不等价的空号。"""
        pid = self._problem()
        runner = self._runner({
            "slim_ladder": GOOD_LADDER,
            "slim_judge": {"verdict": "held", "reaction": "空2 说的是别的事",
                           "wrong_blank": 2},
        }, pid)
        tutor.ensure_ladder_pack(self.db, runner, pid, STU)
        self._walk_to_l3(pid, runner)
        tutor.ladder_present(self.db, pid, STU, "self")
        result = tutor.ladder_answer(
            self.db, pid, STU, {"answers": ["cnt[v]++", "停下来吧", "最大值"]}, runner)
        self.assertEqual(result["verdict"], "held")
        self.assertEqual(result["wrong_blank"], 2)
        self.assertEqual(result["reaction"], "空2 说的是别的事")

    def test_l4_review_collected_and_persisted(self) -> None:
        """L4 v2：弯路回顾进返回值并持久化到 hint_states.reaction（前端反应块展示）。"""
        pid = self._problem()
        self._start(pid)
        self._walk_to_l4(pid)   # 无 runner：判断级走查表规则，会留下 held 留痕吗——不会，全过
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO submissions(problem_id, lang, code, code_hash, created_at)"
                " VALUES(?, 'python', 'print(1)', 'abc', '2026-11-25T00:00:00+00:00')",
                (pid,),
            )
        result = tutor.ladder_give_l4(
            self.db,
            self._runner({"slim_ladder_l4": {**GOOD_L4,
                          "review": "此前你判断在单价最低处一次买够，但比价要看整体"}},
                         pid),
            pid, STU,
        )
        self.assertEqual(result["verdict"], "l4")
        self.assertEqual(result["review"], "此前你判断在单价最低处一次买够，但比价要看整体")
        state = self.db.query_one(
            "SELECT reaction FROM hint_states WHERE problem_id=? AND user=?", (pid, STU))
        self.assertEqual(state["reaction"], "此前你判断在单价最低处一次买够，但比价要看整体")

    def test_l4_without_review_keeps_empty_reaction(self) -> None:
        """L4 模型没给 review（或无梯子记录）→ 不产出 review 字段，reaction 为空。"""
        pid = self._problem()
        self._start(pid)
        self._walk_to_l4(pid)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO submissions(problem_id, lang, code, code_hash, created_at)"
                " VALUES(?, 'python', 'print(1)', 'abc', '2026-11-25T00:00:00+00:00')",
                (pid,),
            )
        result = tutor.ladder_give_l4(
            self.db, self._runner({"slim_ladder_l4": GOOD_L4}, pid), pid, STU)
        self.assertNotIn("review", result)
        state = self.db.query_one(
            "SELECT reaction FROM hint_states WHERE problem_id=? AND user=?", (pid, STU))
        self.assertEqual(state["reaction"], "")

    def test_l4_requires_submission(self) -> None:
        pid = self._problem()
        self._start(pid)
        self._walk_to_l4(pid)
        locked = tutor.ladder_give_l4(
            self.db, self._runner({"slim_ladder_l4": GOOD_L4}, pid), pid, STU
        )
        self.assertEqual(locked.get("code"), "NO_SUBMISSION")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO submissions(problem_id, lang, code, code_hash, created_at)"
                " VALUES(?, 'python', ?, 'abc', '2026-11-25T00:00:00+00:00')",
                (pid, "n = int(input())\nbest = 0\ncnt = {}\nfor s in input().split():\n"
                      "    cnt[s] = cnt.get(s, 0) + 1\nfor v, c in cnt.items():\n"
                      "    if c > best:\n        best = c\nprint(best)"),
            )
        result = tutor.ladder_give_l4(
            self.db, self._runner({"slim_ladder_l4": GOOD_L4}, pid), pid, STU
        )
        self.assertEqual(result["verdict"], "l4")
        self.assertEqual(result["line"], 5)
        state = self.db.query_one(
            "SELECT done FROM hint_states WHERE problem_id=? AND user=?", (pid, STU)
        )
        self.assertTrue(int(state["done"]))
        log = self.db.query_one(
            "SELECT level, outcome, attribution FROM hint_log WHERE user=? AND problem_id=?"
            " ORDER BY id DESC LIMIT 1", (STU, pid)
        )
        self.assertEqual(log["level"], 4)
        self.assertEqual(log["outcome"], "l4")

    def test_tag_promotes_start_level_and_collapses(self) -> None:
        first = self._problem("题 A")
        self._start(first)
        self._walk_to_l4(first)
        # 走完一遍 → 同标签计数 = 1 → 第二次起点 L1
        second = self._problem("题 B")
        tutor.ensure_ladder_pack(self.db, self._runner({"slim_ladder": GOOD_LADDER}, second), second, STU)
        view = tutor.ladder_present(self.db, second, STU, "self")
        self.assertEqual(view["level"], 1)
        self.assertFalse(view["collapsed"])
        self.assertTrue(view["answered"] is False)
        # 第二遍走完 → 第三次塌缩：只保留 L1 与 L4
        tutor.ladder_answer(self.db, second, STU, {"answer": "并列取最小"})
        tutor.ladder_present(self.db, second, STU, "self")
        tutor.ladder_answer(self.db, second, STU, {"answer": "遍历取最大"})
        tutor.ladder_present(self.db, second, STU, "self")
        tutor.ladder_answer(self.db, second, STU, {"answers": ["cnt[v]++", "即停", "最大值"]})
        third = self._problem("题 C")
        tutor.ensure_ladder_pack(self.db, self._runner({"slim_ladder": GOOD_LADDER}, third), third, STU)
        view = tutor.ladder_present(self.db, third, STU, "self")
        self.assertEqual(view["level"], 1)
        self.assertTrue(view["collapsed"])

    def test_fail2_advances_level(self) -> None:
        pid = self._problem()
        self._start(pid)
        view = tutor.ladder_present(self.db, pid, STU, "fail2")
        self.assertEqual(view["level"], 1)
        self.assertFalse(view["answered"])

    def test_l4_waiting_state_has_no_question_content(self) -> None:
        """L4 + answered=0（fail2 后回到待提示）不得携带 L3 内容——否则前端渲染空白、按钮消失。"""
        pid = self._problem()
        self._start(pid)
        self._walk_to_l4(pid)                      # 走到 L4，answered=1
        with self.db.transaction() as conn:        # 模拟 fail2 点掉：answered=0
            conn.execute("UPDATE hint_states SET answered=0 WHERE problem_id=? AND user=?",
                         (pid, STU))
        view = tutor.ladder_status(self.db, pid, STU)
        self.assertEqual(view["level"], 4)
        self.assertFalse(view["answered"])
        self.assertNotIn("content", view)


class TestPurge(TutorTestBase):
    def test_purge_cascades_tutor_tables(self) -> None:
        pid = self._problem()
        tutor.ensure_probe_pack(self.db, self._runner({"slim_probe": GOOD_PROBE}, pid), pid)
        tutor.start_probe(self.db, self._runner({}, pid), pid, STU)
        tutor.ensure_ladder_pack(self.db, self._runner({"slim_ladder": GOOD_LADDER}, pid), pid, STU)
        tutor.ladder_present(self.db, pid, STU, {"trigger": "self"})
        problem_purge.purge_problem(self.db, pid)
        for table in ("probe_packs", "probe_progress", "probe_records",
                      "hint_packs", "hint_states", "hint_log"):
            count = self.db.query_scalar(f"SELECT COUNT(*) FROM {table}", (), -1)
            self.assertEqual(count, 0, f"{table} 没有随题清理")
        # 档位表是跨题的个人档案，不随题删
        self.assertGreater(
            self.db.query_scalar("SELECT COUNT(*) FROM probe_tiers WHERE user=?", (STU,), 0), 0
        )


if __name__ == "__main__":
    unittest.main()
