"""针对学生错解生成**对抗性测试数据**（`adversarial` 层）。

设计要点（对应第二轮试用反馈「adversarial 层未实现」）：

- 纯随机 fuzz 命中率极低（实测 60 条候选仅 5 条能卡住 bug，8%，比题库已有数据还差），
  所以由 LLM 读「参考解 vs 错解」**定向构造**候选，模型不可用时才降级本地变异。
- 规模门槛型 bug（如「q>50 且 x>50 才出错」）：模型说得出触发条件却没预算逐行写出
  53 行输入，改由本地「规模阶梯」把已有大数据用例截断成不同 q 的合法长序列，零 LLM 成本。
- 候选池 = LLM 定向 + 本地变异 + 规模阶梯；三者按输入文本去重后一起过下面的三重门。
- 模型只负责「提候选」，**不负责定答案**。每条候选必须过本地三重门：
  ① grammar + 约束合法性；② 两份参考解交叉验证期望输出；③ 错解在该输入上确实 WA。
  只有三者同时成立才入库 —— 期望输出永远是程序算出来的，不是模型编的。
- 全程 0~1 次 LLM 调用；执行复用 `judge.run_solution`（整批只编译一次）。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..engine.verify.comparator import compare
from ..prompts import loader as prompts
from . import judge
from .prep_store import PrepStore
from ..store.db import Database, get_db

LAYER = "adversarial"
MAX_LLM_CASES = 12
MAX_FUZZ_CASES = 120
SUPPORTED_LANGS = ("c", "cpp", "python")
#: 规模阶梯：从已有的大数据用例截断出不同 q 的合法长序列。
#: 模型擅长给"触发条件"但不擅长（也没预算）逐行写出长输入，这一步由本地确定性完成。
SCALE_STEPS = (20, 51, 60, 80, 100, 200, 500, 1000, 5000)
#: 模型给出的 trigger 用结构化写法（提示词 v4 强制），本地才解析得了：
#: `q>=51, x>50` / `y>1000000` / `seq=2,4,3`。自由中文句子无法稳定消费。
_TRIGGER_KV_RE = re.compile(r"([A-Za-z_]\w*)\s*(>=|≥|>|<=|≤|<|=|＞|＜)\s*(\d+)")
_TRIGGER_SEQ_RE = re.compile(r"seq\s*=\s*([\d,，、→\s]+)", re.IGNORECASE)
_ORIGINS = ("llm", "trigger", "mutation", "scale")
_LABELS = {"llm": "定向构造", "trigger": "依触发条件构造", "mutation": "本地变异", "scale": "规模阶梯"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clip_material(text: Any, limit: int = 6000) -> str:
    """材料超长时头尾各留一段并**明确标注省略**。

    旧实现 `buggy_code[:6000]` 静默硬切：模型以为看到的是完整代码，若 bug 在末尾
    分支就完全看不到，且不会在 reason 里说明。头 70% + 尾 30% 兼顾两头。
    """
    raw = str(text or "")
    if len(raw) <= limit:
        return raw
    head_len = int(limit * 0.7)
    tail_len = limit - head_len
    return (
        raw[:head_len]
        + f"\n…（以下省略中间 {len(raw) - head_len - tail_len} 字符；材料未完整展示）…\n"
        + raw[-tail_len:]
    )


def _adversarial_schema(obj: Dict[str, Any]) -> Optional[str]:
    """client.expect 校验器：解析成功 ≠ 结构可用。

    旧逻辑只判"能解析成 dict"，模型把 `cases` 写成 `test_cases` / 字符串 / 顶层数组
    时会静默拿到 0 条候选（还记成"调用成功"），整次 LLM 白调。
    """
    if not isinstance(obj, dict):
        return "顶层必须是 JSON 对象"
    cases = obj.get("cases")
    if not isinstance(cases, list):
        return "缺少 cases 字段，或 cases 不是数组（必须输出 {\"cases\":[...]}）"
    if not cases:
        return "cases 是空数组，至少给 1 条"
    return None


def _load_context(db: Database, pid: int) -> Tuple[Dict[str, Any], Dict[str, Any], str, Optional[str]]:
    """返回 (problem, spec, 参考解A, 参考解B)。B 可能为空（单解题）。"""
    row = db.query_one("SELECT * FROM problems WHERE id=?", (pid,))
    if row is None:
        raise ValueError(f"题目 #{pid} 不存在")
    problem = dict(row)
    spec = {}
    raw = problem.get("spec_json")
    if raw:
        try:
            spec = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            spec = {}
    if not spec.get("input_grammar"):
        raise ValueError("该题还没有 spec（先完成生成再来做对抗数据）")
    refs = db.query_all(
        "SELECT lang, role, code FROM reference_solutions WHERE problem_id=? "
        "AND lang='python' AND verified=1 AND role IN ('reference','brute','alt1') ORDER BY id",
        (pid,),
    )
    if not refs:
        raise ValueError("该题没有已验证的 Python 参考解，无法判定期望输出")
    primary = next((r["code"] for r in refs if r["role"] == "reference"), refs[0]["code"])
    second = next((r["code"] for r in refs if r["role"] != "reference"), None)
    return problem, spec, primary, second


def _llm_candidates(
    db: Database,
    pid: int,
    problem: Dict[str, Any],
    spec: Dict[str, Any],
    ref_a: str,
    buggy_code: str,
    count: int,
) -> Tuple[List[Dict[str, str]], Dict[str, Any], List[str], List[Dict[str, Any]]]:
    """一次 LLM 调用，让它读参考解与错解后定向构造候选输入。失败一律降级，不抛。

    返回 (cases, stats, notes, triggers)。`triggers` 是模型给出的**结构化触发条件**，
    本地会据此定向构造用例（v3 只把它拼进 reason 展示，等于把模型能力浪费了）。
    """
    from ..engine.datagen import grammar
    from ..llm.client import LlmClient, LlmConfigError, LlmError, extract_json_object

    notes: List[str] = []
    system = prompts.load("adversarial")
    material = {
        "题面": _clip_material(problem.get("statement_md"), 5000),
        "输入格式 grammar": grammar.describe(spec.get("input_grammar")),
        "约束": spec.get("constraints_exec") or [],
        "输出格式": str(spec.get("output_format") or ""),
        "checker": spec.get("checker_type") or "exact",
        "正确参考解代码": _clip_material(ref_a, 6000),
        "学生的错误代码": _clip_material(buggy_code, 6000),
        "期望条数 N": min(count * 2, MAX_LLM_CASES),
    }
    empty_stats = {"used": False, "calls": 0, "tokens": 0, "model": "",
                   "error": "", "advice": "", "triggers": 0}
    try:
        client = LlmClient(verbose=False)
        client.recorder = PrepStore(db).record_llm_call
        result = client.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(material, ensure_ascii=False)},
            ],
            json_mode=True,
            temperature=0.4,
            max_tokens=2560,
            # 下面 4 项是把"提示词要求少写"落实到调用层，避免客户端反向怂恿模型写长。
            respect_max_tokens=True,   # 不再被 MIN_MAX_TOKENS=4096 强抬
            on_truncate="shrink",      # 截断时减条数重出，而不是翻倍预算（v2 翻车点）
            wall_clock_s=60,           # 单次调用真墙钟上限
            expect=_adversarial_schema,  # cases 字段/类型校验，字段写错要带原因重试
            tag="adv_cases",
            stage="adversarial",
            problem_id=pid,
            thinking="disabled",
            prompt_hash=hashlib.sha256(system.encode("utf-8")).hexdigest()[:16],
        )
        obj = result.json_obj or extract_json_object(result.content) or {}
        items = obj.get("cases") or []
        cases: List[Dict[str, str]] = []
        triggers: List[Dict[str, Any]] = []
        for x in items:
            if not isinstance(x, dict) or not str(x.get("input") or "").strip():
                continue
            reason = str(x.get("reason") or "").strip()
            trigger = str(x.get("trigger") or "").strip()
            parsed = _parse_trigger(trigger)
            if parsed:
                triggers.append(parsed)
            if trigger:
                reason = f"[触发条件：{trigger}] {reason}".strip()
            cases.append({"input": str(x["input"]), "reason": reason,
                          "trigger": trigger, "_origin": "llm"})
        cases = cases[:MAX_LLM_CASES]
        stats = {
            "used": True,
            "calls": result.attempts,
            "tokens": result.usage.total_tokens,
            "model": result.model,
            "error": "",
            "advice": "",
            "triggers": len(triggers),
        }
        if not cases:
            notes.append("模型没有给出可用候选，改用本地变异兜底")
        return cases, stats, notes, triggers
    except Exception as exc:  # noqa: BLE001 - 模型不可用不能让学生卡住
        # 旧实现 except Exception 一锅端，上层分不清 401 / 超时 / 截断 / 结构错。
        kind = ("http_config" if isinstance(exc, LlmConfigError) else
                "llm_failed" if isinstance(exc, LlmError) else type(exc).__name__)
        text = str(exc)
        if "墙钟" in text or "预算已耗尽" in text:
            advice = "单次调用超过墙钟上限已放弃；可重试，或降低条数 N"
        elif "截断" in text:
            advice = "输出被截断：模型仍在写长输入，检查提示词条数/行数上限"
        elif "结构" in text or "schema" in text.lower():
            advice = "输出结构不符且重试未修复：核对提示词里的 cases 字段名"
        elif isinstance(exc, LlmConfigError):
            advice = "检查 API key / base_url / 模型名是否与官网一致"
        else:
            advice = "模型不可用，已降级本地变异；可重试"
        empty_stats["error"] = f"{kind}: {text[:180]}"
        empty_stats["advice"] = advice
        notes.append(f"模型调用失败（{kind}: {text[:120]}）→ {advice}；改用本地变异兜底")
        return [], empty_stats, notes, []


def _switch_cases(spec: Dict[str, Any]) -> Dict[str, int]:
    """收集 grammar 里所有 switch 的合法标记 → 该分支的字段个数。

    变异时用它约束操作码，避免造出 `t=5` 这类 grammar 里根本不存在的分支
    （旧实现靠 ±1 改 tag，实测近半数候选因此被格式门白白丢弃）。
    """
    found: Dict[str, int] = {}

    def walk(nodes: Any) -> None:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            if str(node.get("kind")) == "switch":
                for tag, case in (node.get("cases") or {}).items():
                    if isinstance(case, dict):
                        found[str(tag)] = len(case.get("fields") or [])
            for key in ("elem", "fields"):
                value = node.get(key)
                if isinstance(value, dict):
                    walk([value])
                elif isinstance(value, list):
                    walk(value)

    walk(spec.get("input_grammar"))
    return found


def _count_token_index(nodes: Any) -> Optional[int]:
    """首行里"操作条数"这个标量在第几个 token（顶层 repeat 的 count 引用）。"""
    scalars: List[str] = []
    for node in nodes or []:
        if not isinstance(node, dict):
            continue
        if str(node.get("kind")) == "repeat":
            count = node.get("count")
            return scalars.index(count) if isinstance(count, str) and count in scalars else None
        if str(node.get("kind")) == "scalar":
            scalars.append(str(node.get("name") or ""))
    return None


def _parse_trigger(text: str) -> Optional[Dict[str, Any]]:
    """把模型给的结构化 trigger 解析成构造参数。

    支持 `q>=51, x>50` / `y>1000000` / `seq=2,4,3`（提示词 v4 强制这种写法）。
    只认键值对、不猜自由中文；解析不出来返回 None，本地仍走规模阶梯兜底。
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    q_min: Optional[int] = None
    var_mins: Dict[str, int] = {}
    for name, op, value in _TRIGGER_KV_RE.findall(raw):
        num = int(value)
        key = name.lower()
        if key == "seq":
            continue  # `seq=2,4,3` 不能被当成变量门槛，否则会污染关键量取值
        if key == "q" and op in (">=", "≥", ">", "＞", "="):
            q_min = max(q_min or 0, num + (0 if op in (">=", "≥", "=") else 1))
        elif op in (">=", "≥", ">", "＞", "="):
            var_mins[key] = max(var_mins.get(key, 0), num + 1)
    seq: List[str] = []
    match = _TRIGGER_SEQ_RE.search(raw)
    if match:
        seq = re.findall(r"\d+", match.group(1))
    if q_min is None and not var_mins and not seq:
        return None
    return {"q": q_min, "vars": var_mins, "seq": seq, "raw": raw[:120]}


