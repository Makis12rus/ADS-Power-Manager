"""
Модуль: core/_style_graphics.py
Назначение: Векторный цех и генератор графики (Presentation Utilities).
Зона ответственности: JIT-генерация 3D-глобусов стран (Bake & Blit), отрисовка системных
                      иконок (глазок пароля), применение стартовой темной темы и
                      динамическая перекраска векторных иконок (In-Memory XML Patching)
                      для ModeBar и карточек профилей.
                      Реализует паттерн Direct Vector Pipeline для обеспечения
                      субпиксельной точности на High-DPI экранах и Bridge Pattern
                      для обратной совместимости со стандартными виджетами Qt.
Интеграция: Изолированный модуль. Зависит от PySide6 и цветовой палитры (_style_colors.py).
            Импортируется лениво через фасад core/style.py только в момент отрисовки GUI,
            что спасает фоновые воркеры от загрузки тяжелых графических библиотек в ОЗУ.
"""

import threading
from PySide6.QtCore import Qt, QByteArray, QRectF
from PySide6.QtGui import (
    QColor, QPalette, QIcon, QPixmap, QPainter, QPen,
    QPainterPath, QRadialGradient
)
from PySide6.QtWidgets import QApplication
from PySide6.QtSvg import QSvgRenderer

from core._style_colors import Colors


