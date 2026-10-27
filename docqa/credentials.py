"""Optional Windows DPAPI credential storage, scoped to the current OS user."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re
import errno
import uuid


def _path(name):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        raise ValueError("凭证别名只能包含英文字母、数字、下划线或短横线")
    if os.name != "nt" or not os.getenv("LOCALAPPDATA"):
        raise RuntimeError("本机加密凭证存储仅支持 Windows；其他系统请使用环境变量")
    return Path(os.environ["LOCALAPPDATA"]) / "DocQA" / "credentials" / (name + ".dpapi")


def _crypt(raw, decrypt=False):
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buffer), Blob()
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError("Windows 当前用户凭证加密/解密失败")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def save_secret(name, value):
    path = _path(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("密钥不能为空")
    encrypted = _crypt(value.encode("utf-8"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
    temporary.write_bytes(encrypted)
    try:
        temporary.replace(path)
    except OSError as exc:
        if getattr(exc, "winerror", None) != 17 and exc.errno != errno.EXDEV:
            raise
        # Some Windows profile redirection providers cannot rename across mapped mounts.
        path.write_bytes(encrypted)
        temporary.unlink(missing_ok=True)


def read_secret(endpoint):
    value = os.getenv(endpoint.get("api_key_env", ""), "")
    if value:
        return value
    alias = endpoint.get("api_key_store")
    if not alias:
        return ""
    path = _path(alias)
    return _crypt(path.read_bytes(), decrypt=True).decode("utf-8") if path.exists() else ""


def delete_secret(name):
    """Remove only an explicitly named credential created by the caller."""
    _path(name).unlink(missing_ok=True)
