"""
Модуль: core/core.py
Назначение: Генеральный штаб (Facade) приложения ADSProfile Manager.
Зона ответственности: Единая точка входа (реэкспорт) для всех системных утилит,
                      настроек, работы с реестром, безопасным хранилищем, фоновыми
                      воркерами (Watchdog, Gas Oracle, LavaMoatJITPatcher), плагинами
                      и системными константами.
Интеграция: Слой L3. Скрывает внутреннюю декомпозицию подмодулей (Decomposed Facade).
            Обеспечивает 100% обратную совместимость для внешних импортов.
            В конце файла инициализирует резолвер имен для системного логгера.
"""

# =============================================================================
# 1. ИМПОРТЫ ИЗ ИЗОЛИРОВАННЫХ МИКРОСЕРВИСОВ (FLAT NAMESPACE MAPPING)
# =============================================================================

# L0: Базовые константы, дефолты и шаблоны (SSOT)
from core._constants import (
    APP_NAME,
    APP_VERSION,
    USER_AGENT,
    APP_DEFAULTS,
    SETTINGS_DEFAULTS,
    ETH_RPC_ENDPOINTS,
    REG_PATH_CONFIG,
    REG_PATH_STATE,
    GEOIP_PRIMARY,
    GEOIP_SECONDARY,
    GEOIP_FALLBACK,
    PROXY_PROBE_TIMEOUT,
)

# L0: Глобальный маппер имен и метаданных профилей
from core._profiles_reg import (
    register_profile_names,
    update_profile_country,
    get_profile_name,
    get_profile_metadata,
    ProfileMetadata,
    export_cache_dict,
    import_cache_dict,
)

# L1: Безопасное хранилище (Windows Credential Manager)
from core._credentials import (
    CRED_PREFIX,
    WALLETS_KEYS,
)

# L2: Транзакционный менеджер реестра (HKCU)
from core._registry import (
    load_settings_from_registry,
    save_settings_to_registry,
    delete_settings_from_registry,
    open_registry_in_regedit,
    save_api_url,
    load_ui_geometry,
    save_ui_geometry,
)

# L1: Системный монитор жизнеспособности (Heartbeat)
from core._watchdog import (
    start_watchdog,
    stop_watchdog,
    ping_watchdog,
    is_watchdog_active,
)

# L1: Сетевой оракул котировок газа
from core._gas import (
    get_gas_prices_async,
    get_gas_prices_sync,
    format_gas_string,
)

# L1: Интеллектуальный патчер кэша расширений (LavaMoat JIT Bypass)
from core._patcher import (
    LavaMoatJITPatcher,
)

# L1: Системный диспетчер плагинов (Wallet Adapter Engine)
from core._plugin_manager import (
    plugin_manager,
    PluginManager,
)

# L2: Контракт плагинов кошельков (Интерфейс)
from moduls.ads._base_adapter import (
    BaseWalletAdapter,
)

# Системный логгер (для внедрения зависимостей)
from system.logger import logger


# =============================================================================
# 2. ПУБЛИЧНЫЙ КОНТРАКТ (API GATEWAY)
# =============================================================================
# Строго определяем, какие имена будут доступны при импорте `from core.core import *`.
# Это защищает внешние модули от случайного импорта внутренних утилит.

__all__ = [
    # Метаданные и константы
    "APP_NAME",
    "APP_VERSION",
    "USER_AGENT",
    "APP_DEFAULTS",
    "SETTINGS_DEFAULTS",
    "ETH_RPC_ENDPOINTS",
    "REG_PATH_CONFIG",
    "REG_PATH_STATE",
    "GEOIP_PRIMARY",
    "GEOIP_SECONDARY",
    "GEOIP_FALLBACK",
    "PROXY_PROBE_TIMEOUT",
    
    # Реестр профилей и метаданных
    "register_profile_names",
    "update_profile_country",
    "get_profile_name",
    "get_profile_metadata",
    "ProfileMetadata",
    "export_cache_dict",
    "import_cache_dict",
    
    # Хранилище паролей
    "CRED_PREFIX",
    "WALLETS_KEYS",
    
    # Работа с реестром Windows
    "load_settings_from_registry",
    "save_settings_to_registry",
    "delete_settings_from_registry",
    "open_registry_in_regedit",
    "save_api_url",
    "load_ui_geometry",
    "save_ui_geometry",
    
    # Сторожевой таймер
    "start_watchdog",
    "stop_watchdog",
    "ping_watchdog",
    "is_watchdog_active",
    
    # Оракул газа
    "get_gas_prices_async",
    "get_gas_prices_sync",
    "format_gas_string",
    
    # Патчер расширений (JIT Context Manager)
    "LavaMoatJITPatcher",
    
    # Плагинная система (Wallet Adapters)
    "plugin_manager",
    "PluginManager",
    "BaseWalletAdapter",
]


# =============================================================================
# 3. RUNTIME HOOKS (ИНИЦИАЛИЗАЦИЯ ЯДРА)
# =============================================================================

# Мост обратной совместимости (Dynamic Module Aliasing)
# Позволяет старым или сторонним плагинам кошельков, ссылающимся на старый путь
# 'moduls.ads.logic', импортировать утилиты и адаптеры без ModuleNotFoundError.
import sys
import moduls.ads as ads_pkg
from moduls.ads import _base_adapter, _dom_helpers, _utils

sys.modules['moduls.ads.logic'] = ads_pkg
sys.modules['moduls.ads.logic._base_adapter'] = _base_adapter
sys.modules['moduls.ads.logic._dom_helpers'] = _dom_helpers
sys.modules['moduls.ads.logic._utils'] = _utils

# Внедряем функцию разрешения имен профилей в системный логгер.
# Теперь логгер сможет прозрачно подменять безликие ID (например, "j9f82k")
# на читаемые имена (например, "Профиль_01") при выводе в UI.
logger.set_profile_resolver(get_profile_name)