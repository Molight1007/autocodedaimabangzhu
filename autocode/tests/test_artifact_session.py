"""ArtifactSession 测试（设计 §4 决策 8）。

守三件事：
1. 一题 5 个产物会话互不污染（改一个，其余 code/attempts/history/evidence 全不动）。
2. 不同 artifact_kind 的 can_finalize 规则真的不一样（generator / reference / brute 三套）。
3. 公开入口集合是闭合的——不存在"顺手改别的会话"的入口。
"""

from __future__ import annotations

import os
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from autocode.agent.session.artifact_session import (
    ARTIFACT_BRUTE,
    ARTIFACT_GENERATOR,
    ARTIFACT_KINDS,
    ARTIFACT_REF_C,
    ARTIFACT_REF_CPP,
    ARTIFACT_REF_PY,
    ArtifactSession,
    open_problem_sessions,
)

PROBLEM_ID = 42
REFERENCE_KINDS = (ARTIFACT_REF_C, ARTIFACT_REF_CPP, ARTIFACT_REF_PY)


class TestArtifactSessionIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = open_problem_sessions(PROBLEM_ID)

    def test_opens_five_distinct_sessions(self) -> None:
        self.assertEqual(len(self.sessions), 5)
        self.assertEqual(tuple(self.sessions), ARTIFACT_KINDS)
        self.assertEqual(len({id(session) for session in self.sessions.values()}), 5)
        for kind, session in self.sessions.items():
            self.assertEqual(session.artifact_kind, kind)
            self.assertEqual(session.problem_id, PROBLEM_ID)
            self.assertEqual(session.code, "")
            self.assertEqual(session.attempts, 0)
            self.assertEqual(session.history, [])
            self.assertFalse(session.finished)

    def test_mutating_one_session_does_not_touch_others(self) -> None:
        target = self.sessions[ARTIFACT_REF_PY]
        target.set_code("def solve():\n    return 1\n")
        target.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        target.record_attempt({"stage": "run", "passed": True})

        self.assertEqual(target.version, 2)
        self.assertEqual(target.attempts, 2)
        self.assertEqual(len(target.history), 2)

        for kind, other in self.sessions.items():
            if kind == ARTIFACT_REF_PY:
                continue
            with self.subTest(kind=kind):
                self.assertEqual(other.code, "")
                self.assertEqual(other.version, 1)
                self.assertEqual(other.attempts, 0)
                self.assertEqual(other.history, [])
                self.assertEqual(other.evidence["samples_seen"], {})
                self.assertFalse(other.evidence["run_ok"])
                ok, _ = other.can_finalize()
                self.assertFalse(ok)

    def test_all_five_can_be_finalized_independently(self) -> None:
        for kind, session in self.sessions.items():
            session.set_code(f"# {kind}\n")
            if kind == ARTIFACT_GENERATOR:
                session.record_attempt({"stage": "run", "passed": True})
                session.record_attempt({"stage": "grammar", "passed": True})
            elif kind in REFERENCE_KINDS:
                session.set_expected_samples(1)
                session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
                session.record_attempt({"stage": "cross_check", "passed": True})
            else:
                session.set_expected_samples(1)
                session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
            with self.subTest(kind=kind):
                ok, reason = session.can_finalize()
                self.assertTrue(ok, reason)

        for session in self.sessions.values():
            session.finalize()
        self.assertTrue(all(session.finished for session in self.sessions.values()))

    def test_incoming_history_is_copied(self) -> None:
        source = [{"version": 1, "stage": "sample", "passed": True, "label": "a"}]
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, history=source)
        source.append({"version": 1, "stage": "sample", "passed": False})
        source[0]["passed"] = False

        self.assertEqual(len(session.history), 1)
        self.assertTrue(session.history[0]["passed"])

        exported = session.history
        exported.append({"version": 9, "stage": "sample", "passed": True})
        self.assertEqual(len(session.history), 1)

    def test_public_entry_points_are_closed(self) -> None:
        """不允许出现接受其它会话 / 批量写入的新入口。"""
        public = {
            name
            for name in dir(ArtifactSession)
            if not name.startswith("_")
            and callable(getattr(ArtifactSession, name, None))
            and not isinstance(getattr(ArtifactSession, name), property)
        }
        self.assertEqual(
            public,
            {"set_code", "record_attempt", "can_finalize", "finalize", "snapshot", "set_expected_samples"},
        )


