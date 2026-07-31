"""
Модуль: moduls/ads/_wallet_unlocker.py
Назначение: Оркестратор автоматизации Web3-кошельков через Selenium.
Зона ответственности: Инициализация веб-драйвера, динамическая загрузка плагинов
                      кошельков (Wallet Adapters), прохождение циклов разблокировки
                      и гарантированное высвобождение ресурсов (Resource Guard).
Интеграция: Вызывается из фасада ads_logic.py. Делегирует низкоуровневую работу
            изолированным плагинам из папки `wallets/`. Регистрирует процессы
            в `_process_manager.py`. Поддерживает как пакетный "холодный старт",
            так и точечное "горячее бурение" (Targeted Hot Unlock) сразу нескольких целей.
            Является частью плоского пакета `moduls/ads/`.
"""

import time
import threading
from typing import Any

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# Строгие абсолютные импорты ядра
from system.logger import logger
from core.core import plugin_manager

# Строгие относительные импорты внутри плоского пакета ADS
from ._utils import _sleep_or_cancel, OperationCancelled, ProgressCB
from ._dom_helpers import close_unwanted_tabs
from ._process_manager import register_driver, unregister_driver


def _safe_quit_driver(driver: Any) -> None:
    """
    Атомарное и безопасное уничтожение сессии драйвера.
    Гарантирует удаление из реестра активных процессов и подавляет
    любые сетевые ошибки при закрытии сокетов.
    """
    if not driver:
        return
    try:
        unregister_driver(driver)
        driver.quit()
    except Exception as e:
        # Подавляем ошибки вроде ConnectionRefusedError, если процесс уже убит ОС
        logger.warning(
            f"[Resource Guard] Ошибка при закрытии драйвера (процесс уже мертв?): {e}",
            profile_names=["GLOBAL"], category="SYSTEM"
        )


# ======================= Главный Оркестратор Разблокировки =======================

