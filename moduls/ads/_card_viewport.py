"""
Модуль: moduls/ads/_card_viewport.py
Назначение: Движок виртуальной карусели (Viewport Recycler) и менеджер выделения.
Зона ответственности: Высокопроизводительный рендеринг списка профилей (до 10000+ элементов)
                      с использованием динамического пула физических виджетов (O(1) по памяти).
                      Реализует механизм Sweep Selection (Свайп-выделение) с автоскроллом,
                      мгновенным пробросом состояний (State Propagation) и Hit-Testing'ом.
                      Оснащен движком плавного скольжения (Smooth Scroll Engine) и
                      локальным ядром Drag-and-Drop (DND) с магнитной доводкой (Landing Snap),
                      пересчетом номеров в полете (In-Flight Reindexing) и Транзакционным Замком.
Интеграция: Слой GUI. Импортирует `ProfileRowCard` из `_card_row.py`.
            Выступает фундаментом для `AdsProfilePanel`. Общается с презентером
            через сигналы `actionRequested`, `selectionChanged` и `rowDropped`.
"""

import bisect
import math
from typing import Any

from PySide6.QtCore import (
    Qt, Signal, QEvent, QObject, QTimer, QRect, QPoint,
    QPropertyAnimation, QEasingCurve, QAbstractAnimation
)
from PySide6.QtGui import QResizeEvent, QMouseEvent, QCursor
from PySide6.QtWidgets import QAbstractScrollArea, QWidget, QApplication

# Строгие абсолютные импорты ядра
from system.logger import logger
from core._constants import ProfileState
from core.style import SmoothScrollBar, SmoothScrollDelegate

# Импорт физического контейнера строки
from moduls.ads._card_row import ProfileRowCard


