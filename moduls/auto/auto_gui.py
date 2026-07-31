"""
Модуль: moduls/auto/auto_gui.py
Назначение: Графический интерфейс (Presentation Layer) режима AUTO.
Зона ответственности: Отрисовка редактора кода (CodeEditor) с подсветкой синтаксиса,
                      панели настроек таргетинга профилей и параметров запуска песочницы.
Интеграция: Строго изолирован от бизнес-логики. Общается с `moduls.auto.auto_logic` для
            сохранения файлов и парсинга профилей. Реализует паттерн AutoSave-on-the-fly
            с использованием Debouncing и On-Blur сброса кэша для настроек. Общается с
            MainWindow через сигнал saveStatusChanged (Mediator Pattern).
            Интерфейс использует премиальные вдавленные поля DebossedLineEdit,
            поддерживает Gutter Isolation для стабильной верстки скроллбаров и
            оснащен движком плавного скольжения (Smooth Scroll Engine).
"""

import threading
from typing import Any

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QStackedWidget, QSizePolicy, QPlainTextEdit, QTextEdit,
    QFileDialog, QMessageBox, QSplitter, QGroupBox,
    QCheckBox, QGridLayout, QFrame, QComboBox
)
from PySide6.QtCore import Qt, QRect, QSize, QTimer, Signal, QEvent, QObject, QSignalBlocker
from PySide6.QtGui import (
    QPainter, QColor, QTextFormat, QPaintEvent, QResizeEvent
)

# Строгие абсолютные импорты (Monorepo Style)
from moduls.auto.auto_logic import init_code_editor, save_code_to_file, load_and_group_profiles
from core.core import load_settings_from_registry, save_settings_to_registry
from system.logger import logger, log_action
from core.style import (
    Styles, Colors, Texts, AutoSaveIndicator, DebossedLineEdit,
    SmoothScrollBar, SmoothScrollDelegate, SmoothScrollArea
)


# ===================== Виджеты Редактора =====================

class _LineNumberArea(QWidget):
    """Виджет для отрисовки номеров строк слева от редактора."""
    
    def __init__(self, editor: 'CodeEditor') -> None:
        super().__init__(editor)
        self._ed = editor
    
    def sizeHint(self) -> QSize:
        return QSize(self._ed.lineNumberAreaWidth(), 0)
    
    def paintEvent(self, event: QPaintEvent) -> None:
        self._ed.lineNumberAreaPaintEvent(event)


class CodeEditor(QPlainTextEdit):
    """
    QPlainTextEdit с нумерацией строк и подсветкой текущей строки.
    Вся логика поведения (Keys/Syntax) подключается из auto_logic.
    Оснащен движком плавного скроллинга (Smooth Scroll Engine).
    """
    # Используем цвета из палитры
    _BG_GUTTER = QColor(Colors.ED_BG_GUTTER)
    _FG_GUTTER = QColor(Colors.ED_FG_GUTTER)
    _LINE_SPLIT = QColor(Colors.ED_LINE_SPLIT)
    _CUR_LINE_BG = QColor(Colors.ED_CUR_LINE)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        
        # Gutter Isolation: Принудительно резервируем место под скроллбар,
        # чтобы верстка не прыгала при добавлении новых строк кода.
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        # --- Интеграция Smooth Scroll Engine ---
        self.smooth_scrollbar = SmoothScrollBar(self)
        self.smooth_scrollbar.setOrientation(Qt.Orientation.Vertical)
        self.setVerticalScrollBar(self.smooth_scrollbar)
        
        self.smooth_delegate = SmoothScrollDelegate(self.smooth_scrollbar, Qt.Orientation.Vertical, self)
        self.viewport().installEventFilter(self.smooth_delegate)
        
        self._ln_area = _LineNumberArea(self)
        self.extra_base_selections: list[QTextEdit.ExtraSelection] = []
        
        self.blockCountChanged.connect(self._updateLineNumberAreaWidth)
        self.updateRequest.connect(self._updateLineNumberArea)
        self.cursorPositionChanged.connect(self._highlightCurrentLine)
        self.textChanged.connect(self._highlightCurrentLine)
        
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        
        self._updateLineNumberAreaWidth(0)
        self._highlightCurrentLine()
    
    def lineNumberAreaWidth(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        char_w = self.fontMetrics().horizontalAdvance('9')
        return 10 + char_w * digits + 10
    
    def _updateLineNumberAreaWidth(self, _new_block_count: int) -> None:
        self.setViewportMargins(self.lineNumberAreaWidth(), 0, 0, 0)
    
    def _updateLineNumberArea(self, rect: QRect, dy: int) -> None:
        if dy:
            self._ln_area.scroll(0, dy)
        else:
            self._ln_area.update(0, rect.y(), self._ln_area.width(), rect.height())
        
        if rect.contains(self.viewport().rect()):
            self._updateLineNumberAreaWidth(0)
    
    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._ln_area.setGeometry(QRect(cr.left(), cr.top(), self.lineNumberAreaWidth(), cr.height()))
    
    def lineNumberAreaPaintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self._ln_area)
        painter.fillRect(event.rect(), self._BG_GUTTER)
        
        # Разделитель
        x_split = event.rect().right() - 1
        painter.setPen(self._LINE_SPLIT)
        painter.drawLine(x_split, event.rect().top(), x_split, event.rect().bottom())
        
        # Итерация по видимым блокам
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())
        height = self.fontMetrics().height()
        
        painter.setPen(self._FG_GUTTER)
        width = self._ln_area.width()
        
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.drawText(0, top, width - 4, height, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, number)
            
            block = block.next()
            block_number += 1
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
    
    def _highlightCurrentLine(self) -> None:
        """Мягкая подсветка текущей строки (отключается, если пусто, чтобы видеть placeholder)."""
        if self.isReadOnly():
            self.setExtraSelections(self.extra_base_selections)
            return
        
        doc = self.document()
        # Если текст пуст (1 блок без символов), не подсвечиваем
        if doc.blockCount() == 1 and doc.firstBlock().length() <= 1:  # length() включает newline
            self.extra_base_selections = []
            self.setExtraSelections([])
            return
        
        sel = QTextEdit.ExtraSelection()
        fmt = sel.format
        fmt.setBackground(self._CUR_LINE_BG)
        fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
        sel.format = fmt
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()
        
        self.extra_base_selections = [sel]
        self.setExtraSelections(self.extra_base_selections)


