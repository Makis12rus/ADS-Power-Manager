# =========================
# 📝 Файл: auto_logic.py
# =========================
# coding: utf-8

"""
Логика режима AUTO и «умные» функции редактора кода.
Подсветка синтаксиса выполняется через Pygments, обёрнутый в QSyntaxHighlighter.

Совместимо с PySide6. Если Pygments не установлен, подсветка просто отключится
(редактор работает, лог предупредит).
"""

from __future__ import annotations

from typing import Callable, Optional, Iterable, List, Tuple
import bisect
import os

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont, QTextCursor
)
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit  # ExtraSelection живёт в QTextEdit

# --- Внешняя зависимость для подсветки ---
try:
    from pygments import lex
    from pygments.lexers import get_lexer_by_name
    from pygments.styles import get_style_by_name
    from pygments.token import Token
    _HAVE_PYGMENTS = True
except Exception:  # pragma: no cover
    _HAVE_PYGMENTS = False

try:
    from logger import logger  # централизованный логгер проекта
except Exception:  # pragma: no cover
    class _Dummy:
        def __getattr__(self, _):
            return lambda *a, **k: None
    logger = _Dummy()  # type: ignore


# ==================== Pygments -> QSyntaxHighlighter ====================

class PygmentsHighlighter(QSyntaxHighlighter):
    """
    Обёртка над Pygments, совместимая с QSyntaxHighlighter.
    - Лексер берём по имени языка (по умолчанию 'python').
    - Стиль берём по имени Pygments (по умолчанию 'monokai').
    - Чтобы не лексить по 100 раз, кешируем результат на текущую ревизию QTextDocument.
    """
    def __init__(self, document, language: str = "python", style_name: str = "monokai"):
        super().__init__(document)
        self.enabled = bool(_HAVE_PYGMENTS)
        self.language = language
        self.style_name = style_name

        self._lexer = None
        self._fmt_map: dict = {}
        # ВНИМАНИЕ: сохраняем интервалы в ЕДИНИЦАХ QChar (UTF-16), чтобы совпадать с Qt
        self._spans: List[Tuple[int, int, QTextCharFormat]] = []  # (start_qt, length_qt, fmt)
        self._span_starts: List[int] = []  # для bisect по QChar-индексам
        self._last_revision = -1

        if not self.enabled:
            logger.warning(
                "Подсветка Pygments отключена: пакет 'pygments' не найден. "
                "Установите 'pip install pygments'.",
                profile_names=["GLOBAL"], category="AUTO"
            )
            return

        # Инициализация лексера и стиля
        try:
            self._lexer = get_lexer_by_name(self.language)
        except Exception:
            logger.warning(
                f"Pygments: неизвестный язык '{self.language}', используем 'python'.",
                profile_names=["GLOBAL"], category="AUTO"
            )
            self._lexer = get_lexer_by_name("python")

        try:
            style = get_style_by_name(self.style_name)
        except Exception:
            logger.warning(
                f"Pygments: неизвестный стиль '{self.style_name}', используем 'default'.",
                profile_names=["GLOBAL"], category="AUTO"
            )
            style = get_style_by_name("default")

        # Составляем карту токен -> QTextCharFormat
        # Pygments хранит стили как строки: 'bold italic underline #RRGGBB bg:#RRGGBB'
        self._fmt_map = {}
        for ttype, spec in getattr(style, "styles", {}).items():
            if not spec:
                continue
            fmt = QTextCharFormat()
            for part in spec.split():
                p = part.lower()
                try:
                    if p == "bold":
                        fmt.setFontWeight(QFont.Weight.Bold)
                    elif p == "nobold":
                        fmt.setFontWeight(QFont.Weight.Normal)
                    elif p == "italic":
                        fmt.setFontItalic(True)
                    elif p == "noitalic":
                        fmt.setFontItalic(False)
                    elif p == "underline":
                        fmt.setFontUnderline(True)
                    elif p == "nounderline":
                        fmt.setFontUnderline(False)
                    elif p.startswith("bg:") and len(p) >= 4 and p[3] == "#":
                        fmt.setBackground(QColor(p[3:]))
                    elif p.startswith("#"):
                        fmt.setForeground(QColor(p))
                except Exception:
                    # Не даём странному стилю убить хайлайтер
                    continue
            self._fmt_map[ttype] = fmt

    # --- утилиты ---

    @staticmethod
    def _fmt_has_visible_attrs(fmt: QTextCharFormat) -> bool:
        """
        True, если формат несёт видимые атрибуты (цвет/фон/стиль шрифта).
        У QBrush нет isValid(); проверяем style и QColor.isValid().
        """
        fg_brush = fmt.foreground()
        bg_brush = fmt.background()
        has_fg = getattr(fg_brush, "style", lambda: Qt.NoBrush)() != Qt.NoBrush or fg_brush.color().isValid()
        has_bg = getattr(bg_brush, "style", lambda: Qt.NoBrush)() != Qt.NoBrush or bg_brush.color().isValid()
        return (
            has_fg or has_bg or
            fmt.fontItalic() or fmt.fontUnderline() or
            fmt.fontWeight() != QFont.Weight.Normal
        )

    @staticmethod
    def _build_u16_prefix(s: str) -> List[int]:
        """
        Возвращает префикс-суммы по количеству QChar (UTF-16 code units) на каждом
        префиксе строки s. Эмодзи и прочие > U+FFFF занимают два юнита.
        prefix[i] = кол-во QChar в s[:i]
        """
        pref = [0] * (len(s) + 1)
        acc = 0
        for i, ch in enumerate(s):
            # 2 юнита для не-BMP (эмодзи), иначе 1
            acc += 2 if ord(ch) > 0xFFFF else 1
            pref[i + 1] = acc
        return pref

    def _relex_if_needed(self):
        """
        Лексит документ целиком один раз на ревизию и кеширует промежутки.

        ВАЖНО:
        - QTextDocument/Qt считает позиции в QChar (UTF-16), а не в «символах Python».
        - Pygments лексит Python-строку и отдаёт позиции в обычных символах.
        Решение: нормализуем переводы строк и строим карту индексов Python→QChar.
        """
        if not self.enabled or self._lexer is None:
            return
        doc = self.document()
        if doc.revision() == self._last_revision:
            return

        # Нормализуем перевод строки для лексера: U+2029 → '\n' (оба по одному QChar)
        raw_text = doc.toPlainText()
        text_for_lex = raw_text.replace("\u2029", "\n")

        # Префикс-суммы количества QChar (UTF-16) для точного маппинга позиций
        u16pref = self._build_u16_prefix(text_for_lex)

        self._spans.clear()
        self._span_starts.clear()

        pos_py = 0  # позиция в символах Python
        try:
            for ttype, value in lex(text_for_lex, self._lexer):
                if not value:
                    continue
                length_py = len(value)

                # Пересчёт в позиции Qt (QChar / UTF-16 code units)
                start_qt = u16pref[pos_py]
                end_qt = u16pref[pos_py + length_py]
                length_qt = end_qt - start_qt

                fmt = self._format_for(ttype)

                if length_qt > 0 and self._fmt_has_visible_attrs(fmt):
                    self._spans.append((start_qt, length_qt, fmt))
                    self._span_starts.append(start_qt)

                pos_py += length_py
        except Exception as e:
            logger.warning(f"Pygments lex() error: {e}", profile_names=["GLOBAL"], category="AUTO")

        self._last_revision = doc.revision()

    def _format_for(self, ttype) -> QTextCharFormat:
        """Возвращает QTextCharFormat для данного Pygments-токена с учётом иерархии."""
        if ttype in self._fmt_map:
            return self._fmt_map[ttype]
        parent = getattr(ttype, "parent", None)
        while parent is not None:
            if parent in self._fmt_map:
                return self._fmt_map[parent]
            parent = getattr(parent, "parent", None)
        return self._fmt_map.get(Token, QTextCharFormat())

    # --- основной цикл подсветки одного блока ---

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if not self.enabled or self._lexer is None:
            return

        self._relex_if_needed()

        block = self.currentBlock()
        block_start_qt = block.position()                     # позиции в QChar
        block_end_qt = block_start_qt + block.length()        # длина тоже в QChar (включая перенос)

        # Быстрый поиск интересующих промежутков
        i = bisect.bisect_left(self._span_starts, block_start_qt)
        if i > 0:
            i -= 1  # если span начался до блока, но пересекает его

        while i < len(self._spans):
            s_qt, length_qt, fmt = self._spans[i]
            if s_qt >= block_end_qt:
                break
            e_qt = s_qt + length_qt
            if e_qt > block_start_qt and s_qt < block_end_qt:
                rel_start_qt = max(s_qt, block_start_qt) - block_start_qt
                rel_len_qt = min(e_qt, block_end_qt) - max(s_qt, block_start_qt)
                if rel_len_qt > 0:
                    # setFormat ожидает смещения в терминах QChar — их мы и даём
                    self.setFormat(rel_start_qt, rel_len_qt, fmt)
            i += 1


