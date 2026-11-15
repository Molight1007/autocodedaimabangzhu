"""批量预生成（`cli prepare --all`）：单题失败不中断 + 全局预算熔断 + 汇总归类。

三条硬规则（P4 批量口径）：
1. **单题失败不中断**：每题独立 try，失败落 `status=failed` + `failed_stage` + 错误摘要。
2. **全局预算 250 次**：共享一个 `Budget`，到顶即停，剩余题标 `skipped_budget`（不改 DB 状态，
   避免把"没跑"伪装成"跑失败"）。
3. **单题时限**：`deadline_ts` 注入 `PrepService`，在每次 LLM 调用与阶段边界检查；
   超时记 `failed_stage=timeout`。

失败归类（用于统计表）：
- `grammar_unsupported`：spec 阶段 grammar 相关错误（表达力不足，不是 LLM 乱写）
- `spec_invalid`：spec 阶段其它错误
- `ref_sample_fail`：参考解/暴力解样例不过
- `cross_mismatch`：对拍分歧
- `generator_fail`：generator 未通过
- `timeout` / `budget` / `other`
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from ..store.db import Database
from .prep_models import Budget, PrepReport
from .prep_service import PrepService

DEFAULT_BUDGET_PER = 20
DEFAULT_BUDGET_TOTAL = 250
DEFAULT_TIMEOUT_SEC = 180

CLASS_GRAMMAR = "grammar_unsupported"
CLASS_SPEC = "spec_invalid"
CLASS_REF_SAMPLE = "ref_sample_fail"
CLASS_CROSS = "cross_mismatch"
CLASS_GENERATOR = "generator_fail"
CLASS_TIMEOUT = "timeout"
CLASS_BUDGET = "budget"
CLASS_OTHER = "other"

STATUS_OK = "prepared"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped_budget"


@dataclass
class BatchRow:
    problem_id: int
    title: str
    tier: str
    status: str
    calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    failed_stage: str = ""
    category: str = ""
    error_head: str = ""
    elapsed_ms: float = 0.0
    layers: Dict[str, int] = field(default_factory=dict)
    refs: List[Dict[str, Any]] = field(default_factory=list)
    hints_flags: List[int] = field(default_factory=list)
    injection_followed: bool = False
    injection_hits: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "problem_id": self.problem_id,
            "title": self.title,
            "tier": self.tier,
            "status": self.status,
            "calls": self.calls,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "failed_stage": self.failed_stage,
            "category": self.category,
            "error_head": self.error_head,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "layers": dict(self.layers),
            "refs": list(self.refs),
            "hints_flags": list(self.hints_flags),
            "injection_followed": self.injection_followed,
            "injection_hits": list(self.injection_hits),
        }


@dataclass
class BatchReport:
    rows: List[BatchRow] = field(default_factory=list)
    budget: Dict[str, int] = field(default_factory=dict)
    budget_exhausted: bool = False
    elapsed_ms: float = 0.0

    @property
    def ok_count(self) -> int:
        return sum(1 for row in self.rows if row.status == STATUS_OK)

    @property
    def failed_count(self) -> int:
        return sum(1 for row in self.rows if row.status == STATUS_FAILED)

    @property
    def skipped_count(self) -> int:
        return sum(1 for row in self.rows if row.status == STATUS_SKIPPED)

    def category_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for row in self.rows:
            if row.status != STATUS_FAILED:
                continue
            counts[row.category or CLASS_OTHER] = counts.get(row.category or CLASS_OTHER, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok_count": self.ok_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "budget_exhausted": self.budget_exhausted,
            "budget": self.budget,
            "categories": self.category_counts(),
            "elapsed_ms": round(self.elapsed_ms, 1),
            "rows": [row.to_dict() for row in self.rows],
        }

    def render_table(self) -> str:
        """纯文本汇总表（等宽，便于贴进报告）。"""
        header = (
            f"{'id':>4}  {'tier':<4} {'status':<15} {'calls':>5} {'total_tok':>9} "
            f"{'失败阶段':<10} {'归类':<20} 标题"
        )
        lines = [header, "-" * len(header)]
        for row in self.rows:
            total = row.prompt_tokens + row.completion_tokens
            mark = "  [INJ]" if row.injection_followed else ""
            lines.append(
                f"{row.problem_id:>4}  {row.tier or '-':<4} {row.status:<15} {row.calls:>5} {total:>9} "
                f"{row.failed_stage or '-':<10} {row.category or '-':<20} {row.title}{mark}"
            )
        return "\n".join(lines)


def select_targets(db: Database, *, include_prepared: bool = False) -> List[Dict[str, Any]]:
    """默认只跑"还没成功"的题：draft / failed / **preparing**。

    为什么把 `preparing` 也算目标：上一次批量被中断（进程被杀/断电）会把题留在 `preparing`，
    若不算目标，它会被永久跳过且没有任何提示——这是"静默丢题"，比失败更难发现。
    """
    sql = "SELECT id, title, tier, status FROM problems"
    if not include_prepared:
        sql += " WHERE status IS NULL OR status IN ('draft', 'failed', 'preparing')"
    sql += " ORDER BY id"
    return [dict(row) for row in db.query_all(sql)]


def run_batch(
    db: Optional[Database] = None,
    *,
    client: Any = None,
    problem_ids: Optional[Sequence[int]] = None,
    include_prepared: bool = False,
    budget_per: int = DEFAULT_BUDGET_PER,
    budget_total: int = DEFAULT_BUDGET_TOTAL,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    verbose: bool = False,
    progress: Any = None,
    allow_parser_fallback: bool = True,
    enable_llm_extras: bool = True,
) -> BatchReport:
    """批量跑预生成。`client` 可注入（测试用桩）。"""
    database = db or Database()
    report = BatchReport()
    started = time.perf_counter()
    budget = Budget(max_calls=int(budget_total))

    if problem_ids is not None:
        targets = []
        for problem_id in problem_ids:
            row = database.query_one("SELECT id, title, tier, status FROM problems WHERE id = ?", (problem_id,))
            if row is not None:
                targets.append(dict(row))
    else:
        targets = select_targets(database, include_prepared=include_prepared)

    for target in targets:
        problem_id = int(target["id"])
        title = str(target.get("title") or "")
        tier = str(target.get("tier") or "")
        if budget.remaining <= 0:
            report.budget_exhausted = True
            report.rows.append(
                BatchRow(problem_id=problem_id, title=title, tier=tier, status=STATUS_SKIPPED,
                         category=CLASS_BUDGET)
            )
            continue
        per_cap = max(1, min(int(budget_per), budget.remaining))
        usage_before = _usage_snapshot(client)
        service = PrepService(
            db=database,
            client=client,
            budget=budget,
            call_cap=per_cap,
            deadline_ts=time.monotonic() + float(timeout_sec),
            verbose=verbose,
        )
        service.allow_parser_fallback = allow_parser_fallback
        service.enable_llm_extras = enable_llm_extras
        report_row = BatchRow(problem_id=problem_id, title=title, tier=tier, status=STATUS_FAILED)
        stage_started = time.perf_counter()
        try:
            prep = service.prepare(problem_id)
        except Exception as exc:  # noqa: BLE001 - 批量绝不因单题异常中断
            prep = PrepReport(problem_id=problem_id, ok=False, failed_stage="internal",
                              error=f"{type(exc).__name__}: {exc}")
        report_row.elapsed_ms = (time.perf_counter() - stage_started) * 1000.0
        _fill_row(report_row, prep, usage_before, client)
        if budget.remaining <= 0 and not prep.ok:
            report.budget_exhausted = True
        report.rows.append(report_row)
        if progress is not None:
            progress(report_row)

    report.budget = budget.to_dict()
    report.elapsed_ms = (time.perf_counter() - started) * 1000.0
    return report


def _usage_snapshot(client: Any) -> Dict[str, int]:
    usage = getattr(client, "usage", None)
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
    return {
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(usage, "reasoning_tokens", 0) or 0),
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    }


def _fill_row(row: BatchRow, prep: PrepReport, usage_before: Dict[str, int], client: Any) -> None:
    usage_after = _usage_snapshot(client)
    row.calls = int(prep.llm_calls)
    row.prompt_tokens = max(0, usage_after["prompt_tokens"] - usage_before["prompt_tokens"])
    row.completion_tokens = max(0, usage_after["completion_tokens"] - usage_before["completion_tokens"])
    row.reasoning_tokens = max(0, usage_after["reasoning_tokens"] - usage_before["reasoning_tokens"])
    row.failed_stage = prep.failed_stage
    row.error_head = (prep.error or "").splitlines()[0][:300] if prep.error else ""
    row.layers = dict(prep.layers)
    row.refs = [ref.to_dict() for ref in prep.refs]
    row.hints_flags = [int(item.get("quality_flag") or 0) for item in prep.hints]
    row.injection_followed = bool(prep.injection_followed)
    row.injection_hits = list(prep.injection_hits_in_code)
    if prep.ok:
        row.status = STATUS_OK
        row.category = ""
        return
    row.status = STATUS_FAILED
    row.category = classify_failure(prep.failed_stage, prep.error)


def classify_failure(failed_stage: str, error: str) -> str:
    """失败归类（用于统计"卡在哪一类"，不做任何美化）。"""
    stage = (failed_stage or "").strip()
    text = error or ""
    if stage == CLASS_TIMEOUT or "超过时限" in text:
        return CLASS_TIMEOUT
    if stage == CLASS_BUDGET:
        return CLASS_BUDGET
    if stage == "spec":
        if "input_grammar" in text or "grammar" in text.lower():
            return CLASS_GRAMMAR
        return CLASS_SPEC
    if stage == "generator":
        return CLASS_GENERATOR
    if stage == "references":
        if "对拍" in text or "分歧" in text:
            return CLASS_CROSS
        return CLASS_REF_SAMPLE
    return CLASS_OTHER
