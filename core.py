# =========================
# 📝 Файл: core.py
# =========================

import sys
import os
import json
import time
import threading
import traceback
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from logger import logger

# ============ Централизованные метаданные приложения ============
APP_NAME: str = "ADSProfile Manager"
APP_VERSION: str = "0.4.1"  # подняли патч-версию

# =================== Дефолтные настройки приложения ===================
APP_DEFAULTS: Dict[str, str] = {
    "active_mode": "ADS",   # "ADS" или "AUTO"
    "stay_on_top": "0",     # "0"/"1"
}

# ====== Дефолтные настройки текущего функционала ADS ======
# ВАЖНО: ключ adspower_rps оставляем для обратной совместимости хранения,
# но реальный лимит строго 1 RPS теперь зашит в ads_logic.py (по доке AdsPower).
SETTINGS_DEFAULTS: Dict[str, str] = {
    "api_url": "http://local.adspower.com:50395",
    "rabby_pass": "",
    "okx_pass": "",
    "keplr_pass": "",
    "backpack_pass": "",
    "phantom_pass": "",
    "delay_start": "5",
    "delay_stop": "1",
    "wallet_retry_count": "3",
    "adspower_rps": "1.0",   # исторический ключ, не влияет на фактический лимит
    "selenium_pool": "3",
}

# =================== Детект платформы / поддержка пина ===================

def is_windows() -> bool:
    """Удобный флаг платформы."""
    return sys.platform == "win32"

def supports_pin() -> bool:
    """
    Поддерживает ли платформа «Поверх всех окон» в нашем UX-виде.
    На Windows — да. На macOS/Linux — отключаем кнопку и поведение (по ТЗ).
    """
    return is_windows()

# =================== GAS SERVICE (надёжнее и чуть быстрее) ===================

class GasPriceWorker(threading.Thread):
    """
    Фоновый воркер для получения цен газа.
    Без побочных эффектов: только вызов колбэка по завершении.
    """
    def __init__(self, callback):
        super().__init__(daemon=True)
        self._callback = callback

    def run(self):
        prices = {"btc": None, "eth": None}
        error = None
        try:
            prices = get_gas_prices_sync()
        except Exception as e:
            error = str(e)
        try:
            if self._callback:
                self._callback(prices, error)
        except Exception as cb_ex:
            # Не ломаем GUI, просто логируем
            logger.error(
                f"Ошибка обработки результата GAS в колбэке: {cb_ex}",
                profile_names=["GLOBAL"], category="API", extra={"trace": str(cb_ex)}
            )

def get_gas_prices_async(callback):
    """
    Асинхронный запуск запроса цен газа.
    """
    GasPriceWorker(callback).start()

