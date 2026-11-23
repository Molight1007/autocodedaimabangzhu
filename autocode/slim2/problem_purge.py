"""删题与题库上限。

两条规则：

1. 删一道题必须把它名下的东西一起清掉。连接开了 `PRAGMA foreign_keys`，
   `ON DELETE CASCADE` 虽然生效，但历史库里仍可能有没写完的级联，这里仍然逐表删，
   漏一张表就会留下指向空题目的孤儿行。手动删题和「题库超限自动清理」走同一条路径。
2. **只有临时库有上限**：`MAX_PROBLEMS_TOTAL` 道，超出的按 id 从小到大删，
   也就是保留最新入库的那一批。永久库不限数量，`trim_to_cap` 直接返回 0——
   否则管理员刚建的永久库会被这条规则从最早的题开始吃掉。
"""

from __future__ import annotations

import shutil
from typing import Any, Optional

from . import banks, config, jobs


def purge_problem(db: Any, problem_id: int) -> None:
    """删掉一道题及其全部从属数据（测试数据、对抗数据、任务、插图文件）。"""
    pid = int(problem_id)
    # 先停任务：进程还活着的话，它会继续调模型花钱，最后写进一道已经删掉的题。
    for row in db.query_all(
        "SELECT id FROM jobs WHERE problem_id=? AND status='running'", (pid,)
    ):
        jobs.cancel_by_id(db, int(row["id"]))
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM case_reviews WHERE case_id IN"
            " (SELECT id FROM test_cases WHERE problem_id=?)", (pid,)
        )
        conn.execute(
            "DELETE FROM adversarial_reviews WHERE adv_case_id IN"
            " (SELECT id FROM adversarial_cases WHERE problem_id=?)", (pid,)
        )
        conn.execute("DELETE FROM adversarial_cases WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM submissions WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM test_cases WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM dataset_plans WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM jobs WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM problem_figures WHERE problem_id=?", (pid,))
        conn.execute("DELETE FROM problems WHERE id=?", (pid,))
    # 插图是磁盘文件，删库不会带走；忽略失败：目录不在或正被占用都不该挡住删题
    shutil.rmtree(config.DATA_DIR / "assets" / f"problem_{pid}", ignore_errors=True)


def trim_to_cap(db: Any, cap: Optional[int] = None, bank_id: Optional[int] = None) -> int:
    """把某个临时库的题目数量压回上限，返回删掉的道数；永久库恒为 0。

    `bank_id` 不给时按默认题库算：老调用点（命令行、旧测试）不用改。
    """
    key = banks.DEFAULT_ID if bank_id is None else int(bank_id)
    bank = banks.get(db, key)
    if bank is None or banks.is_permanent(bank):
        return 0
    limit = banks.cap_of(bank) if cap is None else int(cap)
    total = int(db.query_scalar("SELECT COUNT(*) FROM problems WHERE bank_id=?", (key,), 0))
    excess = total - limit
    if excess <= 0:
        return 0
    # id 递增等于入库顺序：删最早的那几条，剩下的就是最新的一批
    rows = db.query_all(
        "SELECT id FROM problems WHERE bank_id=? ORDER BY id ASC LIMIT ?", (key, excess)
    )
    for row in rows:
        purge_problem(db, int(row["id"]))
    return len(rows)