class TestCanFinalizeRulesByKind(unittest.TestCase):
    def test_generator_requires_run_and_grammar(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, code="print(1)")
        ok, reason = session.can_finalize()
        self.assertFalse(ok)
        self.assertIn("run", reason)

        session.record_attempt({"stage": "run", "passed": True})
        ok, reason = session.can_finalize()
        self.assertFalse(ok)
        self.assertIn("grammar", reason)

        session.record_attempt({"stage": "grammar", "passed": True})
        ok, reason = session.can_finalize()
        self.assertTrue(ok, reason)

    def test_generator_failed_run_does_not_count(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR)
        session.record_attempt({"stage": "run", "passed": False, "detail": "timeout"})
        session.record_attempt({"stage": "grammar", "passed": True})
        ok, _ = session.can_finalize()
        self.assertFalse(ok)

    def test_reference_requires_all_samples_plus_cross_check(self) -> None:
        for kind in REFERENCE_KINDS:
            with self.subTest(kind=kind):
                session = ArtifactSession(PROBLEM_ID, kind, samples_expected=2)
                ok, reason = session.can_finalize()
                self.assertFalse(ok)
                self.assertIn("未跑全", reason)

                session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
                session.record_attempt({"stage": "sample", "passed": True, "label": "sample#2"})
                ok, reason = session.can_finalize()
                self.assertFalse(ok)
                self.assertIn("cross_check", reason)

                session.record_attempt({"stage": "cross_check", "passed": True})
                ok, reason = session.can_finalize()
                self.assertTrue(ok, reason)

    def test_reference_partial_samples_blocked(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_REF_CPP, samples_expected=2)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session.record_attempt({"stage": "sample", "passed": False, "label": "sample#2"})
        session.record_attempt({"stage": "cross_check", "passed": True})
        ok, reason = session.can_finalize()
        self.assertFalse(ok)
        self.assertIn("1/2", reason)

    def test_cross_check_mismatch_cannot_be_overwritten_by_later_pass(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_REF_C, samples_expected=1)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session.record_attempt({"stage": "cross_check", "passed": False})
        session.record_attempt({"stage": "cross_check", "passed": True})
        ok, reason = session.can_finalize()
        self.assertFalse(ok)
        self.assertIn("对拍", reason)

    def test_brute_only_needs_sample_replay(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE)
        ok, _ = session.can_finalize()
        self.assertFalse(ok)

        session.set_expected_samples(1)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        ok, reason = session.can_finalize()
        self.assertTrue(ok, reason)

        session.record_attempt({"stage": "sample", "passed": False, "label": "sample#2"})
        ok, reason = session.can_finalize()
        self.assertFalse(ok)
        self.assertIn("1/2", reason)

    def test_set_code_resets_evidence_and_finished(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, code="print(1)")
        session.record_attempt({"stage": "run", "passed": True})
        session.record_attempt({"stage": "grammar", "passed": True})
        session.finalize()
        self.assertTrue(session.finished)
        self.assertEqual(session.attempts, 2)
        self.assertEqual(len(session.history), 2)

        session.set_code("print(2)")
        self.assertFalse(session.finished)
        self.assertEqual(session.version, 2)
        ok, _ = session.can_finalize()
        self.assertFalse(ok)
        # 历史保留用于审计，但证据已清空
        self.assertEqual(len(session.history), 2)
        self.assertFalse(session.evidence["run_ok"])

    def test_set_same_code_keeps_evidence(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, code="print(1)")
        session.record_attempt({"stage": "run", "passed": True})
        session.set_code("print(1)")
        self.assertEqual(session.version, 1)
        self.assertTrue(session.evidence["run_ok"])


