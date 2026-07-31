"""
Модуль: moduls/ads/_card_cells.py
Назначение: Изолированный цех сборки ячеек для карточки профиля (Presentation Layer).
Зона ответственности: Декларативное описание всех колонок виртуальной карусели
                      (хваталка, номер, статус, инфо, прокси, пинг, кнопки управления).
                      Реализует паттерн Composite View: каждая ячейка автономна,
                      умеет гидратировать себя из DTO и сбрасывать визуальное состояние.
                      Включает триггеры для локального Drag-and-Drop движка и
                      RichText Hyperlink Delegation для копирования ID и IP в буфер.
Интеграция: Слой GUI. Ячейки собираются воедино контейнером `ProfileRowCard`
            (в `_card_row.py`). Не содержат бизнес-логики, общаются с внешним миром
            исключительно через локальные сигналы (например, `cellActionRequested`, `dragInitiated`).
"""

import threading
from typing import Any

from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QPainter, QPaintEvent, QPixmap, QMouseEvent
from PySide6.QtWidgets import (
    QFrame, QWidget, QHBoxLayout, QVBoxLayout, QLabel,
    QSizePolicy, QApplication
)

# Строгие абсолютные импорты ядра
from system.logger import logger
from core._constants import ProfileState, TRANSIT_STATES, PING_FAST, PING_MEDIUM

# Ленивые импорты из фасада стилей (PEP 562)
from core.style import Colors, Graphics, FlatActionButton

# Импорт изолированного светодиода
from moduls.ads._card_led import StatusLedWidget


# =============================================================================
# 1. БАЗОВЫЙ КОНТРАКТ ЯЧЕЙКИ (COMPOSITE VIEW PATTERN)
# =============================================================================

