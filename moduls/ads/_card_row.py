"""
Модуль: moduls/ads/_card_row.py
Назначение: Изолированный контейнер строки профиля (Presentation Layer).
Зона ответственности: Физическая карточка профиля (`ProfileRowCard`) и её визуальная
                      оболочка (`ProfileCardWrapper`). Управляет сборкой ячеек из
                      `ROW_CELL_PIPELINE`, физикой 3D-левитации (Z-Layer Elevation),
                      аппаратными тенями и маршрутизацией сигналов от кнопок наверх.
                      Реализует Двухконтурный движок анимаций (Double-Pipeline Animation Engine)
                      с динамической интерполяцией отката (Dynamic Rollback) и
                      умной блокировкой гидратации (Smart Hydration Lock) для свайп-выделения.
                      Включает поддержку ручной левитации и In-Flight Reindexing для DND.
Интеграция: Слой GUI. Импортирует ячейки из `_card_cells.py`. Инстанцируется пулом
            внутри `RecyclerScrollArea` (в `_card_viewport.py`). Не содержит
            бизнес-логики, общается с внешним миром через сигналы.
"""

from typing import Any

from PySide6.QtCore import (
    Qt, Signal, QRect, QEvent, QPropertyAnimation,
    QParallelAnimationGroup, QEasingCurve, QAbstractAnimation,
    QVariantAnimation
)
from PySide6.QtGui import (
    QResizeEvent, QPainter, QColor, QPaintEvent, QLinearGradient
)
from PySide6.QtWidgets import (
    QFrame, QWidget, QHBoxLayout, QLabel, QGraphicsDropShadowEffect
)

# Строгие абсолютные импорты ядра
from core._constants import ProfileState
from core.style import Colors

# Импорты из соседнего цеха ячеек
from moduls.ads._card_cells import (
    ROW_CELL_PIPELINE, ProxyCell, LatencyCell, BaseCardCell, OrdinalNumberCell
)


# =============================================================================
# 1. ВИЗУАЛЬНОЕ ТЕЛО КАРТОЧКИ (THE FROSTED GLASS WRAPPER)
# =============================================================================

class ProfileCardWrapper(QFrame):
    """
    Внутреннее визуальное тело карточки.
    В состоянии покоя абсолютно прозрачно (Zero-CPU Idle State).
    Управляется через float-множитель hover_opacity (Оптическая магистраль).
    """
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ProfileCardWrapper")
        
        # КРИТИЧНО: Разрешаем кастомному QFrame транслировать QSS-стили
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        
        # Оптический множитель прозрачности (от 0.0 до 1.0)
        self.hover_opacity: float = 0.0
        
        # Кэширование базовых цветов градиента для предотвращения аллокаций в paintEvent
        self._color_start = QColor(getattr(Colors, "GLASS_CARD_TINT_START", "#23FFFFFF"))
        self._color_end = QColor(getattr(Colors, "GLASS_CARD_TINT_END", "#0AFFFFFF"))
    
    def paintEvent(self, event: QPaintEvent) -> None:
        """
        Аппаратный рендеринг матового стекла без рамок.
        Resource Guard: Если карточка невидима (opacity ~ 0), метод мгновенно завершается.
        """
        if self.hover_opacity <= 0.001:
            return
        
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Отступ в 1px предотвращает обрезку градиента границами виджета
            rect = self.rect().adjusted(1, 1, -1, -1)
            
            # Динамический расчет прозрачности на основе множителя
            c_start = QColor(self._color_start)
            c_start.setAlpha(int(c_start.alpha() * self.hover_opacity))
            
            c_end = QColor(self._color_end)
            c_end.setAlpha(int(c_end.alpha() * self.hover_opacity))
            
            # Диагональный градиент (имитация блика на стекле)
            grad = QLinearGradient(0, 0, rect.width(), rect.height())
            grad.setColorAt(0.0, c_start)
            grad.setColorAt(1.0, c_end)
            
            # Строго без рамок (border-free)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(grad)
            
            painter.drawRoundedRect(rect, 8, 8)
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            painter.end()


# =============================================================================
# 2. ФИЗИЧЕСКАЯ КАРТОЧКА ПРОФИЛЯ (THE ROW CARD)
# =============================================================================

