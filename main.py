# =========================
# 📝 Файл: main.py
# =========================

import sys
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import Qt
from main_window_gui import MainWindow
from core import start_watchdog, stop_watchdog

def _apply_dark_boot_palette(app: QApplication) -> None:
    """
    Мини‑палитра и базовый стиль до создания главного окна.
    Нужна, чтобы на старте не было белых вспышек до применения стилей.
    """
    pal = QPalette()

    # Базовые тёмные цвета
    base_bg = QColor("#232629")
    base_fg = QColor("#F0F0F0")
    hint_fg = QColor("#BBBBBB")
    hi_bg = QColor("#FFE066")
    hi_fg = QColor("#232629")

    pal.setColor(QPalette.Window, base_bg)
    pal.setColor(QPalette.WindowText, base_fg)
    pal.setColor(QPalette.Base, QColor("#1E2124"))
    pal.setColor(QPalette.AlternateBase, base_bg)
    pal.setColor(QPalette.ToolTipBase, base_bg)
    pal.setColor(QPalette.ToolTipText, base_fg)
    pal.setColor(QPalette.Text, base_fg)
    pal.setColor(QPalette.Button, base_bg)
    pal.setColor(QPalette.ButtonText, base_fg)
    pal.setColor(QPalette.BrightText, base_fg)
    pal.setColor(QPalette.Highlight, hi_bg)
    pal.setColor(QPalette.HighlightedText, hi_fg)

    app.setPalette(pal)

    # Бэкап‑стиль, чтобы ВСЕ ранние виджеты были тёмными ещё до CSS окна
    app.setStyleSheet("""
        QWidget { background-color: #232629; color: #F0F0F0; }
        QToolTip { background-color: #1E2124; color: #F0F0F0; border: 1px solid #2D3136; }
    """)

def main():
    app = QApplication(sys.argv)

    # Тёмная палитра/стиль на самый ранний старт — убирает белые «мигания»
    _apply_dark_boot_palette(app)

    # Запуск watchdog для контроля "живости" приложения
    start_watchdog(interval=30)

    # Главное окно (само покажется безопасно изнутри после полной подготовки)
    main_window = MainWindow()
    # ВНИМАНИЕ: show() остаётся, но окно изначально помечено как "не показывать на экран".
    # Когда внутри MainWindow всё будет восстановлено и выровнено, оно финально проявится без прыжка.
    main_window.show()

    exit_code = app.exec()

    # Корректная остановка watchdog
    stop_watchdog()
    sys.exit(exit_code)

if __name__ == "__main__":
    main()
