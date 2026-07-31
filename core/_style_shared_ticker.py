"""
Модуль: core/_style_shared_ticker.py
Назначение: Единый бригадный метроном приложения (Shared Ticking Engine).
Зона ответственности: Глобальная синхронизация всех UI-анимаций (пульсация
                      светодиодов, вращение спинеров) через один системный таймер.
                      Избавляет систему от "таймерного взрыва" и перегрузки
                      Event Loop при отображении сотен активных профилей.
Интеграция: Слой Presentation (L3). Абсолютно автономен.
            Реэкспортируется через фасад core/style.py.
"""

import threading
from typing import Tuple, Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from system.logger import logger


class SharedTicker(QObject):
    """
    Глобальный метроном анимаций (Singleton).
    Генерирует единый пульс для всех динамических элементов интерфейса.
    """
    
    # Сигнал передает: frame_index (0-9) для пульсации, angle (0-359) для вращения
    tick = Signal(int, int)
    
    _instance: 'SharedTicker | None' = None
    _lock: threading.Lock = threading.Lock()
    
    def __new__(cls, *args: Any, **kwargs: Any) -> 'SharedTicker':
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls, *args, **kwargs)
                cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, parent: QObject | None = None) -> None:
        # Защита от повторной инициализации синглтона
        if self._initialized:
            return
        
        super().__init__(parent)
        self._initialized = True
        
        # Внутреннее состояние машины времени
        self._frame: int = 0
        self._angle: int = 0
        self._total_frames: int = 10
        
        # Системный таймер (~30 FPS)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._update)
    
    def start(self) -> None:
        """Запускает глобальный метроном."""
        if not self._timer.isActive():
            logger.info(
                "[SharedTicker] Запускаем бригадный метроном анимаций (30 FPS). Пульс в норме.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            self._timer.start()
    
    def stop(self) -> None:
        """Останавливает глобальный метроном (используется при Graceful Shutdown)."""
        if self._timer.isActive():
            self._timer.stop()
            logger.info(
                "[SharedTicker] Бригадный метроном остановлен.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
    
    def get_state(self) -> Tuple[int, int]:
        """
        Синхронное получение текущего состояния.
        Полезно для первичной отрисовки виджетов до первого тика таймера.
        """
        return self._frame, self._angle
    
    @Slot()
    def _update(self) -> None:
        """
        Ядро метронома. Выполняет O(1) математику и рассылает пульс подписчикам.
        Никаких аллокаций памяти внутри этого метода!
        """
        # Пульсация: 10 кадров (0..9)
        self._frame = (self._frame + 1) % self._total_frames
        
        # Вращение: шаг 12 градусов (полный оборот за 30 тиков = 1 секунда)
        self._angle = (self._angle + 12) % 360
        
        self.tick.emit(self._frame, self._angle)


# Глобальный экземпляр метронома для импорта в другие модули
shared_ticker = SharedTicker()