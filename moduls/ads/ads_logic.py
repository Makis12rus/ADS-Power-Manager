"""
Модуль: moduls/ads/ads_logic.py
Назначение: Генеральный Фасад (API Gateway) конвейера автоматизации AdsPower.
Зона ответственности: Реэкспорт функций из плоского пакета `moduls/ads/` для
                      обеспечения 100% обратной совместимости с GUI-слоем.
                      Оркестрация высокоуровневых операций (open, close, restart, hot_unlock)
                      с применением Smart Polling для предотвращения Race Conditions.
Интеграция: Является единой точкой входа для графических панелей. Скрывает
            всю внутреннюю декомпозицию (Selenium, HTTP-клиенты, DOM-парсеры).
"""

import json
import threading
from typing import Any

# Строгие абсолютные импорты ядра
from core.core import load_settings_from_registry, LavaMoatJITPatcher
from system.logger import logger

# =============================================================================
# 1. ИМПОРТЫ ИЗ ИЗОЛИРОВАННЫХ ПОДМОДУЛЕЙ (DECOMPOSED LOGIC)
# =============================================================================
# После ликвидации папки logic/ все файлы лежат в одном плоском пакете

from ._utils import (
    OperationCancelled,
    ProgressCB,
    _sleep_or_cancel,
    _progress,
    estimate_steps_for_open,
    estimate_steps_for_close,
    estimate_steps_for_status,
    estimate_steps_for_restart,
)

from ._api_client import (
    AdsAsyncClient,
    get_groups_and_log,
    build_group_index,
    get_profiles_and_log,
    _request_ads_api,
    _normalize_base_url,
)

from ._process_manager import (
    emergency_stop_selenium,
    validate_driver_version,
)

from ._wallet_unlocker import (
    unlock_wallets_for_profile,
)

# =============================================================================
# 2. ПУБЛИЧНЫЙ КОНТРАКТ (API GATEWAY EXPORTS)
# =============================================================================

__all__ = [
    "open_profile",
    "close_profile",
    "get_profile_status",
    "restart_profile",
    "hot_unlock_profile",
    "emergency_stop_selenium",
    "get_profiles_and_log",
    "build_group_index",
    "AdsAsyncClient",
    "OperationCancelled",
    "estimate_steps_for_open",
    "estimate_steps_for_close",
    "estimate_steps_for_status",
    "estimate_steps_for_restart",
]


# =============================================================================
# 3. ВЫСОКОУРОВНЕВЫЕ ОРКЕСТРАТОРЫ (PIPELINE CONTROLLERS)
# =============================================================================

