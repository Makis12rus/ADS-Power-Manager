"""
Модуль: core/_style_widgets.py
Назначение: Изолированный цех кастомных Qt-виджетов (Presentation Utilities).
Зона ответственности: Реализация безопасного поля ввода пароля (SecurePasswordLineEdit),
                      интеллектуального индикатора автосохранения (AutoSaveIndicator),
                      универсальной стеклянной плитки (GlassTile), вдавленных полей
                      (DebossedLineEdit), генератора кэшированных светодиодов (CachedLedPainter),
                      движка плавного скроллинга (Smooth Scroll Engine) и
                      премиальной типографики (EngravedLabel).
                      Оснащен алгоритмами Rubber Band Rendering и Bake and Blit для Zero-CPU ресайза и анимаций.
Интеграция: Зависит от базовых классов PySide6, цветовой палитры (_style_colors.py),
            генератора графики (_style_graphics.py) и машины состояний (_constants.py).
            Реэкспортируется через фасад core/style.py.
            Строго изолирован от бизнес-логики и системного ядра.
"""

import math
from typing import Any

from PySide6.QtCore import (
    Qt, QEvent, QTimer, QPropertyAnimation, QEasingCurve, QVariantAnimation, QObject,
    QAbstractAnimation
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QPainterPath, QPixmap, QLinearGradient, QResizeEvent,
    QRadialGradient, QWheelEvent, QPaintEvent
)
from PySide6.QtWidgets import (
    QLineEdit, QWidget, QGraphicsOpacityEffect, QGraphicsDropShadowEffect, QFrame, QVBoxLayout,
    QScrollBar, QScrollArea, QLabel
)

from core._style_colors import Colors
from core._style_graphics import Graphics
from core._constants import ProfileState
from core._registry import load_ui_geometry


# =============================================================================
# SMOOTH SCROLL ENGINE (Движок плавного скольжения)
# =============================================================================

class SmoothScrollBar(QScrollBar):
    """
    Кастомный скроллбар с физикой плавного скольжения (Kinetic Scrolling).
    Использует QPropertyAnimation для интерполяции значений.
    """
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target_value: int = self.value()
        self._smooth_enabled: bool = True
        
        self._anim = QPropertyAnimation(self, b"value")
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        
        # Загружаем настройки физики из реестра (State branch)
        ui_prefs = load_ui_geometry()
        try:
            duration = int(ui_prefs.get("smooth_scroll_duration", "200"))
        except ValueError:
            duration = 200
            
        self._anim.setDuration(duration)
        
        # Синхронизация буфера при внешних изменениях (ресайз, ручной драг ползунка)
        self.valueChanged.connect(self._on_value_changed)
        self.rangeChanged.connect(self._on_range_changed)

    def set_smooth_mode(self, enabled: bool) -> None:
        """
        Временное отключение плавности (Instant Mode).
        Используется виртуальной каруселью во время свайп-выделения (Sweep Selection),
        чтобы скролл не отставал от курсора мыши.
        """
        self._smooth_enabled = enabled
        if not enabled and self._anim.state() == QAbstractAnimation.State.Running:
            self._anim.stop()
            self.setValue(self._target_value)

    def scroll_to(self, target: int) -> None:
        """Плавное перемещение ползунка к новой цели с обрезкой по границам."""
        target = max(self.minimum(), min(target, self.maximum()))
        self._target_value = target
        
        if not self._smooth_enabled:
            self.setValue(target)
            return
            
        self._anim.stop()
        self._anim.setStartValue(self.value())
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_value_changed(self, value: int) -> None:
        """Синхронизация цели, если значение изменилось не нашей анимацией."""
        if self._anim.state() != QAbstractAnimation.State.Running:
            self._target_value = value

    def _on_range_changed(self, min_val: int, max_val: int) -> None:
        """Корректировка цели при изменении размеров контента."""
        self._target_value = max(min_val, min(self._target_value, max_val))


