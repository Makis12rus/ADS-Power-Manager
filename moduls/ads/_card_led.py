"""
Модуль: moduls/ads/_card_led.py
Назначение: Изолированный компонент умного светодиода статуса (Presentation Layer).
Зона ответственности: Высокопроизводительная отрисовка индикатора состояния профиля
                      (пульсирующий неон, статика или векторный спиннер).
                      Использует паттерн Bake and Blit для O(1) рендеринга и
                      подписывается на глобальный метроном (SharedTicker) для
                      Zero-CPU синхронизации анимаций.
Интеграция: Слой GUI. Является частью декомпозированной виртуальной карусели.
            Импортируется в `_card_cells.py` для сборки ячейки статуса.
            Не содержит бизнес-логики, оперирует строгими DTO-состояниями (ProfileState).
"""

from PySide6.QtCore import Qt, Slot, QRectF
from PySide6.QtGui import QPainter, QColor, QPaintEvent, QPen
from PySide6.QtWidgets import QWidget

# Строгие абсолютные импорты ядра
from core._constants import ProfileState, TRANSIT_STATES

# Ленивые импорты из фасада стилей (PEP 562)
from core.style import shared_ticker, CachedLedPainter, Colors


class StatusLedWidget(QWidget):
    """
    Умный маяк статуса профиля.
    Подписан на глобальный метроном (SharedTicker). Отрисовывает закэшированные
    пиксмапы за O(1) или векторный спиннер, обеспечивая Zero-CPU анимацию.
    """
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Жесткая фиксация геометрии для предотвращения Layout Reflow
        self.setFixedSize(20, 20)
        
        self._state: ProfileState = ProfileState.UNKNOWN
        self._frame: int = 0
        self._angle: int = 0
        
        # Подписка на бригадный пульс (глобальный метроном)
        shared_ticker.tick.connect(self._on_tick)
    
    @Slot(int, int)
    def _on_tick(self, frame: int, angle: int) -> None:
        """
        Слот глобального метронома. Обновляет внутреннее состояние кадров
        и запрашивает перерисовку только при необходимости.
        """
        self._frame = frame
        self._angle = angle
        
        # Оптимизация (Resource Guard): перерисовываем только если виджет физически
        # виден на экране и его состояние подразумевает анимацию.
        if self.isVisible() and self._state not in (ProfileState.CLOSED, ProfileState.UNKNOWN):
            self.update()
    
    def set_state(self, state: ProfileState, tooltip: str) -> None:
        """
        Устанавливает новое состояние и всплывающую подсказку.
        Триггерит перерисовку только при фактическом изменении данных.
        """
        if self._state != state or self.toolTip() != tooltip:
            self._state = state
            self.setToolTip(tooltip)
            self.update()
    
    def paintEvent(self, event: QPaintEvent) -> None:
        """
        Блиц-отрисовка диода или спиннера (Bake and Blit Pipeline).
        Никаких тяжелых аллокаций памяти внутри метода!
        """
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.rect()
            
            if self._state in TRANSIT_STATES:
                # Отрисовка векторного спиннера для транзитных состояний (Запуск/Остановка)
                pen_bg = QPen(QColor(Colors.TXT_DIM).darker(150), 2)
                painter.setPen(pen_bg)
                painter.drawEllipse(rect.adjusted(2, 2, -2, -2))
                
                pen_arc = QPen(QColor(Colors.ACCENT), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                painter.setPen(pen_arc)
                # drawArc принимает углы в 1/16 градуса. Отрисовываем дугу в 100 градусов.
                painter.drawArc(rect.adjusted(2, 2, -2, -2), -self._angle * 16, 100 * 16)
            else:
                # Отрисовка закэшированного пиксмапа (Bake and Blit)
                cache_key = f"led_{self._state.value}_{self._frame}"
                
                # Статичные состояния не имеют кадров анимации
                if self._state in (ProfileState.CLOSED, ProfileState.UNKNOWN):
                    cache_key = f"led_{self._state.value}_0"
                
                # Достаем готовый пиксмап из нашего железобетонного Python-словаря
                pm = CachedLedPainter.get_frame(cache_key)
                if pm:
                    # Идеальное центрирование 16x16 пиксмапа в 20x20 виджете
                    x = (rect.width() - 16) / 2.0
                    y = (rect.height() - 16) / 2.0
                    painter.drawPixmap(QRectF(x, y, 16, 16), pm, QRectF(pm.rect()))
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            painter.end()