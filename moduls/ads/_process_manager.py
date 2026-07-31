"""
Модуль: moduls/ads/_process_manager.py
Назначение: Системный менеджер процессов и реестр активных веб-драйверов (Resource Guard).
Зона ответственности: Потокобезопасный учет запущенных сессий Selenium, проверка
                      целостности бинарников chromedriver, реализация протокола
                      экстренного глушения процессов (Kill Switch) и параллельная
                      зачистка сиротских процессов при выходе (Concurrent OS-Level Sweeper).
Интеграция: Вызывается из _wallet_unlocker.py (для регистрации) и из фасада
            ads_logic.py (для аварийной остановки). Изолирован от GUI.
            Является частью плоского пакета `moduls/ads/`.
"""

import os
import subprocess
import threading
import time
import urllib.request
from typing import Any

# Строгие абсолютные импорты ядра
from core.core import load_settings_from_registry
from system.logger import logger

# ======================= Global Driver Registry =======================

# Реестр активных сессий: хранит кортежи (driver_instance, user_id)
_ACTIVE_DRIVERS: list[tuple[Any, str]] = []
_DRIVERS_LOCK = threading.Lock()


def register_driver(driver: Any, user_id: str) -> None:
    """
    Потокобезопасная регистрация активного драйвера и ID профиля.
    Необходимо для возможности аварийной остановки (Kill Switch) при зависаниях.
    """
    with _DRIVERS_LOCK:
        # Защита от двойной регистрации одного и того же инстанса
        if not any(d == driver for d, _ in _ACTIVE_DRIVERS):
            _ACTIVE_DRIVERS.append((driver, str(user_id)))


def unregister_driver(driver: Any) -> None:
    """
    Потокобезопасное удаление драйвера из реестра.
    Вызывается воркером в блоке finally при штатном завершении работы.
    """
    with _DRIVERS_LOCK:
        for i, (d, uid) in enumerate(_ACTIVE_DRIVERS):
            if d == driver:
                _ACTIVE_DRIVERS.pop(i)
                break


# ======================= Kill Switch & Sweeper Protocols =======================

def emergency_stop_selenium() -> None:
    """
    Аварийная остановка процессов AdsPower (Zero-Loss Kill Switch).
    Вызывается ИСКЛЮЧИТЕЛЬНО при нажатии кнопки "СТОП" в интерфейсе (Panic Stop).

    ВНИМАНИЕ: Мы намеренно не вызываем driver.quit() из этого потока, чтобы
    избежать зависания сокетов (CLOSE_WAIT) и крашей C-библиотек Selenium.
    Вместо этого мы отправляем жесткую API-команду на закрытие браузеров,
    а воркеры сами поймают OperationCancelled и закроют свои драйверы.
    """
    with _DRIVERS_LOCK:
        if not _ACTIVE_DRIVERS:
            return
        
        settings = load_settings_from_registry()
        api_url = settings.get("api_url", "").strip().rstrip("/")
        if api_url and not api_url.startswith(("http://", "https://")):
            api_url = "http://" + api_url
        
        if not api_url:
            logger.error(
                "Аварийный стоп невозможен: не задан API URL AdsPower.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            _ACTIVE_DRIVERS.clear()
            return
        
        logger.warning(
            f"Инициирован протокол экстренной остановки для {len(_ACTIVE_DRIVERS)} сессий...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        
        for driver, user_id in _ACTIVE_DRIVERS:
            if user_id:
                try:
                    # Прямой синхронный вызов. В режиме паники мы игнорируем
                    # глобальный лимитер 1 RPS, чтобы мгновенно выжечь процессы.
                    url = f"{api_url}/api/v1/browser/stop?user_id={user_id}"
                    urllib.request.urlopen(url, timeout=2.0)
                    
                    # КРИТИЧНО: Микро-задержка для защиты от DDoS-атаки на локальный API.
                    # Если юзер нажал СТОП на 50 профилях, одновременный залп из 50 запросов
                    # приведет к исчерпанию TCP-портов (Port Exhaustion) на Windows.
                    time.sleep(0.1)
                except Exception as e:
                    logger.warning(
                        f"Не удалось пристрелить процесс профиля {user_id} через API: {e}",
                        profile_names=[user_id], category="SYSTEM"
                    )
        
        _ACTIVE_DRIVERS.clear()
        logger.info(
            "Аварийный протокол завершен. Реестр активных сессий очищен.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )


def local_driver_orphan_sweeper() -> None:
    """
    Локальный санитар (Concurrent OS-Level Sweeper).
    Сканирует дерево процессов ОС и точечно выжигает сиротские процессы chromedriver,
    порожденные текущим приложением. Не трогает сами браузеры Chrome (AdsPower).
    Использует параллельное ожидание (psutil.wait_procs) для мгновенного закрытия.
    """
    try:
        import psutil
    except ImportError:
        logger.warning(
            "Библиотека psutil не установлена. Локальная зачистка сиротских драйверов пропущена.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        return

    current_pid = os.getpid()
    orphans = []

    logger.info(
        "Вызываем локального санитара для зачистки сиротских chromedriver'ов...",
        profile_names=["GLOBAL"], category="SYSTEM"
    )

    # Фаза 1: Сбор и мягкое глушение (SIGTERM)
    for proc in psutil.process_iter(['pid', 'ppid', 'name']):
        try:
            # Проверяем, что процесс порожден нами (PPID совпадает с нашим PID)
            if proc.info['ppid'] == current_pid:
                name = str(proc.info['name']).lower()
                # Ищем именно драйверы, чтобы случайно не убить что-то полезное
                if "chromedriver" in name:
                    proc.terminate()
                    orphans.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception as e:
            logger.warning(
                f"Ошибка при попытке terminate процесса {proc.info.get('pid')}: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )

    if not orphans:
        logger.info(
            "Сиротских процессов не обнаружено. В оперативной памяти чисто.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        return

    # Фаза 2: Параллельное ожидание (Concurrent Wait)
    # Ждем завершения всех процессов разом, а не по очереди. Максимум 3 секунды на всех.
    gone, alive = psutil.wait_procs(orphans, timeout=3.0)

    # Фаза 3: Контрольный выстрел для выживших зомби (SIGKILL)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        except Exception as e:
            logger.warning(
                f"Не удалось добить процесс {proc.pid}: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )

    killed_count = len(gone) + len(alive)
    logger.success(
        f"Санитар успешно вычистил {killed_count} сиротских процессов драйвера. ОЗУ спасена.",
        profile_names=["GLOBAL"], category="SYSTEM"
    )


# ======================= Pre-flight Checks =======================

def validate_driver_version(driver_path: str) -> bool:
    """
    Предполетная проверка (Pre-flight Check).
    Проверяет, существует ли бинарник chromedriver и способен ли он запуститься,
    предотвращая падение пула потоков из-за битых файлов AdsPower.
    """
    if not driver_path or not os.path.exists(driver_path):
        logger.error(
            f"WebDriver не найден по пути: {driver_path}. Кажется, AdsPower забыл его скачать.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        return False
    
    try:
        # Запускаем драйвер с флагом --version для проверки его жизнеспособности
        result = subprocess.run(
            [driver_path, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=3.0,
            check=False
        )
        if result.returncode == 0:
            return True
        else:
            logger.warning(
                f"WebDriver check failed (code {result.returncode}). Драйвер приболел или несовместим с ОС.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            return False
    except subprocess.TimeoutExpired:
        logger.error(
            "WebDriver завис при проверке версии (Timeout).",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        return False
    except Exception as e:
        logger.error(
            f"Критическая ошибка проверки WebDriver: {e}",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        return False