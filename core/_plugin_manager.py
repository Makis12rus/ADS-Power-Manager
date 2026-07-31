"""
Модуль: core/_plugin_manager.py
Назначение: Системный диспетчер плагинов (Wallet Adapter Plugin Engine).
Зона ответственности: Динамическое сканирование папки `/wallets`, чтение легких
                      JSON-манифестов при старте (для генерации GUI) и ленивая
                      компиляция тяжелого Python-кода плагинов строго в момент
                      запуска автоматизации (Resource Guard).
Интеграция: Слой L1. Инжектирует найденные ключи в `_constants.py` и `_credentials.py`.
            Вызывается из `ads_gui.py` (для отрисовки) и `_wallet_unlocker.py` (для работы).
"""

import json
import threading
import importlib.util
import inspect
from pathlib import Path
from typing import Any

from system.logger import logger
import core._constants as consts
import core._credentials as creds


class PluginManager:
    """
    Потокобезопасный Синглтон для управления жизненным циклом плагинов Web3-кошельков.
    Обеспечивает концепцию Plug-and-Play: закинул файл в папку -> кошелек появился в UI.
    """
    _instance: 'PluginManager | None' = None
    _lock: threading.RLock = threading.RLock()
    
    def __new__(cls) -> 'PluginManager':
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._manifests = {}  # type: dict[str, dict[str, Any]]
                cls._instance._adapter_classes = {}  # type: dict[str, type[Any]]
                cls._instance._scanned = False
        return cls._instance
    
    def scan_plugins(self) -> None:
        """
        Холодный старт: сканирует директорию /wallets, читает .json паспорта
        и динамически регистрирует ключи в системных настройках (SSOT).
        Сам Python-код плагинов на этом этапе НЕ загружается для экономии ОЗУ.
        """
        with self._lock:
            if self._scanned:
                return
            
            # Вычисляем путь к папке wallets (на уровне корня проекта)
            root_dir = Path(__file__).resolve().parent.parent
            wallets_dir = root_dir / "wallets"
            
            if not wallets_dir.exists():
                try:
                    wallets_dir.mkdir(parents=True, exist_ok=True)
                    logger.info("Создана папка /wallets для плагинов-картриджей.", profile_names=["GLOBAL"], category="SYSTEM")
                except Exception as e:
                    logger.error(f"Не удалось создать папку /wallets: {e}", profile_names=["GLOBAL"], category="SYSTEM")
                    return
            
            loaded_count = 0
            
            for json_file in wallets_dir.glob("*.json"):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        manifest = json.load(f)
                    
                    plugin_id = json_file.stem
                    name = manifest.get("name")
                    pwd_key = manifest.get("password_key")
                    
                    if not name or not pwd_key:
                        logger.warning(
                            f"Плагин {plugin_id} оказался бракованным (отсутствует name или password_key в манифесте).",
                            profile_names=["GLOBAL"], category="SYSTEM"
                        )
                        continue
                    
                    # Сохраняем ID плагина внутрь манифеста для удобства GUI
                    manifest["id"] = plugin_id
                    self._manifests[plugin_id] = manifest
                    
                    # =========================================================
                    # DYNAMIC SSOT INJECTION (Инъекция в ядро)
                    # =========================================================
                    
                    # 1. Регистрация ключа в Credential Manager
                    if hasattr(creds, "register_wallet_key"):
                        creds.register_wallet_key(pwd_key)
                    elif isinstance(getattr(creds, "WALLETS_KEYS", None), list):
                        if pwd_key not in creds.WALLETS_KEYS:
                            creds.WALLETS_KEYS.append(pwd_key)
                    
                    # 2. Регистрация дефолтных настроек (Тумблер + Пароль)
                    if isinstance(getattr(consts, "SETTINGS_DEFAULTS", None), dict):
                        enable_key = f"unlock_{plugin_id}_enabled"
                        if enable_key not in consts.SETTINGS_DEFAULTS:
                            consts.SETTINGS_DEFAULTS[enable_key] = "1"
                        if pwd_key not in consts.SETTINGS_DEFAULTS:
                            consts.SETTINGS_DEFAULTS[pwd_key] = ""
                    
                    loaded_count += 1
                
                except Exception as e:
                    logger.error(
                        f"Ошибка чтения манифеста {json_file.name}: {e}",
                        profile_names=["GLOBAL"], category="SYSTEM"
                    )
            
            self._scanned = True
            logger.success(
                f"PluginManager: Обнаружено и готово к работе {loaded_count} плагинов кошельков.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
    
    def get_all_manifests(self) -> list[dict[str, Any]]:
        """
        Возвращает список всех валидных манифестов.
        Используется в GUI для динамической отрисовки чекбоксов и полей ввода.
        """
        with self._lock:
            if not self._scanned:
                self.scan_plugins()
            # Возвращаем копию списка, чтобы защитить внутреннее состояние
            return list(self._manifests.values())
    
    def get_manifest(self, plugin_id: str) -> dict[str, Any] | None:
        """Получить манифест конкретного плагина по его ID."""
        with self._lock:
            if not self._scanned:
                self.scan_plugins()
            return self._manifests.get(plugin_id)
    
    def load_adapter_class(self, plugin_id: str) -> type[Any]:
        """
        Ленивая загрузка (Lazy Load) Python-кода плагина.
        Компилирует .py файл в оперативную память только в момент реальной необходимости.
        Кэширует класс адаптера для последующих вызовов.

        :param plugin_id: Имя файла плагина без расширения (например, 'metamask').
        :return: Класс, унаследованный от BaseWalletAdapter.
        """
        with self._lock:
            if plugin_id in self._adapter_classes:
                return self._adapter_classes[plugin_id]
            
            # Локальный импорт базового класса для предотвращения циклических зависимостей
            # на этапе загрузки самого PluginManager.
            # КРИТИЧНО: Используем новый плоский путь после ликвидации папки logic/
            from moduls.ads._base_adapter import BaseWalletAdapter
            
            root_dir = Path(__file__).resolve().parent.parent
            py_file = root_dir / "wallets" / f"{plugin_id}.py"
            
            if not py_file.exists():
                raise FileNotFoundError(f"Файл логики плагина не найден: {py_file}")
            
            logger.info(
                f"PluginManager: Загружаем тяжелую артиллерию из {py_file.name} в оперативную память...",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            
            # Динамический импорт модуля в изолированное пространство имен
            spec = importlib.util.spec_from_file_location(f"wallet_plugin_{plugin_id}", str(py_file))
            if spec is None or spec.loader is None:
                raise ImportError(f"Не удалось создать спецификацию для {py_file}")
            
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
            except Exception as e:
                raise RuntimeError(f"Ошибка компиляции плагина {plugin_id}: {e}")
            
            # Интроспекция: ищем класс, который наследуется от BaseWalletAdapter
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if issubclass(obj, BaseWalletAdapter) and obj is not BaseWalletAdapter:
                    self._adapter_classes[plugin_id] = obj
                    return obj
            
            raise TypeError(f"В файле {py_file.name} не найден класс, наследующий BaseWalletAdapter.")


# Глобальный экземпляр диспетчера плагинов
plugin_manager = PluginManager()