# ==================== Обработчик клавиш ====================

class _EditorKeyHandler(QObject):
    """Привычное поведение Tab/Shift+Tab и Enter с автоотступом."""
    INDENT = 4

    def __init__(self, editor: QPlainTextEdit) -> None:
        super().__init__(editor)
        self.editor = editor
        editor.installEventFilter(self)

    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.KeyPress:
            key = ev.key()
            mods = ev.modifiers()

            # Tab / Shift+Tab — блоковые отступы
            if key == Qt.Key_Tab and not (mods & Qt.ShiftModifier):
                return self._handle_tab()
            if key in (Qt.Key_Backtab, Qt.Key_Tab) and (mods & Qt.ShiftModifier):
                return self._handle_shift_tab()

            # Enter/Return: учитываем NumPad и Shift; игнорируем только Ctrl/Alt
            if key in (Qt.Key_Return, Qt.Key_Enter) and not (mods & (Qt.ControlModifier | Qt.AltModifier)):
                return self._handle_return()

        return super().eventFilter(obj, ev)

    # ---- Tab: отступ или сдвиг выделенного блока ----
    def _handle_tab(self) -> bool:
        ed = self.editor
        tc = ed.textCursor()
        if tc.hasSelection():
            start = min(tc.selectionStart(), tc.selectionEnd())
            end = max(tc.selectionStart(), tc.selectionEnd())
            tc.beginEditBlock()
            tc.setPosition(start)
            while True:
                tc.movePosition(QTextCursor.StartOfBlock)
                tc.insertText(' ' * self.INDENT)
                if tc.position() >= end:
                    break
                if not tc.movePosition(QTextCursor.NextBlock):
                    break
                end += self.INDENT
            tc.endEditBlock()
            return True
        else:
            ed.insertPlainText(' ' * self.INDENT)
            return True

    def _handle_shift_tab(self) -> bool:
        ed = self.editor
        tc = ed.textCursor()
        if tc.hasSelection():
            start = min(tc.selectionStart(), tc.selectionEnd())
            end = max(tc.selectionStart(), tc.selectionEnd())
            tc.beginEditBlock()
            tc.setPosition(start)
            while True:
                tc.movePosition(QTextCursor.StartOfBlock)
                tc.movePosition(QTextCursor.NextCharacter, QTextCursor.KeepAnchor, self.INDENT)
                if tc.selectedText().startswith(' '):
                    tc.removeSelectedText()
                if tc.position() >= end:
                    break
                if not tc.movePosition(QTextCursor.NextBlock):
                    break
                end -= self.INDENT
            tc.endEditBlock()
            return True
        else:
            # Удаляем до 4 пробелов слева у текущей строки
            tc.beginEditBlock()
            tc.movePosition(QTextCursor.StartOfBlock, QTextCursor.KeepAnchor)
            prefix = tc.selectedText()
            n = min(self.INDENT, len(prefix) - len(prefix.rstrip(' ')))
            tc.clearSelection()
            if n:
                for _ in range(n):
                    ed.textCursor().deletePreviousChar()
            tc.endEditBlock()
            return True

    def _handle_return(self) -> bool:
        """
        Вставляет перевод строки и копирует ведущие пробелы/табы ИЗ ВСЕЙ СТРОКИ,
        а не только до курсора.
        """
        ed = self.editor
        tc = ed.textCursor()
        tc.beginEditBlock()

        block_text = tc.block().text()
        # Вычисляем ведущий префикс из пробелов/табов
        i = 0
        while i < len(block_text) and block_text[i] in (' ', '\t'):
            i += 1
        indent_str = block_text[:i]

        ed.insertPlainText('\n' + indent_str)
        tc.endEditBlock()
        return True


