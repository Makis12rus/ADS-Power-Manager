"""
Модуль: moduls/ads/_utils.py
Назначение: Изолированный набор чистых математических и системных утилит.
Зона ответственности: Сортировка профилей, парсинг дат, расчет шагов прогресс-бара
                      и безопасное прерывание потоков (sleep_or_cancel).
Интеграция: Является фундаментом пакета. Не импортирует другие модули
            из `moduls/ads/` для предотвращения циклических зависимостей.
            Является частью плоского пакета `moduls/ads/`.
"""

import time
import re
import threading
from typing import Any, Callable

# Строгие абсолютные импорты ядра
from core.core import load_settings_from_registry, WALLETS_KEYS

# ======================= Типы и Исключения =======================

# Тип колбэка прогресса: один вызов == один микрошаг
ProgressCB = Callable[[str], None] | None


class OperationCancelled(Exception):
    """
    Исключение для мягкой и кооперативной отмены операций пользователем.
    Позволяет воркерам корректно выйти из циклов и закрыть драйверы в блоке finally.
    """
    pass


# ======================= Утилиты управления потоком =======================

def _sleep_or_cancel(seconds: float, event: threading.Event | None) -> None:
    """
    Ожидание с возможностью мгновенной отмены через threading.Event.
    Если событие установлено, немедленно выбрасывает OperationCancelled.
    """
    if seconds <= 0:
        return
    if event:
        # wait() возвращает True, если флаг был установлен до истечения таймаута
        if event.wait(seconds):
            raise OperationCancelled("Прервано пользователем")
    else:
        time.sleep(seconds)


def _progress(cb: ProgressCB, text: str, evt: threading.Event | None = None) -> None:
    """
    Безопасный вызов коллбэка прогресса с проверкой флага отмены.
    """
    if cb:
        cb(text)
    if evt and evt.is_set():
        raise OperationCancelled("Отменено пользователем")


# ======================= Утилиты парсинга и сортировки =======================

def _natural_int(text: Any) -> int:
    """
    Извлекает число для естественной сортировки.
    Если число не найдено, возвращает MaxInt, чтобы элемент ушел в конец списка.
    """
    try:
        if isinstance(text, (int, float)):
            return int(text)
        s = str(text or "")
        m = re.search(r'(\d+)(?!.*\d)', s)
        return int(m.group(1)) if m else 9_223_372_036_854_775_807
    except Exception:
        return 9_223_372_036_854_775_807


def _parse_created_ts(v: Any) -> float:
    """
    Универсальный парсер времени создания профиля для сортировки.
    Поддерживает Unix timestamp (секунды/миллисекунды) и ISO-строки.
    """
    try:
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            val = float(v)
            return val / 1000.0 if val > 1e12 else val
        
        s = str(v).strip()
        if not s:
            return 0.0
        
        if s.isdigit():
            val = float(s)
            return val / 1000.0 if len(s) >= 13 else val
        
        if re.match(r'^\d{4}-\d{2}-\d{2}', s):
            import time as _t
            fmt = "%Y-%m-%dT%H:%M:%S" if "T" in s else "%Y-%m-%d"
            st = _t.strptime(s[:19], fmt)
            return float(_t.mktime(st))
    except Exception:
        pass
    
    return 0.0


def _sort_profiles_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Стабильная многоуровневая сортировка профилей AdsPower:
    Приоритет: Время создания -> Серийный номер (SN) -> ID профиля.
    """
    ts_fields = ["created_time", "create_time", "createdAt", "created", "createdTime", "gmt_create"]
    
    def key_fn(p: dict[str, Any]) -> tuple[float, int, int]:
        ts = 0.0
        for f in ts_fields:
            if p.get(f):
                ts = _parse_created_ts(p.get(f))
                break
        
        sn = _natural_int(p.get("serial_number"))
        pid = _natural_int(p.get("profile_id") or p.get("user_id"))
        
        # Если время создания неизвестно, отправляем в конец
        ts_key = ts if ts > 0 else 9_223_372_036_854_775_807
        return (ts_key, sn, pid)
    
    return sorted(items, key=key_fn)


# ======================= Планировщик шагов (Progress Estimator) =======================

def estimate_steps_for_open(settings: dict[str, Any] | None = None) -> int:
    """
    Расчет количества микрошагов для запуска профиля.
    Базовые шаги (Формирование -> HTTP -> JSON -> Pre-flight Polling -> Selenium)
    + шаги на каждый включенный кошелек.
    """
    settings = settings or load_settings_from_registry()
    wallet_steps = 0
    
    # Динамически проверяем наличие паролей для оценки количества шагов разблокировки
    # на основе загруженных плагинов (SSOT)
    for k in WALLETS_KEYS:
        if str(settings.get(k, "")).strip():
            wallet_steps += 4
            
    # 4 (Базовые шаги API/Selenium) + 3 (Pre-flight Active Verification Polling) = 7
    return 7 + wallet_steps


def estimate_steps_for_close() -> int:
    """Расчет шагов для закрытия профиля."""
    return 3


def estimate_steps_for_status() -> int:
    """Расчет шагов для проверки статуса."""
    return 3


def estimate_steps_for_restart(settings: dict[str, Any] | None = None) -> int:
    """
    Расчет шагов для перезапуска профиля.
    Включает закрытие, умное ожидание (Smart Close Polling) и последующий запуск.
    """
    # Выделяем 5 "весовых" шагов для фазы Smart Close Polling,
    # чтобы прогресс-бар в UI заполнялся плавно во время опроса API.
    smart_close_polling_steps = 5
    
    return estimate_steps_for_close() + smart_close_polling_steps + estimate_steps_for_open(settings)