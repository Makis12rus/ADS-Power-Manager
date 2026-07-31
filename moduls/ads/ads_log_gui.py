"""
Модуль: moduls/ads/ads_log_gui.py
Назначение: Информационное табло (Presentation Layer) для вывода системных логов.
Зона ответственности: Прием чистых DTO от логгера через систему сигналов (Observer),
                      их HTML-рендеринг на основе выбранной пользователем темы,
                      фильтрация вывода по уровням и профилям.
                      Реализует аппаратную анимацию текста (Typewriter Effect)
                      с защитным механизмом Catch-Up Protocol для предотвращения
                      зависаний UI при лавинообразном потоке данных.
                      Использует автономный кэшированный фон (Bake and Blit)
                      и движок плавного скольжения (Smooth Scroll Engine).
Интеграция: Подписывается на события `system.logger`. Строго отделяет визуализацию от
            бизнес-логики сбора логов. Сохраняет свои настройки в реестр через `core.core`.
            Использует StaticVolumetricBackdropWidget и GlassTile для премиального рендеринга.
            Стиль скроллбаров наследуется глобально от главного окна.
            Является частью плоского пакета `moduls/ads/`.
"""

import re
from typing import Any

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPlainTextEdit, QHBoxLayout, QPushButton,
    QComboBox, QLabel, QGridLayout, QApplication, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, QTimeLine
from PySide6.QtGui import QTextCursor, QColor

# Строгие абсолютные импорты (Monorepo Style)
from system.logger import logger
from core.core import load_ui_geometry, save_ui_geometry
from core.style import (
    Styles, LogStyles, StaticVolumetricBackdropWidget, GlassTile,
    SmoothScrollBar, SmoothScrollDelegate
)

# Регулярка для фильтрации ID-подобных профилей (дублирует логику logger, но локально для скорости UI)
_PROFILE_ID_RE = re.compile(r'^[a-z0-9]{6,}$')


# ===================== Класс окна логов =====================

