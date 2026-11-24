"""生成任务的生命周期管理（契约 §4.4，领域层 / 确定性代码）。

为什么单独成模块：任务启动、状态对账、僵尸清理都是"代码必须确定"的部分（契约 §2），
且 `web/api.py` 受 800 行门禁约束。这里负责：

1. **排队**：提交即入队（`generation_jobs.status='queued'`），一次只跑一个；
   前一个落终态后，常驻调度器在 1 秒内接上下一个；
2. 启动子进程 → 写 `generation_jobs(status='running')` 与真实 pid；
3. 查询状态 → DB 说 running 时用**进程现实**对账（Popen.poll / psutil.pid_exists / 2h 超时）；
4. 对账落 failed 前**先杀进程**——只改 DB 不杀进程会导致"UI 说失败、进程继续烧 token"。

队列存在数据库里，服务重启不丢；重启时正在跑的会被标为中断，排队的继续。
调度只由常驻进程做：worker 跑完就退出，若让它顺手拉起下一个，进程会变孤儿。

安全约束：绝不杀当前服务进程自己；PID 复用保护（只有确认是 python 进程才杀）。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

STALE_JOB_HOURS = 2

# 正在运行的生成子进程（problem_id → Popen）；DB 里的 generation_jobs 是持久化记录
RUNNING: Dict[int, subprocess.Popen] = {}
_LOCK = threading.Lock()
# 调度与对账互斥：避免"刚抢到任务、pid 还没写"的瞬间被状态查询判成进程已退出
_DISPATCH_LOCK = threading.RLock()
_SCHEDULER: Optional[threading.Thread] = None
_SCHEDULER_STOP = threading.Event()
# 单独留一层，测试替换它即可，不去 patch 标准库的 subprocess.Popen
_popen = subprocess.Popen


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
    with _LOCK:
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
            (status, error or "", _now(), int(job_id)),
        )


def queue_ahead(db: Any, row: Any) -> int:
    """前面还有几个任务：正在跑的 + 更早排队的（只对排队中的任务有意义）。"""
    if row is None or str(row["status"]) != "queued":
        return 0
    job_id = int(row["id"])
    running = 1 if _one(
        db, "SELECT id FROM generation_jobs WHERE status='running' AND id<? LIMIT 1", (job_id,)
    ) else 0
    queued = int(
        db.query_scalar(
            "SELECT COUNT(*) FROM generation_jobs WHERE status='queued' AND id<?",
            (job_id,),
            0,
        )
    )
    return running + queued


def _active_job(db: Any, problem_id: int) -> Any:
    """同题已有排队或在跑的任务：防止连点产生两条任务、双份烧 token。"""
    return _one(
        db,
        "SELECT id FROM generation_jobs WHERE problem_id=? AND status IN ('queued','running')"
        " ORDER BY id DESC LIMIT 1",
        (int(problem_id),),
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
    with _DISPATCH_LOCK:
        return _reconcile_running_job(db, problem_id, row)


def _reconcile_running_job(db: Any, problem_id: int, row: Any) -> Any:
    job_id = int(row["id"])
    proc = RUNNING.get(problem_id)
    if proc is not None:
        if proc.poll() is not None:
            with _LOCK:
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


def _reap_all() -> None:
    """回收已经退出的子进程句柄（每跑一题一个，不回收会累积）。"""
    with _LOCK:
        items = list(RUNNING.items())
    for problem_id, proc in items:
        if proc.poll() is not None:
            with _LOCK:
                RUNNING.pop(problem_id, None)


def _reconcile_all(db: Any) -> None:
    for row in db.query_all("SELECT * FROM generation_jobs WHERE status='running'"):
        reconcile_running_job(db, int(row["problem_id"]), row)


def dispatch(db: Any, *, root: Path, log_dir: Path) -> bool:
    """有空位就按先来后到启动一个任务；返回本次是否起了进程。"""
    with _DISPATCH_LOCK:
        _reconcile_all(db)
        running = int(
            db.query_scalar("SELECT COUNT(*) FROM generation_jobs WHERE status='running'", (), 0)
        )
        if running >= 1:  # 本地工作台一次只跑一道题
            return False
        row = _one(
            db, "SELECT * FROM generation_jobs WHERE status='queued' ORDER BY id ASC LIMIT 1"
        )
        if row is None:
            return False
        with db.transaction() as conn:
            claimed = conn.execute(
                "UPDATE generation_jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                (_now(), int(row["id"])),
            ).rowcount
        if not claimed:
            return False
        return _spawn(
            db,
            problem_id=int(row["problem_id"]),
            budget=int(row["budget"] or 30),
            job_id=int(row["id"]),
            root=root,
            log_dir=log_dir,
        )


def _spawn(
    db: Any, *, problem_id: int, budget: int, job_id: int, root: Path, log_dir: Path
) -> bool:
    """按 job 行拉起生成子进程；参数从库里取，排队任务重启后也能照常开跑。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / f"generate-{problem_id}.log", "a", encoding="utf-8")  # noqa: SIM115
    try:
        proc = _popen(
            [
                sys.executable, "-m", "autocode.cli", "generate",
                "--problem-id", str(problem_id), "--budget", str(budget), "--quiet",
            ],
            cwd=str(root), stdout=log, stderr=log,
        )
    except OSError as exc:
        close_job(db, job_id, "failed", f"无法启动生成进程：{exc}")
        return False
    finally:
        try:
            log.close()  # 句柄交给子进程继承即可，父进程不长期占用
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        RUNNING[problem_id] = proc
    with db.transaction() as conn:
        conn.execute("UPDATE generation_jobs SET os_pid=? WHERE id=?", (proc.pid, job_id))
    return True