class SmoothScrollDelegate(QObject):
    """
    Сетевой фильтр событий (Event Filter) для перехвата колеса мыши.
    Глушит дефолтный резкий скролл Qt и передает дельту в SmoothScrollBar.
    """
    def __init__(self, scrollbar: SmoothScrollBar, orientation: Qt.Orientation, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.scrollbar = scrollbar
        self.orientation = orientation
        
        ui_prefs = load_ui_geometry()
        try:
            self.step_size = int(ui_prefs.get("smooth_scroll_step", "120"))
        except ValueError:
            self.step_size = 120

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Wheel:
            wheel_event: QWheelEvent = event  # type: ignore
            
            # Защита от тачпадов (Precision Touchpad Guard).
            # Если есть попиксельная дельта, значит ОС сама генерирует плавный скролл.
            if not wheel_event.pixelDelta().isNull():
                return False
                
            # Извлекаем дельту в зависимости от ориентации скроллбара
            delta = wheel_event.angleDelta().y() if self.orientation == Qt.Orientation.Vertical else wheel_event.angleDelta().x()
            
            if delta == 0:
                return False
                
            # 120 - стандартный шаг одного щелчка колеса мыши в Windows
            steps = delta / 120.0
            scroll_amount = steps * self.step_size
            
            new_target = self.scrollbar._target_value - scroll_amount
            self.scrollbar.scroll_to(int(new_target))
            
            return True  # Глушим дефолтное событие Qt
            
        return super().eventFilter(obj, event)


class SmoothScrollArea(QScrollArea):
    """
    Кастомная область прокрутки, оснащенная движком плавного скольжения "из коробки".
    Заменяет стандартный QScrollArea в панелях настроек и редакторах.
    """
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        
        # Вертикальный тракт
        self.v_scrollbar = SmoothScrollBar(self)
        self.v_scrollbar.setOrientation(Qt.Orientation.Vertical)
        self.setVerticalScrollBar(self.v_scrollbar)
        self.v_delegate = SmoothScrollDelegate(self.v_scrollbar, Qt.Orientation.Vertical, self)
        self.viewport().installEventFilter(self.v_delegate)
        
        # Горизонтальный тракт
        self.h_scrollbar = SmoothScrollBar(self)
        self.h_scrollbar.setOrientation(Qt.Orientation.Horizontal)
        self.setHorizontalScrollBar(self.h_scrollbar)
        self.h_delegate = SmoothScrollDelegate(self.h_scrollbar, Qt.Orientation.Horizontal, self)
        self.viewport().installEventFilter(self.h_delegate)


# =============================================================================
# CACHED LED PAINTER (Генератор диодов)
# =============================================================================

class CachedLedPainter:
    """
    Генератор растровых кадров для светодиодов статуса (Bake and Blit Engine).
    Один раз вычисляет все фазы пульсации и неонового свечения, сохраняя их в локальный словарь.
    Избавляет paintEvent карточек от тяжелой математики и аллокаций.
    """
    _baked: bool = False
    _cache: dict[str, QPixmap] = {}

    @classmethod
    def bake_all(cls) -> None:
        """
        Выпекает все кадры анимации для всех состояний и кладет в глобальный кэш.
        Выполняется строго один раз при старте приложения.
        """
        if cls._baked:
            return
        cls._baked = True

        # Маппинг состояний на базовые HEX-цвета
        state_colors = {
            ProfileState.ACTIVE: Colors.NEON_GREEN,
            ProfileState.ERR_API: Colors.ERROR,
            ProfileState.ERR_APP: "#FF8C00",      # Огненно-оранжевый
            ProfileState.THROTTLED: Colors.WARNING, # Лимонно-желтый
            ProfileState.WARMUP: Colors.NEON_PURPLE,
        }

        size = 16
        center = size / 2.0
        radius = 4.0
        total_frames = 10

        # 1. Выпечка динамических состояний (Пульсация)
        for state, hex_color in state_colors.items():
            base_color = QColor(hex_color)
            
            for frame in range(total_frames):
                # Вычисляем синусоиду для плавного дыхания неона (от 0.0 до 1.0)
                progress = frame / float(total_frames)
                sine_val = (math.sin(progress * 2 * math.pi) + 1.0) / 2.0
                
                # Альфа-канал свечения пульсирует от 40 до 190
                glow_alpha = int(40 + 150 * sine_val)

                pm = QPixmap(size, size)
                pm.fill(Qt.GlobalColor.transparent)
                
                p = QPainter(pm)
                try:
                    p.setRenderHint(QPainter.RenderHint.Antialiasing)

                    # Отрисовка неонового ореола (Glow)
                    glow_grad = QRadialGradient(center, center, size / 2.0)
                    glow_c = QColor(base_color)
                    glow_c.setAlpha(glow_alpha)
                    glow_grad.setColorAt(0.0, glow_c)
                    glow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
                    
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(glow_grad)
                    p.drawEllipse(0, 0, size, size)

                    # Отрисовка жесткого ядра диода
                    p.setBrush(base_color)
                    p.drawEllipse(int(center - radius), int(center - radius), int(radius * 2), int(radius * 2))
                finally:
                    p.end()

                # Сохраняем кадр в глобальный кэш Qt
                cls._cache[f"led_{state.value}_{frame}"] = pm

        # 2. Выпечка статических состояний (Без анимации)
        
        # CLOSED (Вдавленный, темно-серый)
        pm_closed = QPixmap(size, size)
        pm_closed.fill(Qt.GlobalColor.transparent)
        p_closed = QPainter(pm_closed)
        try:
            p_closed.setRenderHint(QPainter.RenderHint.Antialiasing)
            p_closed.setPen(Qt.PenStyle.NoPen)
            p_closed.setBrush(QColor("#35393C"))
            p_closed.drawEllipse(int(center - radius), int(center - radius), int(radius * 2), int(radius * 2))
        finally:
            p_closed.end()
        cls._cache[f"led_{ProfileState.CLOSED.value}_0"] = pm_closed
        
        # UNKNOWN (Полый контур)
        pm_unk = QPixmap(size, size)
        pm_unk.fill(Qt.GlobalColor.transparent)
        p_unk = QPainter(pm_unk)
        try:
            p_unk.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen = QPen(QColor(Colors.TXT_DIM), 1.5)
            p_unk.setPen(pen)
            p_unk.setBrush(Qt.BrushStyle.NoBrush)
            p_unk.drawEllipse(int(center - radius), int(center - radius), int(radius * 2), int(radius * 2))
        finally:
            p_unk.end()
        cls._cache[f"led_{ProfileState.UNKNOWN.value}_0"] = pm_unk
    
    @classmethod
    def get_frame(cls, cache_key: str) -> QPixmap | None:
        """Безопасное извлечение кадра из перманентного кэша."""
        return cls._cache.get(cache_key)


# =============================================================================
# SECURE INPUTS & DEBOSSED WRAPPERS
# =============================================================================

class SecurePasswordLineEdit(QLineEdit):
    """
    Умное поле ввода пароля с защитой от Shoulder Surfing и кэширования ОС.
    Включает нативную кнопку-глазок для переключения видимости.
    """
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setEchoMode(QLineEdit.EchoMode.Password)
        
        # Защита от кэширования ОС (IME, T9, AutoUppercase)
        # Предотвращает утечку паролей в системные словари автозамены Windows
        self.setInputMethodHints(
            Qt.InputMethodHint.ImhSensitiveData |
            Qt.InputMethodHint.ImhNoPredictiveText |
            Qt.InputMethodHint.ImhNoAutoUppercase
        )
        
        # Нативный QAction внутри поля (справа)
        self._eye_action = self.addAction(
            Graphics.get_eye_icon(False),
            QLineEdit.ActionPosition.TrailingPosition
        )
        self._eye_action.setCheckable(True)
        self._eye_action.triggered.connect(self._toggle_visibility)
    
    def _toggle_visibility(self, checked: bool) -> None:
        """Переключает видимость пароля, сохраняя позицию каретки."""
        # Запоминаем позицию курсора, чтобы он не прыгал в начало при смене режима
        pos = self.cursorPosition()
        
        if checked:
            self.setEchoMode(QLineEdit.EchoMode.Normal)
            self._eye_action.setIcon(Graphics.get_eye_icon(True))
        else:
            self.setEchoMode(QLineEdit.EchoMode.Password)
            self._eye_action.setIcon(Graphics.get_eye_icon(False))
        
        self.setCursorPosition(pos)
    
    def focusOutEvent(self, event: QEvent) -> None:
        """Защита от подглядывания: прячем пароль при потере фокуса."""
        if self.echoMode() == QLineEdit.EchoMode.Normal:
            self._eye_action.setChecked(False)
            self._toggle_visibility(False)
        super().focusOutEvent(event)


class DebossedLineEdit(QFrame):
    """
    Премиальное вдавленное поле ввода (Neumorphic Debossed Wrapper).
    Использует паттерн двойной буферизации (QPixmap Cache) и конвейер из 4-х градиентов
    для отрисовки сложных внутренних теней за O(1), сохраняя нулевую нагрузку на CPU.
    Оснащен алгоритмом Rubber Band Rendering для эластичного растяжения при ресайзе.
    Включает физические фаски (Bevel & Emboss) для создания реалистичной глубины.
    """
    
    def __init__(self, parent: QWidget | None = None, inner_class: type[QLineEdit] = QLineEdit) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        self._radius: int = 6
        self._is_focused: bool = False
        self._cached_pixmap: QPixmap | None = None
        self._cache_dirty: bool = True
        
        # Debounce-таймер для защиты от спама при ресайзе окна
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._on_resize_timeout)
        
        # Инициализация прозрачного движка ввода
        self.inner_input = inner_class(self)
        self.inner_input.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; color: {Colors.TXT_PRIMARY}; padding: 0px; }}"
        )
        
        # Проброс сигнала textChanged для Duck Typing совместимости с автосохранением
        self.textChanged = self.inner_input.textChanged
        
        # Перехват фокуса для подсветки рамки
        self.inner_input.installEventFilter(self)
        
        layout = QVBoxLayout(self)
        # Отступы внутри рамки (чтобы текст не наезжал на тени)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.addWidget(self.inner_input)
        
        self.setMinimumHeight(37)

    # --- DUCK TYPING API (Проброс методов QLineEdit) ---
    
    def text(self) -> str:
        return self.inner_input.text()
        
    def setText(self, text: str) -> None:
        self.inner_input.setText(text)
        
    def setPlaceholderText(self, text: str) -> None:
        self.inner_input.setPlaceholderText(text)
        
    def setAlignment(self, alignment: Qt.AlignmentFlag) -> None:
        self.inner_input.setAlignment(alignment)
        
    def setReadOnly(self, read_only: bool) -> None:
        self.inner_input.setReadOnly(read_only)
        
    def setEnabled(self, enabled: bool) -> None:
        super().setEnabled(enabled)
        self.inner_input.setEnabled(enabled)
        # При смене состояния перепекаем кэш мгновенно
        self._generate_cache()
        self.update()

    def installEventFilter(self, filter_obj: QObject) -> None:
        """
        КРИТИЧНО: Пробрасываем установку фильтра на внутренний инпут.
        Это гарантирует, что таймер автосохранения (Debounce) в панели настроек
        корректно поймает событие FocusOut и не затрет пароли в реестре.
        """
        self.inner_input.installEventFilter(filter_obj)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Внутренний перехватчик для реактивной подсветки рамки при фокусе."""
        if obj == self.inner_input:
            if event.type() == QEvent.Type.FocusIn:
                self._is_focused = True
                self._generate_cache()
                self.update()
            elif event.type() == QEvent.Type.FocusOut:
                self._is_focused = False
                self._generate_cache()
                self.update()
        return super().eventFilter(obj, event)

    # --- ZERO-CPU RENDERING ENGINE ---
    
    def resizeEvent(self, event: QResizeEvent) -> None:
        """Инвалидация кэша при изменении размеров и запуск эластичного рендеринга."""
        self._cache_dirty = True
        if self._resize_timer.isActive():
            self._resize_timer.stop()
        self._resize_timer.start()
        super().resizeEvent(event)
    
    def _on_resize_timeout(self) -> None:
        """Слот окончания ресайза. Выпекает идеальную текстуру."""
        self._generate_cache()
        self.update()
        
    def paintEvent(self, event: QPaintEvent) -> None:
        """Блиц-отрисовка закэшированной подложки за O(1) с поддержкой Rubber Band."""
        if self._cached_pixmap is None:
            self._generate_cache()
            
        if not self._cached_pixmap:
            return
            
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            if self._cache_dirty:
                # Аппаратное растяжение старого кэша во время ресайза
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawPixmap(self.rect(), self._cached_pixmap)
            else:
                # Идеальный попиксельный рендер в состоянии покоя
                painter.drawPixmap(0, 0, self._cached_pixmap)
        finally:
            painter.end()
            
    def _generate_cache(self) -> None:
        """
        Тяжелая математика генерации неоморфной вдавленной фаски.
        Использует Four-Gradient Edge Blending Pipeline для имитации размытия
        и QPainterPath для физических граней (Bevel & Emboss).
        """
        size = self.size()
        if size.width() <= 0 or size.height() <= 0:
            return
            
        self._cached_pixmap = QPixmap(size)
        self._cached_pixmap.fill(Qt.GlobalColor.transparent)
        
        p = QPainter(self._cached_pixmap)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.rect().adjusted(1, 1, -1, -1)
            
            # 1. Маска обрезки (Clipping Path)
            path = QPainterPath()
            path.addRoundedRect(rect, self._radius, self._radius)
            p.setClipPath(path)
            
            # 2. Полупрозрачная подложка (Glassmorphic Base)
            base_color = QColor(0, 0, 0, 40) if self.isEnabled() else QColor(0, 0, 0, 90)
            p.fillPath(path, base_color)
            
            # 3. Четырехкомпонентный конвейер градиентов (Four-Gradient Edge Blending)
            shadow_dark = QColor(getattr(Colors, "INPUT_SHADOW_DARK", "#000000"))
            shadow_light = QColor(getattr(Colors, "INPUT_SHADOW_LIGHT", "#FFFFFF"))
            
            # Верхняя тень (Top Inner Shadow)
            grad_top = QLinearGradient(0, rect.top(), 0, rect.top() + 6)
            c_top_start = QColor(shadow_dark); c_top_start.setAlpha(160)
            c_top_end = QColor(shadow_dark); c_top_end.setAlpha(0)
            grad_top.setColorAt(0.0, c_top_start)
            grad_top.setColorAt(1.0, c_top_end)
            p.fillRect(rect.x(), rect.y(), rect.width(), 6, grad_top)
            
            # Левая тень (Left Inner Shadow)
            grad_left = QLinearGradient(rect.left(), 0, rect.left() + 6, 0)
            c_left_start = QColor(shadow_dark); c_left_start.setAlpha(130)
            c_left_end = QColor(shadow_dark); c_left_end.setAlpha(0)
            grad_left.setColorAt(0.0, c_left_start)
            grad_left.setColorAt(1.0, c_left_end)
            p.fillRect(rect.x(), rect.y(), 6, rect.height(), grad_left)
            
            # Нижний блик (Bottom Glow Bezel)
            grad_bottom = QLinearGradient(0, rect.bottom(), 0, rect.bottom() - 3)
            c_bot_start = QColor(shadow_light); c_bot_start.setAlpha(45)
            c_bot_end = QColor(shadow_light); c_bot_end.setAlpha(0)
            grad_bottom.setColorAt(0.0, c_bot_start)
            grad_bottom.setColorAt(1.0, c_bot_end)
            p.fillRect(rect.x(), rect.bottom() - 3, rect.width(), 3, grad_bottom)
            
            # Правый блик (Right Glow Bezel)
            grad_right = QLinearGradient(rect.right(), 0, rect.right() - 3, 0)
            c_right_start = QColor(shadow_light); c_right_start.setAlpha(30)
            c_right_end = QColor(shadow_light); c_right_end.setAlpha(0)
            grad_right.setColorAt(0.0, c_right_start)
            grad_right.setColorAt(1.0, c_right_end)
            p.fillRect(rect.right() - 3, rect.y(), 3, rect.height(), grad_right)
            
            # 4. Физическая фаска (Bevel & Emboss для вдавленности)
            # Для вдавленного элемента свет и тень меняются местами: тень сверху-слева, свет снизу-справа
            path_shadow = QPainterPath()
            path_shadow.moveTo(rect.left(), rect.bottom() - self._radius)
            path_shadow.lineTo(rect.left(), rect.top() + self._radius)
            path_shadow.arcTo(rect.left(), rect.top(), self._radius * 2, self._radius * 2, 180, -90)
            path_shadow.lineTo(rect.right() - self._radius, rect.top())
            
            path_light = QPainterPath()
            path_light.moveTo(rect.right(), rect.top() + self._radius)
            path_light.lineTo(rect.right(), rect.bottom() - self._radius)
            path_light.arcTo(rect.right() - self._radius * 2, rect.bottom() - self._radius * 2, self._radius * 2, self._radius * 2, 0, -90)
            path_light.lineTo(rect.left() + self._radius, rect.bottom())
            
            c_shadow_bevel = QColor(shadow_dark)
            c_shadow_bevel.setAlpha(180)
            c_light_bevel = QColor(shadow_light)
            c_light_bevel.setAlpha(40)
            
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(c_shadow_bevel, 1.0))
            p.drawPath(path_shadow)
            
            p.setPen(QPen(c_light_bevel, 1.0))
            p.drawPath(path_light)
            
            # 5. Снимаем клиппинг для отрисовки внешней рамки
            p.setClipping(False)
            
            # 6. Тонкая интерактивная рамка (Focus Highlight)
            if self._is_focused and self.isEnabled():
                p.setPen(QPen(QColor(Colors.ACCENT), 1.0))
            else:
                # Едва заметная рамка в обычном состоянии
                border_color = QColor(Colors.BORDER)
                border_color.setAlpha(60)
                p.setPen(QPen(border_color, 1.0))
                
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect, self._radius, self._radius)
            
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            p.end()
            
        self._cache_dirty = False


class SecureDebossedLineEdit(DebossedLineEdit):
    """
    Премиальное вдавленное поле ввода со встроенной защитой пароля.
    Инкапсулирует SecurePasswordLineEdit внутри Debossed-контейнера.
    """
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent, inner_class=SecurePasswordLineEdit)


# =============================================================================
# GLASS TILE & INDICATORS
# =============================================================================

class AutoSaveIndicator(QWidget):
    """
    Интеллектуальный индикатор статуса фонового сохранения (Zero-RAM Vector Graphics).
    Работает в режиме "Always-on Display" для интеграции в главные панели меню.
    Использует аппаратное ускорение прозрачности для пульсации, снижая нагрузку на CPU до ~0%.
    """
    IDLE: int = 0
    SAVING: int = 1
    ERROR: int = 2
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(20, 20)
        
        self._state: int = self.IDLE
        self._angle: int = 0
        
        # Таймер для вращения спиннера (~60 FPS)
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._update_spin)
        
        # Эффект прозрачности для аппаратной пульсации LED
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity_effect)
        
        # Анимация прозрачности (настраивается динамически в set_state)
        self._pulse_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._pulse_anim.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._pulse_anim.setLoopCount(-1)  # Бесконечный цикл
        
        # Запускаем базовое состояние
        self.set_state(self.IDLE, "Все настройки сохранены")
    
    def set_state(self, state: int, tooltip: str = "") -> None:
        """
        Устанавливает текущее состояние индикатора и переключает режимы анимации.
        """
        self._state = state
        self.setToolTip(tooltip)
        
        if state == self.IDLE:
            self._anim_timer.stop()
            self._pulse_anim.stop()
            # Плавная, медленная пульсация зеленого светодиода (цикл 3 секунды)
            self._pulse_anim.setDuration(3000)
            self._pulse_anim.setKeyValues([
                (0.0, 0.4),
                (0.5, 1.0),
                (1.0, 0.4)
            ])
            self._pulse_anim.start()
            
        elif state == self.SAVING:
            self._pulse_anim.stop()
            self._opacity_effect.setOpacity(1.0)
            # Запускаем вращение спиннера
            if not self._anim_timer.isActive():
                self._anim_timer.start(16)
                
        elif state == self.ERROR:
            self._anim_timer.stop()
            self._pulse_anim.stop()
            # Агрессивное, быстрое мерцание красного треугольника (цикл 0.6 секунды)
            self._pulse_anim.setDuration(600)
            self._pulse_anim.setKeyValues([
                (0.0, 0.2),
                (0.5, 1.0),
                (1.0, 0.2)
            ])
            self._pulse_anim.start()
            
        self.update()
    
    def _update_spin(self) -> None:
        """Обновляет угол вращения спиннера."""
        self._angle = (self._angle + 12) % 360
        self.update()
    
    def paintEvent(self, event: QPaintEvent) -> None:
        """Аппаратная отрисовка векторной графики в зависимости от состояния."""
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = self.rect().adjusted(2, 2, -2, -2)
            
            if self._state == self.IDLE:
                # Зеленый светодиод (LED)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(Colors.SUCCESS))
                center = rect.center()
                # Рисуем аккуратный кружок диаметром 8px по центру
                painter.drawEllipse(center, 4, 4)
            
            elif self._state == self.SAVING:
                # Фоновое кольцо (тусклое)
                pen_bg = QPen(QColor(Colors.TXT_DIM), 2)
                painter.setPen(pen_bg)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(rect)
                
                # Вращающаяся дуга (акцентная)
                pen_arc = QPen(QColor(Colors.ACCENT), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
                painter.setPen(pen_arc)
                # drawArc принимает углы в 1/16 градуса
                painter.drawArc(rect, -self._angle * 16, 100 * 16)
            
            elif self._state == self.ERROR:
                # Красный треугольник с восклицательным знаком
                pen_err = QPen(QColor(Colors.ERROR), 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                painter.setPen(pen_err)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                
                path = QPainterPath()
                path.moveTo(10, 2)
                path.lineTo(2, 16)
                path.lineTo(18, 16)
                path.closeSubpath()
                painter.drawPath(path)
                
                # Восклицательный знак
                painter.drawLine(10, 7, 10, 11)
                painter.drawPoint(10, 14)
        
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            painter.end()


class GlassTile(QFrame):
    """
    Универсальный контейнер-плитка (Псевдо-Глассморфизм).
    Имитирует физику матового стекла с помощью диагональных градиентов и физических фасок (Bevel & Emboss).
    Использует паттерн двойной буферизации (QPixmap Cache) и эластичный рендеринг
    (Rubber Band) для снижения нагрузки на CPU до 0% при ресайзе.
    """
    
    def __init__(self, parent: QWidget | None = None, enable_hover: bool = True) -> None:
        super().__init__(parent)
        # Делаем фон прозрачным, чтобы отрисовать кастомную форму с закруглениями
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        
        self._radius: int = 12
        self._hover_alpha: int = 0
        self._enable_hover: bool = enable_hover
        self._max_hover_alpha: int = 12 if enable_hover else 0
        
        # Кэш растра для Zero-CPU рендеринга
        self._cached_pixmap: QPixmap | None = None
        self._cache_dirty: bool = True
        
        # Debounce-таймер для защиты от спама при ресайзе окна
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._on_resize_timeout)
        
        # Мягкая тень (отрывает плитку от фона)
        self._shadow = QGraphicsDropShadowEffect(self)
        self._shadow.setBlurRadius(25)
        self._shadow.setOffset(0, 8)
        self._shadow.setColor(QColor(0, 0, 0, 60))
        self.setGraphicsEffect(self._shadow)
        
        # Аниматор свечения (Zero-Leak: создается один раз)
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(300)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_step)
    
    def resizeEvent(self, event: QResizeEvent) -> None:
        """Инвалидация кэша при изменении размеров и запуск эластичного рендеринга."""
        self._cache_dirty = True
        if self._resize_timer.isActive():
            self._resize_timer.stop()
        self._resize_timer.start()
        super().resizeEvent(event)

    def _on_resize_timeout(self) -> None:
        """Слот окончания ресайза. Выпекает идеальную текстуру."""
        self._generate_cache()
        self.update()

    def enterEvent(self, event: QEvent) -> None:
        """Запуск анимации свечения при наведении мыши."""
        if not self._enable_hover:
            super().enterEvent(event)
            return
        self._anim.stop()
        self._anim.setStartValue(self._hover_alpha)
        self._anim.setEndValue(self._max_hover_alpha)
        self._anim.start()
        super().enterEvent(event)
    
    def leaveEvent(self, event: QEvent) -> None:
        """Плавное затухание свечения при уходе мыши."""
        if not self._enable_hover:
            super().leaveEvent(event)
            return
        self._anim.stop()
        self._anim.setStartValue(self._hover_alpha)
        self._anim.setEndValue(0)
        self._anim.start()
        super().leaveEvent(event)
        
    def _on_anim_step(self, value: Any) -> None:
        """Слот обновления альфа-канала. Триггерит быструю перерисовку."""
        self._hover_alpha = int(value)
        self.update()
        
    def paintEvent(self, event: QPaintEvent) -> None:
        """
        Высокопроизводительный рендеринг с поддержкой Rubber Band.
        """
        if self._cached_pixmap is None:
            self._generate_cache()
            
        if not self._cached_pixmap:
            return
            
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            # 1. Отрисовка закэшированного "стекла" за O(1)
            if self._cache_dirty:
                # Аппаратное растяжение старого кэша во время ресайза
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawPixmap(self.rect(), self._cached_pixmap)
            else:
                # Идеальный попиксельный рендер в состоянии покоя
                painter.drawPixmap(0, 0, self._cached_pixmap)
                
            # 2. Отрисовка динамического интерактивного слоя (Hover)
            if self._hover_alpha > 0:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(255, 255, 255, self._hover_alpha))
                painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), self._radius, self._radius)
        finally:
            painter.end()
            
    def _generate_cache(self) -> None:
        """
        Генерация оптической иллюзии матового стекла.
        Выполняется только при остановке ресайза, экономя ресурсы процессора.
        """
        size = self.size()
        if size.width() <= 0 or size.height() <= 0:
            return
            
        self._cached_pixmap = QPixmap(size)
        self._cached_pixmap.fill(Qt.GlobalColor.transparent)
        
        p = QPainter(self._cached_pixmap)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            
            # Отступаем 1px от краев, чтобы тень и фаска не обрезались
            rect = self.rect().adjusted(1, 1, -1, -1)
            
            # --- 1. Тело стекла (Диагональный градиент) ---
            bg_color = QColor(getattr(Colors, "GLASS_BG", "#2A2D31"))
            bg_color.setAlpha(160)  # Полупрозрачный верх
            bg_color_dark = QColor(getattr(Colors, "GLASS_BG", "#2A2D31"))
            bg_color_dark.setAlpha(90)   # Более прозрачный низ
            
            grad_bg = QLinearGradient(0, 0, size.width(), size.height())
            grad_bg.setColorAt(0.0, bg_color)
            grad_bg.setColorAt(1.0, bg_color_dark)
            
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(grad_bg)
            p.drawRoundedRect(rect, self._radius, self._radius)
            
            # --- 2. Физическая фаска (Bevel & Emboss) ---
            light_color = QColor(getattr(Colors, "GLASS_BORDER_LIGHT", "#FFFFFF"))
            light_color.setAlpha(45)  # Блик от верхнего света
            dark_color = QColor(getattr(Colors, "GLASS_BORDER_DARK", "#000000"))
            dark_color.setAlpha(80)   # Тень на нижнем ребре
            
            # Верхний левый контур (Свет)
            path_light = QPainterPath()
            path_light.moveTo(rect.left(), rect.bottom() - self._radius)
            path_light.lineTo(rect.left(), rect.top() + self._radius)
            path_light.arcTo(rect.left(), rect.top(), self._radius * 2, self._radius * 2, 180, -90)
            path_light.lineTo(rect.right() - self._radius, rect.top())
            
            # Нижний правый контур (Тень)
            path_shadow = QPainterPath()
            path_shadow.moveTo(rect.right(), rect.top() + self._radius)
            path_shadow.lineTo(rect.right(), rect.bottom() - self._radius)
            path_shadow.arcTo(rect.right() - self._radius * 2, rect.bottom() - self._radius * 2, self._radius * 2, self._radius * 2, 0, -90)
            path_shadow.lineTo(rect.left() + self._radius, rect.bottom())
            
            p.setBrush(Qt.BrushStyle.NoBrush)
            
            p.setPen(QPen(light_color, 1.2))
            p.drawPath(path_light)
            
            p.setPen(QPen(dark_color, 1.2))
            p.drawPath(path_shadow)
            
        finally:
            p.end()
            
        self._cache_dirty = False


# =============================================================================
# ENGRAVED TYPOGRAPHY
# =============================================================================

class EngravedLabel(QLabel):
    """
    Премиальный текстовый лейбл с эффектом лазерной гравировки (Engraving Layout).
    Отрисовывает текст дважды: сначала темную тень со смещением, затем основной цвет.
    Обеспечивает идеальную читаемость на сложных фонах без использования тяжелых HTML-теней.
    """
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
            
            text = self.text()
            rect = self.rect()
            
            # Извлекаем флаги выравнивания
            flags = int(self.alignment())
            if self.wordWrap():
                flags |= Qt.TextFlag.TextWordWrap
                
            # 1. Отрисовка тени (Глубина гравировки)
            shadow_color = QColor(0, 0, 0, 200)
            painter.setPen(shadow_color)
            # Смещение вправо и вниз на 1 пиксель
            painter.drawText(rect.translated(1, 1), flags, text)
            
            # 2. Отрисовка основного текста
            # Берем цвет из текущей палитры (установленный через QSS или setStyleSheet)
            main_color = self.palette().color(self.foregroundRole())
            painter.setPen(main_color)
            painter.drawText(rect, flags, text)
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            painter.end()