"""
Модуль: core/_style_backdrop.py
Назначение: Изолированный цех статического объемного фона (Premium PCB + Matte Glass).
Зона ответственности: Рендеринг премиальной текстуры печатной платы с использованием
                      паттерна Офлайн-буферизации (Bake and Blit).
                      Использует математический JIT-генератор (Octilinear Edge Router),
                      алгоритмы Bipartite Distribution и Collision Detection.
                      Обеспечивает Zero-CPU рендеринг при ресайзе окон за счет
                      эластичного растяжения кэша (Rubber Band Rendering) и
                      поддерживает High-DPI мониторы.
Интеграция: Слой Presentation (L3). Абсолютно автономен, не зависит от бизнес-логики.
            Читает настройки кастомизации (Seed, Complexity) из ветки State реестра.
            Реэкспортируется через фасад core/style.py.
"""

import os
import math
import random
from enum import Enum
from typing import List, Tuple, Optional

from PySide6.QtCore import Qt, QEvent, QTimer, QRectF, QPointF
from PySide6.QtGui import (
    QColor, QPainter, QPixmap, QImage, QResizeEvent,
    QPainterPath, QPen, QPainterPathStroker
)
from PySide6.QtWidgets import QWidget

from system.logger import logger
from core._registry import load_ui_geometry
from core._style_colors import Colors

__all__ = ["StaticVolumetricBackdropWidget"]


# =============================================================================
# OCTILINEAR ROUTING ENGINE (Математика 45 градусов)
# =============================================================================

class Side(Enum):
    """Стороны экрана для маршрутизации."""
    LEFT = 1
    RIGHT = 2


class OctilinearFactory:
    """
    Статический фабричный класс для построения путей с углами 45 градусов.
    Поддерживает создание сложных путей с дополнительными изгибами (Detour).
    """
    
    @staticmethod
    def create_path(start: QPointF, end: QPointF, side: Side, detour: float = 0.0, lead_length: float = 0.0) -> QPainterPath:
        """
        Строит путь между двумя точками строго под углами 45° и 90°.
        
        :param start: Стартовая точка (на краю окна).
        :param end: Конечная точка (на ножке чипа).
        :param side: Сторона окна (LEFT или RIGHT).
        :param detour: Длина вставки прямого участка в середине диагонали (для обхода препятствий).
        :param lead_length: Длина прямого горизонтального участка от начала и конца перед изгибами.
        """
        path = QPainterPath()
        path.moveTo(start)
        
        # 1. Расчет векторов смещения для Lead-in / Lead-out
        if side == Side.LEFT:
            offset_start = QPointF(lead_length, 0)   # Вправо от левого края окна
            offset_end = QPointF(-lead_length, 0)    # Влево от чипа
        else:
            offset_start = QPointF(-lead_length, 0)  # Влево от правого края окна
            offset_end = QPointF(lead_length, 0)     # Вправо от чипа
            
        # 2. Вычисление внутренних точек (где начинается сложная геометрия)
        p_inner_start = start + offset_start
        p_inner_end = end + offset_end
        
        # Safety Check: Если точки пересеклись (слишком длинный lead), схлопываем их
        dist_total = abs(end.x() - start.x())
        if dist_total < (lead_length * 2):
            path.lineTo(end)
            return path

        # 3. Рисуем первый прямой участок (Lead-in)
        path.lineTo(p_inner_start)
        
        # 4. Основная логика построения пути (между inner points)
        curr_start = p_inner_start
        curr_end = p_inner_end
        
        dx = curr_end.x() - curr_start.x()
        dy = curr_end.y() - curr_start.y()
        
        req_x_space = abs(dy) + detour
        avail_x_space = abs(dx)
        
        # Если не хватает места для маневра, отменяем detour
        if avail_x_space < req_x_space:
            detour = 0.0
            req_x_space = abs(dy)
        
        # Если места все равно не хватает, соединяем напрямую (защита от артефактов)
        if avail_x_space < req_x_space:
            path.lineTo(curr_end)
        else:
            mid_x = (curr_start.x() + curr_end.x()) / 2.0
            dir_x = 1.0 if dx > 0 else -1.0
            section_start_x = mid_x - (req_x_space / 2.0) * dir_x
            
            if detour > 0.1:
                half_dy = abs(dy) / 2.0
                p1 = QPointF(section_start_x, curr_start.y())
                p2_x = section_start_x + (half_dy * dir_x)
                p2_y = curr_start.y() + (dy / 2.0)
                p2 = QPointF(p2_x, p2_y)
                p3_x = p2_x + (detour * dir_x)
                p3 = QPointF(p3_x, p2_y)
                p4_x = p3_x + (half_dy * dir_x)
                p4 = QPointF(p4_x, curr_end.y())
                
                path.lineTo(p1)
                path.lineTo(p2)
                path.lineTo(p3)
                path.lineTo(p4)
            else:
                x1 = mid_x - (req_x_space / 2.0) * dir_x
                x2 = mid_x + (req_x_space / 2.0) * dir_x
                p1 = QPointF(x1, curr_start.y())
                p2 = QPointF(x2, curr_end.y())
                path.lineTo(p1)
                path.lineTo(p2)
            
            path.lineTo(curr_end)
        
        # 5. Рисуем последний прямой участок (Lead-out)
        path.lineTo(end)
        
        return path