# ===================== Панель Настроек AUTO =====================

class AutoSettingsPanel(QWidget):
    """
    Панель настроек режима AUTO.
    Слева: Выбор профилей (Targeting) в виде списка чекбоксов с группами.
    Справа: Параметры запуска (Execution) с подробными инструкциями.
    Работает в режиме реактивного автосохранения (AutoSave-on-the-fly).
    """
    # Сигнал для безопасной передачи данных из фонового потока в GUI
    profilesLoaded = Signal(dict, list)
    
    # Сигнал для передачи статуса автосохранения в ModeBar (Mediator Pattern)
    saveStatusChanged = Signal(int, str)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._profile_checkboxes: dict[str, QCheckBox] = {}  # ID -> CheckBox
        
        # Таймер дебаунса для защиты реестра от спама при вводе текста
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(750)  # 750 мс задержки после последнего нажатия клавиши
        self._save_timer.timeout.connect(self._execute_save)
        
        self._init_ui()
        self._load_settings_to_ui()
        
        # Таймер для отложенного обновления счетчика (оптимизация)
        self._counter_timer = QTimer(self)
        self._counter_timer.setSingleShot(True)
        self._counter_timer.setInterval(50)
        self._counter_timer.timeout.connect(self._update_counter_label)
        
        # Подключаем сигнал фоновой загрузки
        self.profilesLoaded.connect(self._on_profiles_loaded)
    
    def _init_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet("QSplitter::handle { background-color: #393B3E; }")
        
        # === ЛЕВАЯ ПАНЕЛЬ (Выбор профилей) ===
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.setSpacing(8)
        
        # Кнопка обновления
        self.refresh_btn = QPushButton("Получить профили")
        self.refresh_btn.setToolTip("Обновить список профилей из AdsPower")
        self.refresh_btn.setStyleSheet(Styles.BTN_ACTION)
        self.refresh_btn.setMinimumHeight(32)
        self.refresh_btn.clicked.connect(self._start_bg_profile_load)
        
        left_layout.addWidget(self.refresh_btn)
        
        # Область прокрутки для списка профилей (Smooth Scroll Engine)
        self.scroll_area = SmoothScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        
        # Gutter Isolation: Принудительно резервируем место под скроллбар
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        # Устанавливаем фон и рамку, как у таблицы (Colors.BG_PANEL = #282B2E)
        self.scroll_area.setStyleSheet(f"""
            QScrollArea {{
                background-color: {Colors.BG_PANEL};
                border: 1px solid #393B3E;
                border-radius: 10px;
            }}
            QScrollArea > QWidget > QWidget {{
                background-color: {Colors.BG_PANEL};
            }}
        """)
        
        # Контейнер для чекбоксов
        self.profiles_container = QWidget()
        self.profiles_container.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.profiles_layout = QVBoxLayout(self.profiles_container)
        self.profiles_layout.setContentsMargins(0, 0, 0, 0)
        self.profiles_layout.setSpacing(10)
        self.profiles_layout.addStretch(1)  # Spacer внизу
        
        self.scroll_area.setWidget(self.profiles_container)
        left_layout.addWidget(self.scroll_area, 1)
        
        # Тулбар выбора (Кнопки по центру)
        sel_toolbar = QHBoxLayout()
        self.btn_all = QPushButton("Выбрать все")
        self.btn_none = QPushButton("Снять все")
        for b in [self.btn_all, self.btn_none]:
            b.setStyleSheet(Styles.BTN_LOG_MINI)
            b.setFixedHeight(24)
        
        self.btn_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_none.clicked.connect(lambda: self._set_all_checked(False))
        
        sel_toolbar.addStretch(1)
        sel_toolbar.addWidget(self.btn_all)
        sel_toolbar.addWidget(self.btn_none)
        sel_toolbar.addStretch(1)
        
        left_layout.addLayout(sel_toolbar)
        
        # Лейбл счетчика (вынесен вниз по центру)
        self.lbl_counter = QLabel("Выбрано: 0")
        self.lbl_counter.setStyleSheet(f"color: {Colors.TXT_SECONDARY}; font-size: 11px; font-weight: bold;")
        self.lbl_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        left_layout.addWidget(self.lbl_counter)
        
        # === ПРАВАЯ ПАНЕЛЬ (Настройки) ===
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        
        # 1. Область прокрутки для настроек (Smooth Scroll Engine)
        settings_scroll = SmoothScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        
        # Gutter Isolation: Принудительно резервируем место под скроллбар
        settings_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        settings_scroll.setStyleSheet("background: transparent;")
        
        # Контейнер внутри скролла
        scroll_content = QWidget()
        scroll_content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setContentsMargins(20, 10, 20, 10)
        scroll_layout.setSpacing(15)
        
        # --- Группа 1: Потоки и Лимиты ---
        g1 = QGroupBox("⚙️ Потоки и Лимиты")
        g1.setStyleSheet(Styles.GROUP_BOX)
        g1_main_lay = QVBoxLayout(g1)
        g1_main_lay.setSpacing(8)
        
        g1_controls = QWidget()
        g1_grid = QGridLayout(g1_controls)
        g1_grid.setContentsMargins(0, 0, 0, 0)
        g1_grid.setHorizontalSpacing(15)
        g1_grid.setVerticalSpacing(8)
        
        self.inp_threads = self._create_mini_input("3")
        self.chk_shuffle = QCheckBox("Перемешать (Shuffle)")
        self.chk_shuffle.setStyleSheet(Styles.CHECKBOX)
        self.chk_shuffle.stateChanged.connect(self._on_discrete_changed)
        
        self.chk_stop_limit = QCheckBox("Стоп после")
        self.chk_stop_limit.setStyleSheet(Styles.CHECKBOX)
        self.chk_stop_limit.stateChanged.connect(self._on_discrete_changed)
        self.inp_stop_count = self._create_mini_input("10")
        
        # Row 0
        g1_grid.addWidget(QLabel("Потоков:"), 0, 0)
        g1_grid.addWidget(self.inp_threads, 0, 1)
        g1_grid.addWidget(self.chk_shuffle, 0, 2, 1, 2)
        
        # Row 1
        stop_cont = QWidget()
        stop_lay = QHBoxLayout(stop_cont)
        stop_lay.setContentsMargins(0, 0, 0, 0)
        stop_lay.setSpacing(10)
        stop_lay.addWidget(self.chk_stop_limit)
        stop_lay.addWidget(self.inp_stop_count)
        stop_lay.addWidget(QLabel("успешных профилей"))
        stop_lay.addStretch(1)
        
        g1_grid.addWidget(stop_cont, 1, 0, 1, 4)
        g1_grid.setColumnStretch(3, 1)
        
        g1_hint = self._create_hint_label(
            "<b>• Потоки:</b> Количество одновременно работающих браузеров.<br>"
            "<span style='color:#FFD700;'>⚠️ Рекомендация:</span> Для 16GB RAM ставьте не более 3-5 потоков.<br>"
            "<b>• Shuffle:</b> Перемешивает очередь запуска профилей. Полезно для защиты от тайминг-анализа (чтобы аккаунты не шли строго по порядку).<br>"
            "<b>• Стоп после:</b> Позволяет разбить большую пачку аккаунтов на «смены». Поток остановится, когда успешно обработает указанное число профилей."
        )
        
        g1_main_lay.addWidget(g1_controls)
        g1_main_lay.addWidget(g1_hint)
        scroll_layout.addWidget(g1)
        
        # --- Группа 2: Тайминги ---
        g2 = QGroupBox("⏱️ Тайминги (Humanization)")
        g2.setStyleSheet(Styles.GROUP_BOX)
        g2_main_lay = QVBoxLayout(g2)
        g2_main_lay.setSpacing(8)
        
        g2_controls = QWidget()
        g2_grid = QGridLayout(g2_controls)
        g2_grid.setContentsMargins(0, 0, 0, 0)
        g2_grid.setHorizontalSpacing(10)
        g2_grid.setVerticalSpacing(8)
        
        self.inp_delay_min = self._create_mini_input("5")
        self.inp_delay_max = self._create_mini_input("20")
        self.inp_cool_min = self._create_mini_input("30")
        self.inp_cool_max = self._create_mini_input("60")
        
        # Row 0
        g2_grid.addWidget(QLabel("Задержка старта (сек):"), 0, 0)
        g2_grid.addWidget(QLabel("от"), 0, 1)
        g2_grid.addWidget(self.inp_delay_min, 0, 2)
        g2_grid.addWidget(QLabel("до"), 0, 3)
        g2_grid.addWidget(self.inp_delay_max, 0, 4)
        
        # Row 1
        g2_grid.addWidget(QLabel("Пауза между профилями:"), 1, 0)
        g2_grid.addWidget(QLabel("от"), 1, 1)
        g2_grid.addWidget(self.inp_cool_min, 1, 2)
        g2_grid.addWidget(QLabel("до"), 1, 3)
        g2_grid.addWidget(self.inp_cool_max, 1, 4)
        
        g2_grid.setColumnStretch(5, 1)
        
        g2_hint = self._create_hint_label(
            "<b>• Задержка старта:</b> Случайная пауза ПОСЛЕ открытия браузера, но ПЕРЕД началом выполнения скрипта. Имитирует «раздумья» пользователя.<br>"
            "<b>• Пауза между профилями:</b> Время отдыха потока ПОСЛЕ закрытия профиля и ПЕРЕД взятием следующего. <span style='color:#40DB78;'>✅ Критично</span> для остывания IP при использовании мобильных прокси."
        )
        
        g2_main_lay.addWidget(g2_controls)
        g2_main_lay.addWidget(g2_hint)
        scroll_layout.addWidget(g2)
        
        # --- Группа 3: Надежность ---
        g3 = QGroupBox("🛡️ Надежность и Сбои")
        g3.setStyleSheet(Styles.GROUP_BOX)
        g3_main_lay = QVBoxLayout(g3)
        g3_main_lay.setSpacing(8)
        
        g3_controls = QWidget()
        g3_grid = QGridLayout(g3_controls)
        g3_grid.setContentsMargins(0, 0, 0, 0)
        g3_grid.setHorizontalSpacing(15)
        g3_grid.setVerticalSpacing(8)
        
        self.inp_timeout = self._create_mini_input("10")
        self.inp_retries = self._create_mini_input("1")
        
        self.cmb_on_error = QComboBox()
        self.cmb_on_error.addItems(["Закрыть браузер", "Оставить открытым"])
        self.cmb_on_error.setStyleSheet(Styles.COMBO_BOX_LOG + "QComboBox { max-width: 300px; }")
        self.cmb_on_error.setMinimumWidth(200)
        self.cmb_on_error.currentIndexChanged.connect(self._on_discrete_changed)
        
        # Row 0
        g3_grid.addWidget(QLabel("Глобальный таймаут (мин):"), 0, 0)
        g3_grid.addWidget(self.inp_timeout, 0, 1)
        g3_grid.addWidget(QLabel("Повторов (Retries):"), 0, 2)
        g3_grid.addWidget(self.inp_retries, 0, 3)
        
        # Row 1
        g3_grid.addWidget(QLabel("При ошибке:"), 1, 0)
        g3_grid.addWidget(self.cmb_on_error, 1, 1, 1, 3)
        
        g3_grid.setColumnStretch(4, 1)
        
        g3_hint = self._create_hint_label(
            "<b>• Таймаут:</b> Если скрипт завис и не отвечает дольше указанного времени, процесс будет убит принудительно, чтобы не блокировать очередь.<br>"
            "<b>• Retries:</b> Количество попыток перезапуска профиля в случае ошибки (например, если прокси не ответил).<br>"
            "<b>• При ошибке:</b> Выберите «Оставить открытым» для отладки, чтобы увидеть причину сбоя в консоли браузера."
        )
        
        g3_main_lay.addWidget(g3_controls)
        g3_main_lay.addWidget(g3_hint)
        scroll_layout.addWidget(g3)
        
        # --- Группа 4: Браузер ---
        g4 = QGroupBox("🌐 Браузер и Кошельки")
        g4.setStyleSheet(Styles.GROUP_BOX)
        g4_main_lay = QVBoxLayout(g4)
        g4_main_lay.setSpacing(8)
        
        g4_controls = QWidget()
        g4_grid = QGridLayout(g4_controls)
        g4_grid.setContentsMargins(0, 0, 0, 0)
        g4_grid.setHorizontalSpacing(15)
        g4_grid.setVerticalSpacing(8)
        
        self.chk_unlock = QCheckBox("🔓 Авто-разблокировка (ADS)")
        self.chk_unlock.setStyleSheet(Styles.CHECKBOX)
        self.chk_unlock.stateChanged.connect(self._on_discrete_changed)
        
        self.chk_images = QCheckBox("Загружать картинки")
        self.chk_images.setStyleSheet(Styles.CHECKBOX)
        self.chk_images.stateChanged.connect(self._on_discrete_changed)
        
        self.chk_headless = QCheckBox("Headless режим (без окна)")
        self.chk_headless.setStyleSheet(Styles.CHECKBOX)
        self.chk_headless.stateChanged.connect(self._on_discrete_changed)
        
        self.chk_close = QCheckBox("Закрывать после успеха")
        self.chk_close.setStyleSheet(Styles.CHECKBOX)
        self.chk_close.stateChanged.connect(self._on_discrete_changed)
        
        g4_grid.addWidget(self.chk_unlock, 0, 0)
        g4_grid.addWidget(self.chk_images, 0, 1)
        g4_grid.addWidget(self.chk_headless, 1, 0)
        g4_grid.addWidget(self.chk_close, 1, 1)
        
        g4_hint = self._create_hint_label(
            "<b>• Unlock:</b> Использует пароли из вкладки «Настройки» режима ADS для входа в MetaMask/Phantom перед запуском вашего кода.<br>"
            "<b>• Картинки:</b> Отключение экономит трафик прокси и ускоряет загрузку страниц.<br>"
            "<b>• Headless:</b> Запуск без графического интерфейса. Сильно экономит CPU/RAM, но сложнее отлаживать.<br>"
            "<b>• Закрывать:</b> Снимите галочку, если хотите вручную проверить результат работы скрипта после его завершения."
        )
        
        g4_main_lay.addWidget(g4_controls)
        g4_main_lay.addWidget(g4_hint)
        scroll_layout.addWidget(g4)
        
        scroll_layout.addStretch(1)
        settings_scroll.setWidget(scroll_content)
        right_layout.addWidget(settings_scroll)
        
        # Добавляем в сплиттер
        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        
        # Баланс сплиттера (40% / 60%)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        
        main_layout.addWidget(splitter)
    
    # ===================== AUTOSAVE MECHANICS =====================

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """
        Перехватчик событий (On-Blur Flush).
        Если текстовое поле теряет фокус, а таймер дебаунса еще тикает,
        мы принудительно сбрасываем буфер в реестр.
        """
        if event.type() == QEvent.Type.FocusOut:
            if self._save_timer.isActive():
                self._save_timer.stop()
                self._execute_save()
        return super().eventFilter(obj, event)

    def _on_text_changed(self, *args: Any) -> None:
        """Обработчик непрерывного ввода (Debounced)."""
        self.saveStatusChanged.emit(AutoSaveIndicator.SAVING, "Сохранение...")
        self._save_timer.start()

    def _on_discrete_changed(self, *args: Any) -> None:
        """Обработчик дискретных событий (чекбоксы, комбобоксы). Сохраняет мгновенно."""
        self.saveStatusChanged.emit(AutoSaveIndicator.SAVING, "Сохранение...")
        self._save_timer.stop()
        self._execute_save()

    def force_save(self) -> None:
        """
        Принудительный сброс буфера (Zero-Loss Flush).
        Вызывается из MainWindow.closeEvent перед уничтожением приложения.
        """
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._execute_save()

    def _execute_save(self) -> None:
        """Сбор данных с формы и транзакционное сохранение в реестр."""
        s = load_settings_from_registry()
        
        # 1. Threads
        s["auto_threads"] = self.inp_threads.text().strip()
        s["auto_shuffle"] = "1" if self.chk_shuffle.isChecked() else "0"
        s["auto_stop_enabled"] = "1" if self.chk_stop_limit.isChecked() else "0"
        s["auto_stop_count"] = self.inp_stop_count.text().strip()
        
        # 2. Timings
        s["auto_delay_min"] = self.inp_delay_min.text().strip()
        s["auto_delay_max"] = self.inp_delay_max.text().strip()
        s["auto_cool_down_min"] = self.inp_cool_min.text().strip()
        s["auto_cool_down_max"] = self.inp_cool_max.text().strip()
        
        # 3. Reliability
        s["auto_timeout"] = self.inp_timeout.text().strip()
        s["auto_retries"] = self.inp_retries.text().strip()
        s["auto_on_error"] = "keep" if self.cmb_on_error.currentIndex() == 1 else "close"
        
        # 4. Browser
        s["auto_unlock_wallets"] = "1" if self.chk_unlock.isChecked() else "0"
        s["auto_images"] = "1" if self.chk_images.isChecked() else "0"
        s["auto_headless"] = "1" if self.chk_headless.isChecked() else "0"
        s["auto_close_on_finish"] = "1" if self.chk_close.isChecked() else "0"
        
        ok, msg = save_settings_to_registry(s)
        if ok:
            self.saveStatusChanged.emit(AutoSaveIndicator.IDLE, "Настройки сохранены")
            logger.success("Настройки AUTO сохранены", profile_names=["GLOBAL"], category="AUTO")
        else:
            self.saveStatusChanged.emit(AutoSaveIndicator.ERROR, msg)
            logger.error(f"Ошибка сохранения: {msg}", profile_names=["GLOBAL"], category="AUTO")

    # ===================== UI HELPERS =====================

    def _create_mini_input(self, default: str) -> DebossedLineEdit:
        inp = DebossedLineEdit()
        inp.setText(default)
        inp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inp.setFixedWidth(60)
        
        inp.textChanged.connect(self._on_text_changed)
        inp.installEventFilter(self)
        
        return inp
    
    def _create_hint_label(self, text: str) -> QLabel:
        """Создает лейбл с подсказкой, применяя стиль и перенос слов."""
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(Styles.HINT_TEXT)
        return lbl
    
    def _load_settings_to_ui(self) -> None:
        """Загрузка настроек из реестра и применение их к UI без триггера автосохранения."""
        s = load_settings_from_registry()
        
        # 1. Threads
        # КРИТИЧНО: Блокируем сигналы именно на внутреннем QLineEdit,
        # так как DebossedLineEdit пробрасывает textChanged от него.
        with QSignalBlocker(self.inp_threads.inner_input):
            self.inp_threads.setText(s.get("auto_threads", "3"))
        with QSignalBlocker(self.chk_shuffle):
            self.chk_shuffle.setChecked(s.get("auto_shuffle", "0") == "1")
        with QSignalBlocker(self.chk_stop_limit):
            self.chk_stop_limit.setChecked(s.get("auto_stop_enabled", "0") == "1")
        with QSignalBlocker(self.inp_stop_count.inner_input):
            self.inp_stop_count.setText(s.get("auto_stop_count", "10"))
        
        # 2. Timings
        with QSignalBlocker(self.inp_delay_min.inner_input):
            self.inp_delay_min.setText(s.get("auto_delay_min", "5"))
        with QSignalBlocker(self.inp_delay_max.inner_input):
            self.inp_delay_max.setText(s.get("auto_delay_max", "20"))
        with QSignalBlocker(self.inp_cool_min.inner_input):
            self.inp_cool_min.setText(s.get("auto_cool_down_min", "30"))
        with QSignalBlocker(self.inp_cool_max.inner_input):
            self.inp_cool_max.setText(s.get("auto_cool_down_max", "60"))
        
        # 3. Reliability
        with QSignalBlocker(self.inp_timeout.inner_input):
            self.inp_timeout.setText(s.get("auto_timeout", "10"))
        with QSignalBlocker(self.inp_retries.inner_input):
            self.inp_retries.setText(s.get("auto_retries", "1"))
        with QSignalBlocker(self.cmb_on_error):
            on_err = s.get("auto_on_error", "close")
            idx = 1 if on_err == "keep" else 0
            self.cmb_on_error.setCurrentIndex(idx)
        
        # 4. Browser
        with QSignalBlocker(self.chk_unlock):
            self.chk_unlock.setChecked(s.get("auto_unlock_wallets", "1") == "1")
        with QSignalBlocker(self.chk_images):
            self.chk_images.setChecked(s.get("auto_images", "0") == "1")
        with QSignalBlocker(self.chk_headless):
            self.chk_headless.setChecked(s.get("auto_headless", "0") == "1")
        with QSignalBlocker(self.chk_close):
            self.chk_close.setChecked(s.get("auto_close_on_finish", "1") == "1")
    
    def _clear_profiles_layout(self) -> None:
        """Очистка контейнера профилей."""
        while self.profiles_layout.count():
            item = self.profiles_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
            elif item.layout():
                # Рекурсивно удаляем вложенные лейауты если есть
                while item.layout().count():
                    sub_item = item.layout().takeAt(0)
                    if sub_item.widget():
                        sub_item.widget().deleteLater()
        self._profile_checkboxes.clear()
        # Восстанавливаем spacer внизу
        self.profiles_layout.addStretch(1)
    
    @log_action("Запрос списка профилей (AUTO)", category="AUTO")
    def _start_bg_profile_load(self) -> None:
        """Инициирует фоновую загрузку профилей, не блокируя GUI."""
        s = load_settings_from_registry()
        url = s.get("api_url", "")
        if not url:
            logger.error("Не задан API URL в общих настройках", profile_names=["GLOBAL"], category="AUTO")
            return
        
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.setText("Загрузка...")
        
        # Запускаем тяжелый сетевой запрос в изолированном потоке
        threading.Thread(target=self._bg_worker_load_profiles, args=(url,), daemon=True).start()
    
    def _bg_worker_load_profiles(self, url: str) -> None:
        """Фоновый воркер для загрузки профилей."""
        try:
            grouped, logs = load_and_group_profiles(url)
            self.profilesLoaded.emit(grouped, logs)
        except Exception as e:
            self.profilesLoaded.emit({}, [(f"Критическая ошибка загрузки: {e}", "ERROR")])
    
    def _on_profiles_loaded(self, grouped: dict[str, list[dict[str, str]]], logs: list[tuple[str, str]]) -> None:
        """Слот, принимающий данные из фонового потока и обновляющий UI."""
        try:
            for msg, lvl in logs:
                if lvl == "ERROR":
                    logger.error(msg, profile_names=["GLOBAL"], category="API")
            
            with logger.block("Построение списка профилей", category="AUTO"):
                self._clear_profiles_layout()
                # Удаляем spacer, чтобы добавлять виджеты сверху
                self.profiles_layout.takeAt(0)
                
                total_profiles = 0
                
                for gname in sorted(grouped.keys(), key=str.lower):
                    profs = grouped[gname]
                    total_profiles += len(profs)
                    
                    # Заголовок группы
                    group_lbl = QLabel(f"{gname}")
                    group_lbl.setStyleSheet(f"font-weight: bold; color: {Colors.TXT_PRIMARY}; margin-top: 5px;")
                    group_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.profiles_layout.addWidget(group_lbl)
                    
                    # Сетка для профилей (2 колонки)
                    grid_widget = QWidget()
                    grid = QGridLayout(grid_widget)
                    grid.setContentsMargins(10, 0, 10, 5)
                    grid.setHorizontalSpacing(15)
                    grid.setVerticalSpacing(5)
                    
                    row, col = 0, 0
                    for p in profs:
                        name = p.get("name", "No Name")
                        uid = p.get("user_id", "")
                        
                        chk = QCheckBox(name)
                        chk.setStyleSheet(Styles.CHECKBOX)
                        chk.stateChanged.connect(self._on_checkbox_changed)
                        
                        self._profile_checkboxes[uid] = chk
                        
                        grid.addWidget(chk, row, col)
                        
                        col += 1
                        if col > 1:
                            col = 0
                            row += 1
                    
                    self.profiles_layout.addWidget(grid_widget)
                
                # Возвращаем spacer вниз
                self.profiles_layout.addStretch(1)
                
                self._update_counter_label()
                logger.success(f"Загружено профилей: {total_profiles}", profile_names=["GLOBAL"], category="AUTO")
        
        finally:
            self.refresh_btn.setText("Получить профили")
            self.refresh_btn.setEnabled(True)
    
    def _on_checkbox_changed(self, state: int) -> None:
        """Обработчик изменения состояния любого чекбокса."""
        self._counter_timer.start()
    
    def _set_all_checked(self, checked: bool) -> None:
        for chk in self._profile_checkboxes.values():
            chk.blockSignals(True)
            chk.setChecked(checked)
            chk.blockSignals(False)
        self._update_counter_label()
    
    def _update_counter_label(self) -> None:
        total = len(self._profile_checkboxes)
        checked = sum(1 for chk in self._profile_checkboxes.values() if chk.isChecked())
        self.lbl_counter.setText(f"Выбрано: {checked} из {total}")
    
    def get_selected_profile_ids(self) -> list[str]:
        """Возвращает список ID выбранных профилей."""
        return [uid for uid, chk in self._profile_checkboxes.items() if chk.isChecked()]


