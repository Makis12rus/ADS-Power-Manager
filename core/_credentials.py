"""
Модуль: core/_credentials.py
Назначение: Безопасное хранилище паролей (Windows Credential Manager).
Зона ответственности: Шифрование и извлечение паролей Web3-кошельков с использованием
                      нативных механизмов ОС Windows. Предотвращает хранение
                      конфиденциальных данных в открытом виде в реестре.
Интеграция: Слой L1. Поддерживает двойной бэкенд (pywin32 / ctypes) и предоставляет
            безопасные заглушки (mocks) для запуска на Unix-системах.
            Список ключей кошельков (WALLETS_KEYS) теперь заполняется динамически
            через PluginManager.
"""

import sys
import threading
from typing import Any

from system.logger import logger

# =================== КОНСТАНТЫ И ДИНАМИЧЕСКИЙ РЕЕСТР ===================

CRED_PREFIX: str = "ADSProfileManager_"

# Динамический список ключей паролей. Заполняется PluginManager'ом при старте.
WALLETS_KEYS: list[str] = []
_KEYS_LOCK = threading.Lock()


def register_wallet_key(key: str) -> None:
    """
    Потокобезопасная регистрация нового ключа кошелька в сейфе.
    Вызывается диспетчером плагинов при обнаружении нового .json манифеста.
    """
    with _KEYS_LOCK:
        key_str = str(key).strip()
        if key_str and key_str not in WALLETS_KEYS:
            WALLETS_KEYS.append(key_str)
            logger.info(
                f"Сейф подготовлен для хранения пароля: {key_str}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )


# =================== ДЕТЕКТ БЭКЕНДА ===================

_WIN_CRED_BACKEND: str = "none"

if sys.platform == "win32":
    try:
        import win32cred
        import win32con
        
        _WIN_CRED_BACKEND = "pywin32"
    except ImportError:
        _WIN_CRED_BACKEND = "ctypes"
else:
    _WIN_CRED_BACKEND = "none"


def _cm_available() -> bool:
    """Проверяет доступность Windows Credential Manager."""
    return sys.platform == "win32" and _WIN_CRED_BACKEND in ("pywin32", "ctypes")


def _cred_target_name(wallet_key: str) -> str:
    """Формирует уникальное имя таргета для системного хранилища."""
    return f"{CRED_PREFIX}{wallet_key}"


# =================== БЭКЕНД: PYWIN32 ===================

def _cred_write_pywin32(wallet_key: str, secret: str) -> bool:
    try:
        target = _cred_target_name(wallet_key)
        win32cred.CredWrite({
            'Type': win32cred.CRED_TYPE_GENERIC,
            'TargetName': target,
            'UserName': "",
            'CredentialBlob': secret,
            'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
            'Comment': "ADSProfile Manager wallet password",
        }, 0)
        return True
    except Exception as e:
        logger.warning(
            f"Сбой записи в Credential Manager (pywin32) для {wallet_key}: {e}",
            profile_names=["GLOBAL"], category="SETTINGS"
        )
        return False


def _cred_read_pywin32(wallet_key: str) -> str | None:
    try:
        target = _cred_target_name(wallet_key)
        cred = win32cred.CredRead(TargetName=target, Type=win32cred.CRED_TYPE_GENERIC)
        blob = cred.get('CredentialBlob')
        if blob is None:
            return ""
        if isinstance(blob, bytes):
            try:
                return blob.decode("utf-16-le")
            except Exception:
                return blob.decode(errors="ignore")
        return str(blob)
    except Exception:
        # Нормальная ситуация, если пароль еще не был сохранен
        return None


def _cred_delete_pywin32(wallet_key: str) -> bool:
    try:
        target = _cred_target_name(wallet_key)
        win32cred.CredDelete(TargetName=target, Type=win32cred.CRED_TYPE_GENERIC, Flags=0)
        return True
    except Exception:
        return False


# =================== БЭКЕНД: CTYPES (Fallback) ===================

if sys.platform == "win32" and _WIN_CRED_BACKEND == "ctypes":
    import ctypes
    from ctypes import wintypes
    
    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]
    
    _advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    
    _CredWriteW = _advapi32.CredWriteW
    _CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    _CredWriteW.restype = wintypes.BOOL
    
    _CredReadW = _advapi32.CredReadW
    _CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    _CredReadW.restype = wintypes.BOOL
    
    _CredFree = _advapi32.CredFree
    _CredFree.argtypes = [ctypes.c_void_p]
    _CredFree.restype = None
    
    _CredDeleteW = _advapi32.CredDeleteW
    _CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _CredDeleteW.restype = wintypes.BOOL
    
    def _cred_write_ctypes(wallet_key: str, secret: str) -> bool:
        try:
            target = _cred_target_name(wallet_key)
            data = secret.encode("utf-16-le")
            blob = ctypes.create_string_buffer(data)
            
            cred = CREDENTIALW()
            cred.Type = 1  # CRED_TYPE_GENERIC
            cred.TargetName = ctypes.c_wchar_p(target)
            cred.CredentialBlobSize = len(data)
            cred.CredentialBlob = ctypes.cast(blob, ctypes.c_void_p)
            cred.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE
            cred.UserName = ctypes.c_wchar_p("")
            
            return bool(_CredWriteW(ctypes.byref(cred), 0))
        except Exception as e:
            logger.warning(
                f"Сбой записи в Credential Manager (ctypes) для {wallet_key}: {e}",
                profile_names=["GLOBAL"], category="SETTINGS"
            )
            return False
    
    def _cred_read_ctypes(wallet_key: str) -> str | None:
        try:
            target = _cred_target_name(wallet_key)
            pcred = ctypes.POINTER(CREDENTIALW)()
            
            if not _CredReadW(target, 1, 0, ctypes.byref(pcred)):
                return None
            
            try:
                size = pcred.contents.CredentialBlobSize
                ptr = pcred.contents.CredentialBlob
                buf = ctypes.string_at(ptr, size)
                return buf.decode("utf-16-le")
            except Exception:
                return None
            finally:
                # Resource Guard: Обязательное освобождение памяти C-структуры
                _CredFree(pcred)
        except Exception:
            return None
    
    def _cred_delete_ctypes(wallet_key: str) -> bool:
        try:
            return bool(_CredDeleteW(_cred_target_name(wallet_key), 1, 0))
        except Exception:
            return False
