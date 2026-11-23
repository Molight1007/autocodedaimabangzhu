#!/usr/bin/env bash
# 热更新：解压代码包 → 闸门检查在跑任务 → 备份 → 重启 → 自检。
#
# 用法：
#   sudo bash update-linux.sh <代码包.tar.gz> [--force]
#
# 闸门：有任务在跑就拒绝重启（退出码 2）。
# 背景（2026-11-22）：部署补丁时直接 systemctl restart，把正在「数据复核」的
# 一整题连进程杀掉，2.3 万 tokens 白烧，界面上还显示成「进程异常」。
# 确实要强更（比如修的就是 worker 本身）再加 --force。
#
# 环境变量（一般不用改）：
#   AUTOCODE_APP=/opt/autocode     AUTOCODE_SVC=autocode-slim
#   AUTOCODE_DB=...                默认读 /etc/autocode/slim2.env
#   AUTOCODE_BACKUP_DIR=/var/backups/autocode

set -euo pipefail

PKG="${1:?用法: sudo bash update-linux.sh <代码包.tar.gz> [--force]}"
FORCE="${2:-}"
APP="${AUTOCODE_APP:-/opt/autocode}"
SVC="${AUTOCODE_SVC:-autocode-slim}"
ENV_FILE="/etc/autocode/slim2.env"
PY="$APP/.venv/bin/python"
DEST="${AUTOCODE_BACKUP_DIR:-/var/backups/autocode}"

if [ "$(id -u)" -ne 0 ]; then
    echo "[失败] 请用 sudo 运行"
    exit 1
fi
if [ ! -f "$PKG" ]; then
    echo "[失败] 找不到代码包：$PKG"
    exit 1
fi

DB="${AUTOCODE_DB:-}"
if [ -z "$DB" ] && [ -f "$ENV_FILE" ]; then
    # env 文件里没写 AUTOCODE_DB 时 grep 会返回 1，pipefail 下会直接把脚本带走
    DB="$(grep -E '^AUTOCODE_DB=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '[:space:]' || true)"
fi
DB="${DB:-$APP/data/slim2.db}"
if [ ! -f "$DB" ]; then
    echo "[失败] 找不到数据库：$DB"
    exit 1
fi

echo "== 1/4 检查在跑任务（$DB）=="
RUNNING="$("$PY" - "$DB" <<'PY'
import sqlite3
import sys

db = sqlite3.connect(sys.argv[1])
rows = db.execute(
    "SELECT id, problem_id, stage, started_at FROM jobs WHERE status='running'"
).fetchall()
for job_id, problem_id, stage, started in rows:
    print(f"  job {job_id}  题目 {problem_id}  阶段 {stage or '-'}  开始 {started}")
PY
)"

if [ -n "$RUNNING" ]; then
    echo "[拒绝] 有任务正在跑，现在重启会把它们连进程一起杀掉："
    echo "$RUNNING"
    if [ "$FORCE" != "--force" ]; then
        echo
        echo "等它跑完再更新；确实要强更就加 --force（任务会被标记为「服务重启中断」）。"
        exit 2
    fi
    echo "[警告] 已指定 --force，继续"
else
    echo "   没有任务在跑"
fi

echo "== 2/4 备份 =="
mkdir -p "$DEST"
stamp="$(date +%F_%H%M)"
tar czf "$DEST/code-$stamp.tar.gz" -C "$APP" autocode 2>/dev/null || true
"$PY" - "$DB" "$DEST/slim2-$stamp.db" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PY
chmod 600 "$DEST/slim2-$stamp.db"
echo "   代码：$DEST/code-$stamp.tar.gz"
echo "   数据库：$DEST/slim2-$stamp.db"

echo "== 3/4 解压 =="
tar xzf "$PKG" -C "$APP"
find "$APP/autocode" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
chown -R autocode:autocode "$APP/autocode" "$APP/deploy" 2>/dev/null || true
echo "   已覆盖 $APP/autocode"

echo "== 4/4 重启与自检 =="
systemctl restart "$SVC"
sleep 2
if ! systemctl is-active --quiet "$SVC"; then
    echo "[失败] 服务没起来，最近日志："
    journalctl -u "$SVC" -n 40 --no-pager || true
    exit 1
fi
PORT="$(grep -E '^AUTOCODE_SLIM2_PORT=' "$ENV_FILE" 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
PORT="${PORT:-8767}"
if ! curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null; then
    echo "[失败] 健康检查不过，最近日志："
    journalctl -u "$SVC" -n 40 --no-pager || true
    exit 1
fi

echo
echo "================ 更新完成 ================"
echo "服务：$SVC active，healthz ok（127.0.0.1:${PORT}）"
journalctl -u "$SVC" -n 4 --no-pager | sed 's/^/  /'