class BaseCardCell(QFrame):
    """
    Абстрактный фундамент для всех ячеек карточки профиля.
    Обеспечивает жесткую фиксацию ширины (Column Width Locking) и
    стандартизирует методы проливки данных (Hydration).
    """
    
    def __init__(self, width: int = 0, stretch: int = 0, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # КРИТИЧНО: Разрешаем кастомному QFrame транслировать QSS-стили к детям
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        
        # Строго скоупированный стиль (Scoped Style).
        # Предотвращает баг "Wildcard Inheritance", когда transparent применялся ко всем кнопкам внутри.
        self.setStyleSheet("BaseCardCell { background: transparent; border: none; }")
        
        self.layout_box = QHBoxLayout(self)
        self.layout_box.setContentsMargins(0, 0, 0, 0)
        self.layout_box.setSpacing(8)
        
        if width > 0:
            self.setFixedWidth(width)
        elif stretch > 0:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    
    def hydrate(self, dto: dict[str, Any], is_selected: bool) -> None:
        """Наполнение ячейки данными из DTO. Переопределяется потомками."""
        pass
    
    def update_status(self, state: ProfileState, tooltip: str) -> None:
        """Реакция ячейки на изменение статуса профиля. Переопределяется потомками."""
        pass
    
    def reset_visuals(self) -> None:
        """Очистка состояния при переиспользовании виджета (Recycling)."""
        pass


# =============================================================================
# 2. ИЗОЛИРОВАННЫЕ ЯЧЕЙКИ (THE LEGO BLOCKS)
# =============================================================================

class DragHandleCell(BaseCardCell):
    """
    Ячейка №1: Хваталка (Drag Handle).
    Отрисовывает векторную иконку из 6 точек и меняет курсор на перекрестие.
    Использует аппаратный рендеринг SVG для нулевого потребления ОЗУ.
    Выступает триггером для локального движка Drag-and-Drop.
    """
    
    # Сигналы для инициации и завершения перетаскивания
    dragInitiated = Signal()
    dragReleased = Signal()
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(width=30, parent=parent)
        self.setCursor(Qt.CursorShape.SizeAllCursor)
        self.setToolTip("Перетащите для изменения порядка")
    
    def paintEvent(self, event: QPaintEvent) -> None:
        """Аппаратный рендеринг SVG-иконки хваталки."""
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer = Graphics.get_svg_renderer("grip-vertical", Colors.TXT_DIM)
            
            # Идеальное центрирование иконки 16x16 внутри ячейки 30px
            icon_size = 16.0
            x = (self.width() - icon_size) / 2.0
            y = (self.height() - icon_size) / 2.0
            
            renderer.render(painter, QRectF(x, y, icon_size, icon_size))
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Перехват клика для старта локального Drag-and-Drop."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragInitiated.emit()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Перехват отпускания мыши (на случай, если вьюпорт не забрал фокус)."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragReleased.emit()
        super().mouseReleaseEvent(event)


class OrdinalNumberCell(BaseCardCell):
    """
    Ячейка №2: Порядковый номер (Сквозная нумерация).
    Читает заранее вычисленный индекс из DTO за O(1).
    """
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(width=40, parent=parent)
        
        self.lbl_num = QLabel()
        self.lbl_num.setStyleSheet(f"color: {Colors.TXT_DIM}; font-size: 12px; font-weight: bold;")
        self.lbl_num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.layout_box.addWidget(self.lbl_num)
    
    def hydrate(self, dto: dict[str, Any], is_selected: bool) -> None:
        num = dto.get("display_num", "")
        self.lbl_num.setText(str(num) if num else "")


class StatusLedCell(BaseCardCell):
    """Ячейка №3: Маяк статуса (Умный светодиод)."""
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(width=40, parent=parent)
        self.status_led = StatusLedWidget(self)
        self.layout_box.addWidget(self.status_led)
        self.layout_box.addStretch(1)
    
    def update_status(self, state: ProfileState, tooltip: str) -> None:
        self.status_led.set_state(state, tooltip)
    
    def reset_visuals(self) -> None:
        self.status_led.set_state(ProfileState.UNKNOWN, "")


class ProfileInfoCell(BaseCardCell):
    """
    Ячейка №4: Паспорт профиля (Имя и кликабельный ID).
    Использует HTML-трюк для копирования ID без перехвата событий мыши у карусели.
    """
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(stretch=1, parent=parent)
        
        vbox = QVBoxLayout()
        vbox.setContentsMargins(0, 0, 0, 0)
        vbox.setSpacing(2)
        # Строгое выравнивание по центру (исправляет баг прилипания к потолку)
        vbox.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        
        self.lbl_name = QLabel()
        self.lbl_name.setStyleSheet(f"color: {Colors.TXT_PRIMARY}; font-size: 12px; font-weight: bold;")
        
        self.lbl_id = QLabel()
        self.lbl_id.setTextFormat(Qt.TextFormat.RichText)
        # Разрешаем кликать только по ссылкам, чтобы не блокировать свайп-выделение строки
        self.lbl_id.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.lbl_id.linkActivated.connect(self._on_id_clicked)
        
        vbox.addWidget(self.lbl_name)
        vbox.addWidget(self.lbl_id)
        
        self.layout_box.addLayout(vbox)
        self.layout_box.addStretch(1)
    
    def hydrate(self, dto: dict[str, Any], is_selected: bool) -> None:
        self.lbl_name.setText(dto.get("name", "Unknown"))
        uid = dto.get("user_id", "")
        
        # Формируем HTML-ссылку. Qt сам обработает наведение и клик.
        html_link = (
            f'<style>'
            f'a {{ text-decoration: none; color: {Colors.TXT_DIM}; font-size: 10px; font-family: Consolas; }} '
            f'a:hover {{ color: {Colors.ACCENT}; }}'
            f'</style>'
            f'<a href="{uid}">[{uid}]</a>'
        )
        self.lbl_id.setText(html_link)
    
    def _on_id_clicked(self, link: str) -> None:
        """Слот копирования ID в системный буфер обмена."""
        QApplication.clipboard().setText(link)
        logger.info(
            f"ID профиля скопирован в буфер обмена: {link}",
            profile_names=["GLOBAL"], category="SYSTEM"
        )


class ProxyCell(BaseCardCell):
    """
    Ячейка №5: Сеть (3D-Флаг и кликабельный IP-адрес).
    Использует RichText Hyperlink Delegation для копирования IP в буфер обмена.
    """
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(width=130, parent=parent)
        
        self.lbl_flag = QLabel()
        # Идеальный размер 20x20 для предотвращения обрезки SVG-рендера
        self.lbl_flag.setFixedSize(20, 20)
        
        self.lbl_ip = QLabel()
        self.lbl_ip.setTextFormat(Qt.TextFormat.RichText)
        # Разрешаем кликать только по ссылкам, чтобы не блокировать свайп-выделение строки
        self.lbl_ip.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.lbl_ip.linkActivated.connect(self._on_ip_clicked)
        
        self.layout_box.addWidget(self.lbl_flag)
        self.layout_box.addWidget(self.lbl_ip)
        self.layout_box.addStretch(1)
    
    def hydrate(self, dto: dict[str, Any], is_selected: bool) -> None:
        self.update_proxy(dto.get("ip", "No IP"), dto.get("ip_country", ""))
    
    def update_proxy(self, ip: str, country: str) -> None:
        """Точечное обновление ГЕО-данных (O(1) Update)."""
        if not ip or ip in ("No IP", "Проверяется...", "Error"):
            # Не кликабельный текст для системных статусов
            self.lbl_ip.setText(f'<span style="color: {Colors.TXT_SECONDARY}; font-size: 12px;">{ip}</span>')
        else:
            # Кликабельная HTML-ссылка для реальных IP (Zero-CPU Hover)
            html_link = (
                f'<style>'
                f'a {{ text-decoration: none; color: {Colors.TXT_SECONDARY}; font-size: 12px; font-family: Consolas; }} '
                f'a:hover {{ color: {Colors.ACCENT}; }}'
                f'</style>'
                f'<a href="{ip}">{ip}</a>'
            )
            self.lbl_ip.setText(html_link)
            
        icon, _ = Graphics.get_country_icon(country)
        if icon:
            # Запрашиваем строго 20x20 логических пикселей.
            # QIcon сам подберет High-DPI версию (например, 40x40 физических)
            # и QLabel отрисует ее с идеальной субпиксельной резкостью.
            self.lbl_flag.setPixmap(icon.pixmap(20, 20))
        else:
            self.lbl_flag.clear()

    def _on_ip_clicked(self, link: str) -> None:
        """Слот копирования IP-адреса в системный буфер обмена."""
        QApplication.clipboard().setText(link)
        logger.info(
            f"IP-адрес скопирован в буфер обмена: {link}",
            profile_names=["GLOBAL"], category="SYSTEM"
        )


class LatencyCell(BaseCardCell):
    """
    Ячейка №6: Пинг (HTTP RTT).
    Отображает задержку прокси-канала с использованием векторных иконок Lucide.
    Реализует паттерн Bake & Blit для кэширования иконок в ОЗУ.
    """
    
    # Глобальный кэш пиксмапов для всех инстансов ячейки (Zero-RAM Footprint)
    _PIXMAP_CACHE: dict[str, QPixmap] = {}
    _CACHE_LOCK = threading.Lock()
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(width=75, parent=parent)
        self.layout_box.setSpacing(6)
        
        self.lbl_icon = QLabel()
        self.lbl_icon.setFixedSize(16, 16)
        
        self.lbl_text = QLabel()
        self.lbl_text.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        
        self.layout_box.addWidget(self.lbl_icon)
        self.layout_box.addWidget(self.lbl_text)
        self.layout_box.addStretch(1)
        
    def _get_icon_pixmap(self, icon_name: str, color_hex: str) -> QPixmap:
        """Ленивая генерация и кэширование High-DPI иконки."""
        cache_key = f"{icon_name}_{color_hex}"
        
        # Fast-Path
        if cache_key in LatencyCell._PIXMAP_CACHE:
            return LatencyCell._PIXMAP_CACHE[cache_key]
            
        with LatencyCell._CACHE_LOCK:
            if cache_key in LatencyCell._PIXMAP_CACHE:
                return LatencyCell._PIXMAP_CACHE[cache_key]
                
            renderer = Graphics.get_svg_renderer(icon_name, color_hex)
            dpr = self.devicePixelRatioF()
            
            pm = QPixmap(int(16 * dpr), int(16 * dpr))
            pm.setDevicePixelRatio(dpr)
            pm.fill(Qt.GlobalColor.transparent)
            
            painter = QPainter(pm)
            try:
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                renderer.render(painter, QRectF(0, 0, 16, 16))
            finally:
                painter.end()
                
            LatencyCell._PIXMAP_CACHE[cache_key] = pm
            return pm

    def hydrate(self, dto: dict[str, Any], is_selected: bool) -> None:
        self.update_latency(dto.get("latency", -1))
        
    def update_latency(self, latency: int) -> None:
        """Точечное обновление пинга (O(1) Update)."""
        if latency < 0:
            icon_name, color = "wifi-off", Colors.TXT_DIM
            text = "Error"
        elif latency <= PING_FAST:
            icon_name, color = "zap", Colors.NEON_GREEN
            text = f"{latency} ms"
        elif latency <= PING_MEDIUM:
            icon_name, color = "timer", Colors.ACCENT
            text = f"{latency} ms"
        else:
            icon_name, color = "activity", Colors.ERROR
            text = f"{latency} ms"
            
        self.lbl_icon.setPixmap(self._get_icon_pixmap(icon_name, color))
        self.lbl_text.setText(text)
        self.lbl_text.setStyleSheet(f"color: {color}; font-size: 12px; font-weight: bold;")
        
    def reset_visuals(self) -> None:
        self.update_latency(-1)


class ActionButtonsCell(BaseCardCell):
    """
    Ячейка №7: Пульт управления.
    Использует премиальные хрустальные 3D-сферы (FlatActionButton с CrystalCache).
    Оснащена декларативной машиной состояний для предотвращения Layout Shift.
    """
    
    # Локальный сигнал ячейки. Будет проброшен в главную шину карточки.
    cellActionRequested = Signal(str)
    
    # Декларативная карта видимости кнопок для стабильных состояний
    _STABLE_CONFIGS: dict[str, dict[str, bool]] = {
        "ACTIVE": {
            "check_proxy": True,
            "start": False,
            "restart": True,
            "stop": True
        },
        "IDLE": {
            "check_proxy": True,
            "start": True,
            "restart": False,
            "stop": False
        }
    }
    
    def __init__(self, parent: QWidget | None = None) -> None:
        # Увеличена ширина до 165px для размещения 4-й кнопки и "воздуха" для 3D-теней
        super().__init__(width=165, parent=parent)
        
        self.btn_check_proxy = self._make_action_btn(
            icon_name="globe", tooltip="Проверить прокси", mode="check_proxy",
            idle_bg=Colors.BTN_ACTION_IDLE_BG, hover_bg=Colors.BTN_PLAY_HOVER_BG,
            idle_icon=Colors.BTN_PLAY_IDLE_ICON, hover_icon=Colors.NEON_BLUE,
            disabled_bg=Colors.BTN_DISABLED_BG, disabled_icon=Colors.BTN_DISABLED_ICON
        )
        self.btn_start = self._make_action_btn(
            icon_name="play", tooltip="Запустить", mode="open",
            idle_bg=Colors.BTN_ACTION_IDLE_BG, hover_bg=Colors.BTN_PLAY_HOVER_BG,
            idle_icon=Colors.BTN_PLAY_IDLE_ICON, hover_icon=Colors.NEON_GREEN,
            disabled_bg=Colors.BTN_DISABLED_BG, disabled_icon=Colors.BTN_DISABLED_ICON
        )
        self.btn_restart = self._make_action_btn(
            icon_name="rotate-cw", tooltip="Перезапустить", mode="restart",
            idle_bg=Colors.BTN_ACTION_IDLE_BG, hover_bg=Colors.BTN_RESTART_HOVER_BG,
            idle_icon=Colors.BTN_RESTART_IDLE_ICON, hover_icon=Colors.ACCENT,
            disabled_bg=Colors.BTN_DISABLED_BG, disabled_icon=Colors.BTN_DISABLED_ICON
        )
        self.btn_stop = self._make_action_btn(
            icon_name="square-x", tooltip="Закрыть", mode="close",
            idle_bg=Colors.BTN_ACTION_IDLE_BG, hover_bg=Colors.BTN_STOP_HOVER_BG,
            idle_icon=Colors.BTN_STOP_IDLE_ICON, hover_icon=Colors.ERROR,
            disabled_bg=Colors.BTN_DISABLED_BG, disabled_icon=Colors.BTN_DISABLED_ICON
        )
        
        self.layout_box.addStretch(1)
        self.layout_box.addWidget(self.btn_check_proxy)
        self.layout_box.addWidget(self.btn_start)
        self.layout_box.addWidget(self.btn_restart)
        self.layout_box.addWidget(self.btn_stop)
    
    def _make_action_btn(
            self, icon_name: str, tooltip: str, mode: str,
            idle_bg: str, hover_bg: str, idle_icon: str, hover_icon: str,
            disabled_bg: str, disabled_icon: str
    ) -> FlatActionButton:
        """Хелпер для создания хрустальной кнопки и привязки её к локальному сигналу."""
        btn = FlatActionButton(
            icon_name=icon_name, idle_bg_hex=idle_bg, hover_bg_hex=hover_bg,
            idle_icon_hex=idle_icon, hover_icon_hex=hover_icon,
            disabled_bg_hex=disabled_bg, disabled_icon_hex=disabled_icon,
            tooltip=tooltip, button_size=32, icon_size=16, parent=self
        )
        # Пробрасываем клик в локальный сигнал ячейки
        btn.clicked.connect(lambda _=False, m=mode: self.cellActionRequested.emit(m))
        return btn
    
    def update_status(self, state: ProfileState, tooltip: str) -> None:
        """
        Декларативное управление видимостью и доступностью кнопок.
        Предотвращает Cumulative Layout Shift (CLS) и двойные клики во время транзитных состояний.
        """
        if state in TRANSIT_STATES:
            # Транзитное состояние: замораживаем текущий расклад (Zero-CLS)
            self.btn_check_proxy.setEnabled(False)
            self.btn_start.setEnabled(False)
            self.btn_restart.setEnabled(False)
            self.btn_stop.setEnabled(False)
        else:
            # Стабильное состояние: применяем декларативную карту видимости
            config_key = "ACTIVE" if state == ProfileState.ACTIVE else "IDLE"
            cfg = self._STABLE_CONFIGS[config_key]
            
            self.btn_check_proxy.setVisible(cfg["check_proxy"])
            self.btn_start.setVisible(cfg["start"])
            self.btn_restart.setVisible(cfg["restart"])
            self.btn_stop.setVisible(cfg["stop"])
            
            # Разблокируем все кнопки
            self.btn_check_proxy.setEnabled(True)
            self.btn_start.setEnabled(True)
            self.btn_restart.setEnabled(True)
            self.btn_stop.setEnabled(True)
    
    def reset_visuals(self) -> None:
        """
        Сброс анимаций кнопок и возврат к дефолтному (закрытому) состоянию
        при переиспользовании ячейки (Recycling).
        """
        self.update_status(ProfileState.CLOSED, "")
        self.btn_check_proxy.reset_state()
        self.btn_start.reset_state()
        self.btn_restart.reset_state()
        self.btn_stop.reset_state()


# =============================================================================
# 3. ДЕКЛАРАТИВНЫЙ МАНИФЕСТ КОНВЕЙЕРА (THE PIPELINE)
# =============================================================================

# Порядок классов в этом списке строго определяет порядок колонок в карточке профиля.
# Контейнер строки (ProfileRowCard) будет итерироваться по этому списку при сборке.
ROW_CELL_PIPELINE = [
    DragHandleCell,
    OrdinalNumberCell,
    StatusLedCell,
    ProfileInfoCell,
    ProxyCell,
    LatencyCell,
    ActionButtonsCell
]