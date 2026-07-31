"""
Модуль: moduls/ads/profile_execution_engine.py
Назначение: Изолированный движок многопоточности и асинхронности (Concurrency Engine).
Зона ответственности: Управление пулами потоков (ThreadPoolExecutor) для Selenium,
                      асинхронное массовое закрытие профилей (aiohttp), высокопроизводительное
                      зондирование прокси (Proxy Probe Engine с замером HTTP RTT), расчет прогресса,
                      маршрутизация микро-статусов и реализация протокола экстренной
                      остановки (Kill Switch).
Интеграция: Слой Logic / Orchestration. Вызывается из `profile_presenter.py`.
            Не имеет зависимостей от графических виджетов. Общается с внешним
            миром исключительно через сигналы PySide6, передавая строгие
            состояния (ProfileState) вместо магических строк.
"""

import asyncio
import gc
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Tuple, List

from PySide6.QtCore import QObject, Signal

# Строгие абсолютные импорты ядра
from core.core import load_settings_from_registry
from core._constants import ProfileState
from system.logger import logger, log_action

# Строгие относительные импорты внутри плоского пакета ADS
from .ads_logic import (
    open_profile,
    close_profile,
    restart_profile,
    hot_unlock_profile,
    emergency_stop_selenium,
    OperationCancelled
)
from ._api_client import AdsAsyncClient, check_proxy_connection


