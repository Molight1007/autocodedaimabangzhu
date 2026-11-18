"""成本记账测试（P0 回归）：补语言 / 补解析的 LLM 调用必须落 `llm_calls`。

历史 bug：只有 `prepare()` 里才设置 `_problem_id` 与 `client.recorder`；
`cmd_generate` / `cmd_enrich` / `cmd_add_lang` 用另一份 PrepService 调
`add_language` / `enrich_problem`，导致 method_analysis / code_notes 的调用
**一条都不落库**（`.tmp/csp_demo.db` 里 approaches=2、line_notes=9 但 llm_calls 无这两个 stage）。

口径：`use_problem(pid)` 同时绑题 + 装 recorder，任何入口都先调它。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

from autocode.app import enrich_service
from autocode.app.prep_models import TEMPERATURE_POLICY, stage_key
from autocode.app.prep_service import PrepService
from autocode.llm.client import ChatResult, ChatUsage
from autocode.prompts import loader
from autocode.store.db import Database, reset_default_db


class RecordingClient:
    """桩客户端：不联网，但像真客户端一样把**每次尝试**交给 recorder。"""

    def __init__(self, payload: Optional[Dict[str, Any]] = None) -> None:
        self.payload = payload if payload is not None else {}
        self.recorder: Any = None
        self.usage = ChatUsage()
        self.calls: List[Dict[str, Any]] = []
        # 诊断路径会读 `client.config.model`；桩也要有同名属性
        self.config = type("StubConfig", (), {"model": "stub-model"})()

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        json_mode: bool = True,
        tag: str = "",
        stage: str = "",
        problem_id: Optional[int] = None,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        prompt_hash: str = "",
        **_kw: Any,
    ) -> ChatResult:
        self.calls.append(
            {
                "tag": tag,
                "stage": stage,
                "problem_id": problem_id,
                "temperature": temperature,
                "prompt_hash": prompt_hash,
                "json_mode": json_mode,
            }
        )
        if self.recorder is not None:
            self.recorder(
                {
                    "problem_id": problem_id,
                    "stage": stage or tag.split("#")[0],
                    "attempt": 1,
                    "ok": 1,
                    "error_kind": "",
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "reasoning_tokens": 0,
                    "cache_hit_tokens": 0,
                    "total_tokens": 15,
                    "elapsed_ms": 1.0,
                    "tag": tag,
                    "prompt_hash": prompt_hash,
                }
            )
        self.usage.total_tokens += 15
        content = json.dumps(self.payload, ensure_ascii=False) if json_mode else "print(1)"
        return ChatResult(
            content=content,
            usage=ChatUsage(10, 5, 15),
            attempts=1,
            elapsed_ms=1.0,
            model="stub",
            json_obj=self.payload if json_mode else None,
            finish_reason="stop",
        )


class AccountingCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self._tmp.name) / "acct.db"
        self.env = patch.dict(os.environ, {"AUTOCODE_DB": str(self.db_path)}, clear=False)
        self.env.start()
        reset_default_db()
        self.db = Database(self.db_path)
        self.db.init_schema()
        # llm_calls.problem_id 有外键，测试桩也会真写库 → 先建题
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO problems(id, title, statement_md, status, created_at, updated_at)"
                " VALUES(7, '记账题', '记账题面', 'prepared', 'now', 'now')"
            )

    def tearDown(self) -> None:
        reset_default_db()
        self.db.close()
        self.env.stop()
        self._tmp.cleanup()

    def build(self, payload: Optional[Dict[str, Any]] = None) -> PrepService:
        return PrepService(db=self.db, client=RecordingClient(payload), max_calls=10)

    def rows(self, sql: str, params: tuple = ()) -> List[Any]:
        return list(self.db.query_all(sql, params))


class TestUseProblem(AccountingCase):
    def test_use_problem_binds_id_and_recorder(self) -> None:
        service = self.build()
        self.assertIsNone(service._problem_id)
        service.use_problem(7)
        self.assertEqual(service._problem_id, 7)
        self.assertTrue(callable(service.client.recorder), "recorder 必须装配成 store.record_llm_call")

    def test_chat_payload_carries_problem_id_and_stage(self) -> None:
        service = self.build()
        service.use_problem(7)
        service._chat(loader.HINTS, "题面", tag="hints#1")
        rows = self.rows("SELECT problem_id, stage, prompt_hash FROM llm_calls")
        self.assertEqual(len(rows), 1)
        self.assertEqual(int(rows[0]["problem_id"]), 7)
        self.assertEqual(rows[0]["stage"], "hints")
        # prompt 版本可追溯：16 位 sha256 前缀，且与模板原文一致
        expected = hashlib.sha256(loader.load(loader.HINTS).encode("utf-8")).hexdigest()[:16]
        self.assertEqual(rows[0]["prompt_hash"], expected)

    def test_temperature_policy_applied_by_stage(self) -> None:
        service = self.build()
        service.use_problem(7)
        service._chat(loader.SPEC, "题面", tag="spec#1")
        service._chat(loader.HINTS, "题面", tag="hints#1")
        service._chat(loader.METHOD_ANALYSIS, "题面", tag="method_analysis#7")
        temps = [call["temperature"] for call in service.client.calls]
        self.assertEqual(temps, [TEMPERATURE_POLICY["spec"], TEMPERATURE_POLICY["hints"],
                                 TEMPERATURE_POLICY["method_analysis"]])
        # 显式传值优先，不被策略覆盖（参考解重试会升温）
        service._chat(loader.REF_SOLUTION, "题面", tag="reference_python#2", temperature=0.8)
        self.assertEqual(service.client.calls[-1]["temperature"], 0.8)

    def test_stage_key_maps_diagnose(self) -> None:
        self.assertEqual(stage_key("student_diagnose"), "diagnose")
        self.assertEqual(stage_key("method_analysis#3"), "method_analysis")
        self.assertEqual(stage_key("code_notes#3_python_reference"), "code_notes")
        self.assertEqual(TEMPERATURE_POLICY["diagnose"], 0.2)


class TestEnrichAccounting(AccountingCase):
    """enrich 路径（不 prepare）：llm_calls 必须真的多出行、problem_id 正确。"""

    def setUp(self) -> None:
        super().setUp()
        payload = {
            "methods": [
                {"name": "模拟法", "idea": "直接模拟", "correctness": "逐步推演不漏",
                 "complexity": "O(1)", "pitfalls": ["边界"]}
            ],
            "summary": "读入后逐项模拟",
            "steps": [{"part": "读入", "explain": "读一行"}],
            "pitfalls": ["边界"],
        }
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO problems(id, title, statement_md, spec_json, status, created_at, updated_at)"
                " VALUES(1, '隔离题', '隔离题面', ?, 'prepared', 'now', 'now')",
                (json.dumps({"checker_type": "exact", "samples": [{"input": "1\n", "output": "1\n"}]}),),
            )
            conn.execute(
                "INSERT INTO reference_solutions(problem_id, lang, role, code, verified, verification_level,"
                " approach, complexity, is_primary, samples_expected, created_at)"
                " VALUES(1, 'python', 'reference', 'print(1)\n', 1,"
                " 'cross_verified', '模拟法', 'O(1)', 1, 1, 'now')"
            )
        self.payload = payload
        self.spec = {"checker_type": "exact", "samples": [{"input": "1\n", "output": "1\n"}]}
        self.problem = {"id": 1, "statement_md": "题面"}

    def test_enrich_problem_writes_llm_calls(self) -> None:
        before = int(self.db.query_scalar("SELECT COUNT(*) FROM llm_calls", (), 0))
        service = self.build(self.payload)
        service.use_problem(1)
        result = enrich_service.enrich_problem(service, self.problem, self.spec)
        self.assertTrue(result["ok"], result)
        after = int(self.db.query_scalar("SELECT COUNT(*) FROM llm_calls", (), 0))
        self.assertGreater(after, before, "补解析的调用必须落库")

        rows = self.rows("SELECT problem_id, stage FROM llm_calls ORDER BY id")
        self.assertTrue(all(int(row["problem_id"]) == 1 for row in rows), rows)
        stages = {row["stage"] for row in rows}
        self.assertIn("method_analysis", stages)
        self.assertIn("code_notes", stages)
        # 记的是真 stage，不是空串
        self.assertNotIn("", stages)


class TestCliEntrypointAccounting(TestEnrichAccounting):
    """S1：cli 入口必须真的绑题（把 `cli.py` 里的 `use_problem` 删掉，这条测试要挂）。"""

    def test_cmd_enrich_binds_problem_and_records(self) -> None:
        from types import SimpleNamespace

        from autocode import cli

        service = self.build(self.payload)
        args = SimpleNamespace(problem_id=[1], all=False, budget=8)
        with patch("autocode.cli._service", return_value=service):
            rc = cli.cmd_enrich(args)
        self.assertEqual(rc, 0)
        rows = self.rows("SELECT problem_id, stage FROM llm_calls")
        self.assertTrue(rows, "走 cli.cmd_enrich 必须产生 llm_calls 记录")
        self.assertTrue(all(int(row["problem_id"]) == 1 for row in rows), rows)
        self.assertNotIn("", {row["stage"] for row in rows})

    def test_missing_problem_id_exits_without_traceback(self) -> None:
        """G6：题号打错时打印一句并返回 1，不能抛 TypeError + traceback。"""
        from types import SimpleNamespace

        from autocode import cli

        args = SimpleNamespace(problem_id=[99999], all=False, budget=8)
        self.assertEqual(cli.cmd_enrich(args), 1)
        self.assertEqual(cli.cmd_add_lang(args), 1)

    def test_all_with_no_targets_is_not_failure(self) -> None:
        """N2：`--all` 没事可做应退 0（批量脚本/CI 不能把这当失败）。"""
        from types import SimpleNamespace

        from autocode import cli

        with patch("autocode.cli._targets", return_value=[]):
            self.assertEqual(
                cli.cmd_enrich(SimpleNamespace(problem_id=[], all=True, budget=8)), 0
            )
            self.assertEqual(
                cli.cmd_add_lang(SimpleNamespace(problem_id=[], all=True, budget=8, lang="c")), 0
            )
            # 显式题号一个都不存在才算失败
            self.assertEqual(
                cli.cmd_enrich(SimpleNamespace(problem_id=[1], all=False, budget=8)), 1
            )


class TestDiagnoseAccounting(AccountingCase):
    """B2：web 诊断必须落库（历史 bug：recorder 从未装配，stage/problem_id/prompt_hash 全是死参数）。"""

    def setUp(self) -> None:
        super().setUp()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO problems(id, title, statement_md, spec_json, status, created_at, updated_at)"
                " VALUES(1, '诊断题', '题面', ?, 'prepared', 'now', 'now')",
                (json.dumps({"checker_type": "exact", "samples": [{"input": "1\n", "output": "1\n"}]}),),
            )

    def test_diagnose_records_llm_call(self) -> None:
        from autocode.web import api

        stub = RecordingClient({
            "category": "边界遗漏", "what_wrong": "判断条件写错",
            "why": "输入 1 时输出 2", "fix_steps": ["改成 >="],
            "concept": "边界条件", "next_test": "n=1",
        })
        with patch("autocode.llm.client.LlmClient", return_value=stub):
            data = api.diagnose_student_code({
                "problem_id": 1, "lang": "python", "code": "print(2)",
                "case": {"input": "1\n", "expected": "1\n", "actual": "2\n", "verdict": "wa"},
                "model": "",
            })
        self.assertNotIn("error", data, data)
        rows = self.rows("SELECT problem_id, stage, prompt_hash FROM llm_calls")
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(int(rows[0]["problem_id"]), 1)
        self.assertEqual(rows[0]["stage"], "diagnose")
        self.assertEqual(len(str(rows[0]["prompt_hash"])), 16, rows[0]["prompt_hash"])


class TestPromptHashMigration(unittest.TestCase):
    def test_old_llm_calls_table_gets_prompt_hash_column(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = Path(tmp) / "legacy.db"
            db = Database(path)
            conn = db.connect()
            # 老库：llm_calls 没有 prompt_hash
            conn.execute(
                "CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, problem_id INTEGER, stage TEXT,"
                " attempt INTEGER, ok INTEGER NOT NULL DEFAULT 0, error_kind TEXT,"
                " prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,"
                " reasoning_tokens INTEGER NOT NULL DEFAULT 0, cache_hit_tokens INTEGER NOT NULL DEFAULT 0,"
                " total_tokens INTEGER NOT NULL DEFAULT 0, elapsed_ms REAL NOT NULL DEFAULT 0,"
                " tag TEXT, created_at TEXT)"
            )
            conn.execute("INSERT INTO llm_calls(problem_id, stage) VALUES(1, 'hints')")
            db.init_schema()
            columns = {
                str(row[1]) for row in db.query_all("PRAGMA table_info(llm_calls)")
            }
            self.assertIn("prompt_hash", columns)
            # 老行保留 + 默认空串（不重建表、不丢数据）
            row = db.query_one("SELECT stage, prompt_hash FROM llm_calls")
            self.assertEqual(row["stage"], "hints")
            self.assertEqual(row["prompt_hash"], "")
            db.close()


if __name__ == "__main__":
    unittest.main()