def _bump_line(line: str, value: int, position: int) -> str:
    """把操作行第 position 个数值 token 抬到 value（首列操作码绝不动）。"""
    parts = line.split()
    if 0 < position < len(parts) and parts[position].lstrip("-").isdigit():
        parts[position] = str(value)
        return " ".join(parts)
    return line


def _apply_seq(ops: List[str], seq: Sequence[str], cases: Dict[str, int]) -> List[str]:
    """把操作序列模式铺到前几行；换操作码时同步字段数，避免格式门拒收。"""
    out = list(ops)
    for i, tag in enumerate(seq[: len(out)]):
        if tag not in cases:
            continue
        need = cases[tag] + 1  # 首列 tag + 字段数
        parts = out[i].split()
        kept = parts[1:need]
        while len(kept) < need - 1:
            kept.append("1")
        out[i] = " ".join([tag] + kept[: need - 1])
    return out


def _trigger_candidates(
    db: Database, pid: int, spec: Dict[str, Any], triggers: Sequence[Dict[str, Any]]
) -> List[Dict[str, str]]:
    """按模型给的 trigger 定向构造**刚好过门槛的最短**合法用例。

    v3 只把 trigger 拼进 reason 展示，本地却盲跑 SCALE_STEPS(20..5000)，
    结果长序列门槛型 bug 只能靠 q=5000 命中 —— 5001 行学生根本读不懂。
    这里用门槛值决定 q 与关键变量，生成 q=51 / 关键量=51 这种可复现的最小用例。
    """
    if not triggers:
        return []
    index = _count_token_index(spec.get("input_grammar"))
    if index is None:
        return []
    rows = db.query_all(
        "SELECT input_data FROM test_cases WHERE problem_id=? "
        "AND layer IN ('stress','boundary','sample','exhaustive') "
        "ORDER BY LENGTH(input_data) DESC LIMIT 3",
        (pid,),
    )
    if not rows:
        return []
    cases = _switch_cases(spec)
    out: List[Dict[str, str]] = []
    for tr in triggers:
        q_min = tr.get("q")
        var_mins: Dict[str, int] = tr.get("vars") or {}
        seq: List[str] = tr.get("seq") or []
        target_val = max(var_mins.values()) if var_mins else None
        for row in rows[:2]:
            lines = [line for line in str(row["input_data"]).rstrip("\n").split("\n") if line.strip()]
            if len(lines) < 3:
                continue
            head = lines[0].split()
            ops = lines[1:]
            if index >= len(head) or not head[index].lstrip("-").isdigit():
                continue
            targets: List[int] = []
            if q_min:
                targets.extend([q_min, q_min + 1])
            targets.extend(q for q in (6, 12, 20) if q < len(ops))
            for q in sorted({q for q in targets if 2 <= q <= max(8, len(ops))}):
                pool = list(ops[:q])
                while len(pool) < q:  # seed 不够长时重复补足，保证首行 q == 行数
                    pool.append(ops[len(pool) % len(ops)])
                if seq:
                    pool = _apply_seq(pool, seq, cases)
                # 变量门槛可能落在第 2 或第 3 个 token，各出一个变体（都不动首列 tag）
                positions: Tuple[int, ...] = (1, 2) if target_val is not None else (0,)
                for pos in positions:
                    body = ([_bump_line(line, int(target_val), pos) for line in pool]
                            if target_val is not None else pool)
                    new_head = list(head)
                    new_head[index] = str(q)
                    text = "\n".join([" ".join(new_head)] + body) + "\n"
                    bits: List[str] = []
                    if q_min:
                        bits.append(f"q={q}（门槛 {q_min}）")
                    if target_val is not None:
                        bits.append(f"关键量={target_val}（第 {pos} 个数值位）")
                    if seq:
                        bits.append("序列=" + ",".join(seq))
                    out.append({
                        "input": text,
                        "_origin": "trigger",
                        "reason": f"依模型 trigger「{tr.get('raw', '')}」定向构造：" + "、".join(bits),
                    })
    return out


