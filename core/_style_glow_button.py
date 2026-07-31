"""
Модуль: core/_style_glow_button.py
Назначение: Изолированный цех хрустальных 3D-кнопок (Presentation Utilities).
Зона ответственности: Высокопроизводительная отрисовка круглых кнопок управления
                      профилями (Play, Restart, Stop) в стиле Premium Glassmorphism.
                      Реализует паттерн Bake & Blit: однократное запекание 5-слойной
                      оптики (каустика, преломление, френель) в ОЗУ и мгновенный
                      наброс готовых High-DPI текстур при скролле карусели (Zero-CPU).
                      Поддерживает кроссфейд и физический эффект вдавливания (Sink-on-Hover).
Интеграция: Слой Presentation (L3). Наследуется от QAbstractButton для сохранения
            нативного контракта сигналов (clicked).
            Используется в карточках виртуальной карусели (ProfileRowCard).
            Реэкспортируется через фасад core/style.py.
"""

import threading
from typing import Any

from PySide6.QtCore import (
    Qt, QEvent, QRectF, QVariantAnimation, QEasingCurve, QPointF
)
from PySide6.QtGui import (
    QPainter, QColor, QPaintEvent, QMouseEvent, QLinearGradient,
    QRadialGradient, QPixmap, QPen, QBrush
)
from PySide6.QtWidgets import QAbstractButton, QWidget

from core._style_graphics import Graphics
from core._style_colors import Colors


class CrystalCache:
    """
    Изолированный кэшер хрустальных сфер (Bake & Blit Engine).
    Генерирует High-DPI текстуры 3D-кнопок за O(1) и хранит их в ОЗУ.
    Защищен от утечек памяти ограниченным набором ключей.
    """
    _cache: dict[str, QPixmap] = {}
    _lock = threading.Lock()

    @classmethod
    def get_frame(
        cls, icon_name: str, bg_hex: str, icon_hex: str,
        state: str, size: float, icon_size: float, dpr: float
    ) -> QPixmap:
        """
        Извлекает готовый кадр из кэша или запекает его при первом обращении.
        Реализует 5-слойную физическую модель стекла.
        """
        key = f"{icon_name}_{bg_hex}_{icon_hex}_{state}_{size}_{dpr}"
        
        # Fast-Path: O(1) возврат из кэша без блокировки
        if key in cls._cache:
            return cls._cache[key]
            
        with cls._lock:
            # Double-check внутри критической секции
            if key in cls._cache:
                return cls._cache[key]
                
            phys_size = int(size * dpr)
            pm = QPixmap(phys_size, phys_size)
            pm.fill(Qt.GlobalColor.transparent)
            pm.setDevicePixelRatio(dpr)
            
            p = QPainter(pm)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                
                rect = QRectF(0, 0, size, size)
                center = rect.center()
                radius = size / 2.0
                
                # 1. Internal Refraction Glow (Внутреннее преломление)
                # Дает стеклу легкий цветовой оттенок, сохраняя центр абсолютно прозрачным
                refraction_grad = QRadialGradient(center.x() - radius * 0.3, center.y() + radius * 0.3, radius * 1.2)
                c_refract = QColor(bg_hex)
                c_refract.setAlpha(80 if state != "disabled" else 30)
                refraction_grad.setColorAt(0.0, c_refract)
                refraction_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
                
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(refraction_grad)
                p.drawEllipse(rect)
                
                # 2. Internal Caustic / Neon Core (Только при наведении)
                if state == "hover":
                    caustic_grad = QRadialGradient(center.x(), center.y() + radius * 0.5, radius * 0.8)
                    c_caustic = QColor(icon_hex)
                    c_caustic.setAlpha(140)
                    caustic_grad.setColorAt(0.0, c_caustic)
                    caustic_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
                    
                    p.setBrush(caustic_grad)
                    p.drawEllipse(rect)
                
                # 3. Fresnel Edge Rim (Эффект Френеля на гранях)
                # Тонкий стеклянный блик по контуру без жесткой рамки
                fresnel_grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
                fresnel_grad.setColorAt(0.0, QColor(255, 255, 255, 180 if state != "disabled" else 50))
                fresnel_grad.setColorAt(0.4, QColor(255, 255, 255, 20))
                fresnel_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
                
                p.setPen(QPen(QBrush(fresnel_grad), 1.0))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawEllipse(rect.adjusted(0.5, 0.5, -0.5, -0.5))
                
                # 4. Specular Highlight (Спекулярный блик источника света)
                spec_rect = QRectF(center.x() - radius * 0.6, center.y() - radius * 0.85, radius * 1.2, radius * 0.7)
                spec_grad = QLinearGradient(spec_rect.topLeft(), spec_rect.bottomRight())
                spec_grad.setColorAt(0.0, QColor(getattr(Colors, "CRYSTAL_HIGHLIGHT", "#E6FFFFFF")))
                spec_grad.setColorAt(1.0, QColor(255, 255, 255, 0))
                
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(spec_grad)
                p.drawEllipse(spec_rect)
                
                # 5. Icon Glow (Свечение вокруг иконки при наведении)
                if state == "hover":
                    icon_glow = QRadialGradient(center, icon_size * 0.8)
                    ig_color = QColor(icon_hex)
                    ig_color.setAlpha(180)
                    icon_glow.setColorAt(0.0, ig_color)
                    icon_glow.setColorAt(1.0, QColor(0, 0, 0, 0))
                    
                    p.setBrush(icon_glow)
                    p.drawEllipse(center, icon_size * 0.8, icon_size * 0.8)
                
                # 6. Vector Icon (Direct Vector Pipeline)
                icon_rect = QRectF(center.x() - icon_size / 2.0, center.y() - icon_size / 2.0, icon_size, icon_size)
                renderer = Graphics.get_svg_renderer(icon_name, icon_hex)
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                renderer.render(p, icon_rect)
                
            finally:
                # Resource Guard: Гарантированное освобождение графического контекста C++
                p.end()
                
            cls._cache[key] = pm
            return pm


