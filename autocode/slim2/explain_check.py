"""样例解释的代码反推验证（T4）。

题面的"样例解释"是出题人手写的文字，没有任何自动校验——而探针和提示梯子
都从题面生成，解释错了，学生收到的反馈就会错（2026-11-25 实测事故：
题面解释写错购买方案，L0 把学生的正确答案判成 held）。

验证思路（用户拍板"代码反推"）：
1. 先走 D1R 写参考代码并做样例锚定——代码没过样例就没有反推的资格；
2. T4 让模型把解释抽成**可计算断言**，并写一个独立重算的验证程序；
3. 验证程序**真跑**（安全守卫 + 沙箱执行），stdout 的 JSON 就是报告——
   模型只负责翻译和写代码，真伪由执行结果决定，延续"模型改不了字"的架构哲学。

模型不落库参考代码本体的责任在这里：验证报告是唯一交付物。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

from ..engine.runner import (
    LANG_PYTHON,
    VERDICT_AC,
    prepare_program,
    release_program,
    run_prepared,
)
from ..engine.sandbox.agent_guard import check_code_safety
from . import config
from .llm_stage import StageError, StageRunner
from .reference import write_and_anchor
from .stages import EXPLAIN_CHECK

#: 断言数上限：解释不是小作文，超过这个数的多半是把题意复述拆成了碎句。
MAX_CLAIMS = 12


class ExplainError(Exception):
    """带错误码的业务异常，server 层转成 {error, code}。"""

    def __init__(self, message: str, code: str = "INVALID_INPUT") -> None:
        super().__init__(message)
        self.code = code


def _statement_and_samples(db: Any, problem_id: int) -> tuple:
    row = db.query_one(
        "SELECT statement_md, samples_json, judge_type, tolerance FROM problems WHERE id=?",
        (int(problem_id),),
    )
    if row is None:
        raise ExplainError("题目不存在", "NOT_FOUND")
    try:
        samples = json.loads(str(row["samples_json"] or "[]"))
    except json.JSONDecodeError:
        samples = []
    return str(row["statement_md"] or ""), samples if isinstance(samples, list) else [], \
        str(row["judge_type"] or "token"), str(row["tolerance"] or "")


def _user_content(statement: str, samples: Sequence[Dict[str, Any]], ref_code: str) -> str:
    sample_lines = []
    for index, item in enumerate(samples, start=1):
        sample_lines.append(f"[样例{index}]")
        sample_lines.append("输入：")
        sample_lines.append(str(item.get("input", "")))
        sample_lines.append("输出：")
        sample_lines.append(str(item.get("output", "")))
    return (
        "<题目>\n题面：\n" + statement + "\n样例：\n" + "\n".join(sample_lines) + "\n</题目>\n"
        "<参考代码>\n" + ref_code + "\n</参考代码>"
    )


def _parse_report(stdout: str, claims: Sequence[str]) -> List[Dict[str, Any]]:
    """验证程序的 stdout → 报告列表。程序没按契约输出就整体判失败。"""
    text = str(stdout or "").strip()
    # 取最后一行 JSON（程序可能有其他 print；契约要求最后输出 JSON）
    report = None
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("["):
            try:
                report = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(report, list):
        raise ExplainError("验证程序没有按契约输出 JSON 列表", "BAD_VERIFY_OUTPUT")
    for item in report:
        if not isinstance(item, dict) or "claim" not in item or "ok" not in item:
            raise ExplainError("验证程序的输出条目缺 claim/ok 字段", "BAD_VERIFY_OUTPUT")
    # 按 claim 去重（模型写的程序偶尔会把同一判定块重复输出多遍）：
    # 同一断言以首次判定为准，后面的重复块丢弃
    deduped: List[Dict[str, Any]] = []
    seen: set = set()
    for item in report:
        key = str(item.get("claim", "")).strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    report = deduped
    # 条数与断言对不上：以程序输出为准，但把没覆盖到的断言标记为未验证
    covered = {str(item.get("claim", "")).strip() for item in report}
    for claim in claims:
        if str(claim).strip() not in covered:
            report.append({"claim": str(claim), "ok": None, "actual": "（验证程序未覆盖此断言）"})
    return report


def run_explain_check(db: Any, runner: StageRunner, problem_id: int, user: str) -> Dict[str, Any]:
    """对一道题跑完整的解释反推验证，返回报告。"""
    pid = int(problem_id)
    statement, samples, judge_type, tolerance = _statement_and_samples(db, pid)
    if not samples:
        raise ExplainError("这道题没有样例，无法做代码反推验证", "NO_SAMPLES")

    # 1. D1R 参考代码 + 样例锚定：代码没过样例，后面一切都不可信
    prepared = write_and_anchor(
        runner, _user_content(statement, samples, ""),  # 先不给参考代码（这一步就是在生成它）
        samples=samples, judge_type=judge_type, tolerance=tolerance,
    )
    ref_result = prepared.result
    if not ref_result.engaged:
        raise ExplainError(f"参考代码不可用：{ref_result.reason}", "REF_UNAVAILABLE")

    try:
        # 2. T4：抽断言 + 写验证程序
        payload = runner.call(EXPLAIN_CHECK, _user_content(statement, samples, ref_result.code))
        claims = payload.get("claims")
        verify_code = str(payload.get("verify_code") or "")
        if not isinstance(claims, list):
            raise ExplainError("模型输出不合格：claims 必须是列表", "BAD_CLAIMS")
        claims = [str(item).strip() for item in claims if str(item).strip()]
        if len(claims) > MAX_CLAIMS:
            claims = claims[:MAX_CLAIMS]

        if not claims:
            report, status, note = [], "no_claims", "样例解释里没有可计算的断言（只复述了题意），无需验证"
        else:
            # 3. 验证程序：安全守卫 → 沙箱执行 → stdout 即报告
            violation = check_code_safety(verify_code)
            if violation is not None:
                raise ExplainError(
                    f"验证程序未通过安全守卫（{violation.rule}：{violation.detail[:120]}）",
                    "UNSAFE_CODE",
                )
            program = prepare_program(LANG_PYTHON, verify_code)
            if not program.ok:
                raise ExplainError(f"验证程序无法运行：{program.error or 'prepare_failed'}", "BAD_VERIFY_CODE")
            try:
                run = run_prepared(program, "", int(config.REFERENCE_TIMEOUT_MS))
            finally:
                release_program(program)
            if run["verdict"] != VERDICT_AC:
                raise ExplainError(
                    f"验证程序运行失败（{run['verdict']}，{run['elapsed_ms']:.0f} ms）", "VERIFY_CRASHED"
                )
            report = _parse_report(str(run["stdout"]), claims)
            # 锚点自检：验证程序连样例输出都算不对时，它对断言的一切判定都不可信。
            # 没有这条，验证程序自己的 bug 会伪装成"解释有错"（2026-11-25 真机实测撞到）。
            # 用固定前缀精确匹配——明细自检条目文案里也含"锚点"字样，子串匹配会误伤。
            anchor_bad = any(
                str(item.get("claim", "")).startswith("（锚点）") and item.get("ok") is not True
                for item in report
            )
            # 明细自检（v2）：总账对、分项明细错的情况锚点拦不住——验证程序把
            # 每段买油记到段起点而不是前缀最低价站点，"站点2买5升"被误判不一致
            # （2026-11-26 真机撞到）。明细不变量不过 → 数量断言整体不可信。
            # 枚举自检（v3）：贪心算对、枚举漏解，两条路径互相矛盾也没被拦住
            # （2026-11-26 第二次真机撞到）——贪心明细必须出现在枚举集合中。
            detail_bad = any(
                (str(item.get("claim", "")).startswith("（明细自检）")
                 or str(item.get("claim", "")).startswith("（枚举自检）"))
                and item.get("ok") is not True
                for item in report
            )
            ok_count = sum(1 for item in report if item.get("ok") is True)
            if anchor_bad:
                status = "self_check_failed"
                note = "验证程序重算的最终答案与样例输出矛盾——重算不可信，报告仅供参考，需人工复核"
            elif detail_bad:
                status = "self_check_failed"
                note = "验证程序的明细/枚举自检未通过——分项明细与总账矛盾或枚举漏解，数量类断言不可信，需人工复核"
                # 兜底降级：程序没按契约自行置 null 的，服务端统一降级
                for item in report:
                    claim_text = str(item.get("claim", ""))
                    if "锚点" in claim_text or "明细自检" in claim_text:
                        continue
                    if item.get("ok") is not None:
                        item["ok"] = None
                        item["actual"] = str(item.get("actual") or "") + "（明细自检未通过，本条结论不可信）"
            else:
                status = "checked"
                note = "" if ok_count == len(report) else "存在不一致的断言，题面样例解释需要人工复核"
    finally:
        release_program(prepared.program)

    ok_count = sum(1 for item in report if item.get("ok") is True)
    report_row = {
        "problem_id": pid,
        "user": str(user),
        "claims_json": json.dumps(claims, ensure_ascii=False),
        "report_json": json.dumps(report, ensure_ascii=False),
        "ok_count": ok_count,
        "total": len(report),
        "status": status,
        "note": note,
        "model": runner.last_model,
        "prompt_version": EXPLAIN_CHECK.version,
    }
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO explain_checks(problem_id, user, claims_json, report_json,"
            " ok_count, total, status, note, model, prompt_version, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?, datetime('now'))"
            " ON CONFLICT(problem_id) DO UPDATE SET user=excluded.user,"
            " claims_json=excluded.claims_json, report_json=excluded.report_json,"
            " ok_count=excluded.ok_count, total=excluded.total, status=excluded.status,"
            " note=excluded.note, model=excluded.model, prompt_version=excluded.prompt_version,"
            " created_at=excluded.created_at",
            (pid, str(user), report_row["claims_json"], report_row["report_json"],
             ok_count, len(report), status, note, runner.last_model, EXPLAIN_CHECK.version),
        )
    return {"ok": True, **report_row}


def explain_status(db: Any, problem_id: int) -> Dict[str, Any]:
    """读最近一次验证报告（不触发任何调用）。"""
    row = db.query_one("SELECT * FROM explain_checks WHERE problem_id=?", (int(problem_id),))
    if row is None:
        return {"ok": True, "checked": False}
    data = dict(row)
    try:
        data["claims"] = json.loads(str(data.pop("claims_json") or "[]"))
        data["report"] = json.loads(str(data.pop("report_json") or "[]"))
    except json.JSONDecodeError:
        data["claims"], data["report"] = [], []
    data.pop("ref_code", None)
    data["checked"] = True
    return {"ok": True, **data}
