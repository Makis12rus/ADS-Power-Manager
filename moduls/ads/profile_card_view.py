"""
Модуль: moduls/ads/profile_card_view.py
Назначение: Фасадный модуль (API Gateway) виртуальной карусели профилей.
Зона ответственности: Реэкспорт декомпозированных классов карусели (Viewport,
                      Row Card, Status LED) из изолированных файлов-сателлитов.
                      Обеспечивает 100% обратную совместимость для внешних панелей
                      (например, `profile_panel.py`), скрывая внутреннюю архитектуру
                      и защищая систему от каскадных изменений.
Интеграция: Слой GUI. Является главной точкой входа для работы с каруселью.
            Не содержит собственной логики, математики или верстки.
"""

# Строгие абсолютные импорты из изолированных цехов (Lego Blocks)
from moduls.ads._card_viewport import RecyclerScrollArea
from moduls.ads._card_row import ProfileRowCard
from moduls.ads._card_led import StatusLedWidget

# Явный контракт экспорта. Гарантирует чистоту пространства имен
# и подсказывает IDE, какие классы доступны в этом модуле.
__all__ = [
    "RecyclerScrollArea",
    "ProfileRowCard",
    "StatusLedWidget"
]