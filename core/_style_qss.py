"""
Модуль: core/_style_qss.py
Назначение: Изолированный цех QSS-стилей и тем оформления (Presentation Utilities).
Зона ответственности: Хранение каскадных таблиц стилей (CSS/QSS) для виджетов PySide6
                      и HTML-тем для панели логов. Адаптирован под Dark Glassmorphism,
                      Screen-Space Coordinate Projection (SSCP) и новую архитектуру
                      Виртуальной карусели (Recycler View) с эффектом матового стекла (Frosted Glass).
                      Включает глобальную инъекцию премиальных скроллбаров с поддержкой
                      Ghost Mode (прозрачность при неактивности) и Gutter Isolation.
Интеграция: Является автономным модулем. Зависит только от `_style_colors.py`.
            Не импортирует PySide6, что гарантирует нулевое потребление ОЗУ на
            графические движки при импорте в фоновых воркерах.
            Реэкспортируется через фасад core/style.py.
"""

from core._style_colors import Colors


class Styles:
    """Хранилище всех CSS/QSS-стилей виджетов приложения."""
    
    # --- Общие стили приложения (Glassmorphism Base & SSCP Support) ---
    BOOT_STYLESHEET: str = f"""
        QWidget {{ background-color: transparent; color: {Colors.TXT_PRIMARY}; }}
        QToolTip {{ background-color: {Colors.BG_DARK}; color: {Colors.TXT_PRIMARY}; border: 1px solid #2D3136; }}
    """
    
    MAIN_WINDOW: str = f"""
        QWidget {{
            background-color: transparent;
            color: #F0F0F0;
            font-family: 'Segoe UI', Arial, sans-serif;
            font-size: 13px;
        }}
        QMainWindow, QDockWidget {{
            background-color: transparent;
            border: none;
        }}
        QDockWidget::title {{
            background: transparent;
        }}
        QLabel {{
            color: #F0F0F0;
            background: transparent;
        }}
        
        /* --- Единый премиальный стандарт скроллбаров (Gutter Isolation & Ghost Mode) --- */
        QScrollBar:vertical {{
            border: none;
            background: transparent;
            width: 8px;
            margin: 0px;
        }}
        QScrollBar::handle:vertical {{
            background: rgba(90, 90, 90, 0.5);
            min-height: 30px;
            border-radius: 4px;
        }}
        QScrollBar::handle:vertical:hover, QScrollBar::handle:vertical:pressed {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {Colors.ACCENT}, stop:1 {Colors.ACCENT_HOVER});
        }}
        QScrollBar::handle:vertical:disabled {{
            background: transparent;
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            border: none;
            background: transparent;
            height: 0px;
            width: 0px;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: transparent;
        }}

        QScrollBar:horizontal {{
            border: none;
            background: transparent;
            height: 8px;
            margin: 0px;
        }}
        QScrollBar::handle:horizontal {{
            background: rgba(90, 90, 90, 0.5);
            min-width: 30px;
            border-radius: 4px;
        }}
        QScrollBar::handle:horizontal:hover, QScrollBar::handle:horizontal:pressed {{
            background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {Colors.ACCENT}, stop:1 {Colors.ACCENT_HOVER});
        }}
        QScrollBar::handle:horizontal:disabled {{
            background: transparent;
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            border: none;
            background: transparent;
            height: 0px;
            width: 0px;
        }}
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
            background: transparent;
        }}
        
        QAbstractScrollArea::corner {{
            background: transparent;
            border: none;
        }}
    """

    BTN_ACTION: str = """
        QPushButton[class="mass-action"] {
            background-color: rgba(35, 38, 41, 0.6);
            color: #CCCCCC;
            border: 1px solid rgba(90, 90, 90, 0.6);
            border-radius: 4px;
            padding: 4px 16px;
            font-size: 14px;
            font-weight: 500;
            min-width: 110px;
            min-height: 28px;
            max-height: 36px;
        }
        QPushButton[class="mass-action"]:hover:!disabled {
            border: 1px solid #FFE066;
            color: #FFE066;
            background-color: rgba(255, 224, 102, 0.1);
        }
        QPushButton[class="mass-action"]:pressed {
            background-color: rgba(255, 224, 102, 0.2);
            color: #FFD700;
        }
        QPushButton[class="mass-action"]:disabled {
            border: 1px solid rgba(53, 57, 60, 0.5);
            color: #555555;
            background-color: transparent;
        }
    """
    
    BTN_HOT_RUN: str = """
        QPushButton[class="btn-hot-run"] {
            background-color: rgba(255, 224, 102, 0.08);
            color: #FFD700;
            border: 1px solid rgba(255, 215, 0, 0.6);
            border-radius: 6px;
            padding: 8px 16px;
            font-size: 14px;
            font-weight: bold;
            min-height: 36px;
        }
        QPushButton[class="btn-hot-run"]:hover:!disabled {
            background-color: rgba(255, 224, 102, 0.15);
            border: 1px solid #FFE066;
            color: #FFE066;
        }
        QPushButton[class="btn-hot-run"]:pressed {
            background-color: rgba(255, 224, 102, 0.25);
        }
        QPushButton[class="btn-hot-run"]:disabled {
            background-color: transparent;
            border: 1px solid rgba(53, 57, 60, 0.5);
            color: #555555;
        }
    """
    
    BTN_STOP: str = """
        QPushButton[class="stop-action"] {
            background-color: rgba(217, 83, 79, 0.05);
            color: #D9534F;
            border: 1px solid rgba(217, 83, 79, 0.6);
            border-radius: 4px;
            padding: 2px 10px;
            font-size: 11px;
            font-weight: bold;
            min-height: 24px;
            max-height: 24px;
            min-width: 80px;
            max-width: 80px;
        }
        QPushButton[class="stop-action"]:hover:!disabled {
            background-color: rgba(217, 83, 79, 0.15);
            color: #FF4F4F;
            border-color: #FF4F4F;
        }
        QPushButton[class="stop-action"]:pressed {
            background-color: rgba(217, 83, 79, 0.3);
        }
        QPushButton[class="stop-action"]:disabled {
            border-color: rgba(53, 57, 60, 0.5);
            color: #555555;
            background-color: transparent;
        }
    """
    
    BTN_LOG_MINI: str = """
        QPushButton {
            background-color: rgba(35, 38, 41, 0.6);
            color: #CCCCCC;
            border: 1px solid rgba(90, 90, 90, 0.6);
            border-radius: 4px;
            padding: 2px 10px;
            font-size: 11px;
            min-height: 24px;
            min-width: 80px;
            max-height: 24px;
        }
        QPushButton:hover:!disabled {
            border: 1px solid #FFE066;
            color: #FFE066;
            background-color: rgba(255, 224, 102, 0.1);
        }
        QPushButton:pressed {
            background-color: rgba(255, 224, 102, 0.2);
            color: #FFD700;
        }
        QPushButton:disabled {
            border: 1px solid rgba(53, 57, 60, 0.5);
            color: #555555;
            background-color: transparent;
        }
    """
    
    MODE_BAR: str = """
        QWidget#ModeBar {
            background: rgba(30, 33, 36, 0.5);
            border: 1px solid rgba(60, 64, 70, 0.4);
            border-radius: 6px;
        }
        QPushButton[class="mode"] {
            background-color: transparent;
            color: #A0A0A0;
            border: 1px solid transparent;
            border-radius: 4px;
            padding: 4px 12px;
            font-size: 14px;
            font-weight: 500;
            min-width: 90px;
            min-height: 28px;
            max-height: 28px;
        }
        QPushButton[class="mode"]:hover:!disabled {
            color: #E0E0E0;
            border: 1px solid rgba(90, 90, 90, 0.5);
            background-color: rgba(255, 255, 255, 0.03);
        }
        QPushButton[class="mode"]:checked {
            border: 1px solid rgba(255, 215, 0, 0.8);
            color: #FFD700;
            background-color: rgba(255, 215, 0, 0.1);
        }

        QToolButton[class="icon-btn"] {
            background: transparent;
            border: 1px solid transparent;
            width: 32px;
            height: 32px;
            border-radius: 4px;
            padding: 0;
        }
        QToolButton[class="icon-btn"]:hover {
            border: 1px solid rgba(90, 90, 90, 0.5);
            background-color: rgba(255, 255, 255, 0.05);
        }
        QToolButton[class="icon-btn"]:checked {
            border: 1px solid rgba(255, 215, 0, 0.8);
            background-color: rgba(255, 215, 0, 0.15);
        }
    """
    
    RECYCLER_VIEW: str = """
        QScrollArea#RecyclerScrollArea {
            background: transparent;
            border: none;
        }
        QScrollArea#RecyclerScrollArea > QWidget > QWidget {
            background: transparent;
        }
    """
    
    PROFILE_CARD: str = """
        QFrame#ProfileCard {
            background-color: transparent;
            border: none;
        }
    """
    
    PROFILE_CARD_WRAPPER: str = """
        QWidget#ProfileCardWrapper {
            background-color: transparent;
            border: none;
        }
    """
    
    SELECTED_NAMES_BUFFER: str = f"""
        QTextEdit#SelectedNamesBuffer {{
            background-color: transparent;
            border: none;
            color: {Colors.TXT_SECONDARY};
            font-size: 12px;
            selection-background-color: {Colors.ACCENT};
            selection-color: {Colors.BG_DARK};
        }}
    """
    
    INPUT_MAIN: str = """
        QLineEdit[class="mass-action-input"] {
            background-color: rgba(35, 38, 41, 0.6);
            color: #F0F0F0;
            border: 1px solid rgba(90, 90, 90, 0.6);
            border-radius: 4px;
            font-size: 14px;
            padding: 5px 10px;
            min-height: 31px;
            max-height: 37px;
        }
        QLineEdit[class="mass-action-input"]:focus {
            border: 1px solid #FFE066;
            background-color: rgba(255, 224, 102, 0.08);
        }
    """
    
    INPUT_MINI: str = """
        QLineEdit[class="mini-input"] {
            min-width: 40px;
            max-width: 70px;
            background-color: rgba(35, 38, 41, 0.6);
            color: #F0F0F0;
            border: 1px solid rgba(90, 90, 90, 0.6);
            border-radius: 4px;
            font-size: 13px;
            padding: 4px 8px;
        }
        QLineEdit[class="mini-input"]:focus {
            border: 1px solid #FFE066;
            background-color: rgba(255, 224, 102, 0.08);
        }
    """
    
    GROUP_BOX: str = f"""
        QGroupBox {{
            color: {Colors.TXT_SECONDARY};
            font-size: 13px;
            font-weight: bold;
            border: 1px solid rgba(90, 90, 90, 0.4);
            border-radius: 6px;
            margin-top: 14px;
            padding-top: 12px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 10px;
            padding: 0 5px;
        }}
    """
    
    CHIP_PROFILE: str = f"""
        QLabel[class="chip-profile"] {{
            background-color: rgba(53, 57, 60, 0.7);
            color: {Colors.TXT_PRIMARY};
            border-radius: 10px;
            padding: 4px 10px;
            font-size: 12px;
        }}
    """
    
    CHIP_WALLET: str = f"""
        QLabel[class="chip-wallet"] {{
            background-color: rgba(45, 49, 54, 0.7);
            color: {Colors.ACCENT};
            border: 1px solid rgba(90, 90, 90, 0.6);
            border-radius: 10px;
            padding: 4px 10px;
            font-size: 12px;
            font-weight: bold;
        }}
    """
    
    CHECKBOX: str = f"""
        QCheckBox {{
            color: {Colors.TXT_PRIMARY};
            spacing: 6px;
            background: transparent;
            margin: 0px;
            padding: 0px;
        }}
        QCheckBox::indicator {{
            width: 12px;
            height: 12px;
            background-color: rgba(35, 38, 41, 0.6);
            border: 1px solid rgba(90, 90, 90, 0.6);
            border-radius: 3px;
        }}
        QCheckBox::indicator:hover {{
            border: 1px solid {Colors.ACCENT};
        }}
        QCheckBox::indicator:checked {{
            background-color: {Colors.ACCENT};
            border: 1px solid {Colors.ACCENT};
            image: none;
        }}
    """
    
    CODE_EDITOR: str = """
        QPlainTextEdit {
            background: rgba(30, 33, 36, 0.45);
            color: #EDEDED;
            border: 1px solid rgba(60, 64, 70, 0.5);
            border-radius: 10px;
            padding: 10px;
            font-family: Consolas, 'Courier New', monospace;
            font-size: 12px;
            line-height: 1.35em;
        }
    """
    
    INFO_TEXT: str = """
        QTextEdit {
            background: rgba(30, 33, 36, 0.45);
            color: #EDEDED;
            border: 1px solid rgba(60, 64, 70, 0.5);
            border-radius: 12px;
            padding: 12px;
            font-size: 13px;
            line-height: 1.35em;
        }
    """
    
    HINT_TEXT: str = f"""
        QLabel {{
            color: {Colors.TXT_HINT};
            font-size: 11px;
            padding-top: 8px;
            background: transparent;
        }}
    """
    
    PROGRESS_BAR: str = """
        QProgressBar { background-color: rgba(47, 52, 56, 0.6); border-radius: 4px; height: 8px; }
        QProgressBar::chunk { background-color: #FFE066; border-radius: 4px; }
    """
    
    LABEL_SETTING: str = "font-size:14px; font-weight:bold; color:#F0F0F0; background: transparent;"
    
    LABEL_LOG_HEADER: str = """
        QLabel {
            color: #A0A0A0;
            font-size: 12px;
            font-weight: bold;
            min-width: 48px;
            qproperty-alignment: AlignHCenter;
            background: transparent;
        }
    """
    
    COMBO_BOX_LOG: str = """
        QComboBox {
            background: rgba(40, 43, 46, 0.7);
            color: #FFD700;
            border: 1px solid rgba(136, 136, 136, 0.6);
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


class LogStyles:
    """Стилизация окна логов и уровней сообщений."""
    
    # Цвета уровней
    LEVELS: dict[str, dict[str, str]] = {
        "ERROR": {"color": Colors.ERROR, "emoji": "❌"},
        "WARNING": {"color": Colors.WARNING, "emoji": "⚠️"},
        "SUCCESS": {"color": Colors.SUCCESS, "emoji": "✅"},
        "START": {"color": "#6CB7FF", "emoji": "⏳"},
        "INFO": {"color": Colors.INFO, "emoji": "ℹ️"},
        "DEFAULT": {"color": "#DADADA", "emoji": "📝"},
    }
    
    # Темы оформления окна логов
    _BASE_FONT: str = "font-family: 'Segoe UI', Arial, sans-serif; font-size: 12px; padding: 8px; border-radius: 10px; letter-spacing: 0.2px;"
    
    THEMES: dict[str, str] = {
        "REGULAR": f"""
            QPlainTextEdit {{
                background-color: transparent;
                border: none;
                color: #ECECEC;
                {_BASE_FONT}
            }}
        """,
        "MATRIX": f"""
            QPlainTextEdit {{
                background-color: transparent;
                border: none;
                color: #00FF5A;
                {_BASE_FONT}
            }}
        """,
        "NEON": f"""
            QPlainTextEdit {{
                background-color: transparent;
                border: none;
                color: #00fff7;
                text-shadow: 0 0 4px #7d5fff, 0 0 6px #00fff7;
                {_BASE_FONT}
            }}
        """
    }