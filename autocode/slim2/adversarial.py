"""D4/D5：针对学生错解造对抗数据，并用真跑结果做硬判据。

与数据流水线最大的差别：这里的"学生失败"不是模型说的，是评测机跑出来的。
模型只负责两件代码算不了的事：猜错在哪、算正确答案。

顺序刻意如此：
1. 先编译（编译不过就没有对抗可言）；
2. 再跑题面样例（样例都过不了，说明是显性错误，不需要定向构造）；
3. D4 出候选；
4. 每条真跑两次（排除随机/时间相关的不稳定用例）；
5. D5 只复核"输入合法 + 期望正确"，不推翻运行结果。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Sequence, Tuple

from ..engine.runner import (
    LANG_C,
    LANG_CPP,
    LANG_PYTHON,
    VERDICT_AC,
    VERDICT_RE,
    VERDICT_TLE,
    prepare_program,
    release_program,
    run_prepared,
)
from . import config
from .errors import StageFailure
from .llm_stage import StageError, StageRunner
from .stages import ADVERSARIAL, ADVERSARIAL_REVIEW
from .validators import (
    check_adversarial,
    check_adversarial_review,
    compare_output,
    normalize_output,
)

_LANGS = {
    "python": LANG_PYTHON, "py": LANG_PYTHON, "python3": LANG_PYTHON,
    "cpp": LANG_CPP, "c++": LANG_CPP, "cc": LANG_CPP,
    "c": LANG_C,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: Any, default: Any) -> Any:
    try:
        return json.loads(str(raw)) if raw else default
    except (TypeError, ValueError):
        return default


def _middle_truncate(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    head = limit * 2 // 3
    tail = limit - head
    return value[:head] + "\n...(材料截断)...\n" + value[-tail:]


def normalize_lang(raw: str) -> str:
    key = str(raw or "").strip().lower()
    if key not in _LANGS:
        raise StageFailure("input", f"不支持的语言 {raw!r}，可选 python / cpp / c")
    return _LANGS[key]


# ── 用户消息 ────────────────────────────────────────────────────────────────
def _problem_block(problem: Any, samples: Sequence[Dict[str, Any]]) -> List[str]:
    constraints = _loads(problem["constraints_json"], [])
    judge = str(problem["judge_type"] or "token")
    tolerance = str(problem["tolerance"] or "")
    judge_line = f"{judge}({tolerance})" if judge == "float" and tolerance else judge
    lines = [
        "<题目>",
        f"标题：{problem['title']}",
        f"判定方式：{judge_line}",
        "题面：",
        _middle_truncate(str(problem["statement_md"]), 12_000),
        "样例：",
    ]
    if samples:
        for item in samples[:5]:
            lines += [
                f"[样例{item.get('index', 1)}]",
                "输入：",
                str(item.get("input", "")).rstrip("\n"),
                "输出：",
                str(item.get("output", "")).rstrip("\n"),
            ]
    else:
        lines.append("（无样例）")
    lines += ["约束："]
    lines += [str(item) for item in constraints] if constraints else ["（题面未单列约束）"]
    lines.append("</题目>")
    return lines


def build_adversarial_user(
    problem: Any,
    samples: Sequence[Dict[str, Any]],
    code: str,
    lang: str,
    existing: Sequence[Dict[str, Any]],
    limit: int,
) -> str:
    lines = _problem_block(problem, samples)
    lines += [
        "",
        "<学生代码>",
        f"语言：{lang}",
        _middle_truncate(code, 20_000),
        "</学生代码>",
        "",
        "<已有数据>",
    ]
    if existing:
        for item in existing[:20]:
            head = " ".join(str(item.get("input", "")).split())[:120]
            lines.append(f"[{item.get('layer', '')}] {item.get('size_class', '')}: {head}")
    else:
        lines.append("（暂无）")
    lines += ["</已有数据>", "", "<要求>", f"最多 {limit} 条", "</要求>"]
    return "\n".join(lines)


def build_adversarial_review_user(
    problem: Any,
    samples: Sequence[Dict[str, Any]],
    items: Sequence[Dict[str, Any]],
    runs: Dict[int, Dict[str, Any]],
) -> str:
    lines = _problem_block(problem, samples)
    lines += ["", "<对抗数据>"]
    for item in items:
        lines += [
            f"[index={item['index']}]",
            "input:",
            str(item["input"]).rstrip("\n"),
            "expected_output:",
            str(item["expected_output"]).rstrip("\n"),
            "[/index]",
        ]
    lines += ["</对抗数据>", "", "<运行结果>"]
    for item in items:
        run = runs[item["index"]]
        lines += [
            f"[index={item['index']}] verdict={run['verdict']} exit={run['rc']}"
            f" time={run['elapsed_ms']:.0f}ms",
            "stdout:",
            _middle_truncate(str(run.get("stdout") or ""), 800),
            f"attempts: {run.get('attempts', '')}",
            "[/index]",
        ]
    lines.append("</运行结果>")
    return "\n".join(lines)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def _run_once(program: Any, data: str) -> Dict[str, Any]:
    result = run_prepared(program, data, config.RUN_TIMEOUT_MS)
    return {
        "verdict": str(result.get("verdict") or ""),
        "stdout": str(result.get("stdout") or ""),
        "stderr": str(result.get("stderr") or ""),
        "rc": int(result.get("rc") or 0),
        "elapsed_ms": float(result.get("elapsed_ms") or 0.0),
    }


def _classify(run: Dict[str, Any], expected: str, judge: str, tolerance: str) -> str:
    if run["verdict"] == VERDICT_TLE:
        return "TLE"
    if run["verdict"] == VERDICT_RE:
        return "RE"
    if run["verdict"] != VERDICT_AC:
        return run["verdict"].upper() or "RE"
    ok, _ = compare_output(judge, expected, run["stdout"], tolerance)
    return "AC" if ok else "WA"


def run_adversarial(
    db: Any,
    problem_id: int,
    submission_id: int,
    runner: StageRunner,
    *,
    progress=lambda _stage: None,
) -> Dict[str, Any]:
    problem = db.query_one("SELECT * FROM problems WHERE id=?", (int(problem_id),))
    submission = db.query_one("SELECT * FROM submissions WHERE id=?", (int(submission_id),))
    if problem is None or submission is None:
        raise StageFailure("input", "题目或错解不存在")
    if not str(problem["judge_type"]) in ("token", "line", "exact", "float"):
        raise StageFailure("input", f"判定方式 {problem['judge_type']} 本版本不支持")

    judge = str(problem["judge_type"])
    tolerance = str(problem["tolerance"] or "")
    samples = _loads(problem["samples_json"], [])
    lang = str(submission["lang"])
    code = str(submission["code"])

    progress("编译学生代码")
    try:
        program = prepare_program(lang, code)
    except Exception as exc:  # noqa: BLE001 - 缺编译器/环境异常都要变成人话
        raise StageFailure("compile", f"无法编译学生代码：{exc}") from exc
    if not program.ok:
        detail = (program.compile_output or program.error or "编译失败")[:800]
        _update_submission(db, submission_id, "ce", {"compile": detail})
        raise StageFailure("compile", "学生代码编译失败：" + detail)

    try:
        if not samples:
            raise StageFailure("input", "这道题没有样例，无法确认错解是否已通过基线")
        progress("样例基线")
        baseline = _baseline(program, samples, judge, tolerance)
        _update_submission(db, submission_id, "sample_failed" if baseline["failed"] else "new",
                           {"samples": baseline["samples"]})
        if baseline["failed"]:
            raise StageFailure("sample", baseline["message"])

        existing, known_hashes = _collect_known(db, problem_id)

        progress("构造对抗数据")
        limit = config.MAX_ADVERSARIAL_CASES
        user = build_adversarial_user(problem, samples, code, lang, existing, limit)
        try:
            payload = runner.call(ADVERSARIAL, user, attempt=1)
        except StageError as exc:
            raise StageFailure("adversarial", str(exc)) from exc

        draft = check_adversarial(
            payload, limit=limit, existing_hashes=known_hashes,
            max_input_chars=config.MAX_INPUT_CHARS,
        )
        if not draft.ok:
            raise StageFailure("adversarial", "；".join(draft.fatal))

        diagnosis = draft.payload.get("diagnosis") or {}
        if not draft.items:
            _update_submission(db, submission_id, "done", {"samples": baseline["samples"], "diagnosis": diagnosis})
            if str(diagnosis.get("failure_mode") or "") in ("correct", "none_found"):
                message = "模型判定这份代码在合法输入上正确，未生成对抗用例"
            else:
                message = "模型没有给出可用的对抗输入"
            return {
                "caught": 0,
                "total": 0,
                "diagnosis": diagnosis,
                "warnings": list(draft.warnings),
                "message": message,
            }

        progress("真跑验证")
        runs, candidates = _run_candidates(program, draft, judge, tolerance)

        if not candidates:
            _update_submission(db, submission_id, "done", {"samples": baseline["samples"], "diagnosis": diagnosis})
            return {
                "caught": 0,
                "total": len(draft.items),
                "diagnosis": diagnosis,
                "warnings": list(draft.warnings),
                "message": "候选用例都没有卡住这份代码",
            }

        progress("复核对抗数据")
        review_items = [
            {
                "index": item["index"],
                "input": item["input"],
                "expected_output": item["expected_output"],
                "run_verdict": item["run_verdict"],
            }
            for item in candidates
        ]
        try:
            review_payload = runner.call(
                ADVERSARIAL_REVIEW,
                build_adversarial_review_user(problem, samples, review_items, runs),
                attempt=1,
            )
        except StageError as exc:
            raise StageFailure("adversarial_review", f"复核不可用：{exc}") from exc
        review = check_adversarial_review(review_payload, review_items, judge, tolerance)
        if not review.ok:
            raise StageFailure("adversarial_review", "；".join(review.fatal))

        verdict_map = {row["index"]: row for row in review.items}
        accepted = [item for item in candidates if verdict_map[item["index"]]["verdict"] == "accept"]
        if not accepted:
            raise StageFailure("adversarial_review", "复核没有接受任何一条对抗数据")

        stored = _store(
            db, problem_id, submission_id, candidates, review.items, accepted, diagnosis,
            model=runner.last_model or "unknown",
        )
        _update_submission(db, submission_id, "done", {"samples": baseline["samples"], "diagnosis": diagnosis})
        return {
            "caught": len(accepted),
            "total": len(candidates),
            "stored": stored,
            "diagnosis": diagnosis,
            "warnings": list(draft.warnings)[:10],
        }
    finally:
        release_program(program)


def _collect_known(db: Any, problem_id: int) -> Tuple[List[Dict[str, Any]], set]:
    """已有数据摘要（给模型避免撞车）+ 用过的输入哈希（程序侧硬去重）。"""
    existing = [
        dict(row) for row in db.query_all(
            "SELECT layer, size_class, input_data AS input FROM test_cases"
            " WHERE problem_id=? AND status='active' ORDER BY id DESC LIMIT 20",
            (int(problem_id),),
        )
    ] + [
        dict(row) for row in db.query_all(
            "SELECT '' AS layer, '' AS size_class, input_data AS input FROM adversarial_cases"
            " WHERE problem_id=? ORDER BY id DESC LIMIT 20",
            (int(problem_id),),
        )
    ]
    known_hashes = {
        str(row["input_hash"])
        for row in db.query_all(
            "SELECT input_hash FROM test_cases WHERE problem_id=?"
            " UNION SELECT input_hash FROM adversarial_cases WHERE problem_id=?",
            (int(problem_id), int(problem_id)),
        )
    }
    return existing, known_hashes


def _run_candidates(
    program: Any, draft: Any, judge: str, tolerance: str
) -> Tuple[Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
    """每条候选跑两遍：不稳定（随机/时间相关）直接丢，只留真正卡住学生的。

    跑两遍不是浪费：学生代码里 `srand(time(0))`、未初始化变量这类问题会让同一输入
    产出不同结果，这种用例入库后会造成"同代码两次评测结论不同"的争议。
    """
    runs: Dict[int, Dict[str, Any]] = {}
    candidates: List[Dict[str, Any]] = []
    for index, item in enumerate(draft.items):
        first = _run_once(program, item["input"])
        second = _run_once(program, item["input"])
        stable = (
            first["verdict"] == second["verdict"]
            and first["stdout"].strip() == second["stdout"].strip()
        )
        verdict = _classify(first, item["expected_output"], judge, tolerance)
        item.update(
            {
                "index": index,
                "run_verdict": verdict,
                "actual_output": first["stdout"],
                "predicted_output": item.get("predicted_output") or "",
                "stable": stable,
            }
        )
        runs[index] = {
            "verdict": verdict,
            "rc": first["rc"],
            "elapsed_ms": first["elapsed_ms"],
            "stdout": first["stdout"],
            "attempts": f"{first['stdout'].strip()[:40]} | {second['stdout'].strip()[:40]} | "
                        f"{'一致' if stable else '不一致'}",
        }
        if not stable:
            draft.warnings.append(f"第 {index + 1} 条两次运行结果不一致，已丢弃")
            continue
        if verdict in ("WA", "TLE", "RE", "MLE"):
            candidates.append(item)
        else:
            draft.warnings.append(f"第 {index + 1} 条没卡住学生（{verdict}），已丢弃")
    return runs, candidates


def _baseline(program: Any, samples: Sequence[Dict[str, Any]], judge: str, tolerance: str) -> Dict[str, Any]:
    rows = []
    failed = False
    message = ""
    for sample in samples[:5]:
        run = _run_once(program, str(sample.get("input", "")))
        expected = normalize_output(sample.get("output"))
        verdict = _classify(run, expected, judge, tolerance)
        rows.append(
            {
                "index": int(sample.get("index") or len(rows) + 1),
                "expected": expected,
                "actual": run["stdout"],
                "verdict": verdict,
            }
        )
        if verdict != "AC" and not failed:
            failed = True
            message = (
                f"这份代码在样例 {sample.get('index', len(rows))} 上就没有通过"
                f"（期望 {expected.strip()[:60]}，实际 {run['stdout'].strip()[:60]}）。"
                "对抗数据用于暴露隐蔽 bug，样例已失败请先让学生自己修。"
            )
    return {"failed": failed, "message": message, "samples": rows}


def _update_submission(db: Any, submission_id: int, status: str, baseline: Dict[str, Any]) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE submissions SET status=?, baseline_json=? WHERE id=?",
            (status, _dumps(baseline), int(submission_id)),
        )


def _store(
    db: Any,
    problem_id: int,
    submission_id: int,
    candidates: Sequence[Dict[str, Any]],
    reviews: Sequence[Dict[str, Any]],
    accepted: Sequence[Dict[str, Any]],
    diagnosis: Dict[str, Any],
    *,
    model: str,
) -> int:
    accepted_indices = {item["index"] for item in accepted}
    verdicts = {row["index"]: row for row in reviews}
    now = _now()
    stored = 0
    with db.transaction() as conn:
        for item in candidates:
            active = item["index"] in accepted_indices
            cursor = conn.execute(
                "INSERT INTO adversarial_cases(problem_id, submission_id, input_data, input_hash,"
                " expected_output, predicted_output, actual_output, run_verdict, targets, why,"
                " failure_reason, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(problem_id, input_hash) DO NOTHING",
                (
                    int(problem_id),
                    int(submission_id),
                    item["input"],
                    item["input_hash"],
                    item["expected_output"],
                    str(item.get("predicted_output") or ""),
                    str(item.get("actual_output") or "")[:4000],
                    str(item.get("run_verdict") or ""),
                    str(item.get("targets") or "")[:120],
                    str(item.get("derive") or "")[:300],
                    # 模型没写或写得空泛时退回 targets：宁可给一句定位，也不留空
                    str(item.get("failure_reason") or item.get("targets") or "")[:240],
                    "active" if active else "rejected",
                    now,
                ),
            )
            if cursor.rowcount != 1 or not cursor.lastrowid:
                continue
            case_id = int(cursor.lastrowid)
            row = verdicts.get(item["index"], {})
            conn.execute(
                "INSERT INTO adversarial_reviews(adv_case_id, reviewer_model, verdict, reason_code,"
                " reason, corrected_output, prompt_version, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    case_id,
                    model,
                    str(row.get("verdict") or "uncertain"),
                    str(row.get("reason_code") or "other"),
                    str(row.get("reason") or "")[:300],
                    row.get("corrected_output"),
                    ADVERSARIAL_REVIEW.version,
                    now,
                ),
            )
            if active:
                stored += 1
        conn.execute(
            "UPDATE problems SET updated_at=? WHERE id=?", (now, int(problem_id))
        )
    return stored
