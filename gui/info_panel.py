"""
Модуль: gui/info_panel.py
Назначение: Изолированный компонент справочной панели (Presentation Layer).
Зона ответственности: Отрисовка статического HTML-текста с инструкциями и
                      информацией о программе. Оснащен движком плавного
                      скольжения (Smooth Scroll Engine).
Интеграция: Абсолютно независимый виджет. Не импортирует MainWindow или другие панели.
            Получает тексты и стили через ленивый фасад core.style.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QTextEdit
from PySide6.QtCore import Qt

# Строгие абсолютные импорты фасада стилей (Lazy Loading)
from core.style import Styles, Colors, Texts, SmoothScrollBar, SmoothScrollDelegate


class InfoPanel(QWidget):
    """
    Панель со справочной информацией и инструкциями.
    Работает как изолированный, легковесный виджет (Stateless).
    """
    
    def __init__(self, parent: QWidget | None = None) -> None:
        """
        Инициализация информационной панели.

        :param parent: Родительский виджет (обычно контейнер в MainWindow).
        """
        super().__init__(parent)
        
        # Включаем поддержку QSS для кастомного фона, если он будет задан
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        
        self._setup_ui()
    
    def _setup_ui(self) -> None:
        """Инициализация пользовательского интерфейса панели."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        
        # --- Заголовок ---
        title = QLabel("ℹ️ О программе и краткая инструкция")
        title.setStyleSheet(f"font-size: 20px; font-weight: 700; color: {Colors.TXT_ACCENT};")
        layout.addWidget(title)
        
        # --- Текстовое поле (Справочник) ---
        # Используем QTextEdit вместо QLabel для поддержки выделения текста,
        # копирования и нативного скроллинга при большом объеме информации.
        txt = QTextEdit()
        txt.setReadOnly(True)
        
        # Защита от случайного редактирования и фокуса
        txt.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse |
            Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        
        # Gutter Isolation: Принудительно резервируем место под скроллбар
        txt.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        txt.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        # --- Интеграция Smooth Scroll Engine ---
        self.smooth_scrollbar = SmoothScrollBar(txt)
        self.smooth_scrollbar.setOrientation(Qt.Orientation.Vertical)
        txt.setVerticalScrollBar(self.smooth_scrollbar)
        
        self.smooth_delegate = SmoothScrollDelegate(self.smooth_scrollbar, Qt.Orientation.Vertical, txt)
        txt.viewport().installEventFilter(self.smooth_delegate)
        
        txt.setStyleSheet(Styles.INFO_TEXT)
        txt.setHtml(Texts.HELP_MAIN)
        
        # Растягиваем текстовое поле на всё доступное пространство
        layout.addWidget(txt, stretch=1)