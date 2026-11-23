"""按登录账号隔离的模型凭据：谁的 Key 只给谁用。

规则（2026-11-22 用户要求）：
- 一行一个账号，主键就是登录名（学号 / 管理员名）；
- 别人的 Key 既读不到也用不了，接口只回传后 4 位；
- **不设全局默认 Key**：没配就明确报错，避免"以为用自己的、其实花别人的额度"；
- Key 加密后落库（见 `secretbox`），库被单独拷走也还原不出明文。

Base URL 只允许 https，且解析后不能指向内网/回环/链路本地地址——否则服务器会被
当成跳板去访问云厂商元数据、内网服务，那是比泄漏 Key 更严重的事。
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from ..llm.client import LlmConfig
from . import secretbox

DEFAULT_BASE = "https://api.deepseek.com/v1"
#: 默认模型。2026-11-22 从 `deepseek-chat` 换成官方当前型号 `deepseek-flash`：
#: 实测 deepseek-chat 在 D2（生成测试数据）上三次里两次把 16384/32768 预算全烧在
#: 非正文 token 上、content 返回空，整题失败；同一题面同一提示词换 deepseek-flash，
#: 12211 tokens（含 9252 reasoning）一次产出 14 条合法数据。它同时原生支持图片，
#: 图片导入也只认多模态型号，一个型号能把文字/PDF/Word/图片都覆盖。
DEFAULT_MODEL = "deepseek-flash"

_HINT = "还没有配置你自己的 API Key：点右上角「模型设置」填一个，只有你自己能用。"


def mask(key: str) -> str:
    text = str(key or "")
    if len(text) <= 8:
        return "****" if text else ""
    return f"{text[:4]}…{text[-4:]}"


def _row(db: Any, user: str) -> Optional[Dict[str, Any]]:
    row = db.query_one(
        "SELECT user, base_url, model, api_key_enc, updated_at"
        " FROM user_llm_settings WHERE user=?",
        (str(user or ""),),
    )
    return dict(row) if row is not None else None


def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def base_url_error(url: str) -> str:
    """返回空串表示允许；否则是给人看的拒绝理由。"""
    text = str(url or "").strip()
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme != "https":
        return "Base URL 必须是 https:// 开头"
    host = (parts.hostname or "").strip()
    if not host:
        return "Base URL 缺少主机名"
    if parts.username or parts.password:
        return "Base URL 不允许带用户名密码"
    try:
        infos = socket.getaddrinfo(host, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return f"Base URL 的域名解析不了：{host}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if _ip_blocked(ip):
            return f"Base URL 指向内网地址（{ip}），不允许"
    return ""


def get(db: Any, user: str) -> Dict[str, Any]:
    """给前端看的当前状态；永远不含明文。"""
    row = _row(db, user)
    if row is None:
        return {
            "ok": True,
            "configured": False,
            "user": str(user or ""),
            "base_url": DEFAULT_BASE,
            "model": "",
            "api_key_masked": "",
            "updated_at": "",
            "hint": _HINT,
        }
    key = secretbox.unseal(str(row.get("api_key_enc") or ""))
    return {
        "ok": True,
        "configured": bool(key),
        "user": str(row.get("user") or ""),
        "base_url": str(row.get("base_url") or DEFAULT_BASE),
        "model": str(row.get("model") or ""),
        "api_key_masked": mask(key),
        "updated_at": str(row.get("updated_at") or ""),
        "hint": "" if key else _HINT,
    }


def save(db: Any, user: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """按字段更新；`api_key` 空表示不改，`clear_key=true` 才清空。"""
    name = str(user or "").strip()
    if not name:
        return {"error": "没有登录账号", "code": "AUTH_REQUIRED"}
    model = str(payload.get("model") or "").strip()
    base_url = str(payload.get("base_url") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    clear = bool(payload.get("clear_key"))

    if model and len(model) > 80:
        return {"error": "模型名过长", "code": "INVALID_INPUT"}
    if api_key and len(api_key) < 8:
        return {"error": "API Key 看起来太短，请检查是否复制完整", "code": "INVALID_INPUT"}
    reason = base_url_error(base_url)
    if reason:
        return {"error": reason, "code": "INVALID_INPUT"}

    row = _row(db, name) or {}
    old_enc = str(row.get("api_key_enc") or "")
    if clear:
        new_enc = ""
    elif api_key:
        new_enc = secretbox.seal(api_key)
    else:
        new_enc = old_enc

    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO user_llm_settings(user, base_url, model, api_key_enc, updated_at)"
            " VALUES(?,?,?,?,?)"
            " ON CONFLICT(user) DO UPDATE SET base_url=excluded.base_url,"
            " model=excluded.model, api_key_enc=excluded.api_key_enc,"
            " updated_at=excluded.updated_at",
            (name, base_url or str(row.get("base_url") or ""), model or str(row.get("model") or ""), new_enc, now),
        )
    return get(db, name)


def config_for(db: Any, user: str) -> Optional[LlmConfig]:
    """取这个账号自己的配置；没配 Key（或 Key 解不开）返回 None。"""
    row = _row(db, user)
    if row is None:
        return None
    key = secretbox.unseal(str(row.get("api_key_enc") or ""))
    if not key:
        return None
    base_url = str(row.get("base_url") or DEFAULT_BASE)
    reason = base_url_error(base_url)
    if reason:
        return None
    return LlmConfig(
        api_key=key,
        base_url=base_url,
        model=str(row.get("model") or DEFAULT_MODEL),
        key_source="user",
    )


def reset(db: Any, user: str) -> Dict[str, Any]:
    """只清掉这个账号的 Key，保留 base_url/model。"""
    return save(db, user, {"clear_key": True})