# ===================== Панель Режима Auto =====================

class AutoPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("auto-panel")
        
        # Основной layout
        root_v = QVBoxLayout(self)
        root_v.setContentsMargins(0, 0, 0, 0)
        root_v.setSpacing(8)
        
        # 1. Верхняя панель табов
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(0, 0, 0, 0)
        tabs_row.setSpacing(6)
        
        self.code_btn = self._create_tab_btn("Code", checked=True)
        self.settings_btn = self._create_tab_btn("Настройки", checked=False)
        
        tabs_row.addWidget(self.code_btn)
        tabs_row.addWidget(self.settings_btn)
        tabs_row.addStretch(1)
        root_v.addLayout(tabs_row)
        
        # 2. Стек страниц
        self.stack = QStackedWidget(self)
        root_v.addWidget(self.stack, 1)
        
        # --- Страница Code ---
        code_page = QWidget(self)
        code_v = QVBoxLayout(code_page)
        code_v.setContentsMargins(0, 6, 0, 0)
        code_v.setSpacing(10)
        
        title = QLabel("Автоматизация с использованием Selenium 🤖 web3")
        title.setObjectName("auto-code-title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet(f"font-size: 15px; font-weight: 600; color: {Colors.TXT_PRIMARY};")
        code_v.addWidget(title)
        
        guide = QLabel(Texts.GUIDE_AUTO)
        guide.setWordWrap(True)
        guide.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        code_v.addWidget(guide)
        
        self.code_edit = CodeEditor(code_page)
        self.code_edit.setPlaceholderText("Вставьте сюда код вашей автоматизации (Selenium 🤖 web3)...")
        self.code_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.code_edit.setStyleSheet(Styles.CODE_EDITOR)
        code_v.addWidget(self.code_edit, 1)
        
        # Подключение логики редактора
        init_code_editor(self.code_edit)
        
        # Кнопки управления
        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addStretch(1)
        
        self.start_btn = self._create_action_btn("▶️ Старт", enabled=False)
        self.save_btn = self._create_action_btn("💾 Сохранить", enabled=True)
        self.stop_btn = self._create_action_btn("⏹ Стоп", enabled=False)
        
        controls.addWidget(self.start_btn)
        controls.addWidget(self.save_btn)
        controls.addWidget(self.stop_btn)
        controls.addStretch(1)
        code_v.addLayout(controls)
        
        disclaimer = QLabel(Texts.DISCLAIMER_AUTO)
        disclaimer.setAlignment(Qt.AlignmentFlag.AlignCenter)
        code_v.addWidget(disclaimer)
        
        self.stack.addWidget(code_page)
        
        # --- Страница Настроек ---
        self.settings_panel = AutoSettingsPanel(self)
        self.stack.addWidget(self.settings_panel)
        
        # Сигналы
        self.code_btn.clicked.connect(self._show_code)
        self.settings_btn.clicked.connect(self._show_settings)
        self.save_btn.clicked.connect(self._save_code)
    
    def _create_tab_btn(self, text: str, checked: bool) -> QPushButton:
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setChecked(checked)
        btn.setProperty("class", "mass-action")
        btn.setMinimumHeight(26)
        btn.setMaximumHeight(36)
        btn.setStyleSheet(Styles.BTN_ACTION)
        return btn
    
    def _create_action_btn(self, text: str, enabled: bool) -> QPushButton:
        btn = QPushButton(text)
        btn.setProperty("class", "mass-action")
        btn.setEnabled(enabled)
        btn.setMinimumHeight(26)
        btn.setMaximumHeight(36)
        btn.setStyleSheet(Styles.BTN_ACTION)
        return btn
    
    def _sync_tab_checks(self, code_active: bool) -> None:
        self.code_btn.blockSignals(True)
        self.settings_btn.blockSignals(True)
        self.code_btn.setChecked(code_active)
        self.settings_btn.setChecked(not code_active)
        self.code_btn.blockSignals(False)
        self.settings_btn.blockSignals(False)
    
    def _show_code(self) -> None:
        self.stack.setCurrentIndex(0)
        self._sync_tab_checks(True)
    
    def _show_settings(self) -> None:
        self.stack.setCurrentIndex(1)
        self._sync_tab_checks(False)
    
    @log_action("Сохранение скрипта (GUI)", category="AUTO")
    def _save_code(self) -> None:
        text = self.code_edit.toPlainText()
        fname, selected = QFileDialog.getSaveFileName(
            self,
            "Сохранить скрипт",
            "script.py",
            "Python (*.py);;Текстовый (*.txt);;Все файлы (*.*)"
        )
        if not fname:
            return
        
        selected_l = (selected or "").lower()
        preferred_ext = ".txt" if ".txt" in selected_l else ".py"
        
        ok, info = save_code_to_file(text, fname, preferred_ext=preferred_ext, allowed_exts=(".py", ".txt"))
        
        if ok:
            try:
                QMessageBox.information(self, "Сохранено", f"Файл сохранён:\n{info}")
            except Exception:
                pass
        else:
            try:
                QMessageBox.warning(self, "Ошибка сохранения", f"Не удалось сохранить файл:\n{info}")
            except Exception:
                pass