def _scale_candidates(db: Database, pid: int, spec: Dict[str, Any]) -> List[Dict[str, str]]:
    """规模阶梯：把已有的大数据用例截断成不同 q 的**合法**长序列。

    为什么需要：模型能准确说出"q>50 且 x>50 才触发"，但受输出预算限制写不出
    53 行完整输入（实测 12 条候选最长 5 行，q 写 51 只给 3 行 → 全被格式门拒）。
    这里由本地确定性展开，零 LLM 成本，且 n / x / y 都沿用原数据，天然合法。
    """
    index = _count_token_index(spec.get("input_grammar"))
    if index is None:
        return []
    rows = db.query_all(
        "SELECT input_data FROM test_cases WHERE problem_id=? "
        "AND layer IN ('stress','boundary','sample','exhaustive') "
        "ORDER BY LENGTH(input_data) DESC LIMIT 3",
        (pid,),
    )
    out: List[Dict[str, str]] = []
    for row in rows:
        lines = [line for line in str(row["input_data"]).rstrip("\n").split("\n") if line.strip()]
        if len(lines) < 3:
            continue
        head = lines[0].split()
        if index >= len(head) or not head[index].lstrip("-").isdigit():
            continue
        ops = lines[1:]
        for q in SCALE_STEPS:
            if q >= len(ops) or q < 2:
                continue
            new_head = list(head)
            new_head[index] = str(q)
            text = "\n".join([" ".join(new_head)] + ops[:q]) + "\n"
            out.append(
                {
                    "input": text,
                    "_origin": "scale",
                    "reason": f"规模阶梯 q={q}：由大数据用例截断构造，检验只在长序列/多操作下才显现的 bug",
                }
            )

    # 密集重复长序列：把"字段最短"的几种操作各重复到 q 条。
    # 复杂度/累积型 bug（例如每次查询都重放全部历史）靠截断保留的稀疏分布放大不出来，
    # 只有同类型操作堆到上万次才会超时/状态崩坏。字段最短的分支通常正是查询类。
    cases = _switch_cases(spec)
    if cases and rows:
        shortest = min(cases.values())
        tags = [t for t, n in cases.items() if n == shortest][:3]
        row = max(rows, key=lambda r: len(str(r["input_data"])))
        lines = [line for line in str(row["input_data"]).rstrip("\n").split("\n") if line.strip()]
        head = lines[0].split() if lines else []
        ops = lines[1:]
        if index < len(head) and head[index].lstrip("-").isdigit() and len(ops) >= 1000:
            q = len(ops)
            new_head = list(head)
            new_head[index] = str(q)
            for tag in tags:
                line = next((x for x in ops if x.split() and x.split()[0] == tag), None)
                if not line:
                    continue
                text = "\n".join([" ".join(new_head)] + [line] * q) + "\n"
                out.append(
                    {
                        "input": text,
                        "_origin": "scale",
                        "reason": f"密集重复 q={q}（全为 t={tag} 操作）：检验只在复杂度/累积规模下才暴露的 bug",
                    }
                )
    return out


