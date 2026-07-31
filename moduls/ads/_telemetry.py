"""
Модуль: moduls/ads/_telemetry.py
Назначение: Автономный конвейер телеметрии (Autonomous Telemetry Pipeline - ATP).
Зона ответственности: Изолированный фоновый опрос локального API AdsPower для
                      получения списка активных профилей (O(1) сложность).
                      Работает в выделенном потоке ОС (QThread) со своим циклом
                      событий asyncio, полностью обходя глобальный лимитер 1 RPS.
Интеграция: Слой L1 (Logic). Не имеет зависимостей от GUI. Общается с главным
            потоком исключительно через потокобезопасные сигналы Qt (QueuedConnection).
            Является частью плоского пакета `moduls/ads/`.
"""

import asyncio
from typing import Any

import aiohttp
from PySide6.QtCore import QObject, QThread, Signal, Slot

# Строгие абсолютные импорты ядра
from system.logger import logger
from core.core import load_settings_from_registry

# Интервал опроса радара (в секундах).
# 2.5 сек - идеальный баланс между мгновенной отзывчивостью UI и нулевой нагрузкой на CPU.
TELEMETRY_INTERVAL: float = 2.5


class TelemetryWorker(QObject):
    """
    Воркер телеметрии. Живет в изолированном потоке.
    Крутит свой собственный event loop и держит независимую сессию aiohttp,
    чтобы не толкаться в очередях с основными Selenium-воркерами.
    """
    # Сигнал передает set[str] с активными ID, либо None, если API недоступен
    profiles_updated = Signal(object)
    
    def __init__(self) -> None:
        super().__init__()
        self._is_stopped: bool = False
    
    @Slot()
    def run_loop(self) -> None:
        """
        Точка входа для QThread. Создает и запускает изолированный цикл asyncio.
        """
        logger.info(
            "[ATP] Радар телеметрии запущен в изолированном потоке. Начинаем сканирование эфира...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        
        # Создаем новый цикл событий для текущего (фонового) потока ОС
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            loop.run_until_complete(self._async_poll())
        except Exception as e:
            logger.error(
                f"[ATP] Критический сбой в цикле телеметрии: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
        finally:
            # Resource Guard: Гарантированное закрытие цикла и очистка памяти
            try:
                # Отменяем все незавершенные задачи перед закрытием
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            except Exception as cleanup_err:
                logger.warning(
                    f"[ATP] Ошибка при очистке цикла телеметрии: {cleanup_err}",
                    profile_names=["GLOBAL"], category="SYSTEM"
                )
            
            logger.info(
                "[ATP] Радар телеметрии успешно остановлен. Эфир чист.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
    
    async def _async_poll(self) -> None:
        """Асинхронный бесконечный цикл опроса API."""
        # Используем собственную сессию, чтобы не делить _api_lock с основным клиентом
        timeout = aiohttp.ClientTimeout(total=5.0)
        
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while not self._is_stopped:
                    await self._poll_once(session)
                    
                    # Дробим сон на микро-интервалы для мгновенной реакции на stop()
                    # Это предотвращает зависание приложения при закрытии окна
                    slept = 0.0
                    while slept < TELEMETRY_INTERVAL and not self._is_stopped:
                        await asyncio.sleep(0.25)
                        slept += 0.25
        except Exception as e:
            logger.error(
                f"[ATP] Ошибка инициализации aiohttp сессии радара: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
    
    async def _poll_once(self, session: aiohttp.ClientSession) -> None:
        """Единичный акт сканирования локального API AdsPower."""
        settings = load_settings_from_registry()
        api_url = settings.get("api_url", "").strip().rstrip("/")
        
        if not api_url:
            self.profiles_updated.emit(None)
            return
        
        if not api_url.startswith(("http://", "https://")):
            api_url = "http://" + api_url
        
        # Тот самый секретный эндпоинт, возвращающий срез всех активных профилей за O(1)
        endpoint = f"{api_url}/api/v1/browser/local-active"
        
        try:
            async with session.get(endpoint) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("code") == 0:
                        # Парсим список активных профилей
                        active_list = data.get("data", {}).get("list", [])
                        active_ids: set[str] = set()
                        
                        for item in active_list:
                            uid = str(item.get("user_id", "")).strip()
                            if uid:
                                active_ids.add(uid)
                        
                        # Отправляем чистый DTO (множество) в главный поток GUI
                        self.profiles_updated.emit(active_ids)
                    else:
                        # API ответил, но с ошибкой бизнес-логики
                        self.profiles_updated.emit(None)
                else:
                    # HTTP ошибка (например, 500 Internal Server Error)
                    self.profiles_updated.emit(None)
        
        except (aiohttp.ClientError, asyncio.TimeoutError):
            # AdsPower выключен, недоступен или завис
            self.profiles_updated.emit(None)
        except Exception as e:
            # Непредвиденная ошибка (например, битый JSON)
            logger.warning(
                f"[ATP] Радар поймал помехи при расшифровке ответа: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            self.profiles_updated.emit(None)
    
    def stop(self) -> None:
        """Мягкая остановка воркера (Kill Switch)."""
        self._is_stopped = True


class TelemetryThread(QThread):
    """
    Управляющий класс для потока телеметрии.
    Инкапсулирует логику создания воркера и его перемещения в изолированный QThread.
    """
    
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.worker = TelemetryWorker()
        
        # Магия Qt: перемещаем воркер в новый поток ОС.
        # Теперь все слоты воркера будут выполняться вне главного потока GUI.
        self.worker.moveToThread(self)
        
        # При старте потока автоматически запускаем цикл воркера
        self.started.connect(self.worker.run_loop)
    
    def stop(self) -> None:
        """
        Жесткий протокол остановки (Graceful Shutdown).
        Гарантирует отсутствие зомби-потоков и утечек памяти при закрытии приложения.
        """
        if self.isRunning():
            self.worker.stop()
            self.quit()
            # Ждем максимум 2 секунды до принудительного убийства потока,
            # чтобы не повесить процесс закрытия окна
            self.wait(2000)