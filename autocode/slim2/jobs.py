"""任务：入队、单队列调度、起子进程、写终态、崩溃对账、手动取消。

**所有生成任务共用一个队列**（测试数据、对抗数据一起排），最多同时跑
`config.JOB_SLOTS` 个：提交即入队（`status='queued'`），有空位立刻开跑；
前一个落终态后，常驻调度器在 1 秒内把空位补满。队列存在 `jobs` 表里，服务重启不丢。

调度只由常驻服务进程做：worker 跑完就退出，若让它顺手拉起下一个，
它拉起的进程会变成孤儿；统一由 `ensure_scheduler` 的线程接最稳。
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

from . import auth, config

RUNNING: Dict[Tuple[str, int], subprocess.Popen] = {}
_LOCK = threading.Lock()
# 调度与对账互斥：避免"刚抢到任务、os_pid 还没写"的瞬间被状态查询判成「进程已退出」。
# 只有父进程需要这把锁；worker 在自己的进程里写终态，互不影响。
_DISPATCH_LOCK = threading.RLock()
_SCHEDULER: Optional[threading.Thread] = None
_SCHEDULER_STOP = threading.Event()
# 单独留一层，测试替换它即可，不去 patch 标准库的 subprocess.Popen
_popen = subprocess.Popen


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


def _reap(kind: str, problem_id: int) -> None:
    """poll 已退出的子进程并销号。

    worker 写完终态就自己退出，父进程不去 wait 就会留下僵尸（每跑一题一个，
    直到服务重启）。查询任务状态时顺手回收，成本接近零。
    """
    proc = RUNNING.get((kind, int(problem_id)))
    if proc is None:
        return
    if proc.poll() is not None:
        _drop_proc(kind, problem_id)


def reap_all() -> None:
    """把所有已经退出的子进程回收掉（起新任务前清一次）。"""
    with _LOCK:
        keys = list(RUNNING)
    for key in keys:
        _reap(*key)


def _active_job(db: Any, kind: str, problem_id: int) -> Any:
    """同题同类已有排队或在跑的任务：防止连点产生两条任务、双份烧额度。"""
    return _one(
        db,
        "SELECT id FROM jobs WHERE kind=? AND problem_id=? AND status IN ('queued','running')"
        " ORDER BY id DESC LIMIT 1",
        (str(kind), int(problem_id)),
    )


def queue_ahead(db: Any, row: Any) -> int:
    """前面还有几个任务：正在跑的 + 更早排队的（测试与对抗共用一个队列）。"""
    if row is None or str(row["status"]) != "queued":
        return 0
    job_id = int(row["id"])
    running = int(
        db.query_scalar(
            "SELECT COUNT(*) FROM jobs WHERE status='running' AND id<?", (job_id,), 0
        )
    )
    queued = int(
        db.query_scalar(
            "SELECT COUNT(*) FROM jobs WHERE status='queued' AND id<?", (job_id,), 0
        )
    )
    return running + queued


def start(
    db: Any,
    *,
    kind: str,
    problem_id: int,
    args: List[str],
    budget: int = 0,
    user: str = "",
) -> Dict[str, Any]:
    """提交任务：先入库排队，有空位立刻开跑；没空位就等，不再返回「服务正忙」。"""
    problem_id = int(problem_id)
    reap_all()
    # 查重与入队必须在同一把锁里：两个人同时点同一道题，否则会各插一条排队记录
    with _DISPATCH_LOCK:
        reconcile_all(db)
        if _active_job(db, kind, problem_id) is not None:
            return {"error": "该题已有同类任务在排队或运行", "code": "JOB_RUNNING"}

        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(user, problem_id, kind, status, stage, budget, os_pid, error,"
                " failed_stage, result_json, args_json, queued_at, started_at, finished_at)"
                " VALUES(?,?,?,'queued','',?,0,'','',NULL,?,?,'',NULL)",
                (
                    str(user or ""),
                    problem_id,
                    str(kind),
                    int(budget),
                    json.dumps([str(item) for item in (args or [])], ensure_ascii=False),
                    _now(),
                ),
            )
            job_id = int(cursor.lastrowid)

    dispatch(db)
    row = _one(db, "SELECT * FROM jobs WHERE id=?", (job_id,))
    if str(row["status"]) == "failed":
        return {"error": str(row["error"] or "任务启动失败"), "code": "INTERNAL", "job_id": job_id}
    queued = str(row["status"]) == "queued"
    return {
        "ok": True,
        "job_id": job_id,
        "queued": queued,
        "position": (queue_ahead(db, row) + 1) if queued else 0,
        "os_pid": int(row["os_pid"] or 0),
    }


def dispatch(db: Any) -> int:
    """把空闲执行位补满（总数不超过 `config.JOB_SLOTS`）；返回本次起了几个。"""
    started = 0
    with _DISPATCH_LOCK:
        reconcile_all(db)
        while True:
            running = int(
                db.query_scalar("SELECT COUNT(*) FROM jobs WHERE status='running'", (), 0)
            )
            if running >= max(1, int(config.JOB_SLOTS)):
                break
            row = _one(
                db, "SELECT * FROM jobs WHERE status='queued' ORDER BY id ASC LIMIT 1"
            )
            if row is None:
                break
            with db.transaction() as conn:
                claimed = conn.execute(
                    "UPDATE jobs SET status='running', started_at=? WHERE id=? AND status='queued'",
                    (_now(), int(row["id"])),
                ).rowcount
            if not claimed:
                break
            if not _spawn(db, row):
                break
            started += 1
    return started


def _spawn(db: Any, row: Any) -> bool:
    """按 job 行拉起 worker；参数从库里取，重启后剩下的排队任务也能照常开跑。"""
    kind = str(row["kind"])
    problem_id = int(row["problem_id"])
    job_id = int(row["id"])
    try:
        extra = [str(item) for item in json.loads(row["args_json"] or "[]")]
    except (TypeError, ValueError):
        extra = []
    argv = [
        sys.executable, "-m", "autocode.slim2.worker", kind,
        *extra,
        "--job-id", str(job_id),
        "--problem-id", str(problem_id),
    ]
    budget = int(row["budget"] or 0)
    if budget > 0:  # 对抗任务的 budget 是 0，交给 worker 默认值
        argv += ["--budget", str(budget)]

    log_path = _log_dir() / f"{kind}-{problem_id}-{int(time.time())}.log"
    log = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    # POSIX 下必须让任务进程自成会话：取消时用 os.killpg 杀组，
    # 若与服务器同组，一次「取消」会把 Web 服务本身一起杀掉。
    spawn: Dict[str, Any] = {}
    if os.name != "nt":
        spawn["start_new_session"] = True
    try:
        proc = _popen(
            argv, cwd=str(config.ROOT), stdout=log, stderr=log, env=os.environ.copy(), **spawn
        )
    except OSError as exc:
        finish(db, job_id, "failed", f"无法启动任务进程：{exc}", "process")
        return False
    finally:
        try:
            log.close()
        except Exception:  # noqa: BLE001
            pass

    with _LOCK:
        RUNNING[(kind, problem_id)] = proc
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET os_pid=? WHERE id=?", (proc.pid, job_id))
    return True


def finish(
    db: Any,
    job_id: int,
    status: str,
    error: str = "",
    failed_stage: str = "",
    result: Optional[Dict[str, Any]] = None,
) -> None:
    """落终态。这里**不**负责启动下一个任务：由常驻调度器在下一秒接手。"""
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
    status = str(row["status"])
    if status == "queued":
        # 还没起进程：落终态即可，队里后面的任务位次自动前移
        finish(db, int(row["id"]), "failed", "已取消排队", "cancelled")
        return {"ok": True, "job_id": int(row["id"])}
    if status != "running":
        return {"error": "任务已经结束", "code": "NOT_FOUND"}
    detail = kill_tree(row["os_pid"])
    _drop_proc(str(row["kind"]), int(row["problem_id"]))
    finish(db, int(row["id"]), "failed", f"已手动取消{detail}", "cancelled")
    dispatch(db)
    return {"ok": True, "job_id": int(row["id"])}


def cancel(db: Any, problem_id: int, kind: str = "") -> Dict[str, Any]:
    sql = "SELECT * FROM jobs WHERE problem_id=? AND status IN ('running','queued')"
    params: List[Any] = [int(problem_id)]
    if kind:
        sql += " AND kind=?"
        params.append(str(kind))
    # 同一题若既有在跑的又有排队的（历史数据），优先取消在跑的那条
    sql += " ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END, id DESC LIMIT 1"
    row = _one(db, sql, tuple(params))
    if row is None:
        return {"error": "没有正在运行或排队的任务", "code": "NOT_FOUND"}
    return cancel_by_id(db, int(row["id"]))


def mark_orphans(db: Any, reason: str = "服务重启，任务被中断（可重新生成）") -> int:
    """启动时对账：上个进程留下的 running 任务，进程已经不在就标为中断。

    只处理 running；**排队中的任务保留**，服务起来后由调度器接着跑。
    为什么单独做一步：重启会连坐杀掉 worker，但数据库要等到有人来查状态才更新。
    那之前界面一直显示「生成中」，更新时给的理由又是「进程异常」，
    用户会以为程序崩了。pid 仍存活的不动，避免误伤手工拉起的进程。
    """
    count = 0
    for row in db.query_all("SELECT id, os_pid FROM jobs WHERE status='running'"):
        if process_alive(row["os_pid"]):
            continue
        finish(db, int(row["id"]), "failed", reason, "interrupted")
        count += 1
    return count


def _stale(started_at: Any) -> bool:
    started = parse_ts(started_at)
    if started is None:
        return False
    return datetime.now(timezone.utc) - started > timedelta(minutes=config.JOB_TIMEOUT_MIN)


def reconcile(db: Any, row: Any) -> Any:
    with _DISPATCH_LOCK:
        return _reconcile_locked(db, row)


def _reconcile_locked(db: Any, row: Any) -> Any:
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


def reconcile_all(db: Any) -> None:
    """把所有"DB 说 running"的任务和进程现实对齐（抢执行位之前必须先做）。"""
    for row in db.query_all("SELECT * FROM jobs WHERE status='running'"):
        reconcile(db, row)


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
                "started_at": "", "finished_at": "", "queued_at": "", "ahead": 0, "position": 0}
    return _serialize(db, row)


def elapsed_seconds(row: Any) -> int:
    """已用时由服务端算。

    前端自己拿 `Date.now() - started_at` 会带上客户端时钟偏差——实测某台机器
    比服务器慢 21 秒，界面上「已 7 秒」时实际已经跑了 28 秒。这种数字一旦偏，
    用户就更分不清「慢」和「卡死」。
    排队中的任务 started_at 为空，返回 0：等待时长不该算成执行时长。
    """
    started = parse_ts(row["started_at"])
    if started is None:
        return 0
    end = parse_ts(row["finished_at"]) or datetime.now(timezone.utc)
    return max(0, int((end - started).total_seconds()))


def _serialize(db: Any, row: Any) -> Dict[str, Any]:
    _reap(str(row["kind"]), int(row["problem_id"]))
    row = reconcile(db, row)
    result = None
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except (TypeError, ValueError):
            result = None
    state = str(row["status"])
    ahead = queue_ahead(db, row) if state == "queued" else 0
    return {
        "status": state,
        "kind": str(row["kind"]),
        "stage": str(row["stage"] or ""),
        "job_id": int(row["id"]),
        "error": str(row["error"] or ""),
        "failed_stage": str(row["failed_stage"] or ""),
        "result": result,
        "elapsed_s": elapsed_seconds(row),
        "started_at": str(row["started_at"] or ""),
        "finished_at": str(row["finished_at"] or ""),
        "queued_at": str(row["queued_at"] or ""),
        "ahead": ahead,
        "position": (ahead + 1) if state == "queued" else 0,
    }


def by_id(db: Any, job_id: int) -> Dict[str, Any]:
    row = _one(db, "SELECT * FROM jobs WHERE id=?", (int(job_id),))
    if row is None:
        raise KeyError(job_id)
    return _serialize(db, row)


# ── 调度线程 ────────────────────────────────────────────────────────────────
def ensure_scheduler(db: Any, interval: float = config.JOB_TICK_SEC) -> None:
    """跟着服务启动的调度线程；重复调用无副作用。测试里不调用它。"""
    global _SCHEDULER
    with _LOCK:
        if _SCHEDULER is not None and _SCHEDULER.is_alive():
            return
        _SCHEDULER_STOP.clear()
        _SCHEDULER = threading.Thread(
            target=_scheduler_loop,
            args=(db, float(interval)),
            name="slim2-job-scheduler",
            daemon=True,
        )
        _SCHEDULER.start()


def _scheduler_loop(db: Any, interval: float) -> None:
    last_sweep = time.time()
    try:
        while not _SCHEDULER_STOP.wait(interval):
            try:
                # 已退出的 worker 每次心跳收一遍。只靠「查任务状态」和「起新任务」回收不够：
                # 任务跑完没人再点，Popen 就一直挂着，线上实测有 4 个僵尸活了两个多小时。
                reap_all()
                dispatch(db)
            except Exception as exc:  # noqa: BLE001 - 调度线程不能因为一次异常就死
                print(f"任务调度器出错（不影响正在跑的任务）：{exc}", file=sys.stderr, flush=True)
            # 过期会话每天清一次：原来只在启动时 purge，服务长期不重启就会越积越多
            now = time.time()
            if now - last_sweep >= 86400:
                last_sweep = now
                try:
                    auth.purge_expired(db)
                except Exception:  # noqa: BLE001 - 清理失败不影响业务
                    pass
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