# ==================== Публичное API для GUI ====================

def init_code_editor(editor: QPlainTextEdit) -> None:
    """
    Подключает к переданному редактору «умные» возможности:
      - подсветка синтаксиса через Pygments (по умолчанию: язык python, стиль monokai)
      - обработчик клавиш (автоотступы, Tab/Shift+Tab)
    """
    # Моноширинный шрифт
    f = editor.font()
    if f.family().lower().startswith('segoe ui'):
        f.setFamily('Consolas')
    f.setFixedPitch(True)
    f.setPointSize(max(10, f.pointSize()))
    editor.setFont(f)

    # Подсветка (не упадёт, если pygments нет)
    PygmentsHighlighter(editor.document(), language="python", style_name="monokai")

    # Клавиатурные привычки
    _EditorKeyHandler(editor)


def mark_error_lines(editor: QPlainTextEdit, lines: Iterable[int]) -> None:
    """
    Подсветить строки как «ошибочные» мягким фоном (нумерация с 1).
    """
    base = getattr(editor, 'extra_base_selections', [])
    selections = list(base)

    err_bg = QColor('#3d1e1e')
    err_fmt = QTextCharFormat()
    err_fmt.setBackground(err_bg)

    for ln in lines:
        if ln <= 0:
            continue
        block = editor.document().findBlockByNumber(ln - 1)
        if not block.isValid():
            continue
        sel = QTextEdit.ExtraSelection()
        sel.format = err_fmt
        sel.cursor = QTextCursor(editor.document())
        sel.cursor.setPosition(block.position())
        sel.cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        selections.append(sel)

    editor.setExtraSelections(selections)


