# =========================
# 📝 Файл: auto_gui.py
# =========================

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QStackedWidget, QSizePolicy, QPlainTextEdit, QTextEdit,  # ExtraSelection живёт в QTextEdit
    QFileDialog, QMessageBox
)
from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QPainter, QColor, QTextFormat

# «Умная» логика редактора (подсветка, автоотступы, Tab/Shift+Tab и т.п.)
from auto_logic import init_code_editor, save_code_to_file
from logger import logger


# ===== Левое поле с номерами строк =====

class _LineNumberArea(QWidget):
    def __init__(self, editor: 'CodeEditor'):
        super().__init__(editor)
        self._ed = editor

    def sizeHint(self) -> QSize:
        return QSize(self._ed.lineNumberAreaWidth(), 0)

    def paintEvent(self, event):
        self._ed._paintLineNumberArea(event)


class CodeEditor(QPlainTextEdit):
    """
    QPlainTextEdit с нумерацией строк и мягкой подсветкой текущей строки.
    Вся «логика» поведения подключается через auto_logic.init_code_editor().
    """
    # Цвета оставлены в духе существующей тёмной темы
    _BG_GUTTER   = QColor("#1C1F22")
    _FG_GUTTER   = QColor("#8A9099")
    _LINE_SPLIT  = QColor("#2D3136")   # вертикальная линия-разделитель
    _CUR_LINE_BG = QColor("#23282E")   # фон активной строки

    def __init__(self, parent=None):
        super().__init__(parent)

        # Полоса с номерами строк
        self._ln_area = _LineNumberArea(self)

        # Сигналы для пересчёта и перерисовки полосы
        self.blockCountChanged.connect(self._updateLineNumberAreaWidth)
        self.updateRequest.connect(self._updateLineNumberArea)

        # Подсветка текущей строки должна реагировать и на перемещение курсора, и на изменение текста
        self.cursorPositionChanged.connect(self._highlightCurrentLine)
        self.textChanged.connect(self._highlightCurrentLine)

        # Настройки отображения для редактора кода
        self.setLineWrapMode(QPlainTextEdit.NoWrap)

        # Начальная ширина поля с номерами и подсветка строки
        self._updateLineNumberAreaWidth(0)
        self.extra_base_selections = []  # базовые выделения (текущая строка), их дополняет логика из auto_logic
        self._highlightCurrentLine()

    # ---------- Геометрия левой полосы ----------

    def lineNumberAreaWidth(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        char_w = self.fontMetrics().horizontalAdvance('9')
        # поля слева/справа, зазор под рост строк
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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._ln_area.setGeometry(QRect(cr.left(), cr.top(), self.lineNumberAreaWidth(), cr.height()))

    # ---------- Отрисовка полосы и подсветки текущей строки ----------

    def _paintLineNumberArea(self, event) -> None:
        painter = QPainter(self._ln_area)
        painter.fillRect(event.rect(), self._BG_GUTTER)

        # Вертикальная разделительная линия
        x = event.rect().right() - 1
        painter.setPen(self._LINE_SPLIT)
        painter.drawLine(x, event.rect().top(), x, event.rect().bottom())

        # Идём по видимым блокам
        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())

        painter.setPen(self._FG_GUTTER)

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                rect = QRect(0, top, self._ln_area.width() - 4, self.fontMetrics().height())
                painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, number)

            block = block.next()
            block_number += 1
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())

    def _highlightCurrentLine(self) -> None:
        """
        Мягко подсвечиваем текущую строку.
        Важно: когда документ пуст (только один пустой блок), подсветку отключаем,
        чтобы плейсхолдер (placeholderText) был полностью видим.
        """
        # Если редактор read-only, просто возвращаем базовые выделения как есть
        if self.isReadOnly():
            self.setExtraSelections(self.extra_base_selections)
            return

        # Когда редактор пустой (1 блок и пустой текст) — не подсвечиваем, чтобы не перекрывать плейсхолдер
        doc = self.document()
        if doc.blockCount() == 1 and doc.firstBlock().text() == "":
            self.extra_base_selections = []
            self.setExtraSelections([])
            return

        # Обычная подсветка активной строки
        sel = QTextEdit.ExtraSelection()
        fmt = sel.format
        fmt.setBackground(self._CUR_LINE_BG)
        fmt.setProperty(QTextFormat.FullWidthSelection, True)
        sel.format = fmt
        sel.cursor = self.textCursor()
        sel.cursor.clearSelection()

        self.extra_base_selections = [sel]
        # Не затираем маркеры из auto_logic — они добавляются сверху
        self.setExtraSelections(self.extra_base_selections)


