"""任务：起子进程、写终态、崩溃对账、手动取消。

与 `slim/tasks.py` 的差别只有表名与参数传递方式；刻意不合并，
因为 v1 的 `slim_jobs` 还有前端在用，动它等于同时改两个版本。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import config

RUNNING: Dict[Tuple[str, int], subprocess.Popen] = {}
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _one(db: Any, sql: str, params: tuple = ()) -> Any:
    rows = db.query_all(sql, params)
    return rows[0] if rows else None


def _log_dir() -> Path:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    return config.LOG_DIR


def process_alive(pid: Any) -> bool:
    try:
        target = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if target <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(target))
    except ImportError:
        return True
    except Exception:  # noqa: BLE001
        return True


def kill_tree(pid: Any) -> str:
    try:
        target = int(pid or 0)
    except (TypeError, ValueError):
        return ""
    if target <= 0 or target == os.getpid():
        return ""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(target)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
        else:
            os.killpg(os.getpgid(target), signal.SIGKILL)
        return "，已终止任务进程"
    except Exception:  # noqa: BLE001
        return ""


def _drop_proc(kind: str, problem_id: int) -> None:
    with _LOCK:
        RUNNING.pop((kind, int(problem_id)), None)


def running_count(kind: str) -> int:
    with _LOCK:
        alive = 0
        for key, proc in list(RUNNING.items()):
            if proc.poll() is not None:
                RUNNING.pop(key, None)
                continue
            if key[0] == kind:
                alive += 1
        return alive


def _has_running(db: Any, kind: str, problem_id: int) -> bool:
    return _one(
        db,
        "SELECT id FROM jobs WHERE kind=? AND problem_id=? AND status='running'"
        " ORDER BY id DESC LIMIT 1",
        (kind, int(problem_id)),
    ) is not None


def start(
    db: Any,
    *,
    kind: str,
    problem_id: int,
    args: List[str],
    budget: int = 0,
    limit: int = 2,
    user: str = "",
) -> Dict[str, Any]:
    problem_id = int(problem_id)
    if _has_running(db, kind, problem_id):
        return {"error": "该题已有同类任务在运行", "code": "JOB_RUNNING"}
    if running_count(kind) >= max(1, int(limit)):
        return {"error": "服务正忙，请稍后再试", "code": "SERVICE_BUSY"}

    log_path = _log_dir() / f"{kind}-{problem_id}-{int(time.time())}.log"
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO jobs(user, problem_id, kind, status, stage, budget, os_pid, error,"
            " failed_stage, result_json, started_at, finished_at)"
            " VALUES(?,?,?, 'running', '', ?, 0, '', '', NULL, ?, NULL)",
            (str(user or ""), problem_id, kind, int(budget), _now()),
        )
        job_id = int(cursor.lastrowid)

    argv = [
        sys.executable, "-m", "autocode.slim2.worker", kind,
        *args,
        "--job-id", str(job_id),
        "--problem-id", str(problem_id),
    ]
    log = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    # POSIX 下必须让任务进程自成会话：取消时用 os.killpg 杀组，
    # 若与服务器同组，一次「取消」会把 Web 服务本身一起杀掉。
    spawn: Dict[str, Any] = {}
    if os.name != "nt":
        spawn["start_new_session"] = True
    try:
        proc = subprocess.Popen(
            argv, cwd=str(config.ROOT), stdout=log, stderr=log, env=os.environ.copy(), **spawn
        )
    except OSError as exc:
        log.close()
        finish(db, job_id, "failed", f"无法启动任务进程：{exc}", "process")
        return {"error": f"无法启动任务进程：{exc}", "code": "INTERNAL"}
    finally:
        try:
            log.close()
        except Exception:  # noqa: BLE001
            pass

    with _LOCK:
        RUNNING[(kind, problem_id)] = proc
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET os_pid=? WHERE id=?", (proc.pid, job_id))
    return {"ok": True, "job_id": job_id, "os_pid": proc.pid}


def finish(
    db: Any,
    job_id: int,
    status: str,
    error: str = "",
    failed_stage: str = "",
    result: Optional[Dict[str, Any]] = None,
) -> None:
    payload = json.dumps(result, ensure_ascii=False) if result is not None else None
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, error=?, failed_stage=?, result_json=?, finished_at=?"
            " WHERE id=?",
            (status, str(error or "")[:2000], str(failed_stage or ""), payload, _now(), int(job_id)),
        )
        if status == "done":
            # 最后一步的 stage 会一直挂着（比如"参考解复算"），前端会误以为还在跑
            conn.execute("UPDATE jobs SET stage='' WHERE id=?", (int(job_id),))
        else:
            # 任务没成功时必须把题目从「生成中」放出来，否则界面永远停在生成中。
            # 已有旧数据的题回到 ready（旧数据仍然可用），没有数据的才算 failed。
            conn.execute(
                "UPDATE problems SET status = CASE WHEN dataset_version > 0 THEN 'ready' ELSE 'failed' END"
                " WHERE id = (SELECT problem_id FROM jobs WHERE id=?) AND status='generating'",
                (int(job_id),),
            )


def set_stage(db: Any, job_id: int, stage: str) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE jobs SET stage=? WHERE id=? AND status='running'",
            (str(stage or "")[:40], int(job_id)),
        )


def cancel_by_id(db: Any, job_id: int) -> Dict[str, Any]:
    row = _one(db, "SELECT * FROM jobs WHERE id=?", (int(job_id),))
    if row is None:
        return {"error": "任务不存在", "code": "NOT_FOUND"}
    if str(row["status"]) != "running":
        return {"error": "任务已经结束", "code": "NOT_FOUND"}
    detail = kill_tree(row["os_pid"])
    _drop_proc(str(row["kind"]), int(row["problem_id"]))
    finish(db, int(row["id"]), "failed", f"已手动取消{detail}", "cancelled")
    return {"ok": True, "job_id": int(row["id"])}


def cancel(db: Any, problem_id: int, kind: str = "") -> Dict[str, Any]:
    sql = "SELECT * FROM jobs WHERE problem_id=? AND status='running'"
    params: List[Any] = [int(problem_id)]
    if kind:
        sql += " AND kind=?"
        params.append(str(kind))
    sql += " ORDER BY id DESC LIMIT 1"
    row = _one(db, sql, tuple(params))
    if row is None:
        return {"error": "没有正在运行的任务", "code": "NOT_FOUND"}
    detail = kill_tree(row["os_pid"])
    _drop_proc(str(row["kind"]), int(problem_id))
    finish(db, int(row["id"]), "failed", f"已手动取消{detail}", "cancelled")
    return {"ok": True, "job_id": int(row["id"])}


def _stale(started_at: Any) -> bool:
    started = parse_ts(started_at)
    if started is None:
        return False
    return datetime.now(timezone.utc) - started > timedelta(minutes=config.JOB_TIMEOUT_MIN)


def reconcile(db: Any, row: Any) -> Any:
    if row is None or str(row["status"]) != "running":
        return row
    kind = str(row["kind"])
    problem_id = int(row["problem_id"])
    job_id = int(row["id"])
    proc = RUNNING.get((kind, problem_id))

    if proc is not None and proc.poll() is not None:
        _drop_proc(kind, problem_id)
        fresh = _one(db, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if fresh is not None and str(fresh["status"]) == "running":
            finish(db, job_id, "failed", "任务进程已退出但未写终态", "process")
        return _one(db, "SELECT * FROM jobs WHERE id=?", (job_id,))

    if _stale(row["started_at"]):
        detail = kill_tree(row["os_pid"])
        _drop_proc(kind, problem_id)
        finish(db, job_id, "failed", f"任务超过 {config.JOB_TIMEOUT_MIN} 分钟未收敛{detail}", "process")
        return _one(db, "SELECT * FROM jobs WHERE id=?", (job_id,))

    if proc is None and not process_alive(row["os_pid"]):
        fresh = _one(db, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if fresh is not None and str(fresh["status"]) == "running":
            finish(db, job_id, "failed", "任务进程已退出", "process")
        return _one(db, "SELECT * FROM jobs WHERE id=?", (job_id,))
    return row


def status(db: Any, problem_id: int, kind: Optional[str] = None) -> Dict[str, Any]:
    sql = "SELECT * FROM jobs WHERE problem_id=?"
    params: List[Any] = [int(problem_id)]
    if kind:
        sql += " AND kind=?"
        params.append(str(kind))
    sql += " ORDER BY id DESC LIMIT 1"
    row = _one(db, sql, tuple(params))
    if row is None:
        return {"status": "none", "kind": kind or "", "stage": "", "job_id": 0,
                "error": "", "failed_stage": "", "result": None, "elapsed_s": 0,
                "started_at": "", "finished_at": ""}
    return _serialize(db, row)


def elapsed_seconds(row: Any) -> int:
    """已用时由服务端算。

    前端自己拿 `Date.now() - started_at` 会带上客户端时钟偏差——实测某台机器
    比服务器慢 21 秒，界面上「已 7 秒」时实际已经跑了 28 秒。这种数字一旦偏，
    用户就更分不清「慢」和「卡死」。
    """
    started = parse_ts(row["started_at"])
    if started is None:
        return 0
    end = parse_ts(row["finished_at"]) or datetime.now(timezone.utc)
    return max(0, int((end - started).total_seconds()))


def _serialize(db: Any, row: Any) -> Dict[str, Any]:
    row = reconcile(db, row)
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except (TypeError, ValueError):
            result = None
    return {
        "status": str(row["status"]),
        "kind": str(row["kind"]),
        "stage": str(row["stage"] or ""),
        "job_id": int(row["id"]),
        "error": str(row["error"] or ""),
        "failed_stage": str(row["failed_stage"] or ""),
        "result": result,
        "elapsed_s": elapsed_seconds(row),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
    }


def by_id(db: Any, job_id: int) -> Dict[str, Any]:
    row = _one(db, "SELECT * FROM jobs WHERE id=?", (int(job_id),))
    if row is None:
        raise KeyError(job_id)
    return _serialize(db, row)
