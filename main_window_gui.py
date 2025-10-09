# =========================
# 📝 Файл: main_window_gui.py
# =========================

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QPushButton, QHBoxLayout, QVBoxLayout, QLabel,
    QSpacerItem, QSizePolicy, QDockWidget, QButtonGroup, QToolButton, QTextEdit, QScrollArea
)
from PySide6.QtCore import Qt, QTimer, QEvent, Signal, QObject, QRect, QByteArray, QSize, QByteArray as QtQByteArray
from PySide6.QtGui import QIcon, QGuiApplication
from typing import Any

from ads_gui import AdsProfilePanel, AdsSettingsPanel
from auto_gui import AutoPanel  # заменили AntiDrainPanel на AutoPanel
from ads_logic import get_profiles_and_log  # только ADS-логика
from core import (
    get_gas_prices_async, format_gas_string, ping_watchdog, APP_NAME, APP_VERSION,
    load_settings_from_registry, save_settings_to_registry,
    load_ui_geometry, save_ui_geometry
)
from logger import logger

import sys
import base64

# ========= Специальная док-панель, которая всегда «плавающая» и прилипает к краям =========
class StickyDock(QDockWidget):
    def __init__(self, title: str, main_window: "MainWindow", gap: int = 10, snap_threshold: int = 60) -> None:
        super().__init__(title, main_window)
        self._main: MainWindow = main_window
        self._gap: int = max(0, int(gap))
        self._snap_threshold: int = max(0, int(snap_threshold))
        self.topLevelChanged.connect(self._force_floating)
        self.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.setFloating(True)
        self.setAllowedAreas(Qt.NoDockWidgetArea)

    def _force_floating(self, _: bool) -> None:
        if not self.isFloating():
            self.setFloating(True)
            self._main.align_log_window()

    def _maybe_snap_to_main(self) -> None:
        if not self.isFloating():
            return
        mw_geo: QRect = self._main.frameGeometry()
        dock_geo: QRect = self.frameGeometry()
        right_x = mw_geo.x() + mw_geo.width() + self._gap
        left_x = mw_geo.x() - dock_geo.width() - self._gap
        cur_x = dock_geo.x()
        dist_right = abs(cur_x - right_x)
        dist_left = abs(cur_x - left_x)
        if min(dist_left, dist_right) <= self._snap_threshold:
            side = "right" if dist_right <= dist_left else "left"
            self._main.set_log_snap_side(side)
            self._main.align_log_window()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._maybe_snap_to_main()

    def mouseReleaseEvent(self, event) -> None:
        super().mouseReleaseEvent(event)
        self._maybe_snap_to_main()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange or event.type() == QEvent.NonClientAreaMouseButtonDblClick:
            if not self.isFloating():
                self.setFloating(True)
                self._main.align_log_window()