def _http_get_json(url: str, timeout: float = 10.0) -> Dict[str, Any]:
    """
    Маленький, но аккуратный helper: GET + JSON с понятными ошибками.
    Используем один UA, короткий таймаут и защиту от частых сетевых сбоев.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 ADSProfileManager"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        data = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(data)
    except Exception as ex:
        raise RuntimeError(f"Ошибка парсинга JSON: {ex}")

def _with_retry(fn, attempts: int = 2, delay: float = 0.6):
    """
    Простой повтор (retry) для внешних API: 2 попытки по умолчанию.
    Никаких лишних задержек, чтобы не «тормозить» GUI.
    """
    last = None
    for i in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except Exception as ex:
            last = ex
            if i < attempts:
                time.sleep(max(0.0, delay))
    if last:
        raise last

def get_gas_prices_sync() -> Dict[str, Optional[float]]:
    """
    Получение усреднённых котировок БЕЗ внешних API-оракулов.
      • ETH — RPC-пул:
          1) eth_feeHistory + eth_maxPriorityFeePerGas → (base_next + tip) / 1e9 (gwei)
          2) фолбэк: eth_gasPrice / 1e9 (gwei)
      • BTC — mempool.space (среднее по fastest/30min/1h)
    Возвращает {'btc': float|None, 'eth': float|None}.
    Исключение бросается только если обе метрики получить не удалось.
    """
    prices: Dict[str, Optional[float]] = {"btc": None, "eth": None}

    # ---------- ETH через пул публичных RPC ----------
    eth_error = None

    RPC_ENDPOINTS = [
        "https://1rpc.io/eth",                    # 1RPC (приватность/проксирование)
        "https://ethereum-rpc.publicnode.com",    # PublicNode (Allnodes)
        "https://0xrpc.io/eth",                   # 0xRPC
        "https://eth.drpc.org",                   # dRPC public
    ]

    def _rpc_post(url: str, payload: Dict[str, Any], timeout: float = 8.0) -> Dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ADSPM/1.0 (+gas-tracker)"
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
        return json.loads(txt) if txt else {}

    def _eth_feehistory_plus_tip(url: str) -> float:
        """
        Базовый способ для EIP-1559:
          • берём прогноз baseFee следующего блока из eth_feeHistory (последний элемент baseFeePerGas)
          • добавляем tip: eth_maxPriorityFeePerGas
          • если maxPriority не поддержан/упал — пробуем взять 50-й перцентиль tip из reward feeHistory
        """
        # 1) feeHistory за последние 5 блоков, с перцентилями для tip (в т.ч. 50)
        fh = _rpc_post(url, {
            "jsonrpc": "2.0", "id": 1, "method": "eth_feeHistory",
            "params": [5, "latest", [5, 25, 50, 75, 95]]
        })
        fh_res = (fh or {}).get("result") or {}
        base_arr = fh_res.get("baseFeePerGas")
        if not (isinstance(base_arr, list) and base_arr and isinstance(base_arr[-1], str)):
            raise RuntimeError(f"{url}: feeHistory без корректного baseFeePerGas")
        base_next_wei = int(base_arr[-1], 16)

        # 2) пробуем maxPriorityFeePerGas
        tip_wei = None
        try:
            tip = _rpc_post(url, {
                "jsonrpc": "2.0", "id": 2, "method": "eth_maxPriorityFeePerGas", "params": []
            })
            tip_hex = (tip or {}).get("result")
            if isinstance(tip_hex, str) and tip_hex.startswith("0x"):
                tip_wei = int(tip_hex, 16)
        except Exception:
            tip_wei = None

        # 3) если tip не получили — берём 50-й перцентиль reward для самого свежего блока
        if tip_wei is None:
            reward = fh_res.get("reward")
            # reward — список длиной blockCount; каждый элемент — массив percentiles
            if isinstance(reward, list) and reward and isinstance(reward[-1], list) and len(reward[-1]) >= 3:
                # индексы для [5,25,50,75,95] → 50-й перцентиль это индекс 2
                tip_wei = int(reward[-1][2], 16)
            else:
                # как крайний случай — 2 gwei на tip по умолчанию
                tip_wei = int(2e9)

        return (base_next_wei + tip_wei) / 1e9  # gwei

    def _eth_gasprice(url: str) -> float:
        gp = _rpc_post(url, {
            "jsonrpc": "2.0", "id": 3, "method": "eth_gasPrice", "params": []
        })
        hexwei = (gp or {}).get("result")
        if not (isinstance(hexwei, str) and hexwei.startswith("0x")):
            raise RuntimeError(f"{url}: некорректный ответ eth_gasPrice → {gp}")
        return int(hexwei, 16) / 1e9  # gwei

    def _eth_via_rpc_pool() -> float:
        last_err: Optional[Exception] = None
        for url in RPC_ENDPOINTS:
            try:
                # основной путь: feeHistory + maxPriority (или reward 50-й перцентиль)
                return _eth_feehistory_plus_tip(url)
            except Exception as ex1:
                last_err = ex1
                # фолбэк: простая оценка gasPrice
                try:
                    return _eth_gasprice(url)
                except Exception as ex2:
                    last_err = ex2
                    continue
        raise last_err or RuntimeError("Все RPC-эндпоинты вернули ошибки (ETH)")

    try:
        prices["eth"] = _with_retry(_eth_via_rpc_pool, attempts=2, delay=0.5)
    except Exception as e:
        eth_error = f"Ошибка получения ETH gas price (RPC пул): {e}"
        logger.error(eth_error, profile_names=["GLOBAL"], category="API", extra={"trace": str(e)})

    # ---------- BTC через mempool.space ----------
    btc_error = None

    def _btc() -> Optional[float]:
        data = _http_get_json("https://mempool.space/api/v1/fees/recommended", timeout=8.0)
        if not isinstance(data, dict):
            raise RuntimeError(f"mempool.space: невалидный ответ типа {type(data).__name__}")
        cands = [data.get("fastestFee"), data.get("halfHourFee"), data.get("hourFee")]
        nums = [float(x) for x in cands if isinstance(x, (int, float))]
        return (sum(nums) / len(nums)) if nums else None

    try:
        prices["btc"] = _with_retry(_btc, attempts=2, delay=0.5)
    except Exception as e:
        btc_error = f"Ошибка получения BTC fee: {e}"
        logger.error(btc_error, profile_names=["GLOBAL"], category="API", extra={"trace": str(e)})

    # Исключение — только если обе метрики не получены
    if prices["eth"] is None and prices["btc"] is None:
        raise RuntimeError(eth_error or btc_error or "Не удалось получить метрики газа")

    return prices



def format_gas_string(prices: Dict[str, Optional[float]], readable: bool = False) -> str:
    """
    Небольшая утилита форматирования заголовка окна.
    """
    sep = "     ⛽     " if readable else " ⛽ "
    parts: List[str] = []
    btc = prices.get("btc")
    parts.append(f"BTC {btc:.3f} sat/vB" if btc is not None else "BTC —")
    eth = prices.get("eth")
    parts.append(f"ETH {eth:.3f} Gwei" if eth is not None else "ETH —")
    return sep.join(parts)

# =================== СТАБИЛЬНОСТЬ И Watchdog ===================

class AppWatchdog(threading.Thread):
    """
    Watchdog-поток для проверки живости приложения и аварийного перезапуска.
    • Поток «мягкий»: понимает stop(), не оставляет зомби.
    • Перезапуск — через os.execl, как было, но с дополнительной защитой от лавины логов.
    """
    def __init__(self, check_interval: int = 30):
        super().__init__(daemon=True)
        self._check_interval = max(5, int(check_interval))
        self._last_heartbeat = time.time()
        self._running = threading.Event()
        self._running.set()
        self._last_restart_log_ts = 0.0  # анти-спам логов

    def heartbeat(self):
        self._last_heartbeat = time.time()

    def run(self):
        while self._running.is_set():
            try:
                if time.time() - self._last_heartbeat > self._check_interval * 2:
                    now = time.time()
                    if now - self._last_restart_log_ts > 10.0:  # лишний буфер, чтобы логи не сыпались
                        logger.error(
                            "Watchdog: приложение не отвечает, пробую перезапуск...",
                            profile_names=["GLOBAL"], category="SYSTEM"
                        )
                        self._last_restart_log_ts = now
                    try:
                        logger.info(
                            "Watchdog: попытка авто-перезапуска программы...",
                            profile_names=["GLOBAL"], category="SYSTEM"
                        )
                        os.execl(sys.executable, sys.executable, *sys.argv)
                    except Exception as e:
                        logger.error(
                            f"Watchdog: не удалось перезапустить приложение: {e}",
                            profile_names=["GLOBAL"], category="SYSTEM", extra={"trace": str(e)}
                        )
                time.sleep(self._check_interval)
            except Exception as loop_ex:
                # Watchdog сам себя не должен уронить
                logger.error(
                    f"Watchdog: исключение в цикле: {loop_ex}",
                    profile_names=["GLOBAL"], category="SYSTEM", extra={"trace": str(loop_ex)}
                )
                time.sleep(self._check_interval)

    def stop(self):
        self._running.clear()

_watchdog: Optional[AppWatchdog] = None  # Глобальный watchdog

def start_watchdog(interval: int = 30):
    """
    Идемпотентный запуск watchdog.
    """
    global _watchdog
    if _watchdog is None:
        _watchdog = AppWatchdog(check_interval=interval)
        _watchdog.start()
        logger.info("Watchdog запущен.", profile_names=["GLOBAL"], category="SYSTEM")

def stop_watchdog():
    """
    Корректная остановка watchdog: просим остановиться и чуть ждём.
    """
    global _watchdog
    if _watchdog is not None:
        try:
            _watchdog.stop()
            _watchdog.join(timeout=1.5)
        except Exception:
            pass
        _watchdog = None
        logger.info("Watchdog остановлен.", profile_names=["GLOBAL"], category="SYSTEM")

def ping_watchdog():
    """
    Пульс от активной части приложения (GUI/потоки).
    """
    global _watchdog
    if _watchdog is not None:
        _watchdog.heartbeat()

def is_watchdog_active() -> bool:
    return _watchdog is not None

# =================== Глобальный обработчик ошибок ===================

def global_exception_hook(exctype, value, tb):
    """
    Не даём приложению «падать молча»: аккуратно логируем traceback.
    """
    trace_txt = "".join(traceback.format_exception(exctype, value, tb))
    logger.error(
        "Глобальная ошибка: " + trace_txt,
        profile_names=["GLOBAL"], category="SYSTEM",
        extra={"trace": trace_txt}
    )

# Устанавливаем один раз на модульном уровне
sys.excepthook = global_exception_hook

# =================== Работа с реестром и Credential Manager ===================

if sys.platform == "win32":
    import winreg  # type: ignore
    REG_PATH = r"Software\ADSProfileManager"
else:
    winreg = None  # type: ignore
    REG_PATH = None  # type: ignore

# ---- Credential Manager backend detection (pywin32 / ctypes / none) ----
_WIN_CRED_BACKEND = "none"
try:
    if sys.platform == "win32":
        try:
            import win32cred  # type: ignore
            import win32con   # type: ignore
            _WIN_CRED_BACKEND = "pywin32"
        except Exception:
            _WIN_CRED_BACKEND = "ctypes"
    else:
        _WIN_CRED_BACKEND = "none"
except Exception:
    _WIN_CRED_BACKEND = "none"

# === Константы для CM ===
CRED_PREFIX = "ADSProfileManager_"
WALLETS_KEYS = ["rabby_pass", "okx_pass", "keplr_pass", "backpack_pass", "phantom_pass"]

def _open_key(create: bool = False):
    """
    Универсальный открыватель ветки реестра с безопасным create.
    """
    if not winreg:
        return None
    try:
        if create:
            return winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH)
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH)
    except FileNotFoundError:
        if create:
            return winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_PATH)
        return None
    except Exception:
        return None

def _cred_target_name(wallet_key: str) -> str:
    # Пример: ADSProfileManager_rabby_pass
    return f"{CRED_PREFIX}{wallet_key}"

# ---- Реализация через pywin32 ----
def _cred_write_pywin32(wallet_key: str, secret: str) -> bool:
    try:
        target = _cred_target_name(wallet_key)
        blob = secret.encode("utf-16-le")
        win32cred.CredWrite({
            'Type': win32cred.CRED_TYPE_GENERIC,
            'TargetName': target,
            'UserName': "",
            'CredentialBlob': blob,
            'Persist': win32cred.CRED_PERSIST_LOCAL_MACHINE,
            'Comment': "ADSProfile Manager wallet password",
        }, 0)
        return True
    except Exception as e:
        logger.warning(
            f"Не удалось записать пароль в Credential Manager ({wallet_key})",
            profile_names=["GLOBAL"], category="SETTINGS", extra={"trace": str(e)}
        )
        return False

def _cred_read_pywin32(wallet_key: str) -> Optional[str]:
    try:
        target = _cred_target_name(wallet_key)
        cred = win32cred.CredRead(TargetName=target, Type=win32cred.CRED_TYPE_GENERIC)
        blob = cred.get('CredentialBlob') or b""
        try:
            return blob.decode("utf-16-le")
        except Exception:
            return blob.decode(errors="ignore")
    except Exception:
        return None

def _cred_delete_pywin32(wallet_key: str) -> bool:
    try:
        target = _cred_target_name(wallet_key)
        win32cred.CredDelete(TargetName=target, Type=win32cred.CRED_TYPE_GENERIC, Flags=0)
        return True
    except Exception:
        return False

# ---- Резервный бэкенд на ctypes ----
if sys.platform == "win32" and _WIN_CRED_BACKEND == "ctypes":
    import ctypes
    from ctypes import wintypes

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

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
    _CredReadW = _advapi32.CredReadW
    _CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    _CredReadW.restype = wintypes.BOOL

    _CredWriteW = _advapi32.CredWriteW
    _CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    _CredWriteW.restype = wintypes.BOOL

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
            cred.Flags = 0
            cred.Type = 1
            cred.TargetName = ctypes.c_wchar_p(target)
            cred.Comment = None
            cred.CredentialBlobSize = len(data)
            cred.CredentialBlob = ctypes.cast(blob, ctypes.c_void_p)
            cred.Persist = 2
            cred.AttributeCount = 0
            cred.Attributes = None
            cred.TargetAlias = None
            cred.UserName = ctypes.c_wchar_p("")
            ok = _CredWriteW(ctypes.byref(cred), 0)
            return bool(ok)
        except Exception as e:
            logger.warning(
                f"ctypes: не удалось записать пароль ({wallet_key})",
                profile_names=["GLOBAL"], category="SETTINGS", extra={"trace": str(e)}
            )
            return False

    def _cred_read_ctypes(wallet_key: str) -> Optional[str]:
        try:
            target = _cred_target_name(wallet_key)
            pcred = ctypes.POINTER(CREDENTIALW)()
            ok = _CredReadW(target, 1, 0, ctypes.byref(pcred))
            if not ok:
                return None
            try:
                size = pcred.contents.CredentialBlobSize
                ptr = pcred.contents.CredentialBlob
                buf = ctypes.string_at(ptr, size)
                return buf.decode("utf-16-le")
            finally:
                _CredFree(pcred)
        except Exception:
            return None

    def _cred_delete_ctypes(wallet_key: str) -> bool:
        try:
            target = _cred_target_name(wallet_key)
            ok = _CredDeleteW(target, 1, 0)
            return bool(ok)
        except Exception:
            return False
else:
    # Заглушки вне Windows
    def _cred_write_ctypes(wallet_key: str, secret: str) -> bool:  # type: ignore
        return False
    def _cred_read_ctypes(wallet_key: str) -> Optional[str]:       # type: ignore
        return None
    def _cred_delete_ctypes(wallet_key: str) -> bool:              # type: ignore
        return False

def _cm_available() -> bool:
    return sys.platform == "win32" and _WIN_CRED_BACKEND in ("pywin32", "ctypes")

def _cred_write(wallet_key: str, secret: str) -> bool:
    if sys.platform != "win32":
        return False
    if _WIN_CRED_BACKEND == "pywin32":
        return _cred_write_pywin32(wallet_key, secret)
    if _WIN_CRED_BACKEND == "ctypes":
        return _cred_write_ctypes(wallet_key, secret)
    return False

def _cred_read(wallet_key: str) -> Optional[str]:
    if sys.platform != "win32":
        return None
    if _WIN_CRED_BACKEND == "pywin32":
        return _cred_read_pywin32(wallet_key)
    if _WIN_CRED_BACKEND == "ctypes":
        return _cred_read_ctypes(wallet_key)
    return None

def _cred_delete(wallet_key: str) -> bool:
    if sys.platform != "win32":
        return False
    if _WIN_CRED_BACKEND == "pywin32":
        return _cred_delete_pywin32(wallet_key)
    if _WIN_CRED_BACKEND == "ctypes":
        return _cred_delete_ctypes(wallet_key)
    return False

def _migrate_plain_passwords_to_cred(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Если в реестре пароли лежат «в открытую» (наследие прошлых версий),
    переносим их в Credential Manager и заменяем на "cred:<wallet_key>".
    """
    if not _cm_available():
        # Ничего не переносим, возвращаем как есть (пароли не будут развернуты дальше).
        return settings

    changed = False
    for k in WALLETS_KEYS:
        v = str(settings.get(k, "") or "")
        if not v or v.startswith("cred:"):
            continue
        if _cred_write(k, v):
            settings[k] = f"cred:{k}"
            changed = True
    if changed:
        _save_settings_labels_only(settings)
        logger.success(
            "Пароли перенесены в Windows Credential Manager.",
            profile_names=["GLOBAL"], category="SETTINGS"
        )
    return settings

