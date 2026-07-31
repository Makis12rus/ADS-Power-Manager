"""
Модуль: moduls/auto/auto_logic.py
Назначение: Диспетчер режима AUTO и логика редактора кода.
Зона ответственности: Подсветка синтаксиса (Pygments), обработка автоотступов,
                      безопасное сохранение пользовательских скриптов и подготовка
                      изолированной песочницы (Auto Sandbox) для их выполнения.
Интеграция: Взаимодействует с GUI через инъекцию QSyntaxHighlighter. Запускает
            пользовательские скрипты в строго изолированных подпроцессах, пробрасывая
            корневой путь проекта через PYTHONPATH для доступа к ядру.
"""

import bisect
import os
import sys
import subprocess
from pathlib import Path
from collections.abc import Iterable
from typing import Callable, Any

from PySide6.QtCore import QObject, QEvent, Qt
from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont, QTextCursor, QTextDocument
)
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit

# Строгие абсолютные импорты (Monorepo Style)
from core.style import Colors
from moduls.ads.ads_logic import get_profiles_and_log, build_group_index
from system.logger import logger, log_action

# --- Внешняя зависимость для подсветки ---
try:
    from pygments import lex
    from pygments.lexers import get_lexer_by_name
    from pygments.styles import get_style_by_name
    from pygments.token import Token
    
    _HAVE_PYGMENTS = True
except ImportError:
    _HAVE_PYGMENTS = False


# ==================== Auto Sandbox Execution ====================