# === Верхняя панель режимов (отдельный QWidget со своим стилем/фоном) ===
class ModeBar(QWidget):
    modeChanged = Signal(str)          # "ADS" | "AUTO"
    pinToggled = Signal(bool)          # True/False
    infoToggled = Signal(bool)         # True/False (показать/скрыть панель информации)
    logsRequested = Signal()           # запрос показа/скрытия окна логов

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_mode = "ADS"
        self._stay_on_top = False
        self._info_active = False

        # ВАЖНО: рисуем фон силой стилей, чтобы не было белого мигания
        self.setAttribute(Qt.WA_StyledBackground, True)

        # Фиксируем высоту подложки, чтобы в ADS и AUTO она была одинаковой
        self.setFixedHeight(50)
        self.setAutoFillBackground(True)

        self.setStyleSheet("""
            QWidget#ModeBar {
                background: #1E2124;
                border: 1px solid #2D3136;
                border-radius: 10px;
            }
            QPushButton[class="mode"] {
                background: #232629;
                color: #F0F0F0;
                border: 0.5px groove #5A5A5A;   /* возвращаем бордюр */
                border-radius: 8px;
                padding: 3px 12px;
                font-size: 15px;
                min-width: 70px;
                min-height: 25px;
                max-height: 25px;
            }
            QPushButton[class="mode"]:hover:!disabled {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFE066, stop:1 #FFB800);
                color: #232629;
                border: 1px solid #FFE066;
            }
            QPushButton[class="mode"]:checked {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFC300, stop:1 #FFD700);
                color: #232629;
                border: 1px solid #FFD700;
            }
            /* Кнопки-эмодзи (закреп/инфо/логи) */
            QToolButton[class="icon-btn"] {
                background: transparent;
                border: none;
                font-size: 14px;
                width: 30px;
                height: 30px;
                min-width: 30px;
                min-height: 30px;
                max-width: 30px;
                max-height: 30px;
                border-radius: 15px;
                padding: 0;
                margin-right: 0px;
            }
            QToolButton[class="icon-btn"]:hover {
                background: rgba(255,255,255,0.05);
            }
            QToolButton[class="icon-btn"]:checked {
                background: rgba(255,215,0,0.08);
                border: 0px solid #FFD700;
                border-radius: 15px;
            }
        """)
        self.setObjectName("ModeBar")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        # Группа взаимоисключаемых кнопок
        self.ads_btn = QPushButton("🧩 ADS")
        self.ads_btn.setCheckable(True)
        self.ads_btn.setProperty("class", "mode")

        self.auto_btn = QPushButton("🤖 Auto")
        self.auto_btn.setCheckable(True)
        self.auto_btn.setProperty("class", "mode")

        self.ads_btn.setChecked(True)

        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.ads_btn)
        group.addButton(self.auto_btn)

        lay.addWidget(self.ads_btn)
        lay.addWidget(self.auto_btn)

        lay.addStretch(1)  # всё, что справа — прижимаем к правому краю

        # --- Новая кнопка "Логи" ---
        self.logs_btn = QToolButton()
        self.logs_btn.setToolTip("Логи")
        self.logs_btn.setProperty("class", "icon-btn")
        self.logs_btn.setText("📃")
        # Важно: clicked(bool) => лямбда без аргументов
        self.logs_btn.clicked.connect(lambda: self.logsRequested.emit())
        lay.addWidget(self.logs_btn)

        # --- Кнопка "закреп" ---
        self.pin_btn = QToolButton()
        self.pin_btn.setCheckable(True)
        self.pin_btn.setToolTip("Поверх всех окон")
        self.pin_btn.setProperty("class", "icon-btn")
        self.pin_btn.setText("📌")
        lay.addWidget(self.pin_btn)

        # --- Кнопка "информация" ---
        self.info_btn = QToolButton()
        self.info_btn.setCheckable(True)
        self.info_btn.setToolTip("О программе и инструкция")
        self.info_btn.setProperty("class", "icon-btn")
        self.info_btn.setText("ℹ️")
        lay.addWidget(self.info_btn)

        # Сигналы
        self.ads_btn.toggled.connect(self._on_ads_toggled)
        self.auto_btn.toggled.connect(self._on_auto_toggled)
        self.pin_btn.toggled.connect(self._on_pin_clicked)
        self.info_btn.toggled.connect(self._on_info_clicked)

    # -- API внешнего управления/чтения состояния
    def set_mode(self, mode: str) -> None:
        mode = (mode or "ADS").upper()
        if mode == "AUTO":
            self.auto_btn.setChecked(True)
        else:
            self.ads_btn.setChecked(True)

    def get_mode(self) -> str:
        return "AUTO" if self.auto_btn.isChecked() else "ADS"

    def set_pin(self, on: bool) -> None:
        self._stay_on_top = bool(on)
        self.pin_btn.blockSignals(True)
        self.pin_btn.setChecked(on)
        self.pin_btn.blockSignals(False)

    def is_pin_on(self) -> bool:
        return bool(self._stay_on_top)

    def set_info_active(self, active: bool) -> None:
        self._info_active = bool(active)
        self.info_btn.blockSignals(True)
        self.info_btn.setChecked(active)
        self.info_btn.blockSignals(False)

    # -- внутренние обработчики
    def _on_ads_toggled(self, checked: bool) -> None:
        if checked:
            self._current_mode = "ADS"
            self.set_info_active(False)
            self.modeChanged.emit("ADS")

    def _on_auto_toggled(self, checked: bool) -> None:
        if checked:
            self._current_mode = "AUTO"
            self.set_info_active(False)
            self.modeChanged.emit("AUTO")

    def _on_pin_clicked(self, checked: bool) -> None:
        self._stay_on_top = bool(checked)
        self.pinToggled.emit(self._stay_on_top)

    def _on_info_clicked(self, checked: bool) -> None:
        self._info_active = bool(checked)
        self.infoToggled.emit(self._info_active)