class FlatActionButton(QAbstractButton):
    """
    Премиальная хрустальная кнопка в стиле 3D Glassmorphism.
    Отрисовывается аппаратно (Bake & Blit Pipeline), игнорируя баги High-DPI масштабирования.
    Использует кроссфейд (Alpha Blending) и смещения (Sink-on-Hover) для плавного
    перехода между состояниями без нагрузки на процессор.
    """

    def __init__(
            self,
            icon_name: str,
            idle_bg_hex: str,
            hover_bg_hex: str,
            idle_icon_hex: str,
            hover_icon_hex: str,
            disabled_bg_hex: str,
            disabled_icon_hex: str,
            tooltip: str = "",
            button_size: int = 32,
            icon_size: int = 16,
            parent: QWidget | None = None
    ) -> None:
        """
        Инициализация хрустальной сферы.

        :param icon_name: Имя векторной иконки Lucide (например, 'play', 'rotate-cw').
        :param idle_bg_hex: HEX-цвет фона в состоянии покоя.
        :param hover_bg_hex: HEX-цвет фона при наведении.
        :param idle_icon_hex: HEX-цвет иконки в состоянии покоя.
        :param hover_icon_hex: HEX-цвет иконки при наведении.
        :param disabled_bg_hex: HEX-цвет фона для заблокированного состояния.
        :param disabled_icon_hex: HEX-цвет иконки для заблокированного состояния.
        :param tooltip: Всплывающая подсказка.
        :param button_size: Внешний размер кнопки (диаметр).
        :param icon_size: Внутренний размер иконки.
        """
        super().__init__(parent)
        
        self._icon_name = icon_name
        self._idle_bg_hex = idle_bg_hex
        self._hover_bg_hex = hover_bg_hex
        self._idle_icon_hex = idle_icon_hex
        self._hover_icon_hex = hover_icon_hex
        self._disabled_bg_hex = disabled_bg_hex
        self._disabled_icon_hex = disabled_icon_hex
        
        self._button_size = float(button_size)
        self._icon_size = float(icon_size)
        
        # Жесткая фиксация геометрии (Geometry Lock)
        self.setFixedSize(int(self._button_size), int(self._button_size))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            self.setToolTip(tooltip)
            
        # Внутреннее состояние машины рендеринга
        self._hover_progress: float = 0.0
        self._is_pressed: bool = False
        
        # Аниматор плавного перехода (Zero-CPU Hover)
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._anim.valueChanged.connect(self._on_anim_step)

    def reset_state(self) -> None:
        """
        Viewport Recycler Reset: Мгновенная зачистка состояния.
        Вызывается виртуальной каруселью при переиспользовании виджета для нового профиля.
        """
        self._anim.stop()
        self._hover_progress = 0.0
        self._is_pressed = False
        self.update()

    def changeEvent(self, event: QEvent) -> None:
        """Перехватчик изменений состояния (например, блокировки/setEnabled)."""
        if event.type() == QEvent.Type.EnabledChange:
            if not self.isEnabled():
                # Если кнопку заблокировали, гасим ховер-анимацию и сбрасываем прогресс
                self._anim.stop()
                self._hover_progress = 0.0
                self._is_pressed = False
                self.update()
        super().changeEvent(event)

    # ===================== ИНТЕРАКТИВНЫЕ СОБЫТИЯ =====================

    def enterEvent(self, event: QEvent) -> None:
        """Мышь зашла на кнопку: плавно перетекаем в яркий цвет и вдавливаемся."""
        if self.isEnabled():
            self._anim.stop()
            self._anim.setStartValue(self._hover_progress)
            self._anim.setEndValue(1.0)
            self._anim.setDuration(150)  # Быстрый, отзывчивый старт
            self._anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """Мышь ушла: плавно возвращаемся в темный цвет покоя и поднимаемся."""
        if self.isEnabled():
            self._anim.stop()
            self._anim.setStartValue(self._hover_progress)
            self._anim.setEndValue(0.0)
            self._anim.setDuration(250)  # Плавное затухание
            self._anim.start()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """Кнопка вдавлена: активируем состояние Pressed."""
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._is_pressed = True
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        """Кнопка отпущена: возвращаем Hover-стиль и стреляем сигналом clicked."""
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self._is_pressed = False
            self.update()
        super().mouseReleaseEvent(event)

    def _on_anim_step(self, value: Any) -> None:
        """Слот интерполяции: обновляет прогресс и триггерит перерисовку."""
        self._hover_progress = float(value)
        self.update()

    # ===================== АППАРАТНЫЙ РЕНДЕРИНГ (BAKE & BLIT) =====================

    def paintEvent(self, event: QPaintEvent) -> None:
        """
        Аппаратный рендеринг кнопки (Bake & Blit Pipeline).
        Выполняется за O(1) путем наброса готовых High-DPI текстур из кэша.
        """
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            
            dpr = self.devicePixelRatioF()
            circle_size = self._button_size - 4.0
            
            # Вычисляем сдвиг ядра кнопки для эффекта вдавливания (Sink-on-Hover)
            current_progress = 1.0 if self._is_pressed else self._hover_progress
            y_offset = 1.5 * current_progress
            
            # 0. ОТРИСОВКА ТЕНИ И ВНЕШНЕЙ КАУСТИКИ (Directional Shadow & External Caustic)
            if self.isEnabled() and not self._is_pressed:
                # Тень становится чуть светлее и опускается ниже при наведении
                shadow_opacity = int(80 * (1.0 - self._hover_progress * 0.5))
                shadow_offset = 2.5 + (1.5 * self._hover_progress)
                shadow_radius = circle_size / 2.0
                
                shadow_center = QPointF(2.0 + shadow_radius, 1.0 + shadow_offset + shadow_radius)
                
                # Мягкая падающая тень
                if shadow_opacity > 0:
                    shadow_grad = QRadialGradient(shadow_center, shadow_radius * 1.1)
                    shadow_grad.setColorAt(0.0, QColor(0, 0, 0, shadow_opacity))
                    shadow_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
                    
                    painter.setBrush(shadow_grad)
                    painter.setPen(Qt.PenStyle.NoPen)
                    painter.drawEllipse(shadow_center, shadow_radius * 1.1, shadow_radius * 1.1)
                
                # Внешняя каустика (Свет, прошедший сквозь линзу и сфокусированный на фоне)
                if self._hover_progress > 0:
                    caustic_opacity = int(120 * self._hover_progress)
                    caustic_grad = QRadialGradient(shadow_center, shadow_radius * 0.9)
                    
                    c_caustic = QColor(self._hover_icon_hex)
                    c_caustic.setAlpha(caustic_opacity)
                    
                    caustic_grad.setColorAt(0.0, c_caustic)
                    caustic_grad.setColorAt(1.0, QColor(0, 0, 0, 0))
                    
                    painter.setBrush(caustic_grad)
                    painter.drawEllipse(shadow_center, shadow_radius * 0.9, shadow_radius * 0.9)
            
            # 1. ИЗВЛЕЧЕНИЕ ТЕКСТУР ИЗ КЭША
            target_rect = QRectF(2.0, 1.0 + y_offset, circle_size, circle_size)
            
            if not self.isEnabled():
                pm_disabled = CrystalCache.get_frame(
                    self._icon_name, self._disabled_bg_hex, self._disabled_icon_hex,
                    "disabled", circle_size, self._icon_size, dpr
                )
                painter.drawPixmap(target_rect, pm_disabled, QRectF(pm_disabled.rect()))
                return
                
            pm_idle = CrystalCache.get_frame(
                self._icon_name, self._idle_bg_hex, self._idle_icon_hex,
                "idle", circle_size, self._icon_size, dpr
            )
            pm_hover = CrystalCache.get_frame(
                self._icon_name, self._hover_bg_hex, self._hover_icon_hex,
                "hover", circle_size, self._icon_size, dpr
            )
            
            # 2. КРОССФЕЙД (Плавное перетекание состояний Alpha Blending)
            if self._is_pressed or self._hover_progress >= 0.999:
                painter.drawPixmap(target_rect, pm_hover, QRectF(pm_hover.rect()))
            elif self._hover_progress <= 0.001:
                painter.drawPixmap(target_rect, pm_idle, QRectF(pm_idle.rect()))
            else:
                # Анимация прозрачности без нагрузки на процессор
                painter.drawPixmap(target_rect, pm_idle, QRectF(pm_idle.rect()))
                
                painter.setOpacity(self._hover_progress)
                painter.drawPixmap(target_rect, pm_hover, QRectF(pm_hover.rect()))
                
                painter.setOpacity(1.0)
                
        finally:
            # Resource Guard: Гарантированное освобождение графического контекста C++
            painter.end()


# Алиас для обратной совместимости на время миграции импортов
GlowActionButton = FlatActionButton