def launch_custom_script_in_sandbox(script_path: str) -> subprocess.Popen | None:
    """
    Запуск пользовательского скрипта в изолированном подпроцессе (Auto Sandbox).
    Гарантирует, что краш скрипта (SyntaxError, бесконечный цикл) не убьет основной GUI-поток.
    Пробрасывает ROOT_DIR в PYTHONPATH для доступности модулей ядра.
    """
    if not os.path.exists(script_path):
        logger.error(
            f"Скрипт не найден: {script_path}. Нечего запускать.",
            profile_names=["GLOBAL"], category="AUTO"
        )
        return None
    
    # Вычисляем корень проекта: moduls/auto/auto_logic.py -> moduls/auto -> moduls -> root
    root_dir = Path(__file__).resolve().parent.parent.parent
    
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{root_dir}{os.pathsep}{existing_pp}" if existing_pp else str(root_dir)
    
    try:
        logger.info(
            f"Запускаем изолированную песочницу для: {os.path.basename(script_path)}. "
            "Надеемся, скрипт не попытается удалить System32...",
            profile_names=["GLOBAL"], category="AUTO"
        )
        
        # Запускаем в неблокирующем режиме, перехватывая вывод.
        # Строго задаем utf-8, чтобы избежать UnicodeDecodeError при чтении логов.
        process = subprocess.Popen(
            [sys.executable, script_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding='utf-8',
            errors='replace'
        )
        return process
    except Exception as e:
        logger.error(f"Ошибка старта песочницы: {e}", profile_names=["GLOBAL"], category="AUTO")
        return None


# ==================== Data Loading Logic ====================

@log_action("Загрузка и группировка профилей (AUTO)", category="AUTO")
def load_and_group_profiles(api_url: str) -> tuple[dict[str, list[dict[str, str]]], list[tuple[str, str]]]:
    """
    Загружает профили через API и группирует их.
    Использует фасад ads_logic для получения данных.
    Возвращает (grouped_profiles, logs).
    """
    profiles, logs = get_profiles_and_log(api_url)
    if not profiles:
        return {}, logs
    
    grouped = build_group_index(profiles)
    return grouped, logs


# ==================== Pygments -> QSyntaxHighlighter ====================

class PygmentsHighlighter(QSyntaxHighlighter):
    """
    Обёртка над Pygments для QSyntaxHighlighter.
    Лексит весь документ при изменении ревизии, кеширует спаны и
    применяет их в highlightBlock по запросу Qt.
    """
    
    def __init__(self, document: QTextDocument, language: str = "python", style_name: str = "monokai"):
        super().__init__(document)
        self.enabled = bool(_HAVE_PYGMENTS)
        if not self.enabled:
            logger.warning(
                "Pygments не найден. Подсветка синтаксиса отключена. (pip install pygments)",
                profile_names=["GLOBAL"], category="AUTO"
            )
            return
        
        self._lexer: Any = None
        self._fmt_map: dict[Any, QTextCharFormat] = {}
        # (start_qt, length_qt, fmt)
        self._spans: list[tuple[int, int, QTextCharFormat]] = []
        # Индексы начала спанов для бинарного поиска
        self._span_starts: list[int] = []
        self._last_revision = -1
        
        # Инициализация лексера
        try:
            self._lexer = get_lexer_by_name(language)
        except Exception:
            self._lexer = get_lexer_by_name("python")
        
        # Инициализация стиля
        try:
            style = get_style_by_name(style_name)
        except Exception:
            style = get_style_by_name("default")
        
        # Пре-генерация форматов (кеширование стилей)
        self._fmt_map = {}
        if style:
            for ttype, spec in style:
                if not spec:
                    continue
                fmt = QTextCharFormat()
                if spec.get('bold'):
                    fmt.setFontWeight(QFont.Weight.Bold)
                if spec.get('italic'):
                    fmt.setFontItalic(True)
                if spec.get('underline'):
                    fmt.setFontUnderline(True)
                if spec.get('color'):
                    fmt.setForeground(QColor(f"#{spec['color']}"))
                if spec.get('bgcolor'):
                    fmt.setBackground(QColor(f"#{spec['bgcolor']}"))
                self._fmt_map[ttype] = fmt
    
    @staticmethod
    def _fmt_has_visible_attrs(fmt: QTextCharFormat) -> bool:
        """Проверка, имеет ли формат видимые атрибуты (цвет, фон, стиль)."""
        if fmt.fontWeight() != QFont.Weight.Normal: return True
        if fmt.fontItalic(): return True
        if fmt.fontUnderline(): return True
        
        fg = fmt.foreground()
        if fg.style() != Qt.BrushStyle.NoBrush and fg.color().isValid(): return True
        
        bg = fmt.background()
        if bg.style() != Qt.BrushStyle.NoBrush and bg.color().isValid(): return True
        
        return False
    
    @staticmethod
    def _build_u16_prefix(s: str) -> list[int]:
        """
        Префикс-суммы для маппинга индексов Python (символы) -> Qt (UTF-16 code units).
        Эмодзи и суррогатные пары занимают 2 юнита в Qt.
        """
        pref = [0] * (len(s) + 1)
        acc = 0
        for i, ch in enumerate(s):
            acc += 2 if ord(ch) > 0xFFFF else 1
            pref[i + 1] = acc
        return pref
    
    def _relex_if_needed(self) -> None:
        """Полный пересчет токенов, если документ изменился."""
        if not self.enabled or self._lexer is None:
            return
        
        doc = self.document()
        if doc.revision() == self._last_revision:
            return
        
        raw_text = doc.toPlainText()
        # Нормализация: Qt может использовать U+2029 (Paragraph Separator), Pygments хочет \n
        text_for_lex = raw_text.replace("\u2029", "\n")
        
        # Карта смещений для корректного позиционирования (UTF-16)
        u16pref = self._build_u16_prefix(text_for_lex)
        
        self._spans.clear()
        self._span_starts.clear()
        
        pos_py = 0
        try:
            for ttype, value in lex(text_for_lex, self._lexer):
                if not value:
                    continue
                length_py = len(value)
                
                # Pygments токены могут не иметь стиля, пропускаем их
                fmt = self._format_for(ttype)
                if self._fmt_has_visible_attrs(fmt):
                    start_qt = u16pref[pos_py]
                    end_qt = u16pref[pos_py + length_py]
                    length_qt = end_qt - start_qt
                    
                    if length_qt > 0:
                        self._spans.append((start_qt, length_qt, fmt))
                        self._span_starts.append(start_qt)
                
                pos_py += length_py
        except Exception as e:
            logger.warning(f"Pygments lex error: {e}", profile_names=["GLOBAL"], category="AUTO")
        
        self._last_revision = doc.revision()
    
    def _format_for(self, ttype: Any) -> QTextCharFormat:
        """Получение формата для токена с учетом наследования типов."""
        if ttype in self._fmt_map:
            return self._fmt_map[ttype]
        
        # Ищем родительский стиль
        parent = getattr(ttype, "parent", None)
        while parent is not None:
            if parent in self._fmt_map:
                return self._fmt_map[parent]
            parent = getattr(parent, "parent", None)
        
        return self._fmt_map.get(Token, QTextCharFormat())
    
    def highlightBlock(self, text: str) -> None:  # noqa: N802
        if not self.enabled or not self._spans:
            self._relex_if_needed()
            if not self._spans:
                return
        
        # Если ревизия не совпадает, пробуем обновить (в редких случаях race condition)
        if self.document().revision() != self._last_revision:
            self._relex_if_needed()
        
        block = self.currentBlock()
        block_start = block.position()
        block_end = block_start + block.length()
        
        # Бинарный поиск первого потенциального спана
        idx = bisect.bisect_left(self._span_starts, block_start)
        if idx > 0:
            idx -= 1  # Проверяем спан, который мог начаться раньше блока
        
        count = len(self._spans)
        while idx < count:
            s_qt, len_qt, fmt = self._spans[idx]
            
            # Если спан начинается после блока, прерываем (они отсортированы)
            if s_qt >= block_end:
                break
            
            e_qt = s_qt + len_qt
            # Пересечение интервалов
            if e_qt > block_start and s_qt < block_end:
                # Вычисляем относительные координаты внутри блока
                rel_start = max(s_qt, block_start) - block_start
                rel_end = min(e_qt, block_end) - block_start
                self.setFormat(rel_start, rel_end - rel_start, fmt)
            
            idx += 1


# ==================== Обработчик клавиш (Indentation) ====================

class _EditorKeyHandler(QObject):
    """Обработка Tab, Shift+Tab и Enter для автоотступов."""
    INDENT = 4
    
    def __init__(self, editor: QPlainTextEdit) -> None:
        super().__init__(editor)
        self.editor = editor
        editor.installEventFilter(self)
    
    def eventFilter(self, obj: QObject, ev: QEvent) -> bool:  # noqa: N802
        if ev.type() == QEvent.Type.KeyPress:
            key = ev.key()
            mods = ev.modifiers()
            
            # Tab / Shift+Tab
            if key == Qt.Key.Key_Tab:
                if not (mods & Qt.KeyboardModifier.ShiftModifier):
                    return self._handle_tab()
                else:
                    return self._handle_shift_tab()
            if key == Qt.Key.Key_Backtab:  # Shift+Tab sometimes fires this
                return self._handle_shift_tab()
            
            # Enter (исключая Ctrl/Alt)
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (mods & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
                return self._handle_return()
        
        return super().eventFilter(obj, ev)
    
    def _handle_tab(self) -> bool:
        tc = self.editor.textCursor()
        if tc.hasSelection():
            # Блочный отступ
            self._modify_block_indent(tc, increase=True)
        else:
            # Вставка пробелов
            tc.insertText(" " * self.INDENT)
        return True
    
    def _handle_shift_tab(self) -> bool:
        tc = self.editor.textCursor()
        if tc.hasSelection():
            # Блочное удаление отступа
            self._modify_block_indent(tc, increase=False)
        else:
            # Удаление отступа на текущей строке
            self._unindent_line(tc)
        return True
    
    def _modify_block_indent(self, cursor: QTextCursor, increase: bool) -> None:
        """Изменяет отступ для выделенных строк."""
        start = min(cursor.selectionStart(), cursor.selectionEnd())
        end = max(cursor.selectionStart(), cursor.selectionEnd())
        
        cursor.beginEditBlock()
        cursor.setPosition(start)
        
        # Перебираем блоки пока не достигнем конца выделения
        while True:
            cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
            if increase:
                cursor.insertText(" " * self.INDENT)
            else:
                # Удаляем до 4 пробелов
                cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor, self.INDENT)
                sel = cursor.selectedText()
                if sel.strip() == "":  # только пробелы
                    cursor.removeSelectedText()
                elif sel.startswith(" "):
                    # Частичное удаление (если меньше 4 пробелов)
                    ws_len = len(sel) - len(sel.lstrip(" "))
                    cursor.setPosition(cursor.position() - self.INDENT)  # reset
                    cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor, ws_len)
                    cursor.removeSelectedText()
                else:
                    # Нет пробелов в начале, сбрасываем выделение
                    cursor.setPosition(cursor.position() - self.INDENT)
            
            if cursor.position() >= end + (self.INDENT if increase else 0):  # корректировка end
                break
            
            if not cursor.movePosition(QTextCursor.MoveOperation.NextBlock):
                break
            
            # Обновляем end, так как текст сместился
            if increase:
                end += self.INDENT
        
        cursor.endEditBlock()
    
    def _unindent_line(self, cursor: QTextCursor) -> None:
        """Удаляет отступ (shift+tab) без выделения."""
        cursor.beginEditBlock()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        # Смотрим первые N символов
        cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor, self.INDENT)
        txt = cursor.selectedText()
        
        to_remove = 0
        for char in txt:
            if char == " ":
                to_remove += 1
            else:
                break
        
        cursor.clearSelection()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfBlock)
        if to_remove > 0:
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor, to_remove)
            cursor.removeSelectedText()
        
        cursor.endEditBlock()
    
    def _handle_return(self) -> bool:
        """Автоотступ при нажатии Enter (копирует отступ предыдущей строки)."""
        tc = self.editor.textCursor()
        tc.beginEditBlock()
        
        text = tc.block().text()
        indent = ""
        for char in text:
            if char in (" ", "\t"):
                indent += char
            else:
                break
        
        tc.insertText("\n" + indent)
        tc.endEditBlock()
        return True


