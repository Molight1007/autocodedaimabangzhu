"""任务子进程：一次只跑一条流水线，终态由本进程直接写 `jobs`。"""

from __future__ import annotations

import argparse
import sys
import traceback
from typing import Any, Dict, List, Optional

from . import config, jobs
from .errors import StageFailure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autocode.slim2.worker")
    sub = parser.add_subparsers(dest="command", required=True)

    dataset = sub.add_parser("dataset")
    dataset.add_argument("--job-id", type=int, required=True)
    dataset.add_argument("--problem-id", type=int, required=True)
    dataset.add_argument("--budget", type=int, default=config.DEFAULT_BUDGET_CALLS)

    adversarial = sub.add_parser("adversarial")
    adversarial.add_argument("--job-id", type=int, required=True)
    adversarial.add_argument("--problem-id", type=int, required=True)
    adversarial.add_argument("--submission-id", type=int, required=True)
    adversarial.add_argument("--budget", type=int, default=config.DEFAULT_BUDGET_CALLS)
    return parser


def _run(db: Any, args: argparse.Namespace) -> Dict[str, Any]:
    from . import user_llm
    from .llm_stage import StageRunner

    # 任务由谁发起，就用谁的 Key：job 行里存了登录名，这里按名字取本人凭据。
    # 没有 Key 就直接失败，绝不回落到别人或全局的 Key。
    row = db.query_one("SELECT user FROM jobs WHERE id=?", (int(args.job_id),))
    job_user = str((row["user"] if row is not None else "") or "")
    llm_config = user_llm.config_for(db, job_user)
    if llm_config is None:
        raise StageFailure(
            "config",
            f"账号 {job_user or '（空）'} 还没有配置自己的 API Key，"
            "请在页面右上角「模型设置」里填写后重试",
        )

    runner = StageRunner(
        db, problem_id=int(args.problem_id), budget=int(args.budget),
        llm_config=llm_config, user=job_user,
    )
    progress = lambda stage: jobs.set_stage(db, int(args.job_id), stage)  # noqa: E731
    if args.command == "dataset":
        from .dataset import run_dataset

        return run_dataset(db, int(args.problem_id), runner, progress=progress)

    from .adversarial import run_adversarial

    return run_adversarial(
        db, int(args.problem_id), int(args.submission_id), runner, progress=progress
    )


def _reset_problem(db: Any, args: argparse.Namespace) -> None:
    """数据生成失败时把 problems.status 从 generating 拉回来，别让前端永远转圈。"""
    if args.command != "dataset":
        return
    row = db.query_one(
        "SELECT dataset_version FROM problems WHERE id=?", (int(args.problem_id),)
    )
    if row is None:
        return
    status = "ready" if int(row["dataset_version"] or 0) > 0 else "failed"
    with db.transaction() as conn:
        conn.execute("UPDATE problems SET status=? WHERE id=?", (status, int(args.problem_id)))


def main(argv: Optional[List[str]] = None) -> int:
    args = _parser().parse_args(argv)
    config.apply_env()
    from .db import get_db

    db = get_db()
    try:
        result = _run(db, args)
    except StageFailure as exc:
        jobs.finish(db, int(args.job_id), "failed", exc.message, exc.stage)
        _reset_problem(db, args)
        return 1
    except Exception as exc:  # noqa: BLE001 - 任何异常都必须落终态，不能留 running
        traceback.print_exc()
        jobs.finish(
            db, int(args.job_id), "failed",
            f"{type(exc).__name__}: {exc}"[:2000], "process",
        )
        _reset_problem(db, args)
        return 1
    jobs.finish(db, int(args.job_id), "done", result=result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
