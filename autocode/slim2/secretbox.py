"""用户 API Key 的落库封装：只用标准库的 HMAC-CTR 加密 + Encrypt-then-MAC。

为什么不直接存明文：库文件和备份会被整体复制走，明文等于把所有人的 Key 一起送出去。
主密钥单独放 `data/llm_master.key`（600），与库文件分开；换掉它等于作废所有已存 Key，
各人重新填一次即可，不会影响账号本身。

密文格式：`v1:<nonce_b64>:<ct_b64>:<tag_b64>`，单行字符串，直接塞进 TEXT 列。
还原失败（主密钥丢失/被篡改）返回空串，由上层提示"请重新填写 Key"，不抛异常。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path
from typing import Tuple

from . import config

_MASTER_ENV = "AUTOCODE_SLIM2_MASTER_KEY_FILE"
_VERSION = "v1"
_NONCE_BYTES = 12
_TAG_BYTES = 16


def master_path() -> Path:
    raw = str(os.environ.get(_MASTER_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return config.DATA_DIR / "llm_master.key"


def _master() -> bytes:
    """读主密钥；不存在就生成一份 32 字节随机值（首次写入用户 Key 时发生）。"""
    path = master_path()
    if path.exists():
        data = path.read_bytes()
        if len(data) >= 32:
            return data[:32]
    path.parent.mkdir(parents=True, exist_ok=True)
    data = secrets.token_bytes(32)
    # 先以 600 建文件再写内容：避免"写入后 chmod"之间被同机其它用户读到
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _master()
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return data


def _subkeys(master: bytes) -> Tuple[bytes, bytes]:
    enc = hmac.new(master, b"autocode/enc", hashlib.sha256).digest()
    mac = hmac.new(master, b"autocode/mac", hashlib.sha256).digest()
    return enc, mac


def _keystream(key: bytes, nonce: bytes, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:size])


def _xor(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def seal(plain: str) -> str:
    """明文 -> 单行密文；空串原样返回（表示"没有 Key"）。"""
    text = str(plain or "")
    if not text:
        return ""
    enc, mac = _subkeys(_master())
    nonce = os.urandom(_NONCE_BYTES)
    body = text.encode("utf-8")
    ct = _xor(body, _keystream(enc, nonce, len(body)))
    tag = hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:_TAG_BYTES]
    return f"{_VERSION}:{_b64(nonce)}:{_b64(ct)}:{_b64(tag)}"


def unseal(token: str) -> str:
    """密文 -> 明文；版本不对、被改过、主密钥换了都返回空串。"""
    raw = str(token or "")
    if not raw:
        return ""
    try:
        version, nonce_b64, ct_b64, tag_b64 = raw.split(":")
        if version != _VERSION:
            return ""
        nonce, ct, tag = _unb64(nonce_b64), _unb64(ct_b64), _unb64(tag_b64)
        enc, mac = _subkeys(_master())
        want = hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:_TAG_BYTES]
        if not hmac.compare_digest(tag, want):
            return ""
        return _xor(ct, _keystream(enc, nonce, len(ct))).decode("utf-8")
    except Exception:  # noqa: BLE001 - 任何解析失败都当成"没有 Key"
        return ""