class TestArtifactSessionValidation(unittest.TestCase):
    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ArtifactSession(PROBLEM_ID, "ref_java")

    def test_unknown_stage_rejected(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR)
        with self.assertRaises(ValueError):
            session.record_attempt({"stage": "vibes", "passed": True})

    def test_non_mapping_attempt_rejected(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR)
        with self.assertRaises(TypeError):
            session.record_attempt("passed")  # type: ignore[arg-type]

    def test_finalize_rejects_unverified(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE)
        with self.assertRaises(RuntimeError):
            session.finalize()
        self.assertFalse(session.finished)

    def test_snapshot_shape(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_REF_PY, code="print(1)")
        snapshot = session.snapshot()
        self.assertEqual(snapshot["problem_id"], PROBLEM_ID)
        self.assertEqual(snapshot["artifact_kind"], ARTIFACT_REF_PY)
        self.assertTrue(str(snapshot["code_hash"]).startswith("sha256:"))
        self.assertFalse(snapshot["finished"])


class TestPassedMustBeRealBool(unittest.TestCase):
    """S1：passed 是唯一决定放行的字段，禁止真值转换。"""

    def test_string_false_rejected(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR)
        with self.assertRaises(TypeError):
            session.record_attempt({"stage": "run", "passed": "false"})
        self.assertFalse(session.evidence["run_ok"])
        self.assertEqual(session.history, [])

    def test_zero_and_one_rejected(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR)
        for bad in (1, 0):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    session.record_attempt({"stage": "run", "passed": bad})
        self.assertFalse(session.evidence["run_ok"])

    def test_none_rejected(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR)
        with self.assertRaises(TypeError):
            session.record_attempt({"stage": "run", "passed": None})
        self.assertFalse(session.evidence["run_ok"])

    def test_real_bool_accepted(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR)
        session.record_attempt({"stage": "run", "passed": True})
        self.assertTrue(session.evidence["run_ok"])
        session.record_attempt({"stage": "run", "passed": False})
        self.assertFalse(session.evidence["run_ok"])

    def test_missing_passed_defaults_false(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR)
        session.record_attempt({"stage": "run"})
        self.assertFalse(session.evidence["run_ok"])

    def test_history_passed_must_be_bool(self) -> None:
        with self.assertRaises(TypeError):
            ArtifactSession(
                PROBLEM_ID,
                ARTIFACT_BRUTE,
                history=[{"version": 1, "stage": "sample", "passed": "false", "label": "a"}],
            )