class Graphics:
    """Генерация графики и иконок (Inline SVG Engine & Vector Paint Shop)."""
    
    # Кэш растровых иконок (для флагов, глазка пароля, ModeBar и стандартных кнопок)
    _ICON_CACHE: dict[str, QIcon | None] = {}
    
    # Кэш скомпилированных векторных рендереров (Direct Vector Pipeline)
    _RENDERER_CACHE: dict[str, QSvgRenderer] = {}
    _RENDERER_LOCK = threading.Lock()
    
    # =========================================================================
    # ВЕКТОРНЫЙ МАЛЯРНЫЙ ЦЕХ (JIT XML PATCHING)
    # =========================================================================
    # Строковые XML-шаблоны иконок Lucide. Атрибут stroke="{{COLOR}}" используется
    # для динамической инъекции HEX-цвета прямо в оперативной памяти.
    _VECTOR_SVG_TEMPLATES: dict[str, str] = {
        # --- Mode Bar ---
        "puzzle": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19.439 7.85c-.049.322.059.648.289.878l1.568 1.568c.47.47.706 1.087.706 1.704s-.235 1.233-.706 1.704l-1.611 1.611a.98.98 0 0 1-.867.279 1 1 0 0 0-1.071 1.071c.046.3-.045.621-.279.867l-1.611 1.611a2.408 2.408 0 0 1-3.409 0l-1.568-1.568a1.026 1.026 0 0 0-.877-.29 1 1 0 0 1-1.071-1.071c.047-.322-.061-.65-.29-.88l-1.569-1.568a2.408 2.408 0 0 1 0-3.409l1.612-1.611a.98.98 0 0 1 .867-.279 1 1 0 0 0 1.07-1.071c-.045-.3.046-.621.279-.867l1.611-1.611a2.408 2.408 0 0 1 3.409 0l1.568 1.568c.23.23.556.338.877.29a1 1 0 0 1 1.071 1.071z"/></svg>',
        "bot": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/></svg>',
        "file-text": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>',
        "pin": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" x2="12" y1="17" y2="22"/><path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.6V6a3 3 0 0 0-6 0v4.6a2 2 0 0 1-1.11 1.96l-1.78.9A2 2 0 0 0 5 15.24Z"/></svg>',
        "info": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
        
        # --- Card Actions (Кругляши управления профилем) ---
        "play": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 3 20 12 6 21 6 3"/></svg>',
        "rotate-cw": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>',
        "square-x": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/></svg>',
        "globe": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></svg>',
        
        # --- Drag and Drop (Хваталка) ---
        "grip-vertical": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="12" r="1"/><circle cx="9" cy="5" r="1"/><circle cx="9" cy="19" r="1"/><circle cx="15" cy="12" r="1"/><circle cx="15" cy="5" r="1"/><circle cx="15" cy="19" r="1"/></svg>',
        
        # --- Proxy Probe Engine (Индикаторы пинга) ---
        "zap": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
        "timer": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="10" x2="14" y1="2" y2="2"/><line x1="12" x2="15" y1="14" y2="11"/><circle cx="12" cy="14" r="8"/></svg>',
        "activity": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
        "wifi-off": '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="2" x2="22" y1="2" y2="22"/><path d="M8.5 16.5a5 5 0 0 1 7 0"/><path d="M2 8.82a15 15 0 0 1 4.17-2.65"/><path d="M10.66 5c4.01-.36 8.14.9 11.34 3.82"/><path d="M16.85 11.25a10 10 0 0 1 2.22 1.68"/><path d="M5 12.55a10 10 0 0 1 5.17-2.39"/><line x1="12" x2="12.01" y1="20" y2="20"/></svg>',
    }

    @staticmethod
    def get_svg_renderer(name: str, color_hex: str) -> QSvgRenderer:
        """
        Ленивая JIT-компиляция векторной иконки.
        Инжектирует цвет в XML-шаблон и возвращает готовый к отрисовке QSvgRenderer.
        Использует потокобезопасный кэш для O(1) доступа при массовой перерисовке.
        """
        cache_key = f"{name}_{color_hex}"
        
        # Fast-path: O(1) возврат из кэша без блокировки
        if cache_key in Graphics._RENDERER_CACHE:
            return Graphics._RENDERER_CACHE[cache_key]
            
        with Graphics._RENDERER_LOCK:
            # Double-check внутри критической секции
            if cache_key in Graphics._RENDERER_CACHE:
                return Graphics._RENDERER_CACHE[cache_key]
                
            template = Graphics._VECTOR_SVG_TEMPLATES.get(name)
            if not template:
                # Fallback: пустой квадрат, если имя иконки указано неверно
                template = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{{COLOR}}" stroke-width="2"><rect width="18" height="18" x="3" y="3" rx="2"/></svg>'
                
            patched_xml = template.replace("{{COLOR}}", color_hex)
            byte_array = QByteArray(patched_xml.encode("utf-8"))
            renderer = QSvgRenderer(byte_array)
            
            Graphics._RENDERER_CACHE[cache_key] = renderer
            return renderer

    @staticmethod
    def get_modebar_icon(name: str, color_hex: str, size: int = 20) -> QIcon:
        """
        Генерация простой однорежимной векторной иконки.
        Сохранена для обратной совместимости с ModeBar, где состояния
        управляются через ручную композицию пиксмапов.
        """
        cache_key = f"modebar_{name}_{color_hex}_{size}"
        
        if cache_key in Graphics._ICON_CACHE:
            cached_icon = Graphics._ICON_CACHE[cache_key]
            if cached_icon is not None:
                return cached_icon
                
        renderer = Graphics.get_svg_renderer(name, color_hex)
        
        # Прозрачный холст ARGB32
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        
        painter = QPainter(pixmap)
        try:
            # Включаем аппаратное сглаживание векторов
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            renderer.render(painter)
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            painter.end()
            
        icon = QIcon(pixmap)
        Graphics._ICON_CACHE[cache_key] = icon
        return icon

    @staticmethod
    def get_multi_state_icon(name: str, normal_hex: str, hover_hex: str, disabled_hex: str, size: int = 24) -> QIcon:
        """
        Генератор многорежимных (Multi-State) иконок для стандартных кнопок (QPushButton).
        Выпекает три состояния (Normal, Hover/Active, Disabled) и передает
        управление C++ машине состояний Qt.
        """
        cache_key = f"multi_state_{name}_{normal_hex}_{hover_hex}_{disabled_hex}_{size}"
        
        # Fast-Path: O(1) возврат из кэша
        if cache_key in Graphics._ICON_CACHE:
            cached_icon = Graphics._ICON_CACHE[cache_key]
            if cached_icon is not None:
                return cached_icon
                
        icon = QIcon()
        
        def render_to_pixmap(color_hex: str) -> QPixmap:
            """Локальный хелпер для безопасной растеризации вектора."""
            renderer = Graphics.get_svg_renderer(name, color_hex)
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pm)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                renderer.render(painter)
            finally:
                painter.end()
            return pm
        
        # Рендерим три физических пиксмапа для разных состояний
        pm_normal = render_to_pixmap(normal_hex)
        pm_hover = render_to_pixmap(hover_hex)
        pm_disabled = render_to_pixmap(disabled_hex)
        
        # Маппинг состояний C++ ядра Qt
        icon.addPixmap(pm_normal, QIcon.Mode.Normal, QIcon.State.Off)
        icon.addPixmap(pm_hover, QIcon.Mode.Active, QIcon.State.Off)    # Hover (наведение мыши)
        icon.addPixmap(pm_disabled, QIcon.Mode.Disabled, QIcon.State.Off) # Блокировка
        
        Graphics._ICON_CACHE[cache_key] = icon
        return icon

    # Алиас для обратной совместимости с AdsProfilePanel
    get_card_action_icon = get_multi_state_icon

    # =========================================================================
    # ГЕНЕРАТОР 3D-ГЛОБУСОВ СТРАН (BAKE & BLIT ENGINE)
    # =========================================================================
    # Ультра-минимизированные векторные формулы флагов (Zero-Disk Footprint).
    # Маски обрезки удалены для экономии памяти. Обрезка по кругу выполняется
    # математически через QPainterPath на этапе запекания.
    _SVG_FLAGS_DATA: dict[str, str] = {
        # --- СНГ и Восточная Европа ---
        "RU": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v171H0z"/><path fill="#0052b4" d="M0 171h512v170H0z"/><path fill="#d80027" d="M0 341h512v171H0z"/></svg>',
        "UA": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v256H0z"/><path fill="#ffda44" d="M0 256h512v256H0z"/></svg>',
        "BY": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v341H0z"/><path fill="#6da544" d="M0 341h512v171H0z"/><path fill="#eee" d="M0 0h60v512H0z"/><path fill="#d80027" d="M10 20l40 40-40 40 40 40-40 40z"/></svg>',
        "KZ": '<svg viewBox="0 0 512 512"><path fill="#00b0ea" d="M0 0h512v512H0z"/><circle fill="#ffda44" cx="256" cy="220" r="80"/><path fill="#ffda44" d="M40 0h40v512h-40z"/></svg>',
        "UZ": '<svg viewBox="0 0 512 512"><path fill="#00b0ea" d="M0 0h512v171H0z"/><path fill="#eee" d="M0 171h512v170H0z"/><path fill="#6da544" d="M0 341h512v171H0z"/><circle fill="#eee" cx="120" cy="85" r="40"/><circle fill="#00b0ea" cx="140" cy="85" r="40"/></svg>',
        "GE": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v512H0z"/><path fill="#d80027" d="M216 0h80v512h-80zM0 216h512v80H0z"/><circle fill="#d80027" cx="108" cy="108" r="30"/><circle fill="#d80027" cx="404" cy="108" r="30"/><circle fill="#d80027" cx="108" cy="404" r="30"/><circle fill="#d80027" cx="404" cy="404" r="30"/></svg>',
        "AM": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v171H0z"/><path fill="#0052b4" d="M0 171h512v170H0z"/><path fill="#ff9800" d="M0 341h512v171H0z"/></svg>',
        "MD": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h171v512H0z"/><path fill="#ffda44" d="M171 0h170v512H171z"/><path fill="#d80027" d="M341 0h171v512H341z"/></svg>',
        
        # --- Северная Америка и Океания ---
        "US": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v512H0z"/><path fill="#d80027" d="M0 46h512v46H0zm0 93h512v46H0zm0 93h512v46H0zm0 93h512v46H0zm0 93h512v46H0z"/><path fill="#0052b4" d="M0 0h256v279H0z"/><circle fill="#eee" cx="128" cy="139" r="80"/></svg>',
        "CA": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><path fill="#eee" d="M128 0h256v512H128z"/><path fill="#d80027" d="M256 120l50 150h50l-70 70 30 100-60-50-60 50 30-100-70-70h50z"/></svg>',
        "AU": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v512H0z"/><g transform="scale(0.5)"><path stroke="#eee" d="M0 0l512 512m0-512L0 512" stroke-width="115"/><path stroke="#d80027" d="M0 0l512 512m0-512L0 512" stroke-width="75"/><path stroke="#eee" d="M256 0v512M0 256h512" stroke-width="170"/><path stroke="#d80027" d="M256 0v512M0 256h512" stroke-width="115"/></g><circle fill="#eee" cx="128" cy="384" r="30"/><circle fill="#eee" cx="384" cy="128" r="20"/><circle fill="#eee" cx="448" cy="256" r="20"/><circle fill="#eee" cx="384" cy="384" r="20"/><circle fill="#eee" cx="320" cy="256" r="20"/></svg>',
        "NZ": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v512H0z"/><g transform="scale(0.5)"><path stroke="#eee" d="M0 0l512 512m0-512L0 512" stroke-width="115"/><path stroke="#d80027" d="M0 0l512 512m0-512L0 512" stroke-width="75"/><path stroke="#eee" d="M256 0v512M0 256h512" stroke-width="170"/><path stroke="#d80027" d="M256 0v512M0 256h512" stroke-width="115"/></g><circle fill="#d80027" cx="384" cy="128" r="20"/><circle fill="#d80027" cx="448" cy="256" r="20"/><circle fill="#d80027" cx="384" cy="384" r="20"/><circle fill="#d80027" cx="320" cy="256" r="20"/></svg>',
        
        # --- Европа (Западная, Центральная, Южная, Северная) ---
        "GB": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v512H0z"/><path stroke="#eee" d="M0 0l512 512m0-512L0 512" stroke-width="115"/><path stroke="#d80027" d="M0 0l512 512m0-512L0 512" stroke-width="75"/><path stroke="#eee" d="M256 0v512M0 256h512" stroke-width="170"/><path stroke="#d80027" d="M256 0v512M0 256h512" stroke-width="115"/></svg>',
        "NL": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v171H0z"/><path fill="#eee" d="M0 171h512v170H0z"/><path fill="#0052b4" d="M0 341h512v171H0z"/></svg>',
        "DE": '<svg viewBox="0 0 512 512"><path fill="#333" d="M0 0h512v171H0z"/><path fill="#d80027" d="M0 171h512v170H0z"/><path fill="#ffda44" d="M0 341h512v171H0z"/></svg>',
        "FR": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h171v512H0z"/><path fill="#eee" d="M171 0h170v512H171z"/><path fill="#d80027" d="M341 0h171v512H341z"/></svg>',
        "IT": '<svg viewBox="0 0 512 512"><path fill="#6da544" d="M0 0h171v512H0z"/><path fill="#eee" d="M171 0h170v512H171z"/><path fill="#d80027" d="M341 0h171v512H341z"/></svg>',
        "ES": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v128H0zm0 384h512v128H0z"/><path fill="#ffda44" d="M0 128h512v256H0z"/><circle fill="#d80027" cx="170" cy="256" r="50"/></svg>',
        "PT": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><path fill="#6da544" d="M0 0h200v512H0z"/><circle fill="#ffda44" cx="200" cy="256" r="60"/></svg>',
        "PL": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v256H0z"/><path fill="#d80027" d="M0 256h512v256H0z"/></svg>',
        "SE": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v512H0z"/><path fill="#ffda44" d="M150 0h80v512h-80zM0 216h512v80H0z"/></svg>',
        "FI": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v512H0z"/><path fill="#0052b4" d="M150 0h80v512h-80zM0 216h512v80H0z"/></svg>',
        "NO": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><path fill="#eee" d="M130 0h120v512h-120zM0 196h512v120H0z"/><path fill="#0052b4" d="M150 0h80v512h-80zM0 216h512v80H0z"/></svg>',
        "DK": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><path fill="#eee" d="M150 0h80v512h-80zM0 216h512v80H0z"/></svg>',
        "CH": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><path fill="#eee" d="M216 100h80v312h-80z"/><path fill="#eee" d="M100 216h312v80H100z"/></svg>',
        "AT": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v171H0zm0 341h512v171H0z"/><path fill="#eee" d="M0 171h512v170H0z"/></svg>',
        "BE": '<svg viewBox="0 0 512 512"><path fill="#333" d="M0 0h171v512H0z"/><path fill="#ffda44" d="M171 0h170v512H171z"/><path fill="#d80027" d="M341 0h171v512H341z"/></svg>',
        "IE": '<svg viewBox="0 0 512 512"><path fill="#6da544" d="M0 0h171v512H0z"/><path fill="#eee" d="M171 0h170v512H171z"/><path fill="#ff9800" d="M341 0h171v512H341z"/></svg>',
        "CZ": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v256H0z"/><path fill="#d80027" d="M0 256h512v256H0z"/><path fill="#0052b4" d="M0 0l256 256L0 512z"/></svg>',
        "HU": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v171H0z"/><path fill="#eee" d="M0 171h512v170H0z"/><path fill="#6da544" d="M0 341h512v171H0z"/></svg>',
        "RO": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h171v512H0z"/><path fill="#ffda44" d="M171 0h170v512H171z"/><path fill="#d80027" d="M341 0h171v512H341z"/></svg>',
        "BG": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v171H0z"/><path fill="#6da544" d="M0 171h512v170H0z"/><path fill="#d80027" d="M0 341h512v171H0z"/></svg>',
        "GR": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v57H0zm0 114h512v57H0zm0 114h512v57H0zm0 114h512v57H0zm0 114h512v55H0z"/><path fill="#eee" d="M0 57h512v57H0zm0 114h512v57H0zm0 114h512v57H0zm0 114h512v57H0z"/><path fill="#0052b4" d="M0 0h256v285H0z"/><path fill="#eee" d="M100 0h56v285h-56zM0 114h256v57H0z"/></svg>',
        "RS": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v171H0z"/><path fill="#0052b4" d="M0 171h512v170H0z"/><path fill="#eee" d="M0 341h512v171H0z"/></svg>',
        "HR": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v171H0z"/><path fill="#eee" d="M0 171h512v170H0z"/><path fill="#0052b4" d="M0 341h512v171H0z"/></svg>',
        "SK": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v171H0z"/><path fill="#0052b4" d="M0 171h512v170H0z"/><path fill="#d80027" d="M0 341h512v171H0z"/></svg>',
        "EE": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v171H0z"/><path fill="#333" d="M0 171h512v170H0z"/><path fill="#eee" d="M0 341h512v171H0z"/></svg>',
        "LV": '<svg viewBox="0 0 512 512"><path fill="#9e3039" d="M0 0h512v200H0zm0 312h512v200H0z"/><path fill="#eee" d="M0 200h512v112H0z"/></svg>',
        "LT": '<svg viewBox="0 0 512 512"><path fill="#ffda44" d="M0 0h512v171H0z"/><path fill="#6da544" d="M0 171h512v170H0z"/><path fill="#d80027" d="M0 341h512v171H0z"/></svg>',
        
        # --- Азия ---
        "CN": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><circle fill="#ffda44" cx="128" cy="128" r="60"/></svg>',
        "JP": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v512H0z"/><circle fill="#d80027" cx="256" cy="256" r="120"/></svg>',
        "KR": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v512H0z"/><path fill="#d80027" d="M156 256 a100 100 0 0 1 200 0 z"/><path fill="#0052b4" d="M156 256 a100 100 0 0 0 200 0 z"/></svg>',
        "IN": '<svg viewBox="0 0 512 512"><path fill="#ff9800" d="M0 0h512v171H0z"/><path fill="#eee" d="M0 171h512v170H0z"/><path fill="#6da544" d="M0 341h512v171H0z"/><circle fill="none" stroke="#0052b4" stroke-width="20" cx="256" cy="256" r="40"/></svg>',
        "VN": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><path fill="#ffda44" d="M256 120l40 120h120l-100 70 40 120-100-70-100 70 40-120-100-70h120z"/></svg>',
        "TH": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v85H0zm0 427h512v85H0z"/><path fill="#eee" d="M0 85h512v85H0zm0 256h512v85H0z"/><path fill="#0052b4" d="M0 170h512v171H0z"/></svg>',
        "ID": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v256H0z"/><path fill="#eee" d="M0 256h512v256H0z"/></svg>',
        "MY": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v512H0z"/><path fill="#d80027" d="M0 36h512v36H0zm0 73h512v36H0zm0 73h512v36H0zm0 73h512v36H0zm0 73h512v36H0zm0 73h512v36H0zm0 73h512v36H0z"/><path fill="#0052b4" d="M0 0h256v256H0z"/><circle fill="#ffda44" cx="128" cy="128" r="60"/><circle fill="#0052b4" cx="148" cy="128" r="50"/></svg>',
        "PH": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v256H0z"/><path fill="#d80027" d="M0 256h512v256H0z"/><path fill="#eee" d="M0 0l256 256L0 512z"/><circle fill="#ffda44" cx="85" cy="256" r="30"/></svg>',
        "SG": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v256H0z"/><path fill="#eee" d="M0 256h512v256H0z"/><circle fill="#eee" cx="150" cy="128" r="60"/><circle fill="#d80027" cx="180" cy="128" r="60"/></svg>',
        "HK": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><path fill="#eee" d="M256 100 q 50 100 0 200 q -50 -100 0 -200 z"/><path fill="#eee" d="M256 100 q 50 100 0 200 q -50 -100 0 -200 z" transform="rotate(72 256 256)"/><path fill="#eee" d="M256 100 q 50 100 0 200 q -50 -100 0 -200 z" transform="rotate(144 256 256)"/><path fill="#eee" d="M256 100 q 50 100 0 200 q -50 -100 0 -200 z" transform="rotate(216 256 256)"/><path fill="#eee" d="M256 100 q 50 100 0 200 q -50 -100 0 -200 z" transform="rotate(288 256 256)"/></svg>',
        "TW": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><path fill="#0052b4" d="M0 0h256v256H0z"/><circle fill="#eee" cx="128" cy="128" r="60"/></svg>',
        
        # --- Ближний Восток и Африка ---
        "TR": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v512H0z"/><circle fill="#eee" cx="256" cy="256" r="120"/><circle fill="#d80027" cx="290" cy="256" r="100"/><circle fill="#eee" cx="350" cy="256" r="30"/></svg>',
        "AE": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 171h512v170H0z"/><path fill="#6da544" d="M0 0h512v171H0z"/><path fill="#333" d="M0 341h512v171H0z"/><path fill="#d80027" d="M0 0h140v512H0z"/></svg>',
        "SA": '<svg viewBox="0 0 512 512"><path fill="#6da544" d="M0 0h512v512H0z"/><path fill="#eee" d="M100 256 h312 v20 h-312 z"/><path fill="#eee" d="M150 200 h212 v15 h-212 z"/></svg>',
        "IL": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v512H0z"/><path fill="#0052b4" d="M0 80h512v60H0zm0 312h512v60H0z"/><path fill="none" stroke="#0052b4" stroke-width="20" d="M256 160l80 140h-160zM256 352l80-140h-160z"/></svg>',
        "EG": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h512v171H0z"/><path fill="#eee" d="M0 171h512v170H0z"/><path fill="#333" d="M0 341h512v171H0z"/><circle fill="#ffda44" cx="256" cy="256" r="30"/></svg>',
        "ZA": '<svg viewBox="0 0 512 512"><path fill="#0052b4" d="M0 0h512v512H0z"/><path fill="#d80027" d="M0 0h512v256H0z"/><path fill="#eee" d="M0 30 l 200 196 h 312 v 60 h -312 l -200 196 z"/><path fill="#6da544" d="M0 70 l 160 156 h 352 v 60 h -352 l -160 156 z"/><path fill="#ffda44" d="M0 110 l 120 116 v 60 l -120 116 z"/><path fill="#333" d="M0 150 l 80 76 v 60 l -80 76 z"/></svg>',
        "NG": '<svg viewBox="0 0 512 512"><path fill="#6da544" d="M0 0h171v512H0z"/><path fill="#eee" d="M171 0h170v512H171z"/><path fill="#6da544" d="M341 0h171v512H341z"/></svg>',
        
        # --- Южная Америка ---
        "BR": '<svg viewBox="0 0 512 512"><path fill="#6da544" d="M0 0h512v512H0z"/><path fill="#ffda44" d="M256 64l192 192-192 192L64 256z"/><circle fill="#0052b4" cx="256" cy="256" r="90"/></svg>',
        "AR": '<svg viewBox="0 0 512 512"><path fill="#74acdf" d="M0 0h512v171H0zm0 341h512v171H0z"/><path fill="#eee" d="M0 171h512v170H0z"/><circle fill="#ffda44" cx="256" cy="256" r="40"/></svg>',
        "CO": '<svg viewBox="0 0 512 512"><path fill="#ffda44" d="M0 0h512v256H0z"/><path fill="#0052b4" d="M0 256h512v128H0z"/><path fill="#d80027" d="M0 384h512v128H0z"/></svg>',
        "MX": '<svg viewBox="0 0 512 512"><path fill="#6da544" d="M0 0h171v512H0z"/><path fill="#eee" d="M171 0h170v512H171z"/><path fill="#d80027" d="M341 0h171v512H341z"/><circle fill="#964B00" cx="256" cy="256" r="30"/></svg>',
        "CL": '<svg viewBox="0 0 512 512"><path fill="#eee" d="M0 0h512v256H0z"/><path fill="#d80027" d="M0 256h512v256H0z"/><path fill="#0052b4" d="M0 0h256v256H0z"/><circle fill="#eee" cx="128" cy="128" r="40"/></svg>',
        "PE": '<svg viewBox="0 0 512 512"><path fill="#d80027" d="M0 0h171v512H0z"/><path fill="#eee" d="M171 0h170v512H171z"/><path fill="#d80027" d="M341 0h171v512H341z"/></svg>',
        "VE": '<svg viewBox="0 0 512 512"><path fill="#ffda44" d="M0 0h512v171H0z"/><path fill="#0052b4" d="M0 171h512v170H0z"/><path fill="#d80027" d="M0 341h512v171H0z"/><circle fill="#eee" cx="256" cy="256" r="30"/></svg>',
    }
    
    @staticmethod
    def _generate_fallback_svg(code: str) -> str:
        """Динамическая генерация стильной заглушки для неизвестных стран."""
        c = code[:2].upper()
        return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
            <rect width="512" height="512" fill="#35393C"/>
            <text x="256" y="340" font-family="Arial, sans-serif" font-size="220" font-weight="bold" fill="#A0A0A0" text-anchor="middle">{c}</text>
        </svg>'''
    
    @staticmethod
    def get_country_icon(code: str) -> tuple[QIcon | None, str]:
        """
        Возвращает иконку 3D-глобуса страны (с кэшированием в ОЗУ).
        Использует многослойный композитный конвейер QPainter для наложения
        радиальных градиентов (Ambient Occlusion и Specular Highlight) поверх
        плоского вектора, обеспечивая идеальное сглаживание на High-DPI экранах.
        """
        c = (code or "").strip().upper()[:2]
        if not c:
            return None, "🌐 N/A"
        
        # Уникальный ключ кэша для 3D-версии
        cache_key = f"{c}:3D:20x20"
        
        # Fast-Path: O(1) возврат из кэша
        if cache_key in Graphics._ICON_CACHE:
            return Graphics._ICON_CACHE[cache_key], c
        
        # Slow-Path: Рендеринг (выполняется 1 раз на страну за сессию)
        svg_str = Graphics._SVG_FLAGS_DATA.get(c)
        if not svg_str:
            svg_str = Graphics._generate_fallback_svg(c)
        
        byte_array = QByteArray(svg_str.encode("utf-8"))
        renderer = QSvgRenderer(byte_array)
        
        # --- High-DPI Scaling ---
        app = QApplication.instance()
        dpr = 1.0
        if app and app.primaryScreen():
            dpr = app.primaryScreen().devicePixelRatio()
            
        base_size = 20
        phys_size = int(base_size * dpr)
        
        # Прозрачный холст ARGB32
        pixmap = QPixmap(phys_size, phys_size)
        pixmap.fill(Qt.GlobalColor.transparent)
        pixmap.setDevicePixelRatio(dpr)
        
        painter = QPainter(pixmap)
        try:
            # Включаем аппаратное сглаживание
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            
            rect_f = QRectF(0, 0, base_size, base_size)
            
            # 1. Clipping Path (Круглая маска)
            # Обрезаем квадратный SVG до идеального круга
            path = QPainterPath()
            path.addEllipse(rect_f)
            painter.setClipPath(path)
            
            # 2. Базовый слой (Плоский векторный флаг)
            renderer.render(painter, rect_f)
            
            # 3. Слой тени (Ambient Occlusion)
            # Смещение в правый нижний угол для создания объема
            shadow_grad = QRadialGradient(base_size * 0.7, base_size * 0.7, base_size * 0.8)
            shadow_grad.setColorAt(0.0, QColor(0, 0, 0, 0))
            shadow_grad.setColorAt(1.0, QColor(Colors.FLAG_SHADOW))
            
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            painter.fillRect(rect_f, shadow_grad)
            
            # 4. Слой блика (Specular Highlight)
            # Смещение в левый верхний угол для имитации источника света
            highlight_grad = QRadialGradient(base_size * 0.3, base_size * 0.3, base_size * 0.6)
            highlight_grad.setColorAt(0.0, QColor(Colors.FLAG_HIGHLIGHT))
            highlight_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            
            painter.fillRect(rect_f, highlight_grad)
            
            # 5. Сглаживающая фаска (Anti-aliasing Bezel)
            # Снимаем клиппинг и рисуем тонкую полупрозрачную рамку для устранения лесенок
            painter.setClipping(False)
            pen = QPen(QColor(0, 0, 0, 40), 0.5)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Рисуем чуть внутри, чтобы рамка не обрезалась границами холста
            painter.drawEllipse(QRectF(0.25, 0.25, base_size - 0.5, base_size - 0.5))
            
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            painter.end()
        
        icon = QIcon(pixmap)
        Graphics._ICON_CACHE[cache_key] = icon
        
        return icon, c
    
    @staticmethod
    def get_eye_icon(is_open: bool) -> QIcon:
        """
        Генерирует векторную иконку глаза (открытый/закрытый) для полей паролей.
        Избавляет от необходимости хранить внешние .png/.svg файлы.
        """
        pm = QPixmap(24, 24)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            pen = QPen(QColor(Colors.TXT_SECONDARY))
            pen.setWidth(2)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            
            # Рисуем контур глаза (овал)
            p.drawEllipse(3, 7, 18, 10)
            
            if is_open:
                # Зрачок
                p.drawEllipse(9, 9, 6, 6)
            else:
                # Перечеркивающая линия (закрытый глаз)
                p.drawLine(4, 20, 20, 4)
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста
            p.end()
        
        return QIcon(pm)
    
    @staticmethod
    def apply_boot_theme(app: QApplication) -> None:
        """Применяет базовую темную палитру для старта приложения."""
        pal = QPalette()
        
        c_bg = QColor(Colors.BG_MAIN)
        c_fg = QColor(Colors.TXT_PRIMARY)
        c_base = QColor(Colors.BG_DARK)
        c_highlight = QColor(Colors.ACCENT)
        c_highlighted_text = QColor(Colors.BG_MAIN)
        
        pal.setColor(QPalette.ColorRole.Window, c_bg)
        pal.setColor(QPalette.ColorRole.WindowText, c_fg)
        pal.setColor(QPalette.ColorRole.Base, c_base)
        pal.setColor(QPalette.ColorRole.AlternateBase, c_bg)
        pal.setColor(QPalette.ColorRole.ToolTipBase, c_bg)
        pal.setColor(QPalette.ColorRole.ToolTipText, c_fg)
        pal.setColor(QPalette.ColorRole.Text, c_fg)
        pal.setColor(QPalette.ColorRole.Button, c_bg)
        pal.setColor(QPalette.ColorRole.ButtonText, c_fg)
        pal.setColor(QPalette.ColorRole.BrightText, c_fg)
        pal.setColor(QPalette.ColorRole.Highlight, c_highlight)
        pal.setColor(QPalette.ColorRole.HighlightedText, c_highlighted_text)
        
        app.setPalette(pal)
        
        # Применяем базовый стиль для предотвращения белого мерцания
        app.setStyleSheet(f"""
            QWidget {{ background-color: {Colors.BG_MAIN}; color: {Colors.TXT_PRIMARY}; }}
            QToolTip {{ background-color: {Colors.BG_DARK}; color: {Colors.TXT_PRIMARY}; border: 1px solid #2D3136; }}
        """)