class ProfileRowCard(QFrame):
    """
    Интерактивная горизонтальная плита профиля (Gutter Bounding Box).
    Служит прозрачным контейнером фиксированного размера (52px).
    Реализует Двухконтурный движок анимаций:
    - Оптический контур (Hover): Плавное проявление стекла без изменения Layout.
    - Геометрический контур (Selection/Drag): Левитация с динамической интерполяцией отката.
    Оснащена Smart Hydration Lock для защиты от рывков при свайп-выделении.
    """
    
    # Сигналы пробрасываются наверх в RecyclerScrollArea
    actionRequested = Signal(str, int)  # mode, flat_idx
    dragStarted = Signal(int)           # flat_idx
    dragEnded = Signal(int)             # flat_idx
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ProfileCard")
        
        # КРИТИЧНО: Разрешаем кастомному QFrame транслировать QSS-стили
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        
        self.logical_idx: int = -1
        self.user_id: str = ""
        self.is_group: bool = False
        self.is_last_in_group: bool = False
        
        self._is_hovered: bool = False
        self._is_selected: bool = False
        self._is_dragging: bool = False
        
        # Smart Hydration Lock: Флаг "только что из пула"
        self._just_recycled: bool = False
        
        # Базовая геометрия для анимаций (Zero-Reflow)
        self._base_rect = QRect(0, 0, 0, 0)
        self._active_rect = QRect(0, 0, 0, 0)
        
        # Хранилище инстансов ячеек
        self._cells: list[BaseCardCell] = []
        
        self._setup_ui()
        self._setup_animations()
    
    def _setup_ui(self) -> None:
        """Сборка внутреннего макета карточки."""
        # --- РЕЖИМ ГРУППЫ (Скрыт по умолчанию) ---
        self.group_container = QWidget(self)
        self.group_container.setStyleSheet("background: transparent;")
        group_lay = QHBoxLayout(self.group_container)
        group_lay.setContentsMargins(12, 0, 0, 0)
        
        self.lbl_group_name = QLabel()
        self.lbl_group_name.setStyleSheet(f"color: {Colors.TXT_PRIMARY}; font-size: 13px; font-weight: bold;")
        group_lay.addWidget(self.lbl_group_name)
        group_lay.addStretch(1)
        self.group_container.hide()
        
        # --- РЕЖИМ ПРОФИЛЯ (Левитирующее стеклянное тело) ---
        self.content_wrapper = ProfileCardWrapper(self)
        
        # Тень для эффекта парения (автоматически исчезает, если content_wrapper прозрачен)
        self.shadow = QGraphicsDropShadowEffect(self)
        self.shadow.setBlurRadius(6)
        self.shadow.setOffset(0, 2)
        shadow_color = QColor(getattr(Colors, "GLASS_CARD_SHADOW", "#64000000"))
        self.shadow.setColor(shadow_color)
        self.content_wrapper.setGraphicsEffect(self.shadow)
        
        wrapper_lay = QHBoxLayout(self.content_wrapper)
        wrapper_lay.setContentsMargins(12, 0, 12, 0)
        wrapper_lay.setSpacing(0)
        
        # ДИНАМИЧЕСКАЯ СБОРКА ЯЧЕЕК (The Assembly Line)
        for CellClass in ROW_CELL_PIPELINE:
            cell = CellClass(parent=self.content_wrapper)
            
            # Проброс сигналов кнопок управления
            if hasattr(cell, 'cellActionRequested'):
                cell.cellActionRequested.connect(
                    lambda mode: self.actionRequested.emit(mode, self.logical_idx)
                )
            
            # Проброс сигналов локального Drag-and-Drop движка
            if hasattr(cell, 'dragInitiated'):
                cell.dragInitiated.connect(lambda: self.dragStarted.emit(self.logical_idx))
            if hasattr(cell, 'dragReleased'):
                cell.dragReleased.connect(lambda: self.dragEnded.emit(self.logical_idx))
            
            wrapper_lay.addWidget(cell)
            self._cells.append(cell)
    
    def _setup_animations(self) -> None:
        """Инициализация двухконтурного привода анимаций (Hardware Accelerated)."""
        # 1. Геометрическая магистраль (Selection / Levitation)
        self.anim_group = QParallelAnimationGroup(self)
        
        self.anim_geom = QPropertyAnimation(self.content_wrapper, b"geometry")
        self.anim_geom.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim_geom.setDuration(200)
        
        self.anim_blur = QPropertyAnimation(self.shadow, b"blurRadius")
        self.anim_blur.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim_blur.setDuration(200)
        
        self.anim_offset = QPropertyAnimation(self.shadow, b"yOffset")
        self.anim_offset.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.anim_offset.setDuration(200)
        
        self.anim_group.addAnimation(self.anim_geom)
        self.anim_group.addAnimation(self.anim_blur)
        self.anim_group.addAnimation(self.anim_offset)
        
        # 2. Оптическая магистраль (Hover / Glass Fade)
        self.hover_anim = QVariantAnimation(self)
        self.hover_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hover_anim.setDuration(200)
        self.hover_anim.valueChanged.connect(self._on_hover_anim_step)
    
    def _on_hover_anim_step(self, value: Any) -> None:
        """Слот интерполяции прозрачности стекла."""
        self.content_wrapper.hover_opacity = float(value)
        self.content_wrapper.update()
    
    def resizeEvent(self, event: QResizeEvent) -> None:
        """Ручной пересчет геометрии (Zero-Reflow)."""
        super().resizeEvent(event)
        w = self.width()
        
        if self.is_group:
            self.group_container.setGeometry(0, 15, w, 30)
        else:
            # Gutter Isolation: 52px высота строки, 40px тело карточки.
            # Базовые отступы: 10px по бокам, 6px сверху/снизу.
            self._base_rect = QRect(10, 6, w - 20, 40)
            # Активные отступы (Скейлинг): 4px по бокам, 3px сверху/снизу.
            self._active_rect = QRect(4, 3, w - 8, 46)
            
            # Если анимация не идет, жестко применяем геометрию.
            # Если идет — позволяем ей завершиться плавно.
            if self.anim_group.state() != QAbstractAnimation.State.Running:
                is_elevated = self._is_selected or self._is_dragging
                self.content_wrapper.setGeometry(self._active_rect if is_elevated else self._base_rect)
    
    def enterEvent(self, event: QEvent) -> None:
        """Обработка наведения мыши (Запуск оптического контура)."""
        self._is_hovered = True
        self._update_state(instant=False)
        super().enterEvent(event)
    
    def leaveEvent(self, event: QEvent) -> None:
        """Обработка ухода мыши (Откат оптического контура)."""
        self._is_hovered = False
        self._update_state(instant=False)
        super().leaveEvent(event)
        
    def set_levitation_state(self, active: bool) -> None:
        """
        Ручное управление левитацией для движка Drag-and-Drop.
        Позволяет вьюпорту принудительно поднять карточку над остальными.
        """
        if self._is_dragging != active:
            self._is_dragging = active
            self._update_state(instant=False)
    
    def _update_state(self, instant: bool = False) -> None:
        """
        Ядро машины состояний (Double-Pipeline Engine).
        Управляет Z-индексом, прозрачностью стекла и геометрией левитации.
        Реализует Dynamic Rollback для плавного приземления и Smart Hydration Lock.
        """
        if self.is_group:
            return
            
        # --- SMART HYDRATION LOCK ---
        # Защита от рывков при свайп-выделении (Sweep Selection).
        # Если вьюпорт просит instant=True, но карточка уже давно на экране (не из пула),
        # мы ИГНОРИРУЕМ приказ и анимируем плавно.
        actual_instant = instant
        if self._just_recycled:
            # Карточка только что телепортировалась при скролле. Жестко применяем координаты.
            actual_instant = True
            self._just_recycled = False
        else:
            # Карточка уже на экране. Игнорируем панику вьюпорта, делаем красиво.
            actual_instant = False
        
        # 1. Z-Layer Elevation (Выталкивание на передний план)
        if self._is_dragging or self._is_selected:
            self.raise_()
        else:
            self.lower()
        
        # 2. Оптическая магистраль (Hover / Glass Fade)
        target_opacity = 1.0 if (self._is_dragging or self._is_selected or self._is_hovered) else 0.0
        self.hover_anim.stop()
        
        if actual_instant:
            self.content_wrapper.hover_opacity = target_opacity
            self.content_wrapper.update()
        else:
            self.hover_anim.setStartValue(self.content_wrapper.hover_opacity)
            self.hover_anim.setEndValue(target_opacity)
            self.hover_anim.start()
        
        # 3. Геометрическая магистраль (Selection / Drag Levitation)
        if self._is_dragging:
            target_rect = self._active_rect
            target_blur = 24  # Максимальная тень для эффекта "взятия в руку"
            target_offset = 8
        elif self._is_selected:
            target_rect = self._active_rect
            target_blur = 16  # Стандартная тень выделения
            target_offset = 6
        else:
            target_rect = self._base_rect
            target_blur = 6   # Тень покоя
            target_offset = 2
        
        self.anim_group.stop()
        
        if actual_instant:
            # Мгновенное применение геометрии без нагрузки на CPU (при скролле)
            self.content_wrapper.setGeometry(target_rect)
            self.shadow.setBlurRadius(target_blur)
            self.shadow.setYOffset(target_offset)
        else:
            # Dynamic Rollback: StartValue ВСЕГДА берется из фактического текущего состояния!
            # Это обеспечивает бесшовный разворот анимации прямо в воздухе.
            self.anim_geom.setStartValue(self.content_wrapper.geometry())
            self.anim_geom.setEndValue(target_rect)
            
            self.anim_blur.setStartValue(self.shadow.blurRadius())
            self.anim_blur.setEndValue(target_blur)
            
            self.anim_offset.setStartValue(self.shadow.yOffset())
            self.anim_offset.setEndValue(target_offset)
            
            self.anim_group.start()
    
    def reset_visuals(self) -> None:
        """
        Recycling Reset: Полная зачистка состояния при переиспользовании виджета.
        Предотвращает появление "призрачных" анимаций на новых профилях при скролле.
        """
        # Жесткий фикс Memory Leak: останавливаем C++ аниматоры только если они запущены
        if self.anim_group.state() == QAbstractAnimation.State.Running:
            self.anim_group.stop()
        if self.hover_anim.state() == QAbstractAnimation.State.Running:
            self.hover_anim.stop()
        
        self._is_hovered = False
        self._is_selected = False
        self._is_dragging = False
        self._just_recycled = False
        
        # Моментальный сброс альфы и геометрии
        self.content_wrapper.hover_opacity = 0.0
        self.content_wrapper.setGeometry(self._base_rect)
        self.shadow.setBlurRadius(6)
        self.shadow.setYOffset(2)
        self.content_wrapper.update()
        
        # Плита уходит на задний план, чтобы не перекрывать другие при переработке
        self.lower()
        
        # Делегируем очистку всем ячейкам
        for cell in self._cells:
            cell.reset_visuals()
    
    def hydrate(self, dto: dict[str, Any], is_selected: bool) -> None:
        """Мгновенное наполнение карточки данными (O(1))."""
        # Стерилизация: жестко глушим любые остаточные визуальные эффекты перед заливкой новых данных
        self._is_dragging = False
        self._is_selected = is_selected
        self._is_hovered = False
        self._update_state(instant=True)
        
        # Взводим предохранитель гидратации ДО вызова _update_state
        self._just_recycled = True
        
        self.logical_idx = dto.get("flat_idx", -1)
        self.is_group = dto.get("is_group", False)
        self.is_last_in_group = dto.get("is_last_in_group", False)
        
        if self.is_group:
            self.content_wrapper.hide()
            self.group_container.show()
            
            gname = dto.get("group_name", "Без группы")
            count = dto.get("group_count", 0)
            self.lbl_group_name.setText(f"📁 Группа: {gname}  ({count})")
        else:
            self.group_container.hide()
            self.content_wrapper.show()
            
            self.user_id = dto.get("user_id", "")
            
            # Делегируем проливку данных всем ячейкам
            for cell in self._cells:
                cell.hydrate(dto, is_selected)
            
            state = dto.get("state", ProfileState.UNKNOWN)
            tooltip = dto.get("status_tooltip", "")
            self.update_status(state, tooltip)
        
        # Форсируем пересчет геометрии для новой роли (группа/профиль)
        self.resizeEvent(QResizeEvent(self.size(), self.size()))
    
    def update_status(self, state: ProfileState, tooltip: str) -> None:
        """Точечное обновление статуса. Делегируется ячейкам."""
        for cell in self._cells:
            cell.update_status(state, tooltip)
    
    def update_proxy_data(self, ip: str, country: str, latency: int = -1) -> None:
        """Точечное обновление данных прокси и пинга. Делегируется ячейкам ProxyCell и LatencyCell."""
        for cell in self._cells:
            if isinstance(cell, ProxyCell):
                cell.update_proxy(ip, country)
            elif isinstance(cell, LatencyCell):
                cell.update_latency(latency)

    def update_display_num(self, num: int) -> None:
        """
        Точечное (O(1)) обновление порядкового номера (In-Flight Reindexing).
        Позволяет вьюпорту перекрашивать номера слотов прямо во время анимации
        перетаскивания, не вызывая тяжелую перерисовку всей карточки.
        """
        for cell in self._cells:
            if isinstance(cell, OrdinalNumberCell):
                cell.lbl_num.setText(str(num) if num else "")
                break