class ProfileExecutionEngine(QObject):
    """
    Бригада мотористов. Управляет тяжелыми фоновыми задачами, пулами потоков
    и асинхронными очередями. Не знает о существовании графического интерфейса.
    """
    
    # --- СИГНАЛЫ (КОНТРАКТ МЕДИАТОРА) ---
    # Передает: flat_idx, ProfileState (enum), tooltip_text
    updateStatusSignal = Signal(int, object, str)
    # Передает: flat_idx, uid, ip_address, country_code, latency_ms (для Proxy Probe Engine и синхронизации кэша)
    updateProxySignal = Signal(int, str, str, str, int)
    
    progressSignal = Signal(int)  # percentage (0-100)
    stageSignal = Signal(str)  # text description
    updateStatsSignal = Signal(int, int, int)  # done_steps, success_count, error_count
    allTasksFinished = Signal()  # Уведомление о завершении батча
    
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        
        # Механизмы синхронизации и отмены (Kill Switch)
        self._stop_event = threading.Event()
        self._executor: ThreadPoolExecutor | None = None
        
        # Потокобезопасные счетчики для прогресс-бара
        self._stats_lock = threading.Lock()
        self._micro_total_steps: int = 0
        self._micro_done_steps: int = 0
        self._suc_count: int = 0
        self._err_count: int = 0
    
    # ===================== PUBLIC API =====================
    
    def run_mass_action(
            self,
            mode: str,
            targets: List[Tuple[Any, ...]],
            plugin_ids: List[str] | None = None
    ) -> None:
        """
        Инициализация массовой операции (open, close, restart, hot_unlock, check_proxy).
        Запускает диспетчер в фоновом потоке, чтобы не блокировать вызывающий поток.

        :param mode: Режим работы ("open", "close", "restart", "hot_unlock", "check_proxy").
        :param targets: Список кортежей с данными профилей (flat_idx, uid, name, [proxy_url]).
        :param plugin_ids: Список ID плагинов (только для режима hot_unlock).
        """
        if not targets:
            return
        
        self._stop_event.clear()
        
        # Сброс статистики перед новым батчем
        with self._stats_lock:
            self._micro_total_steps = len(targets)
            self._micro_done_steps = 0
            self._suc_count = 0
            self._err_count = 0
        
        self.updateStatsSignal.emit(0, 0, 0)
        self.progressSignal.emit(0)
        
        # Запускаем диспетчер в изолированном потоке ОС
        threading.Thread(
            target=self._mass_worker,
            args=(mode, targets, plugin_ids),
            daemon=True,
            name=f"MassWorker_{mode}"
        ).start()
    
    def stop_all(self) -> None:
        """
        Аварийная остановка конвейера (Panic Stop / Kill Switch).
        Мгновенно прерывает очередь задач и выжигает зависшие процессы через API AdsPower.
        """
        # 1. Устанавливаем событие отмены для всех активных воркеров
        self._stop_event.set()
        
        # 2. Очищаем очередь пула потоков (отменяем ожидающие задачи)
        if self._executor:
            # cancel_futures=True доступно в Python 3.9+, мгновенно очищает очередь
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        
        # 3. Принудительно убиваем драйверы через API AdsPower
        emergency_stop_selenium()
        
        logger.warning(
            "АВАРИЙНАЯ ОСТАНОВКА: Завершение процессов и очистка очередей...",
            profile_names=["GLOBAL"], category="PROFILE"
        )
        self.stageSignal.emit("⛔ ОСТАНОВКА...")
    
    # ===================== INTERNAL DISPATCHER =====================
    
    def _mass_worker(
            self,
            mode: str,
            targets: List[Tuple[Any, ...]],
            plugin_ids: List[str] | None = None
    ) -> None:
        """Диспетчер задач. Выбирает стратегию выполнения (Async vs ThreadPool)."""
        with logger.block(f"Массовая операция {mode} ({len(targets)} шт.)", category="PROFILE"):
            if mode == "close":
                # Легкие сетевые операции выполняем через асинхронный движок
                asyncio.run(self._run_async_batch(mode, targets))
            elif mode == "check_proxy":
                # Зондирование прокси также идет через асинхронный движок (Zero-RAM)
                asyncio.run(self._run_proxy_check_batch(targets))
            else:
                # Тяжелую автоматизацию отправляем в пул системных потоков
                self._run_selenium_batch(mode, targets, plugin_ids)
    
    # ===================== ASYNC ENGINE (LIGHTWEIGHT) =====================
    
    async def _run_async_batch(self, mode: str, targets: List[Tuple[Any, ...]]) -> None:
        """Асинхронное выполнение легких операций (закрытие) через aiohttp."""
        settings = load_settings_from_registry()
        api_url = settings.get("api_url", "")
        
        # Ограничиваем количество одновременных HTTP-запросов, чтобы не положить локальный API
        sem = asyncio.Semaphore(50)
        
        async def worker(flat: int, uid: str, name: str) -> None:
            if self._stop_event.is_set():
                return
            
            async with sem:
                if self._stop_event.is_set():
                    return
                
                self.stageSignal.emit(f"{mode.capitalize()}: {name}")
                
                ok = False
                if mode == "close":
                    self.updateStatusSignal.emit(flat, ProfileState.TRANS_BUSY, "Закрытие профиля...")
                    
                    # Вызов асинхронного клиента
                    ok, msg = await client.close_profile(uid, api_url)
                    
                    if not self._stop_event.is_set():
                        if ok:
                            self.updateStatusSignal.emit(flat, ProfileState.AWAITING_CLOSE, "Ожидание завершения процесса...")
                        else:
                            self.updateStatusSignal.emit(flat, ProfileState.ERR_API, f"Ошибка API: {msg}")
                
                # Потокобезопасное обновление статистики
                if not self._stop_event.is_set():
                    self._increment_stats(ok)
        
        async with AdsAsyncClient() as client:
            tasks = []
            for target in targets:
                # Безопасная распаковка (DTO может содержать 3 или 4 элемента)
                flat, uid, name = target[:3]
                tasks.append(worker(flat, uid, name))
            
            await asyncio.gather(*tasks)
        
        self._finish_mass_action()

    async def _run_proxy_check_batch(self, targets: List[Tuple[Any, ...]]) -> None:
        """
        Асинхронное зондирование прокси-каналов (Proxy Probe Engine).
        Выполняется без запуска браузеров, экономя гигабайты ОЗУ.
        """
        # Жесткий лимит на 30 одновременных SOCKS-соединений для защиты от Port Exhaustion
        sem = asyncio.Semaphore(30)
        
        logger.info(
            f"Отправляем асинхронные зонды на проверку {len(targets)} прокси-каналов...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )

        async def worker(flat: int, uid: str, name: str, proxy_url: str) -> None:
            if self._stop_event.is_set():
                return
                
            async with sem:
                if self._stop_event.is_set():
                    return
                    
                self.stageSignal.emit(f"Зондирование: {name}")
                self.updateStatusSignal.emit(flat, ProfileState.TRANS_BUSY, "Проверка прокси-канала...")
                
                if not proxy_url:
                    self.updateStatusSignal.emit(flat, ProfileState.ERR_APP, "Нет настроек прокси")
                    self._increment_stats(False)
                    return
                    
                # Вызов трехконтурного зонда с замером HTTP RTT
                is_alive, ip_or_err, country, latency = await check_proxy_connection(proxy_url)
                
                if not self._stop_event.is_set():
                    if is_alive:
                        # Точечно обновляем ГЕО-данные и пинг в модели, UI и глобальном кэше (SSOT)
                        self.updateProxySignal.emit(flat, uid, ip_or_err, country, latency)
                        self.updateStatusSignal.emit(flat, ProfileState.ACTIVE, f"Прокси жив (IP: {ip_or_err}, Пинг: {latency}мс)")
                        self._increment_stats(True)
                    else:
                        self.updateStatusSignal.emit(flat, ProfileState.ERR_API, f"Прокси мертв: {ip_or_err}")
                        self._increment_stats(False)

        tasks = []
        for target in targets:
            flat, uid, name = target[:3]
            proxy_url = target[3] if len(target) > 3 else ""
            tasks.append(worker(flat, uid, name, proxy_url))
            
        await asyncio.gather(*tasks)
        self._finish_mass_action()
    
    # ===================== THREAD POOL ENGINE (HEAVYWEIGHT) =====================
    
    def _run_selenium_batch(
            self,
            mode: str,
            targets: List[Tuple[Any, ...]],
            plugin_ids: List[str] | None = None
    ) -> None:
        """Выполнение тяжелых операций (Selenium) через ThreadPoolExecutor."""
        settings = load_settings_from_registry()
        d_start = float(settings.get("delay_start", 5))
        d_stop = float(settings.get("delay_stop", 1))
        
        try:
            pool_size = int(settings.get("selenium_pool", "3"))
        except Exception:
            pool_size = 3
        
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, pool_size),
            thread_name_prefix="SelWorker"
        )
        
        for i, target in enumerate(targets):
            if self._stop_event.is_set():
                break
            
            flat, uid, name = target[:3]
            
            self.stageSignal.emit(f"Очередь: {name} ({i + 1}/{len(targets)})")
            self.updateStatusSignal.emit(flat, ProfileState.THROTTLED, "Ожидание в очереди...")
            
            self._executor.submit(
                self._sync_profile_task,
                mode, flat, uid, name, settings, plugin_ids
            )
            
            # Throttled Concurrency: Фоновый диспетчер выдерживает паузу между отправкой задач.
            # Это плавно распределяет нагрузку на CPU/RAM и прокси-серверы при массовом запуске.
            if mode != "hot_unlock" and i < len(targets) - 1 and not self._stop_event.is_set():
                delay = d_start if mode == "open" else d_stop
                # Используем wait у события отмены для прерываемого сна
                self._stop_event.wait(delay)
        
        if self._executor:
            self._executor.shutdown(wait=True)
        
        self._finish_mass_action()
    
    def _sync_profile_task(
            self,
            mode: str,
            flat: int,
            uid: str,
            name: str,
            settings: dict[str, Any],
            plugin_ids: List[str] | None = None
    ) -> None:
        """
        Синхронная задача для ThreadPoolExecutor (Selenium).
        Вызывает фасад AdsLogic, обновляет статус, обрабатывает ошибки.
        """
        if self._stop_event.is_set():
            return
        
        action_name = {
            "open": "Запуск",
            "close": "Закрытие",
            "restart": "Перезапуск",
            "hot_unlock": "Горячее бурение"
        }.get(mode, "Обработка")
        
        self.updateStatusSignal.emit(flat, ProfileState.TRANS_BUSY, f"{action_name}...")
        
        ok = False
        try:
            with logger.block(f"{action_name} {name}", profile_names=[name], category="PROFILE"):
                if self._stop_event.is_set():
                    raise OperationCancelled("Отменено пользователем")
                
                cb = self._make_progress_cb(flat)
                
                # Маршрутизация в зависимости от режима
                if mode == "open":
                    ok, msg, _ = open_profile(
                        uid, name, None, None, cb,
                        cancel_event=self._stop_event, perform_automation=True
                    )
                elif mode == "restart":
                    ok, msg, _ = restart_profile(
                        uid, name, None, None, cb,
                        cancel_event=self._stop_event, perform_automation=True
                    )
                elif mode == "close":
                    ok, msg = close_profile(
                        uid, name, None, None, cb,
                        cancel_event=self._stop_event
                    )
                elif mode == "hot_unlock":
                    ok, msg, _ = hot_unlock_profile(
                        uid, name, plugin_ids, None, None, cb,
                        cancel_event=self._stop_event
                    )
                else:
                    raise RuntimeError(f"Неизвестный режим работы: {mode}")
                
                if not ok:
                    # Эскалируем ошибку для фиксации в logger.block
                    raise RuntimeError(msg)
                    
                # Формирование финального статуса при успехе
                if mode == "close":
                    self.updateStatusSignal.emit(flat, ProfileState.AWAITING_CLOSE, "Ожидание завершения процесса...")
                else:
                    self.updateStatusSignal.emit(flat, ProfileState.ACTIVE, f"{action_name} успешно завершен")
        
        except OperationCancelled:
            self.updateStatusSignal.emit(flat, ProfileState.CLOSED, "Операция отменена пользователем")
            ok = False
        except Exception as e:
            err_msg = str(e)
            # Семантическое разделение ошибок для правильной окраски диода (Красный vs Оранжевый)
            if "API" in err_msg or "HTTP" in err_msg:
                state = ProfileState.ERR_API
            else:
                state = ProfileState.ERR_APP
                
            self.updateStatusSignal.emit(flat, state, f"Ошибка: {err_msg}")
            ok = False
        
        # Потокобезопасное обновление статистики
        if not self._stop_event.is_set() or ok:
            self._increment_stats(ok)
    
    # ===================== UTILS & HELPERS =====================
    
    def _make_progress_cb(self, flat: int) -> Callable[[str], None]:
        """
        Создает коллбэк для передачи микро-шагов из Selenium в UI.
        Обновляет как глобальную стадию, так и всплывающую подсказку конкретной карточки.
        """
        def cb(text: str) -> None:
            self.stageSignal.emit(text)
            self.updateStatusSignal.emit(flat, ProfileState.TRANS_BUSY, text)
        
        return cb
    
    def _increment_stats(self, success: bool) -> None:
        """Потокобезопасное обновление счетчиков и эмиссия сигнала."""
        with self._stats_lock:
            if success:
                self._suc_count += 1
            else:
                self._err_count += 1
            self._micro_done_steps += 1
            
            done = self._micro_done_steps
            suc = self._suc_count
            err = self._err_count
            total = max(1, self._micro_total_steps)
        
        self.updateStatsSignal.emit(done, suc, err)
        
        pct = int((done / total) * 100)
        self.progressSignal.emit(min(99, pct))
    
    def _finish_mass_action(self) -> None:
        """Завершение массовой операции, отправка финальных сигналов и очистка ресурсов."""
        self.progressSignal.emit(100)
        self.stageSignal.emit("Готово")
        self.allTasksFinished.emit()
        
        with self._stats_lock:
            suc = self._suc_count
            err = self._err_count
        
        if self._stop_event.is_set():
            logger.warning(
                f"Операция остановлена. Успех: {suc}, Ошибок: {err}",
                profile_names=["GLOBAL"], category="PROFILE"
            )
        else:
            logger.info(
                f"Операция завершена. Успех: {suc}, Ошибок: {err}",
                profile_names=["GLOBAL"], category="PROFILE"
            )
        
        # Resource Guard: принудительная сборка мусора после массовой операции.
        # Гарантирует удаление отработавших объектов Selenium и освобождение памяти.
        gc.collect()