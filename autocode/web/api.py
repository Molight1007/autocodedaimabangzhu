"""autoCode 业务层（契约 §1）：**所有业务函数只在这里**。

分层铁律（docs/v2-contract.md §1）：
- `web/server.py` 只做 HTTP 路由/状态码/二进制下载；
- `desktop/app.py` 只做 JS bridge 映射；
- 两者都 `import autocode.web.api`，因此网页版与桌面版行为天然一致。

代码 vs LLM（契约 §2）：
- 本文件里凡是"计数/评分/去重/版本号/任务状态"的逻辑全部是确定性代码，LLM 不参与；
- LLM 只出现在 `diagnose_student_code`（错因诊断）与后台生成子进程里，且产物都要过代码门禁。

错误口径：所有函数返回 `{"error": "..."}` 表示失败，不抛穿透（除 `problem_detail` 保留
`KeyError`，因为 HTTP 层要把它映射成 404）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..app import jobs
from ..app.quality import score_problem
from ..llm.client import LlmConfigError, load_config
from ..llm.settings import effective as saved_settings
from ..llm.settings import update_fields
from ..store.db import get_db

# ── 常量 ────────────────────────────────────────────────────────────────────
MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]
LAYER_ORDER = {"sample": 0, "edge": 1, "tiny": 2, "small": 3, "mid": 4, "stress": 5}
USER_LAYER = "user"

# 正在运行的生成子进程（pid → Popen）；DB 里的 generation_jobs 是持久化记录
RUNNING: Dict[int, subprocess.Popen] = {}

__all__ = [
    "overview",
    "problem_list",
    "problem_detail",
    "problem_quality",
    "quality",
    "settings_view",
    "test_connection",
    "update_fields",
    "import_problem",
    "import_batch",
    "start_generate",
    "job_status",
    "run_student_code",
    "diagnose_student_code",
    "merge_cases",
    "export_problem",
    "list_exports",
    "MODELS",
    "LAYER_ORDER",
]


# ── 基础工具 ────────────────────────────────────────────────────────────────
def _rows(sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    return get_db().query_all(sql, params)


def _one(sql: str, params: tuple = ()) -> Any:
    rows = _rows(sql, params)
    return rows[0] if rows else None


def _jsonable(value: Any) -> Any:
    try:
        return json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── 概览 / 列表 / 详情 ──────────────────────────────────────────────────────
def overview() -> Dict[str, Any]:
    problems = _rows(
        "SELECT id, title, status, failed_stage, tier, tags_json, algorithm_tags, analysis_json"
        " FROM problems"
    )
    total = len(problems)
    prepared = sum(1 for p in problems if p["status"] == "prepared")
    cases = _one("SELECT COUNT(*) AS n FROM test_cases")["n"]
    solutions = _one("SELECT COUNT(*) AS n FROM reference_solutions")["n"]
    hints = _one("SELECT COUNT(*) AS n FROM hints WHERE is_active=1")["n"]
    injections = sum(1 for p in problems if (_jsonable(p["tags_json"]) or {}).get("injection_tokens"))

    def _tags(row):
        value = _jsonable(row["algorithm_tags"])
        return value if isinstance(value, list) else []

    tag_counter: Dict[str, int] = {}
    for problem in problems:
        for tag in _tags(problem):
            tag_counter[tag] = tag_counter.get(tag, 0) + 1
    top_tags = sorted(tag_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:8]
    by_stage = _rows(
        "SELECT stage, COUNT(*) AS calls, SUM(total_tokens) AS tokens"
        " FROM llm_calls GROUP BY stage ORDER BY tokens DESC"
    )
    tokens = _one("SELECT COALESCE(SUM(total_tokens),0) AS n FROM llm_calls")["n"]
    return {
        "problems": {"total": total, "prepared": prepared, "failed": total - prepared},
        "cases": cases,
        "solutions": solutions,
        "hints": hints,
        "injections": injections,
        "tokens": tokens,
        "tags": [{"name": name, "count": count} for name, count in top_tags],
        "stages": [
            {"stage": r["stage"], "calls": r["calls"], "tokens": r["tokens"] or 0} for r in by_stage
        ],
    }


def problem_list() -> List[Dict[str, Any]]:
    rows = _rows(
        "SELECT p.id, p.title, p.status, p.failed_stage, p.tier, p.tags_json, p.algorithm_tags,"
        " (SELECT COUNT(*) FROM test_cases c WHERE c.problem_id=p.id) AS cases,"
        " (SELECT COUNT(*) FROM reference_solutions s WHERE s.problem_id=p.id) AS sols,"
        " (SELECT GROUP_CONCAT(DISTINCT lang) FROM reference_solutions s"
        "  WHERE s.problem_id=p.id AND verified=1) AS langs"
        " FROM problems p ORDER BY p.id"
    )
    out: List[Dict[str, Any]] = []
    for row in rows:
        tags = _jsonable(row["tags_json"]) or {}
        algo = _jsonable(row["algorithm_tags"])
        algo = algo if isinstance(algo, list) else []
        out.append(
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "failed_stage": row["failed_stage"],
                "tier": row["tier"],
                "cases": row["cases"],
                "solutions": row["sols"],
                "langs": [x for x in (row["langs"] or "").split(",") if x],
                "tags": algo,
                "injection": bool(tags.get("injection_tokens")),
            }
        )
    return out


def problem_detail(pid: int) -> Dict[str, Any]:
    """题目详情；`quality` 字段是 §4.1 的完整评分结构。"""
    problem = _one("SELECT * FROM problems WHERE id=?", (pid,))
    if not problem:
        raise KeyError(pid)
    spec = _jsonable(problem["spec_json"]) or {}
    tags = _jsonable(problem["tags_json"]) or {}
    layers = _rows(
        "SELECT layer, COUNT(*) AS n FROM test_cases WHERE problem_id=? GROUP BY layer ORDER BY layer",
        (pid,),
    )
    samples = [
        {"input": s.get("input", ""), "output": s.get("output", "")}
        for s in (spec.get("samples") or [])[:3]
    ]
    previews: Dict[str, List[Dict[str, Any]]] = {}
    for layer in ("sample", "edge", "tiny", "small", "mid", "stress", USER_LAYER):
        rows = _rows(
            "SELECT input_data, expected_output, size_class, trust_level FROM test_cases"
            " WHERE problem_id=? AND layer=? ORDER BY ordinal LIMIT 2",
            (pid, layer),
        )
        if rows:
            previews[layer] = [
                {
                    "input": r["input_data"][:1200],
                    "output": (r["expected_output"] or "")[:600],
                    "size": r["size_class"],
                    "trust": r["trust_level"],
                }
                for r in rows
            ]
    solutions = _rows(
        "SELECT lang, role, approach, complexity, verified, is_primary, verification_level, code,"
        " line_notes_json FROM reference_solutions WHERE problem_id=?"
        " ORDER BY is_primary DESC, role, lang",
        (pid,),
    )
    hints = _rows(
        "SELECT level, content, quality_flag FROM hints WHERE problem_id=? AND is_active=1"
        " ORDER BY level",
        (pid,),
    )
    similar = _rows("SELECT url, title, platform, note FROM similar_problems WHERE problem_id=?", (pid,))
    algorithm_tags = _jsonable(problem["algorithm_tags"])
    return {
        "id": problem["id"],
        "title": problem["title"],
        "status": problem["status"],
        "failed_stage": problem["failed_stage"],
        "tier": problem["tier"],
        "statement": problem["statement_md"] or "",
        "spec": {
            "grammar": spec.get("input_grammar"),
            "constraints": spec.get("constraints_exec") or [],
            "checker": spec.get("checker_type"),
            "output_type": spec.get("output_type"),
            "limits": spec.get("time_limit_ms"),
            "traps": spec.get("traps") or [],
        },
        "samples": samples,
        "layers": [{"layer": r["layer"], "count": r["n"]} for r in layers],
        "previews": previews,
        "solutions": [
            {
                "lang": s["lang"],
                "role": s["role"],
                "approach": s["approach"],
                "complexity": s["complexity"],
                "verified": bool(s["verified"]),
                "primary": bool(s["is_primary"]),
                "level": s["verification_level"],
                "code": s["code"][:20000],
                "line_notes": (_jsonable(s["line_notes_json"]) or "") if s["line_notes_json"] else "",
            }
            for s in solutions
        ],
        "hints": [
            {"level": h["level"], "content": h["content"], "flag": h["quality_flag"]} for h in hints
        ],
        "analysis": _jsonable(problem["analysis_json"]) or {},
        "quality": problem_quality(pid),
        "similar": [dict(s) for s in similar],
        "injection_tokens": tags.get("injection_tokens") or [],
        "tags": algorithm_tags if isinstance(algorithm_tags, list) else [],
    }


# ── 质量：单题评分 / 全局运行质量 ───────────────────────────────────────────
def problem_quality(pid: int) -> Dict[str, Any]:
    """单题确定性评分（契约 §4.1）：纯函数在 `app/quality.py`，这里只取数。"""
    cases = _rows(
        "SELECT layer, input_data, expected_output, trust_level FROM test_cases WHERE problem_id=?",
        (pid,),
    )
    solutions = _rows(
        "SELECT lang, role, verified FROM reference_solutions WHERE problem_id=?", (pid,)
    )
    return score_problem(cases, solutions)


def quality() -> Dict[str, Any]:
    """全局运行质量视图（既有接口，保持返回结构不变）。"""
    stages = _rows(
        "SELECT r.stage, COUNT(*) AS runs,"
        " SUM(CASE WHEN r.error_text IS NOT NULL AND r.error_text!='' THEN 1 ELSE 0 END) AS errors,"
        " (SELECT COALESCE(SUM(l.total_tokens),0) FROM llm_calls l"
        "  WHERE l.stage = CASE r.stage WHEN 'references' THEN 'reference'"
        "  ELSE r.stage END) AS tokens"
        " FROM problem_stage_runs r GROUP BY r.stage ORDER BY runs DESC"
    )
    top_tokens = _rows(
        "SELECT problem_id, SUM(total_tokens) AS tokens, COUNT(*) AS calls"
        " FROM llm_calls GROUP BY problem_id ORDER BY tokens DESC LIMIT 10"
    )
    tokens_by_stage = _rows(
        "SELECT stage, SUM(total_tokens) AS tokens, SUM(reasoning_tokens) AS reasoning"
        " FROM llm_calls GROUP BY stage ORDER BY tokens DESC"
    )
    return {
        "stages": [dict(r) for r in stages],
        "top_tokens": [dict(r) for r in top_tokens],
        "tokens_by_stage": [dict(r) for r in tokens_by_stage],
    }


# ── 设置 ────────────────────────────────────────────────────────────────────
def settings_view() -> Dict[str, Any]:
    saved = saved_settings()
    try:
        cfg = load_config()
    except LlmConfigError:
        cfg = None
    if cfg is not None:
        return {
            "base_url": cfg.base_url,
            "model": cfg.model,
            "key_set": True,
            "key_masked": cfg.masked_key,
            "key_source": cfg.key_source,
            "saved_model": saved.get("model") or "",
            "saved_base_url": saved.get("base_url") or "",
            "models": MODELS,
        }
    return {
        "base_url": saved.get("base_url")
        or os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1"),
        "model": saved.get("model") or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "key_set": False,
        "key_masked": "",
        "key_source": "missing",
        "saved_model": saved.get("model") or "",
        "saved_base_url": saved.get("base_url") or "",
        "models": MODELS,
    }


def test_connection() -> Dict[str, Any]:
    try:
        cfg = load_config()
    except LlmConfigError as exc:
        return {"ok": False, "error": str(exc)}
    url = cfg.base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg.api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        names = [item.get("id") for item in payload.get("data", []) if isinstance(item, dict)]
        return {"ok": True, "models": names, "model": cfg.model}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": exc.read().decode("utf-8", "ignore")[:300]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


# ── 导入 ────────────────────────────────────────────────────────────────────
def import_problem(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..app.ingest_csp import parse_samples

    title = str(payload.get("title") or "").strip()
    statement = str(payload.get("statement") or "").strip()
    languages = [str(x) for x in (payload.get("languages") or []) if str(x) in ("c", "cpp", "python")]
    if not title or not statement:
        return {"error": "标题和题面都不能为空"}
    parsed, _external, _warnings = parse_samples(statement.splitlines())
    samples = [
        {"input": item.input_text, "output": item.output_text}
        for item in parsed
        if item.input_text and item.output_text
    ]
    spec_json = json.dumps({"samples": samples}, ensure_ascii=False)
    now = _now()
    try:
        with get_db().transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO problems(title, source, statement_md, spec_json, lang_available, status,"
                " created_at, updated_at) VALUES(?, '用户导入', ?, ?, ?, 'draft', ?, ?)",
                (title, statement, spec_json, json.dumps(languages or ["cpp", "c", "python"]), now, now),
            )
            pid = cursor.lastrowid
    except sqlite3.IntegrityError:
        # 题面去重键（statement_key，前 500 字）或 (source, source_id) 撞车
        return {"error": "题面或标题重复"}
    return {"id": pid, "samples": len(samples)}


def split_batch_text(text: str) -> List[Dict[str, Any]]:
    """按单独一行 `---` 分块；每块第一行标题，其余题面（契约 §4.5）。

    `index` 是**原始块序号**（0 基，含中间的空块位置），用于把失败原因准确回给调用方；
    只带标题没有题面的块**保留**在结果里（`statement==""`），由 `import_batch` 记进 failed，
    不允许静默丢弃。完全空白的块（连标题都没有）才忽略。
    """
    raw_blocks: List[List[str]] = []
    current: List[str] = []
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        if line.strip() == "---":
            raw_blocks.append(current)
            current = []
            continue
        current.append(line)
    raw_blocks.append(current)

    blocks: List[Dict[str, Any]] = []
    for index, lines in enumerate(raw_blocks):
        body = list(lines)
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        if not body:
            continue
        title = body[0].strip().lstrip("#").strip()
        if not title:
            continue
        blocks.append(
            {"index": index, "title": title, "statement": "\n".join(body[1:]).strip()}
        )
    return blocks


def import_batch(payload: Dict[str, Any]) -> Dict[str, Any]:
    """批量导入：逐块复用 `import_problem`，**单块失败不中断、不 500**（B3）。

    每块独立 try/except（数据库唯一约束、参数错误、其它异常都不许穿透），
    失败一律进 `failed:[{index,title,error}]`，`index` 是原始块序号。
    """
    blocks = split_batch_text(str(payload.get("text") or ""))
    if not blocks:
        return {"error": "没有可导入的题目（按单独一行 --- 分块）"}
    languages = list(payload.get("languages") or [])
    imported: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for block in blocks:
        index = int(block.get("index", 0))
        title = str(block.get("title") or "")
        if not str(block.get("statement") or "").strip():
            failed.append({"index": index, "title": title, "error": "题面为空"})
            continue
        try:
            result = import_problem(
                {"title": title, "statement": block["statement"], "languages": languages}
            )
        except sqlite3.IntegrityError as exc:
            failed.append({"index": index, "title": title, "error": f"题面或标题重复：{exc}"})
            continue
        except (ValueError, TypeError) as exc:
            failed.append({"index": index, "title": title, "error": f"参数错误：{exc}"})
            continue
        except Exception as exc:  # noqa: BLE001 - 单块异常绝不能拖垮整批
            failed.append({"index": index, "title": title, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if result.get("error"):
            failed.append({"index": index, "title": title, "error": result["error"]})
        else:
            imported.append({"id": result["id"], "title": title, "samples": result["samples"]})
    return {"imported": imported, "failed": failed}


# ── 生成任务（持久化）────────────────────────────────────────────────────────
RUNNING = jobs.RUNNING  # 兼容既有调用点/测试：同一个 dict 对象


def start_generate(payload: Dict[str, Any]) -> Dict[str, Any]:
    """薄封装：任务生命周期全在 `app/jobs.py`。"""
    root = Path(__file__).resolve().parents[2]
    return jobs.start_generate(get_db(), payload, root=root, log_dir=_debug_log_dir())


def job_status(pid: int) -> Dict[str, Any]:
    return jobs.job_status(get_db(), pid)


def _debug_log_dir() -> Path:
    root = Path(__file__).resolve().parents[2]
    log_dir = root / ".tmp"
    log_dir.mkdir(exist_ok=True)
    return log_dir


# ── 学生代码运行 / 诊断 ─────────────────────────────────────────────────────
def _student_cases(pid: int, spec: Dict[str, Any], scope: str, limit: int) -> List[Dict[str, Any]]:
    if scope == "samples":
        return [
            {"id": f"sample{i}", "layer": "sample", "input": s.get("input", ""), "expected": s.get("output", "")}
            for i, s in enumerate(spec.get("samples") or [], 1)
        ]
    rows = _rows(
        "SELECT id, layer, input_data, expected_output FROM test_cases WHERE problem_id=?"
        " ORDER BY CASE layer WHEN 'sample' THEN 0 WHEN 'boundary' THEN 1 WHEN 'edge' THEN 1"
        " WHEN 'tiny' THEN 2 WHEN 'small' THEN 3 WHEN 'exhaustive' THEN 3"
        " WHEN 'mid' THEN 4 WHEN 'stress' THEN 5 ELSE 9 END, ordinal LIMIT ?",
        (pid, limit),
    )
    return [
        {"id": f"case{r['id']}", "layer": r["layer"], "input": r["input_data"], "expected": r["expected_output"] or ""}
        for r in rows
    ]


def run_student_code(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..app import judge

    pid = int(payload.get("problem_id") or 0)
    lang = str(payload.get("lang") or "")
    code = str(payload.get("code") or "")
    if lang not in ("c", "cpp", "python"):
        return {"error": f"不支持的语言：{lang}"}
    if not code.strip() or len(code) > 30000:
        return {"error": "代码为空或超过 30000 字符"}
    problem = _one("SELECT id, spec_json FROM problems WHERE id=?", (pid,))
    if not problem:
        return {"error": "题目不存在"}
    spec = _jsonable(problem["spec_json"]) or {}
    scope = str(payload.get("scope") or "samples")
    limit = max(1, min(int(payload.get("limit") or 20), 30))
    custom = payload.get("custom_input")
    if custom is not None:
        cases = [{"id": "custom", "layer": "自定义", "input": str(custom)[:8000], "expected": ""}]
    else:
        cases = _student_cases(pid, spec, scope, limit)
    if not cases:
        return {"error": "没有可运行的用例"}
    batch = judge.run_solution(
        lang,
        code,
        cases,
        checker_type=spec.get("checker_type", "exact"),
        float_eps=float(spec.get("float_eps") or 1e-6),
        time_limit_ms=3000,
        compare_expected=custom is None,
    )
    runs = []
    for run in batch.runs:
        case = next((c for c in cases if c["id"] == run.case_id), {})
        runs.append(
            {
                "id": run.case_id,
                "layer": case.get("layer", ""),
                "verdict": run.verdict,
                "elapsed_ms": round(run.elapsed_ms, 1),
                "input": case.get("input", "")[:4000],
                "expected": (case.get("expected") or "")[:2000],
                "actual": (run.stdout or "")[:2000],
                "stderr": (run.stderr or "")[:800],
            }
        )
    first = next((r for r in runs if r["verdict"] != "ac"), None)
    return {
        "ok": not batch.compile_error,
        "compile_error": batch.compile_error[:4000],
        "total": len(runs),
        "passed": sum(1 for r in runs if r["verdict"] == "ac"),
        "runs": runs,
        "first_failure": first,
        "model": "",
        "custom": custom is not None,
    }


def diagnose_student_code(payload: Dict[str, Any]) -> Dict[str, Any]:
    from dataclasses import replace

    from ..app.prep_rules import spec_block, statement_excerpt
    from ..llm.client import LlmClient
    from ..prompts import loader as prompts

    pid = int(payload.get("problem_id") or 0)
    problem = _one("SELECT * FROM problems WHERE id=?", (pid,))
    if not problem:
        return {"error": "题目不存在"}
    spec = _jsonable(problem["spec_json"]) or {}
    case = payload.get("case") or {}
    lang = str(payload.get("lang") or "")
    code = str(payload.get("code") or "")[:20000]
    body = "\n".join(
        [
            statement_excerpt(dict(problem)),
            "",
            "## 约束",
            spec_block(spec),
            "",
            f"## 学生代码（{lang}）",
            f"```{lang}",
            code,
            "```",
            "",
            "## 失败用例",
            "输入：",
            str(case.get("input") or "")[:3000],
            "",
            "标准答案：",
            str(case.get("expected") or "")[:2000],
            "",
            "实际输出：",
            str(case.get("actual") or "")[:2000],
            "",
            f"判定：{case.get('verdict') or 'wa'}",
        ]
    )
    try:
        client = LlmClient(verbose=False)
        model = str(payload.get("model") or "").strip()
        if model and model != client.config.model:
            client = LlmClient(config=replace(client.config, model=model), verbose=False)
        result = client.chat(
            [
                {"role": "system", "content": prompts.load(prompts.STUDENT_DIAGNOSE)},
                {"role": "user", "content": body},
            ],
            json_mode=True,
            max_tokens=4096,
            temperature=0.2,
            tag="student_diagnose",
        )
        data = result.json_obj or {}
        data["model"] = client.config.model
        return data
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}


# ── 合并“我的数据”（契约 §4.2）──────────────────────────────────────────────
def merge_cases(payload: Dict[str, Any]) -> Dict[str, Any]:
    """薄封装：校验/去重/对拍/入库全在 `app/cases_merge.py`（确定性代码，可单测）。"""
    from ..app.cases_merge import merge_cases as _merge

    return _merge(payload, db=get_db())


# ── 导出 + 版本历史（契约 §4.3）─────────────────────────────────────────────
def export_problem(pid: int) -> Dict[str, Any]:
    """薄封装：打包/版本递增/写历史全在 `app/exporter.py`。"""
    from ..app.exporter import export_problem as _export

    return _export(pid, db=get_db())


def list_exports(pid: int) -> List[Dict[str, Any]]:
    from ..app.exporter import list_exports as _list

    return _list(pid, db=get_db())