else:
    # Заглушки для Unix-систем или если бэкенд pywin32 активен
    def _cred_write_ctypes(k: str, s: str) -> bool:
        return False
    
    def _cred_read_ctypes(k: str) -> str | None:
        return None
    
    def _cred_delete_ctypes(k: str) -> bool:
        return False


# =================== УНИВЕРСАЛЬНЫЕ ОБЕРТКИ (PUBLIC API) ===================

def _cred_write(k: str, s: str) -> bool:
    """Безопасная запись пароля в системное хранилище."""
    if sys.platform != "win32":
        return False
    if _WIN_CRED_BACKEND == "pywin32":
        return _cred_write_pywin32(k, s)
    if _WIN_CRED_BACKEND == "ctypes":
        return _cred_write_ctypes(k, s)
    return False


def _cred_read(k: str) -> str | None:
    """Безопасное чтение пароля из системного хранилища."""
    if sys.platform != "win32":
        return None
    if _WIN_CRED_BACKEND == "pywin32":
        return _cred_read_pywin32(k)
    if _WIN_CRED_BACKEND == "ctypes":
        return _cred_read_ctypes(k)
    return None


def _cred_delete(k: str) -> bool:
    """Удаление пароля из системного хранилища."""
    if sys.platform != "win32":
        return False
    if _WIN_CRED_BACKEND == "pywin32":
        return _cred_delete_pywin32(k)
    if _WIN_CRED_BACKEND == "ctypes":
        return _cred_delete_ctypes(k)
    return False