def _mutation_candidates(
    db: Database, pid: int, rng: random.Random, spec: Dict[str, Any]
) -> List[Dict[str, str]]:
    """从已有用例做结构变异（保持行数、只改字段值/操作码/行序），合法性交给验证门筛。"""
    cases = _switch_cases(spec)
    seeds = db.query_all(
        "SELECT input_data FROM test_cases WHERE problem_id=? AND layer IN ('sample','boundary') "
        "ORDER BY id LIMIT 6",
        (pid,),
    )
    out: List[Dict[str, str]] = []
    for seed in seeds:
        lines = [line for line in str(seed["input_data"]).rstrip("\n").split("\n") if line.strip()]
        if len(lines) < 2:
            continue
        for _ in range(20):
            work = list(lines)
            mode = rng.choice(("num", "num", "opcode", "swap"))
            index = rng.randrange(1, len(work))
            parts = work[index].split()
            if mode == "num" and len(parts) >= 2:
                # 只改字段值，不动首列操作码（旧实现会把它改成 5 之类非法值）
                pos = rng.randrange(1, len(parts))
                token = parts[pos]
                if token.lstrip("-").isdigit():
                    parts[pos] = str(max(1, int(token) + rng.choice((-2, -1, 1, 2, 3, 7))))
                    work[index] = " ".join(parts)
            elif mode == "opcode" and parts and cases:
                # 换操作码必须同步换成该分支的字段数，否则整行 token 数不匹配
                tag = rng.choice(list(cases))
                need = cases[tag] + 1
                kept = parts[1:need]
                while len(kept) < need - 1:
                    kept.append(str(rng.randint(1, 10)))
                work[index] = " ".join([tag] + kept[: need - 1])
            elif mode == "swap" and len(work) > 2:
                a = rng.randrange(1, len(work))
                b = rng.randrange(1, len(work))
                work[a], work[b] = work[b], work[a]
            text = "\n".join(work) + "\n"
            out.append(
                {
                    "input": text,
                    "_origin": "mutation",
                    "reason": "由已有用例变异而来，经本地差分筛选命中",
                }
            )
    return out[:MAX_FUZZ_CASES]


