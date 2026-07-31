"""
Модуль: core/_gas.py
Назначение: Сетевой оракул котировок газа (Gas Tracker).
Зона ответственности: Фоновый опрос публичных RPC-нод для получения актуальных
                      комиссий сетей Ethereum (BaseFee + Priority) и Bitcoin (Mempool).
Интеграция: Слой L1. Работает в изолированном фоновом потоке (GasPriceWorker).
            Возвращает данные через callback, не блокируя Event Loop интерфейса.
            Зависит от констант L0 (_constants.py) и системного логгера.
"""

import json
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Callable

from system.logger import logger
from core._constants import USER_AGENT, ETH_RPC_ENDPOINTS


# =================== NETWORK HELPERS ===================

def _make_json_request(url: str, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 10.0) -> Any:
    """
    Универсальный легковесный хелпер для JSON-запросов.
    Использует стандартную библиотеку urllib, чтобы не тащить тяжелые сессии aiohttp/requests
    ради простых пингов оракула.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json"
    }
    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
    
    # Resource Guard: Гарантированное закрытие сокета через контекстный менеджер
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status not in (200, 201):
            raise RuntimeError(f"HTTP {resp.status}")
        resp_data = resp.read().decode("utf-8", errors="replace")
    
    if not resp_data:
        return {}
    return json.loads(resp_data)


def _with_retry(fn: Callable[[], Any], attempts: int = 2, delay: float = 0.6) -> Any:
    """
    Механизм повторных попыток для нестабильных публичных RPC-нод.
    """
    last_err: Exception | None = None
    for i in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except Exception as ex:
            last_err = ex
            if i < attempts:
                time.sleep(max(0.0, delay))
    
    if last_err:
        raise last_err


# =================== GAS ORACLE LOGIC ===================

def get_gas_prices_sync() -> dict[str, float | None]:
    """
    Синхронное получение усреднённых котировок БЕЗ платных оракулов.
    Возвращает {'btc': float|None, 'eth': float|None}.
    """
    prices: dict[str, float | None] = {"btc": None, "eth": None}
    
    def _eth_feehistory_plus_tip(url: str) -> float:
        """Расчет газа по EIP-1559 (BaseFee + PriorityFee)."""
        fh = _make_json_request(url, "POST", {
            "jsonrpc": "2.0", "id": 1, "method": "eth_feeHistory",
            "params": [5, "latest", [5, 25, 50, 75, 95]]
        }, timeout=8.0)
        
        fh_res = (fh or {}).get("result") or {}
        base_arr = fh_res.get("baseFeePerGas")
        if not (isinstance(base_arr, list) and base_arr and isinstance(base_arr[-1], str)):
            raise RuntimeError(f"{url}: feeHistory вернул ответ без корректного baseFeePerGas")
        
        base_next_wei = int(base_arr[-1], 16)
        tip_wei: int | None = None
        
        # Пытаемся получить актуальный tip через eth_maxPriorityFeePerGas
        try:
            tip = _make_json_request(url, "POST", {
                "jsonrpc": "2.0", "id": 2, "method": "eth_maxPriorityFeePerGas", "params": []
            }, timeout=5.0)
            tip_hex = (tip or {}).get("result")
            if isinstance(tip_hex, str) and tip_hex.startswith("0x"):
                tip_wei = int(tip_hex, 16)
        except Exception:
            pass
        
        # Fallback: берем медиану из исторических ревордов или хардкодим 2 Gwei
        if tip_wei is None:
            reward = fh_res.get("reward")
            if isinstance(reward, list) and reward and isinstance(reward[-1], list) and len(reward[-1]) >= 3:
                tip_wei = int(reward[-1][2], 16)
            else:
                tip_wei = int(2e9)
        
        return (base_next_wei + tip_wei) / 1e9
    
    def _eth_gasprice(url: str) -> float:
        """Legacy расчет газа (Fallback)."""
        gp = _make_json_request(url, "POST", {
            "jsonrpc": "2.0", "id": 3, "method": "eth_gasPrice", "params": []
        }, timeout=8.0)
        hexwei = (gp or {}).get("result")
        if not (isinstance(hexwei, str) and hexwei.startswith("0x")):
            raise RuntimeError(f"{url}: некорректный ответ eth_gasPrice")
        return int(hexwei, 16) / 1e9
    
    def _eth_via_rpc_pool() -> float:
        """Перебор пула нод до первого успешного ответа."""
        last_err: Exception | None = None
        for url in ETH_RPC_ENDPOINTS:
            try:
                return _eth_feehistory_plus_tip(url)
            except Exception as ex1:
                last_err = ex1
                try:
                    return _eth_gasprice(url)
                except Exception as ex2:
                    last_err = ex2
                    continue
        
        raise last_err or RuntimeError("Все RPC-эндпоинты ETH прилегли отдохнуть")
    
    # --- Запрос ETH ---
    try:
        prices["eth"] = _with_retry(_eth_via_rpc_pool, attempts=2, delay=0.5)
    except Exception as e:
        # Логируем только в дебаг/ворнинг, чтобы не спамить юзера при отвале интернета
        logger.warning(f"Оракул ETH недоступен: {e}", profile_names=["GLOBAL"], category="API")
    
    # --- Запрос BTC ---
    def _btc() -> float | None:
        data = _make_json_request("https://mempool.space/api/v1/fees/recommended", timeout=8.0)
        cands = [data.get("fastestFee"), data.get("halfHourFee"), data.get("hourFee")]
        nums = [float(x) for x in cands if isinstance(x, (int, float))]
        return (sum(nums) / len(nums)) if nums else None
    
    try:
        prices["btc"] = _with_retry(_btc, attempts=2, delay=0.5)
    except Exception as e:
        logger.warning(f"Оракул BTC недоступен: {e}", profile_names=["GLOBAL"], category="API")
    
    return prices


# =================== BACKGROUND WORKER ===================

class GasPriceWorker(threading.Thread):
    """
    Фоновый воркер для получения цен газа и BTC fee.
    Гарантирует, что сетевые задержки не заморозят интерфейс PySide6.
    """
    
    def __init__(self, callback: Callable[[dict[str, float | None], str | None], None]) -> None:
        super().__init__(daemon=True, name="GasOracleThread")
        self._callback = callback
    
    def run(self) -> None:
        prices: dict[str, float | None] = {"btc": None, "eth": None}
        error: str | None = None
        
        try:
            prices = get_gas_prices_sync()
        except Exception as e:
            error = str(e)
        
        try:
            if self._callback:
                self._callback(prices, error)
        except Exception as cb_ex:
            logger.error(
                f"Ошибка обработки колбэка GAS: {cb_ex}",
                profile_names=["GLOBAL"],
                category="API",
                extra={"trace": str(cb_ex)}
            )


def get_gas_prices_async(callback: Callable[[dict[str, float | None], str | None], None]) -> None:
    """
    Асинхронная обертка для запуска воркера оракула.
    """
    GasPriceWorker(callback).start()


def format_gas_string(prices: dict[str, float | None], readable: bool = False) -> str:
    """
    Форматирует словарь с ценами в красивую строку для заголовка окна.
    """
    sep = "     ⛽     " if readable else " ⛽ "
    parts: list[str] = []
    
    btc = prices.get("btc")
    parts.append(f"BTC {btc:.0f} sat/vB" if btc is not None else "BTC —")
    
    eth = prices.get("eth")
    parts.append(f"ETH {eth:.2f} Gwei" if eth is not None else "ETH —")
    
    return sep.join(parts)