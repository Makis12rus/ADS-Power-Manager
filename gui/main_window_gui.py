"""
Модуль: gui/main_window_gui.py
Назначение: Главная диспетчерская (Presentation Layer & Mediator) приложения.
Зона ответственности: Координация визуальных панелей (ADS, AUTO, Logs), управление
                      геометрией окон, переключение режимов и маршрутизация сигналов.
                      Реализует автономный рендеринг фонов (Bake and Blit),
                      протокол Graceful Shutdown при закрытии и Авто-онбординг
                      (Centered Modal Dialog) при холодном старте.
                      Полностью совместим с политикой Gutter Isolation (стабильные скроллбары).
                      Включает глобальный Viewport Hit-Test Protocol для сброса выделения
                      и маршрутизацию сигналов синхронизации кэша (Event-Driven Cache Coherence).
Интеграция: Выступает Посредником (Mediator) между изолированными UI-компонентами
            (ModeBar, StickyDock, InfoPanel) и контроллером (MainWindowPresenter).
            Строго изолирует сетевые и дисковые вызовы от главного потока Qt.
            Взаимодействует с `AdsProfilePanel` через Duck Typing для обратной совместимости.
            Использует архитектуру Master-Slave и Perfect Alignment Math для управления
            независимым окном логов и модальным окном онбординга.
"""

import sys
import json
import base64
import threading
import asyncio
from typing import Any

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QPushButton, QHBoxLayout, QVBoxLayout,
    QApplication, QMessageBox, QDialog, QLabel
)
from PySide6.QtCore import Qt, QEvent, Signal, QByteArray, QRect, QPoint, QTimer
from PySide6.QtGui import QGuiApplication, QResizeEvent, QMoveEvent, QMouseEvent

# Строгие абсолютные импорты ядра
from core.core import (
    APP_NAME, load_settings_from_registry, save_settings_to_registry,
    load_ui_geometry, import_cache_dict, save_api_url
)
from core._constants import ADSPOWER_DEFAULT_PORTS
from system.logger import logger
from core.style import Styles, Colors, StaticVolumetricBackdropWidget, GlassTile, DebossedLineEdit

# Импорты изолированных UI-компонентов и Презентера
from gui.sticky_dock import StickyDock
from gui.mode_bar import ModeBar
from gui.info_panel import InfoPanel
from gui.main_window_presenter import MainWindowPresenter

# Импорты панелей бизнес-логики (строго через фасады)
from moduls.ads.ads_gui import AdsProfilePanel, AdsSettingsPanel
from moduls.auto.auto_gui import AutoPanel
from moduls.ads.ads_log_gui import LogWindow
from moduls.ads.ads_logic import get_profiles_and_log
from moduls.ads._api_client import async_scan_local_ports


# ===================== Win32 TopMost Helpers =====================
# Используем ctypes для установки флага "поверх всех" без мерцания (SetWindowPos).
if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes
    
    _user32 = ctypes.windll.user32
    _SetWindowPos = _user32.SetWindowPos
    _SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT
    ]
    _SetWindowPos.restype = wintypes.BOOL
    
    HWND_TOPMOST = wintypes.HWND(-1)
    HWND_NOTOPMOST = wintypes.HWND(-2)
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    
    def _win32_set_topmost(hwnd_int: int, on: bool) -> bool:
        try:
            flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
            hwnd = wintypes.HWND(hwnd_int)
            target = HWND_TOPMOST if on else HWND_NOTOPMOST
            return bool(_SetWindowPos(hwnd, target, 0, 0, 0, 0, flags))
        except Exception:
            return False
else:
    def _win32_set_topmost(hwnd_int: int, on: bool) -> bool:
        return False


# ===================== MAIN WINDOW =====================

