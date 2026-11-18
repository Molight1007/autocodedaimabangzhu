"""autoCode 命令行入口：`python -m autocode.cli <子命令>`。

已实现子命令：
- `ingest <file.md> [--source CSP] [--db PATH]`  题面入册（样例归一 + 注入打标）
- `list [--db PATH]`                             题库概览（每题样例/数据条数）
- `prepare --problem-id N [--budget 40]`         预生成流水线（LLM + 执行引擎）
- `show --problem-id N [--layer stress]`          查题（spec/参考解/数据/提示）
- `delete --problem-id N --yes`                   删整题（不可恢复，先预览再确认）
- `spec-set --problem-id N --file spec.json`      人工注入 spec（spec 阶段失败的接管口）
- `adversarial --problem-id N --code-file x.py`   针对学生错解生成对抗数据（入库 adversarial 层）

约定：所有子命令只打印结构化摘要（不做 UI），退出码 0=成功 / 1=失败（便于脚本判）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from .app.ingest_csp import ingest_file
from .store.db import Database


def _db_from_args(args: argparse.Namespace) -> Database:
    path = Path(args.db) if getattr(args, "db", None) else None
    db = Database(path) if path is not None else Database()
    db.init_schema()
    return db


def _print_report(report) -> None:
    total = len(report.problems)
    print(f"== ingest {report.path} ==")
    print(f"识别题目: {total} 道 | 去重命中: {sum(1 for p in report.problems if not p.created)} 道")
    for item in report.problems:
        flag = "新增" if item.created else "已存在"
        inj = f" | 注入打标: {','.join(item.injection_hits)}" if item.injection_hits else ""
        print(
            f"  [{item.problem_id}] {item.title}（{item.source_id}）{flag} | "
            f"内联样例 {len(item.samples)} 组 | 外链样例 {item.external_samples} 组 | "
            f"题面 {item.statement_chars} 字{inj}"
        )
        for sample in item.samples[:1]:
            head = sample.input_text.strip().split("\n")[:3]
            out_head = sample.output_text.strip().split("\n")[:3]
            print(f"      样例{sample.index} 输入前3行: {head}")
            print(f"      样例{sample.index} 输出前3行: {out_head}")
    if report.warnings:
        print(f"-- 警告 {len(report.warnings)} 条 --")
        for warning in report.warnings[:20]:
            print(f"  ! {warning}")


def _service(max_calls: int, token_cap: int = 0):
    from .app.prep_service import PrepService
    from .llm.client import LlmClient
    from .store.db import get_db
    service = PrepService(db=get_db(), client=LlmClient(verbose=False), max_calls=max_calls, verbose=False)
    if token_cap:
        service.token_cap = int(token_cap)
    return service


def _targets(args):
    """按 --problem-id / --all 取目标。

    题号不存在时打印并跳过：历史 bug 是 `dict(None)` 直接抛 TypeError + traceback。
    """
    from .store.db import get_db
    db = get_db()
    if getattr(args, "all", False):
        return [dict(r) for r in db.query_all("SELECT * FROM problems WHERE status='prepared' ORDER BY id")]
    targets = []
    for pid in (args.problem_id or []):
        row = db.query_one("SELECT * FROM problems WHERE id=?", (pid,))
        if row is None:
            print(f"#{pid} 不存在，已跳过")
            continue
        targets.append(dict(row))
    return targets


def _parse_lang_list(raw: object) -> List[str]:
    """lang_available 的兼容解析：落库格式是逗号串（'python,cpp,c'），也容忍 JSON 数组。"""
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        value = json.loads(text)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in text.split(",") if item.strip()]


def _failed_guard(problem: dict, force: bool) -> Optional[str]:
    """失败题的重跑护栏：确定性失败直接重跑只会再烧一遍预算，必须显式 --force。"""
    if problem.get("status") != "failed" or force:
        return None
    from .app.prep_rules import failure_advice
    from .store.db import get_db

    db = get_db()
    last = db.query_one(
        "SELECT stage, error_text FROM problem_stage_runs WHERE problem_id=?"
        " ORDER BY id DESC LIMIT 1",
        (problem["id"],),
    )
    lines = [
        f"#{problem['id']} 上次生成已失败（stage={problem.get('failed_stage')}），"
        "直接重跑大概率重蹈覆辙。",
    ]
    if last is not None and last["error_text"]:
        lines.append(f"上次错误：{str(last['error_text'])[:200]}")
    advice = failure_advice(
        str(problem.get("failed_stage") or ""),
        str(last["error_text"]) if last is not None else "",
    )
    if advice:
        lines.append(f"建议：{advice}")
    lines.append("确认要重试请加 --force。")
    return "\n".join(lines)


def cmd_generate(args) -> int:
    import json as _json
    from .app import jobs
    from .app.enrich_service import add_language, enrich_problem
    from .store.db import get_db

    db = get_db()
    row = db.query_one("SELECT * FROM problems WHERE id=?", (int(args.problem_id),))
    if row is None:
        print(f"#{args.problem_id} 不存在")
        return 1
    problem = dict(row)
    refusal = _failed_guard(problem, bool(getattr(args, "force", False)))
    if refusal:
        print(refusal)
        return 1
    busy = jobs.running_job_error(db, int(args.problem_id))
    if busy:
        print(busy)
        return 1
    service = _service(args.budget, getattr(args, "token_cap", 0))
    report = service.prepare(int(args.problem_id))
    print(_json.dumps(report.to_dict(), ensure_ascii=False, indent=1))
    # prepare 会补 lang_available；必须重读，否则补语言步骤拿的是旧行（lang_available 为空 → 直接跳过）
    problem = dict(db.query_one("SELECT * FROM problems WHERE id=?", (int(args.problem_id),)) or problem)
    if not report.ok:
        from .app.prep_rules import failure_advice

        advice = failure_advice(report.failed_stage, report.error)
        if advice:
            print(f"建议：{advice}")
        return 1
    db.execute("UPDATE problems SET status='prepared', failed_stage=NULL WHERE id=?", (problem["id"],))
    post = _service(args.budget)
    # 记账口径：不 prepare 也必须先绑题 + 装 recorder，否则补语言/补解析的调用不落库
    post.use_problem(int(problem["id"]))
    available = _parse_lang_list(problem.get("lang_available"))
    spec = _json.loads(problem.get("spec_json") or "{}")
    have = {r["lang"] for r in db.query_all(
        "SELECT DISTINCT lang FROM reference_solutions WHERE problem_id=? AND verified=1", (problem["id"],))}
    for lang in available:
        if lang not in have:
            try:
                print(add_language(post, problem, spec, lang))
            except Exception as exc:  # noqa: BLE001
                print(f"补 {lang} 失败：{str(exc)[:120]}")
    try:
        print(enrich_problem(post, problem, spec))
    except Exception as exc:  # noqa: BLE001
        print(f"补解析失败：{str(exc)[:120]}")
    return 0


def cmd_enrich(args) -> int:
    import json
    from .app.enrich_service import enrich_problem
    targets = _targets(args)
    if not targets:
        print("没有可处理的题目")
        # `--all` 下“没事可做”不是失败（批量脚本/CI 不应误判）；显式题号全不存在才算失败
        return 0 if getattr(args, "all", False) else 1
    for problem in targets:
        spec = json.loads(problem.get("spec_json") or "{}")
        try:
            service = _service(args.budget)
            service.use_problem(int(problem["id"]))
            result = enrich_problem(service, problem, spec)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "reason": str(exc)[:160]}
        print(f"#{problem['id']} {problem['title']}: {result}")
    return 0


def cmd_add_lang(args) -> int:
    import json
    from .app.enrich_service import add_language
    targets = _targets(args)
    if not targets:
        print("没有可处理的题目")
        return 0 if getattr(args, "all", False) else 1
    for problem in targets:
        spec = json.loads(problem.get("spec_json") or "{}")
        try:
            service = _service(args.budget)
            service.use_problem(int(problem["id"]))
            result = add_language(service, problem, spec, args.lang)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "reason": str(exc)[:160]}
        print(f"#{problem['id']} {problem['title']}: {result}")
    return 0


def cmd_desktop(args: argparse.Namespace) -> int:
    from .desktop.app import main as desktop_main
    return desktop_main()


def cmd_ui(args: argparse.Namespace) -> int:
    from .web.server import serve
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    db = _db_from_args(args)
    report = ingest_file(Path(args.file), db=db, source=args.source)
    _print_report(report)
    return 0 if report.problems else 1


def cmd_ingest_pdf(args: argparse.Namespace) -> int:
    """整卷 PDF：LLM 只标边界，题面与样例由代码从原文切片。"""
    from .app.ingest_pdf import ingest_pdf_file
    from .app.prep_store import PrepStore
    from .llm.client import LlmClient

    db = _db_from_args(args)
    client = LlmClient(verbose=False)
    client.recorder = PrepStore(db).record_llm_call
    result = ingest_pdf_file(Path(args.file), db=db, client=client)
    payload = result.to_dict()
    print(f"PDF: {payload['path']}")
    for item in payload["imported"]:
        flag = "新建" if item["created"] else "已存在"
        print(f"  #{item['id']} {item['title']}（{item['source_id']}）样例 {item['samples']} 组  {flag}")
    for item in payload["skipped"]:
        print(f"  跳过 {item['title']}：{item['reason']}")
    for warn in payload["warnings"][:10]:
        print(f"  ! {warn}")
    return 0 if payload["imported"] else 1


def cmd_label_approaches(args: argparse.Namespace) -> int:
    """给空白解法标签的题反推算法名，回填参考解 / analysis / 提示分支。"""
    from .app.approach_label import infer_approach_name, pick_code, relabel_problem
    from .store.db import get_db

    db = get_db()
    if args.problem_id:
        ids = [int(x) for x in args.problem_id]
    else:
        ids = [
            int(row["problem_id"])
            for row in db.query_all(
                "SELECT DISTINCT problem_id FROM reference_solutions"
                " WHERE coalesce(trim(approach),'')='' ORDER BY problem_id"
            )
        ]
    if not ids:
        print("没有需要补标签的题")
        return 0
    failed = 0
    for pid in ids:
        problem = db.query_one("SELECT * FROM problems WHERE id=?", (pid,))
        rows = db.query_all(
            "SELECT lang, role, code FROM reference_solutions WHERE problem_id=?"
            " ORDER BY is_primary DESC, role, lang",
            (pid,),
        )
        if problem is not None:
            problem = dict(problem)
        if problem is None or not rows:
            print(f"#{pid} 跳过：没有参考解")
            failed += 1
            continue
        l2 = db.query_one(
            "SELECT content FROM hints WHERE problem_id=? AND level=2 AND is_active=1"
            " ORDER BY id DESC LIMIT 1",
            (pid,),
        )
        service = _service(args.budget)
        service.use_problem(pid)
        name = infer_approach_name(service, problem, pick_code(rows), l2["content"] if l2 else "")
        if not name:
            print(f"#{pid} 失败：模型没有给出可用的算法名")
            failed += 1
            continue
        counts = relabel_problem(db, pid, name)
        print(f"#{pid} -> {name}  " + " ".join(f"{k}={v}" for k, v in counts.items()))
    return 0 if failed == 0 else 1


def cmd_hints(args: argparse.Namespace) -> int:
    """只重生成提示（复用已落库的 spec / 解法），不重跑数据与参考解。"""
    import json as _json
    from .app.prep_models import RefArtifact
    from .store.db import get_db

    db = get_db()
    row = db.query_one("SELECT * FROM problems WHERE id=?", (int(args.problem_id),))
    if row is None:
        print(f"#{args.problem_id} 不存在")
        return 1
    problem = dict(row)
    spec = _json.loads(problem.get("spec_json") or "{}")
    refs = {}
    for sol in db.query_all(
        "SELECT lang, role, code, verified, verification_level, approach, complexity,"
        " samples_expected FROM reference_solutions WHERE problem_id=?",
        (int(args.problem_id),),
    ):
        refs[f"{sol['lang']}/{sol['role']}"] = RefArtifact(
            lang=sol["lang"], role=sol["role"], code=sol["code"],
            verified=int(sol["verified"] or 0), verification_level=sol["verification_level"] or "",
            approach=sol["approach"] or "", complexity=sol["complexity"] or "",
            samples_total=int(sol["samples_expected"] or 0),
        )
    service = _service(args.budget)
    service.use_problem(int(args.problem_id))
    summary, detail = service._stage_hints(problem, spec, refs)
    print(f"#{args.problem_id} 提示已重生成：{detail}")
    print(_json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    db = _db_from_args(args)
    rows = db.query_all(
        "SELECT p.id, p.title, p.source_id, p.status, p.tier,"
        " (SELECT COUNT(*) FROM test_cases t WHERE t.problem_id = p.id) AS cases,"
        " (SELECT COUNT(*) FROM test_cases t WHERE t.problem_id = p.id AND t.layer='sample') AS samples"
        " FROM problems p ORDER BY p.id"
    )
    if not rows:
        print("题库为空（先跑 ingest）")
        return 0
    print(f"共 {len(rows)} 道题")
    print(f"{'id':>4}  {'status':<10} {'tier':<4} {'样例':>4} {'数据':>4}  标题")
    for row in rows:
        print(
            f"{row['id']:>4}  {row['status']:<10} {str(row['tier'] or '-'):<4} "
            f"{row['samples']:>4} {row['cases']:>4}  {row['title']}（{row['source_id']}）"
        )
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    from .app import jobs
    from .app.prep_service import PrepService

    db = _db_from_args(args)
    if args.all:
        return _cmd_prepare_all(args, db)
    if not args.problem_id:
        print("错误：需要 --problem-id 或 --all", file=sys.stderr)
        return 1
    row = db.query_one("SELECT * FROM problems WHERE id=?", (int(args.problem_id),))
    if row is None:
        print(f"#{args.problem_id} 不存在")
        return 1
    refusal = _failed_guard(dict(row), bool(getattr(args, "force", False)))
    if refusal:
        print(refusal)
        return 1
    busy = jobs.running_job_error(db, int(args.problem_id))
    if busy:
        print(busy)
        return 1
    service = PrepService(db=db, max_calls=args.budget, verbose=not args.quiet)
    report = service.prepare(int(args.problem_id))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if not report.ok:
        from .app.prep_rules import failure_advice

        advice = failure_advice(report.failed_stage, report.error)
        if advice:
            print(f"建议：{advice}")
    return 0 if report.ok else 1


def cmd_delete(args: argparse.Namespace) -> int:
    from .app import problems_admin
    from .store.db import get_db

    db = get_db()
    row = db.query_one("SELECT id, title, status FROM problems WHERE id=?", (int(args.problem_id),))
    if row is None:
        print(f"#{args.problem_id} 不存在")
        return 1
    if not args.yes:
        print(
            f"即将删除 #{row['id']} {row['title']}（status={row['status']}）："
            "参考解、测试数据、提示、练习记录、导出历史会一并删除，不可恢复。\n"
            "确认请加 --yes。"
        )
        return 1
    result = problems_admin.delete_problem(int(args.problem_id), db=db)
    if result.get("error"):
        print(result["error"])
        return 1
    print(f"已删除 #{result['id']} {result['title']}")
    print("清理明细:", json.dumps(result["removed"], ensure_ascii=False))
    return 0


def cmd_spec_set(args: argparse.Namespace) -> int:
    from .app import problems_admin
    from .store.db import get_db

    try:
        text = Path(args.file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读不到 spec 文件：{exc}")
        return 1
    try:
        payload = problems_admin.load_spec_file(text)
    except ValueError as exc:
        print(exc)
        return 1
    result = problems_admin.set_spec(int(args.problem_id), payload, db=get_db())
    if result.get("error"):
        print(result["error"])
        return 1
    print(
        f"#{result['id']} spec 已落库（grammar {result['grammar_nodes']} 节点，"
        f"{result['samples_replayed']} 组样例回放通过）；状态已复位为 draft，可重新 generate"
    )
    return 0


def cmd_adversarial(args: argparse.Namespace) -> int:
    from .app.adversarial import generate_adversarial

    try:
        code = Path(args.code_file).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读不到代码文件：{exc}")
        return 1
    result = generate_adversarial(
        {
            "problem_id": int(args.problem_id),
            "lang": args.lang,
            "code": code,
            "count": int(args.count),
            "use_llm": not args.no_llm,
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("error") else 1


def _cmd_prepare_all(args: argparse.Namespace, db) -> int:
    """批量预生成：单题失败不中断、全局预算熔断、输出汇总表。"""
    from .app.prep_batch import run_batch

    def _progress(row) -> None:
        print(
            f"[{row.status}] id={row.problem_id} {row.title} calls={row.calls} "
            f"failed_stage={row.failed_stage or '-'} category={row.category or '-'}",
            file=sys.stderr,
        )

    report = run_batch(
        db=db,
        problem_ids=args.problem_id_list,
        include_prepared=args.force,
        budget_per=args.budget_per,
        budget_total=args.budget_total,
        timeout_sec=args.timeout_sec,
        verbose=not args.quiet,
        progress=_progress if not args.quiet else None,
    )
    print("=== 批量预生成汇总 ===")
    print(report.render_table())
    print(
        f"\n成功 {report.ok_count} | 失败 {report.failed_count} | 因预算跳过 {report.skipped_count} "
        f"| 预算耗竭={report.budget_exhausted} | 总耗时 {report.elapsed_ms / 1000:.1f}s"
    )
    print("失败归类:", json.dumps(report.category_counts(), ensure_ascii=False))
    print("预算:", json.dumps(report.budget, ensure_ascii=False))
    injected = [row for row in report.rows if row.injection_followed]
    if injected:
        print("!! 服从题面注入的题（需人工复核）:")
        for row in injected:
            print(f"   id={row.problem_id} {row.title} 命中={row.injection_hits}")
    else:
        print("题面注入：无生成代码命中被指定标识符（injection_followed=0）")
    empty_layers = [row for row in report.rows if row.status == "prepared" and not row.layers]
    if empty_layers:
        print("!! prepared 但没有数据层的题:", [row.problem_id for row in empty_layers])
    print(json.dumps(report.to_dict(), ensure_ascii=False))
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    db = _db_from_args(args)
    problem_id = int(args.problem_id)
    problem = db.query_one("SELECT * FROM problems WHERE id = ?", (problem_id,))
    if problem is None:
        print(f"题目 {problem_id} 不存在")
        return 1
    print(f"[{problem['id']}] {problem['title']}  status={problem['status']} tier={problem['tier']}")
    print(f"failed_stage={problem['failed_stage']} lang_available={problem['lang_available']}")
    spec = json.loads(problem["spec_json"] or "{}")
    print("spec:", json.dumps({k: v for k, v in spec.items() if k != "samples"}, ensure_ascii=False)[:1500])
    sql = "SELECT layer, COUNT(*) AS n, GROUP_CONCAT(DISTINCT trust_level) AS trust FROM test_cases"
    params: List[object] = [problem_id]
    if args.layer:
        sql += " WHERE problem_id = ? AND layer = ?"
        params.append(args.layer)
    else:
        sql += " WHERE problem_id = ?"
    sql += " GROUP BY layer ORDER BY layer"
    print("数据分层:")
    for row in db.query_all(sql, tuple(params)):
        print(f"  {row['layer']:<12} {row['n']:>4} 条  trust={row['trust']}")
    print("参考解:")
    for row in db.query_all(
        "SELECT lang, role, verified, verification_level, length(code) AS n FROM reference_solutions"
        " WHERE problem_id = ? ORDER BY role, lang",
        (problem_id,),
    ):
        print(
            f"  {row['role']:<9} {row['lang']:<7} verified={row['verified']} "
            f"level={row['verification_level']} 代码 {row['n']} 字"
        )
    print("提示:")
    for row in db.query_all(
        "SELECT level, quality_flag, length(content) AS n FROM hints WHERE problem_id = ?"
        " ORDER BY level",
        (problem_id,),
    ):
        print(f"  L{row['level']} flag={row['quality_flag']} {row['n']} 字")
    if args.layer:
        print(f"== {args.layer} 层明细（最多 5 条）==")
        for row in db.query_all(
            "SELECT id, seed, size_class, length(input_data) AS n, source_of_truth, trust_level"
            " FROM test_cases WHERE problem_id = ? AND layer = ? ORDER BY ordinal LIMIT 5",
            (problem_id, args.layer),
        ):
            print(f"  #{row['id']} seed={row['seed']} size={row['size_class']} "
                  f"{row['n']} 字 {row['source_of_truth']}/{row['trust_level']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m autocode.cli", description="autoCode CLI")
    parser.add_argument("--db", default="", help="SQLite 路径（默认 data/autocode.db）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_generate = sub.add_parser("generate", help="导入后一键生成：预生成 + 补语言 + 解析")
    p_generate.add_argument("--problem-id", type=int, required=True)
    p_generate.add_argument("--budget", type=int, default=30)
    p_generate.add_argument("--token-cap", type=int, default=0, help="单题 token 上限（默认 150000）")
    p_generate.add_argument("--force", action="store_true", help="上次失败仍要重跑时显式加")
    p_generate.add_argument("--quiet", action="store_true")
    p_generate.set_defaults(func=cmd_generate)

    p_delete = sub.add_parser("delete", help="删除整题（含参考解/数据/提示/练习记录，不可恢复）")
    p_delete.add_argument("--problem-id", type=int, required=True)
    p_delete.add_argument("--yes", action="store_true", help="确认删除（不加则只打印预览）")
    p_delete.set_defaults(func=cmd_delete)

    p_spec_set = sub.add_parser("spec-set", help="人工注入 spec JSON（过归一化+样例回放门）")
    p_spec_set.add_argument("--problem-id", type=int, required=True)
    p_spec_set.add_argument("--file", required=True, help="spec JSON 文件路径（允许 ```json 围栏）")
    p_spec_set.set_defaults(func=cmd_spec_set)

    p_adv = sub.add_parser("adversarial", help="针对学生错解生成对抗性测试数据（入库 adversarial 层）")
    p_adv.add_argument("--problem-id", type=int, required=True)
    p_adv.add_argument("--code-file", required=True, help="学生错解代码文件路径")
    p_adv.add_argument("--lang", default="python", choices=["c", "cpp", "python"])
    p_adv.add_argument("--count", type=int, default=8, help="最多生成几条（默认 8）")
    p_adv.add_argument("--no-llm", action="store_true", help="只用本地变异，不调模型")
    p_adv.set_defaults(func=cmd_adversarial)

    p_enrich = sub.add_parser("enrich", help="补按方法解析与逐行代码解析")
    p_enrich.add_argument("--problem-id", type=int, action="append")
    p_enrich.add_argument("--all", action="store_true")
    p_enrich.add_argument("--budget", type=int, default=8)
    p_enrich.set_defaults(func=cmd_enrich)

    p_lang = sub.add_parser("add-lang", help="补生成某语言答案")
    p_lang.add_argument("--lang", required=True, choices=["c", "cpp", "python"])
    p_lang.add_argument("--problem-id", type=int, action="append")
    p_lang.add_argument("--all", action="store_true")
    p_lang.add_argument("--budget", type=int, default=8)
    p_lang.set_defaults(func=cmd_add_lang)

    p_desktop = sub.add_parser("desktop", help="以桌面窗口启动（不需要 HTTP 端口）")
    p_desktop.set_defaults(func=cmd_desktop)

    p_ui = sub.add_parser("ui", help="启动本地题库工作台")
    p_ui.add_argument("--port", type=int, default=8765)
    p_ui.add_argument("--no-browser", action="store_true")
    p_ui.set_defaults(func=cmd_ui)

    p_ingest = sub.add_parser("ingest", help="导入 CSP 题面 md")
    p_ingest.add_argument("file")
    p_ingest.add_argument("--source", default="CSP")
    p_ingest.set_defaults(func=cmd_ingest)

    p_label = sub.add_parser("label-approaches", help="给空白解法标签的题反推算法名")
    p_label.add_argument("--problem-id", type=int, action="append")
    p_label.add_argument("--budget", type=int, default=4)
    p_label.set_defaults(func=cmd_label_approaches)

    p_hints = sub.add_parser("hints", help="只重生成提示（复用已有 spec/参考解）")
    p_hints.add_argument("--problem-id", type=int, required=True)
    p_hints.add_argument("--budget", type=int, default=8)
    p_hints.set_defaults(func=cmd_hints)

    p_ingest_pdf = sub.add_parser("ingest-pdf", help="整卷 PDF：LLM 标边界 → 原文切片入库")
    p_ingest_pdf.add_argument("file")
    p_ingest_pdf.set_defaults(func=cmd_ingest_pdf)

    p_list = sub.add_parser("list", help="题库概览")
    p_list.set_defaults(func=cmd_list)

    p_prep = sub.add_parser("prepare", help="预生成流水线")
    p_prep.add_argument("--problem-id", type=int, default=0, help="单题 id（与 --all 互斥）")
    p_prep.add_argument("--problem-id-list", type=int, nargs="+", default=None,
                        help="配合 --all 指定子集")
    p_prep.add_argument("--all", action="store_true", help="批量跑所有 draft/failed 的题")
    p_prep.add_argument("--force", action="store_true", help="配合 --all：连已 prepared 的题也重跑；单题：上次失败仍要重跑")
    p_prep.add_argument("--budget", type=int, default=40, help="单题 LLM 调用上限（默认 40）")
    p_prep.add_argument("--budget-per", type=int, default=20, help="--all 时单题调用上限（默认 20）")
    p_prep.add_argument("--budget-total", type=int, default=250, help="--all 时全局调用上限（默认 250）")
    p_prep.add_argument("--timeout-sec", type=float, default=180.0, help="--all 时单题墙钟上限（默认 180s）")
    p_prep.add_argument("--quiet", action="store_true", help="不打印阶段进度")
    p_prep.set_defaults(func=cmd_prepare)

    p_show = sub.add_parser("show", help="查看入库结果")
    p_show.add_argument("--problem-id", type=int, required=True)
    p_show.add_argument("--layer", default="")
    p_show.set_defaults(func=cmd_show)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