def _format_ok(text: str, spec: Dict[str, Any]) -> Tuple[bool, str]:
    """① 格式门：grammar 解析 + 校验 + 约束（best-effort，未判定的不拦）。"""
    from ..engine.datagen import grammar

    nodes = spec.get("input_grammar") or []
    try:
        parsed = grammar.parse(nodes, text)
    except Exception as exc:  # noqa: BLE001 - GrammarError 及其子类都算不合法
        return False, f"输入格式不符合 grammar：{str(exc)[:100]}"
    violations = grammar.validate(nodes, parsed)
    if violations:
        return False, "输入未通过格式校验：" + "; ".join(violations[:2])
    hard = [
        c for c in grammar.evaluate_constraints(spec.get("constraints_exec") or [], parsed)
        if c.evaluated and not c.ok
    ]
    if hard:
        return False, "输入超出题面约束：" + hard[0].expr
    return True, ""


def _batch_run(
    lang: str,
    code: str,
    inputs: Sequence[str],
    checker: str,
    eps: float,
    expected: Optional[Dict[str, str]] = None,
    time_limit_ms: int = 3000,
) -> Dict[int, Any]:
    """整批执行一次（只编译一次）；expected 非空时按 checker 比较并给 wa/ac 判定。"""
    cases = [
        {"id": str(i), "input": text, "expected": (expected or {}).get(text)}
        for i, text in enumerate(inputs)
    ]
    batch = judge.run_solution(
        lang,
        code,
        cases,
        checker_type=checker,
        float_eps=eps,
        time_limit_ms=time_limit_ms,
        compare_expected=expected is not None,
    )
    if batch.compile_error:
        return {}
    return {int(run.case_id): run for run in batch.runs}


