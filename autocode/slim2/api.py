"""业务层：路由只做参数搬运，校验与状态判断都在这里。

返回约定：成功 `{ok: True, ...}`；失败 `{error, code}`，由 server 映射 HTTP 状态。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import time
from dataclasses import replace
from typing import Any, Dict, List, Optional

from ..llm.client import LlmClient, LlmConfig, LlmError
from . import banks, config, files, jobs, problem_purge, segment, user_llm
from .api_common import _int, _loads, _now, _page_limit
from .llm_stage import StageRunner
from .validators import code_hash, judge_supported


# ── 导入 ────────────────────────────────────────────────────────────────────
def import_text(
    db: Any, runner: StageRunner, payload: Dict[str, Any], role: str = "student"
) -> Dict[str, Any]:
    text = str(payload.get("text") or "")
    if not text.strip():
        return {"error": "文本为空", "code": "INVALID_INPUT"}
    # 文件导入分两步时，前端会把第一步的图块坐标与 PDF 缓存键回传；
    # 纯文本粘贴没有这两项，留空即可（图块为空就是"这题没图"）。
    figures = payload.get("figures")
    ranges = payload.get("page_ranges")
    return segment.segment_text(
        db,
        runner,
        text,
        source_name=str(payload.get("source_name") or "")[:120],
        kind=str(payload.get("kind") or "text"),
        figures=figures if isinstance(figures, list) else None,
        page_ranges=ranges if isinstance(ranges, list) else None,
        pdf_key=str(payload.get("pdf_key") or ""),
        bank_id=_int(payload.get("bank_id"), banks.DEFAULT_ID),
        role=role,
    )


def import_detail(db: Any, batch_id: int) -> Dict[str, Any]:
    return segment.batch_detail(db, int(batch_id))


#: PDF 字节缓存：切题阶段只拿到文本，确认入库时要按原 PDF 裁图，所以先按内容 hash 存一份。
PDF_CACHE_DIR = config.DATA_DIR / "pdf_cache"
#: 插图根目录（server 的 /assets/ 路由指向这里）。
ASSETS_DIR = config.DATA_DIR / "assets"


def cache_pdf(raw: bytes) -> str:
    """把 PDF 按内容 hash 存进缓存目录，返回 16 位 key（同一份文件不会重复写）。"""
    key = hashlib.sha256(raw).hexdigest()[:16]
    PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = PDF_CACHE_DIR / f"{key}.pdf"
    if not target.exists():
        target.write_bytes(raw)
    return key


def import_extract(
    db: Any, payload: Dict[str, Any], runner: Optional[StageRunner] = None
) -> Dict[str, Any]:
    """上传文件（PDF / Word / 图片 / TXT / MD）→ 只抽文本，不切题。

    刻意不在这里顺手切题：PDF 抽出来的文字常有页眉、页码、公式乱码，
    必须让人先看一眼、必要时改两笔，再点"开始切题"。否则错文本会被固化进库。

    图片没有文字层：交给多模态模型转写（`vision.transcribe`），需要调用者的 Key，
    所以 `runner` 为 None 时直接拒绝——绝不复用别人的额度。

    PDF 额外回传图块清单与页-行映射（都很小：16 张图约 1KB）。
    图片本身不走前端，确认入库时由服务端按 bbox 从缓存的 PDF 裁剪。
    """
    figures: List[Dict[str, Any]] = []
    page_ranges: List[List[int]] = []
    pdf_key = ""
    try:
        name, raw = files.decode_upload(payload)
        kind = files.sniff_kind(name, raw)
        bundle = files.extract_pdf_bundle(raw) if kind == files.KIND_PDF else None
        if kind == files.KIND_IMAGE:
            if runner is None:
                raise files.FileError(
                    "识别图片要用到你自己的模型 Key：请先在右上角「模型设置」里配置后再上传",
                    "NO_KEY",
                )
            from . import vision

            text = vision.transcribe(runner, name, raw)
        elif bundle is not None:
            # PDF 的文本**只用 bundle 这一份**：它已删掉页眉页脚与图内文字，
            # 行号、页映射、图锚点都按它编号。另起一份文本会让行号错位，
            # 图归属会静默失效（实测：两者只差 38 行，4 张图全部丢失）。
            cleaned = str(bundle.get("text") or "")
            if len(cleaned.strip()) >= 50:
                text = cleaned
                figures = list(bundle["figures"])
                page_ranges = list(bundle["page_ranges"])
                pdf_key = cache_pdf(raw)
            else:
                # 扫描件/拍照导出的 PDF：没有文字层，但页面本身就是图片。
                # 渲染成图走同一条视觉通道，而不是让用户自己截图重传。
                if runner is None:
                    raise files.FileError(
                        "这份 PDF 没有文字层（扫描件或拍照导出），需要多模态模型识别页面。"
                        "请先在右上角「模型设置」里配置 Key 后再上传",
                        "NO_KEY",
                    )
                from . import vision

                text = vision.transcribe_pages(runner, name, files.render_pdf_pages(raw))
        else:
            text = files.extract_text(name, raw)
    except files.FileError as exc:
        return {"error": str(exc), "code": exc.code}

    if len(text) > config.MAX_IMPORT_CHARS:
        return {
            "error": f"抽出 {len(text)} 字，超过单次上限 {config.MAX_IMPORT_CHARS} 字，请拆分后分批导入",
            "code": "TOO_LARGE",
        }

    return {
        "ok": True,
        "filename": name,
        "chars": len(text),
        "text": text,
        "figures": figures,
        "page_ranges": page_ranges,
        "pdf_key": pdf_key,
        "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
    }


def import_file(
    db: Any, runner: StageRunner, payload: Dict[str, Any], role: str = "student"
) -> Dict[str, Any]:
    """上传文件 → 抽文本 → 直接切题。

    切题本来就是模型干的活，不该让人先当校对员。抽取的原文随结果回传一份，
    只在"切出来的题不对"时展开看，不挡在正常流程里。
    """
    prepared = import_extract(db, payload, runner)
    if prepared.get("error"):
        return prepared
    text = str(prepared["text"])
    result = segment.segment_text(
        db,
        runner,
        text,
        source_name=str(prepared["filename"]),
        kind="file",
        figures=prepared.get("figures") or [],
        page_ranges=prepared.get("page_ranges") or [],
        pdf_key=str(prepared.get("pdf_key") or ""),
        bank_id=_int(payload.get("bank_id"), banks.DEFAULT_ID),
        role=role,
    )
    if isinstance(result, dict) and result.get("ok"):
        result["source"] = {
            "filename": prepared["filename"],
            "chars": len(text),
            # 全文回传：切题用的是它，人排查时看到的也该是同一份
            "preview": text,
            "truncated": False,
        }
    return result


# ── LLM 设置（每人自己的 API Key，互不通用） ────────────────────────────────
_DEFAULT_BASE = user_llm.DEFAULT_BASE


def llm_settings(db: Any, user: str = "") -> Dict[str, Any]:
    """当前账号自己的配置。永不回传明文 key，只给后 4 位。"""
    return user_llm.get(db, user)


def save_llm_settings(db: Any, payload: Dict[str, Any], user: str = "") -> Dict[str, Any]:
    """按字段保存到当前账号名下；`api_key` 传空表示不改，`clear_key=true` 才清空。"""
    return user_llm.save(db, user, payload)


def probe_llm(db: Any, payload: Dict[str, Any], user: str = "") -> Dict[str, Any]:
    """用表单里的值（没填的用本账号已保存的）真发一次最小请求。

    只判"能不能通"：不校验回复内容。也绝不用别人的 Key 代测。
    """
    cfg = user_llm.config_for(db, user)
    key = str(payload.get("api_key") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    model = str(payload.get("model") or "").strip()

    reason = user_llm.base_url_error(base_url)
    if reason:
        return {"error": reason, "code": "INVALID_INPUT"}

    if cfg is None:
        if not key:
            return {"error": "还没有 API Key，请先填写你自己的 Key", "code": "NO_KEY"}
        cfg = LlmConfig(
            api_key=key,
            base_url=base_url or _DEFAULT_BASE,
            model=model or user_llm.DEFAULT_MODEL,
            key_source="user",
        )
    else:
        cfg = replace(
            cfg,
            api_key=key or cfg.api_key,
            base_url=base_url or cfg.base_url,
            model=model or cfg.model,
        )
    cfg = replace(cfg, timeout_s=25, max_retries=1)

    started = time.perf_counter()
    try:
        result = LlmClient(config=cfg).chat(
            [{"role": "user", "content": "只回复 ok"}],
            json_mode=False, temperature=0.0, max_tokens=8, timeout_s=25,
            tag="settings_probe", stage="settings_probe",
        )
    except LlmError as exc:
        return {"error": f"连接失败：{exc}", "code": "LLM_UNREACHABLE"}
    except Exception as exc:  # noqa: BLE001 - 网络层异常都要变成人话
        return {"error": f"连接异常：{type(exc).__name__}", "code": "LLM_UNREACHABLE"}
    return {
        "ok": True,
        "model": str(result.model or cfg.model),
        "reply": str(result.content or "")[:40],
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def confirm_import(
    db: Any, batch_id: int, payload: Dict[str, Any], role: str = "student"
) -> Dict[str, Any]:
    raw = payload.get("indices")
    indices = [int(item) for item in raw] if isinstance(raw, list) else []
    return segment.confirm_import(
        db,
        int(batch_id),
        indices,
        bank_id=_int(payload.get("bank_id"), banks.DEFAULT_ID),
        role=role,
    )


# ── 题库 ────────────────────────────────────────────────────────────────────
def list_banks(db: Any, role: str = "student") -> Dict[str, Any]:
    return {"ok": True, "banks": banks.list_banks(db, role)}


def create_bank(db: Any, payload: Dict[str, Any], role: str, user: str = "") -> Dict[str, Any]:
    """新建永久库：只有管理员能建，建出来的库一律是 permanent。"""
    if str(role or "") != "admin":
        return {"error": "只有管理员可以新建永久库", "code": "FORBIDDEN"}
    return banks.create(db, payload.get("name"), user)


def bank_write_error(db: Any, bank_id: Any, role: str) -> Optional[Dict[str, Any]]:
    """入库前的第一道检查：库在不在、这个角色能不能往里加题。

    放在服务器入口而不是只放在 `segment_text` 里，是为了让"学生选了永久库"
    直接得到明确的拒绝，而不是先被"没配 Key"挡住——那句提示会把人带偏。
    """
    bank = banks.get(db, _int(bank_id, banks.DEFAULT_ID))
    if bank is None:
        return {"error": "题库不存在", "code": "NOT_FOUND"}
    if not banks.can_manage(bank, role):
        return {"error": banks.DENY_ADD, "code": "FORBIDDEN"}
    return None


# ── 生成任务 ────────────────────────────────────────────────────────────────
def start_dataset(db: Any, payload: Dict[str, Any], user: str = "") -> Dict[str, Any]:
    pid = _int(payload.get("id"))
    row = db.query_one("SELECT * FROM problems WHERE id=?", (pid,))
    if row is None:
        return {"error": "题目不存在", "code": "NOT_FOUND"}
    if not judge_supported(str(row["judge_type"])):
        return {
            "error": f"判定方式 {row['judge_type']} 本版本不支持（没有自定义 checker）",
            "code": "INVALID_INPUT",
        }
    # 老题的样例是导入那一刻算的：先按当前规则补算一次，再判断能不能生成
    samples = _loads(row["samples_json"], []) or segment.refresh_samples(db, row)
    if config.require_samples() and not samples:
        return {"error": "这道题没有解析出样例，无法校准期望输出", "code": "NEED_SAMPLES"}
    budget = max(2, min(10, _int(payload.get("budget"), config.DEFAULT_BUDGET_CALLS)))
    result = jobs.start(
        db, kind="dataset", problem_id=pid, args=[], budget=budget,
        limit=config.MAX_DATASET_JOBS, user=user,
    )
    if not result.get("ok"):
        return result
    with db.transaction() as conn:
        conn.execute("UPDATE problems SET status='generating' WHERE id=?", (pid,))
    return {
        "ok": True,
        "job_id": int(result["job_id"]),
        "problem_id": pid,
        "budget": budget,
    }


def start_adversarial(db: Any, payload: Dict[str, Any], user: str = "") -> Dict[str, Any]:
    pid = _int(payload.get("id"))
    row = db.query_one("SELECT * FROM problems WHERE id=?", (pid,))
    if row is None:
        return {"error": "题目不存在", "code": "NOT_FOUND"}
    if int(row["dataset_version"] or 0) <= 0:
        return {"error": "请先生成测试数据，再用错解生成对抗数据", "code": "NO_DATA"}
    code = str(payload.get("code") or "")
    if not code.strip():
        return {"error": "错解代码为空", "code": "INVALID_INPUT"}
    if len(code) > config.MAX_CODE_CHARS:
        return {"error": f"代码超过 {config.MAX_CODE_CHARS} 字符", "code": "PAYLOAD_TOO_LARGE"}

    from .adversarial import normalize_lang
    from .errors import StageFailure

    try:
        lang = normalize_lang(str(payload.get("lang") or "python"))
    except StageFailure as exc:
        return {"error": exc.message, "code": "INVALID_INPUT"}

    digest = code_hash(code)
    existed = db.query_one(
        "SELECT id FROM submissions WHERE problem_id=? AND code_hash=?", (pid, digest)
    )
    if existed is not None:
        submission_id = int(existed["id"])
    else:
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO submissions(problem_id, lang, code, code_hash, status, created_at)"
                " VALUES(?,?,?,?, 'new', ?)",
                (pid, lang, code, digest, _now()),
            )
            submission_id = int(cursor.lastrowid)

    result = jobs.start(
        db, kind="adversarial", problem_id=pid,
        args=["--submission-id", str(submission_id)],
        budget=config.DEFAULT_BUDGET_CALLS, limit=config.MAX_ADVERSARIAL_JOBS,
        user=user,
    )
    if not result.get("ok"):
        return result
    return {
        "ok": True,
        "job_id": int(result["job_id"]),
        "submission_id": submission_id,
        "problem_id": pid,
    }


MAX_ADV_CODE_CHARS = 12_000


def list_adversarial(db: Any, problem_id: int, limit: int = 100) -> Dict[str, Any]:
    row = db.query_one("SELECT id FROM problems WHERE id=?", (int(problem_id),))
    if row is None:
        raise KeyError(problem_id)
    rows = db.query_all(
        "SELECT a.*, r.verdict AS review_verdict, r.reason_code, r.reason AS review_reason,"
        "       s.lang AS sub_lang"
        " FROM adversarial_cases a"
        " LEFT JOIN adversarial_reviews r ON r.adv_case_id = a.id"
        " LEFT JOIN submissions s ON s.id = a.submission_id"
        " WHERE a.problem_id=? AND a.status='active'"
        " ORDER BY a.submission_id DESC, a.id DESC LIMIT ?",
        (int(problem_id), max(1, min(200, int(limit)))),
    )
    # 同一份错解的所有用例共用一个 submission：代码只回传一次，
    # 否则 12 条用例会把同一份 30 KB 源码重复 12 遍。
    submissions: List[Dict[str, Any]] = []
    seen: set = set()
    for sub in db.query_all(
        "SELECT id, lang, code, baseline_json FROM submissions WHERE problem_id=?"
        " ORDER BY id DESC LIMIT 50",
        (int(problem_id),),
    ):
        sid = int(sub["id"])
        if sid in seen:
            continue
        if not any(int(item["submission_id"]) == sid for item in rows):
            continue  # 本题没有对抗数据的错解不必回传源码
        seen.add(sid)
        code = str(sub["code"] or "")
        baseline = _loads(sub["baseline_json"], {})
        submissions.append(
            {
                "id": sid,
                "lang": str(sub["lang"] or ""),
                "code": code[:MAX_ADV_CODE_CHARS],
                "truncated": len(code) > MAX_ADV_CODE_CHARS,
                # 整份代码的 bug 定位：每条用例的 failure_reason 是它下面的一行
                "diagnosis": baseline.get("diagnosis") or {},
            }
        )
    return {
        "ok": True,
        "items": [
            {
                "id": int(item["id"]),
                "input": str(item["input_data"]),
                "expected_output": str(item["expected_output"]),
                "actual_output": str(item["actual_output"]),
                "predicted_output": str(item["predicted_output"]),
                "verdict": str(item["run_verdict"]),
                "targets": str(item["targets"]),
                "why": str(item["why"]),
                # 老数据没有这一列：退回 targets，界面至少能给一句定位
                "failure_reason": str(item["failure_reason"] or item["targets"] or ""),
                # 让界面能如实区分"模型给的原因"和"只是目标描述的旧数据"
                "reason_is_fallback": not str(item["failure_reason"] or ""),
                "review": str(item["review_verdict"] or ""),
                "review_reason": str(item["review_reason"] or ""),
                "submission_id": int(item["submission_id"]),
                # 同一道题会有多份错解：用例必须能追溯到"卡的是哪份代码"，
                # 否则几代错解的用例混在一屏，人会以为这次生成了这么多
                "lang": str(item["sub_lang"] or ""),
            }
            for item in rows
        ],
        "submissions": submissions,
    }


def job_status(db: Any, job_id: int) -> Dict[str, Any]:
    return jobs.by_id(db, int(job_id))


def cancel_job(db: Any, payload: Dict[str, Any]) -> Dict[str, Any]:
    pid = _int(payload.get("id"))
    if pid <= 0:
        return {"error": "缺少题目 id", "code": "INVALID_INPUT"}
    return jobs.cancel(db, pid, str(payload.get("kind") or ""))


def cancel_job_by_job(db: Any, job_id: int) -> Dict[str, Any]:
    return jobs.cancel_by_id(db, int(job_id))


def export_problem(db: Any, problem_id: int, fmt: str = "json") -> Dict[str, Any]:
    """导出当前版本的数据：JSON（结构化）或 CSV（Excel 直接打开）。

    导出的是"拿得走"的完整一份：题面、样例、约束、测试数据、对抗数据。
    对抗数据也带上学生实际输出——否则读的人无法判断这条用例到底卡住了什么。
    """
    row = db.query_one("SELECT * FROM problems WHERE id=?", (int(problem_id),))
    if row is None:
        return {"error": "题目不存在", "code": "NOT_FOUND"}
    pid = int(row["id"])
    version = int(row["dataset_version"] or 0)
    if version <= 0:
        return {"error": "这道题还没有测试数据，先生成再导出", "code": "NO_DATA"}

    cases = [
        {
            "layer": str(item["layer"]),
            "size_class": str(item["size_class"] or ""),
            "input": str(item["input_data"]),
            "expected_output": str(item["expected_output"]),
            "trust": str(item["trust"] or ""),
        }
        for item in db.query_all(
            "SELECT layer, size_class, input_data, expected_output, trust FROM test_cases"
            " WHERE problem_id=? AND version=? AND status='active' ORDER BY ordinal, id",
            (pid, version),
        )
    ]
    adversarial = [
        {
            "input": str(item["input_data"]),
            "expected_output": str(item["expected_output"]),
            "actual_output": str(item["actual_output"]),
            "verdict": str(item["run_verdict"] or ""),
            "targets": str(item["targets"] or ""),
            "failure_reason": str(item["failure_reason"] or ""),
        }
        for item in db.query_all(
            "SELECT input_data, expected_output, actual_output, run_verdict, targets, failure_reason"
            " FROM adversarial_cases WHERE problem_id=? AND status='active' ORDER BY id",
            (pid,),
        )
    ]

    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["kind", "layer", "size_class", "input", "expected_output",
                         "actual_output", "verdict", "trust", "failure_reason"])
        for item in cases:
            writer.writerow(["dataset", item["layer"], item["size_class"], item["input"],
                             item["expected_output"], "", "", item["trust"], ""])
        for item in adversarial:
            writer.writerow(["adversarial", "", "", item["input"], item["expected_output"],
                             item["actual_output"], item["verdict"], "", item["failure_reason"]])
        # BOM：不加的话 Excel 打开中文会乱码
        return {
            "ok": True,
            "filename": f"problem-{pid}-v{version}.csv",
            "content": "\ufeff" + buffer.getvalue(),
            "content_type": "text/csv; charset=utf-8",
        }

    payload = {
        "problem": {
            "id": pid,
            "title": str(row["title"]),
            "judge_type": str(row["judge_type"]),
            "tolerance": str(row["tolerance"] or ""),
            "multi_testcase": bool(row["multi_testcase"]),
            "version": version,
            "statement": str(row["statement_md"]),
        },
        "samples": _loads(row["samples_json"], []),
        "constraints": _loads(row["constraints_json"], []),
        "dataset": cases,
        "adversarial": adversarial,
    }
    return {
        "ok": True,
        "filename": f"problem-{pid}-v{version}.json",
        "content": json.dumps(payload, ensure_ascii=False, indent=2),
        "content_type": "application/json; charset=utf-8",
    }


def delete_problem(
    db: Any, problem_id: int, confirm_title: str = "", role: str = "student"
) -> Dict[str, Any]:
    row = db.query_one(
        "SELECT id, bank_id, title, status, dataset_version FROM problems WHERE id=?",
        (int(problem_id),),
    )
    if row is None:
        return {"error": "题目不存在", "code": "NOT_FOUND"}
    pid = int(row["id"])
    bank = banks.get(db, row["bank_id"])
    if bank is not None and banks.is_permanent(bank) and str(role or "") != "admin":
        return {"error": banks.DENY_DELETE, "code": "FORBIDDEN"}
    title = str(row["title"])
    cases = int(db.query_scalar(
        "SELECT COUNT(*) FROM test_cases WHERE problem_id=? AND status='active'", (pid,), 0
    ))
    adversarial = int(db.query_scalar(
        "SELECT COUNT(*) FROM adversarial_cases WHERE problem_id=? AND status='active'", (pid,), 0
    ))
    # 有数据的题必须输入标题确认：误点一次就丢整题数据，代价太大。
    # 草稿题没有数据，直接删。
    if (cases or adversarial) and confirm_title.strip() != title:
        return {
            "error": "这道题已有数据，删除前请输入题目名称确认",
            "code": "CONFIRM_REQUIRED",
            "title": title,
            "cases": cases,
            "adversarial": adversarial,
        }
    # 级联清理与「题库超限自动删题」共用一条路径：漏一张表就会留下孤儿行
    problem_purge.purge_problem(db, pid)
    return {"ok": True}


def restore_statement(db: Any, problem_id: int) -> Dict[str, Any]:
    """把模型补全过的题面还原成原文。

    补全是有副作用的写库操作，必须给回退路径：否则用户看到的题一旦被改坏，
    除了重新导入没有别的办法（而重新导入会被 statement_hash 判成重复题）。
    """
    row = db.query_one(
        "SELECT statement_original, statement_repaired FROM problems WHERE id=?",
        (int(problem_id),),
    )
    if row is None:
        return {"error": "题目不存在", "code": "NOT_FOUND"}
    original = str(row["statement_original"] or "")
    if not int(row["statement_repaired"] or 0) or not original:
        return {"error": "这道题的题面没有被模型补全过", "code": "NOT_REPAIRED"}
    with db.transaction() as conn:
        conn.execute(
            "UPDATE problems SET statement_md=?, statement_original='', statement_repaired=0,"
            " samples_json='[]', exec_spec_json='', updated_at=? WHERE id=?",
            (original, _now(), int(problem_id)),
        )
    return {"ok": True, "statement": original, "problem_id": int(problem_id)}

# 反馈相关函数移到了 api_feedback，这里再导出一次，保持 `api.xxx` 的调用方式不变。
# 必须放在文件末尾：api_feedback 反过来要用本模块的 _page_limit/_now。
from .api_feedback import (  # noqa: E402  (晚导入，避免循环依赖)
    FEEDBACK_DAILY_LIMIT,
    FEEDBACK_KINDS,
    close_feedback,
    create_feedback,
    list_feedback,
    reply_feedback,
)
# 题库读取拆到 api_catalog（api.py 的 800 行硬线）；这里再导出一次，
# 保持 `api.list_problems / api.problem_detail / api.list_cases` 的调用方式不变。
from .api_catalog import list_cases, list_problems, problem_detail  # noqa: E402