# ========= Газ-сигналы =========
class GasSignals(QObject):
    gas_loaded = Signal(object, object)  # prices, error


# ===================== Панель «О программе и инструкция» =====================
class InfoPanel(QWidget):
    """
    Небольшая самостоятельная вкладка с описанием программы и пошаговой инструкцией.
    Никаких зависимостей от ADS/Auto. Только статический контент.
    """
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_StyledBackground, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)

        title = QLabel("ℹ️ О программе и краткая инструкция")
        title.setStyleSheet("font-size: 20px; font-weight: 700; color: #FFD700;")
        layout.addWidget(title)

        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setStyleSheet("""
            QTextEdit {
                background: #1E2124;
                color: #EDEDED;
                border: 1px solid #2D3136;
                border-radius: 12px;
                padding: 12px;
                font-size: 13px;
                line-height: 1.35em;
            }
        """)
        txt.setHtml(self._build_help_html())
        layout.addWidget(txt, stretch=1)

    def _build_help_html(self) -> str:
        return (
            "<h3>Что это?</h3>"
            "<p><b>ADSProfile Manager</b> — утилита для управления профилями AdsPower "
            "с удобной панелью логов и автоматизацией разблокировки популярных кошельков.</p>"
            "<h3>Быстрый старт</h3>"
            "<ol>"
            "<li>Откройте вкладку <b>Настройки</b> (кнопка вверху) и проверьте адрес API AdsPower.</li>"
            "<li>При необходимости укажите пароли кошельков — они сохраняются безопасно в Windows Credential Manager.</li>"
            "<li>Вернитесь на вкладку <b>Профили</b> и нажмите <b>Тест подключения</b>. Если всё ок — появится список профилей.</li>"
            "<li>Выделите нужные профили и используйте кнопки <b>Запустить</b> / <b>Перезапустить</b> / <b>Закрыть</b>.</li>"
            "<li>Статусы операций и ошибки всегда видны в правой панели логов. Фильтры логов — сверху.</li>"
            "</ol>"
            "<h3>Советы</h3>"
            "<ul>"
            "<li>Кнопка с булавкой вверху справа — закрепляет окно <i>поверх всех</i>.</li>"
            "<li>Массовые операции исполняются последовательно и показывают <i>микро-прогресс</i> каждого действия.</li>"
            "<li>Режимы <b>ADS</b> и <b>Auto</b> независимы — их логика и GUI не пересекаются. Общая только панель логов.</li>"
            "</ul>"
            "<h3>Где искать проблемы?</h3>"
            "<ul>"
            "<li>Проверьте адрес API и доступность AdsPower.</li>"
            "<li>Если используется автограф кошельков — убедитесь, что пароли заданы и расширения установлены.</li>"
            "</ul>"
            "<p style='color:#A0A0A0;'>Эта вкладка — справочная. Содержимое можно обновлять по мере роста функциональности.</p>"
        )


# =====================================================================