def _screen(
    candidates: Sequence[Dict[str, str]],
    spec: Dict[str, Any],
    ref_a: str,
    ref_b: Optional[str],
    lang: str,
    buggy_code: str,
    count: int,
) -> Tuple[List[Dict[str, str]], List[str], Dict[str, int]]:
    """②③ 交叉验证 + 错解必须 WA；返回 (合格用例, 说明, 统计)。"""
    from ..engine.runner import VERDICT_AC

    checker = str(spec.get("checker_type") or "exact")
    eps = float(spec.get("float_eps") or 1e-6)
    seen: set[str] = set()
    unique: List[Dict[str, str]] = []
    notes: List[str] = []
    for item in candidates:
        text = item["input"]
        if not text.strip() or text in seen:
            continue
        seen.add(text)
        ok, why = _format_ok(text, spec)
        if not ok:
            notes.append(why)
            continue
        unique.append(item)
    if not unique:
        return [], notes, {"format_ok": 0, "dual_ok": 0, "kills": 0}

    inputs = [x["input"] for x in unique]
    # 参考解只负责算正确答案，给宽时限；错解保持 3s（超时本身就是一种"被卡住"）
    run_a = _batch_run("python", ref_a, inputs, checker, eps, time_limit_ms=10000)
    out_a = {i: r.stdout for i, r in run_a.items() if r.verdict == VERDICT_AC}
    dual_ok = 0
    if ref_b:
        run_b = _batch_run("python", ref_b, inputs, checker, eps, time_limit_ms=10000)
        for i in list(out_a):
            b = run_b.get(i)
            if b is None or b.verdict != VERDICT_AC or not compare(checker, out_a[i], b.stdout, float_eps=eps).ok:
                out_a.pop(i, None)
            else:
                dual_ok += 1
    else:
        dual_ok = len(out_a)
        notes.append("该题只有一份参考解，期望输出标为 reference_only（未交叉验证）")

    expected_map = {unique[i]["input"]: out for i, out in out_a.items()}
    bug_inputs = list(expected_map)
    # 注意：错解批次是**重新编号**的 case_id（0..M-1），不能拿 unique 的下标去取
    run_bug = _batch_run(lang, buggy_code, bug_inputs, checker, eps, expected_map)
    bug_by_input = {bug_inputs[i]: run for i, run in run_bug.items()}
    valid: List[Dict[str, str]] = []
    for item in unique:
        text = item["input"]
        if text not in expected_map:
            continue
        run = bug_by_input.get(text)
        # 错解干净跑完却与正确答案一致 → 没有对抗价值；非 ac（含 wa/re/tle）都算能卡住
        if run is None or run.verdict == VERDICT_AC:
            continue
        valid.append(
            {
                "input": text,
                "expected": expected_map[text],
                "buggy_actual": run.stdout,
                "reason": item["reason"] or "错解在该输入上与参考解不一致",
                "origin": item.get("_origin") or "llm",
            }
        )
    total_kills = len(valid)
    # 同等有效时优先保留短用例：学生能一眼看懂，导出包也不会被 5001 行数据撑大
    valid.sort(key=lambda v: len(v["input"]))
    valid = valid[:count]
    return valid, notes, {
        "format_ok": len(unique),
        "dual_ok": dual_ok,
        "kills": len(valid),
        "kills_total": total_kills,
    }


