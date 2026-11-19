"""预生成流水线（设计 §5，P4）：一次付费、产物永久复用。

顺序固定流水线，每阶段**独立落库、独立可重跑**：

    ① spec      题面 → spec JSON          门：schema + grammar 定义 + 样例回放解析
    ② generator 写 generator.py（不写数据）门：子进程执行 → grammar.parse+validate → 多 profile 覆盖
    ③ 参考解    ref_py / ref_cpp / brute   门：编译 → 样例全过 → 小规模对拍
    ④⑤ 分层数据 sample/boundary/exhaustive/stress + 期望输出（adversarial 需错解输入，由独立入口按需生成）
    ⑥ 提示      L1/L2/L3（LLM，禁代码）+ L4（引用已验证参考解，不调 LLM）

失败口径：任一阶段门不过 → `problems.status='failed'`、`failed_stage=<stage>`、错误原文入
`problem_stage_runs.error_text`，**不静默降级成 prepared**。

预算：`Budget(max_calls=40)`，每次 LLM 调用记 1；超限立刻 `BudgetExhausted` → failed_stage=budget。

文件分工（R4 行数门禁）：
- 数据结构/词表 → `prep_models.py`
- 纯判定规则  → `prep_rules.py`
- SQL 写入     → `prep_store.py`
- 本文件只做编排 + 每阶段的门
"""

from __future__ import annotations

import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from ..engine.datagen import grammar as grammar_engine
from ..engine.datagen import input_parser
from ..engine.runner import VERDICT_AC, run_case
from ..engine.verify import cross_check as cross_check_engine
from ..prompts import loader as prompts
from ..store.db import Database, get_db
from . import judge
from .prep_models import (
    DEFAULT_MAX_CALLS,
    DEFAULT_TOKEN_CAP,
    GENERATOR_TIMEOUT_MS,
    MAX_GENERATOR_ATTEMPTS,
    MAX_SPEC_ATTEMPTS,
    ORACLE_TIMEOUT_MS,
    SAMPLE_TIMEOUT_MS,
    STAGE_DATASETS,
    STAGE_GENERATOR,
    STAGE_HINTS,
    STAGE_REFERENCES,
    STAGE_SPEC,
    STATUS_PREPARED,
    STATUS_PREPARING,
    Budget,
    BudgetExhausted,
    PrepReport,
    RefArtifact,
    StageFailure,
    StageRecord,
)
from .prep_rules import (
    agree,
    available_langs,
    normalize_spec,
    peer_consistent,
    replay_samples,
    references_ready,
    spec_block,
    statement_excerpt,
    user_blocks,
)
from .prep_llm import LlmStageMixin
from .prep_store import PrepStore
from .solutions import build_all, explain, mark_primary, similar_local
from .spec_fallback import parser_fallback

__all__ = [
    "Budget",
    "BudgetExhausted",
    "PrepReport",
    "PrepService",
    "RefArtifact",
    "StageFailure",
    "StageRecord",
    "STAGE_SPEC",
    "STAGE_GENERATOR",
    "STAGE_REFERENCES",
    "STAGE_DATASETS",
    "STAGE_HINTS",
]


#: 分阶段思考策略与 tag→stage 解析见 `prep_models.THINKING_POLICY / stage_key`。


