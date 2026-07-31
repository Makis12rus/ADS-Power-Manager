"""
Модуль: moduls/ads/flow_layout.py
Назначение: Кастомный менеджер компоновки (Flow Layout) для графического интерфейса.
Зона ответственности: Динамический расчет геометрии и автоматический перенос виджетов
                      на новую строку, если они не помещаются по ширине родительского
                      контейнера. Идеально подходит для создания "облака тегов" (чипсов).
                      Реализует паттерн Size Invalidation Pipeline для реактивной высоты.
Интеграция: Слой Presentation (GUI). Абсолютно независимый компонент. Не импортирует
            бизнес-логику, ядро или системные утилиты. Используется в панели настроек
            (AdsSettingsPanel) для рендеринга выбранных профилей и кошельков.
            Является частью плоского пакета `moduls/ads/`.
"""

from PySide6.QtCore import Qt, QRect, QPoint, QSize, Signal
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget, QStyle, QSizePolicy


class FlowLayout(QLayout):
    """
    Менеджер компоновки, размещающий элементы слева направо и переносящий их
    на следующую строку при нехватке горизонтального пространства.
    Оснащен механизмом Size Invalidation для динамического изменения высоты родителя.
    """
    
    # Сигнал эмитируется, когда фактическая высота компоновки изменяется.
    # Используется для устранения вложенных скролл-баров (Nested Scroll Trap).
    heightChanged = Signal(int)
    
    def __init__(
            self,
            parent: QWidget | None = None,
            margin: int = 0,
            hSpacing: int = 0,
            vSpacing: int = 0
    ) -> None:
        """
        Инициализация FlowLayout.

        :param parent: Родительский виджет.
        :param margin: Внешние отступы (со всех сторон).
        :param hSpacing: Горизонтальное расстояние между элементами.
        :param vSpacing: Вертикальное расстояние между строками.
        """
        super().__init__(parent)
        self._hSpace: int = hSpacing
        self._vSpace: int = vSpacing
        self.itemList: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        
        # Кэш последней рассчитанной высоты для защиты от Layout Recursion Loop
        self._last_height: int = -1
    
    def __del__(self) -> None:
        """
        Resource Guard: Гарантированное удаление элементов компоновки из памяти
        при уничтожении объекта, предотвращающее утечки C++ указателей Qt.
        """
        item = self.takeAt(0)
        while item is not None:
            item = self.takeAt(0)
    
    def addItem(self, item: QLayoutItem) -> None:
        """Добавляет элемент в конец списка компоновки."""
        self.itemList.append(item)
    
    def horizontalSpacing(self) -> int:
        """Возвращает горизонтальный отступ между элементами."""
        if self._hSpace >= 0:
            return self._hSpace
        return self.smartSpacing(QStyle.PixelMetric.PM_LayoutHorizontalSpacing)
    
    def verticalSpacing(self) -> int:
        """Возвращает вертикальный отступ между строками."""
        if self._vSpace >= 0:
            return self._vSpace
        return self.smartSpacing(QStyle.PixelMetric.PM_LayoutVerticalSpacing)
    
    def count(self) -> int:
        """Возвращает количество элементов в компоновке."""
        return len(self.itemList)
    
    def itemAt(self, index: int) -> QLayoutItem | None:
        """Возвращает элемент по индексу без его удаления."""
        if 0 <= index < len(self.itemList):
            return self.itemList[index]
        return None
    
    def takeAt(self, index: int) -> QLayoutItem | None:
        """Извлекает и возвращает элемент по индексу, удаляя его из компоновки."""
        if 0 <= index < len(self.itemList):
            return self.itemList.pop(index)
        return None
    
    def expandingDirections(self) -> Qt.Orientation:
        """
        Указывает, в каких направлениях компоновка может расширяться.
        Возвращает 0, так как FlowLayout расширяется только по мере необходимости.
        """
        return Qt.Orientation(0)
    
    def hasHeightForWidth(self) -> bool:
        """Указывает, что высота компоновки зависит от ее ширины."""
        return True
    
    def heightForWidth(self, width: int) -> int:
        """Рассчитывает необходимую высоту для заданной ширины."""
        return self.doLayout(QRect(0, 0, width, 0), True)
    
    def setGeometry(self, rect: QRect) -> None:
        """Применяет геометрию к компоновке и перераспределяет элементы."""
        super().setGeometry(rect)
        self.doLayout(rect, False)
    
    def sizeHint(self) -> QSize:
        """Рекомендуемый размер компоновки."""
        return self.minimumSize()
    
    def minimumSize(self) -> QSize:
        """
        Рассчитывает минимальный размер, необходимый для отображения
        самого большого элемента с учетом отступов.
        """
        size = QSize()
        for item in self.itemList:
            size = size.expandedTo(item.minimumSize())
        
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size
    
    def doLayout(self, rect: QRect, testOnly: bool) -> int:
        """
        Ядро математики FlowLayout. Рассчитывает координаты X и Y для каждого
        элемента, перенося их на новую строку при достижении правой границы rect.

        :param rect: Доступная геометрия для размещения.
        :param testOnly: Если True, элементы не перемещаются (только расчет высоты).
        :return: Итоговая высота, занимаемая всеми элементами.
        """
        x, y = rect.x(), rect.y()
        lineHeight = 0
        
        for item in self.itemList:
            wid = item.widget()
            
            # Расчет горизонтального отступа
            spaceX = self.horizontalSpacing()
            if spaceX == -1 and wid is not None:
                spaceX = wid.style().layoutSpacing(
                    QSizePolicy.ControlType.PushButton,
                    QSizePolicy.ControlType.PushButton,
                    Qt.Orientation.Horizontal
                )
            
            # Расчет вертикального отступа
            spaceY = self.verticalSpacing()
            if spaceY == -1 and wid is not None:
                spaceY = wid.style().layoutSpacing(
                    QSizePolicy.ControlType.PushButton,
                    QSizePolicy.ControlType.PushButton,
                    Qt.Orientation.Vertical
                )
            
            # Проверка на перенос строки
            nextX = x + item.sizeHint().width() + spaceX
            if nextX - spaceX > rect.right() and lineHeight > 0:
                x = rect.x()
                y = y + lineHeight + spaceY
                nextX = x + item.sizeHint().width() + spaceX
                lineHeight = 0
            
            # Фактическое применение геометрии, если это не тестовый прогон
            if not testOnly:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))
            
            x = nextX
            lineHeight = max(lineHeight, item.sizeHint().height())
        
        calculated_height = y + lineHeight - rect.y()
        
        # Size Invalidation Pipeline:
        # Если это реальная отрисовка (не тест) и высота изменилась, уведомляем родителя.
        # Проверка _last_height критически важна для предотвращения бесконечной рекурсии (Layout Loop).
        if not testOnly and calculated_height != self._last_height:
            self._last_height = calculated_height
            self.heightChanged.emit(calculated_height)
            
        return calculated_height
    
    def smartSpacing(self, pm: QStyle.PixelMetric) -> int:
        """
        Интеллектуальный расчет отступов на основе стиля родительского виджета.
        """
        parent = self.parent()
        if parent is None:
            return -1
        elif parent.isWidgetType():
            # Приведение типа для безопасного вызова методов QWidget
            parent_widget = parent  # type: ignore
            return parent_widget.style().pixelMetric(pm, None, parent_widget)
        else:
            # Если родитель — другая компоновка
            parent_layout = parent  # type: ignore
            return parent_layout.spacing()