def unlock_wallets_for_profile(
        ws_url: str,
        chrome_driver_path: str,
        profile_name: str,
        user_id: str,
        settings: dict[str, Any],
        progress_cb: ProgressCB = None,
        cancel_event: threading.Event | None = None,
        target_plugin_ids: list[str] | None = None
) -> None:
    """
    Главная точка входа для автоматизации кошельков профиля.
    Инициализирует Selenium, управляет жизненным циклом драйвера и запускает
    делегирование задач плагинам-адаптерам.
    
    :param target_plugin_ids: Если указан список ID, воркер переходит в режим "Снайпера" (Горячий запуск)
                              и разблокирует только эти конкретные кошельки, игнорируя остальные.
    """
    # Проверка наличия MetaMask (исторически важно для обхода LavaMoat)
    if not plugin_manager.get_manifest("metamask"):
        logger.warning(
            "MetaMask не найден в загруженных плагинах. Если это не ошибка, игнорируйте. "
            "Иначе проверьте наличие metamask.json в папке /wallets.",
            profile_names=[profile_name], category="SYSTEM"
        )
    
    driver = None
    mode_str = f"Горячее бурение ({', '.join(target_plugin_ids)})" if target_plugin_ids else "Холодный старт"
    
    with logger.block(f"Автоматизация кошельков {profile_name} [{mode_str}]", profile_names=[profile_name], category="WALLET"):
        try:
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Прервано пользователем до старта Selenium")
            
            logger.info(
                f"[DEBUG] [wallet_unlocker] Инициализируем сессию для {profile_name}, "
                f"отправляем воркера бурить DOM-формы через плагины...",
                profile_names=[profile_name], category="WALLET"
            )
            
            opts = Options()
            opts.add_experimental_option("debuggerAddress", ws_url)
            
            srv = Service(executable_path=chrome_driver_path)
            driver = webdriver.Chrome(service=srv, options=opts)
            
            # Resource Guard: Регистрируем драйвер для возможности аварийной остановки
            register_driver(driver, user_id)
            
            driver.implicitly_wait(0.2)
            _sleep_or_cancel(1.5, cancel_event)
            
            # Зачищаем мусорные рекламные вкладки AdsPower (только при холодном старте, чтобы не мешать юзеру при горячем)
            if not target_plugin_ids:
                close_unwanted_tabs(driver)
            
            # Внедряем паузу "прогрева" (Warm-up) для инициализации Service Workers расширений
            logger.info(
                "Прогрев браузера: даем расширениям 2 секунды на инициализацию баз данных...",
                profile_names=[profile_name], category="WALLET"
            )
            _sleep_or_cancel(2.0, cancel_event)
            
            try:
                retries = int(settings.get("wallet_retry_count", "3"))
            except Exception:
                retries = 3
            retries = max(1, min(10, retries))
            
            # Запускаем боевой цикл разблокировки через плагины.
            # КРИТИЧНО: Принимаем обновленную ссылку на driver, так как он мог быть пересоздан внутри!
            unlocked, failed, driver = unlock_wallets_robust(
                driver, ws_url, chrome_driver_path, profile_name, user_id,
                settings, progress_cb, retries, cancel_event, target_plugin_ids
            )
            
            if unlocked:
                logger.success(
                    f"Успешно разблокированы: {', '.join(unlocked)}",
                    profile_names=[profile_name], category="WALLET"
                )
            if failed:
                # Soft Failure: логируем предупреждение, но не роняем конвейер
                logger.warning(
                    f"Частичный успех. Не дались: {', '.join(failed)}. Оставляем браузер открытым для ручного ввода.",
                    profile_names=[profile_name], category="WALLET"
                )
        
        except OperationCancelled:
            logger.info(
                "Разблокировка прервана пользователем (Kill Switch / Graceful Exit).",
                profile_names=[profile_name], category="WALLET"
            )
            raise
        except Exception as e:
            logger.error(f"Ошибка Selenium: {e}", profile_names=[profile_name], category="WALLET")
            raise e
        finally:
            # Resource Guard: Железобетонное высвобождение памяти и дескрипторов.
            # Вызов quit() корректно закрывает отладочную сессию и убивает chromedriver.exe,
            # оставляя окно AdsPower открытым (так как мы подключались через debuggerAddress).
            _safe_quit_driver(driver)


