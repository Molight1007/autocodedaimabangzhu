"""autoCode 存储层（P3 补齐）。

规划模块：
- db.py        连接工厂（per-thread + WAL + foreign_keys=ON + busy_timeout=5000）
- migrations/  §7 DDL 与迁移
- repositories 仓储
- secrets.py   DPAPI 密钥存储（搬自旧 secure_storage）

约束（R6）：禁字符串拼 SQL；禁 INSERT OR REPLACE；子表重跑一律 ON CONFLICT DO UPDATE。
"""

from __future__ import annotations

__all__: list[str] = []
