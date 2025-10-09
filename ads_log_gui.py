# =========================
# 📝 Файл: ads_log_gui.py
# =========================

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QHBoxLayout, QPushButton, QComboBox, QSizePolicy, QLabel, QGridLayout
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QTextCursor
from logger import logger


class LogWindow(QWidget):
    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ADSProfile Manager — Логи")
        self.setMinimumSize(440, 540)
        self.all_logs = []
        self.level_filter = "ALL"
        self.profile_filter = "ALL"
        self.current_font_size = 10
        self.current_theme = "REGULAR"
        
        self.build_ui()
        logger.set_log_window(self)
        self.apply_theme(self.current_theme)
    
    def build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        
        # Новый аккуратный блок фильтров — лейблы над комбобоксами, всё по центру
        filters_grid = QGridLayout()
        filters_grid.setHorizontalSpacing(16)
        filters_grid.setVerticalSpacing(2)
        
        # Подписи (лежат над фильтрами, выравнены по центру)
        label_style = """
            QLabel[class="filter-label"] {
                color: #A0A0A0;
                font-size: 12px;
                font-weight: bold;
                min-width: 48px;
                qproperty-alignment: AlignHCenter;
            }
        """
        box_style = """
            QComboBox {
                background: #282B2E;
                color: #FFD700;
                border: 1px solid #888888;
                border-radius: 6px;
                padding: 1px 6px;
                min-width: 60px;
                max-width: 96px;
                font-size: 12px;
            }
            QComboBox QAbstractItemView {
                background: #232629;
                color: #FFD700;
                selection-background-color: #FFD700;
                selection-color: #232629;
            }
        """
        
        self.theme_label = QLabel("Тема")
        self.theme_label.setProperty("class", "filter-label")
        self.theme_label.setStyleSheet(label_style)
        self.theme_label.setAlignment(Qt.AlignHCenter)
        
        self.fontsize_label = QLabel("Шрифт")
        self.fontsize_label.setProperty("class", "filter-label")
        self.fontsize_label.setStyleSheet(label_style)
        self.fontsize_label.setAlignment(Qt.AlignHCenter)
        
        self.level_label = QLabel("Уровень")
        self.level_label.setProperty("class", "filter-label")
        self.level_label.setStyleSheet(label_style)
        self.level_label.setAlignment(Qt.AlignHCenter)
        
        self.profile_label = QLabel("Профиль")
        self.profile_label.setProperty("class", "filter-label")
        self.profile_label.setStyleSheet(label_style)
        self.profile_label.setAlignment(Qt.AlignHCenter)
        
        self.theme_box = QComboBox()
        self.theme_box.addItems(["REGULAR", "MATRIX", "NEON"])
        self.theme_box.setCurrentIndex(0)
        self.theme_box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.theme_box.currentTextChanged.connect(self.theme_changed)
        self.theme_box.setStyleSheet(box_style)
        
        self.fontsize_box = QComboBox()
        self.fontsize_box.addItems(["8", "10", "12", "14"])
        self.fontsize_box.setCurrentText("10")
        self.fontsize_box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.fontsize_box.currentTextChanged.connect(self.fontsize_changed)
        self.fontsize_box.setStyleSheet(box_style)
        
        self.filter_box = QComboBox()
        self.filter_box.addItems(["ALL", "INFO", "SUCCESS", "WARNING", "ERROR", "START", "DEFAULT"])
        self.filter_box.setCurrentText("ALL")
        self.filter_box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.filter_box.currentTextChanged.connect(self.on_level_filter)
        self.filter_box.setStyleSheet(box_style)
        
        self.profile_box = QComboBox()
        self.profile_box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.profile_box.currentTextChanged.connect(self.on_profile_filter)
        self.profile_box.setStyleSheet(box_style)
        
        # Первая строка — подписи
        filters_grid.addWidget(self.theme_label, 0, 0, alignment=Qt.AlignHCenter)
        filters_grid.addWidget(self.fontsize_label, 0, 1, alignment=Qt.AlignHCenter)
        filters_grid.addWidget(self.level_label, 0, 2, alignment=Qt.AlignHCenter)
        filters_grid.addWidget(self.profile_label, 0, 3, alignment=Qt.AlignHCenter)
        # Вторая строка — фильтры
        filters_grid.addWidget(self.theme_box, 1, 0)
        filters_grid.addWidget(self.fontsize_box, 1, 1)
        filters_grid.addWidget(self.filter_box, 1, 2)
        filters_grid.addWidget(self.profile_box, 1, 3)
        filters_grid.setColumnStretch(4, 1)
        
        layout.addLayout(filters_grid)
        
        self.log_text = QTextEdit(self)
        self.log_text.setReadOnly(True)
        self.log_text.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.log_text.setStyleSheet("""
            QTextEdit {
                background-color: #232629;
                border: 1px solid #292A2D;
                color: #ECECEC;
                font-family: 'Segoe UI', Arial, sans-serif;
                font-size: 12px;
                padding: 8px;
                border-radius: 10px;
                letter-spacing: 0.2px;
            }
        """)
        layout.addWidget(self.log_text)
        
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(14)
        bottom_row.addStretch(1)
        self.clear_btn = QPushButton("🗑️ Очистить лог")
        self.copy_btn = QPushButton("📋 Копировать лог")
        self.clear_btn.setMinimumWidth(110)
        self.copy_btn.setMinimumWidth(110)
        # Универсальный стиль для кнопок
        common_btn_style = """
            QPushButton {
                background: #232629;
                color: #F0F0F0;
                border: 0.5px groove #5A5A5A;
                border-radius: 6px;
                padding: 2px 7px;
                font-size: 11px;
                min-height: 18px;
                min-width: 76px;
                max-height: 24px;
            }
            QPushButton:hover:!disabled {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #FFE066,
                    stop:1 #FFB800
                );
                color: #232629;
                border: 1px solid #FFE066;
            }
            QPushButton:pressed {
                background: qlineargradient(
                    x1:0, y1:0, x2:1, y2:1,
                    stop:0 #FFC300,
                    stop:1 #FFD700
                );
            }
            QPushButton:disabled {
                background: #35393C;
                color: #767676;
                border: 1px solid #434343;
            }
        """
        
        self.clear_btn.setStyleSheet(common_btn_style)
        self.copy_btn.setStyleSheet(common_btn_style)
        bottom_row.addWidget(self.clear_btn)
        bottom_row.addSpacing(10)
        bottom_row.addWidget(self.copy_btn)
        bottom_row.addStretch(1)
        layout.addLayout(bottom_row)
        
        self.clear_btn.clicked.connect(self.clear_logs)
        self.copy_btn.clicked.connect(self.copy_log)
        
        QTimer.singleShot(50, self.init_filters)
    
    def theme_changed(self, theme_name: str):
        self.current_theme = theme_name
        self.apply_theme(theme_name)
        self.apply_filter()
    
    def apply_theme(self, theme_name: str):
        # Переключение темы влияет только на лог-окно
        if theme_name == "MATRIX":
            self.log_text.setStyleSheet("""
                QTextEdit {
                    background-color: #0b0f09;
                    border: 1px solid #133d13;
                    color: #00FF5A;
                    font-family: 'Segoe UI', Arial, sans-serif;
                    font-size: 12px;
                    padding: 8px;
                    letter-spacing: 0.2px;
                    border-radius: 10px;
                }
            """)
        elif theme_name == "NEON":
            self.log_text.setStyleSheet("""
                QTextEdit {
                    background-color: #120026;
                    border: 1px solid #7b42f6;
                    color: #00fff7;
                    font-family: 'Segoe UI', Arial, sans-serif;
                    font-size: 12px;
                    padding: 8px;
                    letter-spacing: 0.2px;
                    text-shadow: 0 0 4px #7d5fff, 0 0 6px #00fff7;
                    border-radius: 10px;
                }
            """)
        else:
            self.log_text.setStyleSheet("""
                QTextEdit {
                    background-color: #232629;
                    border: 1px solid #292A2D;
                    color: #ECECEC;
                    font-family: 'Segoe UI', Arial, sans-serif;
                    font-size: 12px;
                    padding: 8px;
                    border-radius: 10px;
                    letter-spacing: 0.2px;
                }
            """)
    
    def fontsize_changed(self, size: str):
        try:
            int_size = int(size)
        except Exception:
            int_size = 10
        self.current_font_size = int_size
        self.apply_filter()
    
    def append_log_html(self, html: str, log_entry):
        # Без подсветки/анимации: просто добавляем запись и обновляем фильтры/отображение
        self.all_logs.append(log_entry)
        self.init_filters()
        if self._log_matches_filter(log_entry):
            html_with_font = f'<span style="font-size:{self.current_font_size * 1.33}px;">{html}</span>'
            self.log_text.append(html_with_font)
            self.log_text.moveCursor(QTextCursor.End)
    
    def clear_logs(self) -> None:
        self.all_logs = []
        self.log_text.clear()
        self.init_filters()
    
    def copy_log(self) -> None:
        text = self.log_text.toPlainText()
        if text:
            from PySide6.QtWidgets import QApplication
            clipboard = QApplication.clipboard()
            clipboard.setText(text)
    
    def init_filters(self):
        self._init_filter_box(self.profile_box, "profile_names", "Профиль")
    
    def _init_filter_box(self, box: QComboBox, field: str, title: str):
        box.blockSignals(True)
        current_value = box.currentText()
        box.clear()
        values = logger.get_unique_values(field)
        box.addItem("ALL")
        for v in values:
            box.addItem(str(v))
        if current_value in [str(v) for v in values] or current_value == "ALL":
            box.setCurrentText(current_value)
        else:
            box.setCurrentText("ALL")
        box.blockSignals(False)
    
    def on_level_filter(self, level: str):
        self.level_filter = level
        self.apply_filter()
    
    def on_profile_filter(self, profile: str):
        self.profile_filter = profile
        self.apply_filter()
    
    def apply_filter(self):
        self.log_text.clear()
        logs = logger.filter_logs(
            level=self.level_filter,
            profile=self.profile_filter
        )
        for log_entry in logs:
            html = logger.make_log_html(log_entry)
            html_with_font = f'<span style="font-size:{self.current_font_size * 1.33}px;">{html}</span>'
            self.log_text.append(html_with_font)
        self.log_text.moveCursor(QTextCursor.End)
    
    def _log_matches_filter(self, log_entry) -> bool:
        if self.level_filter != "ALL":
            if (log_entry.get("level") or "INFO").upper() != self.level_filter.upper():
                return False
        if self.profile_filter != "ALL":
            if self.profile_filter not in log_entry.get("profile_names", ["GLOBAL"]):
                return False
        return True
