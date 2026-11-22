"""D2/D3：生成测试数据 → 独立复核 → 版本化入库。

一次尝试 = 两次对话（生成 + 复核），互不共享上下文。
复核驳回的条目不会"改一改留下"，而是整条丢弃：错误答案比缺数据更危险。
剩余不足 10 条就把驳回原因写进 `<已驳回>`，换一轮重生成。
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Sequence, Tuple

from . import config, reference
from .errors import StageFailure
from .llm_stage import StageError, StageRunner
from .stages import DATASET, DATASET_REVIEW
from .validators import (
    check_dataset,
    check_dataset_review,
    input_hash,
    layer_stats,
    normalize_input,
    normalize_output,
    summarize_reviews,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(str(raw)) if raw else default
    except (TypeError, ValueError):
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _chunk_items(
    items: Sequence[Dict[str, Any]], *, max_chars: int = 40_000, max_items: int = 20
) -> List[List[Dict[str, Any]]]:
    """把待复核条目切成若干批：单批不超过 40 KB / 20 条。"""
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    size = 0
    for item in items:
        length = len(str(item.get("input", ""))) + len(str(item.get("expected_output", ""))) + 80
        if current and (size + length > max_chars or len(current) >= max_items):
            chunks.append(current)
            current, size = [], 0
        current.append(item)
        size += length
    if current:
        chunks.append(current)
    return chunks


class _ReferenceTask:
    """后台写标程：只依赖题面，因此能和 D2 生成并行。

    实测（deepseek，14 条数据）D2 用 65 秒、D2R 用 30~60 秒，串起来是纯等待；
    并行后总时长从"D2 + D3 + D2R"降到 max(D2, D2R) + D3 + 复算。

    用独立 `StageRunner`：预算与计数互不干扰，`llm_calls` 仍按同一 problem_id 落库。
    线程内任何异常都收成 `engaged=False`，绝不让后台线程把整个任务带崩。
    """

    def __init__(
        self,
        db: Any,
        problem_id: int,
        user_content: str,
        *,
        samples: Sequence[Dict[str, Any]],
        judge_type: str,
        tolerance: str,
        responder: Any = None,
        llm_config: Any = None,
        user: str = "",
    ) -> None:
        self._box: Dict[str, Any] = {}
        self._thread = threading.Thread(
            target=self._run,
            args=(db, int(problem_id), user_content, samples, judge_type, tolerance,
                  responder, llm_config, user),
            daemon=True,
            name="slim2-reference",
        )
        self._thread.start()

    def _run(self, db: Any, problem_id: int, user_content: str,
             samples: Sequence[Dict[str, Any]], judge_type: str, tolerance: str,
             responder: Any, llm_config: Any = None, user: str = "") -> None:
        try:
            # 必须继承主 runner 的 responder：离线自测/回放时不能偷偷联网
            # 凭据与归属同理：这条线程花的是同一个人的额度，不能匿名
            runner = StageRunner(
                db, problem_id=problem_id, budget=1, responder=responder,
                llm_config=llm_config, user=user,
            )
            self._box["prepared"] = reference.write_and_anchor(
                runner, user_content, samples=samples,
                judge_type=judge_type, tolerance=tolerance,
            )
        except BaseException as exc:  # noqa: BLE001 - 线程里不能抛给主线程
            self._box["error"] = f"{type(exc).__name__}: {exc}"

    def wait(self) -> reference.Prepared:
        self._thread.join()
        prepared = self._box.get("prepared")
        if prepared is None:
            return reference.Prepared(reference.ReferenceResult(
                engaged=False, reason=f"参考解任务异常：{self._box.get('error') or '未知'}",
            ))
        return prepared


# ── 用户消息拼装 ────────────────────────────────────────────────────────────
_TAGS = ("</题目>", "</已驳回>", "</已有数据>")


def neutralize(text: str) -> str:
    """题面里的闭合标签会破坏分段；把 `<` 换成全角，内容一个字不改。"""
    value = str(text or "")
    for tag in _TAGS:
        value = value.replace(tag, tag.replace("<", "＜"))
    return value


def _judge_line(problem: Any) -> str:
    judge = str(problem["judge_type"] or "token")
    tolerance = str(problem["tolerance"] or "")
    return f"{judge}({tolerance})" if judge == "float" and tolerance else judge


def _samples_block(samples: Sequence[Dict[str, Any]]) -> str:
    if not samples:
        return "（题面没有解析出样例）"
    lines: List[str] = []
    for item in samples[:5]:
        lines.append(f"[样例{item.get('index', 1)}]")
        lines.append("输入：")
        lines.append(str(item.get("input", "")).rstrip("\n"))
        lines.append("输出：")
        lines.append(str(item.get("output", "")).rstrip("\n"))
    if len(samples) > 5:
        lines.append("（仅展示前 5 组）")
    return "\n".join(lines)


def build_dataset_user(
    problem: Any,
    samples: Sequence[Dict[str, Any]],
    rejected: Sequence[str] = (),
    existing: Sequence[Dict[str, Any]] = (),
) -> str:
    constraints = _loads(problem["constraints_json"], [])
    parts = [
        "<题目>",
        f"标题：{problem['title']}",
        f"判定方式：{_judge_line(problem)}",
        "题面：",
        neutralize(str(problem["statement_md"])),
        "样例：",
        _samples_block(samples),
        "约束：",
        "\n".join(str(item) for item in constraints) if constraints else "（题面未单列约束）",
        "</题目>",
    ]
    if rejected:
        parts += ["<已驳回>", "\n".join(rejected), "</已驳回>"]
    if existing:
        digest = []
        for item in existing[:20]:
            head = " ".join(str(item.get("input", "")).split())[:120]
            digest.append(f"[{item.get('layer', '')}] {item.get('size_class', '')}: {head}")
        parts += ["<已有数据>", "\n".join(digest), "</已有数据>"]
    return "\n".join(parts)


def build_review_user(
    problem: Any,
    samples: Sequence[Dict[str, Any]],
    items: Sequence[Dict[str, Any]],
) -> str:
    constraints = _loads(problem["constraints_json"], [])
    parts = [
        "<题目>",
        f"标题：{problem['title']}",
        f"判定方式：{_judge_line(problem)}",
        "题面：",
        neutralize(str(problem["statement_md"])),
        "样例：",
        _samples_block(samples),
        "约束：",
        "\n".join(str(item) for item in constraints) if constraints else "（题面未单列约束）",
        "</题目>",
        "",
        "<待复核数据>",
    ]
    for item in items:
        parts += [
            f"[index={item['index']}] [layer={item['layer']}]",
            "input:",
            str(item["input"]).rstrip("\n"),
            "expected_output:",
            str(item["expected_output"]).rstrip("\n"),
            "[/index]",
        ]
    parts.append("</待复核数据>")
    return "\n".join(parts)


def build_reference_user(problem: Any, samples: Sequence[Dict[str, Any]]) -> str:
    """给 D2R 的输入：只要题目原文与样例，不带任何已生成的数据或结论。

    刻意比 D2 的输入更窄：标程应当只依据题面写出来，看到"别人算过的答案"
    会诱导它照着凑。
    """
    constraints = _loads(problem["constraints_json"], [])
    return "\n".join(
        [
            "<题目>",
            f"标题：{problem['title']}",
            f"判定方式：{_judge_line(problem)}",
            "题面：",
            neutralize(str(problem["statement_md"])),
            "样例：",
            _samples_block(samples),
            "约束：",
            "\n".join(str(item) for item in constraints) if constraints else "（题面未单列约束）",
            "</题目>",
        ]
    )


# ── 主流程 ──────────────────────────────────────────────────────────────────
def run_dataset(
    db: Any,
    problem_id: int,
    runner: StageRunner,
    *,
    progress: Callable[[str], None] = lambda _stage: None,
) -> Dict[str, Any]:
    problem = db.query_one("SELECT * FROM problems WHERE id=?", (int(problem_id),))
    if problem is None:
        raise StageFailure("input", "题目不存在")
    if not str(problem["judge_type"]) in ("token", "line", "exact", "float"):
        raise StageFailure(
            "input",
            f"判定方式 {problem['judge_type']} 本版本不支持（没有自定义 checker）",
        )

    samples = _loads(problem["samples_json"], [])
    if not samples:
        raise StageFailure("input", "这道题没有解析出样例，无法校准期望输出；请补充样例后重新导入")

    existing = [
        dict(row)
        for row in db.query_all(
            "SELECT layer, size_class, input_data AS input FROM test_cases"
            " WHERE problem_id=? AND status='active' ORDER BY id DESC LIMIT 20",
            (int(problem_id),),
        )
    ]

    version = int(problem["dataset_version"] or 0) + 1
    feedback: List[str] = []
    last_error = ""

    # 写标程只依赖题面与样例，与 D2 无数据依赖：开工就并行跑，D3 跑完时它早已就绪
    ref_task = _ReferenceTask(
        db, int(problem_id), build_reference_user(problem, samples),
        samples=samples, judge_type=str(problem["judge_type"]),
        tolerance=str(problem["tolerance"] or ""),
        responder=runner.responder,
        llm_config=getattr(runner, "llm_config", None),
        user=getattr(runner, "user", ""),
    )

    prepared: Any = None
    try:
        for attempt in (1, 2):
            # 第二轮要显式告诉界面"在重试"：整段重来又要几十秒，只改回同一个
            # 「正在生成测试数据」会被当成卡死。
            progress("数据生成" if attempt == 1 else "重试生成")
            user = build_dataset_user(problem, samples, feedback, existing)
            try:
                payload = runner.call(DATASET, user, attempt=attempt)
            except StageError as exc:
                raise StageFailure("dataset", str(exc)) from exc

            draft = check_dataset(
                payload,
                min_cases=config.MIN_CASES,
                max_cases=config.MAX_CODEGEN_CASES,
                samples=samples,
                judge_type=str(problem["judge_type"]),
                tolerance=str(problem["tolerance"] or ""),
            )
            if not draft.ok:
                # 把丢弃明细带出来：只留一句 fatal 时，看不出到底是哪一条、因为什么被丢，
                # 排查只能靠猜（2026-11-21 压力层那次就是这么误判成"模型不听话"的）
                detail = "；".join(draft.warnings[:3])
                last_error = "；".join(draft.fatal) + (f"（丢弃明细：{detail}）" if detail else "")
                feedback = [f"上一轮不可用：{msg}" for msg in (draft.fatal + draft.warnings)[:4]]
                continue

            progress("数据复核")
            order = list(range(len(draft.items)))
            random.Random(int(problem_id) * 100 + attempt).shuffle(order)
            # 复核按体积分块：单条 8 KB × 40 条会撑爆上下文，D3 只对一批负责
            all_review_items = [
                {
                    "index": index,
                    "layer": draft.items[index]["layer"],
                    "input": draft.items[index]["input"],
                    "expected_output": draft.items[index]["expected_output"],
                }
                for index in order
            ]
            merged: List[Dict[str, Any]] = []
            for chunk in _chunk_items(all_review_items):
                chunk_ok = False
                for review_attempt in (1, 2):
                    try:
                        payload = runner.call(
                            DATASET_REVIEW,
                            build_review_user(problem, samples, chunk),
                            attempt=attempt,
                        )
                    except StageError as exc:
                        raise StageFailure("dataset_review", f"复核不可用：{exc}") from exc
                    review = check_dataset_review(
                        payload,
                        chunk,
                        str(problem["judge_type"]),
                        str(problem["tolerance"] or ""),
                    )
                    if review.ok:
                        merged.extend(review.items)
                        chunk_ok = True
                        break
                    last_error = "；".join(review.fatal)
                if not chunk_ok:
                    raise StageFailure("dataset_review", f"复核结果不可用：{last_error}")
            review_items = merged

            verdicts = {item["index"]: item for item in review_items}

            # ── D2R：参考解复算（标程早在后台写好，这里只等它 + 逐条真跑） ──
            progress("参考解复算")
            prepared = ref_task.wait()
            ref = reference.run_items(
                prepared,
                [{"index": index, **item} for index, item in enumerate(draft.items)],
                str(problem["judge_type"]),
                str(problem["tolerance"] or ""),
            )
            accepted, rejected, decisions = reference.decide_cases(
                draft.items, verdicts, ref,
                str(problem["judge_type"]), str(problem["tolerance"] or ""),
            )

            if len(accepted) < config.MIN_CASES:
                feedback = _feedback_lines(rejected)[:8] or ["复核后可用数据不足 10 条"]
                if ref.engaged:
                    feedback.append(
                        f"（本次已用参考解真跑复算：{ref.samples_passed}/{ref.samples_total} 样例通过；"
                        f"结论 {len(accepted)} 条可用）"
                    )
                last_error = f"复核后只剩 {len(accepted)} 条可用数据（下限 {config.MIN_CASES}）"
                continue

            return _store(
                db, problem, version, draft, review_items, accepted, rejected, decisions, ref,
                model=runner.last_model or "unknown",
                # 参考解那次调用记在它自己的 runner 上，这里合并成"本题总调用"
                calls=runner.calls + ref.calls, tokens=runner.tokens + ref.tokens,
            )
        raise StageFailure("dataset", last_error or "两轮生成都没有达到入库条件")
    finally:
        if prepared is not None:
            reference.release_program(prepared.program)


def _feedback_lines(
    rejected: Sequence[Tuple[int, Dict[str, Any], Dict[str, Any]]]
) -> List[str]:
    lines: List[str] = []
    for index, item, verdict in rejected:
        if verdict["verdict"] == "mismatch":
            lines.append(
                f"index={index}：你给出的期望输出是 {item['expected_output'].strip()}，"
                f"复核独立算出 {str(verdict.get('recomputed_output') or '').strip()}（{verdict.get('reason') or '结果不同'}）"
            )
        elif verdict["verdict"] == "invalid":
            lines.append(f"index={index}：输入不合法（{verdict.get('reason') or '违反约束'}）")
        else:
            lines.append(f"index={index}：复核无法独立验算（{verdict.get('reason') or '计算不确定'}）")
    return lines


def _store(
    db: Any,
    problem: Any,
    version: int,
    draft: Any,
    reviews: Sequence[Dict[str, Any]],
    accepted: Sequence[Dict[str, Any]],
    rejected: Sequence[Tuple[int, Dict[str, Any], Dict[str, Any]]],
    decisions: Dict[int, Dict[str, Any]],
    ref: "reference.ReferenceResult",
    *,
    model: str,
    calls: int,
    tokens: int,
) -> Dict[str, Any]:
    problem_id = int(problem["id"])
    plan = draft.payload.get("plan") or {}
    samples = _loads(problem["samples_json"], [])
    now = _now()
    verdicts = {item["index"]: item for item in reviews}

    # index 是「原始顺序」的下标；入库时按原始顺序编号，前端展示稳定
    original = list(enumerate(list(draft.items)))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO dataset_plans(problem_id, version, difficulty, layers_json,"
            " coverage_json, target_count, model, prompt_version, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (
                problem_id,
                version,
                str(plan.get("difficulty") or "")[:40],
                _dumps(plan.get("layers") or {}),
                _dumps(plan.get("coverage") or []),
                int(plan.get("target_count") or len(draft.items)),
                model,
                DATASET.version,
                now,
            ),
        )

        ordinal = 0
        sample_hashes = set()
        for sample in samples:
            key = input_hash(sample.get("input"))
            if key in sample_hashes:
                continue
            sample_hashes.add(key)
            conn.execute(
                "INSERT INTO test_cases(problem_id, version, layer, size_class, input_data,"
                " input_hash, expected_output, trust, source, status, ordinal, created_at)"
                " VALUES(?,?, 'sample', 'tiny', ?,?,?, 'sample', 'sample', 'active', ?, ?)"
                " ON CONFLICT(problem_id, version, input_hash) DO NOTHING",
                (
                    problem_id,
                    version,
                    normalize_input(sample.get("input")),
                    key,
                    normalize_output(sample.get("output")),
                    ordinal,
                    now,
                ),
            )
            ordinal += 1

        inserted: List[Dict[str, Any]] = []
        for index, item in original:
            verdict = verdicts[index]
            decision = decisions[index]
            active = decision["action"] != "reject"
            cursor = conn.execute(
                "INSERT INTO test_cases(problem_id, version, layer, size_class, input_data,"
                " input_hash, expected_output, trust, source, status, ordinal, created_at)"
                " VALUES(?,?,?,?,?,?,?,?, 'generated', ?, ?, ?)"
                " ON CONFLICT(problem_id, version, input_hash) DO NOTHING",
                (
                    problem_id,
                    version,
                    item["layer"],
                    item["size_class"],
                    item["input"],
                    item["input_hash"],
                    decision["output"],
                    decision["trust"] if active else "unreviewed",
                    "active" if active else "rejected",
                    ordinal,
                    now,
                ),
            )
            ordinal += 1
            if cursor.lastrowid:
                conn.execute(
                    "INSERT INTO case_reviews(case_id, reviewer_model, verdict, reason,"
                    " recomputed_output, prompt_version, created_at)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (
                        int(cursor.lastrowid),
                        model,
                        verdict["verdict"],
                        str(verdict.get("reason") or decision["note"])[:300],
                        verdict.get("recomputed_output") or None,
                        DATASET_REVIEW.version,
                        now,
                    ),
                )
            if active:
                materialized = dict(item)
                materialized["expected_output"] = decision["output"]
                inserted.append(materialized)

        quality = {
            "version": version,
            "target": int(plan.get("target_count") or len(draft.items)),
            "accepted": len(inserted),
            "rejected": len(rejected),
            "layers": layer_stats(inserted),
            "reviews": summarize_reviews(reviews),
            # 执行验证：engaged=False 表示这道题这次没有跑过参考解，数据仅经 LLM 复核
            "run_verification": {
                "engaged": bool(ref.engaged),
                "samples": f"{ref.samples_passed}/{ref.samples_total}",
                "reason": ref.reason[:200],
                "verdicts": ref.counts(),
                # 标程必须留档：事后要能拿它把每条数据重算一遍，否则"验证过"无法复核
                "code_hash": hashlib.sha256(ref.code.encode("utf-8")).hexdigest()[:16] if ref.code else "",
                "code": ref.code,
            },
            "model": model,
            "prompt_version": DATASET.version,
            "calls": calls,
            "tokens": tokens,
        }
        conn.execute(
            "UPDATE problems SET status='ready', dataset_version=?, quality_json=?, updated_at=?"
            " WHERE id=?",
            (version, _dumps(quality), now, problem_id),
        )

    return {
        "version": version,
        "accepted": len(inserted),
        "rejected": len(rejected),
        "layers": layer_stats(inserted),
        "reviews": summarize_reviews(reviews),
        "run_verification": quality["run_verification"],
        "warnings": list(draft.warnings)[:10],
    }