def recreate_driver(ws_url: str, chrome_driver_path: str) -> Any | None:
    """
    Попытка переподключиться к отладочному порту браузера, если драйвер отвалился
    из-за сетевого сбоя или краша вкладки.
    """
    try:
        opts = Options()
        opts.add_experimental_option("debuggerAddress", ws_url)
        
        srv = Service(executable_path=chrome_driver_path)
        driver = webdriver.Chrome(service=srv, options=opts)
        driver.implicitly_wait(0.2)
        time.sleep(1.5)
        return driver
    except Exception as e:
        logger.warning(
            f"Некромантия не удалась. Не смогли переподключиться к браузеру: {e}",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        return None


# ======================= Циклы попыток и маршрутизация =======================

def unlock_wallets_robust(
        driver: Any,
        ws_url: str,
        chrome_driver_path: str,
        profile_name: str,
        user_id: str,
        settings: dict[str, Any],
        progress_cb: ProgressCB,
        retry_count: int,
        cancel_event: threading.Event | None = None,
        target_plugin_ids: list[str] | None = None
) -> tuple[list[str], list[str], Any]:
    """
    Проходит по всем загруженным плагинам кошельков, проверяет настройки и делегирует
    им процесс разблокировки. Обеспечивает изоляцию сбоев: падение одного плагина
    не прерывает работу с остальными кошельками.
    
    Возвращает: (Список успешных, Список проваленных, Актуальный инстанс драйвера)
    """
    unlocked: list[str] = []
    failed: list[str] = []
    
    # Получаем список всех доступных кошельков от диспетчера плагинов
    manifests = plugin_manager.get_all_manifests()
    
    for manifest in manifests:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Прервано пользователем")
        
        plugin_id = manifest.get("id", "")
        name = manifest.get("name", "UnknownWallet")
        pwd_key = manifest.get("password_key", "")
        
        # Снайперский режим: если указаны конкретные цели, игнорируем все остальные кошельки
        if target_plugin_ids and plugin_id not in target_plugin_ids:
            continue
        
        # Холодный старт: проверяем тумблер. В горячем режиме тумблер игнорируется (юзер сам нажал Play на пульте)
        is_enabled = settings.get(f"unlock_{plugin_id}_enabled", "1") == "1"
        if not target_plugin_ids and not is_enabled:
            logger.info(
                f"Тумблер для {name} выключен. Пропускаем кошелек.",
                profile_names=[profile_name], category="WALLET"
            )
            continue
        
        # Извлекаем расшифрованный пароль
        pwd = settings.get(pwd_key, "")
        if not pwd:
            logger.warning(
                f"Пароль для {name} отсутствует в сейфе. Пропускаем.",
                profile_names=[profile_name], category="WALLET"
            )
            continue
        
        # Оптимизация: Ленивая загрузка класса плагина вынесена за пределы цикла ретраев (O(1) компиляция)
        try:
            AdapterClass = plugin_manager.load_adapter_class(plugin_id)
        except Exception as e:
            logger.error(
                f"Не удалось скомпилировать плагин {name}: {e}",
                profile_names=[profile_name], category="WALLET"
            )
            failed.append(name)
            continue

        ok = False
        driver_ok = (driver is not None)
        
        for i in range(1, retry_count + 1):
            if cancel_event and cancel_event.is_set():
                raise OperationCancelled("Прервано пользователем")
            
            # Если драйвер умер на предыдущем шаге, пытаемся его воскресить
            if not driver_ok:
                logger.info(
                    f"chromedriver откинул копыта. Вызываем некроманта для переподключения ({i}/{retry_count})...",
                    profile_names=[profile_name], category="SYSTEM"
                )
                
                # Безопасно вычищаем старый труп из реестра
                _safe_quit_driver(driver)
                del driver
                
                driver = recreate_driver(ws_url, chrome_driver_path)
                if driver:
                    register_driver(driver, user_id)
                    driver_ok = True
                    logger.success("Успешно переподключились к браузеру!", profile_names=[profile_name], category="SYSTEM")
                else:
                    break
            
            adapter = None
            try:
                with logger.block(f"Разблокировка {name} ({i}/{retry_count})", profile_names=[profile_name], category="WALLET"):
                    
                    # Инициализируем инстанс адаптера
                    adapter = AdapterClass(manifest)
                    
                    # Делегируем грязную работу плагину
                    success, msg = adapter.unlock(driver, pwd, profile_name, progress_cb, cancel_event)
                    
                    if success:
                        if msg == "already unlocked":
                            logger.info(f"{name} уже разблокирован, идем дальше.", profile_names=[profile_name], category="WALLET")
                        unlocked.append(name)
                        ok = True
                        break
                    else:
                        driver_ok = False
                        logger.warning(
                            f"{name}: неудача ({msg}). chromedriver упрямится.",
                            profile_names=[profile_name], category="WALLET"
                        )
            except OperationCancelled:
                raise
            except Exception as e:
                logger.error(f"{name}: сбой плагина {e}", profile_names=[profile_name], category="WALLET")
                driver_ok = False
                _sleep_or_cancel(1.0, cancel_event)
            finally:
                # Resource Guard: Явно удаляем инстанс адаптера, чтобы сборщик мусора
                # мог немедленно вычистить его из оперативной памяти.
                if adapter:
                    del adapter
        
        if not ok:
            failed.append(name)
    
    # КРИТИЧНО: Возвращаем актуальную ссылку на драйвер, так как он мог быть пересоздан!
    return unlocked, failed, driver