class AutoPanel(QWidget):
    """
    Панель режима AUTO: две кнопки (Code / Настройки) и два экрана.
    Функциональность кнопок старт/стоп остаётся отключённой (как и было).
    """
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("auto-panel")

        root_v = QVBoxLayout(self)
        root_v.setContentsMargins(0, 0, 0, 0)
        root_v.setSpacing(8)

        # ===== Верхняя панель режима AUTO: две кнопки =====
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(0, 0, 0, 0)
        tabs_row.setSpacing(6)

        self.code_btn = QPushButton("Code")
        self.code_btn.setCheckable(True)
        self.code_btn.setChecked(True)
        self.code_btn.setProperty("class", "mass-action")
        self.code_btn.setMinimumHeight(26)
        self.code_btn.setMaximumHeight(36)

        self.settings_btn = QPushButton("Настройки")
        self.settings_btn.setCheckable(True)
        self.settings_btn.setProperty("class", "mass-action")
        self.settings_btn.setMinimumHeight(26)
        self.settings_btn.setMaximumHeight(36)

        tabs_row.addWidget(self.code_btn)
        tabs_row.addWidget(self.settings_btn)
        tabs_row.addStretch(1)

        root_v.addLayout(tabs_row)

        # ===== Центральная область с экранами =====
        self.stack = QStackedWidget(self)
        root_v.addWidget(self.stack, 1)

        # --- Экран «Code» ---
        code_page = QWidget(self)
        code_v = QVBoxLayout(code_page)
        code_v.setContentsMargins(0, 6, 0, 0)
        code_v.setSpacing(10)

        # Заголовок экрана Code — по центру (как было)
        title = QLabel("Автоматизация с использованием Selenium 🤖 web3")
        title.setObjectName("auto-code-title")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size: 15px; font-weight: 600; color: #E6E6E6;")
        code_v.addWidget(title)

        # Краткое руководство (шаги, слева)
        guide = QLabel(
            "<div style='color:#C7CCD1; font-size:12px; line-height:1.35em;'>"
            "<ol style='margin:6px 0 10px 18px; padding:0;'>"
            "<li><b>Вставьте</b> ваш Python-код (Selenium/web3) в поле ниже.</li>"
            "<li><b>Подготовьте окружение</b>: драйвер браузера, зависимости, ключи.</li>"
            "<li><b>Продумайте остановку</b>: таймауты, try/except, корректный выход.</li>"
            "<li><b>Сохраните код</b> кнопкой «Сохранить» под редактором.</li>"
            "<li><b>Запуск/стоп</b> добавим позже. Сейчас кнопки запуск/стоп отключены.</li>"
            "</ol>"
            "</div>"
        )
        guide.setWordWrap(True)
        guide.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        code_v.addWidget(guide)

        # Редактор кода — CodeEditor с номерами строк
        self.code_edit = CodeEditor(code_page)
        self.code_edit.setPlaceholderText("Вставьте сюда код вашей автоматизации (Selenium 🤖 web3)...")
        self.code_edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.code_edit.setStyleSheet("""
            QPlainTextEdit {
                background: #1E2124;
                color: #EDEDED;
                border: 1px solid #2D3136;
                border-radius: 10px;
                padding: 10px;
                font-family: Consolas, 'Courier New', monospace;
                font-size: 12px;
                line-height: 1.35em;
            }
        """)
        code_v.addWidget(self.code_edit, 1)

        # Подключаем «умную» логику редактора (подсветка, табы, скобки)
        init_code_editor(self.code_edit)

        # Кнопки по центру: Старт | Сохранить | Стоп
        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addStretch(1)

        self.start_btn = QPushButton("▶️ Старт")
        self.start_btn.setProperty("class", "mass-action")
        self.start_btn.setEnabled(False)
        self.start_btn.setMinimumHeight(26)
        self.start_btn.setMaximumHeight(36)

        self.save_btn = QPushButton("💾 Сохранить")
        self.save_btn.setProperty("class", "mass-action")
        self.save_btn.setEnabled(True)
        self.save_btn.setMinimumHeight(26)
        self.save_btn.setMaximumHeight(36)

        self.stop_btn = QPushButton("⏹ Стоп")
        self.stop_btn.setProperty("class", "mass-action")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setMinimumHeight(26)
        self.stop_btn.setMaximumHeight(36)

        controls.addWidget(self.start_btn)
        controls.addWidget(self.save_btn)
        controls.addWidget(self.stop_btn)
        controls.addStretch(1)
        code_v.addLayout(controls)

        # Дисклеймер внизу (как было)
        disclaimer = QLabel(
            "<span style='color:#9AA0A6; font-size:11px;'>"
            "Это только интерфейс. Исполнение скриптов будет добавлено позже. "
            "Не храните приватные ключи и пароли в открытом виде."
            "</span>"
        )
        disclaimer.setAlignment(Qt.AlignCenter)
        code_v.addWidget(disclaimer)

        self.stack.addWidget(code_page)

        # --- Экран «Настройки» (заглушка, визуал не меняем) ---
        settings_page = QWidget(self)
        settings_v = QVBoxLayout(settings_page)
        settings_v.setContentsMargins(0, 12, 0, 0)

        stub = QLabel("<span style='color:#9AA0A6; font-size:12px;'>"
                      "Тут появятся настройки режима AUTO. Пока заглушка.</span>")
        stub.setAlignment(Qt.AlignCenter)
        settings_v.addStretch(1)
        settings_v.addWidget(stub)
        settings_v.addStretch(1)

        self.stack.addWidget(settings_page)

        # ===== Связи кнопок =====
        self.code_btn.clicked.connect(self._show_code)
        self.settings_btn.clicked.connect(self._show_settings)
        self.save_btn.clicked.connect(self._save_code)

    # --- Переключатели вкладок ---
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

    # --- Сохранение кода (.py / .txt) ---
    def _save_code(self) -> None:
        text = self.code_edit.toPlainText()

        # ВАЖНО: несколько фильтров одной строкой, разделитель — ';;'
        # Документация Qt/PySide6 подтверждает именно такой формат.
        # https://doc.qt.io/qtforpython-6/PySide6/QtWidgets/QFileDialog.html
        fname, selected = QFileDialog.getSaveFileName(
            self,
            "Сохранить скрипт",
            "script.py",
            "Python (*.py);;Текстовый (*.txt);;Все файлы (*.*)"
        )
        if not fname:
            return

        # Определяем желаемое расширение по выбранному фильтру
        selected_l = (selected or "").lower()
        preferred_ext = ".txt" if ".txt" in selected_l else ".py"

        ok, info = save_code_to_file(text, fname, preferred_ext=preferred_ext, allowed_exts=(".py", ".txt"))
        if ok:
            logger.info(f"AUTO: файл сохранён: {info}", profile_names=["GLOBAL"], category="AUTO")
            try:
                QMessageBox.information(self, "Сохранено", f"Файл сохранён:\n{info}")
            except Exception:
                pass
        else:
            logger.warning(f"AUTO: ошибка сохранения: {info}", profile_names=["GLOBAL"], category="AUTO")
            try:
                QMessageBox.warning(self, "Ошибка сохранения", f"Не удалось сохранить файл:\n{info}")
            except Exception:
                pass