def _store(db: Database, pid: int, cases: Sequence[Dict[str, str]], dual: bool) -> int:
    """入库 adversarial 层；同层按 input_hash 去重（重跑不会堆重复数据）。"""
    if not cases:
        return 0
    added = 0
    trust = "cross_verified" if dual else "reference_only"
    truth = "dual_impl" if dual else "reference"
    now = _now()
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(ordinal),0) FROM test_cases WHERE problem_id=? AND layer=?",
            (pid, LAYER),
        ).fetchone()
        base = int(row[0] or 0)
        for offset, case in enumerate(cases, start=1):
            digest = hashlib.sha256(case["input"].encode("utf-8")).hexdigest()[:32]
            cursor = conn.execute(
                "INSERT INTO test_cases(problem_id, layer, ordinal, input_data, input_hash,"
                " expected_output, source_of_truth, trust_level, size_class, verified_at, source)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(problem_id, layer, input_hash) DO NOTHING",
                (
                    pid,
                    LAYER,
                    base + offset,
                    case["input"],
                    digest,
                    case["expected"],
                    truth,
                    trust,
                    LAYER,
                    now,
                    "generated",
                ),
            )
            added += max(0, int(cursor.rowcount or 0))
    return added


def generate_adversarial(payload: Dict[str, Any], db: Optional[Database] = None) -> Dict[str, Any]:
    """对一份学生错解生成对抗性测试数据并入库。

    payload: problem_id / lang / code / count / use_llm / use_trigger(可选，默认都开)
    """
    database = db or get_db()
    pid = int(payload.get("problem_id") or 0)
    lang = str(payload.get("lang") or "python")
    code = str(payload.get("code") or "")
    count = max(1, min(int(payload.get("count") or 8), 30))
    use_llm = payload.get("use_llm", True) is not False
    use_trigger = payload.get("use_trigger", True) is not False
    if lang not in SUPPORTED_LANGS:
        return {"error": f"不支持的语言：{lang}"}
    if not code.strip() or len(code) > 30000:
        return {"error": "代码为空或超过 30000 字符"}
    try:
        problem, spec, ref_a, ref_b = _load_context(database, pid)
    except ValueError as exc:
        return {"error": str(exc)}

    notes: List[str] = []
    llm_stats: Dict[str, Any] = {
        "used": False, "calls": 0, "tokens": 0, "model": "",
        "error": "", "advice": "", "triggers": 0,
    }
    llm_cases: List[Dict[str, str]] = []
    triggers: List[Dict[str, Any]] = []
    if use_llm:
        llm_cases, llm_stats, llm_notes, triggers = _llm_candidates(
            database, pid, problem, spec, ref_a, code, count
        )
        notes.extend(llm_notes)
        if llm_stats.get("error"):
            notes.append(f"失败原因：{llm_stats['error']}")
    else:
        notes.append("已指定 --no-llm，只用本地变异")
    if not use_trigger:
        notes.append("已指定 use_trigger=False，忽略模型给出的 trigger（回退旧行为）")
        triggers = []
    rng = random.Random(20261118)
    fuzz_cases = _mutation_candidates(database, pid, rng, spec)
    scale_cases = _scale_candidates(database, pid, spec)
    trigger_cases = _trigger_candidates(database, pid, spec, triggers)
    # trigger 定向候选排在最前：同等有效时 _screen 会按输入长度升序取前 N 条，
    # 短用例（q=51 而非 q=5000）因此优先入库，学生能读懂。
    candidates = llm_cases + trigger_cases + fuzz_cases + scale_cases
    valid, screen_notes, stats = _screen(candidates, spec, ref_a, ref_b, lang, code, count)
    notes.extend(screen_notes[:3])

    added = _store(database, pid, valid, dual=ref_b is not None)
    by_origin = {
        key: sum(1 for v in valid if v.get("origin") == key)
        for key in _ORIGINS
    }
    mode = "+".join(_LABELS[k] for k in _ORIGINS if by_origin[k]) or "无命中"
    if not valid:
        notes.append("未找到能卡住该错解的输入：可能是代码已正确，或 bug 只在特殊格式下触发")
    return {
        "ok": True,
        "problem_id": pid,
        "added": added,
        "generated": len(valid),
        "layer": LAYER,
        "mode": mode,
        "by_origin": by_origin,
        "llm": llm_stats,
        "screen": stats,
        "candidates": len(candidates),
        "pool": {
            "llm": len(llm_cases),
            "trigger": len(trigger_cases),
            "mutation": len(fuzz_cases),
            "scale": len(scale_cases),
        },
        "trigger_used": len(triggers),
        "notes": notes,
        "cases": valid,
    }