class TestSampleEvidenceIsKeyedByLabel(unittest.TestCase):
    """S2：样例证据按 label 去重，重复录同一条不能冒充"多条全过"。"""

    def test_sample_requires_label(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE)
        with self.assertRaises(ValueError):
            session.record_attempt({"stage": "sample", "passed": True})
        with self.assertRaises(ValueError):
            session.record_attempt({"stage": "sample", "passed": True, "label": "   "})
        self.assertFalse(session.can_finalize()[0])

    def test_duplicate_label_does_not_count_as_two_samples(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=1)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        self.assertEqual(len(session.evidence["samples_seen"]), 1)
        self.assertEqual(session.attempts, 2)
        ok, reason = session.can_finalize()
        self.assertTrue(ok, reason)

    def test_duplicate_label_after_failure_stays_failed(self) -> None:
        """fail-closed：同一 label 曾失败则永久失败，不允许"重试到过"。"""
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=1)
        session.record_attempt({"stage": "sample", "passed": False, "label": "sample#1"})
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        self.assertFalse(session.evidence["samples_seen"]["sample#1"])
        ok, reason = session.can_finalize()
        self.assertFalse(ok)
        self.assertIn("0/1", reason)

    def test_reference_needs_distinct_passing_labels(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_REF_PY, samples_expected=1)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session.record_attempt({"stage": "cross_check", "passed": True})
        self.assertEqual(len(session.evidence["samples_seen"]), 1)
        self.assertTrue(session.can_finalize()[0])

        session2 = ArtifactSession(PROBLEM_ID, ARTIFACT_REF_PY, samples_expected=2)
        session2.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session2.record_attempt({"stage": "sample", "passed": False, "label": "sample#2"})
        session2.record_attempt({"stage": "cross_check", "passed": True})
        self.assertEqual(session2.evidence["samples_seen"], {"sample#1": True, "sample#2": False})
        self.assertFalse(session2.can_finalize()[0])

    def test_evidence_copy_does_not_leak_inner_dict(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        leaked = session.evidence
        leaked["samples_seen"]["injected"] = True
        self.assertEqual(list(session.evidence["samples_seen"]), ["sample#1"])


class TestFinishedIsTerminal(unittest.TestCase):
    """S3：finalize 之后不接受新证据，避免"已完成 + 证据失败"并存。"""

    def test_record_attempt_after_finalize_raises(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, code="print(1)")
        session.record_attempt({"stage": "run", "passed": True})
        session.record_attempt({"stage": "grammar", "passed": True})
        session.finalize()
        self.assertTrue(session.finished)

        with self.assertRaises(RuntimeError):
            session.record_attempt({"stage": "run", "passed": False})
        self.assertEqual(len(session.history), 2)
        self.assertEqual(session.attempts, 2)
        self.assertTrue(session.evidence["run_ok"])
        self.assertTrue(session.can_finalize()[0])

    def test_set_code_reopens_and_allows_records(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, code="print(1)")
        session.record_attempt({"stage": "run", "passed": True})
        session.record_attempt({"stage": "grammar", "passed": True})
        session.finalize()

        session.set_code("print(2)")
        self.assertFalse(session.finished)
        session.record_attempt({"stage": "run", "passed": True})
        self.assertEqual(len(session.history), 3)


class TestReloadFromHistory(unittest.TestCase):
    """S5：落库重开要恢复证据与 finished，不能把已通过的产物判成未达标。"""

    def test_snapshot_roundtrip(self) -> None:
        source = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, code="print(1)", samples_expected=2)
        source.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        source.record_attempt({"stage": "sample", "passed": True, "label": "sample#2"})
        source.finalize()
        self.assertTrue(source.can_finalize()[0])

        reloaded = ArtifactSession(
            PROBLEM_ID,
            ARTIFACT_BRUTE,
            code="print(1)",
            history=source.history,
            finished=source.finished,
            samples_expected=source.samples_expected,
            version=source.version,
        )
        self.assertEqual(reloaded.snapshot(), source.snapshot())
        self.assertEqual(reloaded.can_finalize(), source.can_finalize())
        self.assertEqual(reloaded.evidence["samples_seen"], {"sample#1": True, "sample#2": True})
        self.assertEqual(reloaded.attempts, source.attempts)
        self.assertEqual(reloaded.samples_expected, 2)

    def test_reload_without_expected_is_rejected(self) -> None:
        """S2 新增契约：分母属落库字段，漏存时重开必须报错，不能"假已完成"。"""
        source = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=2)
        source.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        source.record_attempt({"stage": "sample", "passed": True, "label": "sample#2"})
        source.finalize()

        with self.assertRaises(ValueError) as ctx:
            ArtifactSession(
                PROBLEM_ID, ARTIFACT_BRUTE, history=source.history, finished=source.finished
            )
        self.assertIn("samples_expected", str(ctx.exception))

    def test_unfinished_reload_without_expected_still_judged_unqualified(self) -> None:
        """未 finalize 的重开：不报错，但判据必须如实为"未声明分母"。"""
        source = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=2)
        source.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        source.record_attempt({"stage": "sample", "passed": True, "label": "sample#2"})

        reloaded = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, history=source.history)
        ok, reason = reloaded.can_finalize()
        self.assertFalse(ok)
        self.assertIn("samples_expected", reason)

    def test_evidence_replay_keeps_failures(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=1)
        session.record_attempt({"stage": "sample", "passed": False, "label": "sample#1"})
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})

        reloaded = ArtifactSession(
            PROBLEM_ID,
            ARTIFACT_BRUTE,
            history=session.history,
            finished=session.finished,
            samples_expected=1,
        )
        self.assertFalse(reloaded.evidence["samples_seen"]["sample#1"])
        self.assertFalse(reloaded.can_finalize()[0])

    def test_other_version_records_not_replayed(self) -> None:
        """N1：重开必须按快照 version 回灌，否则 v1 证据会被套到 v2 代码上。"""
        v1_records = [
            {"version": 1, "stage": "run", "passed": True, "label": ""},
            {"version": 1, "stage": "grammar", "passed": True, "label": ""},
        ]
        v2_records = [{"version": 2, "stage": "run", "passed": False, "label": ""}]

        at_v2 = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, history=v1_records + v2_records, version=2)
        self.assertEqual(at_v2.version, 2)
        self.assertFalse(at_v2.evidence["run_ok"], "v2 的失败记录必须生效")
        self.assertFalse(at_v2.evidence["grammar_ok"], "v1 的 grammar 不得回放")
        self.assertFalse(at_v2.can_finalize()[0])

        at_v1 = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, history=v1_records, version=1)
        self.assertTrue(at_v1.evidence["run_ok"])
        self.assertTrue(at_v1.evidence["grammar_ok"])
        self.assertTrue(at_v1.can_finalize()[0])

    def test_history_newer_than_version_rejected(self) -> None:
        """漏传 version 会重现 N1 的 fail-open，这里把它变成显式报错。"""
        history = [
            {"version": 1, "stage": "run", "passed": True, "label": ""},
            {"version": 2, "stage": "run", "passed": False, "label": ""},
        ]
        with self.assertRaises(ValueError) as ctx:
            ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, history=history)  # version 默认 1
        self.assertIn("version", str(ctx.exception))

    def test_finished_must_be_bool(self) -> None:
        with self.assertRaises(TypeError):
            ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, finished="true")  # type: ignore[arg-type]

    def test_reopen_after_set_code_does_not_replay_old_version(self) -> None:
        """N1 回归：换码后 v2 实际失败，重开不得把 v1 的通过证据套上来（fail-open）。"""
        session = ArtifactSession(9, ARTIFACT_GENERATOR, code="v1")
        session.record_attempt({"stage": "run", "passed": True})
        session.record_attempt({"stage": "grammar", "passed": True})
        session.set_code("v2")
        session.record_attempt({"stage": "run", "passed": False})
        ok_original, reason_original = session.can_finalize()
        self.assertFalse(ok_original)

        reopened = ArtifactSession(
            9,
            ARTIFACT_GENERATOR,
            code="v2",
            history=session.history,
            finished=session.finished,
            version=session.version,
        )
        self.assertEqual(reopened.snapshot()["version"], 2)
        self.assertFalse(reopened.evidence["run_ok"], "v1 的 run 证据不得回放到 v2")
        self.assertFalse(reopened.evidence["grammar_ok"], "v1 的 grammar 证据不得回放到 v2")
        self.assertFalse(reopened.can_finalize()[0])
        self.assertEqual(reopened.can_finalize(), (ok_original, reason_original))
        self.assertEqual(reopened.snapshot(), session.snapshot())

    def test_version_validation(self) -> None:
        for bad in (0, -1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, version=bad)
        for bad in (True, "2", 2.0):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, version=bad)  # type: ignore[arg-type]