class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()

        # ВАЖНО: стилизованный фон сразу, чтобы избежать «белой вспышки»
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(950, 710)
        self.setMaximumWidth(950)

        # Какая сторона прилипания панели логов: "right" | "left"
        self._log_snap_side: str = "right"

        # === Глобальный стиль главного окна ===
        self.setStyleSheet("""
            QWidget {
                background-color: #232629;
                color: #F0F0F0;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 13px;
            }
            QMainWindow { background-color: #232629; }
            QLabel { color: #F0F0F0; }
            QPushButton, QPushButton[class="mass-action"], QPushButton.mass-action {
                background: #232629;
                color: #F0F0F0;
                border: 0.5px groove #5A5A5A;  /* возвращаем бордюр */
                border-radius: 8px;
                padding: 3px 12px;
                font-size: 15px;
                min-width: 110px;
                min-height: 26px;
                max-height: 36px;
            }
            QPushButton:hover:!disabled, QPushButton[class="mass-action"]:hover:!disabled, QPushButton.mass-action:hover:!disabled {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFE066, stop:1 #FFB800);
                color: #232629;
                border: 1px solid #FFE066;
            }
            QPushButton:pressed, QPushButton[class="mass-action"]:pressed, QPushButton.mass-action:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFC300, stop:1 #FFD700);
            }
            QPushButton:disabled, QPushButton[class="mass-action"]:disabled, QPushButton.mass-action:disabled {
                background: #35393C;
                color: #767676;
                border: 1px solid #434343;
            }
        """)

        # === Центральный виджет/макет ===
        central_widget = QWidget()
        central_widget.setAttribute(Qt.WA_StyledBackground, True)
        self.setCentralWidget(central_widget)
        self.main_layout = QVBoxLayout(central_widget)
        self.main_layout.setContentsMargins(10, 10, 10, 10)
        self.main_layout.setSpacing(12)

        # === Верхняя панель режимов ===
        self.mode_bar = ModeBar(self)
        self.main_layout.addWidget(self.mode_bar)

        # === Топ-бар ADS (контейнер, чтобы прятать целиком) ===
        self.top_bar_widget = QWidget()
        self.top_bar_widget.setAttribute(Qt.WA_StyledBackground, True)

        self.top_bar = QHBoxLayout(self.top_bar_widget)
        self.top_bar.setContentsMargins(0, 0, 0, 0)
        self.top_bar.setSpacing(6)
        self.dashboard_btn = QPushButton("Профили")
        self.settings_btn = QPushButton("Настройки")
        self.test_conn_btn = QPushButton("Тест подключения")
        for btn in [self.dashboard_btn, self.settings_btn, self.test_conn_btn]:
            btn.setProperty("class", "mass-action")
        self.top_bar.addWidget(self.dashboard_btn)
        self.top_bar.addWidget(self.settings_btn)
        self.top_bar.addStretch(1)
        self.top_bar.addWidget(self.test_conn_btn)
        self.main_layout.addWidget(self.top_bar_widget)
        self._set_top_bar_enabled(False)

        # === Центральные панели приложения ===
        self.panels = QWidget()
        self.panels.setAttribute(Qt.WA_StyledBackground, True)
        self.panels_layout = QVBoxLayout(self.panels)
        self.panels_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.addWidget(self.panels)

        self.profile_panel = AdsProfilePanel(self)
        self.settings_panel = AdsSettingsPanel(self)
        self.auto_panel = AutoPanel(self)
        self.info_panel = InfoPanel(self)

        self.panels_layout.addWidget(self.profile_panel)
        self.panels_layout.addWidget(self.settings_panel)
        self.panels_layout.addWidget(self.auto_panel)
        self.panels_layout.addWidget(self.info_panel)

        # Начальные состояния
        self.profile_panel.hide()
        self.settings_panel.hide()
        self.auto_panel.hide()
        self.info_panel.hide()
        self.panels.hide()

        # === ДОК-ПАНЕЛЬ ЛОГОВ ===
        from ads_log_gui import LogWindow
        self.log_widget = LogWindow(self)
        self.log_dock = StickyDock("Логи", self)
        self.log_dock.setObjectName("LogDockWidget")
        self.log_dock.setWidget(self.log_widget)
        self.addDockWidget(Qt.RightDockWidgetArea, self.log_dock)
        self.log_dock.setFloating(True)
        self.log_dock.hide()

        # (исторически дублировалось — оставляю как есть, чтобы не менять поведение)
        self.log_dock = StickyDock("Логи", self)
        self.log_dock.setObjectName("LogDockWidget")
        self.log_dock.setWidget(self.log_widget)
        self.addDockWidget(Qt.RightDockWidgetArea, self.log_dock)
        self.log_dock.setVisible(False)
        self.log_dock.setFloating(True)

        # === Кнопки и сигналы ===
        self.dashboard_btn.clicked.connect(self.show_profiles)
        self.settings_btn.clicked.connect(self.show_settings)
        self.test_conn_btn.clicked.connect(self.test_connection)

        # Сигналы ModeBar
        self.mode_bar.modeChanged.connect(self._on_mode_changed)
        self.mode_bar.pinToggled.connect(self._on_pin_toggled)
        self.mode_bar.infoToggled.connect(self._on_info_toggled)
        # Новая кнопка «📃» — именно toggle (как старая «Логи» делала)
        self.mode_bar.logsRequested.connect(self.toggle_log_window)

        # === Газ: интервал по умолчанию 30 c ===
        self._gas_interval = 30
        self._gas_counter = self._gas_interval
        self._gas_timer = QTimer(self)
        self._gas_timer.setInterval(1000)
        self._gas_timer.timeout.connect(self._update_gas_timer)
        self._gas_timer.start()
        self._current_gas_prices = None
        self._current_gas_error = None
        self._last_gas_title_text: str | None = None
        self.gas_signals = GasSignals()
        self.gas_signals.gas_loaded.connect(self._on_gas_loaded_mainthread)
        self._gas_query_active = False
        self._refresh_gas()

        # === Применяем сохранённые настройки окна/режима/пина и геометрию ===
        self._apply_saved_window_prefs()
        self._restore_ui_geometry()

        QTimer.singleShot(0, lambda: self._set_top_bar_enabled(self.mode_bar.get_mode() != "AUTO"))

        self._stay_on_top: bool = self.mode_bar.is_pin_on()

    # --- сохранение/восстановление геометрии (base64) ---
    def _qbytearray_to_b64(self, ba: QtQByteArray) -> str:
        try:
            return bytes(ba.toBase64()).decode("ascii", errors="ignore")
        except Exception:
            try:
                return base64.b64encode(bytes(ba)).decode("ascii", errors="ignore")
            except Exception:
                return ""

    def _b64_to_qbytearray(self, b64: str) -> QtQByteArray:
        try:
            raw = base64.b64decode((b64 or "").encode("ascii"), validate=False)
            return QtQByteArray(raw)
        except Exception:
            return QtQByteArray()

    def _ensure_on_screen(self):
        try:
            rect = self.frameGeometry()
            screens = QGuiApplication.screens()
            if not screens:
                return
            visible_any = False
            for s in screens:
                if rect.intersects(s.availableGeometry()):
                    visible_any = True
                    break
            if not visible_any:
                geo = QGuiApplication.primaryScreen().availableGeometry()
                self.move(geo.x() + 80, geo.y() + 80)
        except Exception:
            pass

    def _restore_ui_geometry(self) -> None:
        ui = load_ui_geometry()
        main_b64 = ui.get("main_geometry", "") or ""
        log_b64 = ui.get("logdock_geometry", "") or ""
        side = (ui.get("logdock_side", "right") or "right").lower()
        restored_any = False

        if main_b64:
            try:
                ba = self._b64_to_qbytearray(main_b64)
                if not ba.isEmpty():
                    self.restoreGeometry(ba)
                    restored_any = True
            except Exception:
                pass

        if restored_any:
            self._ensure_on_screen()

        vis = (ui.get("logdock_visible", "1") or "1") == "1"

        if self.log_dock and self.log_dock.isFloating():
            if log_b64:
                try:
                    ba = self._b64_to_qbytearray(log_b64)
                    if not ba.isEmpty():
                        self.log_dock.restoreGeometry(ba)
                except Exception:
                    pass
            self.log_dock.setVisible(vis)

        if not restored_any and self.log_dock:
            if vis:
                QTimer.singleShot(0, self._place_log_dock_initial)
        else:
            if vis:
                QTimer.singleShot(0, self.align_log_window)

    def _place_log_dock_initial(self):
        if not self.log_dock.isFloating():
            self.log_dock.setFloating(True)
        self.align_log_window()

    def set_log_snap_side(self, side: str) -> None:
        if side in ("left", "right"):
            self._log_snap_side = side
            save_ui_geometry(logdock_side=self._log_snap_side)

    # ===== Применение сохранённых настроек =====
    def _apply_saved_window_prefs(self) -> None:
        settings = load_settings_from_registry()
        active_mode = (settings.get("active_mode", "ADS") or "ADS").upper()
        self.mode_bar.set_mode(active_mode)
        self._apply_mode(active_mode, initial=True)

        stay_on_top = str(settings.get("stay_on_top", "0") or "0") == "1"
        self.mode_bar.set_pin(stay_on_top)
        self._apply_stay_on_top(stay_on_top)

    # ===== Панель режима =====
    def _on_mode_changed(self, mode: str) -> None:
        self._apply_mode(mode)
        self.mode_bar.set_info_active(False)
        settings = load_settings_from_registry()
        settings["active_mode"] = "AUTO" if (mode or "").upper() == "AUTO" else "ADS"
        save_settings_to_registry(settings)
        logger.info(
            f"Режим переключён на: {settings['active_mode']}",
            profile_names=["GLOBAL"],
            category="SYSTEM"
        )

    def _apply_mode(self, mode: str, initial: bool = False) -> None:
        """
        • ADS — показ топ-бара и панелей ADS; Auto скрыт.
        • AUTO — топ-бар ADS полностью скрыт, показываем только панель Auto.
        Вкладка «Инфо» — независимая.
        """
        self.info_panel.hide()

        mode = (mode or "ADS").upper()
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

    # ======= ВЕРХ НАД ВСЕМИ ОКНАМИ — БЕЗ МИГАНИЯ =======
    def _set_always_on_top_safely(self, widget: QWidget, on: bool) -> bool:
        try:
            win = widget.windowHandle()
            if win is not None:
                try:
                    win.setFlag(Qt.WindowStaysOnTopHint, bool(on))
                    return True
                except Exception:
                    pass

            if sys.platform == "win32":
                import ctypes
                from ctypes import wintypes

                HWND = wintypes.HWND
                UINT = wintypes.UINT
                BOOL = wintypes.BOOL
                HWND_TOPMOST = HWND(-1)
                HWND_NOTOPMOST = HWND(-2)
                SWP_NOMOVE = 0x0002
                SWP_NOSIZE = 0x0001
                SWP_NOACTIVATE = 0x0010
                SWP_SHOWWINDOW = 0x0040

                SetWindowPos = ctypes.windll.user32.SetWindowPos
                SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, UINT]
                SetWindowPos.restype = BOOL

                hwnd = int(widget.winId())
                flags = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
                top = HWND_TOPMOST if on else HWND_NOTOPMOST
                ok = SetWindowPos(HWND(hwnd), top, 0, 0, 0, 0, flags)
                if ok:
                    return True
        except Exception:
            pass
        return False

    def _apply_stay_on_top(self, on: bool) -> None:
        if not self._set_always_on_top_safely(self, on):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))

        if self.log_dock and self.log_dock.isFloating():
            if not self._set_always_on_top_safely(self.log_dock, on):
                self.log_dock.setWindowFlag(Qt.WindowStaysOnTopHint, bool(on))

        self._stay_on_top = bool(on)
        self.mode_bar.set_pin(self._stay_on_top)

    # ===== Pin / поверх всех =====
    def _on_pin_toggled(self, on: bool) -> None:
        self._apply_stay_on_top(on)
        settings = load_settings_from_registry()
        settings["stay_on_top"] = "1" if on else "0"
        save_settings_to_registry(settings)

    # ===== Хелперы топ-бара =====
    def _set_top_bar_enabled(self, enabled: bool) -> None:
        self.dashboard_btn.setEnabled(enabled)
        self.settings_btn.setEnabled(enabled)
        self.test_conn_btn.setEnabled(enabled)

    # ==== Переключение центральных панелей ADS ====
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

    # ==== Панель логов ====
    def toggle_log_window(self) -> None:
        if self.log_dock.isVisible():
            self.log_dock.hide()
        else:
            self.log_dock.setFloating(True)
            self.log_dock.show()
            self.align_log_window()

    def _bring_logs_to_front(self) -> None:
        if getattr(self, "log_dock", None) is None:
            return
        self.log_dock.setVisible(True)
        self.log_dock.setFloating(True)
        self.log_dock.show()
        self.align_log_window()
        self.log_dock.raise_()
        if self.log_dock.isFloating():
            self.log_dock.activateWindow()

    def align_log_window(self) -> None:
        if not self.log_dock or not self.log_dock.isVisible() or not self.log_dock.isFloating():
            return
        gap = 10
        geo = self.frameGeometry()
        main_client_height = self.geometry().height()
        dock_w = self.log_dock.width()
        x = geo.x() + geo.width() + gap if self._log_snap_side == "right" else geo.x() - dock_w - gap
        y = geo.y()
        self.log_dock.move(x, y)
        self.log_dock.resize(dock_w, main_client_height)

    # ==== События окна ====
    def resizeEvent(self, event: QEvent) -> None:
        super().resizeEvent(event)
        if self.log_dock and self.log_dock.isVisible() and self.log_dock.isFloating():
            self.align_log_window()

    def moveEvent(self, event: QEvent) -> None:
        super().moveEvent(event)
        if self.log_dock and self.log_dock.isVisible() and self.log_dock.isFloating():
            self.align_log_window()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange:
            if self.isMinimized():
                if self.log_dock.isFloating() and self.log_dock.isVisible():
                    self.log_dock.showMinimized()
            elif self.isVisible():
                if self.log_dock.isFloating() and self.log_dock.isVisible():
                    self.log_dock.showNormal()
                    self.align_log_window()

    def focusInEvent(self, event: QEvent) -> None:
        super().focusInEvent(event)
        if self.log_dock and self.log_dock.isVisible():
            self.log_dock.raise_()
            if self.log_dock.isFloating():
                self.log_dock.activateWindow()

    def closeEvent(self, event: QEvent) -> None:
        try:
            main_b64 = self._qbytearray_to_b64(self.saveGeometry())
        except Exception:
            main_b64 = ""
        try:
            log_b64 = self._qbytearray_to_b64(self.log_dock.saveGeometry()) if (self.log_dock and self.log_dock.isFloating()) else ""
        except Exception:
            log_b64 = ""
        ok, msg = save_ui_geometry(
            main_geometry_b64=main_b64,
            logdock_geometry_b64=log_b64,
            logdock_side=self._log_snap_side,
            logdock_visible=(self.log_dock.isVisible() if self.log_dock else False)
        )
        if ok:
            logger.info("UI-геометрия сохранена.", profile_names=["GLOBAL"], category="SETTINGS")
        else:
            logger.warning(msg or "Не удалось сохранить UI-геометрию.", profile_names=["GLOBAL"], category="SETTINGS")
        event.accept()

    # ==== Тест подключения (остаётся в ADS) ====
    def test_connection(self) -> None:
        api_url: str = self.settings_panel.get_api_url().strip()
        if not api_url:
            logger.error("API-адрес не заполнен.", profile_names=["GLOBAL"], category="API")
            return

        logger.start("Тест соединения с AdsPower API...", profile_names=["GLOBAL"], category="API", force=True)
        logger.info(f"Адрес API: {api_url}", profile_names=["GLOBAL"], category="API")

        self.profile_panel.set_progress_stage("Тест соединения с API...")
        self.profile_panel.set_progress(0)

        profiles, logs = get_profiles_and_log(api_url)

        for log in logs:
            if isinstance(log, tuple) and len(log) == 2:
                message, level = log
                logger.log(message, level, profile_names=["GLOBAL"], category="API")
            else:
                logger.info(str(log), profile_names=["GLOBAL"], category="API")

        self.profile_panel.set_progress(100)
        self.profile_panel.set_progress_stage("Тест подключения завершён!")

        if profiles:
            logger.success(
                f"Тест подключения к API выполнен успешно. Профилей получено: {len(profiles)}.",
                profile_names=["GLOBAL"], category="API", force=True
            )
            self.profile_panel.update_profiles(profiles)
        else:
            self.profile_panel.update_profiles([])
            logger.error(
                "Не удалось получить список профилей. Проверьте адрес API в настройках и доступность AdsPower.",
                profile_names=["GLOBAL"],
                category="API"
            )

        QTimer.singleShot(1000, lambda: self.profile_panel.set_progress(0))
        QTimer.singleShot(1000, lambda: self.profile_panel.set_progress_stage(""))

    def append_log(self, message: str, level: str = "INFO") -> None:
        logger.log(message, level, profile_names=["GLOBAL"], category="SYSTEM")

    # ===== Панель «Инфо» =====
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

    # ===== Газ =====
    def _update_gas_timer(self) -> None:
        self._gas_counter -= 1
        if self._gas_counter <= 0:
            self._gas_counter = self._gas_interval
            self._refresh_gas()
        else:
            self._update_title_only_timer()

    def _refresh_gas(self) -> None:
        if getattr(self, '_gas_query_active', False):
            return
        self._gas_query_active = True
        spaces = " " * 50
        title = f"{APP_NAME} v{APP_VERSION}{spaces}Загрузка цен газа...    ({self._gas_counter}c)"
        self.setWindowTitle(title)
        def on_gas_loaded(prices, error):
            self.gas_signals.gas_loaded.emit(prices, error)
        get_gas_prices_async(on_gas_loaded)

    def _on_gas_loaded_mainthread(self, prices, error):
        spaces = " " * 50
        if error:
            if self._last_gas_title_text:
                title = f"{APP_NAME} v{APP_VERSION}{spaces}{self._last_gas_title_text}    ({self._gas_counter}c)"
                self.setWindowTitle(title)
            logger.error(
                f"Ошибка загрузки цен газа: {error}",
                profile_names=["GLOBAL"],
                category="API",
                extra={"trace": str(error)}
            )
            self._current_gas_prices = None
            self._current_gas_error = error
        else:
            formatted = format_gas_string(prices, readable=True)
            self._last_gas_title_text = formatted
            title = f"{APP_NAME} v{APP_VERSION}{spaces}{formatted}    ({self._gas_counter}c)"
            self.setWindowTitle(title)
            self._current_gas_prices = prices
            self._current_gas_error = None
        self._gas_query_active = False
        ping_watchdog()

    def _update_title_only_timer(self) -> None:
        current_title = self.windowTitle()
        import re
        new_title = re.sub(r"\(\d+c\)", f"({self._gas_counter}c)", current_title)
        self.setWindowTitle(new_title)

    # ======== Отложенный показ без «прыжка» ========
    def show_deferred(self) -> None:
        self.setUpdatesEnabled(False)
        try:
            self.align_log_window()
            super().show()
            if self.log_dock:
                self.log_dock.setFloating(True)
                self.log_dock.show()
                self.align_log_window()
        finally:
            QTimer.singleShot(0, lambda: self.setUpdatesEnabled(True))
