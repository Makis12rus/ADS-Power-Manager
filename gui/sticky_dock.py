"""
Модуль: gui/sticky_dock.py
Назначение: Изолированный компонент плавающей панели логов (Presentation Layer).
Зона ответственности: Удержание панели логов в виде независимого окна (Qt.Tool),
                      расчет расстояния до краев главного окна и триггер прилипания
                      (Magnetic Snapping).
                      Обеспечивает тактильный комфорт пользователя без блокировки
                      разметки главного окна (Layout Recursion Loop устранен).
Интеграция: Абсолютно независимый виджет. Не импортирует MainWindow или LogWindow.
            Общается с внешним миром исключительно через сигналы (Mediator Pattern).
            Поддерживает Duck Typing для совместимости со старым кодом QDockWidget.
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QMoveEvent, QResizeEvent, QShowEvent, QHideEvent, QMouseEvent

from system.logger import logger


class StickyDock(QWidget):
    """
    Плавающая панель-инструмент (Tool Window), которая прилипает к краям родительского окна.
    Работает абсолютно автономно, не встраиваясь в QMainWindowLayout, что гарантирует
    плавный ресайз главного окна без зависаний и черных дыр.
    Использует нативную шапку ОС для перетаскивания, но без системных кнопок закрытия.
    """
    
    # --- СИГНАЛЫ (КОНТРАКТ МЕДИАТОРА) ---
    # Запрос к главному окну на принудительное выравнивание координат
    alignmentRequested = Signal()
    # Уведомление о смене стороны прилипания ("left" или "right")
    snapSideChanged = Signal(str)
    # Уведомление о том, что панель оторвали от краев (свободное плавание)
    unsnapped = Signal()
    # Уведомление об изменении видимости (замена нативного сигнала QDockWidget)
    visibilityChanged = Signal(bool)
    
    def __init__(
            self,
            title: str,
            parent: QWidget | None = None,
            gap: int = 10,
            snap_threshold: int = 60
    ) -> None:
        """
        Инициализация магнитного дока.

        :param title: Заголовок панели.
        :param parent: Родительский виджет (обычно MainWindow).
        :param gap: Зазор в пикселях между главным окном и доком при прилипании.
        :param snap_threshold: Дистанция срабатывания магнита в пикселях.
        """
        super().__init__(parent)
        
        self.setWindowTitle(title)
        
        # КРИТИЧНО: Превращаем виджет в независимую палитру инструментов.
        # Qt.Tool - делает окно плавающим поверх родителя (не появляется в панели задач).
        # Qt.CustomizeWindowHint - отменяет дефолтные кнопки ОС (закрыть, свернуть).
        # Qt.WindowTitleHint - возвращает полосу заголовка для нативного перетаскивания.
        self.setWindowFlags(
            Qt.WindowType.Tool |
            Qt.WindowType.CustomizeWindowHint |
            Qt.WindowType.WindowTitleHint
        )
        
        # Защита от перехвата фокуса клавиатуры у главного окна при кликах по фону логов
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        
        self._gap: int = gap
        self._snap_threshold: int = snap_threshold
        self._current_snap_state: str | None = None
        
        # Внутренняя разметка для удержания контента (LogWindow)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
    
    # ===================== DUCK TYPING (СОВМЕСТИМОСТЬ С QDOCKWIDGET) =====================
    
    def setWidget(self, widget: QWidget) -> None:
        """Помещает целевой виджет (LogWindow) внутрь нашей независимой панели."""
        self._layout.addWidget(widget)
        
    def isFloating(self) -> bool:
        """Заглушка. Tool-окно всегда находится в свободном плавании."""
        return True
        
    def setFloating(self, floating: bool) -> None:
        """Заглушка для обратной совместимости с вызовами из MainWindow."""
        pass

    # ===================== MAGNETIC SNAPPING ENGINE =====================
    
    def _maybe_snap_to_main(self) -> None:
        """
        Логика магнитных краев (Snapping).
        Вычисляет дистанцию до родительского окна и эмитирует сигналы прилипания.
        """
        parent = self.parentWidget()
        if not parent:
            return
        
        mw_geo = parent.frameGeometry()
        dock_geo = self.frameGeometry()
        
        # Вычисляем целевые координаты для левого и правого бортов
        right_target = mw_geo.x() + mw_geo.width() + self._gap
        left_target = mw_geo.x() - dock_geo.width() - self._gap
        
        dist_right = abs(dock_geo.x() - right_target)
        dist_left = abs(dock_geo.x() - left_target)
        
        # Если мы в зоне действия магнита
        if min(dist_left, dist_right) <= self._snap_threshold:
            side = "right" if dist_right <= dist_left else "left"
            
            # Эмитируем смену стороны только если она реально изменилась (защита от спама)
            if self._current_snap_state != side:
                self._current_snap_state = side
                self.snapSideChanged.emit(side)
                logger.info(
                    f"[StickyDock] Панель примагнитилась к {side} борту. Держим строй.",
                    profile_names=["GLOBAL"], category="SYSTEM"
                )
            
            # Запрос на выравнивание отправляем всегда, пока мы в зоне магнита,
            # чтобы родитель мог жестко зафиксировать координаты при отпускании мыши.
            self.alignmentRequested.emit()
        else:
            # Если оторвались от магнита
            if self._current_snap_state is not None:
                self._current_snap_state = None
                self.unsnapped.emit()
    
    # ===================== ПЕРЕХВАТЧИКИ СОБЫТИЙ ОС =====================
    
    def moveEvent(self, event: QMoveEvent) -> None:
        """Перехватчик движения дока. Триггерит магнит."""
        super().moveEvent(event)
        self._maybe_snap_to_main()
    
    def resizeEvent(self, event: QResizeEvent) -> None:
        """
        Перехватчик изменения размеров.
        Если юзер пытается изменить высоту примагниченного дока,
        мы шлем сигнал выравнивания, чтобы главное окно вернуло высоту на место.
        """
        super().resizeEvent(event)
        if self._current_snap_state is not None:
            self.alignmentRequested.emit()
            
    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Финальная доводка магнита при отпускании кнопки мыши внутри клиентской зоны."""
        super().mouseReleaseEvent(event)
        self._maybe_snap_to_main()
        
    def showEvent(self, event: QShowEvent) -> None:
        """Трансляция события появления для синхронизации кнопок в ModeBar."""
        super().showEvent(event)
        self.visibilityChanged.emit(True)
        
    def hideEvent(self, event: QHideEvent) -> None:
        """Трансляция события скрытия для синхронизации кнопок в ModeBar."""
        super().hideEvent(event)
        self.visibilityChanged.emit(False)