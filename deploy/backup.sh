#!/usr/bin/env bash
# 每日备份：SQLite 在线快照 + 完整性校验 + 保留 N 天。
# 不依赖 sqlite3 命令行：服务器上常常没装，退化用项目 venv 的 python（标准库即可）。
#
# 安装：
#   chmod 700 /opt/autocode/deploy/backup.sh
#   cp deploy/autocode-backup.service deploy/autocode-backup.timer /etc/systemd/system/
#   systemctl daemon-reload && systemctl enable --now autocode-backup.timer
#
# 手动跑一次：systemctl start autocode-backup.service
set -euo pipefail

DB="${AUTOCODE_SLIM2_DB:-/opt/autocode/data/slim2.db}"
DEST="${AUTOCODE_BACKUP_DIR:-/var/backups/autocode}"
KEEP_DAYS="${AUTOCODE_BACKUP_KEEP_DAYS:-14}"
PY="${AUTOCODE_PYTHON:-/opt/autocode/.venv/bin/python}"

if [ ! -f "$DB" ]; then
    echo "database not found: $DB" >&2
    exit 1
fi

mkdir -p "$DEST"
chmod 700 "$DEST"
stamp="$(date +%F_%H%M)"
target="$DEST/slim2-$stamp.db"

if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$target'"
    check="$(sqlite3 "$target" 'PRAGMA integrity_check;')"
else
    check="$("$PY" - "$DB" "$target" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(src)
d = sqlite3.connect(dst)
s.backup(d)
d.close()
s.close()
c = sqlite3.connect(dst)
print(c.execute("PRAGMA integrity_check").fetchone()[0])
c.close()
PY
)"
fi
chmod 600 "$target"

if [ "$check" != "ok" ]; then
    echo "integrity check failed: $check" >&2
    rm -f "$target"
    exit 1
fi

find "$DEST" -maxdepth 1 -name 'slim2-*.db' -type f -mtime "+$KEEP_DAYS" -delete

echo "backup ok: $target ($(du -h "$target" | cut -f1))"
