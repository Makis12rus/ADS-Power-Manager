"""
Модуль: gui/mode_bar.py
Назначение: Изолированный компонент верхней панели управления (Presentation Layer).
Зона ответственности: Отрисовка кнопок переключения режимов (ADS/Auto), тулбара
                      инструментов (Логи, Пин, Инфо) и индикатора автосохранения.
                      Управляет нативной C++ машиной состояний иконок (Zero-CPU Hover).
Интеграция: Абсолютно независимый виджет. Не импортирует MainWindow или другие панели.
            Общается с внешним миром исключительно через сигналы (Mediator Pattern).
"""

from PySide6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QButtonGroup, QToolButton
)
from PySide6.QtCore import Qt, Signal, QSize, QSignalBlocker
from PySide6.QtGui import QIcon

# Строгие абсолютные импорты фасада стилей (Lazy Loading)
from core.style import Styles, Colors, Graphics, AutoSaveIndicator


class ModeBar(QWidget):
    """
    Верхняя панель: переключение режимов ADS/AUTO, индикатор сохранения, логи, пин, инфо.
    Работает как изолированный пульт управления, транслирующий команды через сигналы.
    """
    
    # --- СИГНАЛЫ (КОНТРАКТ МЕДИАТОРА) ---
    modeChanged = Signal(str)
    pinToggled = Signal(bool)
    infoToggled = Signal(bool)
    logsToggled = Signal(bool)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ModeBar")
        
        # Включаем поддержку QSS для кастомного фона
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(50)
        self.setAutoFillBackground(True)
        self.setStyleSheet(Styles.MODE_BAR)
        
        # Внутреннее состояние
        self._current_mode: str = "ADS"
        self._stay_on_top: bool = False
        self._info_active: bool = False
        
        self._setup_ui()
    
    def _setup_ui(self) -> None:
        """Инициализация пользовательского интерфейса панели."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        
        # --- Кнопки режимов ---
        self.ads_btn = self._make_mode_btn(" ADS", "puzzle", True)
        self.auto_btn = self._make_mode_btn(" Auto", "bot", False)
        
        # Группируем кнопки для эксклюзивного переключения (Radio-button behavior)
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        self._mode_group.addButton(self.ads_btn)
        self._mode_group.addButton(self.auto_btn)
        
        layout.addWidget(self.ads_btn)
        layout.addWidget(self.auto_btn)
        
        # Пружина для выравнивания элементов по краям
        layout.addStretch(1)
        
        # --- Индикатор автосохранения (Always-on Display) ---
        self.save_indicator = AutoSaveIndicator(self)
        layout.addWidget(self.save_indicator)
        
        # --- Инструментальные кнопки ---
        self.logs_btn = self._make_icon_btn("file-text", "Логи", checkable=True)
        self.pin_btn = self._make_icon_btn("pin", "Поверх всех окон", checkable=True)
        self.info_btn = self._make_icon_btn("info", "О программе", checkable=True)
        
        layout.addWidget(self.logs_btn)
        layout.addWidget(self.pin_btn)
        layout.addWidget(self.info_btn)
        
        # --- Подключение сигналов ---
        self.ads_btn.toggled.connect(self._on_ads_toggled)
        self.auto_btn.toggled.connect(self._on_auto_toggled)
        
        self.logs_btn.toggled.connect(self.logsToggled)
        self.pin_btn.toggled.connect(self._on_pin_clicked)
        self.info_btn.toggled.connect(self._on_info_clicked)
    
    def _create_stateful_icon(self, icon_name: str) -> QIcon:
        """
        Создает многосоставную иконку, которая сама меняет цвет при наведении и клике.
        Использует нативную C++ машину состояний QIcon, обеспечивая Zero-CPU Hover.
        """
        icon = QIcon()
        # Базовый цвет (Серый)
        pm_normal = Graphics.get_modebar_icon(icon_name, Colors.TXT_SECONDARY, 18).pixmap(18, 18)
        # Акцентный цвет (Золотой)
        pm_active = Graphics.get_modebar_icon(icon_name, Colors.ACCENT, 18).pixmap(18, 18)
        
        icon.addPixmap(pm_normal, QIcon.Mode.Normal, QIcon.State.Off)
        icon.addPixmap(pm_active, QIcon.Mode.Active, QIcon.State.Off)  # Hover
        icon.addPixmap(pm_active, QIcon.Mode.Normal, QIcon.State.On)  # Checked
        icon.addPixmap(pm_active, QIcon.Mode.Active, QIcon.State.On)  # Checked & Hover
        return icon
    
    def _make_mode_btn(self, text: str, icon_name: str, checked: bool) -> QPushButton:
        """Хелпер для создания кнопки переключения режима."""
        btn = QPushButton(text)
        btn.setIcon(self._create_stateful_icon(icon_name))
        btn.setIconSize(QSize(18, 18))
        btn.setCheckable(True)
        btn.setChecked(checked)
        btn.setProperty("class", "mode")
        return btn
    
    def _make_icon_btn(self, icon_name: str, tooltip: str, checkable: bool) -> QToolButton:
        """Хелпер для создания компактной инструментальной кнопки."""
        btn = QToolButton()
        btn.setIcon(self._create_stateful_icon(icon_name))
        btn.setIconSize(QSize(18, 18))
        btn.setToolTip(tooltip)
        btn.setCheckable(checkable)
        btn.setProperty("class", "icon-btn")
        return btn
    
    # ===================== PUBLIC API (ГЕТТЕРЫ И СЕТТЕРЫ) =====================
    
    def set_mode(self, mode: str) -> None:
        """
        Программное переключение режима.
        Используется при инициализации для восстановления состояния из реестра.
        """
        mode = (mode or "ADS").upper()
        if mode == "AUTO":
            self.auto_btn.setChecked(True)
        else:
            self.ads_btn.setChecked(True)
    
    def get_mode(self) -> str:
        """Возвращает текущий активный режим."""
        return "AUTO" if self.auto_btn.isChecked() else "ADS"
    
    def set_pin(self, on: bool) -> None:
        """
        Программное переключение состояния кнопки "Поверх всех".
        Использует QSignalBlocker для предотвращения бесконечного цикла сигналов.
        """
        self._stay_on_top = bool(on)
        with QSignalBlocker(self.pin_btn):
            self.pin_btn.setChecked(on)
    
    def is_pin_on(self) -> bool:
        """Возвращает текущее состояние пина."""
        return self._stay_on_top
    
    def set_info_active(self, active: bool) -> None:
        """Программное переключение состояния кнопки 'О программе'."""
        self._info_active = bool(active)
        with QSignalBlocker(self.info_btn):
            self.info_btn.setChecked(active)
    
    def set_logs_active(self, active: bool) -> None:
        """Программное переключение состояния кнопки 'Логи'."""
        with QSignalBlocker(self.logs_btn):
            self.logs_btn.setChecked(active)
    
    # ===================== ВНУТРЕННИЕ ОБРАБОТЧИКИ СОБЫТИЙ =====================
    
    def _on_ads_toggled(self, checked: bool) -> None:
        """Обработчик выбора режима ADS."""
        if checked:
            self._current_mode = "ADS"
            self.set_info_active(False)
            self.modeChanged.emit("ADS")
    
    def _on_auto_toggled(self, checked: bool) -> None:
        """Обработчик выбора режима AUTO."""
        if checked:
            self._current_mode = "AUTO"
            self.set_info_active(False)
            self.modeChanged.emit("AUTO")
    
    def _on_pin_clicked(self, checked: bool) -> None:
        """Обработчик клика по кнопке 'Поверх всех'."""
        self._stay_on_top = checked
        self.pinToggled.emit(checked)
    
    def _on_info_clicked(self, checked: bool) -> None:
        """Обработчик клика по кнопке 'О программе'."""
        self._info_active = checked
        self.infoToggled.emit(checked)