def _expand_credentials_in_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Разворачивает метки cred:<key> в реальные значения паролей.
    Если Credential Manager недоступен — оставляем пусто.
    """
    for k in WALLETS_KEYS:
        v = str(settings.get(k, "") or "")
        if v.startswith("cred:"):
            wallet_key = v.split(":", 1)[1]
            if _cm_available():
                real = _cred_read(wallet_key)
                settings[k] = real or ""
            else:
                settings[k] = ""
    return settings

def _merge_defaults() -> Dict[str, str]:
    """Слияние дефолтов приложения и прикладных дефолтов ADS (строки)."""
    merged = dict(APP_DEFAULTS)
    merged.update(SETTINGS_DEFAULTS)
    return merged

def _save_settings_labels_only(settings: Dict[str, Any]) -> None:
    """
    Сохраняет только «видимые» значения (включая метки cred:<...>) в реестр.
    Реальных паролей в реестр НЕ пишет.
    """
    if not winreg:
        return
    try:
        reg = _open_key(True)
        merged_keys = _merge_defaults()
        for key in merged_keys:
            value = str(settings.get(key, merged_keys[key]))
            if key in WALLETS_KEYS:
                v = str(value or "")
                if v and not v.startswith("cred:"):
                    v = f"cred:{key}" if _cm_available() else ""
                winreg.SetValueEx(reg, key, 0, winreg.REG_SZ, v)
            else:
                winreg.SetValueEx(reg, key, 0, winreg.REG_SZ, str(value))
        winreg.CloseKey(reg)
    except Exception as ex:
        logger.warning(
            f"Не удалось сохранить метки настроек в реестр: {ex}",
            profile_names=["GLOBAL"], category="SETTINGS", extra={"trace": str(ex)}
        )

def load_settings_from_registry() -> Dict[str, str]:
    """
    Единая точка загрузки настроек приложения (включая ADS).
    • На Windows читаем из реестра HKCU\\Software\\ADSProfileManager
    • На других платформах — возвращаем дефолты (пароли пустые)
    • Мигрируем «голые» пароли в Credential Manager
    • Разворачиваем cred: метки в реальные значения в возвращаемом dict
    """
    settings = _merge_defaults().copy()
    if not winreg:
        # Вне Windows — просто вернём дефолты (пароли пустые)
        return _expand_credentials_in_settings(settings)

    try:
        reg = _open_key(False)
        if not reg:
            return _expand_credentials_in_settings(settings)
        merged_keys = _merge_defaults()
        for key, def_val in merged_keys.items():
            try:
                value, _ = winreg.QueryValueEx(reg, key)
                settings[key] = value
            except FileNotFoundError:
                settings[key] = def_val
        winreg.CloseKey(reg)
    except Exception:
        # Тихо возвращаем дефолты + разворачивание (если возможно)
        pass

    # Автомиграция и разворачивание паролей
    settings = _migrate_plain_passwords_to_cred(settings)
    settings = _expand_credentials_in_settings(settings)
    return settings

def save_settings_to_registry(settings: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Сохранение настроек:
      • пароли пишем в Credential Manager (если доступен), а в реестр — метки "cred:<key>"
      • остальные строки сохраняем напрямую
    """
    if not winreg:
        return False, "Сохранение настроек поддерживается только под Windows!"

    try:
        to_save_labels = dict(settings)
        merged = _merge_defaults()
        for k in merged:
            if k not in to_save_labels:
                to_save_labels[k] = merged[k]

        # Пароли
        for k in WALLETS_KEYS:
            val = str(settings.get(k, "") or "")
            if val:
                if _cm_available():
                    if _cred_write(k, val):
                        to_save_labels[k] = f"cred:{k}"
                    else:
                        to_save_labels[k] = ""
                else:
                    to_save_labels[k] = ""
            else:
                _cred_delete(k)
                to_save_labels[k] = ""

        # Остальные поля/метки
        _save_settings_labels_only(to_save_labels)
        return True, "Настройки успешно сохранены в реестре."
    except Exception as ex:
        return False, f"Ошибка при сохранении настроек в реестр: {ex}"