class PrepService(LlmStageMixin):
    """流水线编排器。`client` 允许注入（测试用桩），默认按环境变量建真实客户端。"""

    def __init__(
        self,
        db: Optional[Database] = None,
        *,
        client: Any = None,
        max_calls: int = DEFAULT_MAX_CALLS,
        budget: Optional[Budget] = None,
        call_cap: Optional[int] = None,
        deadline_ts: Optional[float] = None,
        verbose: bool = False,
        cross_cases: int = 12,
        exhaustive_cases: int = 30,
        stress_cases: int = 2,
        boundary_cases: int = 3,
    ) -> None:
        self.db = db or get_db()
        self.store = PrepStore(self.db)
        self.client = client
        # 批量跑时由外部注入共享 Budget（全局熔断）与单题 call_cap / deadline
        self.budget = budget or Budget(max_calls=max_calls)
        self.call_cap = int(call_cap if call_cap is not None else self.budget.max_calls)
        self.deadline_ts = deadline_ts
        self._calls_this_run = 0
        self._tokens_this_run = 0
        self.token_cap = int(DEFAULT_TOKEN_CAP)
        self.verbose = verbose
        self.cross_cases = max(1, cross_cases)
        self.exhaustive_cases = max(1, exhaustive_cases)
        self.stress_cases = max(1, stress_cases)
        self.boundary_cases = max(1, boundary_cases)
        self._notes: List[str] = []
        self._cross_result: Any = None
        self._cross_outputs: Dict[str, Dict[str, str]] = {}
        self._problem_id: Optional[int] = None
        self.allow_parser_fallback = True
        self.enable_llm_extras = True

    def use_problem(self, problem_id: int) -> None:
        """绑定题目并把 `client.recorder` 装好（实现见 `prep_llm.LlmStageMixin`）。"""
        LlmStageMixin.use_problem(self, problem_id)

    def prepare(self, problem_id: int) -> PrepReport:
        started = time.perf_counter()
        report = PrepReport(problem_id=int(problem_id))
        self._notes = []
        self._problem_id = int(problem_id)
        self._calls_this_run = 0
        self._tokens_this_run = 0
        problem = self.store.load_problem(int(problem_id))
        if problem is None:
            report.error = f"题目 {problem_id} 不存在"
            report.failed_stage = STAGE_SPEC
            return report
        samples = self.store.load_samples(int(problem_id))
        if not samples:
            report.error = "题目没有可用样例（ingest 未解出内联样例），拒绝开始预生成"
            report.failed_stage = STAGE_SPEC
            self.store.mark_failed(int(problem_id), STAGE_SPEC, report.error)
            return report
        # 客户端 + 记账回调（含失败/重试），与 use_problem 同一实现
        self.use_problem(int(problem_id))

        self.store.update_problem(int(problem_id), status=STATUS_PREPARING, failed_stage=None)
        try:
            spec = self._run_stage(report, STAGE_SPEC, self._stage_spec_reuse, problem, samples)
            report.spec = {key: value for key, value in spec.items() if key != "samples"}
            problem["spec_json"] = spec_block(spec, drop_samples=False)

            generator_code = self._run_stage(
                report, STAGE_GENERATOR, self._stage_generator, problem, spec
            )
            report.generator_ok = True

            refs = self._run_stage(
                report, STAGE_REFERENCES, self._stage_references, problem, spec, samples, generator_code
            )
            report.refs = list(refs.values())
            ready, missing = references_ready(refs)
            if not ready:
                raise StageFailure(STAGE_REFERENCES, f"参考解未通过对拍认证：{', '.join(missing)}")
            codes = {"generator": generator_code}
            for key, artifact in refs.items():
                codes[key] = artifact.code
            hits = self.store.check_injection_followed(int(problem_id), codes)
            if hits:
                self._note(f"生成代码命中题面注入标识符（injection_followed）：{sorted(hits)}")
            report.injection_followed = bool(hits)
            report.injection_hits_in_code = sorted(hits or [])
            # 参考解可信度审计：排在 datasets / hints / line_notes / explain 之前，
            # 被剔除的解不参与期望输出计算，也不会再写进提示和题解。
            # 纯 AST 判据，不跑代码、不调 LLM、无计时噪声。
            if self.enable_llm_extras:
                try:
                    from . import ref_audit

                    dropped = ref_audit.audit_claims(self, refs)
                    if dropped:
                        # save_references 会删掉不在 keep 里的 (lang, role)，同步清库
                        self.store.save_references(int(problem_id), refs)
                        report.refs = list(refs.values())
                except Exception as exc:  # noqa: BLE001 - 审计失败不阻断整题
                    self._note(f"参考解复杂度声明审计跳过：{str(exc)[:160]}")
            report.layers = self._run_stage(
                report, STAGE_DATASETS, self._stage_datasets, problem, int(problem_id), spec,
                generator_code, refs,
            )
            report.hints = self._run_stage(
                report, STAGE_HINTS, self._stage_hints, problem, spec, refs
            )
            if self.enable_llm_extras:
                try:
                    from . import line_notes

                    line_notes.generate(self, problem, spec, refs)
                except StageFailure as exc:
                    self._note(f"逐行代码解析跳过：{exc.message[:100]}")
            try:
                analysis = explain(self, problem, spec, refs) if self.enable_llm_extras else None
                if analysis:
                    similar_local(self, int(problem_id), analysis.get("tags") or [])
            except StageFailure as exc:
                self._note(f"题解生成失败：{exc.message[:120]}")
            self.store.update_problem(
                int(problem_id),
                status=STATUS_PREPARED,
                failed_stage=None,
                tier=spec.get("tier") or "A",
                lang_available=",".join(available_langs()),
            )
            report.ok = True
        except BudgetExhausted as exc:
            report.failed_stage = "budget"
            report.error = str(exc)
            self.store.mark_failed(int(problem_id), "budget", str(exc))
        except StageFailure as exc:
            report.failed_stage = exc.stage
            report.error = f"{exc.message}\n{exc.detail}".strip()
            self.store.mark_failed(int(problem_id), exc.stage, report.error)
        finally:
            report.elapsed_ms = (time.perf_counter() - started) * 1000.0
            report.budget = self.budget.to_dict()
            report.llm_calls = self._calls_this_run
            for note in self._notes:
                if note not in report.notes:
                    report.notes.append(note)
        return report

    # ── 阶段骨架 ────────────────────────────────────────────────────────────
    def _run_stage(self, report: PrepReport, stage: str, func: Any, *args: Any) -> Any:
        """跑一个阶段：记录 stage_runs、把任何异常收敛成 StageFailure。"""
        if self.deadline_ts is not None and time.monotonic() > self.deadline_ts:
            error = f"单题超过时限（进入阶段 {stage} 前 deadline 已到）"
            report.stages.append(StageRecord(stage, False, 0.0, error=error))
            self.store.record_stage_run(report.problem_id, stage, detail="", error=error)
            raise StageFailure("timeout", error)
        started = time.perf_counter()
        self._log(f"== 阶段 {stage} 开始 ==")
        try:
            value, detail = func(*args)
        except (BudgetExhausted, StageFailure) as exc:
            elapsed = (time.perf_counter() - started) * 1000.0
            error = getattr(exc, "detail", "") or str(exc)
            if isinstance(exc, BudgetExhausted):
                error = str(exc)
            report.stages.append(StageRecord(stage, False, elapsed, error=error))
            self.store.record_stage_run(report.problem_id, stage, detail="", error=error)
            raise
        except Exception as exc:  # noqa: BLE001 - 任何异常都归到该阶段，禁止静默
            elapsed = (time.perf_counter() - started) * 1000.0
            detail_text = traceback.format_exc(limit=3)[-1500:]
            report.stages.append(StageRecord(stage, False, elapsed, error=repr(exc)))
            self.store.record_stage_run(report.problem_id, stage, detail="", error=detail_text)
            raise StageFailure(stage, f"阶段异常：{exc}", detail=detail_text) from exc
        elapsed = (time.perf_counter() - started) * 1000.0
        report.stages.append(StageRecord(stage, True, elapsed, detail=detail))
        self.store.record_stage_run(report.problem_id, stage, detail=detail, error="")
        self._log(f"== 阶段 {stage} 通过（{elapsed:.0f}ms）：{detail}")
        return value

    # ── ① spec ──────────────────────────────────────────────────────────────
    def _stage_spec_reuse(
        self, problem: Dict[str, Any], samples: List[Dict[str, str]]
    ) -> Tuple[Dict[str, Any], str]:
        """已有有效 spec 时直接复用（0 次 LLM 调用），否则走 `_stage_spec` 生成。

        两个来源：上次成功流水线落库的 spec；`spec-set` 命令人工注入的 spec。
        复用前必须用当前样例重新回放——题面/样例可能被重新导入过。
        """
        raw = problem.get("spec_json")
        if raw:
            try:
                stored = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                stored = None
            if (
                isinstance(stored, dict)
                and (stored.get("input_grammar") or stored.get("input_parser_code"))
                and stored.get("output_type")
            ):
                spec = {key: value for key, value in stored.items() if key != "samples"}
                if not replay_samples(spec, samples):
                    spec["samples"] = samples
                    return spec, "复用已有 spec（样例回放通过，0 次 LLM 调用）"
                self._note("已有 spec 未通过当前样例回放，重新生成 spec")
        return self._stage_spec(problem, samples)

    def _stage_spec(
        self, problem: Dict[str, Any], samples: List[Dict[str, str]]
    ) -> Tuple[Dict[str, Any], str]:
        base_user = user_blocks(statement_excerpt(problem), samples)
        feedback = ""
        last_error = ""
        last_spec = None
        for attempt in range(1, MAX_SPEC_ATTEMPTS + 1):
            result = self._chat(prompts.SPEC, base_user + feedback, tag=f"spec#{attempt}")
            spec, problems = normalize_spec(result.json_obj or {})
            last_spec = spec
            # 构造题（any_solution）判题器没有自定义 checker：如实选它的题第一次就停，
            # 不重试、不进 parser 兜底（重试只会烧调用，模型也无从改）。
            if any(str(item).startswith("output_type=any_solution 暂不支持") for item in problems):
                raise StageFailure(STAGE_SPEC, "；".join(problems), detail="；".join(problems))
            if problems:
                last_error = "；".join(problems)
                feedback = f"\n\n上一次输出不合格：{last_error}\n请严格按字段要求重出完整 JSON。"
                continue
            sample_errors = replay_samples(spec, samples)
            if sample_errors:
                last_error = "样例回放失败：" + "；".join(sample_errors[:3])
                feedback = (
                    f"\n\n上一次的 input_grammar 无法解析题面样例：{last_error}\n"
                    "请修正节点顺序 / count 引用 / allow_empty 后重出完整 JSON。"
                )
                continue
            spec["samples"] = samples
            self.store.save_spec(int(problem["id"]), spec)
            detail = (
                f"tier={spec.get('tier')} checker={spec.get('checker_type')} "
                f"output_type={spec.get('output_type')} grammar=[{grammar_engine.describe(spec['input_grammar'])}] "
                f"constraints={len(spec.get('constraints_exec') or [])} 条 "
                f"样例回放 {len(samples)}/{len(samples)} 通过"
            )
            return spec, detail
        fallback, fallback_detail = parser_fallback(self, problem, last_spec, samples, last_error)
        if fallback is not None:
            return fallback, fallback_detail
        raise StageFailure(STAGE_SPEC, f"spec 连续 {MAX_SPEC_ATTEMPTS} 次不合格", detail=fallback_detail)

    def _normalize_spec(self, raw: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
        return normalize_spec(raw)

    # ── ② generator ─────────────────────────────────────────────────────────
    def _stage_generator(
        self, problem: Dict[str, Any], spec: Dict[str, Any]
    ) -> Tuple[str, str]:
        base_user = (
            user_blocks(statement_excerpt(problem), spec.get("samples") or [])
            + "\n\n## 已确认的 spec\n"
            + spec_block(spec)
        )
        feedback = ""
        last_error = ""
        last_code = ""
        for attempt in range(1, MAX_GENERATOR_ATTEMPTS + 1):
            result = self._chat(
                prompts.GENERATOR, base_user + feedback, tag=f"generator#{attempt}",
                max_tokens=16384, json_mode=False,
            )
            code = judge.extract_code(result.content, "python")
            last_code = code
            ok, detail = self._probe_generator(spec, code)
            if ok:
                self.store.save_generator(int(problem["id"]), code, verified=1)
                return code, detail
            last_error = detail
            feedback = (
                "\n\n上一次生成的 generator.py 未通过验证，错误如下：\n"
                + detail[:1800]
                + "\n上一次的代码：\n```python\n" + last_code[:6000] + "\n```\n请在这份代码上定位修改，不要从零重写。"
            )
        if last_code.strip():
            self.store.save_generator(int(problem["id"]), last_code, verified=0)
        raise StageFailure(
            STAGE_GENERATOR,
            f"generator 连续 {MAX_GENERATOR_ATTEMPTS} 次未通过",
            detail=last_error,
        )

    def _probe_generator(self, spec: Dict[str, Any], code: str) -> Tuple[bool, str]:
        """子进程跑 generator（禁进程内 exec），用 grammar 校验产出。"""
        if not code.strip():
            return False, "返回内容里没有代码"
        checks: List[str] = []
        for profile, seed in (("tiny", 1), ("edge", 2), ("max", 3), ("small", 4)):
            result = run_case("python", code, "", GENERATOR_TIMEOUT_MS, args=(str(seed), profile))
            verdict = str(result.get("verdict"))
            if verdict != VERDICT_AC:
                return False, (
                    f"profile={profile} seed={seed} 执行失败 verdict={verdict}\n"
                    f"stderr={str(result.get('stderr') or '')[:800]}"
                )
            text = str(result.get("stdout") or "")
            if not text.strip():
                return False, f"profile={profile} 没有输出任何数据"
            try:
                parsed = input_parser.parse_input(spec, text)
            except grammar_engine.GrammarError as exc:
                return False, f"profile={profile} 输出不符合 input_grammar：{exc.render()}"
            violations = input_parser.validate_input(spec, parsed)
            if violations:
                return False, f"profile={profile} 校验失败：{violations[0]}"
            checks.append(f"{profile}:{len(text)}B")
        return True, "profiles=" + ",".join(checks)

    # ── ③ 参考解 + 小规模对拍 ────────────────────────────────────────────────
    def _stage_references(
        self,
        problem: Dict[str, Any],
        spec: Dict[str, Any],
        samples: List[Dict[str, str]],
        generator_code: str,
    ) -> Tuple[Dict[str, RefArtifact], str]:
        sample_cases = [
            {"id": f"sample{i}", "input": sample.get("input", ""), "expected": sample.get("output", "")}
            for i, sample in enumerate(samples, start=1)
        ]
        refs: Dict[str, RefArtifact] = build_all(self, problem, spec, samples, sample_cases)
        self.store.save_references(int(problem["id"]), refs)

        tiny_cases, rejected = self._generate_cases(
            spec, generator_code, profile="tiny", count=self.cross_cases, seed_base=1000
        )
        cross_detail = self._cross_check(spec, refs, tiny_cases)
        if rejected:
            cross_detail += f"，生成被拒 {rejected} 条"
        self._verify_from_cross(refs, tiny_cases)
        mark_primary(refs)
        self.store.save_references(int(problem["id"]), refs)
        return refs, cross_detail

    def _build_solution(
        self,
        problem: Dict[str, Any],
        spec: Dict[str, Any],
        samples: List[Dict[str, str]],
        *,
        lang: str,
        role: str,
        template: str,
        cases: List[Dict[str, Any]],
        attempts: int,
        approach_hint: str = "",
    ) -> RefArtifact:
        artifact = RefArtifact(lang=lang, role=role, code="", samples_total=len(cases))
        base_user = (
            user_blocks(statement_excerpt(problem), samples)
            + f"\n\n## 目标语言\nlang = {lang}\n\n## 已确认的 spec\n"
            + spec_block(spec)
        )
        if approach_hint:
            base_user += f"\n\n## 指定解法\n{approach_hint}\n必须按这个思路实现，不要换成别的算法。"
        feedback = ""
        last_error = ""
        for attempt in range(1, attempts + 1):
            fresh = attempt % 2 == 0  # 偶数次独立重写（换思路），避免在同一错解上打转
            result = self._chat(
                template, base_user if fresh else base_user + feedback,
                tag=f"{role}_{lang}#{attempt}", max_tokens=32768, json_mode=False,
                temperature=0.0 if attempt == 1 else (0.8 if fresh else 0.35),
            )
            code = judge.extract_code(result.content, lang)
            last_code = code
            banned = self.store.load_tags(int(problem["id"])).get("injection_tokens") or []
            hit = [name for name in banned if name in code]
            if hit:
                last_error = f"代码使用了题面注入要求的标识符 {hit}，必须改名后重出"; feedback = "\n\n" + last_error
                continue
            batch = judge.run_solution(
                lang,
                code,
                cases,
                checker_type=spec.get("checker_type", "exact"),
                float_eps=float(spec.get("float_eps") or 1e-6),
                time_limit_ms=SAMPLE_TIMEOUT_MS,
            )
            if batch.compile_error:
                last_error = batch.compile_error
            elif not batch.all_accepted:
                last_error = judge.failure_hint(batch, cases=cases)
            else:
                artifact.code = code
                artifact.samples_passed = sum(1 for run in batch.runs if run.accepted)
                artifact.verify_record = {"sample_max_elapsed_ms": batch.max_elapsed_ms}
                return artifact
            feedback = ("\n\n上一次提交的代码未通过样例。错误与输入/期望/实际如下：\n" + last_error[:2200]
                        + "\n上一次的代码：\n```" + lang + "\n" + last_code[:6000] + "\n```\n请在这份代码上定位修改。")
        raise StageFailure(STAGE_REFERENCES, f"{role}({lang}) 连续 {attempts} 次未通过样例", detail=last_error[:2000])

    def _cross_check(
        self,
        spec: Dict[str, Any],
        refs: Dict[str, RefArtifact],
        tiny_cases: List[Dict[str, Any]],
    ) -> str:
        """小规模对拍：ref_py 为基准，ref_cpp / brute 对齐；分歧不投票，直接降级。"""
        self._cross_outputs = {}
        self._cross_result = None
        if not tiny_cases:
            return "无小规模用例，未对拍"
        outputs: Dict[str, Dict[str, str]] = {}
        elapsed: Dict[str, float] = {}
        for key, artifact in refs.items():
            batch = judge.run_solution(
                artifact.lang,
                artifact.code,
                tiny_cases,
                checker_type=spec.get("checker_type", "exact"),
                float_eps=float(spec.get("float_eps") or 1e-6),
                time_limit_ms=ORACLE_TIMEOUT_MS,
                compare_expected=False,
            )
            if batch.compile_error:
                self._note(f"{key} 对拍编译失败：{batch.compile_error[:200]}")
                continue
            outputs[key] = judge.batch_outputs(batch)
            elapsed[key] = batch.max_elapsed_ms
        baseline_key = "python/reference"
        if baseline_key not in outputs:
            return "ref_py 未产出，未对拍"
        candidates = {key: value for key, value in outputs.items() if key != baseline_key}
        result = cross_check_engine.cross_check(
            baseline_name=baseline_key,
            baseline=outputs[baseline_key],
            candidates=candidates,
            checker_type=spec.get("checker_type", "exact"),
            float_eps=float(spec.get("float_eps") or 1e-6),
        )
        self._cross_result = result
        self._cross_outputs = outputs
        spend = {key: round(value, 1) for key, value in elapsed.items()}
        return (
            f"{len(tiny_cases)} 例 × {sorted(outputs)} | 一致={result.ok} "
            f"分歧={len(result.disagreements)} 缺输出={result.missing_cases} "
            f"max_elapsed_ms={spend}"
        )

    def _verify_from_cross(
        self, refs: Dict[str, RefArtifact], tiny_cases: List[Dict[str, Any]]
    ) -> None:
        """把对拍结论回写每份产物的 verified（样例全过 && 对拍一致 才置 1）。"""
        result = self._cross_result
        outputs = self._cross_outputs
        for key, artifact in refs.items():
            artifact.cross_ok = False
            record = dict(artifact.verify_record)
            record.update(
                {
                    "samples_passed": artifact.samples_passed,
                    "samples_total": artifact.samples_total,
                    "small_cases": len(tiny_cases),
                    "max_n_covered": "tiny",
                    "disagreements": (result.disagreements[:3] if result else []),
                }
            )
            if artifact.samples_total and artifact.samples_passed < artifact.samples_total:
                artifact.verified = 0
                artifact.verification_level = cross_check_engine.TRUST_REFERENCE_ONLY
                artifact.degrade_reason = "样例未全过"
            elif peer_consistent(key, result, outputs):
                artifact.cross_ok = True
                artifact.verified = 1
                artifact.verification_level = cross_check_engine.TRUST_CROSS_VERIFIED
                artifact.degrade_reason = ""
            else:
                artifact.verified = 0
                artifact.verification_level = cross_check_engine.TRUST_REFERENCE_ONLY
                artifact.degrade_reason = "小规模对拍不一致（见 verify_record.disagreements）"
            artifact.verify_record = record

    # ── ④⑤ 分层数据 + 期望输出 ───────────────────────────────────────────────
    def _stage_datasets(
        self,
        problem: Dict[str, Any],
        problem_id: int,
        spec: Dict[str, Any],
        generator_code: str,
        refs: Dict[str, RefArtifact],
    ) -> Tuple[Dict[str, int], str]:
        """按「数据计划」生成：模型定分组/档位/条数，本地照单造并逐条过格式门。"""
        from . import data_plan

        layers: Dict[str, List[Dict[str, Any]]] = {
            "sample": [
                {
                    "id": f"sample{i}",
                    "input": sample.get("input", ""),
                    "expected": sample.get("output", ""),
                    "seed": None,
                    "size_class": "sample",
                }
                for i, sample in enumerate(spec.get("samples") or [], start=1)
                if str(sample.get("input") or "").strip()
            ],
            "boundary": [],
            "exhaustive": [],
            "stress": [],
        }
        fallback = data_plan.default_plan(
            self.boundary_cases, self.exhaustive_cases, self.stress_cases
        )
        payload = data_plan.request_plan(self, problem, spec) if self.enable_llm_extras else None
        plan = data_plan.normalize_plan(payload, fallback)

        raw_hand = list(plan.get("hand_cases") or [])
        hand = [item for item in raw_hand if data_plan.hand_case_ok(spec, item["input"])]
        if hand:
            layers["boundary"].extend(
                {
                    "id": f"hand{i}",
                    "input": item["input"],
                    "seed": None,
                    "size_class": "llm_edge",
                }
                for i, item in enumerate(hand, start=1)
            )
        if len(hand) < len(raw_hand):
            self._note(f"模型手写用例 {len(raw_hand) - len(hand)} 条未通过格式门，已丢弃")

        seen: set[str] = set()
        picked_detail: List[str] = []
        for index, group in enumerate(plan["groups"]):
            cases, rejected = self._generate_cases(
                spec,
                generator_code,
                profile=group["profile"],
                count=data_plan.attempt_budget(group["profile"], group["count"]),
                seed_base=data_plan.group_seed(index),
            )
            picked = data_plan.diverse_select(cases, group["count"], seen)
            layers[group["layer"]].extend(picked)
            picked_detail.append(f"{group['name']}({group['profile']})={len(picked)}/{group['count']}")
            if rejected:
                self._note(f"数据组「{group['name']}」被格式门拒绝 {rejected} 条")

        generated = sum(len(layers[layer]) for layer in data_plan.LAYERS)
        if plan.get("source") == "llm" and generated < data_plan.MIN_TOTAL:
            want = data_plan.MIN_TOTAL - generated
            extra, _rejected = self._generate_cases(
                spec, generator_code, profile="tiny", count=want + 2, seed_base=8000
            )
            layers["exhaustive"].extend(data_plan.diverse_select(extra, want, seen))
            self._note(f"有效数据不足 {data_plan.MIN_TOTAL} 条，已补生成 {want} 条")

        self._note(
            "adversarial 层需学生错解，由独立入口 generate_adversarial 按需生成，"
            "prepare 不产出"
        )
        rows: List[Dict[str, Any]] = []
        for layer, cases in layers.items():
            if not cases:
                if layer != "sample":
                    self._note(f"{layer} 层没有合法数据（生成全被拒），跳过")
                continue
            rows.extend(self._build_layer_rows(spec, refs, layer=layer, cases=cases))
        self.store.clear_generated_layers(problem_id)
        counts = self.store.save_cases(problem_id, rows)
        self.store.save_data_plan(problem_id, data_plan.summary(plan, counts))

        actual = sum(count for layer, count in counts.items() if layer != "sample")
        self._note(
            f"数据计划（{plan.get('source')}）：计划 {plan.get('total')} 条 / 实际 {actual} 条 | "
            + " ".join(picked_detail)
        )
        if plan.get("must_cover"):
            self._note("建议覆盖：" + "；".join(plan["must_cover"]))
        return counts, " ".join(f"{layer}={count}" for layer, count in sorted(counts.items()))

    def _build_layer_rows(
        self,
        spec: Dict[str, Any],
        refs: Dict[str, RefArtifact],
        *,
        layer: str,
        cases: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        checker = spec.get("checker_type", "exact")
        eps = float(spec.get("float_eps") or 1e-6)
        impls: Dict[str, Dict[str, str]] = {}
        for key, artifact in refs.items():
            if artifact.role == "brute" and layer == "stress":
                continue  # 大数据层不跑暴力解（无 oracle 意义，且会跑到天荒地老）
            batch = judge.run_solution(
                artifact.lang, artifact.code, cases, checker_type=checker,
                float_eps=eps, time_limit_ms=ORACLE_TIMEOUT_MS, compare_expected=False,
            )
            if batch.compile_error:
                self._note(f"{key} 在 {layer} 层编译失败，跳过该实现")
                continue
            impls[key] = judge.batch_outputs(batch)
        if not impls:
            raise StageFailure(STAGE_DATASETS, f"{layer} 层没有任何实现产出期望输出")

        rows: List[Dict[str, Any]] = []
        for case in cases:
            case_id = case["id"]
            values = {key: outputs.get(case_id) for key, outputs in impls.items()}
            verdict = self._agree(values, checker, eps)
            if verdict["output"] is None:
                self._note(f"{layer}/{case_id} 无一致期望输出，跳过")
                continue
            trust = cross_check_engine.decide_trust_level(
                layer=layer,
                agreeing_impls=verdict["agreeing"],
                disagreeing_impls=0 if verdict["consistent"] else 1,
            )
            source = cross_check_engine.source_of_truth_for(layer, impls=verdict["agreeing"])
            if layer == "sample":
                trust = cross_check_engine.TRUST_ORACLE
                source = cross_check_engine.SOURCE_SAMPLE
            if trust == cross_check_engine.TRUST_CONFLICT:
                self._note(f"{layer}/{case_id} 各实现分歧，标 conflict 待人工复核")
            rows.append(
                {
                    "layer": layer,
                    "input": case["input"],
                    "expected": verdict["output"],
                    "seed": case.get("seed"),
                    "size_class": case.get("size_class") or layer,
                    "source": source,
                    "trust": trust,
                    "impls": verdict["agreeing"],
                }
            )
        return rows

    def _agree(self, values: Dict[str, Optional[str]], checker: str, eps: float) -> Dict[str, Any]:
        return agree(values, checker, eps)

    # ── ⑥ 提示 ──────────────────────────────────────────────────────────────
    def _stage_hints(
        self,
        problem: Dict[str, Any],
        spec: Dict[str, Any],
        refs: Dict[str, RefArtifact],
    ) -> Tuple[List[Dict[str, Any]], str]:
        from .hint_service import run_hints_stage

        return run_hints_stage(self, problem, spec, refs)

    # ── 数据生成工具 ────────────────────────────────────────────────────────
    def _generate_cases(
        self,
        spec: Dict[str, Any],
        generator_code: str,
        *,
        profile: str,
        count: int,
        seed_base: int,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """子进程调 generator 出数据，逐条过 grammar + constraints（不合法即丢弃并计数）。"""
        cases: List[Dict[str, Any]] = []
        rejected = 0
        for offset in range(count):
            seed = seed_base + offset
            result = run_case(
                "python", generator_code, "", GENERATOR_TIMEOUT_MS, args=(str(seed), profile)
            )
            if str(result.get("verdict")) != VERDICT_AC:
                rejected += 1
                continue
            text = str(result.get("stdout") or "")
            try:
                parsed = input_parser.parse_input(spec, text)
            except grammar_engine.GrammarError:
                rejected += 1
                continue
            if input_parser.validate_input(spec, parsed):
                rejected += 1
                continue
            checks = grammar_engine.evaluate_constraints(spec.get("constraints_exec") or [], parsed)
            # 只对"确实求值为假"的约束拒数据；写不出来的约束是未判定，不能反过来卡死生成
            if any(check.evaluated and not check.ok for check in checks):
                rejected += 1
                continue
            cases.append(
                {
                    "id": f"{profile}{seed}",
                    "input": text,
                    "seed": seed,
                    "size_class": profile,
                }
            )
        return cases, rejected
