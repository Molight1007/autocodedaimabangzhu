"""D1R 代码答案 → D2 数据设计 → 执行 → D3 复核 → 版本化入库。

为什么答案不再由 D2 口算：
- 大数、取模、浮点、边界下标是口算的固有短板，两轮 LLM 会一起犯同样的错；
- "期望输出必须闭式可算"把压力层锁死在很小的数据上；
- 题面没有样例时，旧实现连代码都不跑，答案只剩 LLM 口算一条证据。

现在的分工：
- LLM 写代码答案（题目 + 可选样例），它负责"算对"；
- LLM 设计 I/O 契约与分层输入（能看到代码答案，契约必须与代码的读入一致）；
- 代码逐条执行，stdout 直接作为期望输出；
- D3 独立复核再算一遍，作为第二证据。

样例是可选锚：有就必须全过（否则重写一次代码），没有就标 `anchored=False` 照常跑。
"""

from __future__ import annotations

import hashlib
import json
import random
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Sequence, Tuple

from . import config, io_spec, reference, repair, segment
from .errors import StageFailure
from .llm_stage import StageError, StageRunner
from .stages import DATASET, DATASET_REVIEW
from .validators import (
    LAYER_FLOORS,
    check_dataset,
    check_dataset_review,
    input_hash,
    judge_supported,
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
    items: Sequence[Dict[str, Any]], *,
    max_chars: int = config.REVIEW_MAX_CHARS, max_items: int = 20,
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


# ── 用户消息拼装 ────────────────────────────────────────────────────────────
_TAGS = ("</题目>", "</已驳回>", "</已有数据>", "</代码答案>", "</上次失败>")


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
        return "（题面没有样例）"
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


def _problem_block(problem: Any, samples: Sequence[Dict[str, Any]]) -> List[str]:
    constraints = _loads(problem["constraints_json"], [])
    return [
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


def build_dataset_user(
    problem: Any,
    samples: Sequence[Dict[str, Any]],
    code: str = "",
    rejected: Sequence[str] = (),
    existing: Sequence[Dict[str, Any]] = (),
) -> str:
    """D2 的输入：题目 + 代码答案 + 可选样例。

    代码答案必须一起给：契约要按代码的读入方式描述，否则"数据能读进去、
    代码读不进去"会在执行阶段成批失败。
    """
    parts = _problem_block(problem, samples)
    parts += ["<代码答案>", str(code or "（无）").rstrip(), "</代码答案>"]
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
    parts = _problem_block(problem, samples)
    parts += [
        "",
        "<待复核数据>",
        "（expected_output 是参考解在评测机上真实跑出来的，不是生成者写的；仍请独立计算）",
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


def build_reference_user(
    problem: Any, samples: Sequence[Dict[str, Any]], failure: str = ""
) -> str:
    """D1R 的输入：题目原文 + 可选样例（重复生成时还带已确定的契约）。

    首次生成没有契约：代码是"格式的提出者"，随后由 D2 按它写契约。
    题面没写标准输入时，代码自己定一个最简单的协议并在注释里写明。
    """
    parts = _problem_block(problem, samples)
    if failure:
        parts += ["<上次失败>", failure, "</上次失败>"]
    return "\n".join(parts)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def run_dataset(
    db: Any,
    problem_id: int,
    runner: StageRunner,
    *,
    progress: Callable[[str], None] = lambda _stage: None,
) -> Dict[str, Any]:
    raw = db.query_one("SELECT * FROM problems WHERE id=?", (int(problem_id),))
    if raw is None:
        raise StageFailure("input", "题目不存在")
    # 题面短到看不出输入输出格式时，先让 D0 补全（只补格式、不改题意）
    raw = _repair_if_needed(db, int(problem_id), raw, runner, progress)

    # 契约（程序怎么读）与判题能力（能不能判）是两件事：老题的 judge_type 可能是
    # special/interactive（要自定义 checker），那种题即便有契约也生成不了。
    contract = io_spec.loads(raw["exec_spec_json"])
    if not judge_supported(str(raw["judge_type"])) and not contract:
        raise StageFailure(
            "input",
            f"判定方式 {raw['judge_type']} 本版本不支持（没有自定义 checker）",
        )

    # 已存过契约就用契约里的样例与判定方式：那是上一轮验证过的协议，
    # 重复生成不该换一套格式，否则两次数据喂不进同一个程序。
    samples = (
        list(contract["samples"])
        if contract.get("samples")
        else (_loads(raw["samples_json"], []) or segment.refresh_samples(db, raw))
    )
    judge_type = str((contract.get("judge_type") if contract else "") or raw["judge_type"] or "token")
    tolerance = str(raw["tolerance"] or "")
    if not samples and config.require_samples():
        raise StageFailure("input", "这道题没有解析出样例，无法校准期望输出；请补充样例后重新导入")

    existing = [
        dict(row)
        for row in db.query_all(
            "SELECT layer, size_class, input_data AS input FROM test_cases"
            " WHERE problem_id=? AND status='active' ORDER BY id DESC LIMIT 20",
            (int(problem_id),),
        )
    ]

    version = int(raw["dataset_version"] or 0) + 1
    feedback: List[str] = []
    last_error = ""
    problem = _with_contract(raw, contract)

    # 代码先写：首次生成时只看题面；已有契约时把契约一并给它，
    # 避免重复生成换一套输入格式（同一个程序喂不进两版数据）。
    progress("代码答案")
    prepared = _write_code(runner, problem, samples, judge_type, tolerance)
    try:
        for attempt in (1, 2):
            progress("数据生成" if attempt == 1 else "重试生成")
            draft, contract = _draft_cases(
                runner, problem, samples, prepared.result.code,
                feedback, existing, attempt,
            )
            if not draft.ok:
                detail = "；".join(draft.warnings[:3])
                last_error = "；".join(draft.fatal) + (f"（丢弃明细：{detail}）" if detail else "")
                feedback = [f"上一轮不可用：{msg}" for msg in (draft.fatal + draft.warnings)[:4]]
                continue
            # 契约改了，样例、题面视图、复核口径都要跟着换；同时确认代码读得动
            # 改写后的样例——读不动说明"D2 写的契约"与"代码的读法"走岔了。
            if contract:
                problem = _with_contract(raw, contract)
                new_samples = list(contract.get("samples") or samples)
                judge_type = str(contract.get("judge_type") or judge_type)
                trouble = reference.check_samples(prepared, new_samples, judge_type, tolerance)
                if trouble:
                    feedback = trouble
                    last_error = trouble[0]
                    continue
                # 题面无样例时，这一组是"按契约重写"的，不能算 anchored（那是题面原文
                # 才有的可信度）；但代码确实跑通了，如实记下来，别让人以为完全没验过。
                if not prepared.result.anchored and new_samples:
                    prepared.result.contract_passed = len(new_samples)
                    prepared.result.contract_total = len(new_samples)
                samples = new_samples

            input_chars = sum(len(str(item.get("input") or "")) for item in draft.items)
            if input_chars > config.MAX_BATCH_INPUT_CHARS:
                feedback = [
                    f"本批输入共 {input_chars} 字符，超过单批上限 "
                    f"{config.MAX_BATCH_INPUT_CHARS}；请减少条数或缩小单条规模"
                ]
                last_error = feedback[0]
                continue

            progress("执行代码答案")
            executed, dropped = reference.execute_all(prepared, draft.items)
            if dropped:
                draft.warnings.extend(dropped[:5])
            if len(executed) < config.MIN_CASES:
                feedback = dropped[:8] or [
                    f"代码只跑通 {len(executed)} 条，检查契约与代码的读入是否一致"
                ]
                last_error = (
                    f"代码答案只跑通 {len(executed)} 条（下限 {config.MIN_CASES}）"
                )
                continue
            total_chars = sum(
                len(str(item.get("input") or "")) + len(str(item.get("expected_output") or ""))
                for item in executed
            )
            if total_chars > config.MAX_BATCH_CHARS:
                feedback = [
                    f"本批输入+输出共 {total_chars} 字符，超过单批上限 "
                    f"{config.MAX_BATCH_CHARS}；请减少条数或缩小单条规模"
                ]
                last_error = feedback[0]
                continue
            warn = _output_warning(executed)
            if warn:
                draft.warnings.append(warn)

            progress("数据复核")
            review_items = _review_executed(
                runner, problem, samples, executed, judge_type, tolerance, attempt
            )
            verdicts = {item["index"]: item for item in review_items}
            accepted, rejected, decisions = reference.decide_cases(
                executed, verdicts, prepared.result, judge_type, tolerance
            )
            if len(accepted) < config.MIN_CASES:
                feedback = _feedback_lines(rejected)[:8] or ["复核后可用数据不足 10 条"]
                last_error = (
                    f"复核后只剩 {len(accepted)} 条可用数据（下限 {config.MIN_CASES}）"
                    f"；例如：{feedback[0][:120]}"
                )
                continue

            # 契约只在整批数据可用后才落库：中途失败不该把下一轮的格式定死
            if contract:
                _save_contract(db, int(problem_id), contract)
            return _store(
                db, problem, version, draft, executed, review_items,
                accepted, rejected, decisions, prepared.result, samples,
                dropped=len(dropped),
                model=runner.last_model or "unknown",
                calls=runner.calls, tokens=runner.tokens,
            )
        raise StageFailure("dataset", last_error or "两轮生成都没有达到入库条件")
    finally:
        reference.release_program(prepared.program)


def _review_executed(
    runner: StageRunner, problem: Any, samples: Sequence[Dict[str, Any]],
    executed: Sequence[Dict[str, Any]], judge_type: str, tolerance: str, attempt: int,
) -> List[Dict[str, Any]]:
    """把执行后的条目分块送 D3 独立复核。

    顺序打乱、index 保留原位置：复核员只看到"输入 + 代码跑出的输出"，
    看不到生成理由，也看不到代码答案。
    """
    order = list(range(len(executed)))
    random.Random(int(problem["id"]) * 100 + attempt).shuffle(order)
    review_input = [
        {
            "index": index,
            "layer": executed[index]["layer"],
            "input": executed[index]["input"],
            "expected_output": executed[index]["expected_output"],
        }
        for index in order
    ]
    review_items: List[Dict[str, Any]] = []
    last_error = ""
    for chunk in _chunk_items(review_input):
        chunk_ok = False
        for _review_attempt in (1, 2):
            try:
                payload = runner.call(
                    DATASET_REVIEW,
                    build_review_user(problem, samples, chunk),
                    attempt=attempt,
                )
            except StageError as exc:
                raise StageFailure("dataset_review", f"复核不可用：{exc}") from exc
            review = check_dataset_review(payload, chunk, judge_type, tolerance)
            if review.ok:
                review_items.extend(review.items)
                chunk_ok = True
                break
            last_error = "；".join(review.fatal)
        if not chunk_ok:
            raise StageFailure("dataset_review", f"复核结果不可用：{last_error}")
    return review_items


def _write_code(
    runner: StageRunner, problem: Any, samples: Sequence[Dict[str, Any]],
    judge_type: str, tolerance: str,
) -> reference.Prepared:
    """写代码答案；样例没过或编译失败时带着原因重写一次。"""
    first = _ask_code(runner, build_reference_user(problem, samples), samples,
                      judge_type, tolerance)
    if first.result.engaged:
        return first
    reason = first.result.reason or "代码答案不可用"
    reference.release_program(first.program)
    second = _ask_code(
        runner, build_reference_user(problem, samples, failure=reason), samples,
        judge_type, tolerance,
    )
    if second.result.engaged:
        return second
    reference.release_program(second.program)
    raise StageFailure(
        "reference",
        f"两次都没写出可用的代码答案：{second.result.reason or reason}；"
        "请确认题目不是交互题/自定义判定，或稍后重试",
    )


def _ask_code(
    runner: StageRunner, user: str, samples: Sequence[Dict[str, Any]],
    judge_type: str, tolerance: str,
) -> reference.Prepared:
    try:
        return reference.write_and_anchor(
            runner, user, samples=samples, judge_type=judge_type, tolerance=tolerance,
        )
    except Exception as exc:  # noqa: BLE001 - 代码生成失败也要变成可读结论
        return reference.Prepared(reference.ReferenceResult(
            engaged=False, reason=f"{type(exc).__name__}: {exc}",
        ))


def _draft_cases(
    runner: StageRunner, problem: Any, samples: Sequence[Dict[str, Any]],
    code: str, feedback: Sequence[str], existing: Sequence[Dict[str, Any]], attempt: int,
) -> Tuple[Any, Dict[str, Any]]:
    """跑一次 D2，产出 `(数据草案, 契约)`。

    两道门槛顺序固定：**先契约后输入**。契约不成立，这一批数据全部作废——
    拿不到"程序怎么读"的确定答案时，任何"看起来对"的输入都无法证明能被评测机读进去。
    """
    user = build_dataset_user(problem, samples, code, feedback, existing)
    try:
        payload = runner.call(DATASET, user, attempt=attempt)
    except StageError as exc:
        raise StageFailure("dataset", str(exc)) from exc

    contract = io_spec.check_contract(payload, samples)
    if not contract.ok:
        return contract, {}

    draft = check_dataset(
        payload,
        min_cases=config.MIN_CASES,
        max_cases=config.MAX_CODEGEN_CASES,
        samples=samples,
        judge_type=str(problem["judge_type"]),
        tolerance=str(problem["tolerance"] or ""),
    )
    if draft.ok:
        _enforce_contract(draft, contract.payload["io"])
    return draft, contract.payload


def _enforce_contract(draft: Any, io: Dict[str, Any]) -> None:
    """逐条按契约读输入；读不通的剔除，并写清原因。

    这是"数据能用"的唯一结构性保证：留下的每一条都能被同一份契约读出、
    且没有多余 token，它就一定能喂进按同一契约写的程序。
    """
    kept: List[Any] = []
    # D2 的 cases 不带 index（那是复核阶段才编的号），这里按给出顺序编号，
    # 否则提示里全是"第 ? 条"，用户没法回去找是哪一条。
    for position, item in enumerate(draft.items, start=1):
        ok, reason = io_spec.validate_input(io, str(item.get("input") or ""))
        if ok:
            kept.append(item)
        else:
            draft.warnings.append(f"第 {position} 条不符合契约：{reason}")
    dropped = len(draft.items) - len(kept)
    draft.items = kept
    if dropped and len(kept) < config.MIN_CASES:
        draft.fail(f"符合契约的只有 {len(kept)} 条（剔除 {dropped} 条），少于下限 {config.MIN_CASES}")


def _output_warning(items: Sequence[Dict[str, Any]]) -> str:
    """执行完才能看输出多样性：同一答案占一半以上时提示区分度偏低。"""
    counts: Dict[str, int] = {}
    for item in items:
        key = str(item.get("expected_output") or "")
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return ""
    top = max(counts.values())
    if top * 2 > len(items):
        return f"单一输出出现 {top}/{len(items)} 次（超过一半），区分度偏低"
    return ""


def _with_contract(problem: Any, contract: Dict[str, Any]) -> Dict[str, Any]:
    """题面 + 已确定的契约。契约必须出现在给模型的输入里，否则它会另起一套格式。"""
    row = dict(problem)
    if not contract:
        return row
    row["judge_type"] = str(contract.get("judge_type") or row.get("judge_type"))
    block = [
        "",
        "【已确定的 I/O 契约（本轮数据必须按它写，程序按它校验）】",
        io_spec.describe(contract["io"]),
    ]
    row["statement_md"] = str(row.get("statement_md") or "").rstrip() + "\n" + "\n".join(block) + "\n"
    return row


def _save_contract(db: Any, problem_id: int, contract: Dict[str, Any]) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE problems SET exec_spec_json=?, judge_type=?, updated_at=? WHERE id=?",
            (_dumps(contract), str(contract.get("judge_type") or "token"), _now(), int(problem_id)),
        )


def _feedback_lines(
    rejected: Sequence[Tuple[int, Dict[str, Any], Dict[str, Any]]]
) -> List[str]:
    lines: List[str] = []
    for index, item, verdict in rejected:
        if verdict["verdict"] == "mismatch":
            lines.append(
                f"index={index}：代码答案算出 {item['expected_output'].strip()}，"
                f"复核独立算出 {str(verdict.get('recomputed_output') or '').strip()}"
                f"（{verdict.get('reason') or '结果不同'}）"
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
    executed_items: Sequence[Dict[str, Any]],
    reviews: Sequence[Dict[str, Any]],
    accepted: Sequence[Dict[str, Any]],
    rejected: Sequence[Tuple[int, Dict[str, Any], Dict[str, Any]]],
    decisions: Dict[int, Dict[str, Any]],
    ref: "reference.ReferenceResult",
    samples: Sequence[Dict[str, Any]],
    *,
    dropped: int,
    model: str,
    calls: int,
    tokens: int,
) -> Dict[str, Any]:
    problem_id = int(problem["id"])
    plan = draft.payload.get("plan") or {}
    now = _now()
    verdicts = {item["index"]: item for item in reviews}

    # executed_items 的下标与 `decide_cases` / 复核批次同一坐标系；
    # 代码跑挂的条目已被剔除，不能拿它去对 `draft.items` 的位置。
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

        inserted, skipped = _write_cases(
            conn, problem_id, version, executed_items, verdicts, decisions, samples, model, now
        )
        if skipped:
            draft.warnings.append(f"{skipped} 条与已有输入重复，未重复入库")
        quality = {
            "version": version,
            "target": int(plan.get("target_count") or len(draft.items)),
            "accepted": len(inserted),
            "rejected": len(rejected),
            "layers": layer_stats(inserted),
            "reviews": summarize_reviews(reviews),
            # 执行验证：代码答案在评测机上逐条跑过，stdout 就是期望输出
            "run_verification": {
                "engaged": bool(ref.engaged),
                # 锚定＝代码答案在题面样例上全过。没有样例就没有锚点，
                # 但代码照样执行；两个字段分开记，界面才能说清"差在哪"
                "anchored": bool(ref.anchored),
                "samples": f"{ref.samples_passed}/{ref.samples_total}" if ref.samples_total else "0/0",
                # 题面无样例时，代码答案改为跑"按契约重写"的样例；单独记，不混进 anchored
                "contract_samples": (
                    f"{ref.contract_passed}/{ref.contract_total}" if ref.contract_total else ""
                ),
                "reason": ref.reason[:200],
                "verdicts": ref.counts(),
                "executed": len(executed_items),
                "dropped": int(dropped),
                "conflicts_kept": int(ref.conflicts_kept),
                # 代码答案必须留档：事后要能拿它把每条数据重算一遍，否则"验证过"无法复核
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

    layers = layer_stats(inserted)
    warnings = list(draft.warnings)
    note = str(problem.get("repair_note") or "")
    if note:
        # 题面被动过，必须在结果最显眼处说出来：用户看到的题可能已经不是原文
        warnings.insert(0, note)
    # D2 交付时四层齐全，不代表入库时还齐：契约校验会剔除读不通的条目，
    # 代码执行也会丢掉跑挂/超长的条目。不重新算一遍，界面就会显示
    # "10 条全部通过"而实际一条压力数据都没有（实测题 28 即如此）。
    if not any("分层覆盖不足" in item for item in warnings):
        warnings += _layer_gap_warnings(layers)
    return {
        "version": version,
        "accepted": len(inserted),
        "rejected": len(rejected),
        "layers": layers,
        "reviews": summarize_reviews(reviews),
        "run_verification": quality["run_verification"],
        "warnings": warnings[:10],
    }


def _repair_if_needed(
    db: Any, problem_id: int, raw: Any, runner: StageRunner, progress: Callable[[str], None]
) -> Any:
    """题面残缺时先补全；补不了就抛出，不拿残缺题面去烧调用。

    实测题 26（158 字、无格式）直接生成会连烧 3 次调用、写满 32768 token 后截断。
    """
    trouble = _statement_trouble(str(raw["statement_md"] or ""))
    if not trouble:
        return raw
    progress("补全题面")
    fixed = repair.repair(runner, raw)
    if not fixed.ok:
        raise StageFailure(
            "repair",
            f"{trouble}；模型也补不了：{fixed.reason}。请手工补全输入/输出格式后再生成",
        )
    _save_repaired(db, problem_id, str(raw["statement_md"] or ""), fixed)
    row = dict(db.query_one("SELECT * FROM problems WHERE id=?", (problem_id,)))
    row["repair_note"] = (
        f"题面已由模型补全（置信度 {fixed.confidence}）：{repair.summary(fixed)}"
    )
    return row


def _save_repaired(db: Any, problem_id: int, original: str, fixed: Any) -> None:
    """落库补全结果：原文留档、样例与契约作废（题面变了，旧的都不再适用）。"""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE problems SET statement_original=?, statement_md=?, statement_repaired=1,"
            " samples_json='[]', exec_spec_json='', updated_at=? WHERE id=?",
            (original, fixed.statement, _now(), int(problem_id)),
        )


def _statement_trouble(statement: str) -> str:
    """题面是否短到无法确定 I/O 格式；返回空串表示可以用。

    只拦「又短、又没提输入、又没提输出」这一种：绝大多数正常题面都含
    「输入 / 输出」或「第一行 / 读入」之类的字样，不会误伤。长题面一律放行，
    因为 PDF 题面经常把格式写在很靠后的位置，按字数拦会误杀。
    """
    text = statement.strip()
    if len(text) >= config.MIN_STATEMENT_CHARS:
        return ""
    low = text.lower()
    has_input = any(word in low for word in ("输入", "读入", "第一行", "input"))
    has_output = any(word in low for word in ("输出", "打印", "output"))
    if has_input and has_output:
        return ""
    missing = "输入格式" if not has_input else "输出格式"
    return f"题面只有 {len(text)} 字，且没有说明 {missing}"


def _layer_gap_warnings(layers: Dict[str, int]) -> List[str]:
    """按入库结果算层缺口；只在确实缺层时返回一句。"""
    name_cn = {"boundary": "边界", "typical": "典型", "stress": "压力", "trap": "陷阱"}
    gaps = [
        f"{name_cn[key]}层 {int(layers.get(key, 0))}/{floor}"
        for key, floor in LAYER_FLOORS.items()
        if int(layers.get(key, 0)) < floor
    ]
    if not gaps:
        return []
    return [
        "入库后分层覆盖不足：" + "、".join(gaps)
        + "（多半是这些条目在契约校验或执行阶段被剔除，下一轮需补）"
    ]


def _write_cases(
    conn: Any,
    problem_id: int,
    version: int,
    items: Sequence[Dict[str, Any]],
    verdicts: Dict[int, Dict[str, Any]],
    decisions: Dict[int, Dict[str, Any]],
    samples: Sequence[Dict[str, Any]],
    model: str,
    now: str,
) -> Tuple[List[Dict[str, Any]], int]:
    """写样例 + 生成条目 + 复核记录，返回 `(可入库条目, 重复跳过数)`。"""
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
    skipped = 0
    for index, item in enumerate(items):
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
        if not cursor.lastrowid:
            # 输入与样例/同批条目重复：库里已有，别把它算进 accepted
            skipped += 1
            continue
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
    return inserted, skipped
