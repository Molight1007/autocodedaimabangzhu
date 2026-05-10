from __future__ import annotations

import os
import shutil
from typing import Optional, Tuple


def is_safe_directory_target(path: str) -> bool:
    p = os.path.abspath(str(path or "").strip())
    if not p:
        return False
    drive, tail = os.path.splitdrive(p)
    if drive and tail in {"\\", "/"}:
        return False
    return os.path.isdir(p)


def delete_output_dir_contents(output_dir: str, *, cutoff_ts: Optional[float]) -> Tuple[int, int]:
    root = os.path.abspath(str(output_dir or "").strip())
    if not is_safe_directory_target(root):
        raise ValueError("invalid_output_dir")

    deleted_files = 0
    deleted_dirs = 0

    if cutoff_ts is None:
        for entry in os.scandir(root):
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, ignore_errors=False)
                    deleted_dirs += 1
                else:
                    os.remove(entry.path)
                    deleted_files += 1
            except Exception:
                continue
        return deleted_files, deleted_dirs

    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.stat(fp)
                if float(st.st_mtime) < float(cutoff_ts):
                    os.remove(fp)
                    deleted_files += 1
            except Exception:
                continue

        for dn in dirnames:
            dp = os.path.join(dirpath, dn)
            try:
                if os.path.isdir(dp) and not os.listdir(dp):
                    os.rmdir(dp)
                    deleted_dirs += 1
            except Exception:
                continue

    return deleted_files, deleted_dirs


def count_deletable_files(output_dir: str, *, cutoff_ts: Optional[float]) -> Tuple[int, int]:
    root = os.path.abspath(str(output_dir or "").strip())
    if not is_safe_directory_target(root):
        raise ValueError("invalid_output_dir")

    if cutoff_ts is None:
        files = 0
        dirs = 0
        for entry in os.scandir(root):
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs += 1
                else:
                    files += 1
            except Exception:
                continue
        return files, dirs

    files = 0
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                st = os.stat(fp)
                if float(st.st_mtime) < float(cutoff_ts):
                    files += 1
            except Exception:
                continue
    return files, 0