# =============================================================================
# BACKDROP WIDGET (Движок рендеринга)
# =============================================================================

class StaticVolumetricBackdropWidget(QWidget):
    """
    Движок статического фона (Premium PCB + Matte Glass).
    Генерирует кэшированную подложку (Bake Phase) с помощью математического роутера,
    накрывает её полупрозрачным матовым слоем и процедурным шумом.
    Оснащен защитой от мерцания (Opaque Paint) и алгоритмом Rubber Band Rendering
    для нулевой нагрузки на процессор при изменении размеров окна.
    """
    
    # Глобальный кэш текстуры шума (генерируется 1 раз на весь класс)
    _noise_pixmap: Optional[QPixmap] = None
    
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        
        # --- СИСТЕМНЫЕ АТРИБУТЫ ОПТИМИЗАЦИИ (BIGTECH STANDARD) ---
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        
        # Локальный кэш готовой подложки (Bake & Blit)
        self._cached_background: Optional[QPixmap] = None
        self._cache_dirty: bool = True
        
        # Debounce-таймер для защиты от спама при ресайзе окна
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(100)
        self._resize_timer.timeout.connect(self._on_resize_timeout)
        
        # Холодный старт: генерируем текстуру шума
        if StaticVolumetricBackdropWidget._noise_pixmap is None:
            self._generate_noise_texture()
    
    @classmethod
    def _generate_noise_texture(cls, size: int = 240) -> None:
        """Процедурный генератор монохроматического шума (Zero-Disk Footprint)."""
        try:
            pixel_count = size * size
            rand_bytes = os.urandom(pixel_count)
            data = bytearray(pixel_count * 4)
            alpha = 6
            
            for i in range(pixel_count):
                v = rand_bytes[i]
                idx = i * 4
                data[idx] = v          # Blue
                data[idx + 1] = v      # Green
                data[idx + 2] = v      # Red
                data[idx + 3] = alpha  # Alpha
            
            img = QImage(data, size, size, QImage.Format.Format_ARGB32).copy()
            cls._noise_pixmap = QPixmap.fromImage(img)
            
            logger.info(
                "[Backdrop] Процедурная текстура шума (Dithering) успешно сгенерирована в ОЗУ.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
        except Exception as e:
            logger.warning(
                f"[Backdrop] Ошибка генерации шума: {e}. Будет использован чистый фон.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            pm = QPixmap(size, size)
            pm.fill(Qt.GlobalColor.transparent)
            cls._noise_pixmap = pm

    def resizeEvent(self, event: QResizeEvent) -> None:
        """Инвалидация кэша при изменении размеров и запуск эластичного рендеринга."""
        super().resizeEvent(event)
        self._cache_dirty = True
        if self._resize_timer.isActive():
            self._resize_timer.stop()
        self._resize_timer.start()

    def _on_resize_timeout(self) -> None:
        """Слот окончания ресайза. Выпекает идеальную High-DPI текстуру."""
        self._bake_background()
        self.update()

    def refresh_theme(self) -> None:
        """Принудительная перепековка текстуры (вызывается из панели настроек)."""
        self._bake_background()
        self.update()

    def _generate_partition(self, total_pins: int, rng: random.Random) -> List[int]:
        """Разбивает число total_pins на случайные слагаемые (группы) от 1 до 3."""
        groups = []
        remaining = total_pins
        while remaining > 0:
            chunk = rng.randint(1, 3)
            if chunk > remaining:
                chunk = remaining
            groups.append(chunk)
            remaining -= chunk
        return groups

    def _calc_window_points(self, side: Side, count: int, height: float, rng: random.Random) -> List[QPointF]:
        """
        Bipartite Interval Distribution.
        Вычисляет точки на грани окна, разделяя сторону на две зоны (до и после центра),
        чтобы создать гарантированный "заповедный" разрыв посередине.
        """
        points = []
        if count <= 0:
            return points
            
        pin_spacing = 15.0
        padding = 40.0
        center_gap = height * 0.3  # 30% высоты экрана в центре - пустые
        
        groups_sizes = self._generate_partition(count, rng)
        
        mid_idx = math.ceil(len(groups_sizes) / 2)
        groups_A = groups_sizes[:mid_idx]
        groups_B = groups_sizes[mid_idx:]
        
        center = height / 2.0
        span_A_start = padding
        span_A_end = center - (center_gap / 2.0)
        span_B_start = center + (center_gap / 2.0)
        span_B_end = height - padding
        
        x_pos = 0.0 if side == Side.LEFT else self.width()
        
        def process_span(span_start: float, span_end: float, groups: List[int]) -> None:
            if not groups: return
            
            usable = span_end - span_start
            occupied = sum([(g - 1) * pin_spacing for g in groups])
            
            num_gaps = len(groups) + 1
            free = usable - occupied
            gap = max(0.0, free / num_gaps)
            
            curr = span_start
            for g_size in groups:
                curr += gap
                for i in range(g_size):
                    offset = i * pin_spacing
                    abs_pos = curr + offset
                    points.append(QPointF(x_pos, abs_pos))
                curr += (g_size - 1) * pin_spacing
                
        process_span(span_A_start, span_A_end, groups_A)
        process_span(span_B_start, span_B_end, groups_B)
        
        return points

    def _generate_pcb_path(self, width: float, height: float, seed_val: str, complexity: int, thickness: int) -> QPainterPath:
        """
        Математический JIT-генератор печатной платы (Edge Router 45°).
        Создает уникальный узор, где дорожки растут строго от левого и правого краев
        к чипам, оставляя "Заповедную зону" (Sanctuary Zone) посередине чистой.
        """
        rng = random.Random(str(seed_val))
        final_path = QPainterPath()
        occupied_space = QPainterPath()
        
        # Настройка "обводчика" для проверки пересечений (Collision Detection)
        stroker = QPainterPathStroker()
        stroker.setWidth(thickness + 10.0) # Запас пространства вокруг дорожки
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        
        # 1. Определение зон
        left_zone_end = width * 0.30
        right_zone_start = width * 0.70
        
        num_chips_per_side = max(1, min(6, complexity))
        
        def place_chips_and_route(side: Side) -> None:
            total_pins = 0
            chips_data = []
            
            # Расстановка чипов
            for _ in range(num_chips_per_side):
                w = rng.randint(30, 60)
                h = rng.randint(30, 60)
                
                # Разделяем экран на вертикальные слоты для равномерного распределения
                slot_h = height / num_chips_per_side
                cy = rng.uniform(slot_h * _ + 20, slot_h * (_ + 1) - h - 20)
                
                if side == Side.LEFT:
                    cx = rng.uniform(40, left_zone_end - w)
                else:
                    cx = rng.uniform(right_zone_start, width - w - 40)
                    
                chip_rect = QRectF(cx, cy, w, h)
                
                # Добавляем чип в финальный путь и в зону коллизий
                final_path.addRoundedRect(chip_rect, 4, 4)
                occupied_space.addRoundedRect(chip_rect.adjusted(-10, -10, 10, 10), 4, 4)
                
                pins_count = rng.randint(2, max(3, complexity))
                total_pins += pins_count
                
                # Вычисляем точки ножек на чипе (со стороны окна)
                chip_points = []
                pin_step = h / (pins_count + 1)
                for i in range(pins_count):
                    py = cy + pin_step * (i + 1)
                    px = cx if side == Side.LEFT else cx + w
                    chip_points.append(QPointF(px, py))
                    
                chips_data.append(chip_points)
                
            # Генерация стартовых точек на краю окна
            window_points = self._calc_window_points(side, total_pins, height, rng)
            
            # Трассировка (Routing)
            win_idx = 0
            for chip_points in chips_data:
                for chip_pt in chip_points:
                    if win_idx >= len(window_points):
                        break
                    win_pt = window_points[win_idx]
                    win_idx += 1
                    
                    # Цикл попыток с разным detour (Collision Avoidance)
                    success = False
                    for attempt in range(5):
                        detour = 0.0
                        if rng.random() < 0.7:
                            detour = rng.uniform(10.0, 50.0)
                            
                        candidate_path = OctilinearFactory.create_path(
                            win_pt, chip_pt, side, detour=detour, lead_length=20.0
                        )
                        
                        stroked = stroker.createStroke(candidate_path)
                        if not stroked.intersects(occupied_space):
                            # Путь свободен
                            occupied_space.addPath(stroked)
                            final_path.addPath(candidate_path)
                            
                            # Добавляем контактные площадки (Vias)
                            final_path.addEllipse(win_pt, 3, 3)
                            final_path.addEllipse(chip_pt, 2, 2)
                            success = True
                            break
                            
                    # Если не нашли путь без коллизий, рисуем прямой (Fallback)
                    if not success:
                        fallback_path = OctilinearFactory.create_path(win_pt, chip_pt, side, detour=0.0, lead_length=10.0)
                        final_path.addPath(fallback_path)
                        final_path.addEllipse(win_pt, 3, 3)
                        final_path.addEllipse(chip_pt, 2, 2)

        # Запускаем трассировку для обоих берегов
        place_chips_and_route(Side.LEFT)
        place_chips_and_route(Side.RIGHT)
        
        return final_path

    def _bake_background(self) -> None:
        """
        JIT-выпечка текстуры (Bake Phase).
        Читает настройки из реестра, генерирует векторный путь платы, рендерит его
        с учетом дробного масштабирования (High-DPI), накладывает матовый щит и шум.
        """
        logic_size = self.size()
        if logic_size.width() <= 0 or logic_size.height() <= 0:
            return
            
        try:
            # Запрашиваем физический коэффициент масштабирования монитора
            dpr = self.devicePixelRatioF()
            
            # КРИТИЧНО: Явное приведение к int для защиты от ошибки QSize.toSize()
            phys_w = int(logic_size.width() * dpr)
            phys_h = int(logic_size.height() * dpr)
            
            pm = QPixmap(phys_w, phys_h)
            pm.setDevicePixelRatio(dpr)
            
            ui_prefs = load_ui_geometry()
            bg_base = ui_prefs.get("bg_base_color", Colors.BG_SAPPHIRE_BASE)
            pcb_color = ui_prefs.get("bg_pcb_color", Colors.PCB_NEON_BLUE)
            seed_val = ui_prefs.get("bg_pcb_seed", "42")
            
            # Бронебойные предохранители (Clamping) для защиты от зависаний
            try:
                opacity_pct = max(0, min(100, int(ui_prefs.get("bg_pcb_opacity", "85"))))
            except ValueError:
                opacity_pct = 85
                
            try:
                thickness = max(1, min(5, int(ui_prefs.get("bg_pcb_thickness", "2"))))
            except ValueError:
                thickness = 2
                
            try:
                complexity = max(1, min(10, int(ui_prefs.get("bg_pcb_complexity", "5"))))
            except ValueError:
                complexity = 5

            pm.fill(QColor(bg_base))
            
            p = QPainter(pm)
            try:
                p.setRenderHint(QPainter.RenderHint.Antialiasing)
                
                # 1. Генерация и отрисовка математической платы (Edge Router)
                pcb_path = self._generate_pcb_path(logic_size.width(), logic_size.height(), seed_val, complexity, thickness)
                
                pen = QPen(QColor(pcb_color))
                pen.setWidth(thickness)
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(pcb_path)
                
                # 2. Матовый щит (Frosted Glass Shield)
                # Заливаем плату базовым цветом с указанной прозрачностью, уводя её на задний план
                shield_color = QColor(bg_base)
                shield_color.setAlpha(int(255 * (opacity_pct / 100.0)))
                p.fillRect(QRectF(0, 0, logic_size.width(), logic_size.height()), shield_color)
                
                # 3. Дизеринг (Шум)
                if self._noise_pixmap:
                    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                    p.drawTiledPixmap(QRectF(0, 0, logic_size.width(), logic_size.height()), self._noise_pixmap)
            finally:
                # Resource Guard: Гарантированное освобождение C++ контекста
                p.end()

            self._cached_background = pm
            self._cache_dirty = False
            
            logger.info(
                f"[Backdrop] Математическая трассировка платы (Seed: {seed_val}, DPR: {dpr:.2f}) завершена. Процессор уходит в спячку.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
        except Exception as e:
            logger.error(
                f"[Backdrop] Сбой при выпекании текстуры: {e}. Откат на базовый цвет.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            # Fallback: заливаем сплошным цветом при критическом сбое
            fallback_pm = QPixmap(self.size())
            fallback_pm.fill(QColor(Colors.BG_SAPPHIRE_BASE))
            self._cached_background = fallback_pm
            self._cache_dirty = False

    def paintEvent(self, event: QEvent) -> None:
        """
        Блиц-отрисовка (O(1) Blit-Copy) закэшированной текстуры.
        Абсолютно не нагружает процессор в состоянии покоя.
        """
        if self._cached_background is None:
            self._bake_background()
            
        if not self._cached_background:
            return
            
        painter = QPainter(self)
        try:
            if self._cache_dirty:
                # Rubber Band Rendering: Аппаратное растяжение старого кэша во время ресайза
                painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                painter.drawPixmap(self.rect(), self._cached_background)
            else:
                # Идеальный попиксельный рендер в состоянии покоя (Blit Phase)
                painter.drawPixmap(0, 0, self._cached_background)
        finally:
            # Resource Guard: Гарантированное освобождение C++ контекста
            painter.end()