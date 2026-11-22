"""产物门（v4 契约）：写 `artifact_version` 之前，让**库里的产物**自证流程确实走完了。

为什么必须有这一步（历史事故）：
`prepare` 里「逐行解析 / 题解 / 复杂度审计」都是软阶段——失败只 `_note`、不回滚，
因为任何一处失手都不该把整题废掉。但旧行为紧接着就无条件盖上当前版本号，
于是「只有 1 种解法、解析还是旧整段文字」的题也被标成新版；
`select_targets` 认定它已升级 ⇒ 永远不会再重跑。表现出来就是：
**代码改了、流程也跑了，界面还是旧内容**。

所以版本号必须是**证据**，不是打卡：

- 只有全部检查通过，才允许落 `artifact_version=当前版本`；
- 任一不通过 → `StageFailure(STAGE_ARTIFACTS)` → 整题回滚到旧产物并标 failed，
  下次 `prepare --all` 会自动把它重新选中。

判据全部读库（落库的产物），不读内存里的"我生成了"——那样等于让考生自己批卷。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .data_plan import MIN_TOTAL
from .line_notes import MAX_METHODS, MIN_COVERAGE, is_current_notes
from .prep_models import STAGE_ARTIFACTS, StageFailure
from .solutions import same_structure

#: 由 prepare 产出的分层（adversarial 依赖学生错解，单独入口生成，不在契约内）
GENERATED_LAYERS = ("boundary", "exhaustive", "stress")


def _load_json(raw: Any) -> Optional[Any]:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _notes_ok(raw: Any) -> bool:
    """逐行结构合格：v3 + 行表非空 + 非空代码行的讲解覆盖率达到 MIN_COVERAGE。"""
    if not is_current_notes(raw):
        return False
    data = _load_json(raw) or {}
    lines = data.get("lines") or []
    if not isinstance(lines, list) or not lines:
        return False
    non_empty = 0
    explained = 0
    for item in lines:
        if not isinstance(item, dict):
            continue
        if str(item.get("code") or "").strip():
            non_empty += 1
            if str(item.get("explain") or "").strip():
                explained += 1
    return non_empty > 0 and explained >= non_empty * MIN_COVERAGE


def _methods(rows: List[Dict[str, Any]]) -> List[str]:
    """列出**界面上真能切换**的解法名（判据要与 UI 口径一致）。

    为什么必须算暴力解：`hints` 会给它单独一组 L2/L3/L4，`代码解析` 也会给它出逐行讲解，
    学生点开就能切换——那它就是"第二种解法"。历史口径把它排除，导致「模拟 + 暴力枚举」
    这种组合被判成单解，白跑一遍还回滚。
    但**换壳的暴力解不算**：控制流和主解一样，只是变量名不同，不是另一种思路。
    """
    ordered = sorted(rows, key=lambda r: str(r.get("role") or ""))
    picks = [row for row in ordered if str(row.get("role") or "") != "brute"]
    for row in ordered:
        if str(row.get("role") or "") != "brute" or not int(row.get("verified") or 0):
            continue
        code = str(row.get("code") or "")
        lang = str(row.get("lang") or "")
        if not code.strip():
            continue
        if any(
            str(other.get("lang") or "") == lang
            and same_structure(code, str(other.get("code") or ""))
            for other in picks
        ):
            continue
        picks.append(row)
    names: List[str] = []
    for row in picks:
        name = str(row.get("approach") or "").strip()
        if name and name not in names:
            names.append(name)
    return names


def audit(
    store: Any, problem_id: int, *, strict: bool = True, profile: str = "full"
) -> List[str]:
    """返回不符合契约的问题清单；空列表 = 可以盖章。

    `profile="full"`：完整版五项契约（解法 / 逐行解析 / 提示 / 分层数据 / 题解）。
    `profile="data"`：精简版数据门，只校验「参考解 + 分层数据」，
    不查提示、逐行解析、题解与多解法——精简版根本不生成这些产物。

    `strict=False` 用于离线/测试模式（`--no-llm-extras`）：那一档本来就不生成
    备选解、逐行解析和题解，只校验「接线是否通」（解法、提示分组、有数据），
    这种产物**不会**被盖 v4 版本号。
    """
    db = store.db
    pid = int(problem_id)
    issues: List[str] = []
    full = str(profile or "full") != "data"

    refs = [dict(row) for row in db.query_all(
        "SELECT lang, role, approach, verified, code, line_notes_json"
        " FROM reference_solutions WHERE problem_id=?",
        (pid,),
    )]
    methods = _methods(refs)
    if full and strict and len(methods) < 2:
        issues.append(
            f"解法只有 {len(methods)} 种（要求 ≥2 种不同思路）"
            + (f"：{methods[0]}" if methods else "")
        )

    # 代码解析：每种可切换解法各一份逐行对照（cpp 不单独出解析，与 line_notes.generate 同口径）
    if full:
        shown = set(methods)
        py = [
            row for row in sorted(refs, key=lambda r: str(r.get("role") or ""))
            if str(row.get("lang") or "") == "python"
            and str(row.get("approach") or "").strip() in shown
            and int(row.get("verified") or 0)
        ]
        want = min(len(py), MAX_METHODS) if strict else 0
        got = sum(1 for row in py if _notes_ok(row.get("line_notes_json")))
        if want and got < want:
            issues.append(f"逐行代码解析 {got}/{want} 份（其余仍是旧版整段讲解或未生成）")

    # 提示：L1 通用 + 每种解法的 L2/L3
    if full:
        hints = [dict(row) for row in db.query_all(
            "SELECT level, approach FROM hints WHERE problem_id=? AND is_active=1", (pid,)
        )]
        levels: Dict[str, set] = {}
        for row in hints:
            levels.setdefault(str(row.get("approach") or ""), set()).add(int(row.get("level") or 0))
        if 0 not in levels.get("", set()):
            issues.append("提示缺 L1（通用方向提示）")
        missing = [name for name in methods if not {2, 3} <= levels.get(name, set())]
        if missing:
            issues.append("提示缺 L2/L3：" + "、".join(missing))

    # 分层数据：条数下限 + 至少覆盖边界与小规模两档（两种档案都查）
    rows = [dict(row) for row in db.query_all(
        "SELECT layer, COUNT(*) AS c FROM test_cases"
        " WHERE problem_id=? AND layer!='sample' GROUP BY layer",
        (pid,),
    )]
    counts = {str(row["layer"]): int(row["c"]) for row in rows}
    total = sum(count for layer, count in counts.items() if layer in GENERATED_LAYERS)
    required = bool(strict or not full)  # 数据门不因离线档而放宽
    if required and total < MIN_TOTAL:
        issues.append(f"非样例数据 {total} 条（要求 ≥{MIN_TOTAL}）")
    elif not required and total < 1:
        issues.append("没有任何非样例数据")
    empty = [layer for layer in ("boundary", "exhaustive") if not counts.get(layer)]
    if empty and required:
        issues.append("缺少分层数据：" + "、".join(empty))

    # 题解：题干转述 + 思考路径（旧版可能只有一段算法说明）
    row = db.query_one("SELECT analysis_json FROM problems WHERE id=?", (pid,))
    if full and strict:
        analysis = _load_json(row["analysis_json"] if row else None) or {}
        if not str(analysis.get("plain_question") or "").strip():
            issues.append("题解缺 plain_question")
        if not analysis.get("thinking_path"):
            issues.append("题解缺 thinking_path")
    return issues


def gate_stage(service: Any, problem_id: int):
    """流水线的 ⑦ 产物门：不合格抛 StageFailure（由 `_run_stage` 记档并触发出题回滚）。"""
    strict = bool(getattr(service, "enable_llm_extras", True))
    profile = str(getattr(service, "gate_profile", "full") or "full")
    issues = audit(service.store, int(problem_id), strict=strict, profile=profile)
    if issues:
        raise StageFailure(
            STAGE_ARTIFACTS,
            "产物未达到 v4 契约：" + "；".join(issues),
            detail="；".join(issues),
        )
    if profile == "data":
        return None, "数据门通过：参考解已验证，分层数据齐全"
    if not strict:
        return None, "离线模式：仅校验解法/提示/数据（不生成逐行解析与题解，不盖 v4 版本号）"
    return None, "解法/逐行解析/提示/分层数据/题解 五项契约全部通过"