class MainWindow(QMainWindow):
    """
    Главный оркестратор интерфейса (View / Mediator).
    Управляет жизненным циклом панелей, геометрией и маршрутизацией сигналов.
    Делегирует фоновые системные задачи классу MainWindowPresenter.
    """
    # Сигнал для возврата данных из фонового потока проверки соединения
    connection_tested = Signal(list, list)
    
    def __init__(self) -> None:
        super().__init__()
        
        # Reentrancy Lock: Замок для предотвращения бесконечной рекурсии событий (Stack Overflow)
        # при взаимном выравнивании главного окна и StickyDock.
        self._is_aligning_log: bool = False
        
        # Флаг состояния склейки окон для магнитной стыковки
        self._is_log_snapped: bool = False
        self._log_snap_side: str = "right"
        self._stay_on_top: bool = False
        
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(950, 710)
        self.setMaximumWidth(950)
        self.setStyleSheet(Styles.MAIN_WINDOW)
        
        # Инициализация Презентера (Контроллера)
        self.presenter = MainWindowPresenter(self)
        self.presenter.titleUpdated.connect(self.setWindowTitle)
        
        self._setup_ui()
        self._connect_signals()
        
        # Восстановление состояния
        self._apply_saved_window_prefs()
        self._restore_ui_geometry()
        
        QTimer.singleShot(0, lambda: self._set_top_bar_enabled(self.mode_bar.get_mode() != "AUTO"))
        
        # Запуск фоновых систем через презентер (включая Auto-Discovery Engine)
        self.presenter.start_systems()
    
    def _setup_ui(self) -> None:
        """Сборка пользовательского интерфейса."""
        # === Main Layout (Premium PCB Engine) ===
        self.backdrop = StaticVolumetricBackdropWidget(self)
        self.setCentralWidget(self.backdrop)
        
        self.main_layout = QVBoxLayout(self.backdrop)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(12)
        
        # 1. Mode Bar (Изолированный компонент)
        self.mode_bar = ModeBar(self)
        self.main_layout.addWidget(self.mode_bar)
        
        # 2. ADS Top Bar
        self.top_bar_widget = QWidget()
        self.top_bar_widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        top_lay = QHBoxLayout(self.top_bar_widget)
        top_lay.setContentsMargins(0, 0, 0, 0)
        top_lay.setSpacing(6)
        
        self.dashboard_btn = self._create_top_btn("Профили")
        self.settings_btn = self._create_top_btn("Настройки")
        self.test_conn_btn = self._create_top_btn("Тест подключения")
        
        top_lay.addWidget(self.dashboard_btn)
        top_lay.addWidget(self.settings_btn)
        top_lay.addStretch(1)
        top_lay.addWidget(self.test_conn_btn)
        self.main_layout.addWidget(self.top_bar_widget)
        
        # 3. Content Stack
        self.panels = QWidget()
        self.panels.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.panels_layout = QVBoxLayout(self.panels)
        self.panels_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.panels)
        
        # Инициализация панелей (AdsProfilePanel теперь Passive View)
        self.profile_panel = AdsProfilePanel(self)
        self.settings_panel = AdsSettingsPanel(self)
        self.auto_panel = AutoPanel(self)
        self.info_panel = InfoPanel(self)
        
        self.panels_layout.addWidget(self.profile_panel)
        self.panels_layout.addWidget(self.settings_panel)
        self.panels_layout.addWidget(self.auto_panel)
        self.panels_layout.addWidget(self.info_panel)
        
        self.profile_panel.hide()
        self.settings_panel.hide()
        self.auto_panel.hide()
        self.info_panel.hide()
        self.panels.hide()
        
        # 4. Dock / Logs (Изолированный компонент)
        # КРИТИЧНО: Мы больше не используем addDockWidget. Окно логов — это независимый QWidget (Tool),
        # который летит рядом с главным окном, не блокируя его разметку (Layout Recursion Loop устранен).
        self.log_widget = LogWindow(self)
        self.log_dock = StickyDock("Логи", self)
        self.log_dock.setObjectName("LogDockWidget")
        self.log_dock.setWidget(self.log_widget)
        self.log_dock.hide()

    def _connect_signals(self) -> None:
        """Маршрутизация сигналов между изолированными компонентами (Mediator Pattern)."""
        # Навигация
        self.dashboard_btn.clicked.connect(self.show_profiles)
        self.settings_btn.clicked.connect(self.show_settings)
        self.test_conn_btn.clicked.connect(self.test_connection)
        self.connection_tested.connect(self._on_connection_tested)
        
        # Авто-онбординг (Сигналы от Презентера)
        self.presenter.startupConnectionTested.connect(self._on_connection_tested)
        self.presenter.showOnboardingRequested.connect(self.show_onboarding_dialog)
        
        # ModeBar
        self.mode_bar.modeChanged.connect(self._on_mode_changed)
        self.mode_bar.pinToggled.connect(self._on_pin_toggled)
        self.mode_bar.infoToggled.connect(self._on_info_toggled)
        self.mode_bar.logsToggled.connect(self._set_log_visible)
        
        # Индикация автосохранения и обновление темы
        self.settings_panel.saveStatusChanged.connect(self.mode_bar.save_indicator.set_state)
        self.settings_panel.themeUpdated.connect(self._on_theme_updated)
        self.auto_panel.settings_panel.saveStatusChanged.connect(self.mode_bar.save_indicator.set_state)
        
        # Горячий запуск (Targeted Hot Unlock)
        self.settings_panel.hotUnlockBatchRequested.connect(self.profile_panel.run_hot_unlock)
        
        # Телеметрия (ATP) и Синхронизация прокси
        self.presenter.telemetry_thread.worker.profiles_updated.connect(self.profile_panel.on_telemetry_update)
        self.presenter.telemetry_thread.worker.profiles_updated.connect(self.settings_panel.on_telemetry_update)
        self.profile_panel.forceTelemetrySignal.connect(self._force_telemetry_ping)
        
        # Мост: Proxy Probe Engine -> Hot Automation Panel (Live-перекраска флагов)
        self.profile_panel.execution_engine.updateProxySignal.connect(self.settings_panel.on_proxy_updated)
        
        # StickyDock
        self.log_dock.alignmentRequested.connect(self.align_log_window)
        self.log_dock.snapSideChanged.connect(self.set_log_snap_side)
        self.log_dock.unsnapped.connect(self.set_log_unsnapped)
        self.log_dock.visibilityChanged.connect(self.mode_bar.set_logs_active)

    def _create_top_btn(self, text: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setProperty("class", "mass-action")
        btn.setStyleSheet(Styles.BTN_ACTION)
        return btn
    
    # --- Geometry Helpers ---
    
    def _qbytearray_to_b64(self, ba: QByteArray) -> str:
        try:
            return str(ba.toBase64().data(), encoding='ascii')
        except Exception:
            return ""
    
    def _b64_to_qbytearray(self, b64: str) -> QByteArray:
        try:
            return QByteArray.fromBase64(b64.encode('ascii'))
        except Exception:
            return QByteArray()
    
    def _ensure_on_screen(self) -> None:
        try:
            rect = self.frameGeometry()
            screens = QGuiApplication.screens()
            if not any(rect.intersects(s.availableGeometry()) for s in screens):
                geo = QGuiApplication.primaryScreen().availableGeometry()
                self.move(geo.x() + 80, geo.y() + 80)
        except Exception:
            pass
    
    def _restore_ui_geometry(self) -> None:
        """Восстановление геометрии окон и кэша профилей из реестра."""
        ui = load_ui_geometry()
        main_b64 = ui.get("main_geometry", "")
        log_b64 = ui.get("logdock_geometry", "")
        self._log_snap_side = (ui.get("logdock_side", "right") or "right").lower()
        
        if main_b64:
            ba = self._b64_to_qbytearray(main_b64)
            if not ba.isEmpty():
                self.restoreGeometry(ba)
        
        self._ensure_on_screen()
        
        should_show_log = (ui.get("logdock_visible", "1") == "1")
        if log_b64:
            ba = self._b64_to_qbytearray(log_b64)
            if not ba.isEmpty():
                self.log_dock.restoreGeometry(ba)
        
        if should_show_log:
            QTimer.singleShot(0, self._place_log_dock_initial)
            
        # State Hydration (Cold Start Fix)
        cache_b64 = ui.get("profile_metadata_cache_b64", "")
        if cache_b64:
            try:
                cache_json = base64.b64decode(cache_b64).decode('utf-8')
                cache_dict = json.loads(cache_json)
                import_cache_dict(cache_dict)
                logger.info(
                    "Кэш профилей успешно гидратирован. Имена восстановлены.",
                    profile_names=["GLOBAL"], category="SYSTEM"
                )
            except Exception as e:
                logger.warning(
                    f"Кэш профилей поврежден или пуст, начинаем с чистого листа: {e}",
                    profile_names=["GLOBAL"], category="SYSTEM"
                )
    
    def _place_log_dock_initial(self) -> None:
        self.log_dock.setVisible(True)
        self.align_log_window()
        self.mode_bar.set_logs_active(True)
    
    def set_log_snap_side(self, side: str) -> None:
        self._log_snap_side = side
        
    def set_log_unsnapped(self) -> None:
        self._is_log_snapped = False
    
    # --- Logic ---
    
    def _apply_saved_window_prefs(self) -> None:
        s = load_settings_from_registry()
        mode = s.get("active_mode", "ADS").upper()
        self.mode_bar.set_mode(mode)
        self._apply_mode(mode, initial=True)
        
        on_top = (s.get("stay_on_top", "0") == "1")
        self.mode_bar.set_pin(on_top)
        self._apply_stay_on_top(on_top)
    
    def _on_mode_changed(self, mode: str) -> None:
        self._apply_mode(mode)
        self.mode_bar.set_info_active(False)
        s = load_settings_from_registry()
        s["active_mode"] = mode
        save_settings_to_registry(s)
        logger.info(f"Режим: {mode}", profile_names=["GLOBAL"], category="SYSTEM")
    
    def _apply_mode(self, mode: str, initial: bool = False) -> None:
        self.info_panel.hide()
        if mode == "AUTO":
            self.top_bar_widget.setVisible(False)
            self._set_top_bar_enabled(False)
            self.profile_panel.hide()
            self.settings_panel.hide()
            self.panels.show()
            self.auto_panel.show()
        else:
            self.top_bar_widget.setVisible(True)
            self._set_top_bar_enabled(True)
            self.auto_panel.hide()
            self.panels.show()
            self.show_profiles()
        self.align_log_window()
    
    def _apply_stay_on_top(self, on: bool) -> None:
        self._stay_on_top = on
        
        if sys.platform == "win32" and _win32_set_topmost(int(self.winId()), on):
            # На Windows применяем нативный SetWindowPos без разрушения оконных дескрипторов
            if self.log_dock.isVisible():
                _win32_set_topmost(int(self.log_dock.winId()), on)
        else:
            # Кроссплатформенный запасной план через Qt-флаги
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            self.log_dock.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
            
            # КРИТИЧНО: Принудительно восстанавливаем флаг прозрачности, так как
            # setWindowFlag разрушает нативный дескриптор окна и сбрасывает атрибуты WA_!
            self.log_dock.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
            
            self.show()
            if self.log_dock.isVisible():
                self.log_dock.show()
        
        self.mode_bar.set_pin(on)
    
    def _on_pin_toggled(self, on: bool) -> None:
        self._apply_stay_on_top(on)
        s = load_settings_from_registry()
        s["stay_on_top"] = "1" if on else "0"
        save_settings_to_registry(s)
    
    def _set_top_bar_enabled(self, enabled: bool) -> None:
        self.dashboard_btn.setEnabled(enabled)
        self.settings_btn.setEnabled(enabled)
        self.test_conn_btn.setEnabled(enabled)
    
    # --- Panel Switching ---
    
    def show_profiles(self) -> None:
        self.auto_panel.hide()
        self.info_panel.hide()
        self.settings_panel.hide()
        self.profile_panel.show()
    
    def show_settings(self) -> None:
        self.auto_panel.hide()
        self.info_panel.hide()
        self.profile_panel.hide()
        self.settings_panel.show()
        
    def _on_info_toggled(self, active: bool) -> None:
        if active:
            self.top_bar_widget.setVisible(False)
            self._set_top_bar_enabled(False)
            self.profile_panel.hide()
            self.settings_panel.hide()
            self.auto_panel.hide()
            self.panels.show()
            self.info_panel.show()
        else:
            self._apply_mode(self.mode_bar.get_mode())
    
    # --- Logs & Magnetic Snapping ---
    
    def _set_log_visible(self, visible: bool) -> None:
        if visible:
            self.log_dock.show()
            # Синхронизируем статус "поверх всех" для вновь открытого окна логов
            self._apply_stay_on_top(self._stay_on_top)
            self.align_log_window()
        else:
            self.log_dock.hide()
            self.set_log_unsnapped()
    
    def align_log_window(self) -> None:
        """
        Выравнивает окно логов относительно главного окна (Магнитная стыковка).
        Защищено Reentrancy Lock (_is_aligning_log) для предотвращения Stack Overflow.
        Реализует жесткую диктатуру геометрии: ведомое окно мгновенно принимает высоту тягача.
        Использует Perfect Alignment Math для стыковки внешних рамок (Frame Geometry).
        """
        if self._is_aligning_log:
            return
            
        if not self.log_dock.isVisible():
            return
            
        self._is_aligning_log = True
        try:
            gap = 10
            main_geo = self.frameGeometry()
            w_dock = self.log_dock.width()
            
            # 1. Вычисляем целевые координаты для внешней рамки
            target_x = main_geo.x() + main_geo.width() + gap if self._log_snap_side == "right" else main_geo.x() - w_dock - gap
            target_y = main_geo.top()
            target_frame_height = main_geo.height()
            
            # 2. Вычисляем толщину шапки окна логов (оверхед системной рамки)
            log_frame_overhead = max(0, self.log_dock.frameGeometry().height() - self.log_dock.geometry().height())
            
            # 3. Вычисляем требуемую внутреннюю высоту логов
            target_inner_height = max(100, target_frame_height - log_frame_overhead)
            
            # 4. Применяем координаты и размеры раздельно
            self.log_dock.move(target_x, target_y)
            self.log_dock.resize(w_dock, target_inner_height)
            
            self._is_log_snapped = True
        finally:
            self._is_aligning_log = False
            
    def _on_theme_updated(self) -> None:
        """
        Принудительное обновление текстуры фона при изменении настроек кастомизации.
        Вызывается по сигналу от AdsSettingsPanel.
        """
        self.backdrop.refresh_theme()
        if hasattr(self.log_widget, 'backdrop') and self.log_widget.backdrop:
            self.log_widget.backdrop.refresh_theme()
    
    # --- Events ---
    
    def mousePressEvent(self, event: QMouseEvent) -> None:
        """
        Глобальный перехватчик кликов по пустому пространству (Viewport Hit-Test Protocol).
        Если юзер кликает мимо активных элементов (кнопок, карточек), событие всплывает сюда.
        Мы ловим его и проверяем: если клик был вне карусели профилей, то сбрасываем выделение.
        Если клик был внутри карусели — мы туда не лезем, карусель разберется сама.
        """
        # КРИТИЧНО: Сначала передаем событие дочерним виджетам (например, FlatActionButton).
        # Если они его обработают (accept), наша логика сброса не помешает их работе.
        super().mousePressEvent(event)
        
        if event.button() == Qt.MouseButton.LeftButton:
            # Сбрасываем рамсы только если мы на вкладке профилей и есть что сбрасывать
            if self.profile_panel.isVisible() and self.profile_panel.recycler._selected_ids:
                recycler = self.profile_panel.recycler
                
                # --- ВЫПОЛНЯЕМ HIT-TESTING ---
                # Маппим глобальные координаты клика в локальную систему координат карусели
                local_pos = recycler.mapFromGlobal(event.globalPosition().toPoint())
                
                # Если клик пришелся на территорию карусели - мы туда не лезем. Карусель разберется сама.
                if recycler.rect().contains(local_pos):
                    return
                    
                # Если клик был мимо (на боковое меню, хедер и т.д.) - вызываем бронированный сброс
                recycler.clear_selection()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.isVisible() and not self.isMinimized():
            if self._is_log_snapped:
                self.align_log_window()
    
    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        if self.isVisible() and not self.isMinimized():
            if self._is_log_snapped:
                self.align_log_window()
    
    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isVisible() and not self.isMinimized():
                if self.log_dock.isVisible():
                    self.log_dock.showNormal()
                    if self._is_log_snapped:
                        self.align_log_window()
    
    def closeEvent(self, event: QEvent) -> None:
        """
        Протокол Graceful Shutdown.
        Атомарное сохранение геометрии окон и безопасная остановка фоновых тредов.
        """
        settings = load_settings_from_registry()
        
        # 1. Проверка активных воркеров и диалог-предохранитель
        # Используем Duck Typing для совместимости с новой архитектурой AdsProfilePanel
        is_running = getattr(self.profile_panel, '_mass_action_running', False)
        
        if is_running and settings.get("confirm_exit_on_active", "1") == "1":
            reply = QMessageBox.question(
                self,
                "Подтверждение выхода",
                "В данный момент выполняются автоматические сценарии.\n\n"
                "Вы действительно хотите закрыть ADSProfile Manager?\n"
                "Все запущенные браузеры останутся открытыми, но автоматизация будет остановлена.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.No:
                event.ignore()
                return

        # 2. ФАЗА СЛЕПКА СОСТОЯНИЯ (State Capture)
        mb = self._qbytearray_to_b64(self.saveGeometry())
        lb = self._qbytearray_to_b64(self.log_dock.saveGeometry())
        log_visible = self.log_dock.isVisible()

        # 3. ФАЗА МГНОВЕННОГО СКРЫТИЯ (Visual Vanishing)
        self.hide()
        self.log_dock.hide()
        
        QApplication.processEvents()

        # 4. Мягкая остановка воркеров (Silent Detach)
        if is_running:
            logger.info(
                "Пользователь запросил выход. Переводим активные потоки в режим тихой остановки...",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            # Безопасно взводим флаг отмены через проброшенное свойство
            stop_event = getattr(self.profile_panel, '_stop_event', None)
            if stop_event:
                stop_event.set()

        # 5. Принудительный сброс (Flush) всех in-memory черновиков настроек
        logger.info(
            "Синхронизация кэша. Сбрасываем недописанные черновики в сейф перед уходом...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        try:
            self.settings_panel.force_save()
            self.auto_panel.settings_panel.force_save()
        except Exception as e:
            logger.error(f"Сбой при автосохранении перед выходом: {e}", profile_names=["GLOBAL"], category="SYSTEM")

        # 6. Делегация аппаратного глушения презентеру
        self.presenter.shutdown_systems(mb, lb, self._log_snap_side, log_visible)
        
        event.accept()
    
    # --- API Tests & Onboarding ---
    
    def show_onboarding_dialog(self) -> None:
        """
        Вызов таможенного поста (Centered Modal Onboarding).
        Окно появляется строго по центру и блокирует интерфейс асинхронно (без nested event loops).
        """
        self.onboarding_dialog = AdsOnboardingDialog(self)
        self.onboarding_dialog.adjustSize()
        
        # Perfect Alignment Math (Идеальное центрирование)
        parent_geo = self.frameGeometry()
        dialog_geo = self.onboarding_dialog.frameGeometry()
        
        target_x = parent_geo.x() + (parent_geo.width() - dialog_geo.width()) // 2
        target_y = parent_geo.y() + (parent_geo.height() - dialog_geo.height()) // 2
        
        self.onboarding_dialog.move(target_x, target_y)
        
        # Подписываемся на успешное завершение онбординга
        self.onboarding_dialog.accepted.connect(self._on_onboarding_accepted)
        
        # Асинхронный вызов модального окна (Zero-Blocking)
        self.onboarding_dialog.open()

    def _on_onboarding_accepted(self) -> None:
        """Слот успешного завершения онбординга. Синхронизирует настройки и запускает тест."""
        # Принудительно обновляем панель настроек, чтобы она подхватила новый URL из реестра
        self.settings_panel.load_settings()
        self.test_connection()

    def test_connection(self) -> None:
        """Инициирует проверку соединения с AdsPower API в фоновом потоке."""
        self.settings_panel.force_save()

        url = self.settings_panel.get_api_url().strip()
        if not url:
            logger.error("API адрес не заполнен", profile_names=["GLOBAL"], category="API")
            return
        
        logger.start("Тест соединения...", profile_names=["GLOBAL"], category="API", force=True)
        self.profile_panel.set_progress_stage("Тест API...")
        self.profile_panel.set_progress(0)
        self._set_top_bar_enabled(False)
        
        threading.Thread(target=self._bg_test_connection, args=(url,), daemon=True).start()
    
    def _bg_test_connection(self, url: str) -> None:
        profiles, logs = get_profiles_and_log(url)
        self.connection_tested.emit(profiles, logs)
    
    def _on_connection_tested(self, profiles: list[dict[str, str]], logs: list[tuple[str, str]]) -> None:
        for msg, lvl in logs:
            logger.log(msg, level=lvl, profile_names=["GLOBAL"], category="API")
        
        self.profile_panel.set_progress(100)
        self.profile_panel.set_progress_stage("Готово!")
        
        if profiles:
            logger.success(f"Успех. Профилей: {len(profiles)}", profile_names=["GLOBAL"], category="API", force=True)
            self.profile_panel.update_profiles(profiles)
            
            # Операция "Чистый Радар": Автоматически запускаем асинхронный шмон прокси
            logger.info("Инициируем автоматическую проверку прокси-каналов...", profile_names=["GLOBAL"], category="SYSTEM")
            self.profile_panel.presenter.on_proxy_check_requested([])
        else:
            self.profile_panel.update_profiles([])
            logger.error("Профили не получены", profile_names=["GLOBAL"], category="API")
        
        QTimer.singleShot(1000, lambda: self.profile_panel.set_progress(0))
        QTimer.singleShot(1000, lambda: self.profile_panel.set_progress_stage(""))
        self._set_top_bar_enabled(True)
        
    def _force_telemetry_ping(self) -> None:
        logger.info(
            "Радар телеметрии работает в автоматическом фоновом режиме. Статусы обновятся в течение 2.5 секунд.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )


# ===================== ONBOARDING DIALOG =====================

class AdsOnboardingDialog(QDialog):
    """
    Таможенный пост (Centered Modal Onboarding).
    Появляется, если AdsPower не найден при старте. Оснащен асинхронным сканером портов.
    """
    scanFinished = Signal(object)  # Передает str (URL) или None
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        
        # Безрамочное окно с прозрачным фоном для отрисовки GlassTile
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        
        self._setup_ui()
        self.scanFinished.connect(self._on_scan_finished)
        
    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.tile = GlassTile(self, enable_hover=False)
        tile_layout = QVBoxLayout(self.tile)
        tile_layout.setContentsMargins(24, 24, 24, 24)
        tile_layout.setSpacing(16)
        
        title = QLabel("📡 Подключение к AdsPower")
        title.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {Colors.TXT_PRIMARY};")
        tile_layout.addWidget(title)
        
        desc = QLabel(
            "Программа не смогла автоматически найти запущенный AdsPower.\n"
            "Пожалуйста, запустите браузер и нажмите «Автодетекция»,\n"
            "или введите адрес локального API вручную."
        )
        desc.setStyleSheet(f"color: {Colors.TXT_SECONDARY}; font-size: 13px;")
        tile_layout.addWidget(desc)
        
        self.url_input = DebossedLineEdit()
        self.url_input.setPlaceholderText("http://local.adspower.com:50325")
        tile_layout.addWidget(self.url_input)
        
        btn_layout = QHBoxLayout()
        
        self.btn_cancel = QPushButton(" Отмена")
        self.btn_cancel.setStyleSheet(Styles.BTN_ACTION)
        self.btn_cancel.clicked.connect(self.reject)
        
        self.btn_detect = QPushButton(" 🔍 Автодетекция")
        self.btn_detect.setStyleSheet(Styles.BTN_ACTION)
        self.btn_detect.clicked.connect(self._run_scan)
        
        self.btn_apply = QPushButton(" Подключиться")
        self.btn_apply.setStyleSheet(Styles.BTN_HOT_RUN)
        self.btn_apply.clicked.connect(self._apply)
        
        btn_layout.addWidget(self.btn_cancel)
        btn_layout.addStretch(1)
        btn_layout.addWidget(self.btn_detect)
        btn_layout.addWidget(self.btn_apply)
        
        tile_layout.addLayout(btn_layout)
        layout.addWidget(self.tile)
        
    def _run_scan(self) -> None:
        """Запуск асинхронного сканера в изолированном потоке."""
        self.btn_detect.setEnabled(False)
        self.btn_apply.setEnabled(False)
        self.btn_detect.setText(" ⏳ Сканирование...")
        
        logger.info("Таможня дает добро на сканирование портов...", profile_names=["GLOBAL"], category="SYSTEM")
        
        def worker() -> None:
            res = asyncio.run(async_scan_local_ports(ADSPOWER_DEFAULT_PORTS))
            self.scanFinished.emit(res)
            
        threading.Thread(target=worker, daemon=True).start()
        
    def _on_scan_finished(self, res: str | None) -> None:
        """Обработка результатов сканирования."""
        self.btn_detect.setEnabled(True)
        self.btn_apply.setEnabled(True)
        self.btn_detect.setText(" 🔍 Автодетекция")
        
        if res:
            logger.success("Ищейки вернулись с добычей! Порт найден.", profile_names=["GLOBAL"], category="SYSTEM")
            self.url_input.setText(res)
            self.btn_apply.click()  # Автоматически применяем найденный порт
        else:
            logger.warning("Ищейки вернулись ни с чем. Проверьте AdsPower.", profile_names=["GLOBAL"], category="SYSTEM")
            self.url_input.setText("")
            self.url_input.setPlaceholderText("Порты молчат. Проверьте AdsPower.")
            
    def _apply(self) -> None:
        """Сохранение введенного адреса и закрытие диалога с кодом Accepted."""
        url = self.url_input.text().strip()
        if url:
            save_api_url(url)
            self.accept()