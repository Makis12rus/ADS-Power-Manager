"""
Модуль: core/style.py
Назначение: Умный фасад (API Gateway) для подсистемы стилей и графики.
Зона ответственности: Динамическая маршрутизация импортов (PEP 562). Скрывает
                      внутреннюю декомпозицию цеха визуализации от остального приложения.
                      Обеспечивает ленивую загрузку (Lazy Loading) тяжелых графических
                      компонентов (включая Векторный малярный цех, плоские кнопки
                      FlatActionButton, генератор диодов CachedLedPainter,
                      движок хрустальных сфер CrystalCache, глобальный метроном SharedTicker,
                      движок плавного скроллинга Smooth Scroll Engine и
                      премиальную типографику EngravedLabel).
Интеграция: Обеспечивает 100% обратную совместимость. Внешние модули продолжают
            импортировать классы отсюда (например, `from core.style import Colors`),
            а фасад лениво подгружает их из изолированных файлов-сателлитов.
            Это гарантирует, что фоновые воркеры не загрузят в ОЗУ тяжелые
            библиотеки Qt, если им нужны только текстовые константы.
"""

import importlib
from typing import Any, TYPE_CHECKING

# =============================================================================
# STATIC ANALYSIS BRIDGE (IDE & LINTERS)
# =============================================================================
# Этот блок выполняется ТОЛЬКО статическими анализаторами (PyCharm, VSCode, mypy).
# В реальном рантайме (при запуске программы) TYPE_CHECKING всегда False.
# Это позволяет нам обмануть IDE, дав ей автодополнение и проверку типов,
# но при этом сохранить ленивую загрузку и сэкономить оперативную память.
if TYPE_CHECKING:
    from core._style_colors import Colors
    from core._style_qss import Styles, LogStyles
    from core._style_texts import Texts
    from core._style_graphics import Graphics
    from core._style_widgets import (
        SecurePasswordLineEdit,
        AutoSaveIndicator,
        GlassTile,
        DebossedLineEdit,
        SecureDebossedLineEdit,
        CachedLedPainter,
        SmoothScrollBar,
        SmoothScrollDelegate,
        SmoothScrollArea,
        EngravedLabel
    )
    from core._style_backdrop import StaticVolumetricBackdropWidget
    from core._style_glow_button import GlowActionButton, FlatActionButton, CrystalCache
    from core._style_shared_ticker import SharedTicker, shared_ticker


# Явный контракт экспорта. Гарантирует корректную работу `from core.style import *`
# и подсказывает IDE, какие классы доступны в этом модуле.
__all__ = [
    "Colors",
    "Styles",
    "LogStyles",
    "Texts",
    "Graphics",
    "SecurePasswordLineEdit",
    "AutoSaveIndicator",
    "GlassTile",
    "DebossedLineEdit",
    "SecureDebossedLineEdit",
    "StaticVolumetricBackdropWidget",
    "GlowActionButton",
    "FlatActionButton",
    "CrystalCache",
    "CachedLedPainter",
    "SharedTicker",
    "shared_ticker",
    "SmoothScrollBar",
    "SmoothScrollDelegate",
    "SmoothScrollArea",
    "EngravedLabel",
]

# Карта маршрутизации: связывает имя запрашиваемого класса с его физическим модулем.
_MODULE_MAP: dict[str, str] = {
    "Colors": "core._style_colors",
    "Styles": "core._style_qss",
    "LogStyles": "core._style_qss",
    "Texts": "core._style_texts",
    "Graphics": "core._style_graphics",
    "SecurePasswordLineEdit": "core._style_widgets",
    "AutoSaveIndicator": "core._style_widgets",
    "GlassTile": "core._style_widgets",
    "DebossedLineEdit": "core._style_widgets",
    "SecureDebossedLineEdit": "core._style_widgets",
    "CachedLedPainter": "core._style_widgets",
    "SmoothScrollBar": "core._style_widgets",
    "SmoothScrollDelegate": "core._style_widgets",
    "SmoothScrollArea": "core._style_widgets",
    "EngravedLabel": "core._style_widgets",
    "StaticVolumetricBackdropWidget": "core._style_backdrop",
    "GlowActionButton": "core._style_glow_button",
    "FlatActionButton": "core._style_glow_button",
    "CrystalCache": "core._style_glow_button",
    "SharedTicker": "core._style_shared_ticker",
    "shared_ticker": "core._style_shared_ticker",
}


def __getattr__(name: str) -> Any:
    """
    Магия PEP 562: Ленивая загрузка атрибутов модуля.
    Вызывается интерпретатором Python только в том случае, если запрошенное имя
    не найдено в текущем глобальном пространстве имен (globals).
    """
    if name in _MODULE_MAP:
        module_name = _MODULE_MAP[name]
        
        # Динамически импортируем целевой микро-модуль (цех)
        module = importlib.import_module(module_name)
        
        # Извлекаем нужный класс (например, Graphics с его малярным цехом)
        attribute = getattr(module, name)
        
        # Resource Guard (Оптимизация O(1)):
        # Кэшируем загруженный класс в глобальной области видимости этого фасада.
        # При следующем импорте этого же класса интерпретатор найдет его в globals()
        # и даже не вызовет __getattr__, обеспечивая максимальную производительность
        # при частых перерисовках интерфейса (например, Hover-эффекты кнопок).
        globals()[name] = attribute
        
        return attribute
        
    raise AttributeError(f"Модуль '{__name__}' не имеет атрибута '{name}'")


def __dir__() -> list[str]:
    """
    Переопределение встроенной функции dir() для модуля.
    Обеспечивает корректную работу автодополнения (IntelliSense) в современных IDE,
    показывая разработчику доступные классы, даже если они еще не были импортированы.
    """
    return __all__