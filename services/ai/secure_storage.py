from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from typing import Optional

from PySide6.QtCore import QSettings


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def _crypt_protect_data(data: bytes) -> bytes:
    if not data:
        return b""

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()

    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise OSError("CryptProtectData failed")

    try:
        out = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return out
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _crypt_unprotect_data(data: bytes) -> bytes:
    if not data:
        return b""

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    in_blob = _DATA_BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DATA_BLOB()

    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    ):
        raise OSError("CryptUnprotectData failed")

    try:
        out = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        return out
    finally:
        kernel32.LocalFree(out_blob.pbData)


class SecureSettingsStore:
    def __init__(self, *, organization: str, application: str):
        self._settings = QSettings(organization, application)

    def set_value(self, key: str, value) -> None:
        self._settings.setValue(key, value)
        self._settings.sync()

    def get_value(self, key: str, default=None):
        return self._settings.value(key, default)

    def set_secret(self, key: str, value: str) -> None:
        raw = (value or "").encode("utf-8")
        encrypted = _crypt_protect_data(raw)
        b64 = base64.b64encode(encrypted).decode("ascii")
        self._settings.setValue(key, b64)
        self._settings.sync()

    def get_secret(self, key: str) -> Optional[str]:
        v = self._settings.value(key, "")
        if not v:
            return None
        try:
            encrypted = base64.b64decode(str(v).encode("ascii"), validate=True)
            raw = _crypt_unprotect_data(encrypted)
            return raw.decode("utf-8")
        except Exception:
            return None

    def clear_secret(self, key: str) -> None:
        self._settings.remove(key)
        self._settings.sync()
