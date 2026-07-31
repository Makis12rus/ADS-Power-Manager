"""
Модуль: gui/main_window_presenter.py
Назначение: Не-визуальный контроллер главного окна (Presentation Logic Layer).
Зона ответственности: Управление системными таймерами (Watchdog, SharedTicker),
                      координация фонового потока Оракула Газа (GasPriceWorker),
                      управление жизненным циклом радара телеметрии (ATP),
                      выполнение протокола Graceful Shutdown (зачистка процессов) и
                      оркестрация Авто-онбординга (Auto-Discovery Engine).
Интеграция: Выступает посредником (Mediator) между ядром и MainWindow.
            Не импортирует графические виджеты (QWidget, QMainWindow), общаясь
            с интерфейсом исключительно через потокобезопасные сигналы.
            Очищен от логики SSCP для снижения нагрузки на Event Loop.
"""

import asyncio
import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal, Slot

# Строгие абсолютные импорты ядра
from core.core import (
    ping_watchdog, get_gas_prices_async, format_gas_string,
    APP_NAME, APP_VERSION, save_ui_geometry,
    load_settings_from_registry, save_api_url
)
from core._constants import ADSPOWER_DEFAULT_PORTS
from system.logger import logger, log_action
from core.style import shared_ticker

# Строгие импорты изолированных процессоров
from moduls.ads._process_manager import local_driver_orphan_sweeper
from moduls.ads._telemetry import TelemetryThread
from moduls.ads._api_client import async_scan_local_ports
from moduls.ads.ads_logic import get_profiles_and_log


class GasSignals(QObject):
    """
    Потокобезопасный канал для передачи котировок газа из фонового воркера.
    Изолирован в отдельный класс для чистоты пространства имен сигналов.
    """
    # Передает: dict[str, float | None] (цены), str | None (ошибка)
    gas_loaded = Signal(object, object)


