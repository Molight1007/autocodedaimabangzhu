"""autoCode 命令行入口：`python -m autocode.cli <子命令>`。

已实现子命令：
- `ingest <file.md> [--source CSP] [--db PATH]`  题面入册（样例归一 + 注入打标）
- `list [--db PATH]`                             题库概览（每题样例/数据条数）
- `prepare --problem-id N [--budget 40]`         预生成流水线（LLM + 执行引擎）
- `show --problem-id N [--layer stress]`          查题（spec/参考解/数据/提示）

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


def _service(max_calls: int):
    from .app.prep_service import PrepService
    from .llm.client import LlmClient
    from .store.db import get_db
    return PrepService(db=get_db(), client=LlmClient(verbose=False), max_calls=max_calls, verbose=False)


def _targets(args):
    from .store.db import get_db
    db = get_db()
    if getattr(args, "all", False):
        return [dict(r) for r in db.query_all("SELECT * FROM problems WHERE status='prepared' ORDER BY id")]
    return [dict(db.query_one("SELECT * FROM problems WHERE id=?", (pid,))) for pid in (args.problem_id or [])]


def cmd_generate(args) -> int:
    import json as _json
    from .app.enrich_service import add_language, enrich_problem
    from .store.db import get_db

    db = get_db()
    problem = dict(db.query_one("SELECT * FROM problems WHERE id=?", (int(args.problem_id),)))
    service = _service(args.budget)
    report = service.prepare(int(args.problem_id))
    print(_json.dumps(report.to_dict(), ensure_ascii=False)[:400])
    if not report.ok:
        return 1
    db.execute("UPDATE problems SET status='prepared', failed_stage=NULL WHERE id=?", (problem["id"],))
    post = _service(args.budget)
    available = _json.loads(problem.get("lang_available") or "[]")
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
    for problem in _targets(args):
        spec = json.loads(problem.get("spec_json") or "{}")
        try:
            result = enrich_problem(_service(args.budget), problem, spec)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "reason": str(exc)[:160]}
        print(f"#{problem['id']} {problem['title']}: {result}")
    return 0


def cmd_add_lang(args) -> int:
    import json
    from .app.enrich_service import add_language
    for problem in _targets(args):
        spec = json.loads(problem.get("spec_json") or "{}")
        try:
            result = add_language(_service(args.budget), problem, spec, args.lang)
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
    from .app.prep_service import PrepService

    db = _db_from_args(args)
    if args.all:
        return _cmd_prepare_all(args, db)
    if not args.problem_id:
        print("错误：需要 --problem-id 或 --all", file=sys.stderr)
        return 1
    service = PrepService(db=db, max_calls=args.budget, verbose=not args.quiet)
    report = service.prepare(int(args.problem_id))
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 1


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
    p_generate.add_argument("--quiet", action="store_true")
    p_generate.set_defaults(func=cmd_generate)

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

    p_list = sub.add_parser("list", help="题库概览")
    p_list.set_defaults(func=cmd_list)

    p_prep = sub.add_parser("prepare", help="预生成流水线")
    p_prep.add_argument("--problem-id", type=int, default=0, help="单题 id（与 --all 互斥）")
    p_prep.add_argument("--problem-id-list", type=int, nargs="+", default=None,
                        help="配合 --all 指定子集")
    p_prep.add_argument("--all", action="store_true", help="批量跑所有 draft/failed 的题")
    p_prep.add_argument("--force", action="store_true", help="配合 --all：连已 prepared 的题也重跑")
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