# ==================== Public API ====================

def init_code_editor(editor: QPlainTextEdit) -> None:
    """Подключение хайлайтера и обработчика клавиш к редактору."""
    # Настройка шрифта
    font = editor.font()
    if font.family().lower().startswith(('segoe ui', 'ms shell')):
        font.setFamily('Consolas')
    font.setFixedPitch(True)
    font.setPointSize(max(10, font.pointSize()))
    editor.setFont(font)
    
    # Подключение логики
    PygmentsHighlighter(editor.document(), language="python", style_name="monokai")
    _EditorKeyHandler(editor)


def mark_error_lines(editor: QPlainTextEdit, lines: Iterable[int]) -> None:
    """Подсветка строк с ошибками."""
    selections = getattr(editor, 'extra_base_selections', []).copy()
    
    fmt = QTextCharFormat()
    # Используем цвет из централизованной палитры
    fmt.setBackground(QColor(Colors.ED_ERROR_LINE))
    
    doc = editor.document()
    for ln in lines:
        if ln < 1: continue
        block = doc.findBlockByNumber(ln - 1)
        if not block.isValid(): continue
        
        sel = QTextEdit.ExtraSelection()
        sel.format = fmt
        sel.cursor = QTextCursor(doc)
        sel.cursor.setPosition(block.position())
        sel.cursor.movePosition(QTextCursor.MoveOperation.EndOfBlock, QTextCursor.MoveMode.KeepAnchor)
        selections.append(sel)
    
    editor.setExtraSelections(selections)