class MainWindowPresenter(QObject):
    """
    Мозг главного окна. Берет на себя всю рутину, не связанную с отрисовкой пикселей.
    Управляет фоновыми тредами, таймерами, транзакционным сохранением состояния
    и первичной разведкой портов (Auto-Onboarding).
    """
    
    # --- СИГНАЛЫ (КОНТРАКТ МЕДИАТОРА) ---
    # Команда для MainWindow обновить текст заголовка (цены на газ)
    titleUpdated = Signal(str)
    
    # Сигналы конвейера Авто-онбординга
    startupConnectionTested = Signal(list, list)  # Успех: передает (profiles, logs)
    showOnboardingRequested = Signal()            # Провал: требует показать модальное окно таможни
    
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        
        # 1. Радар телеметрии (ATP)
        self.telemetry_thread = TelemetryThread(self)
        
        # 2. Watchdog Heartbeat Timer (Локальный пульс)
        # Отвязан от сетевых запросов газа для предотвращения ложных срабатываний
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(5000)
        self._watchdog_timer.timeout.connect(ping_watchdog)
        
        # 3. Gas Oracle (Оракул котировок)
        self._gas_interval: int = 30
        self._gas_counter: int = self._gas_interval
        self._gas_timer = QTimer(self)
        self._gas_timer.setInterval(1000)
        self._gas_timer.timeout.connect(self._update_gas_timer)
        
        self._gas_query_active: bool = False
        self._last_gas_title_text: str | None = None
        
        self.gas_signals = GasSignals()
        self.gas_signals.gas_loaded.connect(self._on_gas_loaded_mainthread)
    
    # ===================== PUBLIC API =====================
    
    def start_systems(self) -> None:
        """
        Запуск всех фоновых систем. Вызывается из MainWindow после полной сборки UI.
        """
        logger.info(
            "Презентер: Включаем питание радара телеметрии и запускаем пульс Watchdog...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        self.telemetry_thread.start()
        self._watchdog_timer.start()
        self._gas_timer.start()
        
        # Запускаем бригадный метроном анимаций (светодиоды и спинеры)
        shared_ticker.start()
        
        self._refresh_gas()
        
        # Запускаем фоновую разведку портов (Auto-Discovery Engine)
        threading.Thread(
            target=self._startup_probe_worker,
            daemon=True,
            name="StartupProbeThread"
        ).start()
    
    def shutdown_systems(
            self,
            main_geo_b64: str,
            log_geo_b64: str,
            log_side: str,
            log_visible: bool
    ) -> None:
        """
        Протокол Graceful Shutdown.
        Останавливает треды, выжигает сиротские процессы и атомарно сохраняет состояние.

        :param main_geo_b64: Base64 строка геометрии главного окна.
        :param log_geo_b64: Base64 строка геометрии дока логов.
        :param log_side: Сторона прилипания дока ("left" или "right").
        :param log_visible: Флаг видимости дока логов.
        """
        logger.info(
            "Презентер: Отключаем питание радара телеметрии и пульс Watchdog...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        self._watchdog_timer.stop()
        self._gas_timer.stop()
        
        # Останавливаем глобальный таймер анимаций
        shared_ticker.stop()
        
        self.telemetry_thread.stop()
        
        logger.info(
            "Презентер: Завершение работы. Вызываем локального санитара...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        try:
            local_driver_orphan_sweeper()
        except Exception as e:
            logger.error(
                f"Сбой при зачистке процессов: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
        
        # Атомарное сохранение геометрии (строго в ветку State)
        save_ui_geometry(
            main_geometry_b64=main_geo_b64,
            logdock_geometry_b64=log_geo_b64,
            logdock_side=log_side,
            logdock_visible=log_visible
        )
    
    # ===================== AUTO-DISCOVERY ENGINE =====================
    
    @log_action("Авто-разведка AdsPower", category="SYSTEM")
    def _startup_probe_worker(self) -> None:
        """
        Фоновый воркер для авто-онбординга.
        Проверяет сохраненный URL, если глухо — спускает асинхронных ищеек по портам.
        Работает в изолированном потоке, не блокируя Event Loop.
        """
        settings = load_settings_from_registry()
        api_url = settings.get("api_url", "").strip()
        
        # 1. Пробуем постучаться по сохраненному URL
        if api_url:
            profiles, logs = get_profiles_and_log(api_url)
            if profiles:
                logger.info(
                    "AdsPower найден по сохраненному адресу. Запускаем конвейер.",
                    profile_names=["GLOBAL"], category="SYSTEM"
                )
                self.startupConnectionTested.emit(profiles, logs)
                return
                
        # 2. Если глухо — спускаем ищеек (Auto-Discovery Engine)
        logger.warning(
            "AdsPower не отвечает по старому адресу. Начинаем сканирование портов...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        
        # Запускаем асинхронный сканер в новом потоке (безопасно, так как тут нет своего Event Loop)
        found_url = asyncio.run(async_scan_local_ports(ADSPOWER_DEFAULT_PORTS))
        
        if found_url:
            # Атомарно сохраняем новый порт в реестр
            save_api_url(found_url)
            profiles, logs = get_profiles_and_log(found_url)
            
            if profiles:
                logger.success(
                    f"Авто-разведка успешна! Новый адрес {found_url} сохранен в сейф.",
                    profile_names=["GLOBAL"], category="SYSTEM"
                )
                self.startupConnectionTested.emit(profiles, logs)
                return
                
        # 3. Если порты молчат — вызываем таможню (Onboarding Dialog)
        logger.error(
            "Разведка провалилась. AdsPower выключен или использует нестандартный порт.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        self.showOnboardingRequested.emit()

    # ===================== GAS ORACLE LOGIC =====================
    
    def _update_gas_timer(self) -> None:
        """Тик таймера оракула газа. Обновляет счетчик в заголовке."""
        self._gas_counter -= 1
        if self._gas_counter <= 0:
            self._gas_counter = self._gas_interval
            self._refresh_gas()
        else:
            self._update_title()
    
    def _refresh_gas(self) -> None:
        """Инициирует асинхронный запрос котировок газа в фоновом потоке."""
        if self._gas_query_active:
            return
        self._gas_query_active = True
        self._update_title(loading=True)
        
        # Запускаем воркер, передавая коллбэк, который дернет сигнал
        get_gas_prices_async(lambda p, e: self.gas_signals.gas_loaded.emit(p, e))
    
    @Slot(object, object)
    def _on_gas_loaded_mainthread(self, prices: dict[str, float | None], error: str | None) -> None:
        """Слот для приема данных от фонового воркера газа в главном потоке."""
        self._gas_query_active = False
        if not error:
            self._last_gas_title_text = format_gas_string(prices, readable=True)
        self._update_title()
    
    def _update_title(self, loading: bool = False) -> None:
        """Формирует новую строку заголовка и отправляет её в MainWindow."""
        spaces = " " * 50
        suffix = f"({self._gas_counter}c)"
        
        if loading:
            mid = "Загрузка цен газа..."
        elif self._last_gas_title_text:
            mid = self._last_gas_title_text
        else:
            mid = ""
        
        new_title = f"{APP_NAME} v{APP_VERSION}{spaces}{mid}    {suffix}"
        self.titleUpdated.emit(new_title)