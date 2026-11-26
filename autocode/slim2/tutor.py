"""教学层：建模探针 + 五步提示。

与数据链（dataset/adversarial）完全独立的第三条业务线。设计约束来自
《建模助手-探针与询问规范》与《编程题助教的五步提示法》两份操作规范：

- **判对错只用两个仪器——反例与规模**。探针判定是精确匹配（学生指层 + 错型），
  核对型提问是关键词匹配，L3 挖空是归一化比对——运行期除 L4 外零 LLM 调用，
  模型只在生成探针包 / 提示包时出场一次，之后无权决定任何级别与判定。
- **一轮只走一级**：追问提示逐级下降（反后果问 → 指位置 → 给事实），文案在
  生成探针包时一次出齐，运行期按轮数取用，不现编。
- **档位浮动不对称**（降级要敏感，升级要谨慎）：某层连续两题"找出"→ 免检；
  免检层一出错 → 立即恢复详查并清零连击。纯查表，无训练数据。
- **级别推进由状态机决定**：判断型（预测/解释/草图/失败描述）答了就放行、
  答错也放行；核对型答错不放行，只把题面那一句指回去重读。
- **全程留痕**：probe_records 聚成 (层 × 错因) 计数向量，hint_log 按
  读题/建模/复杂度/实现/策略 五类归因——向量本身即班级诊断报告。

免检口径（v1 的诚实说明）：免检层发题时直接放行（记 outcome=exempt），
"免检期间一出错恢复详查"的触发源在代码环节，留待判题链路接入后启用；
本版本该规则已实现（`tier_reset`），只是没有自动触发源。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from . import config  # noqa: F401  （超时分钟数后续可挪进 config；先保留导入口径）
from .api_common import _loads, _now
from .llm_stage import StageError, StageRunner
from .stages import JUDGE, LADDER, LADDER_L4, PROBE, REACT

# ── 常量 ────────────────────────────────────────────────────────────────────

#: 四栏表：layer 序号 1~4 → 展示名
LAYER_NAMES: Dict[int, str] = {1: "① 输入", 2: "② 输出", 3: "③ 转移", 4: "④ 状态"}

#: 探针只埋这两类错，判据明确；含糊的错（表述不当之类）一律拒绝入库
ERROR_TYPES: Tuple[str, ...] = ("missing", "redundant")
ERROR_TYPE_NAMES: Dict[str, str] = {"missing": "缺一项", "redundant": "多一项"}

STATE_TYPES: Tuple[str, ...] = ("积累", "演化", "匹配", "搜索", "维护")

#: 追问轮数上限：三轮不中 → 代填 + 讲解 + 记"需重教"（规范第四节）
ROUND_LIMIT = 3

#: 档位浮动：连续两题找出 → 免检（规范第五节，不对称规则）
EXEMPT_STREAK = 2

TIER_DETAIL = "detail"
TIER_EXEMPT = "exempt"

#: 五步提示法
ATTRIBUTIONS: Tuple[str, ...] = ("读题", "建模", "复杂度", "实现", "策略")
LADDER_LEVELS = 4

#: 停留超时（分）弹出本级提问：读题/建模 15 分钟，实现/调试 30 分钟（规范二节）
TIMEOUT_MIN_READ = 15
TIMEOUT_MIN_IMPL = 30
#: attribution → 超时口径的映射；实现/策略/复杂度按"动手类"算
_IMPL_LIKE = ("复杂度", "实现", "策略")

#: 同一考点标签第 N 次做到 → 起始级别 = N-1；第 3 次起梯子塌缩成 L1 + L4
COLLAPSED_FROM = 3

#: 探针/提示包生成的 JSON 校验失败时，最多带反馈重试的次数
MAX_PACK_ATTEMPTS = 2


class TutorError(RuntimeError):
    """教学层可预期的失败（生成不合格 / 状态冲突），消息直接给人看。"""

    def __init__(self, message: str, code: str = "TUTOR_FAILED") -> None:
        super().__init__(message)
        self.code = code


# ── 小工具 ──────────────────────────────────────────────────────────────────

def _norm(text: Any) -> str:
    """归一化：去空白、转小写。用于核对型匹配与挖空比对。"""
    return re.sub(r"\s+", "", str(text or "")).lower()


def _student_profile(db: Any, user: str, layer: int) -> Dict[str, Any]:
    """(user, layer) 的档位查表行，没有就当场建（连击从 0、详查起）。"""
    row = db.query_one(
        "SELECT streak, tier FROM probe_tiers WHERE user=? AND layer=?", (str(user), int(layer))
    )
    if row is not None:
        return {"streak": int(row["streak"]), "tier": str(row["tier"])}
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO probe_tiers(user, layer, streak, tier, updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(user, layer) DO NOTHING",
            (str(user), int(layer), 0, TIER_DETAIL, _now()),
        )
    return {"streak": 0, "tier": TIER_DETAIL}


def _tier_promote(db: Any, user: str, layer: int) -> str:
    """该层"找出"一次：连击 +1，连续 EXEMPT_STREAK 次 → 免检。返回新档位。
    免检后继续找出保持免检（连击继续涨），只有出错才清零。"""
    profile = _student_profile(db, user, layer)
    streak = int(profile["streak"]) + 1
    tier = TIER_EXEMPT if (profile["tier"] == TIER_EXEMPT or streak >= EXEMPT_STREAK) else TIER_DETAIL
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO probe_tiers(user, layer, streak, tier, updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(user, layer) DO UPDATE SET streak=excluded.streak,"
            " tier=excluded.tier, updated_at=excluded.updated_at",
            (str(user), int(layer), streak, tier, _now()),
        )
    return tier


def tier_reset(db: Any, user: str, layer: int) -> None:
    """免检层一出错：恢复详查并清零（不对称规则里"降级要敏感"的那一半）。"""
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO probe_tiers(user, layer, streak, tier, updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(user, layer) DO UPDATE SET streak=0, tier=?, updated_at=excluded.updated_at",
            (str(user), int(layer), 0, TIER_DETAIL, _now(), TIER_DETAIL),
        )


def _record_probe(
    db: Any, problem_id: int, user: str, layer: int, error_type: str,
    outcome: str, attempts: int, tier: str,
) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO probe_records(problem_id, user, layer, error_type, outcome,"
            " attempts, tier, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (int(problem_id), str(user), int(layer), str(error_type), str(outcome),
             int(attempts), str(tier), _now()),
        )


def _log_hint(
    db: Any, problem_id: int, user: str, level: int, trigger: str, kind: str,
    outcome: str, attribution: str, detail: str = "",
) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO hint_log(problem_id, user, level, trigger, kind, outcome,"
            " attribution, detail, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (int(problem_id), str(user), int(level), str(trigger), str(kind),
             str(outcome), str(attribution), str(detail)[:300], _now()),
        )


# ── 建模探针：生成 ──────────────────────────────────────────────────────────

def _probe_user_content(row: Any, samples: List[Dict[str, Any]]) -> str:
    constraints = _loads(row["constraints_json"], [])
    lines = [
        "<题目>",
        f"标题：{row['title']}",
        "题面：",
        str(row["statement_md"]),
        "样例：",
    ]
    for index, sample in enumerate(samples, start=1):
        lines.append(f"[样例{index}]")
        lines.append("输入：")
        lines.append(str(sample.get("input") or ""))
        lines.append("输出：")
        lines.append(str(sample.get("output") or ""))
    if constraints:
        lines.append("约束：")
        lines.extend(str(item) for item in constraints)
    lines.append("</题目>")
    return "\n".join(lines)


def _validate_probe_pack(payload: Any) -> Dict[str, Any]:
    """严格校验模型出的探针包；不合格抛 TutorError，消息作为反馈回灌重试。"""
    if not isinstance(payload, dict):
        raise TutorError("模型输出不是 JSON 对象")
    if str(payload.get("state_type") or "") not in STATE_TYPES:
        raise TutorError(f"state_type 必须是 {'/'.join(STATE_TYPES)} 之一")
    if not str(payload.get("state_type_note") or "").strip():
        raise TutorError("state_type_note 不能为空")
    table = payload.get("table")
    if not isinstance(table, dict) or set(table) != {"1", "2", "3", "4"}:
        raise TutorError("table 必须是键 1~4 的四栏表")
    for key in ("1", "2", "3", "4"):
        if not str(table.get(key) or "").strip():
            raise TutorError(f"四栏表第 {key} 栏为空")
    probes = payload.get("probes")
    if not isinstance(probes, list) or len(probes) != 4:
        raise TutorError("probes 必须恰好 4 个（一层一个探针）")
    for index, probe in enumerate(probes, start=1):
        if not isinstance(probe, dict):
            raise TutorError(f"第 {index} 个探针不是对象")
        if int(probe.get("layer") or 0) != index:
            raise TutorError(f"第 {index} 个探针的 layer 应为 {index}，按 1→4 顺序给")
        if str(probe.get("error_type") or "") not in ERROR_TYPES:
            raise TutorError(
                f"探针 {index} 的 error_type 只允许 {'/'.join(ERROR_TYPES)}；"
                "含糊的错（表述不当之类）不许埋"
            )
        for field in ("planted", "correct", "should_find", "explain"):
            if not str(probe.get(field) or "").strip():
                raise TutorError(f"探针 {index} 缺 {field}")
        followups = probe.get("followups")
        if not isinstance(followups, list) or len(followups) != ROUND_LIMIT:
            raise TutorError(f"探针 {index} 的 followups 必须恰好 {ROUND_LIMIT} 条（逐级下降）")
        for item in followups:
            if not str(item or "").strip():
                raise TutorError(f"探针 {index} 的 followups 有空条目")
    return payload


def ensure_probe_pack(db: Any, runner: StageRunner, problem_id: int) -> Dict[str, Any]:
    """取本题探针包；没有就生成（一次对话，带结构反馈重试）。"""
    pid = int(problem_id)
    row = db.query_one("SELECT * FROM probe_packs WHERE problem_id=?", (pid,))
    if row is not None:
        return {
            "state_type": str(row["state_type"]),
            "state_type_note": str(row["state_type_note"]),
            "table": _loads(row["table_json"], {}),
            "probes": _loads(row["probes_json"], []),
        }
    problem = db.query_one("SELECT * FROM problems WHERE id=?", (pid,))
    if problem is None:
        raise KeyError(problem_id)
    samples = _loads(problem["samples_json"], [])
    user_content = _probe_user_content(problem, samples)
    feedback = ""
    last_error = ""
    for attempt in range(1, MAX_PACK_ATTEMPTS + 1):
        try:
            payload = _validate_probe_pack(runner.call(PROBE, user_content + feedback))
        except (StageError, TutorError) as exc:
            last_error = str(exc)
            feedback = (
                f"\n\n上一次输出不合格：{last_error}\n"
                "请严格按契约重出完整 JSON：state_type 五选一；table 键 1~4 全非空；"
                "probes 恰好 4 个、layer 按 1→4；每探针 followups 恰好 3 条；"
                "error_type 只允许 missing/redundant。"
            )
            continue
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO probe_packs(problem_id, state_type, state_type_note,"
                " table_json, probes_json, model, prompt_version, created_at)"
                " VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(problem_id) DO NOTHING",
                (pid, str(payload["state_type"]), str(payload["state_type_note"]),
                 json.dumps(payload["table"], ensure_ascii=False),
                 json.dumps(payload["probes"], ensure_ascii=False),
                 runner.last_model, PROBE.version, _now()),
            )
        return payload
    raise TutorError(f"探针包连续 {MAX_PACK_ATTEMPTS} 次不合格：{last_error}")


# ── 建模探针：进度与判定 ────────────────────────────────────────────────────

def _ensure_progress(db: Any, problem_id: int, user: str) -> Any:
    pid, account = int(problem_id), str(user)
    row = db.query_one(
        "SELECT * FROM probe_progress WHERE problem_id=? AND user=?", (pid, account)
    )
    if row is not None:
        return row
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO probe_progress(problem_id, user, active_layer, round, tier,"
            " done, passed_json, created_at, updated_at) VALUES(?,?,1,0,?,0,'[]',?,?)"
            " ON CONFLICT(problem_id, user) DO NOTHING",
            (pid, account, TIER_DETAIL, _now(), _now()),
        )
    return db.query_one(
        "SELECT * FROM probe_progress WHERE problem_id=? AND user=?", (pid, account)
    )


def _probe_view(pack: Dict[str, Any], progress: Any, user: str) -> Dict[str, Any]:
    """界面的探针视图：整张表可见，但只有当前激活层埋着错；
    已过的层显示正确内容；未来层显示正确内容（学生要找出"唯一那处错"）。
    绝不外泄：error_type / correct / followups / explain / should_find 不出接口。"""
    active = int(progress["active_layer"])
    table = dict(pack["table"])
    probes = {int(item["layer"]): item for item in pack["probes"]}
    if active <= 4 and not int(progress["done"]):
        table[str(active)] = str(probes[active]["planted"])
    rows = [
        {
            "layer": layer,
            "name": LAYER_NAMES[layer],
            "content": str(table[str(layer)]),
            "status": (
                "passed" if layer < active or int(progress["done"]) else
                "active" if layer == active and not int(progress["done"]) else "locked"
            ),
        }
        for layer in range(1, 5)
    ]
    return {
        "state_type": str(pack["state_type"]),
        "state_type_note": str(pack["state_type_note"]),
        "rows": rows,
        "active_layer": active,
        "round": int(progress["round"]),
        "done": bool(int(progress["done"])),
        "passed": _loads(progress["passed_json"], []),
        "error_types": [{"value": value, "name": ERROR_TYPE_NAMES[value]} for value in ERROR_TYPES],
        "tier": str(progress["tier"]),
    }


def probe_status(db: Any, problem_id: int, user: str) -> Dict[str, Any]:
    pack_row = db.query_one("SELECT * FROM probe_packs WHERE problem_id=?", (int(problem_id),))
    if pack_row is None:
        return {"ok": True, "started": False}
    pack = {
        "state_type": str(pack_row["state_type"]),
        "state_type_note": str(pack_row["state_type_note"]),
        "table": _loads(pack_row["table_json"], {}),
        "probes": _loads(pack_row["probes_json"], []),
    }
    progress = _ensure_progress(db, int(problem_id), str(user))
    view = _probe_view(pack, progress, str(user))
    # 每层的个人档位（放行卡上要显示"本层免检中"）
    view["tiers"] = {
        layer: _student_profile(db, user, layer)["tier"] for layer in range(1, 5)
    }
    return {"ok": True, "started": True, "probe": view}


def start_probe(db: Any, runner: StageRunner, problem_id: int, user: str) -> Dict[str, Any]:
    pack = ensure_probe_pack(db, runner, int(problem_id))
    progress = _ensure_progress(db, int(problem_id), str(user))
    # 免检层直接放行（发题那一刻就该跳过，而不是让学生对着免检层白找一轮）
    active = int(progress["active_layer"])
    passed = _loads(progress["passed_json"], [])
    while active <= 4 and _student_profile(db, user, active)["tier"] == TIER_EXEMPT:
        passed = (passed + ["exempt"])[:4]
        _record_probe(db, int(problem_id), user, active, "", "exempt", 0, TIER_EXEMPT)
        active += 1
    done = active > 4
    with db.transaction() as conn:
        conn.execute(
            "UPDATE probe_progress SET active_layer=?, round=0, done=?, passed_json=?,"
            " updated_at=? WHERE id=?",
            (active, 1 if done else 0, json.dumps(passed, ensure_ascii=False), _now(),
             int(progress["id"])),
        )
    return probe_status(db, int(problem_id), user)


def probe_answer(db: Any, problem_id: int, user: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """学生指认：哪一层、什么错型。判定是精确匹配，不调模型。"""
    pid, account = int(problem_id), str(user)
    progress = _ensure_progress(db, pid, account)
    if int(progress["done"]):
        return {"error": "这道题的探针已经全部通过，不用再答了", "code": "ALREADY_DONE"}
    pack_row = db.query_one("SELECT * FROM probe_packs WHERE problem_id=?", (pid,))
    if pack_row is None:
        return {"error": "这道题还没有探针，请先点「开始探针」", "code": "NOT_STARTED"}
    probes = {int(item["layer"]): item for item in _loads(pack_row["probes_json"], [])}
    active = int(progress["active_layer"])
    round_no = int(progress["round"])
    tier_now = str(progress["tier"])

    said_layer = int(payload.get("layer") or 0)
    said_type = str(payload.get("error_type") or "").strip().lower()
    if said_layer not in range(1, 5):
        return {"error": "请先选出你认为埋错的那一层（①~④）", "code": "INVALID_INPUT"}
    if said_type not in ERROR_TYPES:
        return {"error": "错型只允许「缺一项」或「多一项」", "code": "INVALID_INPUT"}

    planted = probes[active]
    found = said_layer == active and said_type == str(planted["error_type"])
    passed = _loads(progress["passed_json"], [])

    if found:
        attempts = round_no + 1
        new_tier = _tier_promote(db, account, active)
        passed = (passed + ["found"])[:4]
        _record_probe(db, pid, account, active, str(planted["error_type"]), "found",
                      attempts, new_tier)
        next_layer = active + 1
        # 下一层若免检，同样直接放行
        while next_layer <= 4 and _student_profile(db, account, next_layer)["tier"] == TIER_EXEMPT:
            passed = (passed + ["exempt"])[:4]
            _record_probe(db, pid, account, next_layer, "", "exempt", 0, TIER_EXEMPT)
            next_layer += 1
        done = next_layer > 4
        with db.transaction() as conn:
            conn.execute(
                "UPDATE probe_progress SET active_layer=?, round=0, tier=?, done=?,"
                " passed_json=?, updated_at=? WHERE id=?",
                (next_layer, tier_now, 1 if done else 0, json.dumps(passed, ensure_ascii=False),
                 _now(), int(progress["id"])),
            )
        result: Dict[str, Any] = {
            "ok": True, "verdict": "found", "layer": active,
            "should_find": str(planted["should_find"]),
            "next_layer": next_layer if not done else None,
            "done": done, "tier": new_tier,
        }
        if not done and new_tier == TIER_EXEMPT:
            # 放行三动作之一：明示 + 前瞻挂一句（规范第四节），不表扬、不展开
            result["note"] = "本层已连续两题找出，转入免检。"
        return result

    # 没找出：追问一轮，提示逐级下降；三轮不中 → 代填 + 讲解 + 记"需重教"
    round_no += 1
    if round_no >= ROUND_LIMIT:
        tier_reset(db, account, active)
        passed = (passed + ["reteach"])[:4]
        _record_probe(db, pid, account, active, str(planted["error_type"]), "reteach",
                      ROUND_LIMIT, TIER_DETAIL)
        next_layer = active + 1
        while next_layer <= 4 and _student_profile(db, account, next_layer)["tier"] == TIER_EXEMPT:
            passed = (passed + ["exempt"])[:4]
            _record_probe(db, pid, account, next_layer, "", "exempt", 0, TIER_EXEMPT)
            next_layer += 1
        done = next_layer > 4
        with db.transaction() as conn:
            conn.execute(
                "UPDATE probe_progress SET active_layer=?, round=0, tier=?, done=?,"
                " passed_json=?, updated_at=? WHERE id=?",
                (next_layer, TIER_DETAIL, 1 if done else 0, json.dumps(passed, ensure_ascii=False),
                 _now(), int(progress["id"])),
            )
        return {
            "ok": True, "verdict": "reteach", "layer": active,
            "correct": str(planted["correct"]),
            "explain": str(planted["explain"]),
            "next_layer": next_layer if not done else None,
            "done": done,
        }

    with db.transaction() as conn:
        conn.execute(
            "UPDATE probe_progress SET round=?, updated_at=? WHERE id=?",
            (round_no, _now(), int(progress["id"])),
        )
    followups = [str(item) for item in planted["followups"]]
    return {
        "ok": True, "verdict": "missed", "layer": active, "round": round_no,
        "followup": followups[round_no - 1],
    }


# ── 五步提示：提示包生成 ────────────────────────────────────────────────────

def _validate_ladder_pack(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise TutorError("模型输出不是 JSON 对象")
    tag = str(payload.get("tag") or "").strip()
    if not 2 <= len(tag) <= 12:
        raise TutorError("tag 必须是 2~12 个字的考点标签")
    if str(payload.get("attribution") or "") not in ATTRIBUTIONS:
        raise TutorError(f"attribution 必须是 {'/'.join(ATTRIBUTIONS)} 之一")
    zero = payload.get("L0")
    if not isinstance(zero, dict) or not str(zero.get("question") or "").strip():
        raise TutorError("L0.question 不能为空")
    kind = str(zero.get("kind") or "")
    if kind not in ("judgment", "check"):
        raise TutorError("L0.kind 必须是 judgment / check")
    if kind == "check":
        expected = zero.get("expected")
        if not isinstance(expected, list) or not [str(x) for x in expected if str(x).strip()]:
            raise TutorError("check 型必须给 expected（可接受说法清单，判定是精确匹配）")
    one = payload.get("L1")
    if not isinstance(one, dict) or not str(one.get("quote") or "").strip() \
            or not str(one.get("content") or "").strip():
        raise TutorError("L1 需要 quote（逐字引用题面）与 content 两项")
    two = payload.get("L2")
    if not isinstance(two, dict) or not str(two.get("fact") or "").strip():
        raise TutorError("L2.fact 不能为空")
    three = payload.get("L3")
    if not isinstance(three, dict) or not str(three.get("lead") or "").strip():
        raise TutorError("L3.lead 不能为空")
    blanks = three.get("blanks")
    if not isinstance(blanks, list) or len(blanks) != 3:
        raise TutorError("L3.blanks 必须恰好 3 个空")
    for index, blank in enumerate(blanks, start=1):
        if not isinstance(blank, dict) or not str(blank.get("hint") or "").strip() \
                or not str(blank.get("answer") or "").strip():
            raise TutorError(f"L3 第 {index} 个空缺 hint 或 answer")
        accepts = [str(x) for x in (blank.get("accept") or []) if str(x).strip()]
        blank["accept"] = accepts or [str(blank["answer"])]
    return payload


def _ladder_user_content(row: Any, samples: List[Dict[str, Any]], start_level: int) -> str:
    constraints = _loads(row["constraints_json"], [])
    lines = ["<题目>", f"标题：{row['title']}", "题面：", str(row["statement_md"]), "样例："]
    for index, sample in enumerate(samples, start=1):
        lines.append(f"[样例{index}]")
        lines.append("输入：")
        lines.append(str(sample.get("input") or ""))
        lines.append("输出：")
        lines.append(str(sample.get("output") or ""))
    if constraints:
        lines.append("约束：")
        lines.extend(str(item) for item in constraints)
    lines.append("</题目>")
    lines.append(f"<起始级别>\nL{min(3, max(0, int(start_level)))}  ←（系统告知：起点上移）\n</起始级别>")
    return "\n".join(lines)


def _tag_count(db: Any, user: str, tag: str) -> int:
    """这个学生之前做到过几道同标签的题（走到 L3 及以后就算"做到"——
    L3 的步骤骨架过了，说明这条梯子起过作用；不必等到 L4 指错）。"""
    return int(db.query_scalar(
        "SELECT COUNT(*) FROM hint_states s JOIN hint_packs p ON p.problem_id=s.problem_id"
        " WHERE s.user=? AND p.tag=? AND (s.done=1 OR s.level>=3)",
        (str(user), str(tag)), 0,
    ))


def ensure_ladder_pack(
    db: Any, runner: StageRunner, problem_id: int, user: str,
) -> Tuple[Dict[str, Any], int, bool]:
    """取（或生成）提示包，并按考点标签历史决定起始级别与塌缩。

    规范口径：第二次做到同标签 → 起始级别上移一级（L1 起）；
    第三次起只保留 L1 与 L4（塌缩）。返回 (pack, start_level, collapsed)。
    """
    pid = int(problem_id)
    row = db.query_one("SELECT * FROM hint_packs WHERE problem_id=?", (pid,))
    if row is not None:
        pack = _loads(row["pack_json"], {})
        return pack, int(row["start_level"] or 0), bool(int(row["collapsed"] or 0))
    problem = db.query_one("SELECT * FROM problems WHERE id=?", (pid,))
    if problem is None:
        raise KeyError(problem_id)
    # 先占位拿到标签才知道起点；标签未定前按 0 起（首做是绝大多数）
    samples = _loads(problem["samples_json"], [])
    user_content = _ladder_user_content(problem, samples, 0)
    feedback = ""
    last_error = ""
    pack: Optional[Dict[str, Any]] = None
    for attempt in range(1, MAX_PACK_ATTEMPTS + 1):
        try:
            payload = _validate_ladder_pack(runner.call(LADDER, user_content + feedback))
        except (StageError, TutorError) as exc:
            last_error = str(exc)
            feedback = (
                f"\n\n上一次输出不合格：{last_error}\n"
                "请严格按契约重出完整 JSON：tag 2~12 字；attribution 五选一；"
                "L0.kind 为 check 时必须给 expected；L1.quote 逐字引用；L3 恰好 3 空。"
            )
            continue
        pack = payload
        break
    if pack is None:
        raise TutorError(f"提示包连续 {MAX_PACK_ATTEMPTS} 次不合格：{last_error}")

    tag = str(pack["tag"])
    seen = _tag_count(db, user, tag)
    collapsed = seen + 1 >= COLLAPSED_FROM              # 第 3 次起只保留 L1 与 L4
    start_level = 1 if seen >= 1 else 0                 # 第 2 次起 L1 上移一级
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO hint_packs(problem_id, tag, attribution, pack_json,"
            " start_level, collapsed, model, prompt_version, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(problem_id) DO NOTHING",
            (pid, tag, str(pack["attribution"]),
             json.dumps(pack, ensure_ascii=False), start_level,
             1 if collapsed else 0,
             runner.last_model, LADDER.version, _now()),
        )
    return pack, start_level, collapsed


# ── 五步提示：状态机 ────────────────────────────────────────────────────────

def _ladder_content(pack: Dict[str, Any], level: int) -> Dict[str, Any]:
    """本级要展示的内容；只给本级，不预告下一级内容（规范第四节规则一）。"""
    if level == 0:
        zero = pack["L0"]
        return {"kind": str(zero["kind"]), "question": str(zero["question"])}
    if level == 1:
        one = pack["L1"]
        return {"quote": str(one["quote"]), "content": str(one["content"])}
    if level == 2:
        return {"fact": str(pack["L2"]["fact"])}
    return {
        "lead": str(pack["L3"]["lead"]),
        "blanks": [
            {"index": index, "hint": str(blank["hint"])}
            for index, blank in enumerate(pack["L3"]["blanks"], start=1)
        ],
    }


def _timeout_minutes(pack: Dict[str, Any]) -> int:
    attribution = str(pack.get("attribution") or "")
    return TIMEOUT_MIN_IMPL if attribution in _IMPL_LIKE else TIMEOUT_MIN_READ


def _ensure_ladder_state(db: Any, problem_id: int, user: str, start_level: int) -> Any:
    pid, account = int(problem_id), str(user)
    row = db.query_one(
        "SELECT * FROM hint_states WHERE problem_id=? AND user=?", (pid, account)
    )
    if row is not None:
        return row
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO hint_states(problem_id, user, level, answered, done,"
            " started_at, updated_at) VALUES(?,?,?,0,0,?,?)"
            " ON CONFLICT(problem_id, user) DO NOTHING",
            (pid, account, max(0, int(start_level)), _now(), _now()),
        )
    return db.query_one(
        "SELECT * FROM hint_states WHERE problem_id=? AND user=?", (pid, account)
    )


def _has_submission(db: Any, problem_id: int) -> bool:
    """L4 的门槛：没有提交就不给（避免助教被当"先问再写"的代码生成器）。"""
    return int(db.query_scalar(
        "SELECT COUNT(*) FROM submissions WHERE problem_id=?", (int(problem_id),), 0
    )) > 0


def _ladder_view(db: Any, pack: Dict[str, Any], state: Any, collapsed: bool,
                 problem_id: int) -> Dict[str, Any]:
    level = int(state["level"])
    view: Dict[str, Any] = {
        "ok": True,
        "level": level,
        "answered": bool(int(state["answered"])),
        "collapsed": collapsed,
        "attribution": str(pack["attribution"]),
        "tag": str(pack["tag"]),
        "done": bool(int(state["done"])),
        "timeout_minutes": _timeout_minutes(pack),
        "reaction": str(state["reaction"] or "") if "reaction" in state.keys() else "",
    }
    if int(state["answered"]) == 0:
        # L4 是顶层（指你的错），没有"等待回答的问题"——不给 content，
        # 否则前端会把它当未答题渲染成空白（2026-11-26 真机：L4 按钮消失）。
        if level < LADDER_LEVELS:
            view["content"] = _ladder_content(pack, level)
        if level == 0 and str(pack["L0"]["kind"]) == "check":
            # 核对型的答案写在题面里：答错只指回题面那句重读（quote 此时还不给——
            # 它是 L1 的内容；这里只提示"重读题面"）
            view["reread_hint"] = "答案就在题面里，重读一遍再答。"
    else:
        view["awaiting"] = "present"
    view["requires_submission"] = level >= LADDER_LEVELS - 1 or (
        collapsed and level == 1
    )
    view["has_submission"] = _has_submission(db, int(problem_id))
    return view


def ladder_status(db: Any, problem_id: int, user: str) -> Dict[str, Any]:
    pack_row = db.query_one("SELECT * FROM hint_packs WHERE problem_id=?", (int(problem_id),))
    if pack_row is None:
        return {"ok": True, "started": False}
    pack = _loads(pack_row["pack_json"], {})
    start_level = int(pack_row["start_level"] or 0)
    collapsed = bool(int(pack_row["collapsed"] or 0))
    state = _ensure_ladder_state(db, int(problem_id), user, start_level)
    view = _ladder_view(db, pack, state, collapsed, int(problem_id))
    view["started"] = True
    # 停留超时：只弹本级提问，不推进（规范二节）。
    if not int(state["answered"]) and not int(state["done"]):
        from datetime import datetime, timezone

        try:
            updated = datetime.fromisoformat(str(state["updated_at"]))
            minutes = (datetime.now(timezone.utc) - updated).total_seconds() / 60.0
        except ValueError:
            minutes = 0.0
        if minutes >= _timeout_minutes(pack):
            view["timed_out"] = True
            _log_hint(db, int(problem_id), user, int(state["level"]), "timeout",
                      "", "present", str(pack["attribution"]))
    return view


def start_ladder(db: Any, runner: StageRunner, problem_id: int, user: str) -> Dict[str, Any]:
    pack, start_level, collapsed = ensure_ladder_pack(db, runner, int(problem_id), str(user))
    _ensure_ladder_state(db, int(problem_id), user, start_level)
    # 塌缩梯子（同标签第 3 次起）起点就是 L1，L0/L2 的内容存在但不展示
    return ladder_status(db, int(problem_id), user)


def ladder_present(db: Any, problem_id: int, user: str, trigger: str) -> Dict[str, Any]:
    """抛出本级提问。trigger: self（要提示）/ fail2（连续两次没过，跳级）。"""
    pid, account = int(problem_id), str(user)
    pack_row = db.query_one("SELECT * FROM hint_packs WHERE problem_id=?", (pid,))
    if pack_row is None:
        return {"error": "这道题还没有提示包，请先点「要提示」生成", "code": "NOT_STARTED"}
    pack = _loads(pack_row["pack_json"], {})
    start_level = int(pack_row["start_level"] or 0)
    collapsed = bool(int(pack_row["collapsed"] or 0))
    state = _ensure_ladder_state(db, pid, account, start_level)
    if int(state["done"]):
        return {"error": "这道题的梯子已经走完", "code": "ALREADY_DONE"}
    level = int(state["level"])
    if str(trigger or "") == "fail2" and level < (1 if collapsed else 3):
        # 升级触发：跳到下一级（塌缩梯子 L1 之后直接到 L4）
        next_level = 4 if (collapsed and level == 1) else min(level + 1, LADDER_LEVELS - 1)
        _log_hint(db, pid, account, level, "fail2", "", "advanced", str(pack["attribution"]))
        with db.transaction() as conn:
            conn.execute(
                "UPDATE hint_states SET level=?, answered=0, reaction='', updated_at=? WHERE id=?",
                (next_level, _now(), int(state["id"])),
            )
        return ladder_status(db, pid, account)
    if int(state["answered"]) == 0:
        # 上一问没答：只把那道问题再问一遍，不降级也不跳级（规范二节）
        return ladder_status(db, pid, account)
    _log_hint(db, pid, account, level, str(trigger or "self"), "", "present",
              str(pack["attribution"]))
    with db.transaction() as conn:
        conn.execute("UPDATE hint_states SET answered=0, reaction='', updated_at=? WHERE id=?",
                     (_now(), int(state["id"])))
    return ladder_status(db, pid, account)


def _react_user_content(pack: Dict[str, Any], level: int, answer: str,
                        verdict: str, problem: Any, samples: List[Dict[str, Any]]) -> str:
    """反应调用的输入：题面 + 本级已展示内容 + 学生原话 + 状态机判定结果。"""
    lines = ["<题目>", f"标题：{problem['title']}", "题面：", str(problem["statement_md"]), "样例："]
    for index, sample in enumerate(samples, start=1):
        lines.append(f"[样例{index}]")
        lines.append("输入：")
        lines.append(str(sample.get("input") or ""))
        lines.append("输出：")
        lines.append(str(sample.get("output") or ""))
    lines.append("</题目>")
    lines.append("<本级内容>")
    content = _ladder_content(pack, level)
    if level == 0:
        lines.append(f"问题：{content['question']}（判断型）")
    elif level == 1:
        lines.append(f"引用：{content['quote']}")
        lines.append(f"引导：{content['content']}")
    else:
        lines.append(f"事实卡：{content['fact']}")
    lines.append("</本级内容>")
    lines.append(f"<判定>{verdict}</判定>")
    lines.append("<学生回答>")
    lines.append(str(answer or "").strip())
    lines.append("</学生回答>")
    return "\n".join(lines)


def _judge_user_content(pack: Dict[str, Any], level: int, answer: str, answers: List[str],
                        problem: Any, samples: List[Dict[str, Any]]) -> str:
    """判定调用的输入：题面 + 本级内容 + 学生回答（L3 时附标准答案清单）。"""
    lines = ["<题目>", f"标题：{problem['title']}", "题面：", str(problem["statement_md"]), "样例："]
    for index, sample in enumerate(samples, start=1):
        lines.append(f"[样例{index}]")
        lines.append("输入：")
        lines.append(str(sample.get("input") or ""))
        lines.append("输出：")
        lines.append(str(sample.get("output") or ""))
    lines.append("</题目>")
    lines.append("<本级内容>")
    if level == 3:
        for index, blank in enumerate(pack["L3"]["blanks"], start=1):
            accepts = "、".join(str(item) for item in (blank.get("accept") or []))
            lines.append(f"空{index} 提示：{blank['hint']}｜标准答案：{blank['answer']}"
                         f"（可接受：{accepts or '（无补充）'}）")
        lines.append("学生填的空：" + "；".join(
            f"空{i}={value}" for i, value in enumerate(answers, start=1)))
    elif level == 0:
        lines.append(f"问题：{pack['L0']['question']}")
    elif level == 1:
        lines.append(f"引用：{pack['L1']['quote']}")
        lines.append(f"引导：{pack['L1']['content']}")
    else:
        lines.append(f"事实卡：{pack['L2']['fact']}")
    lines.append("</本级内容>")
    lines.append("<学生回答>")
    lines.append(str(answer or "").strip())
    lines.append("</学生回答>")
    return "\n".join(lines)


def _judge_answer(runner: Optional[StageRunner], pack: Dict[str, Any], level: int,
                  answer: str, answers: List[str], problem_id: int) -> Optional[Dict[str, Any]]:
    """AI 判定一个回答字段；返回 None = 降级回查表规则（模型不可用/输出不合格）。"""
    if runner is None:
        return None
    try:
        problem = runner.db.query_one("SELECT * FROM problems WHERE id=?", (int(problem_id),))
        if problem is None:
            return None
        samples = _loads(problem["samples_json"], [])
        payload = runner.call(JUDGE, _judge_user_content(
            pack, level, answer, answers, problem, samples))
        verdict = str(payload.get("verdict") or "").strip()
        if verdict not in ("passed", "held"):
            return None
        return payload
    except Exception:  # noqa: BLE001  判定失败不卡死流程，降级查表
        return None


def _generate_reaction(runner: Optional[StageRunner], pack: Dict[str, Any], level: int,
                       answer: str, verdict: str, problem_id: int) -> str:
    """判断级回答的 AI 反应；任何失败都静默降级为空串——反应只是旁白，
    绝不影响判定与级别推进（判定是状态机查表，模型无权决定）。"""
    if runner is None:
        return ""
    try:
        problem = runner.db.query_one("SELECT * FROM problems WHERE id=?", (int(problem_id),))
        if problem is None:
            return ""
        samples = _loads(problem["samples_json"], [])
        payload = runner.call(REACT, _react_user_content(
            pack, level, answer, verdict, problem, samples))
        text = str(payload.get("reaction") or "").strip()
        return text[:600]
    except Exception:  # noqa: BLE001  反应失败不打断放行流程
        return ""


def _save_reaction(db: Any, state: Any, text: str) -> None:
    """反应持久化到 hint_states；下一次 present 时会被清空。"""
    if not text:
        return
    try:
        with db.transaction() as conn:
            conn.execute("UPDATE hint_states SET reaction=?, updated_at=? WHERE id=?",
                         (str(text), _now(), int(state["id"])))
    except Exception:  # noqa: BLE001  老库缺列等情况：不显示反应，不影响主流程
        pass


def _judge_result(db: Any, state: Any, judged: Dict[str, Any], pid: int, account: str,
                  level: int, kind: str, attribution: str, answer: str, next_level: int,
                  held_message: str) -> Dict[str, Any]:
    """把 AI 判定结果落账：passed → 记日志+存反应+推进；held → 记日志+存反应+留级。"""
    reaction = str(judged.get("reaction") or "").strip()[:600]
    if str(judged.get("verdict")) == "passed":
        _log_hint(db, pid, account, level, "self", kind, "passed", attribution, answer)
        _save_reaction(db, state, reaction)
        with db.transaction() as conn:
            conn.execute(
                "UPDATE hint_states SET level=?, answered=1, updated_at=? WHERE id=?",
                (next_level, _now(), int(state["id"])),
            )
        return {"ok": True, "verdict": "passed", "level": next_level, "reaction": reaction}
    _log_hint(db, pid, account, level, "self", kind, "held", attribution, answer)
    _save_reaction(db, state, reaction)
    result: Dict[str, Any] = {"ok": True, "verdict": "held", "level": level,
                              "reaction": reaction, "message": held_message}
    try:
        wrong_blank = int(judged.get("wrong_blank") or 0)
    except (TypeError, ValueError):
        wrong_blank = 0
    if wrong_blank >= 1:
        result["wrong_blank"] = wrong_blank
    return result


def ladder_answer(db: Any, problem_id: int, user: str, payload: Dict[str, Any],
                  runner: Optional[StageRunner] = None) -> Dict[str, Any]:
    """学生作答 → 本级判定 → 放行或按类型处理。"""
    pid, account = int(problem_id), str(user)
    pack_row = db.query_one("SELECT * FROM hint_packs WHERE problem_id=?", (pid,))
    if pack_row is None:
        return {"error": "这道题还没有提示包", "code": "NOT_STARTED"}
    pack = _loads(pack_row["pack_json"], {})
    start_level = int(pack_row["start_level"] or 0)
    collapsed = bool(int(pack_row["collapsed"] or 0))
    state = _ensure_ladder_state(db, pid, account, start_level)
    if int(state["done"]):
        return {"error": "这道题的梯子已经走完", "code": "ALREADY_DONE"}
    if int(state["answered"]):
        return {"error": "还没有要你回答的问题，先点「要提示」", "code": "NOT_PRESENTED"}
    level = int(state["level"])
    answer = str(payload.get("answer") or "").strip()
    attribution = str(pack["attribution"])

    def _advance(next_level: int) -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE hint_states SET level=?, answered=1, updated_at=? WHERE id=?",
                (next_level, _now(), int(state["id"])),
            )

    # L0：判断型（AI 判定对错，降级才用长度门槛）与核对型（精确匹配 + AI 等价兜底）
    if level == 0:
        zero = pack["L0"]
        kind = str(zero["kind"])
        if kind == "check":
            expected = [str(x) for x in (zero.get("expected") or []) if str(x).strip()]
            norm_answer = _norm(answer)
            passed = bool(norm_answer) and any(_norm(item) in norm_answer for item in expected)
            if not passed:
                # 精确匹配没中 → AI 判等价表述（"四十" vs "40"）；AI 不可用维持原判
                judged = _judge_answer(runner, pack, 0, answer, [], pid)
                if judged is not None:
                    return _judge_result(
                        db, state, judged, pid, account, 0, kind, attribution, answer,
                        next_level=1,
                        held_message="答案就写在题面里，重读题面再答一遍。")
        else:
            judged = _judge_answer(runner, pack, 0, answer, [], pid)
            if judged is not None:
                return _judge_result(
                    db, state, judged, pid, account, 0, kind, attribution, answer,
                    next_level=1,
                    held_message="把你看到的判断说出来（哪怕只是猜），本级才放行。")
            passed = len(answer) >= 6      # 降级：预测/解释/草图/失败描述，答错也放行
        if passed:
            _log_hint(db, pid, account, 0, "self", kind, "passed", attribution, answer)
            reaction = "" if kind == "check" else _generate_reaction(
                runner, pack, 0, answer, "passed", pid)
            _save_reaction(db, state, reaction)
            _advance(1)
            return {"ok": True, "verdict": "passed", "level": 1, "reaction": reaction}
        _log_hint(db, pid, account, 0, "self", kind, "held", attribution, answer)
        return {
            "ok": True, "verdict": "held", "level": 0,
            "message": "答案就写在题面里，重读题面再答一遍。",
        }

    # L1 / L2：判断对错由 AI 判定（降级才退回"≥4 字就放行"的旧门槛）；
    # 判 held 时模型给引导问句，判 passed 时给一段针对学生原话的反应
    if level in (1, 2):
        judged = _judge_answer(runner, pack, level, answer, [], pid)
        if judged is not None:
            return _judge_result(
                db, state, judged, pid, account, level, "judgment", attribution, answer,
                next_level=4 if (collapsed and level == 1) else level + 1,
                held_message="这次判断没有通过，按助教反应里的引导再答一遍。")
        if len(answer) < 4:
            _log_hint(db, pid, account, level, "self", "judgment", "held", attribution, answer)
            reaction = _generate_reaction(runner, pack, level, answer, "held", pid)
            _save_reaction(db, state, reaction)
            return {"ok": True, "verdict": "held", "level": level, "reaction": reaction,
                    "message": "把你看到的判断说出来（哪怕只是猜），本级才放行。"}
        _log_hint(db, pid, account, level, "self", "judgment", "passed", attribution, answer)
        reaction = _generate_reaction(runner, pack, level, answer, "passed", pid)
        _save_reaction(db, state, reaction)
        next_level = 4 if (collapsed and level == 1) else level + 1
        _advance(next_level)
        return {"ok": True, "verdict": "passed", "level": next_level, "reaction": reaction}

    # L3：挖空填空。快路径 = 归一化精确比对 accept 清单（零 LLM）；
    # 没中时交给 AI 判语义等价（"停下来" vs "即停"），AI 不可用维持原判
    if level == 3:
        raw = payload.get("answers")
        answers = [str(item or "").strip() for item in raw] if isinstance(raw, list) else []
        blanks = pack["L3"]["blanks"]
        if len(answers) != len(blanks) or any(not item for item in answers):
            return {"error": "三个空都要填", "code": "INVALID_INPUT"}
        miss_index = 0
        for index, (blank, said) in enumerate(zip(blanks, answers), start=1):
            accepts = {_norm(item) for item in ([blank["answer"]] + list(blank.get("accept") or []))}
            if _norm(said) not in accepts:
                miss_index = index
                break
        if miss_index:
            judged = _judge_answer(runner, pack, 3, "", answers, pid)
            if judged is not None:
                return _judge_result(
                    db, state, judged, pid, account, 3, "blanks", attribution, "",
                    next_level=4,
                    held_message=f"第 {miss_index} 个空不对，再想想。")
            _log_hint(db, pid, account, 3, "self", "blanks", "held", attribution,
                      f"空{miss_index}={answers[miss_index - 1]}")
            return {"ok": True, "verdict": "held", "level": 3, "wrong_blank": miss_index,
                    "message": f"第 {miss_index} 个空不对，再想想。"}
        _log_hint(db, pid, account, 3, "self", "blanks", "passed", attribution)
        _advance(4)
        return {"ok": True, "verdict": "passed", "level": 4}

    return {"error": "状态越界", "code": "INTERNAL"}


def ladder_give_l4(db: Any, runner: StageRunner, problem_id: int, user: str) -> Dict[str, Any]:
    """L4 指你的错：必须有提交记录，一次运行期调用只指一处。"""
    pid, account = int(problem_id), str(user)
    pack_row = db.query_one("SELECT * FROM hint_packs WHERE problem_id=?", (pid,))
    if pack_row is None:
        return {"error": "这道题还没有提示包", "code": "NOT_STARTED"}
    pack = _loads(pack_row["pack_json"], {})
    state = _ensure_ladder_state(
        db, pid, account, int(pack_row["start_level"] or 0)
    )
    if int(state["done"]):
        return {"error": "这道题的梯子已经走完", "code": "ALREADY_DONE"}
    if int(state["level"]) < LADDER_LEVELS - 1:
        return {"error": "还没到 L4：先把前面级别的判断做完", "code": "LEVEL_LOCKED"}
    if not _has_submission(db, pid):
        return {
            "error": "L4 需要先提交过代码：没有提交就停在 L3，避免先问再写",
            "code": "NO_SUBMISSION",
        }
    submission = db.query_one(
        "SELECT id, lang, code FROM submissions WHERE problem_id=? ORDER BY id DESC LIMIT 1",
        (pid,),
    )
    code = str(submission["code"] or "")
    numbered = "\n".join(
        f"{no}  {line}" for no, line in enumerate(code.splitlines()[:500], start=1)
    )
    # 失败上下文：这道题最近一条对抗数据的失败原因（有就带上，没有就直说）
    failure_row = db.query_one(
        "SELECT failure_reason, run_verdict FROM adversarial_cases"
        " WHERE problem_id=? AND status='active' ORDER BY id DESC LIMIT 1", (pid,),
    )
    failure = (
        f"verdict={failure_row['run_verdict']}；{failure_row['failure_reason']}"
        if failure_row is not None else "系统未附具体差异（该提交没有对抗数据）"
    )
    samples = db.query_one("SELECT samples_json FROM problems WHERE id=?", (pid,))
    sample_text = str(samples["samples_json"] or "[]") if samples is not None else "[]"
    # 弯路回顾素材：学生在这道题上被拦下的记录（判断没过 / 填空没中 / 三轮未过）
    misses = [
        f"- L{row['level']}（{row['kind'] or '判断'}）：{str(row['detail'] or '').strip()}"
        for row in db.query_all(
            "SELECT level, kind, detail FROM hint_log"
            " WHERE problem_id=? AND user=? AND outcome IN ('held','reteach')"
            " AND level>=0 ORDER BY id DESC LIMIT 10", (pid, account))
        if str(row["detail"] or "").strip()
    ]
    miss_section = (
        "<学生梯子记录>\n（此前被拦下的回答，据此写回顾；为空则 review 给空串）\n"
        + "\n".join(reversed(misses)) + "\n</学生梯子记录>\n" if misses else ""
    )
    user_content = (
        f"<题目>\n题面：\n{problem_statement(db, pid)}\n样例：\n{sample_text[:4000]}\n</题目>\n"
        f"<学生代码>\n{numbered}\n</学生代码>\n"
        f"<失败上下文>\n{failure[:1200]}\n</失败上下文>\n"
        f"{miss_section}"
    )
    result = runner.call(LADDER_L4, user_content)
    line = int(result.get("line") or 0)
    message = str(result.get("message") or "").strip()
    # 弯路回顾：学生此前被拦下的回答的复盘（可选字段，无记录时为空）
    review = str(result.get("review") or "").strip()[:600]
    if line < 1 or not message:
        raise TutorError("L4 输出不合格：需要 line 与 message")
    total_lines = max(1, len(code.splitlines()[:500]))
    if line > total_lines:
        line = total_lines
    with db.transaction() as conn:
        conn.execute(
            "UPDATE hint_states SET level=4, answered=1, done=1, reaction=?, updated_at=? WHERE id=?",
            (review, _now(), int(state["id"])),
        )
    _log_hint(db, pid, account, 4, "self", "code", "l4", str(pack["attribution"]),
              f"L{line}: {message}")
    result: Dict[str, Any] = {"ok": True, "verdict": "l4", "level": 4,
                              "line": line, "message": message}
    if review:
        result["review"] = review
    return result


def problem_statement(db: Any, problem_id: int) -> str:
    row = db.query_one("SELECT statement_md FROM problems WHERE id=?", (int(problem_id),))
    return str(row["statement_md"] or "") if row is not None else ""


def reset_tutor(db: Any, problem_id: int, user: str, full: bool = False) -> Dict[str, Any]:
    """重置一道题的教学状态（管理员操作，用于演示与复测）。

    默认只清"进度与留痕"：probe_progress / probe_records / hint_states / hint_log，
    保留 probe_packs / hint_packs（不用再花一次模型调用）。
    full=True 连包一起删，下次「开始探针」「生成提示包」会重新生成。
    档位 probe_tiers 是跨题档案，一律不动。
    """
    pid = int(problem_id)
    row = db.query_one("SELECT id FROM problems WHERE id=?", (pid,))
    if row is None:
        raise TutorError("题目不存在", "NOT_FOUND")
    with db.transaction() as conn:
        conn.execute("DELETE FROM probe_progress WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM probe_records WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM hint_states WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM hint_log WHERE problem_id=?", (pid,))
        if full:
            conn.execute("DELETE FROM probe_packs WHERE problem_id=?", (pid,))
            conn.execute("DELETE FROM hint_packs WHERE problem_id=?", (pid,))
    _ = user
    return {"ok": True, "reset": "full" if full else "progress"}
