"""
Модуль: wallets/metamask.py
Назначение: Плагин-адаптер для автоматической разблокировки кошелька MetaMask.
Зона ответственности: Инкапсуляция логики обхода React-форм, умное ожидание (Smart Wait)
                      гидратации DOM-дерева и надежная отправка формы через ENTER
                      с учетом асинхронного обновления состояний React 18.
                      Включает защиту от крашей фронтенда (Action Fallback Chain)
                      и алгоритм State Reconciliation для устранения ложных поражений
                      при мгновенном размонтировании DOM-дерева.
Интеграция: Загружается динамически через `core/_plugin_manager.py`. Наследует
            строгий контракт `BaseWalletAdapter`.
"""

import time
import threading
from typing import Any, Callable

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

from moduls.ads._base_adapter import BaseWalletAdapter
from moduls.ads._dom_helpers import react_set_val
from moduls.ads._utils import _sleep_or_cancel, OperationCancelled


class MetaMaskAdapter(BaseWalletAdapter):
    """
    Адаптер для кошелька MetaMask (Лиса).
    Реализует алгоритм проверки состояния, инъекции пароля и отправки формы через ENTER.
    Использует data-testid для железобетонной привязки к элементам, Action Fallback Chain
    для защиты от летаргического сна React 18 и многоуровневый перехват StaleElement
    для подтверждения успешного входа.
    """
    
    def unlock(
            self,
            driver: Any,
            password: str,
            profile_name: str,
            progress_cb: Callable[[str], None] | None = None,
            cancel_event: threading.Event | None = None
    ) -> tuple[bool, str]:
        """
        Выполняет разблокировку MetaMask с учетом задержек рендеринга React и резервным планом.
        """
        try:
            self.check_cancel(cancel_event)
            
            # 1. Открываем новую вкладку и переходим по URL расширения
            driver.switch_to.new_window('tab')
            driver.get(self.extension_url)
            short_wait = WebDriverWait(driver, 1.5)
            
            self.report_progress("UI", profile_name, progress_cb, cancel_event)
            
            # 2. Pre-flight State Detection (Проверка на уже разблокированный статус)
            # Даем роутеру расширения время на редирект
            _sleep_or_cancel(1.0, cancel_event)
            current_url = driver.current_url.lower()
            
            if "unlock" not in current_url and any(marker in current_url for marker in ["home", "dashboard"]):
                self.log_info("Кошелек уже разблокирован (маркер unlock отсутствует в URL).", profile_name)
                return True, "already unlocked"
            
            # 3. Smart Wait: Ищем поле ввода пароля по data-testid
            # Даем тяжелому React-приложению до 15 секунд на гидратацию и рендеринг DOM
            pwd_xpath = "//input[@data-testid='unlock-password']"
            self.log_info("Ожидаем рендеринга React-формы (Smart Wait до 15с)...", profile_name)
            
            try:
                pwd_el = WebDriverWait(driver, 15).until(
                    EC.visibility_of_element_located((By.XPATH, pwd_xpath))
                )
            except TimeoutException:
                self.check_cancel(cancel_event)
                self.log_warning("Форма не прогрузилась за 15с. Либо лиса зависла, либо кошелек уже разблокирован.", profile_name)
                return False, "Input not found (Timeout)"
            
            self.check_cancel(cancel_event)
            
            # 3.5. Warm-up Delay (Прогрев React)
            # Даем тяжелому бандлу время на гидратацию DOM-дерева и инициализацию Service Worker'а
            self.log_info("Ожидаем гидратации React (Warm-up 1.0с)...", profile_name)
            _sleep_or_cancel(1.0, cancel_event)
            
            # 4. Фокус и инъекция пароля (обход React)
            driver.execute_script("arguments[0].focus();", pwd_el)
            react_set_val(driver, pwd_el, password)
            
            self.report_progress("Ввод", profile_name, progress_cb, cancel_event)
            
            # 4.5. Humanized React Flow: Микро-пауза для синхронизации State
            self.log_info("Пароль введен. Ждем 0.5с, чтобы React успел обновить внутренний стейт...", profile_name)
            _sleep_or_cancel(0.5, cancel_event)
            
            # 5. Удар по ENTER (План А - Амортизированный)
            self.log_info("Отправляем форму через Keys.ENTER...", profile_name)
            try:
                pwd_el.send_keys(Keys.ENTER)
            except Exception as react_crash:
                # Глушим панику. React 18 часто падает при конфликте синтетических и доверенных событий.
                self.log_warning(f"React отверг системный ENTER (ошибка: {type(react_crash).__name__}). Игнорируем и ждем План Б...", profile_name)
            
            self.report_progress("Вход", profile_name, progress_cb, cancel_event)
            
            # 6. Проверка успеха и План Б (State Reconciliation)
            success = False
            try:
                # План А: Ждем штатного исчезновения инпута пароля
                short_wait.until_not(EC.visibility_of(pwd_el))
                success = True
            except TimeoutException:
                self.check_cancel(cancel_event)
                self.log_info("Инпут не исчез после ENTER. Задействуем резервный JS-клик по кнопке...", profile_name)
                try:
                    # Ищем кнопку по data-testid ИЛИ по тексту 'unlock' (case-insensitive)
                    btn_xpath = "//button[@data-testid='unlock-submit' or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'unlock')]"
                    btn = driver.find_element(By.XPATH, btn_xpath)
                    
                    # JS-клик минует систему событий React
                    driver.execute_script("arguments[0].click();", btn)
                    
                    try:
                        # Снова ждем исчезновения инпута
                        short_wait.until_not(EC.visibility_of(pwd_el))
                        success = True
                    except StaleElementReferenceException:
                        # КРИТИЧНО: Инпут испарился из DOM во время ожидания — это УСПЕХ!
                        self.log_info("Инпут испарился из DOM после клика. Вход подтвержден!", profile_name)
                        success = True
                        
                except StaleElementReferenceException:
                    # КРИТИЧНО: Сама кнопка или инпут исчезли в момент поиска/клика — это УСПЕХ!
                    self.log_info("Элементы входа уничтожены компонентом React. Вход подтвержден!", profile_name)
                    success = True
                except Exception as fallback_err:
                    self.log_warning(f"Резервный клик не удался: {fallback_err}", profile_name)
                    
            except StaleElementReferenceException:
                # КРИТИЧНО: Инпут испарился из DOM сразу после ENTER — это УСПЕХ!
                self.log_info("Инпут испарился из DOM сразу после ENTER. Вход подтвержден!", profile_name)
                success = True
            
            if not success:
                return False, "Пароль не подошел или интерфейс завис на анимации загрузки"
            
            return True, "ok"
        
        except OperationCancelled:
            # Пробрасываем сигнал остановки выше, чтобы оркестратор убил процесс
            raise
        except Exception as e:
            self.log_error(f"Сбой при бурении MetaMask: {e}", profile_name)
            return False, str(e)
        finally:
            # 7. Resource Guard: Уборка мусора
            # Независимо от результата, мы обязаны закрыть вкладку кошелька и вернуть фокус
            try:
                driver.switch_to.default_content()
                if len(driver.window_handles) > 1:
                    driver.close()
                if driver.window_handles:
                    driver.switch_to.window(driver.window_handles[0])
            except Exception:
                pass