def open_profile(
    user_id: str | int,
    name: str,
    logger_func: Any | None = None,
    api_url: str | None = None,
    progress_cb: ProgressCB = None,
    cancel_event: threading.Event | None = None,
    perform_automation: bool = True
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Оркестратор холодного запуска профиля.
    Связывает воедино: AdsPower API -> Pre-flight Polling -> Проверку драйвера -> LavaMoatJITPatcher -> Selenium Воркер.
    """
    settings = load_settings_from_registry()
    base = _normalize_base_url(api_url or settings.get("api_url", ""))
    p_name = name.strip() if name else "Профиль"
    
    try:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Отменено пользователем")
        
        # 1. Запрос к API на запуск браузера (БЕЗ ПАТЧЕРА, чистый старт)
        url = f"{base}/api/v1/browser/start?user_id={user_id}&open_tabs=0&ip_tab=0"
        code, data = _request_ads_api(url, timeout=30.0)
        
        js = json.loads(data.decode("utf-8"))
        if code == 200 and js.get("code") == 0:
            ws = js.get("data", {}).get("ws", {}).get("selenium")
            driver_path = js.get("data", {}).get("webdriver")
            selenium_data = {"ws": ws, "driver_path": driver_path}
            
            # 2. Pre-flight Active Verification (Fail-Fast)
            # Проверяем, что процесс Chrome действительно выжил после старта (например, не упал из-за прокси)
            max_start_attempts = 8
            is_active = False
            
            for attempt in range(max_start_attempts):
                if cancel_event and cancel_event.is_set():
                    raise OperationCancelled("Отменено пользователем")
                
                status = get_profile_status(user_id, p_name, logger_func, api_url, progress_cb, cancel_event)
                if status == "Active":
                    is_active = True
                    break
                    
                _progress(progress_cb, f"{p_name}: Проверка активности {attempt + 1}/{max_start_attempts}", cancel_event)
                _sleep_or_cancel(1.0, cancel_event)
            
            if not is_active:
                msg = "Браузер аварийно закрылся при старте (краш процесса или ошибка прокси)"
                logger.error(msg, profile_names=[p_name], category="PROFILE")
                return False, msg, None
            
            # 3. Предполетная проверка драйвера
            if perform_automation or (ws and driver_path):
                if not validate_driver_version(driver_path):
                    logger.warning(
                        f"Драйвер {driver_path} не прошел проверку. Автоматизация может не работать.",
                        profile_names=[p_name], category="SYSTEM"
                    )
            
            # 4. Передача управления в цех автоматизации кошельков
            if perform_automation:
                if settings.get("auto_unlock_wallets", "1") == "1":
                    try:
                        try:
                            import selenium
                            _SELENIUM_AVAILABLE = True
                        except ImportError:
                            _SELENIUM_AVAILABLE = False
                        
                        if ws and driver_path and _SELENIUM_AVAILABLE:
                            logger.info(
                                f"[DEBUG] [ads_logic] Инициализировали сессию для профиля {p_name}, отправляем воркера бурить DOM-формы кошельков...",
                                profile_names=[p_name], category="PROFILE"
                            )
                            
                            is_metamask_enabled = settings.get("unlock_metamask_enabled", "1") == "1"
                            
                            if is_metamask_enabled:
                                # Транзакционный патчинг ТОЛЬКО на время работы Selenium
                                with LavaMoatJITPatcher(str(user_id)):
                                    unlock_wallets_for_profile(
                                        ws, driver_path, p_name, str(user_id),
                                        settings, progress_cb, cancel_event
                                    )
                            else:
                                # Запуск без патчера для остальных кошельков
                                unlock_wallets_for_profile(
                                    ws, driver_path, p_name, str(user_id),
                                    settings, progress_cb, cancel_event
                                )
                    except OperationCancelled:
                        raise
                    except Exception as e:
                        logger.error(f"Сбой автоматизации кошельков: {e}", profile_names=[p_name], category="WALLET")
                        return False, str(e), None
                else:
                    logger.info(
                        f"Авто-разблокировка глобально выключена (Мастер-тумблер). Пропускаем запуск Selenium для {p_name}.",
                        profile_names=[p_name], category="PROFILE"
                    )
            
            return True, "Открыт", selenium_data
        else:
            msg = js.get("msg", f"HTTP {code}")
            logger.error(f"Не удалось открыть: {msg}", profile_names=[p_name], category="PROFILE")
            return False, msg, None
    
    except OperationCancelled:
        return False, "Отменено пользователем", None
    except Exception as ex:
        return False, str(ex), None


def hot_unlock_profile(
    user_id: str | int,
    name: str,
    target_plugin_ids: list[str] | None = None,
    logger_func: Any | None = None,
    api_url: str | None = None,
    progress_cb: ProgressCB = None,
    cancel_event: threading.Event | None = None
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Оркестратор горячей разблокировки (Снайперский режим).
    Подключается к УЖЕ ЗАПУЩЕННОМУ профилю и точечно разблокирует выбранные кошельки.
    Использует LavaMoatJITPatcher только если цель — MetaMask.
    """
    settings = load_settings_from_registry()
    base = _normalize_base_url(api_url or settings.get("api_url", ""))
    p_name = name.strip() if name else "Профиль"

    try:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Отменено пользователем")

        # Запрашиваем данные активной сессии (AdsPower не перезапустит браузер, а просто вернет порты)
        url = f"{base}/api/v1/browser/start?user_id={user_id}&open_tabs=0&ip_tab=0"
        code, data = _request_ads_api(url, timeout=15.0)

        js = json.loads(data.decode("utf-8"))
        if code == 200 and js.get("code") == 0:
            ws = js.get("data", {}).get("ws", {}).get("selenium")
            driver_path = js.get("data", {}).get("webdriver")
            selenium_data = {"ws": ws, "driver_path": driver_path}

            if not ws or not driver_path:
                msg = "AdsPower не вернул порты отладки. Профиль точно запущен?"
                logger.error(msg, profile_names=[p_name], category="PROFILE")
                return False, msg, None

            try:
                import selenium
                _SELENIUM_AVAILABLE = True
            except ImportError:
                _SELENIUM_AVAILABLE = False

            if _SELENIUM_AVAILABLE:
                targets_str = ", ".join(target_plugin_ids) if target_plugin_ids else "ВСЕ"
                logger.info(
                    f"[DEBUG] [ads_logic] Горячее подключение к {p_name}. Цели: {targets_str}. Высылаем отряд снайперов...",
                    profile_names=[p_name], category="PROFILE"
                )
                
                if target_plugin_ids and "metamask" in target_plugin_ids:
                    # Транзакционный патчинг на лету перед открытием вкладки кошелька
                    with LavaMoatJITPatcher(str(user_id)):
                        unlock_wallets_for_profile(
                            ws, driver_path, p_name, str(user_id),
                            settings, progress_cb, cancel_event,
                            target_plugin_ids=target_plugin_ids
                        )
                else:
                    unlock_wallets_for_profile(
                        ws, driver_path, p_name, str(user_id),
                        settings, progress_cb, cancel_event,
                        target_plugin_ids=target_plugin_ids
                    )
            else:
                logger.error("Selenium не установлен!", profile_names=[p_name], category="SYSTEM")
                return False, "Selenium missing", None

            return True, "Горячая разблокировка завершена", selenium_data
        else:
            msg = js.get("msg", f"HTTP {code}")
            logger.error(f"Не удалось получить порты для горячего старта: {msg}", profile_names=[p_name], category="PROFILE")
            return False, msg, None

    except OperationCancelled:
        return False, "Отменено пользователем", None
    except Exception as ex:
        return False, str(ex), None


def close_profile(
    user_id: str | int,
    name: str,
    logger_func: Any | None = None,
    api_url: str | None = None,
    progress_cb: ProgressCB = None,
    cancel_event: threading.Event | None = None
) -> tuple[bool, str]:
    """
    Оркестратор закрытия профиля через синхронный API.
    """
    settings = load_settings_from_registry()
    base = _normalize_base_url(api_url or settings.get("api_url", ""))
    p_name = name.strip() if name else "Профиль"
    
    try:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Отменено пользователем")
        
        code, data = _request_ads_api(f"{base}/api/v1/browser/stop?user_id={user_id}", timeout=30.0)
        
        js = json.loads(data.decode("utf-8"))
        if code == 200 and js.get("code") == 0:
            return True, "Закрыт"
        else:
            msg = js.get("msg", f"HTTP {code}")
            logger.error(f"Ошибка закрытия: {msg}", profile_names=[p_name], category="PROFILE")
            return False, msg
    
    except OperationCancelled:
        return False, "Отменено пользователем"
    except Exception as ex:
        return False, str(ex)


def get_profile_status(
    user_id: str | int,
    profile_name: str | None = None,
    logger_func: Any | None = None,
    api_url: str | None = None,
    progress_cb: ProgressCB = None,
    cancel_event: threading.Event | None = None
) -> str:
    """
    Оркестратор проверки статуса профиля через синхронный API.
    """
    settings = load_settings_from_registry()
    base = _normalize_base_url(api_url or settings.get("api_url", ""))
    p_name = profile_name or str(user_id)
    
    try:
        if cancel_event and cancel_event.is_set():
            return "Cancelled"
        
        code, data = _request_ads_api(f"{base}/api/v1/browser/active?user_id={user_id}", timeout=10.0)
        
        js = json.loads(data.decode("utf-8"))
        if code == 200 and js.get("code") == 0:
            return js.get("data", {}).get("status", "")
        
        msg = js.get("msg", "Unknown")
        logger.error(f"Ошибка статуса: {msg}", profile_names=[p_name], category="PROFILE")
        return f"Error API: {msg}"
    
    except OperationCancelled:
        return "Cancelled"
    except Exception as ex:
        return f"Error: {ex}"


def restart_profile(
    user_id: str | int,
    name: str,
    logger_func: Any | None = None,
    api_url: str | None = None,
    progress_cb: ProgressCB = None,
    cancel_event: threading.Event | None = None,
    perform_automation: bool = True
) -> tuple[bool, str, dict[str, Any] | None]:
    """
    Оркестратор перезапуска профиля.
    Выполняет закрытие, использует Smart Close Polling для ожидания освобождения файлов ОС,
    и запускает профиль заново.
    """
    p_name = name.strip() if name else "Профиль"
    
    try:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Отменено пользователем")
        
        # 1. Закрываем профиль
        ok_close, msg_close = close_profile(user_id, p_name, None, api_url, progress_cb, cancel_event)
        if not ok_close and msg_close == "Отменено пользователем":
            return False, msg_close, None
        
        # 2. Smart Close Polling (Умное ожидание закрытия)
        # Ждем, пока процесс Chrome реально умрет и отпустит LevelDB LOCK файлы
        max_close_attempts = 15
        is_closed = False
        
        for attempt in range(max_close_attempts):
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Отменено пользователем")
            
            status = get_profile_status(user_id, p_name, logger_func, api_url, progress_cb, cancel_event)
            
            # Если статус Inactive, Closed или API вернул ошибку (иногда бывает при полном закрытии)
            if status in ("Inactive", "Closed") or status.startswith("Error API:"):
                is_closed = True
                break
                
            _progress(progress_cb, f"{p_name}: Ожидание закрытия {attempt + 1}/{max_close_attempts}", cancel_event)
            _sleep_or_cancel(1.0, cancel_event)
            
        if not is_closed:
            logger.warning(
                f"Профиль {p_name} не закрылся штатно за {max_close_attempts}с. Попробуем запустить всё равно...",
                profile_names=[p_name], category="PROFILE"
            )
        
        # 3. Запускаем профиль заново
        return open_profile(
            user_id, p_name, None, api_url, progress_cb,
            cancel_event, perform_automation=perform_automation
        )
    
    except OperationCancelled:
        return False, "Отменено пользователем", None
    except Exception as ex:
        return False, str(ex), None