# ==== Заглушки режима AUTO (на будущее) ====

ProgressCB = Optional[Callable[[str], None]]

def start_preview(progress_cb: ProgressCB = None) -> bool:
    logger.start('Auto: предпросмотр не доступен (заглушка).',
                 profile_names=['GLOBAL'], category='AUTO')
    try:
        if progress_cb:
            progress_cb('Auto: функциональность временно отключена.')
    except Exception:
        pass
    logger.warning('Auto: функциональность в разработке.',
                   profile_names=['GLOBAL'], category='AUTO')
    return False

def stop_any() -> None:
    logger.info('Auto: stop_any()', profile_names=['GLOBAL'], category='AUTO')


# ==== Сохранение кода в .py / .txt ====

def _normalize_extension(path: str, preferred_ext: str, allowed_exts: Tuple[str, ...]) -> Tuple[bool, str]:
    """
    Возвращает (ok, final_path):
      - если расширение отсутствует — дописывает preferred_ext;
      - если расширение из allowed_exts — оставляет как есть;
      - иначе — ошибка.
    """
    preferred_ext = preferred_ext.lower()
    allowed_exts = tuple(e.lower() for e in allowed_exts)

    base = os.path.basename(path)
    name, ext = os.path.splitext(base)
    ext = ext.lower()

    if not name:
        return False, "Имя файла пустое"

    if ext == "":
        return True, path + preferred_ext

    if ext in allowed_exts:
        return True, path

    return False, f"Поддерживаются только файлы: {', '.join(allowed_exts)}"

def save_code_to_file(
    text: str,
    path: str,
    preferred_ext: str = ".py",
    allowed_exts: Tuple[str, ...] = (".py", ".txt"),
) -> Tuple[bool, str]:
    """
    Сохраняет содержимое редактора в файл (UTF-8, без BOM).
    Поддерживаемые расширения: .py, .txt
    Возвращает (ok, info), где info — конечный путь при успехе либо сообщение об ошибке.
    """
    try:
        if not isinstance(path, str) or not path.strip():
            return False, "Путь к файлу не задан"

        # Каталог должен существовать
        final_dir = os.path.dirname(path.strip()) or "."
        if not os.path.isdir(final_dir):
            return False, f"Каталог не существует: {final_dir}"

        ok, final_path = _normalize_extension(path.strip(), preferred_ext, allowed_exts)
        if not ok:
            return False, final_path  # здесь final_path = сообщение об ошибке

        # Обеспечиваем завершающий перевод строки
        content = text if text.endswith('\n') or text == '' else text + '\n'

        with open(final_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(content)

        logger.info(f"AUTO: код сохранён в файл: {final_path}",
                    profile_names=['GLOBAL'], category='AUTO')
        return True, final_path

    except Exception as e:
        logger.warning(f"AUTO: ошибка сохранения файла: {e}",
                       profile_names=['GLOBAL'], category='AUTO')
        return False, str(e)