def delete_settings_from_registry() -> Tuple[bool, str]:
    """
    Полное удаление ветки реестра приложения и чистка CM-паролей.
    """
    if not winreg:
        return False, "Удаление из реестра поддерживается только под Windows!"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_PATH, 0, winreg.KEY_ALL_ACCESS) as reg:
            try:
                while True:
                    name, _, _ = winreg.EnumValue(reg, 0)
                    winreg.DeleteValue(reg, name)
            except OSError:
                pass
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_PATH)

        if _cm_available():
            for k in WALLETS_KEYS:
                _cred_delete(k)

        return True, "Ветка настроек программы и пароли в Credential Manager удалены."
    except FileNotFoundError:
        if _cm_available():
            for k in WALLETS_KEYS:
                _cred_delete(k)
        return True, "Ветка реестра уже была удалена или не существует."
    except Exception as ex:
        return False, f"Ошибка при удалении ветки реестра: {ex}"

def open_registry_in_regedit() -> Tuple[bool, str]:
    """
    Запуск regedit с правами администратора (Windows-только).
    """
    if sys.platform != "win32":
        return False, "Открытие редактора реестра поддерживается только под Windows!"
    try:
        import ctypes
        regedit_full_path = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'regedit.exe')
        ShellExecute = ctypes.windll.shell32.ShellExecuteW
        ret = ShellExecute(None, "runas", regedit_full_path, None, None, 1)
        if ret <= 32:
            return False, "Не удалось запустить редактор реестра с правами администратора."
        return True, "Редактор реестра успешно открыт с правами администратора."
    except Exception as ex:
        return False, f"Не удалось открыть редактор реестра: {ex}"

