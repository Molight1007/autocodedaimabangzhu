"""生成任务的生命周期管理（契约 §4.4，领域层 / 确定性代码）。

为什么单独成模块：任务启动、状态对账、僵尸清理都是"代码必须确定"的部分（契约 §2），
且 `web/api.py` 受 800 行门禁约束。这里负责：

1. 启动子进程 → 写 `generation_jobs(status='running')`；
2. 查询状态 → DB 说 running 时用**进程现实**对账（Popen.poll / psutil.pid_exists / 2h 超时）；
3. 对账落 failed 前**先杀进程**——只改 DB 不杀进程会导致"UI 说失败、进程继续烧 token"，
   紧接着 `start_generate` 又会起第二个进程。

安全约束：绝不杀当前服务进程自己；PID 复用保护（只有确认是 python 进程才杀）。
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

STALE_JOB_HOURS = 2

# 正在运行的生成子进程（problem_id → Popen）；DB 里的 generation_jobs 是持久化记录
RUNNING: Dict[int, subprocess.Popen] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _one(db: Any, sql: str, params: tuple = ()) -> Any:
    rows = db.query_all(sql, params)
    return rows[0] if rows else None


def parse_ts(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def job_stale(started_at: Any, *, hours: int = STALE_JOB_HOURS) -> bool:
    started = parse_ts(started_at)
    if started is None:
        return False
    return (datetime.now(timezone.utc) - started).total_seconds() > hours * 3600


def process_alive(os_pid: Any) -> bool:
    """判断子进程是否还活着。

    用 psutil.pid_exists 而不是 `os.kill(pid, 0)`：Windows 上后者会调用
    TerminateProcess，等于"查活把进程杀了"。psutil 不存在时保守返回 True。
    """
    try:
        pid = int(os_pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return True
    try:
        return bool(psutil.pid_exists(pid))
    except Exception:  # noqa: BLE001
        return True


def kill_job_process(problem_id: int, os_pid: Any) -> str:
    """尽力终止僵尸任务进程，返回做了什么（写进 job.error 便于追溯）。"""
    proc = RUNNING.pop(problem_id, None)
    if proc is not None:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:  # noqa: BLE001 - 超时后补一刀
                    proc.kill()
                return "，已终止遗留进程"
        except Exception:  # noqa: BLE001
            return ""
        return ""
    if not process_alive(os_pid):
        return ""
    try:
        target_pid = int(os_pid)
    except (TypeError, ValueError):
        return ""
    if target_pid == os.getpid():
        return "，跳过终止（os_pid 指向当前服务进程）"
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return ""
    try:
        target = psutil.Process(target_pid)
        if "python" not in (target.name() or "").lower():
            return "，跳过终止（pid 已被非 python 进程复用）"
        for child in target.children(recursive=True):
            child.kill()
        target.kill()
        return "，已终止遗留进程"
    except Exception:  # noqa: BLE001
        return ""


def close_job(db: Any, job_id: int, status: str, error: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE generation_jobs SET status=?, error=?, finished_at=? WHERE id=?",
            (status, error or "", _now(), job_id),
        )


def _finalize_from_status(db: Any, problem_id: int, job_id: int) -> Any:
    problem = _one(db, "SELECT status, failed_stage FROM problems WHERE id=?", (problem_id,))
    ok = bool(problem) and problem["status"] == "prepared"
    status = "done" if ok else "failed"
    error = "" if ok else str((problem["failed_stage"] if problem else "") or "生成未完成")
    close_job(db, job_id, status, error)
    return _one(db, "SELECT * FROM generation_jobs WHERE id=?", (job_id,))


def reconcile_from_problem(db: Any, problem_id: int, row: Any) -> Any:
    problem = _one(db, "SELECT status, failed_stage FROM problems WHERE id=?", (problem_id,))
    if not problem:
        return row
    if problem["status"] == "prepared":
        close_job(db, int(row["id"]), "done", "")
        return _one(db, "SELECT * FROM generation_jobs WHERE id=?", (row["id"],))
    if problem["status"] == "failed":
        close_job(db, int(row["id"]), "failed", str(problem["failed_stage"] or "failed"))
        return _one(db, "SELECT * FROM generation_jobs WHERE id=?", (row["id"],))
    return row


def reconcile_running_job(db: Any, problem_id: int, row: Any) -> Any:
    """把"DB 说 running"和"现实"对齐，返回最新的 job 行（落 failed 前先杀进程）。"""
    job_id = int(row["id"])
    proc = RUNNING.get(problem_id)
    if proc is not None:
        if proc.poll() is not None:
            RUNNING.pop(problem_id, None)
            return _finalize_from_status(db, problem_id, job_id)
        if job_stale(row["started_at"]):
            detail = kill_job_process(problem_id, row["os_pid"])
            close_job(db, job_id, "failed", f"任务超过 {STALE_JOB_HOURS} 小时未收敛{detail}")
            return _one(db, "SELECT * FROM generation_jobs WHERE id=?", (job_id,))
        return row
    if not process_alive(row["os_pid"]):
        close_job(db, job_id, "failed", "进程已退出")
        return _one(db, "SELECT * FROM generation_jobs WHERE id=?", (job_id,))
    if job_stale(row["started_at"]):
        detail = kill_job_process(problem_id, row["os_pid"])
        close_job(db, job_id, "failed", f"任务超过 {STALE_JOB_HOURS} 小时未收敛{detail}")
        return _one(db, "SELECT * FROM generation_jobs WHERE id=?", (job_id,))
    return reconcile_from_problem(db, problem_id, row)


def job_status(db: Any, problem_id: int) -> Dict[str, Any]:
    """查任务状态；DB 仍 running 时用进程现实对账并落终态。"""
    row = _one(
        db,
        "SELECT * FROM generation_jobs WHERE problem_id=? ORDER BY id DESC LIMIT 1",
        (problem_id,),
    )
    if not row:
        return {"status": "none", "started_at": "", "finished_at": "", "error": ""}
    if row["status"] == "running":
        row = reconcile_running_job(db, problem_id, row)
    return {
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"] or "",
        "error": row["error"] or "",
        "budget": row["budget"],
        "os_pid": row["os_pid"],
        "job_id": row["id"],
    }


def start_generate(
    db: Any, payload: Dict[str, Any], *, root: Path, log_dir: Path
) -> Dict[str, Any]:
    """启动生成子进程（先对账历史 running，仍活着则拒绝，防同题双进程）。"""
    problem_id = int(payload.get("id") or 0)
    for key, proc in list(RUNNING.items()):
        if proc.poll() is not None:
            RUNNING.pop(key, None)
    if not problem_id or problem_id in RUNNING:
        return {"error": "无效或正在生成"}
    if not _one(db, "SELECT id FROM problems WHERE id=?", (problem_id,)):
        return {"error": "题目不存在"}
    for existing in db.query_all(
        "SELECT * FROM generation_jobs WHERE problem_id=? AND status='running'", (problem_id,)
    ):
        if reconcile_running_job(db, problem_id, existing)["status"] == "running":
            return {"error": "该题已有生成任务在运行"}
    budget = int(payload.get("budget") or 30)
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"generate-{problem_id}.log", "a", encoding="utf-8")  # noqa: SIM115
    try:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "autocode.cli", "generate",
                "--problem-id", str(problem_id), "--budget", str(budget), "--quiet",
            ],
            cwd=str(root), stdout=log, stderr=log,
        )
    finally:
        log.close()  # 句柄交给子进程继承即可，父进程不长期占用
    RUNNING[problem_id] = proc
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO generation_jobs(problem_id, status, budget, os_pid, error, started_at)"
            " VALUES(?, 'running', ?, ?, '', ?)",
            (problem_id, budget, proc.pid, _now()),
        )
        job_id = cursor.lastrowid
    return {"started": True, "pid": problem_id, "os_pid": proc.pid, "job_id": job_id}