class TestSampleCoverage(unittest.TestCase):
    """S2 残留：只跑 1 条不得放行——"全过"必须有分母。"""

    def test_undeclared_expected_blocks_brute(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        ok, reason = session.can_finalize()
        self.assertFalse(ok, "只跑 1 条且未声明分母，不得放行")
        self.assertIn("未声明应跑样例数", reason)
        self.assertIn("samples_expected", reason)

    def test_undeclared_expected_blocks_reference(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_REF_PY)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session.record_attempt({"stage": "cross_check", "passed": True})
        ok, reason = session.can_finalize()
        self.assertFalse(ok)
        self.assertIn("未声明应跑样例数", reason)

    def test_partial_coverage_blocked(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=2)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        ok, reason = session.can_finalize()
        self.assertFalse(ok)
        self.assertIn("样例未跑全", reason)
        self.assertIn("已跑 1 / 应跑 2", reason)

    def test_full_coverage_passes(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=2)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#2"})
        ok, reason = session.can_finalize()
        self.assertTrue(ok, reason)

    def test_more_samples_than_expected_is_allowed(self) -> None:
        """多跑不算错：不裁剪、不报错，只看"跑满且全过"。"""
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=2)
        for idx in range(1, 4):
            session.record_attempt({"stage": "sample", "passed": True, "label": f"sample#{idx}"})
        self.assertEqual(len(session.evidence["samples_seen"]), 3)
        ok, reason = session.can_finalize()
        self.assertTrue(ok, reason)

    def test_set_expected_samples_validation(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE)
        for bad in (0, -1):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    session.set_expected_samples(bad)
        for bad in (True, "2", 2.0, None):
            with self.subTest(value=bad):
                with self.assertRaises(TypeError):
                    session.set_expected_samples(bad)  # type: ignore[arg-type]
        self.assertEqual(session.samples_expected, 0)
        self.assertEqual(session.set_expected_samples(2), 2)

    def test_constructor_rejects_negative_expected(self) -> None:
        with self.assertRaises(ValueError):
            ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=-1)
        with self.assertRaises(TypeError):
            ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=True)  # type: ignore[arg-type]

    def test_expected_survives_set_code(self) -> None:
        """换码清证据，但"应跑几条"是题级声明，不该被清掉。"""
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=2)
        session.set_code("print(2)")
        self.assertEqual(session.samples_expected, 2)

    def test_set_expected_after_finish_raises(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=1)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        session.finalize()
        with self.assertRaises(RuntimeError):
            session.set_expected_samples(2)

    def test_generator_unaffected_by_sample_coverage(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, code="print(1)")
        session.record_attempt({"stage": "run", "passed": True})
        session.record_attempt({"stage": "grammar", "passed": True})
        ok, reason = session.can_finalize()
        self.assertTrue(ok, reason)
        self.assertEqual(session.samples_expected, 0)

    def test_open_problem_sessions_forwards_expected(self) -> None:
        sessions = open_problem_sessions(PROBLEM_ID, samples_expected=5)
        self.assertTrue(all(item.samples_expected == 5 for item in sessions.values()))

    def test_set_expected_after_samples_recorded_raises(self) -> None:
        """N2-①：跑过样例后不能再调分母（调高也不行，必须 set_code 重开）。"""
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        with self.assertRaises(RuntimeError):
            session.set_expected_samples(1)
        self.assertEqual(session.samples_expected, 0)
        self.assertFalse(session.can_finalize()[0])

    def test_set_expected_up_after_samples_recorded_also_raises(self) -> None:
        """调高同样拒绝，避免"跑 1 条后把分母写大再改"绕开单调约束。"""
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=5)
        session.record_attempt({"stage": "sample", "passed": True, "label": "sample#1"})
        with self.assertRaises(RuntimeError):
            session.set_expected_samples(20)
        self.assertEqual(session.samples_expected, 5)

        # 唯一出口：set_code 重开（会清空 samples_seen，之后可重新声明）
        session.set_code("print(2)")
        self.assertEqual(session.set_expected_samples(20), 20)

    def test_expected_can_only_increase(self) -> None:
        """N2-②：只增不减 —— 跑 1 条被拒后把分母从 5 改成 1 必须 raise。"""
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE, samples_expected=5)
        with self.assertRaises(ValueError):
            session.set_expected_samples(1)
        self.assertEqual(session.samples_expected, 5)
        self.assertEqual(session.set_expected_samples(5), 5)
        self.assertEqual(session.set_expected_samples(7), 7)

    def test_expected_can_be_set_from_undeclared(self) -> None:
        session = ArtifactSession(PROBLEM_ID, ARTIFACT_BRUTE)
        self.assertEqual(session.set_expected_samples(5), 5)


