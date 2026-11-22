"""数据版改密：`python -m autocode.slim2.passwd`。

与精简版共用 `slim.auth` 的 scrypt 实现，但路径来自 `slim2.config`。
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import List, Optional

from . import auth
from . import config


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="autocode.slim2.passwd")
    parser.add_argument("--password", help="直接指定新密码（不推荐，会留在 shell 历史里）")
    parser.add_argument("--keep-sessions", action="store_true", help="保留已登录会话")
    args = parser.parse_args(argv)

    config.apply_env()
    if config.password_hash():
        print("检测到 AUTOCODE_SLIM2_PASSWORD_HASH，环境变量优先；请先撤销该变量再改密。")
        return 1

    password = str(args.password or "")
    if not password:
        password = getpass.getpass("新密码：")
        confirm = getpass.getpass("再输一次：")
        if password != confirm:
            print("两次输入不一致")
            return 1
    if len(password) < 8:
        print("密码至少 8 位")
        return 1

    auth.update_password(password)
    if not args.keep_sessions:
        from .db import get_db

        auth.delete_all_sessions(get_db())
    print("密码已更新；初始化密码文件如仍存在，请手动删除。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