def save_api_url(api_url: str) -> None:
    """
    Маленький helper для точечного обновления адреса API в реестре.
    """
    settings = load_settings_from_registry()
    settings["api_url"] = api_url
    save_settings_to_registry(settings)

# =================== UI-настройки: геометрия окна и лог-дока ===================

# Ключи в нашей ветке реестра:
#   main_geometry      — base64 строка от QByteArray (saveGeometry)
#   logdock_geometry   — base64 строка от QByteArray (saveGeometry док-панели)
#   logdock_side       — "left" | "right" (сторона прилипания)
#   stay_on_top        — уже есть в APP_DEFAULTS ("0"/"1")

_UI_DEFAULTS: Dict[str, str] = {
    "main_geometry": "",
    "logdock_geometry": "",
    "logdock_side": "right",
    "logdock_visible": "1",
}

def load_ui_geometry() -> Dict[str, str]:
    """
    Читает из реестра сохранённые строки геометрии главного окна и плавающего
    окна логов, а также выбранную сторону прилипания. Возвращает dict со
    строками (пустые строки означают «не сохранено»).
    """
    result = dict(_UI_DEFAULTS)
    if not winreg:
        return result
    try:
        reg = _open_key(False)
        if not reg:
            return result
        for key in _UI_DEFAULTS:
            try:
                val, _ = winreg.QueryValueEx(reg, key)
                result[key] = str(val or "")
            except FileNotFoundError:
                result[key] = _UI_DEFAULTS[key]
        return result
    except Exception as ex:
        logger.warning(
            f"Не удалось прочитать UI-геометрию из реестра: {ex}",
            profile_names=["GLOBAL"], category="SETTINGS", extra={"trace": str(ex)}
        )
        return dict(_UI_DEFAULTS)