class TestFinishedDataConsistency(unittest.TestCase):
    """N3：finished=True 必须能被证据复核，防止"假已完成"。"""

    def test_finished_without_evidence_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, finished=True, history=[])
        message = str(ctx.exception)
        self.assertIn("不自洽", message)
        self.assertIn("finished=True", message)

    def test_finished_with_full_evidence_loads(self) -> None:
        source = ArtifactSession(PROBLEM_ID, ARTIFACT_GENERATOR, code="print(1)")
        source.record_attempt({"stage": "run", "passed": True})
        source.record_attempt({"stage": "grammar", "passed": True})
        source.finalize()

        reopened = ArtifactSession(
            PROBLEM_ID,
            ARTIFACT_GENERATOR,
            code="print(1)",
            history=source.history,
            finished=source.finished,
            version=source.version,
        )
        self.assertTrue(reopened.finished)
        self.assertTrue(reopened.can_finalize()[0])
        self.assertEqual(reopened.snapshot(), source.snapshot())

    def test_finished_reference_without_cross_check_rejected(self) -> None:
        """证据只有样例、没有对拍记录，不得算"已完成"。"""
        history = [
            {"version": 1, "stage": "sample", "passed": True, "label": "sample#1"},
        ]
        with self.assertRaises(ValueError):
            ArtifactSession(
                PROBLEM_ID,
                ARTIFACT_REF_PY,
                history=history,
                finished=True,
                samples_expected=1,
            )


if __name__ == "__main__":
    unittest.main()
