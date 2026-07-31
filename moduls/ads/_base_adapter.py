"""
Модуль: moduls/ads/_base_adapter.py
Назначение: Абстрактный контракт (Интерфейс) для плагинов Web3-кошельков.
Зона ответственности: Декларация базового класса BaseWalletAdapter, который
                      обязан реализовать каждый подключаемый кошелек (Plug-and-Play).
                      Обеспечивает стандартизацию методов разблокировки и логирования.
Интеграция: Слой L2 (Logic). Наследуется внешними плагинами из папки `wallets/`.
            Используется диспетчером `_wallet_unlocker.py` для полиморфного вызова.
            Является частью плоского пакета `moduls/ads/`.
"""

import abc
import threading
from typing import Any, Callable

from system.logger import logger

# Строгие относительные импорты внутри плоского пакета ADS
from ._utils import OperationCancelled, _progress


class BaseWalletAdapter(abc.ABC):
    """
    Абстрактный базовый класс для всех адаптеров Web3-кошельков.
    Работает как "USB-розетка": ядро приложения не знает, как устроен конкретный кошелек,
    оно лишь дергает стандартизированный метод unlock() и ждет результат.
    """
    
    def __init__(self, manifest: dict[str, Any]) -> None:
        """
        Инициализация адаптера на основе данных из его .json паспорта.

        :param manifest: Словарь с метаданными плагина (name, extension_url, password_key и т.д.)
        """
        self.manifest = manifest
        self.name = str(manifest.get("name", "UnknownWallet"))
        self.password_key = str(manifest.get("password_key", ""))
        self.extension_url = str(manifest.get("extension_url", ""))
        self.has_update_modal = bool(manifest.get("has_update_modal", False))
    
    @abc.abstractmethod
    def unlock(
            self,
            driver: Any,
            password: str,
            profile_name: str,
            progress_cb: Callable[[str], None] | None = None,
            cancel_event: threading.Event | None = None
    ) -> tuple[bool, str]:
        """
        Главный боевой метод плагина. Выполняет поиск инпутов и инъекцию пароля.
        ОБЯЗАТЕЛЕН к реализации во всех дочерних классах.

        :param driver: Экземпляр Selenium WebDriver.
        :param password: Расшифрованный пароль из Credential Manager.
        :param profile_name: Имя текущего профиля (для красивых логов).
        :param progress_cb: Коллбэк для передачи микро-статусов в GUI.
        :param cancel_event: Флаг экстренной остановки (Kill Switch).
        :return: Кортеж (Успех: bool, Сообщение: str).
        """
        pass
    
    def onboard(
            self,
            driver: Any,
            seed_phrase: str,
            password: str,
            profile_name: str,
            progress_cb: Callable[[str], None] | None = None,
            cancel_event: threading.Event | None = None
    ) -> tuple[bool, str]:
        """
        Метод для первичного импорта кошелька по сид-фразе.
        Опционален. Если плагин его не поддерживает, выбрасывает исключение.
        """
        raise NotImplementedError(f"Кошелек {self.name} пока не поддерживает автоматический импорт сид-фразы.")
    
    # =========================================================================
    # ВСТРОЕННЫЕ ХЕЛПЕРЫ ДЛЯ АВТОРОВ ПЛАГИНОВ (DRY)
    # =========================================================================
    
    def check_cancel(self, cancel_event: threading.Event | None) -> None:
        """
        Проверка рубильника экстренной остановки.
        Если пользователь нажал СТОП, мгновенно прерывает работу плагина.
        """
        if cancel_event and cancel_event.is_set():
            self.log_warning("Работа прервана пользователем (Kill Switch активирован).", "GLOBAL")
            raise OperationCancelled("Прервано пользователем")
    
    def report_progress(
            self,
            step_name: str,
            profile_name: str,
            progress_cb: Callable[[str], None] | None,
            cancel_event: threading.Event | None = None
    ) -> None:
        """
        Безопасная отправка микро-статуса в GUI с одновременной проверкой Kill Switch.
        """
        self.check_cancel(cancel_event)
        _progress(progress_cb, f"{profile_name}: {self.name} - {step_name}", cancel_event)
    
    def log_info(self, message: str, profile_name: str) -> None:
        """Стандартизированный вывод успешных действий в системный логгер."""
        logger.info(f"[{self.name}] {message}", profile_names=[profile_name], category="WALLET")
    
    def log_warning(self, message: str, profile_name: str) -> None:
        """Стандартизированный вывод предупреждений (например, не найден элемент)."""
        logger.warning(f"[{self.name}] {message}", profile_names=[profile_name], category="WALLET")
    
    def log_error(self, message: str, profile_name: str) -> None:
        """Стандартизированный вывод критических ошибок плагина."""
        logger.error(f"[{self.name}] {message}", profile_names=[profile_name], category="WALLET")