def save_ui_geometry(
    main_geometry_b64: Optional[str] = None,
    logdock_geometry_b64: Optional[str] = None,
    logdock_side: Optional[str] = None,
    logdock_visible: Optional[bool] = None
) -> Tuple[bool, str]:
    """
    Точечная запись значений геометрии/стороны/видимости док-панели в реестр.
    Передавайте только те аргументы, которые хотите обновить.
    """
    if not winreg:
        return False, "Сохранение геометрии поддерживается только под Windows!"
    try:
        reg = _open_key(True)
        if main_geometry_b64 is not None:
            winreg.SetValueEx(reg, "main_geometry", 0, winreg.REG_SZ, str(main_geometry_b64))
        if logdock_geometry_b64 is not None:
            winreg.SetValueEx(reg, "logdock_geometry", 0, winreg.REG_SZ, str(logdock_geometry_b64))
        if logdock_side is not None:
            side = "left" if str(logdock_side).lower() == "left" else "right"
            winreg.SetValueEx(reg, "logdock_side", 0, winreg.REG_SZ, side)
        if logdock_visible is not None:
            vis = "1" if bool(logdock_visible) else "0"
            winreg.SetValueEx(reg, "logdock_visible", 0, winreg.REG_SZ, vis)
        winreg.CloseKey(reg)
        return True, "UI-геометрия успешно сохранена."
    except Exception as ex:
        return False, f"Ошибка при сохранении UI-геометрии: {ex}"