class RecyclerScrollArea(QAbstractScrollArea):
    """
    Высокопроизводительный менеджер списков (Virtual Scroll).
    Держит в памяти динамический пул карточек и переиспользует их при скроллинге.
    Реализует механизм Sweep Selection (Свайп-выделение) с автоскроллом,
    кинетическую плавную прокрутку (Smooth Scroll) и магнитный Drag-and-Drop
    с защитой от коллизий через Identity Map и Transaction Lock.
    """
    
    # Сигнал пробрасывается наверх от карточек (mode, flat_idx)
    actionRequested = Signal(str, int)
    # Сигнал изменения выделения для обновления счетчиков в UI
    selectionChanged = Signal()
    # Сигнал завершения DND (start_flat_idx, target_flat_idx)
    rowDropped = Signal(int, int)
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        
        # Инициализируем переменные состояния СТРОГО до вызова методов Qt,
        # которые могут спровоцировать раннюю рассылку событий.
        self.canvas: QWidget | None = None
        self._flat_model: list[dict[str, Any]] = []
        self._selected_ids: set[str] = set()
        self._y_offsets: list[int] = []
        self._total_height: int = 0
        
        # Транзакционный замок (Transaction Guard)
        # Блокирует паразитные клики и жесты во время перестроения модели или DND
        self._is_transacting: bool = False
        
        # Карта соответствия (Identity Map) для защиты от коллизий при Recycling
        self._active_id_to_card_map: dict[str, ProfileRowCard] = {}
        
        # Состояние свайп-выделения (Sweep Selection)
        self._sweep_start_idx: int = -1
        self._is_sweeping: bool = False
        self._paint_mode: bool = True
        self._selection_snapshot: set[str] = set()
        self._last_mouse_y_abs: float = 0.0
        
        # Состояние Drag-and-Drop (DND Engine)
        self._dnd_active_card: ProfileRowCard | None = None
        self._dnd_orig_idx: int = -1
        self._dnd_hovered_idx: int = -1
        self._dnd_y_min: int = 0
        self._dnd_y_max: int = 0
        self._dnd_mouse_offset: float = 0.0
        self._dnd_group_start: int = -1
        self._dnd_group_end: int = -1
        self._dnd_anims: dict[int, QPropertyAnimation] = {}
        self._dnd_is_landing: bool = False
        self._landing_anim: QPropertyAnimation | None = None
        
        # Автоскролл при перетаскивании (Общий для Sweep и DND)
        self._auto_scroll_timer = QTimer(self)
        self._auto_scroll_timer.timeout.connect(self._on_auto_scroll_tick)
        self._auto_scroll_speed: int = 0
        
        # Динамический пул виджетов
        self._pool: list[ProfileRowCard] = []
        
        self.setObjectName("RecyclerScrollArea")
        self.setStyleSheet("background: transparent; border: none;")
        
        # Gutter Isolation: Принудительно резервируем место под скроллбар
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        
        # --- Интеграция Smooth Scroll Engine ---
        self.v_scrollbar = SmoothScrollBar(self)
        self.v_scrollbar.setOrientation(Qt.Orientation.Vertical)
        self.setVerticalScrollBar(self.v_scrollbar)
        self.v_delegate = SmoothScrollDelegate(self.v_scrollbar, Qt.Orientation.Vertical, self)
        self.viewport().installEventFilter(self.v_delegate)
        
        # Холст, на котором будут позиционироваться карточки
        self.canvas = QWidget(self.viewport())
        self.canvas.setStyleSheet("background: transparent;")
        self.canvas.installEventFilter(self)
        
        self.verticalScrollBar().valueChanged.connect(self._on_scroll)
    
    # ===================== DYNAMIC POOL ENGINE =====================
    
    def _ensure_pool_size(self) -> None:
        """
        Динамическое расширение пула карточек под размер экрана.
        Предотвращает нехватку виджетов на 4K-мониторах и экономит ОЗУ на маленьких окнах.
        """
        viewport_height = self.viewport().height()
        if viewport_height <= 0:
            return
        
        # Минимальная высота строки - 45px (группа). Берем с запасом +10 карточек для плавности.
        needed_size = math.ceil(viewport_height / 45.0) + 10
        
        if len(self._pool) < needed_size:
            old_size = len(self._pool)
            for _ in range(needed_size - old_size):
                card = ProfileRowCard(self.canvas)
                card.hide()
                # Инициализируем атрибут для Identity Map
                setattr(card, '_map_key', None)
                card.actionRequested.connect(self.actionRequested.emit)
                # Подключаем триггер старта DND
                card.dragStarted.connect(self._on_card_drag_started)
                self._pool.append(card)
            
            logger.info(
                f"[Viewport] Экран растянулся. Расширяем пул карточек: {old_size} -> {needed_size}. ОЗУ под контролем.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
    
    # ===================== DATA BINDING =====================
    
    def set_model(self, flat_model: list[dict[str, Any]], selected_ids: set[str]) -> None:
        """
        Загрузка новой плоской модели и пересчет геометрии холста.
        Выполняется за O(N), где N - общее количество профилей.
        Снимает транзакционный замок после полной перерисовки.
        """
        self._flat_model = flat_model
        self._selected_ids = selected_ids
        
        self._y_offsets = []
        current_y = 0
        
        for dto in self._flat_model:
            self._y_offsets.append(current_y)
            if dto.get("is_group"):
                current_y += 45
            elif dto.get("is_last_in_group"):
                current_y += 58  # 52px + 6px extra margin
            else:
                current_y += 52
        
        self._total_height = current_y
        self.canvas.setFixedSize(self.viewport().width(), self._total_height)
        
        self._update_scrollbar_geometry()
        self._on_scroll(self.verticalScrollBar().value())
        
        # Транзакция завершена, снимаем замок. Вьюпорт снова готов принимать жесты.
        self._is_transacting = False
    
    def _update_scrollbar_geometry(self) -> None:
        """Обновление лимитов скроллбара на основе общей высоты холста."""
        if not self._flat_model:
            self.verticalScrollBar().setRange(0, 0)
            return
        
        viewport_height = self.viewport().height()
        scroll_max_range = max(0, self._total_height - viewport_height)
        
        self.verticalScrollBar().setRange(0, scroll_max_range)
        self.verticalScrollBar().setPageStep(viewport_height)
        self.verticalScrollBar().setSingleStep(52)
    
    # ===================== STATE PROPAGATION =====================
    
    def update_item_status(self, flat_idx: int, state: ProfileState, tooltip: str = "") -> None:
        """Точечное обновление состояния профиля в модели и на экране (O(1))."""
        if 0 <= flat_idx < len(self._flat_model):
            self._flat_model[flat_idx]["state"] = state
            self._flat_model[flat_idx]["status_tooltip"] = tooltip
            
            for card in self._pool:
                if card.isVisible() and card.logical_idx == flat_idx:
                    card.update_status(state, tooltip)
                    break
    
    def update_item_proxy(self, flat_idx: int, ip: str, country: str, latency: int = -1) -> None:
        """Точечное обновление ГЕО-данных прокси и пинга на экране (O(1))."""
        if 0 <= flat_idx < len(self._flat_model):
            self._flat_model[flat_idx]["ip"] = ip
            self._flat_model[flat_idx]["ip_country"] = country
            self._flat_model[flat_idx]["latency"] = latency
            
            for card in self._pool:
                if card.isVisible() and card.logical_idx == flat_idx:
                    card.update_proxy_data(ip, country, latency)
                    break
    
    def refresh_visible_selection_states(self, instant: bool = False) -> None:
        """
        O(M) State Propagation Pipeline.
        Мгновенно обновляет визуальное состояние выделения только у видимых карточек.
        """
        for card in self._pool:
            if card.isVisible() and card.logical_idx != -1:
                dto = self._flat_model[card.logical_idx]
                uid = dto.get("user_id")
                is_selected = uid in self._selected_ids
                if card._is_selected != is_selected:
                    card._is_selected = is_selected
                    # Если instant=True, анимация левитации отключается для экономии CPU
                    card._update_state(instant=instant)
    
    # ===================== DRAG AND DROP ENGINE =====================
    
    def _on_card_drag_started(self, flat_idx: int) -> None:
        """
        Инициализация локального Drag-and-Drop.
        Вызывается по сигналу от DragHandleCell.
        """
        # Gesture Gate: Блокируем старт, если система в транзакции или сажает плиту
        if self._is_transacting or self._dnd_is_landing:
            return
            
        # Включаем транзакционный замок
        self._is_transacting = True
        
        card = next((c for c in self._pool if c.isVisible() and c.logical_idx == flat_idx), None)
        if not card:
            self._is_transacting = False
            return
            
        self._dnd_active_card = card
        self._dnd_orig_idx = flat_idx
        self._dnd_hovered_idx = flat_idx
        
        # 1. Вычисляем границы дозволенного (Броня папки)
        group_name = self._flat_model[flat_idx].get("group_name")
        
        self._dnd_group_start = flat_idx
        while self._dnd_group_start > 0:
            prev = self._flat_model[self._dnd_group_start - 1]
            if prev.get("is_group") or prev.get("group_name") != group_name:
                break
            self._dnd_group_start -= 1
            
        self._dnd_group_end = flat_idx
        while self._dnd_group_end < len(self._flat_model) - 1:
            nxt = self._flat_model[self._dnd_group_end + 1]
            if nxt.get("is_group") or nxt.get("group_name") != group_name:
                break
            self._dnd_group_end += 1
            
        self._dnd_y_min = self._y_offsets[self._dnd_group_start]
        self._dnd_y_max = self._y_offsets[self._dnd_group_end]
        
        # 2. Вычисляем смещение мыши относительно левого верхнего угла карточки
        global_pos = QCursor.pos()
        card_pos = card.mapFromGlobal(global_pos)
        self._dnd_mouse_offset = card_pos.y()
        
        # 3. Визуализация и захват
        card.set_levitation_state(True)
        self.canvas.grabMouse()
        
        # Отключаем плавность скролла на время драга (Instant Mode Guard)
        if isinstance(self.verticalScrollBar(), SmoothScrollBar):
            self.verticalScrollBar().set_smooth_mode(False)

    def _process_dnd_step(self, y_abs: float) -> None:
        """Обработка одного кадра перетаскивания карточки."""
        if not self._dnd_active_card or self._dnd_is_landing:
            return
            
        # 1. Зажимаем карточку в тисках группы (Clamping)
        target_y = y_abs - self._dnd_mouse_offset
        target_y = max(self._dnd_y_min, min(target_y, self._dnd_y_max))
        
        geo = self._dnd_active_card.geometry()
        geo.moveTop(int(target_y))
        self._dnd_active_card.setGeometry(geo)
        
        # 2. Hit-Testing: над каким слотом мы висим? (26px - половина высоты карточки)
        hovered_idx = self._get_idx_at_y(target_y + 26)
        hovered_idx = max(self._dnd_group_start, min(hovered_idx, self._dnd_group_end))
        
        # 3. In-Flight Reindexing: Обновляем номер парящей карточки
        hovered_num = self._flat_model[hovered_idx].get("display_num", 0)
        self._dnd_active_card.update_display_num(hovered_num)
        
        # 4. Если слот сменился — запускаем анимацию разъезжания
        if hovered_idx != self._dnd_hovered_idx:
            self._dnd_hovered_idx = hovered_idx
            self._animate_displacement()

    def _animate_displacement(self) -> None:
        """
        Матрица виртуальных смещений.
        Плавно раздвигает соседние карточки, уступая место левитирующему профилю.
        """
        for card in self._pool:
            if not card.isVisible() or card == self._dnd_active_card:
                continue
                
            idx = card.logical_idx
            if idx < self._dnd_group_start or idx > self._dnd_group_end:
                continue
                
            # Вычисляем виртуальный индекс
            virtual_idx = idx
            if self._dnd_orig_idx < self._dnd_hovered_idx:
                if self._dnd_orig_idx < idx <= self._dnd_hovered_idx:
                    virtual_idx = idx - 1
            elif self._dnd_orig_idx > self._dnd_hovered_idx:
                if self._dnd_hovered_idx <= idx < self._dnd_orig_idx:
                    virtual_idx = idx + 1
                    
            target_y = self._y_offsets[virtual_idx]
            
            # In-Flight Reindexing: Обновляем номер соседней карточки
            virtual_num = self._flat_model[virtual_idx].get("display_num", 0)
            card.update_display_num(virtual_num)
            
            # Если анимация уже идет к этой цели — не трогаем
            if idx in self._dnd_anims:
                anim = self._dnd_anims[idx]
                if anim.endValue().y() == target_y:
                    continue
                anim.stop()
                
            # Запускаем новую анимацию смещения
            anim = QPropertyAnimation(card, b"pos")
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.setDuration(200)
            anim.setStartValue(card.pos())
            anim.setEndValue(QPoint(card.pos().x(), target_y))
            anim.start()
            self._dnd_anims[idx] = anim

    # ===================== SWEEP SELECTION ENGINE =====================
    
    def clear_selection(self) -> None:
        """Полный сброс выделения."""
        self._selected_ids.clear()
        self._sweep_start_idx = -1
        self._selection_snapshot.clear()
        self.selectionChanged.emit()
        self.refresh_visible_selection_states(instant=True)
    
    def _get_idx_at_y(self, y_abs: float) -> int:
        """Бинарный поиск индекса строки по Y-координате холста (O(log N))."""
        if not self._y_offsets or not self._flat_model:
            return -1
        idx = bisect.bisect_right(self._y_offsets, y_abs) - 1
        return max(0, min(idx, len(self._flat_model) - 1))
    
    def _apply_range_selection(self, start_idx: int, end_idx: int, paint_mode: bool) -> None:
        """Применение выделения к диапазону строк."""
        start = min(start_idx, end_idx)
        end = max(start_idx, end_idx)
        for i in range(start, end + 1):
            dto = self._flat_model[i]
            if not dto.get("is_group"):
                uid = dto.get("user_id")
                if uid:
                    if paint_mode:
                        self._selected_ids.add(uid)
                    else:
                        self._selected_ids.discard(uid)
    
    # ===================== EVENT ROUTING =====================
    
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Глобальный перехватчик событий мыши для холста."""
        if self.canvas is None:
            return super().eventFilter(obj, event)
        
        if obj == self.canvas:
            if event.type() == QEvent.Type.MouseButtonPress:
                return self._handle_mouse_press(event)  # type: ignore
            elif event.type() == QEvent.Type.MouseMove:
                return self._handle_mouse_move(event)  # type: ignore
            elif event.type() == QEvent.Type.MouseButtonRelease:
                return self._handle_mouse_release(event)  # type: ignore
        return super().eventFilter(obj, event)
    
    def _handle_mouse_press(self, event: QMouseEvent) -> bool:
        if event.button() != Qt.MouseButton.LeftButton:
            return False
            
        # Gesture Gate: Жестко блокируем клики, если система в транзакции или сажает плиту
        if self._is_transacting or self._dnd_active_card or self._dnd_is_landing:
            return True
        
        pos = event.position().toPoint()
        child = self.canvas.childAt(pos)
        
        y_abs = event.position().y()
        idx = self._get_idx_at_y(y_abs)
        
        # Если кликнули мимо карточек (по пустому холсту)
        if child is None or child == self.canvas:
            self.clear_selection()
            if idx != -1 and not self._flat_model[idx].get("is_group"):
                self._sweep_start_idx = idx
                self._selection_snapshot = set()
                self._last_mouse_y_abs = y_abs
                self._paint_mode = True
            else:
                self._sweep_start_idx = -1
            return True
        
        # Если кликнули по группе
        if idx == -1 or self._flat_model[idx].get("is_group"):
            self.clear_selection()
            return True
        
        uid = self._flat_model[idx].get("user_id")
        if not uid:
            return False
        
        mods = QApplication.keyboardModifiers()
        
        # Shift-клик (выделение диапазона)
        if mods & Qt.KeyboardModifier.ShiftModifier:
            if self._sweep_start_idx != -1:
                self._apply_range_selection(self._sweep_start_idx, idx, True)
                self.selectionChanged.emit()
                self.refresh_visible_selection_states(instant=False)
            return True
        
        # Ctrl-клик (точечное переключение)
        if mods & Qt.KeyboardModifier.ControlModifier:
            self._paint_mode = uid not in self._selected_ids
        else:
            self._selected_ids.clear()
            self._paint_mode = True
        
        self._sweep_start_idx = idx
        self._selection_snapshot = self._selected_ids.copy()
        self._last_mouse_y_abs = y_abs
        
        if self._paint_mode:
            self._selected_ids.add(uid)
        else:
            self._selected_ids.discard(uid)
        
        self.selectionChanged.emit()
        self.refresh_visible_selection_states(instant=False)
        return True
    
    def _handle_mouse_move(self, event: QMouseEvent) -> bool:
        y_abs = event.position().y()
        
        # 1. Маршрутизация в DND Engine
        if self._dnd_active_card:
            self._process_dnd_step(y_abs)
            self._check_auto_scroll(y_abs)
            return True
            
        # 2. Маршрутизация в Sweep Selection Engine
        if self._sweep_start_idx != -1:
            if not self._is_sweeping:
                if abs(y_abs - self._last_mouse_y_abs) > QApplication.startDragDistance():
                    self._is_sweeping = True
                    self.canvas.grabMouse()
                    if isinstance(self.verticalScrollBar(), SmoothScrollBar):
                        self.verticalScrollBar().set_smooth_mode(False)
                else:
                    return True
                    
            self._last_mouse_y_abs = y_abs
            self._process_sweep_step()
            self._check_auto_scroll(y_abs)
            return True
            
        return False
        
    def _process_sweep_step(self) -> None:
        """Обработка одного шага свайп-выделения."""
        current_idx = self._get_idx_at_y(self._last_mouse_y_abs)
        
        if current_idx != -1:
            self._selected_ids.clear()
            self._selected_ids.update(self._selection_snapshot)
            self._apply_range_selection(self._sweep_start_idx, current_idx, self._paint_mode)
            
            # Sweep Gate: При массовом выделении отключаем анимации для экономии CPU
            self.refresh_visible_selection_states(instant=True)
            self.selectionChanged.emit()

    def _check_auto_scroll(self, y_abs: float) -> None:
        """Математика автоскролла у краев вьюпорта (Общая для DND и Sweep)."""
        y_viewport = y_abs - self.verticalScrollBar().value()
        viewport_height = self.viewport().height()
        margin = 35
        
        if y_viewport < margin:
            self._auto_scroll_speed = int((y_viewport - margin) / 2)
            if not self._auto_scroll_timer.isActive():
                self._auto_scroll_timer.start(20)
        elif y_viewport > viewport_height - margin:
            self._auto_scroll_speed = int((y_viewport - (viewport_height - margin)) / 2)
            if not self._auto_scroll_timer.isActive():
                self._auto_scroll_timer.start(20)
        else:
            self._auto_scroll_timer.stop()
    
    def _on_auto_scroll_tick(self) -> None:
        """Тик таймера автоскролла."""
        sb = self.verticalScrollBar()
        # Поскольку smooth_mode отключен, setValue сработает мгновенно
        sb.setValue(sb.value() + self._auto_scroll_speed)
        
        global_pos = QCursor.pos()
        canvas_pos = self.canvas.mapFromGlobal(global_pos)
        y_abs = canvas_pos.y()
        
        if self._dnd_active_card:
            self._process_dnd_step(y_abs)
        elif self._is_sweeping:
            self._last_mouse_y_abs = y_abs
            self._process_sweep_step()
    
    def _handle_mouse_release(self, event: QMouseEvent) -> bool:
        if event.button() == Qt.MouseButton.LeftButton:
            
            # 1. Завершение DND (Landing Snap Pipeline)
            if self._dnd_active_card and not self._dnd_is_landing:
                self.canvas.releaseMouse()
                self._dnd_is_landing = True
                self._auto_scroll_timer.stop()
                
                # Вычисляем идеальное гнездо для приземления
                target_y = self._y_offsets[self._dnd_hovered_idx]
                
                # Запускаем доводку
                self._landing_anim = QPropertyAnimation(self._dnd_active_card, b"pos")
                self._landing_anim.setEndValue(QPoint(self._dnd_active_card.pos().x(), target_y))
                self._landing_anim.setDuration(120)
                self._landing_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
                
                def on_landing_finished() -> None:
                    if self._dnd_active_card:
                        self._dnd_active_card.set_levitation_state(False)
                    self._dnd_is_landing = False
                    
                    # Транзакция: стреляем сигналом бухгалтеру.
                    # ВНИМАНИЕ: _is_transacting остается True, пока презентер не вызовет set_model!
                    self.rowDropped.emit(self._dnd_orig_idx, self._dnd_hovered_idx)
                    
                    # Очистка состояния
                    self._dnd_active_card = None
                    self._dnd_anims.clear()
                    self._landing_anim = None
                    
                    if isinstance(self.verticalScrollBar(), SmoothScrollBar):
                        self.verticalScrollBar().set_smooth_mode(True)
                        
                self._landing_anim.finished.connect(on_landing_finished)
                self._landing_anim.start()
                return True
                
            # 2. Завершение Sweep Selection
            if self._sweep_start_idx != -1:
                if self._is_sweeping:
                    self.canvas.releaseMouse()
                    self._is_sweeping = False
                    self._auto_scroll_timer.stop()
                    
                    if isinstance(self.verticalScrollBar(), SmoothScrollBar):
                        self.verticalScrollBar().set_smooth_mode(True)
                
                self.selectionChanged.emit()
                self._sweep_start_idx = -1
                return True
                
        return False
    
    # ===================== RECYCLING ENGINE (IDENTITY MAP) =====================
    
    def _on_scroll(self, scroll_y: int) -> None:
        """
        Ядро виртуализации с поддержкой Identity Map.
        Сдвигает холст и перераспределяет виджеты из пула на видимые позиции.
        Гарантирует, что одна карточка не будет привязана к двум профилям одновременно.
        """
        self.canvas.move(0, -scroll_y)
        
        if not self._flat_model or not self._y_offsets:
            for card in self._pool:
                card.hide()
            self._active_id_to_card_map.clear()
            return
        
        # Бинарный поиск видимого диапазона
        start_idx = bisect.bisect_right(self._y_offsets, scroll_y) - 1
        start_idx = max(0, start_idx - 2)  # Захватываем пару карточек сверху для плавности
        
        viewport_bottom = scroll_y + self.viewport().height()
        end_idx = start_idx
        while end_idx < len(self._flat_model):
            if self._y_offsets[end_idx] > viewport_bottom + 100:
                break
            end_idx += 1
        
        needed_indices = set(range(start_idx, end_idx))
        
        # 1. Скрываем и стерилизуем карточки, ушедшие за пределы экрана
        for card in self._pool:
            if card.isVisible() and card.logical_idx not in needed_indices:
                # RECYCLING IMMUNITY: Не трогаем карточку, которую юзер держит в руках
                if card == self._dnd_active_card:
                    continue
                    
                # Жесткая зачистка C++ анимаций и геометрии
                card.reset_visuals()
                card.hide()
                
                # Вычеркиваем из карты живых (Identity Map)
                map_key = getattr(card, '_map_key', None)
                if map_key and map_key in self._active_id_to_card_map:
                    del self._active_id_to_card_map[map_key]
                
                setattr(card, '_map_key', None)
                card.logical_idx = -1
                card.user_id = ""
        
        # 2. Показываем и гидратируем новые карточки (с учетом Identity Map)
        for logical_idx in range(start_idx, end_idx):
            dto = self._flat_model[logical_idx]
            uid = dto.get("user_id", "")
            is_group = dto.get("is_group", False)
            
            # Для групп используем имя как уникальный ключ, для профилей - user_id
            map_key = f"group_{dto.get('group_name', '')}" if is_group else uid
            
            # Вычисляем виртуальный Y, если активен DND
            virtual_idx = logical_idx
            if self._dnd_active_card:
                if self._dnd_orig_idx < self._dnd_hovered_idx:
                    if self._dnd_orig_idx < logical_idx <= self._dnd_hovered_idx:
                        virtual_idx = logical_idx - 1
                elif self._dnd_orig_idx > self._dnd_hovered_idx:
                    if self._dnd_hovered_idx <= logical_idx < self._dnd_orig_idx:
                        virtual_idx = logical_idx + 1
                        
            target_y = self._y_offsets[virtual_idx]
            
            if is_group:
                h = 45
            elif dto.get("is_last_in_group"):
                h = 58
            else:
                h = 52
                
            target_rect = QRect(0, target_y, self.canvas.width() - 15, h)
            
            if map_key and map_key in self._active_id_to_card_map:
                # Плита уже на холсте. Просто обновляем ей индекс и Y-координату.
                existing_card = self._active_id_to_card_map[map_key]
                existing_card.logical_idx = logical_idx
                
                # Если плита не анимируется DND смещением, жестко ставим Y
                if logical_idx not in self._dnd_anims and existing_card != self._dnd_active_card:
                    existing_card.setGeometry(target_rect)
                    
                # In-Flight Reindexing
                if self._dnd_active_card and not is_group:
                    virtual_num = self._flat_model[virtual_idx].get("display_num", 0)
                    existing_card.update_display_num(virtual_num)
            else:
                # Плиты нет на экране. Берем свободную из пула (гарантированно стерильную)
                free_card = next((c for c in self._pool if not c.isVisible() and getattr(c, '_map_key', None) is None), None)
                
                if free_card:
                    free_card.setGeometry(target_rect)
                    free_card.show()
                    
                    is_selected = uid in self._selected_ids if not is_group else False
                    
                    # Гидратация (внутри вызывается _update_state(instant=True) для сброса артефактов)
                    free_card.hydrate(dto, is_selected)
                    
                    if map_key:
                        setattr(free_card, '_map_key', map_key)
                        self._active_id_to_card_map[map_key] = free_card
                        
                    # In-Flight Reindexing
                    if self._dnd_active_card and not is_group:
                        virtual_num = self._flat_model[virtual_idx].get("display_num", 0)
                        free_card.update_display_num(virtual_num)
                else:
                    # Сюда мы попадать не должны благодаря _ensure_pool_size
                    logger.warning(
                        "[Viewport] Пул карточек исчерпан! Возможны визуальные артефакты.",
                        profile_names=["GLOBAL"], category="SYSTEM"
                    )
    
    def resizeEvent(self, event: QResizeEvent) -> None:
        """Обработка изменения размеров окна. Триггерит расширение пула."""
        super().resizeEvent(event)
        
        # Проверяем и расширяем пул при необходимости
        self._ensure_pool_size()
        
        new_width = self.viewport().width()
        self.canvas.setFixedWidth(new_width)
        
        # Растягиваем видимые карточки
        for card in self._pool:
            if card.isVisible():
                geo = card.geometry()
                geo.setWidth(new_width - 15)
                card.setGeometry(geo)
        
        self._update_scrollbar_geometry()
        self._on_scroll(self.verticalScrollBar().value())