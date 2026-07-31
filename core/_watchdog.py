"""
Модуль: core/_watchdog.py
Назначение: Системный монитор жизнеспособности (AppWatchdog).
Зона ответственности: Фоновый контроль отзывчивости главного потока (GUI).
                      Если интерфейс перестает подавать признаки жизни (ping),
                      сторож безжалостно перезапускает процесс через os.execl,
                      спасая систему от глухого зависания и утечек памяти.
Интеграция: Слой L1. Зависит только от системных библиотек и логгера.
            Инжектируется в приложение через фасад core.py.
"""

import os
import sys
import time
import threading

from system.logger import logger, log_action


class AppWatchdog(threading.Thread):
    """
    Watchdog: следит, чтобы приложение регулярно подавало признаки жизни.
    Главный поток (GUI) обязан периодически дергать ping_watchdog().
    Если пинга нет слишком долго — рубим канат и перезапускаем процесс.
    """
    
    def __init__(self, check_interval: int = 30) -> None:
        super().__init__(daemon=True, name="WatchdogThread")
        # Минимальный интервал проверки — 5 секунд, чтобы не спамить процессор
        self._check_interval: int = max(5, int(check_interval))
        self._last_heartbeat: float = time.time()
        
        self._running: threading.Event = threading.Event()
        self._running.set()
        
        self._last_restart_log_ts: float = 0.0
    
    def heartbeat(self) -> None:
        """Обновление таймера жизни (вызывается из главного потока)."""
        self._last_heartbeat = time.time()
    
    def run(self) -> None:
        """Основной цикл сторожа."""
        while self._running.is_set():
            try:
                now = time.time()
                # Если с последнего пинга прошло больше двух интервалов — бьем тревогу
                if now - self._last_heartbeat > self._check_interval * 2:
                    if now - self._last_restart_log_ts > 10.0:
                        logger.error(
                            "Watchdog: Главный поток уснул летаргическим сном. Экстренный перезапуск матрицы...",
                            profile_names=["GLOBAL"],
                            category="SYSTEM"
                        )
                        self._last_restart_log_ts = now
                    
                    try:
                        # Жесткий перезапуск текущего процесса с теми же аргументами
                        os.execl(sys.executable, sys.executable, *sys.argv)
                    except Exception as e:
                        logger.error(
                            f"Watchdog: Ошибка при попытке сделать os.execl: {e}",
                            profile_names=["GLOBAL"],
                            category="SYSTEM"
                        )
                
                # Спим до следующей проверки
                time.sleep(self._check_interval)
            
            except Exception as ex:
                # Сторож не должен падать ни при каких обстоятельствах
                logger.error(
                    f"Watchdog: Внутренний сбой сторожа: {ex}",
                    profile_names=["GLOBAL"],
                    category="SYSTEM"
                )
                time.sleep(self._check_interval)
    
    def stop(self) -> None:
        """Мягкая остановка цикла сторожа."""
        self._running.clear()


# Глобальный инстанс сторожа
_watchdog: AppWatchdog | None = None


@log_action("Запуск Watchdog", category="SYSTEM")
def start_watchdog(interval: int = 30) -> None:
    """
    Инициализирует и запускает фоновый поток сторожа.
    """
    global _watchdog
    if _watchdog is None:
        _watchdog = AppWatchdog(check_interval=interval)
        _watchdog.start()


@log_action("Остановка Watchdog", category="SYSTEM")
def stop_watchdog() -> None:
    """
    Останавливает сторожа (вызывается при штатном закрытии приложения).
    """
    global _watchdog
    if _watchdog is not None:
        try:
            _watchdog.stop()
            _watchdog.join(timeout=1.5)
        except Exception as ex:
            logger.warning(
                f"Watchdog: Ошибка при остановке потока: {ex}",
                profile_names=["GLOBAL"],
                category="SYSTEM"
            )
        finally:
            _watchdog = None


def ping_watchdog() -> None:
    """
    Сигнал жизни. Должен регулярно вызываться из неблокирующего GUI-потока.
    """
    if _watchdog is not None:
        _watchdog.heartbeat()


def is_watchdog_active() -> bool:
    """
    Проверка статуса сторожа.
    """
    return _watchdog is not None