# ==== File I/O ====

def _normalize_extension(path: str, preferred: str, allowed: tuple[str, ...]) -> tuple[bool, str]:
    """Валидация расширения файла."""
    preferred = preferred.lower()
    allowed_lower = tuple(e.lower() for e in allowed)
    
    base = os.path.basename(path)
    name, ext = os.path.splitext(base)
    ext = ext.lower()
    
    if not name:
        return False, "Имя файла пустое"
    
    if not ext:
        return True, path + preferred
    
    if ext in allowed_lower:
        return True, path
    
    return False, f"Поддерживаются только файлы: {', '.join(allowed)}"


@log_action("Сохранение скрипта в файл", category="AUTO")
def save_code_to_file(
        text: str,
        path: str,
        preferred_ext: str = ".py",
        allowed_exts: tuple[str, ...] = (".py", ".txt"),
) -> tuple[bool, str]:
    """
    Безопасное сохранение кода в файл.
    Гарантирует использование кодировки UTF-8 и правильных переносов строк.
    """
    try:
        if not path or not path.strip():
            return False, "Путь не задан"
        
        path = path.strip()
        final_dir = os.path.dirname(os.path.abspath(path))
        
        if final_dir and not os.path.exists(final_dir):
            return False, f"Каталог не найден: {final_dir}"
        
        ok, final_path = _normalize_extension(path, preferred_ext, allowed_exts)
        if not ok:
            return False, final_path
        
        content = text
        # Гарантируем терминальный перевод строки (POSIX standard)
        if content and not content.endswith('\n'):
            content += '\n'
        
        # Resource Guard: Оборачиваем I/O операцию в блок with для гарантии закрытия дескриптора
        with logger.block(f"Запись файла {os.path.basename(final_path)}", category="AUTO"):
            with open(final_path, 'w', encoding='utf-8', newline='\n') as f:
                f.write(content)
        
        return True, final_path
    
    except Exception as e:
        # Декоратор @log_action залогирует ошибку, но нам нужно вернуть False для GUI
        return False, str(e)