class LogWindow(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ADSProfile Manager — Логи")
        
        # Убираем жесткий хардкод высоты, оставляем только защиту ширины для фильтров
        self.setMinimumWidth(350)
        
        self._known_profiles: set[str] = set()  # Кеш для быстрой проверки новых профилей
        
        self.level_filter: str = "ALL"
        self.profile_filter: str = "ALL"
        
        # --- Состояние анимации (Typewriter Effect & Catch-Up Protocol) ---
        self._pending_anim_queue: list[dict[str, Any]] = []
        self._is_animating: bool = False
        self._current_payload: str = ""
        self._current_text_pos: int = 0
        self._anim_cursor: QTextCursor | None = None
        
        # Аппаратный таймлайн для плавной анимации (ускорено до 300 мс для многопоточности)
        self._timeline = QTimeLine(200, self)
        self._timeline.setUpdateInterval(16)  # ~60 FPS
        self._timeline.frameChanged.connect(self._on_timeline_frame)
        self._timeline.finished.connect(self._on_timeline_finished)
        
        # Загрузка настроек отображения
        ui_conf = load_ui_geometry()
        self.current_theme = ui_conf.get("log_theme", "REGULAR")
        try:
            self.current_font_size = int(ui_conf.get("log_font_size", "10"))
        except ValueError:
            self.current_font_size = 10
        
        self.build_ui()
        self.apply_theme(self.current_theme)
        
        # =====================================================================
        # ПОДКЛЮЧЕНИЕ К "РАДИОВЫШКЕ" ЛОГГЕРА (ПАТТЕРН OBSERVER)
        # =====================================================================
        # Принудительно инициализируем сигналы логгера, так как QApplication уже жив
        logger._ensure_qt_initialized()
        
        if logger.signals:
            logger.signals.log_signal.connect(self.append_log_entry)
            logger.signals.clear_signal.connect(self._ui_execute_clear)
        
        # "Всасываем" исторический буфер логов мгновенно (без анимации)
        for log_entry in logger.get_buffer():
            # Обновляем кэш профилей
            profiles = log_entry.get("profile_names", [])
            for p in profiles:
                p_str = str(p)
                if p_str != "GLOBAL" and p_str not in self._known_profiles:
                    if not _PROFILE_ID_RE.fullmatch(p_str):
                        self._known_profiles.add(p_str)
                        self.profile_box.addItem(p_str)
            
            if self._log_matches_filter(log_entry):
                html = self.make_log_html(log_entry)
                self.log_text.appendHtml(html)
        
        self._scroll_to_bottom()
        
        # Инициализация фильтров (отложенная, чтобы не блокировать отрисовку окна)
        QTimer.singleShot(50, self._initial_filter_load)
    
    def build_ui(self) -> None:
        # Корневой layout без отступов для фонового виджета
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        
        # 1. Космическая подложка (Premium PCB Engine)
        self.backdrop = StaticVolumetricBackdropWidget(self)
        main_layout.addWidget(self.backdrop)
        
        # Layout поверх подложки
        backdrop_layout = QVBoxLayout(self.backdrop)
        backdrop_layout.setContentsMargins(10, 10, 10, 10)
        backdrop_layout.setSpacing(10)
        
        # --- Блок фильтров ---
        filters_grid = QGridLayout()
        filters_grid.setHorizontalSpacing(16)
        filters_grid.setVerticalSpacing(2)
        
        # Лейблы
        self.theme_label = QLabel("Тема")
        self.fontsize_label = QLabel("Шрифт")
        self.level_label = QLabel("Уровень")
        self.profile_label = QLabel("Профиль")
        
        for lbl in (self.theme_label, self.fontsize_label, self.level_label, self.profile_label):
            lbl.setStyleSheet(Styles.LABEL_LOG_HEADER)
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        
        # Комбобоксы
        self.theme_box = QComboBox()
        self.theme_box.addItems(["REGULAR", "MATRIX", "NEON"])
        self.theme_box.setCurrentText(self.current_theme)
        
        self.fontsize_box = QComboBox()
        self.fontsize_box.addItems(["8", "10", "12", "14"])
        self.fontsize_box.setCurrentText(str(self.current_font_size))
        
        self.filter_box = QComboBox()
        self.filter_box.addItems(["ALL", "INFO", "SUCCESS", "WARNING", "ERROR", "START", "DEFAULT"])
        self.filter_box.setCurrentText("ALL")
        
        self.profile_box = QComboBox()
        self.profile_box.addItem("ALL")
        
        for box in (self.theme_box, self.fontsize_box, self.filter_box, self.profile_box):
            box.setStyleSheet(Styles.COMBO_BOX_LOG)
            box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        
        # Подключение сигналов
        self.theme_box.currentTextChanged.connect(self.theme_changed)
        self.fontsize_box.currentTextChanged.connect(self.fontsize_changed)
        self.filter_box.currentTextChanged.connect(self.on_level_filter)
        self.profile_box.currentTextChanged.connect(self.on_profile_filter)
        
        # Размещение в сетке
        filters_grid.addWidget(self.theme_label, 0, 0)
        filters_grid.addWidget(self.fontsize_label, 0, 1)
        filters_grid.addWidget(self.level_label, 0, 2)
        filters_grid.addWidget(self.profile_label, 0, 3)
        
        filters_grid.addWidget(self.theme_box, 1, 0)
        filters_grid.addWidget(self.fontsize_box, 1, 1)
        filters_grid.addWidget(self.filter_box, 1, 2)
        filters_grid.addWidget(self.profile_box, 1, 3)
        filters_grid.setColumnStretch(4, 1)
        
        backdrop_layout.addLayout(filters_grid)
        
        # --- 2. Бронированная витрина (Glassmorphic Shield) ---
        self.glass_tile = GlassTile(self.backdrop, enable_hover=False)
        glass_layout = QVBoxLayout(self.glass_tile)
        glass_layout.setContentsMargins(4, 4, 4, 4)
        glass_layout.setSpacing(0)
        
        # --- Текстовое поле (Высокопроизводительный движок) ---
        self.log_text = QPlainTextEdit(self.glass_tile)
        self.log_text.setReadOnly(True)
        self.log_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        
        # Gutter Isolation: Принудительно резервируем место под скроллбар,
        # чтобы верстка не прыгала при очистке или наполнении логов.
        self.log_text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.log_text.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        # --- Интеграция Smooth Scroll Engine ---
        self.smooth_scrollbar = SmoothScrollBar(self.log_text)
        self.smooth_scrollbar.setOrientation(Qt.Orientation.Vertical)
        self.log_text.setVerticalScrollBar(self.smooth_scrollbar)
        
        self.smooth_delegate = SmoothScrollDelegate(self.smooth_scrollbar, Qt.Orientation.Vertical, self.log_text)
        self.log_text.viewport().installEventFilter(self.smooth_delegate)
        
        # Resource Guard: Защита от утечек памяти при аптайме в несколько суток
        self.log_text.setMaximumBlockCount(5000)
        
        # Делаем поле "резиновым", но позволяем ему сжиматься, чтобы не выдавливать кнопки
        self.log_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log_text.setMinimumHeight(100)
        
        # КРИТИЧНО: Отключаем автозаливку системного холста (The Viewport Trick).
        # Это позволяет тексту рендериться прямо поверх стеклянной плитки и космического фона.
        self.log_text.viewport().setAutoFillBackground(False)
        
        glass_layout.addWidget(self.log_text)
        backdrop_layout.addWidget(self.glass_tile, 1)
        
        # --- Нижняя панель кнопок ---
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(14)
        bottom_row.addStretch(1)
        
        self.clear_btn = QPushButton("🗑️ Очистить лог")
        self.copy_btn = QPushButton("📋 Копировать лог")
        
        for btn in (self.clear_btn, self.copy_btn):
            btn.setMinimumWidth(110)
            # Жестко фиксируем размер кнопок, чтобы QPlainTextEdit не смог их сплющить
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.setStyleSheet(Styles.BTN_LOG_MINI)
        
        bottom_row.addWidget(self.clear_btn)
        bottom_row.addSpacing(10)
        bottom_row.addWidget(self.copy_btn)
        bottom_row.addStretch(1)
        
        backdrop_layout.addLayout(bottom_row)
        
        # Подключаем кнопку очистки к методу-запросу (Command)
        self.clear_btn.clicked.connect(self.clear_logs_request)
        self.copy_btn.clicked.connect(self.copy_log)
    
    # --- Логика отображения и рендеринга ---
    
    def make_log_html(self, log_entry: dict[str, Any]) -> str:
        """
        Превращает чистый DTO-словарь лога в отформатированный HTML-код.
        Используется для мгновенного вывода (Catch-Up Protocol) и фильтрации.
        """
        ts = log_entry.get("timestamp", "")
        level = (log_entry.get("level", "INFO") or "INFO").upper()
        
        style_info = LogStyles.LEVELS.get(level, LogStyles.LEVELS["DEFAULT"])
        color = style_info["color"]
        emoji = style_info["emoji"]
        
        profiles_raw = log_entry.get("profile_names", ["GLOBAL"])
        category = log_entry.get("category", "SYSTEM")
        
        # Безопасная замена переносов строк для HTML
        msg = str(log_entry.get("message", "")).replace("\n", "<br>")
        
        profiles_visible = [str(p) for p in profiles_raw if str(p) != "GLOBAL"]
        if not profiles_visible:
            profiles_visible = ["GLOBAL"]
        
        profiles_block = " ".join([f"[{p}]" for p in profiles_visible])
        
        html = (
            f'<span style="font-size:{int(self.current_font_size * 1.33)}px;">'
            f'<span style="color:#8A8A8A;">[{ts}]</span> '
            f'{emoji} '
            f'<span style="color:{color};">[{level}] {profiles_block} [{category}] : {msg}</span>'
            f'</span>'
        )
        return html
    
    def _make_prefix_html(self, log_entry: dict[str, Any]) -> tuple[str, str]:
        """
        Генерирует только префикс лога (без сообщения) для старта анимации.
        Возвращает HTML-префикс и цвет текста для настройки виртуальной каретки.
        """
        ts = log_entry.get("timestamp", "")
        level = (log_entry.get("level", "INFO") or "INFO").upper()
        
        style_info = LogStyles.LEVELS.get(level, LogStyles.LEVELS["DEFAULT"])
        color = style_info["color"]
        emoji = style_info["emoji"]
        
        profiles_raw = log_entry.get("profile_names", ["GLOBAL"])
        category = log_entry.get("category", "SYSTEM")
        
        profiles_visible = [str(p) for p in profiles_raw if str(p) != "GLOBAL"]
        if not profiles_visible:
            profiles_visible = ["GLOBAL"]
        
        profiles_block = " ".join([f"[{p}]" for p in profiles_visible])
        
        html = (
            f'<span style="font-size:{int(self.current_font_size * 1.33)}px;">'
            f'<span style="color:#8A8A8A;">[{ts}]</span> '
            f'{emoji} '
            f'<span style="color:{color};">[{level}] {profiles_block} [{category}] : </span>'
            f'</span>'
        )
        return html, color
    
    def theme_changed(self, theme_name: str) -> None:
        self.current_theme = theme_name
        self.apply_theme(theme_name)
        self.apply_filter()  # Перерисовка с новым стилем
        save_ui_geometry(log_theme=theme_name)
    
    def apply_theme(self, theme_name: str) -> None:
        """
        Применяет выбранную тему к текстовому полю.
        Стиль скроллбаров теперь наследуется каскадно от главного окна.
        """
        base_style = LogStyles.THEMES.get(theme_name, LogStyles.THEMES["REGULAR"])
        self.log_text.setStyleSheet(base_style)
    
    def fontsize_changed(self, size: str) -> None:
        try:
            self.current_font_size = int(size)
        except ValueError:
            self.current_font_size = 10
        self.apply_filter()  # Перерисовка с новым размером
        save_ui_geometry(log_font_size=str(self.current_font_size))
    
    # ===================== АНИМАЦИЯ И ОЧЕРЕДЬ (TYPEWRITER ENGINE) =====================
    
    def append_log_entry(self, log_entry: dict[str, Any]) -> None:
        """
        Принимает чистый DTO от логгера (через сигнал).
        Обновляет кэш профилей и ставит лог в очередь на анимацию.
        """
        profiles = log_entry.get("profile_names", [])
        for p in profiles:
            p_str = str(p)
            if p_str != "GLOBAL" and p_str not in self._known_profiles:
                if not _PROFILE_ID_RE.fullmatch(p_str):
                    self._known_profiles.add(p_str)
                    self.profile_box.addItem(p_str)
        
        if self._log_matches_filter(log_entry):
            self._pending_anim_queue.append(log_entry)
            if not self._is_animating:
                self._process_next_log()
    
    def _process_next_log(self) -> None:
        """
        Диспетчер очереди логов.
        Реализует Catch-Up Protocol: если логов слишком много, сбрасывает их мгновенно.
        """
        if not self._pending_anim_queue:
            self._is_animating = False
            return
            
        self._is_animating = True
        
        # CATCH-UP PROTOCOL: Защита от лавины данных и зависания UI
        # Порог увеличен до 25 для обеспечения плавной анимации при многопоточном запуске
        if len(self._pending_anim_queue) > 10:
            for dto in self._pending_anim_queue:
                self.log_text.appendHtml(self.make_log_html(dto))
            self._pending_anim_queue.clear()
            self._is_animating = False
            self._scroll_to_bottom()
            return
            
        # ШТАТНАЯ АНИМАЦИЯ
        current_dto = self._pending_anim_queue.pop(0)
        prefix_html, color = self._make_prefix_html(current_dto)
        self._current_payload = str(current_dto.get("message", ""))
        
        # Если сообщение пустое, просто выводим префикс и идем дальше
        if not self._current_payload:
            self.log_text.appendHtml(prefix_html)
            self._scroll_to_bottom()
            self._is_animating = False
            self._process_next_log()
            return
            
        # 1. Вставляем префикс (создается новый абзац)
        self.log_text.appendHtml(prefix_html)
        
        # 2. Инициализируем виртуальную каретку в конце нового абзаца
        self._anim_cursor = self.log_text.textCursor()
        self._anim_cursor.movePosition(QTextCursor.MoveOperation.End)
        
        # 3. Настраиваем формат каретки, чтобы вставляемый текст наследовал цвет уровня лога
        fmt = self._anim_cursor.charFormat()
        fmt.setForeground(QColor(color))
        self._anim_cursor.setCharFormat(fmt)
        
        # 4. Запускаем аппаратный таймлайн
        self._current_text_pos = 0
        self._timeline.setFrameRange(0, len(self._current_payload))
        self._timeline.start()
    
    def _on_timeline_frame(self, frame_index: int) -> None:
        """Вставка порции текста на каждом тике анимации (Zero-Reflow)."""
        if not self._anim_cursor:
            return
            
        chunk = self._current_payload[self._current_text_pos : frame_index]
        if chunk:
            self._anim_cursor.insertText(chunk)
            self._current_text_pos = frame_index
            self._scroll_to_bottom()
            
    def _on_timeline_finished(self) -> None:
        """Завершение анимации текущего лога и переход к следующему."""
        # Допечатываем остаток, если таймлайн остановился чуть раньше
        if self._anim_cursor and self._current_text_pos < len(self._current_payload):
            chunk = self._current_payload[self._current_text_pos:]
            self._anim_cursor.insertText(chunk)
            self._scroll_to_bottom()
            
        self._is_animating = False
        self._anim_cursor = None
        self._process_next_log()
        
    def _stop_animation(self) -> None:
        """Экстренная остановка анимации (при очистке или смене фильтров)."""
        if self._timeline.state() == QTimeLine.State.Running:
            self._timeline.stop()
        self._pending_anim_queue.clear()
        self._is_animating = False
        self._anim_cursor = None
        
    def _scroll_to_bottom(self) -> None:
        """Надежная прокрутка к последнему вставленному символу с поддержкой Smooth Scroll."""
        if self._anim_cursor:
            self.log_text.setTextCursor(self._anim_cursor)
            self.log_text.ensureCursorVisible()
        else:
            scrollbar = self.log_text.verticalScrollBar()
            if hasattr(scrollbar, 'scroll_to'):
                scrollbar.scroll_to(scrollbar.maximum())
            else:
                scrollbar.setValue(scrollbar.maximum())
    
    # --- Логика очистки (Разделенная) ---
    
    def clear_logs_request(self) -> None:
        """
        Отправляет запрос системному логгеру на очистку буфера.
        Не трогает UI напрямую! Ждет ответного сигнала `clear_signal`.
        """
        logger.clear()
    
    def _ui_execute_clear(self) -> None:
        """
        Фактическая очистка интерфейса. Срабатывает ТОЛЬКО по сигналу от логгера.
        Разрывает рекурсивную петлю вызовов.
        """
        self._stop_animation()
        self.log_text.clear()
        # Сбрасываем кеш профилей, но оставляем "ALL"
        self._known_profiles.clear()
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_box.addItem("ALL")
        self.profile_box.blockSignals(False)
    
    def copy_log(self) -> None:
        text = self.log_text.toPlainText()
        if text:
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
    
    # --- Фильтрация ---
    
    def _initial_filter_load(self) -> None:
        """Первоначальная загрузка фильтров при старте окна."""
        # Загружаем уникальные значения из логгера, если там уже что-то есть
        values = logger.get_unique_values("profile_names")
        self.profile_box.blockSignals(True)
        for v in values:
            v_str = str(v)
            if v_str != "GLOBAL" and v_str not in self._known_profiles:
                self._known_profiles.add(v_str)
                self.profile_box.addItem(v_str)
        self.profile_box.blockSignals(False)
    
    def on_level_filter(self, level: str) -> None:
        self.level_filter = level
        self.apply_filter()
    
    def on_profile_filter(self, profile: str) -> None:
        self.profile_filter = profile
        self.apply_filter()
    
    def apply_filter(self) -> None:
        """Полная перерисовка текста при смене фильтров или настроек вида."""
        self._stop_animation()
        self.log_text.clear()
        
        # Используем фильтрацию логгера для получения нужного среза
        logs = logger.filter_logs(
            level=self.level_filter,
            profile=self.profile_filter
        )
        
        # Пакетная вставка (мгновенная, без анимации)
        for log_entry in logs:
            html = self.make_log_html(log_entry)
            self.log_text.appendHtml(html)
        
        self._scroll_to_bottom()
    
    def _log_matches_filter(self, log_entry: dict[str, Any]) -> bool:
        """Быстрая локальная проверка для новых логов."""
        if self.level_filter != "ALL":
            msg_level = (log_entry.get("level") or "INFO").upper()
            if msg_level != self.level_filter:
                return False
        
        if self.profile_filter != "ALL":
            entry_profiles = log_entry.get("profile_names", ["GLOBAL"])
            if self.profile_filter not in entry_profiles:
                return False
        
        return True