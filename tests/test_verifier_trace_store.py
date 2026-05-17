import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestVerifierTraceStore(unittest.TestCase):
    def test_resolve_logs_dir_points_to_project_root_logs(self):
        from services.isolated_agent.verifier.agent_utils.trace import _resolve_logs_dir

        expected = Path(PROJECT_ROOT) / "logs"
        self.assertEqual(_resolve_logs_dir(), expected)

    def test_trace_store_updates_current_json_and_run_archive(self):
        from services.isolated_agent.verifier.agent_utils.trace import (
            append_agent_trace_code_version,
            append_agent_trace_message,
            finalize_agent_trace_run,
            start_agent_trace_run,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir)
            with patch(
                "services.isolated_agent.verifier.agent_utils.trace._resolve_logs_dir",
                return_value=logs_dir,
            ):
                run_id = start_agent_trace_run(
                    problem={"title": "两数之和", "description": "给定两个数，输出和。"},
                    mode="cross_validation",
                    meta={
                        "strategy": "cross_algo",
                        "test_case_count": 3,
                        "analysis": {"confidence": "high", "algorithms": [{"name": "a"}, {"name": "b"}]},
                    },
                )
                self.assertTrue(run_id)

                append_agent_trace_message(
                    run_id,
                    {"stage": "dual_gen_start", "message": "开始双算法并发生成", "mode": "cross_validation"},
                )
                append_agent_trace_code_version(
                    run_id,
                    slot="a",
                    phase="dual_generated",
                    code="print(1)\n",
                    success=True,
                    rounds=2,
                )
                finalize_agent_trace_run(
                    run_id,
                    {
                        "strategy": "cross_algo",
                        "verification_passed": False,
                        "reason": "mismatch_after_retry",
                        "skip_reason": "mismatch_after_retry",
                        "total_cases": 3,
                        "matched_cases": 2,
                        "mismatch_cases": [
                            {
                                "index": 2,
                                "stdin": "5 6\n",
                                "out_a": "11\n",
                                "out_b": "30\n",
                                "match": False,
                            }
                        ],
                        "analysis": {"confidence": "high"},
                    },
                    status="done",
                )

                trace_path = logs_dir / "agent_trace.json"
                self.assertTrue(trace_path.exists())
                rows = json.loads(trace_path.read_text(encoding="utf-8"))
                self.assertEqual(len(rows), 1)

                row = rows[0]
                self.assertEqual(row["run_id"], run_id)
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["mode"], "cross_validation")
                self.assertEqual(row["message"][0]["message"], "开始双算法并发生成")
                self.assertEqual(row["code"][0]["content"], "print(1)\n")
                self.assertEqual(row["summary"]["reason"], "mismatch_after_retry")

                archive_path = logs_dir / "agent_runs" / f"{run_id}.json"
                self.assertTrue(archive_path.exists())
                archive = json.loads(archive_path.read_text(encoding="utf-8"))
                self.assertEqual(archive["run_id"], run_id)
                self.assertEqual(archive["summary"]["matched_cases"], 2)

    def test_single_agent_trace_uses_same_array_schema(self):
        from services.isolated_agent.verifier.agent_utils.trace import (
            append_agent_trace_code_version,
            append_agent_trace_message,
            finalize_agent_trace_run,
            start_agent_trace_run,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir)
            with patch(
                "services.isolated_agent.verifier.agent_utils.trace._resolve_logs_dir",
                return_value=logs_dir,
            ):
                run_id = start_agent_trace_run(
                    problem={"title": "快乐数"},
                    mode="single_agent",
                    meta={"session_id": "sess-1"},
                )
                append_agent_trace_message(
                    run_id,
                    {"stage": "loop_start", "message": "开始自治修复", "mode": "single_agent"},
                )
                append_agent_trace_code_version(
                    run_id,
                    slot="single_agent",
                    phase="initial",
                    code="print(1)\n",
                    success=False,
                    rounds=0,
                )
                finalize_agent_trace_run(
                    run_id,
                    {"passed": True, "reason": "finalize_ok", "attempts": 2},
                )

                rows = json.loads((logs_dir / "agent_trace.json").read_text(encoding="utf-8"))
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["run_id"], run_id)
                self.assertEqual(row["mode"], "single_agent")
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["message"][0]["message"], "开始自治修复")
                self.assertEqual(row["summary"]["passed"], True)
                self.assertEqual(row["code"][0]["phase"], "initial")

    def test_solve_with_agent_writes_unified_trace(self):
        from services.isolated_agent.facade import solve_with_agent

        with tempfile.TemporaryDirectory() as temp_dir:
            logs_dir = Path(temp_dir)
            with patch(
                "services.isolated_agent.verifier.agent_utils.trace._resolve_logs_dir",
                return_value=logs_dir,
            ), patch(
                "services.isolated_agent.facade.run_autonomous_session",
                return_value={
                    "passed": True,
                    "final_code": "print(2)\n",
                    "summary": {"reason": "finalize_ok"},
                    "attempts": 1,
                },
            ):
                result = solve_with_agent(
                    problem={"title": "两数之和"},
                    candidate_code="print(1)\n",
                    llm_caller=lambda _: "{}",
                )

                self.assertTrue(result["passed"])
                rows = json.loads((logs_dir / "agent_trace.json").read_text(encoding="utf-8"))
                self.assertEqual(len(rows), 1)
                row = rows[0]
                self.assertEqual(row["mode"], "single_agent")
                self.assertEqual(row["summary"]["reason"], "finalize_ok")
                self.assertEqual(len(row["code"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