def job_status(db: Any, problem_id: int) -> Dict[str, Any]:
    """查任务状态；DB 仍 running 时用进程现实对账并落终态。"""
    row = _one(
        db,
        "SELECT * FROM generation_jobs WHERE problem_id=? ORDER BY id DESC LIMIT 1",
        (problem_id,),
    )
    if not row:
        return {
            "status": "none", "started_at": "", "finished_at": "", "error": "",
            "queued_at": "", "ahead": 0, "position": 0,
        }
    if row["status"] == "running":
        row = reconcile_running_job(db, problem_id, row)
    state = str(row["status"])
    ahead = queue_ahead(db, row) if state == "queued" else 0
    return {
        "status": state,
        "started_at": row["started_at"],
        "finished_at": row["finished_at"] or "",
        "error": row["error"] or "",
        "budget": row["budget"],
        "os_pid": row["os_pid"],
        "job_id": row["id"],
        "queued_at": row["queued_at"] or "",
        "ahead": ahead,
        "position": (ahead + 1) if state == "queued" else 0,
    }


def running_job_error(db: Any, problem_id: int) -> Optional[str]:
    """CLI 入口的并发护栏：该题若已有排队/运行中的任务，返回报错文案，否则 None。

    UI 路径由 `start_generate` 自带对账；CLI（generate/prepare）原先没有任何护栏，
    两端同时跑同一题会双份烧 token、互相覆盖中间产物。
    """
    for existing in db.query_all(
        "SELECT * FROM generation_jobs WHERE problem_id=? AND status IN ('queued','running')",
        (problem_id,),
    ):
        row = (
            reconcile_running_job(db, problem_id, existing)
            if str(existing["status"]) == "running"
            else existing
        )
        if row["status"] == "queued":
            return f"该题已有生成任务在排队（job #{row['id']}），等它结束或先取消后再试"
        if row["status"] == "running":
            return (
                f"该题已有生成任务在运行（job #{row['id']}，pid={row['os_pid']}），"
                "等它结束或先终止后再试"
            )
    return None


def start_generate(
    db: Any, payload: Dict[str, Any], *, root: Path, log_dir: Path
) -> Dict[str, Any]:
    """提交生成任务：先入队；空闲立刻开跑，否则排队等前一个结束。"""
    problem_id = int(payload.get("id") or 0)
    _reap_all()
    if not problem_id:
        return {"error": "无效或正在生成"}
    if not _one(db, "SELECT id FROM problems WHERE id=?", (problem_id,)):
        return {"error": "题目不存在"}

    budget = int(payload.get("budget") or 30)
    # 查重与入队同一把锁：两个页签同时点同一道题，否则会各插一条排队记录
    with _DISPATCH_LOCK:
        _reconcile_all(db)
        if _active_job(db, problem_id) is not None:
            return {"error": "该题已有生成任务在排队或运行"}

        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO generation_jobs(problem_id, status, budget, os_pid, error, queued_at,"
                " started_at) VALUES(?, 'queued', ?, 0, '', ?, '')",
                (problem_id, budget, _now()),
            )
            job_id = int(cursor.lastrowid)

    dispatch(db, root=root, log_dir=log_dir)
    row = _one(db, "SELECT * FROM generation_jobs WHERE id=?", (job_id,))
    if str(row["status"]) == "failed":
        return {"error": str(row["error"] or "任务启动失败"), "job_id": job_id}
    queued = str(row["status"]) == "queued"
    return {
        "started": True,
        "queued": queued,
        "position": (queue_ahead(db, row) + 1) if queued else 0,
        "job_id": job_id,
        "os_pid": int(row["os_pid"] or 0),
    }


# ── 调度线程 ────────────────────────────────────────────────────────────────
def ensure_scheduler(db: Any, *, root: Path, log_dir: Path, interval: float = 1.0) -> None:
    """跟着服务启动的调度线程；重复调用无副作用。测试里不调用它。"""
    global _SCHEDULER
    with _LOCK:
        if _SCHEDULER is not None and _SCHEDULER.is_alive():
            return
        _SCHEDULER_STOP.clear()
        _SCHEDULER = threading.Thread(
            target=_scheduler_loop,
            args=(db, root, log_dir, float(interval)),
            name="generation-scheduler",
            daemon=True,
        )
        _SCHEDULER.start()


def _scheduler_loop(db: Any, root: Path, log_dir: Path, interval: float) -> None:
    try:
        while not _SCHEDULER_STOP.wait(interval):
            try:
                dispatch(db, root=root, log_dir=log_dir)
            except Exception as exc:  # noqa: BLE001 - 调度线程不能因为一次异常就死
                print(f"生成任务调度器出错（不影响正在跑的任务）：{exc}", file=sys.stderr, flush=True)
    finally:
        db.close()  # 关掉本线程自己的连接，避免测试/退出时留下未关闭句柄


def stop_scheduler() -> None:
    """测试/退出用：停掉调度线程。"""
    global _SCHEDULER
    _SCHEDULER_STOP.set()
    with _LOCK:
        thread, _SCHEDULER = _SCHEDULER, None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
