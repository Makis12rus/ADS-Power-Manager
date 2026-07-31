"""
Модуль: wallets/backpack.py
Назначение: Плагин-адаптер для автоматической разблокировки кошелька Backpack.
Зона ответственности: Инкапсуляция логики обхода React-форм, предолетная проверка
                      состояния (уже разблокирован) и надежная отправка формы через
                      нажатие ENTER (с резервным кликом по кнопке).
                      Включает защиту от крашей фронтенда (Action Fallback Chain),
                      умное ожидание гидратации DOM-дерева и алгоритм
                      State Reconciliation для устранения ложных поражений.
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
from moduls.ads._dom_helpers import find_in_frames, react_set_val
from moduls.ads._utils import _sleep_or_cancel, OperationCancelled


class BackpackAdapter(BaseWalletAdapter):
    """
    Адаптер для кошелька Backpack.
    Реализует алгоритм проверки состояния, инъекции пароля и отправки формы через ENTER.
    Устойчив к изменениям локализации и верстки кнопок подтверждения.
    Оснащен Action Fallback Chain для защиты от летаргического сна React 18 и
    многоуровневым перехватом StaleElement для подтверждения успешного входа.
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
        Выполняет разблокировку Backpack Wallet с применением Action Fallback Chain и State Reconciliation.
        """
        try:
            self.check_cancel(cancel_event)
            
            # 1. Открываем новую вкладку и переходим по URL расширения
            driver.switch_to.new_window('tab')
            driver.get(self.extension_url)
            wait = WebDriverWait(driver, 12)
            short_wait = WebDriverWait(driver, 1.5)
            
            self.report_progress("UI", profile_name, progress_cb, cancel_event)
            
            # 2. Pre-flight State Detection (Проверка на уже разблокированный статус)
            _sleep_or_cancel(0.5, cancel_event)  # Ждем возможного редиректа
            current_url = driver.current_url.lower()
            
            # Если URL явно указывает на дашборд или токены
            if any(marker in current_url for marker in ["dashboard", "home", "tokens", "portfolio"]):
                self.log_info("Кошелек уже разблокирован (обнаружен дашборд в URL).", profile_name)
                return True, "already unlocked"
            
            # 3. Ищем поле ввода пароля (строго видимое)
            pwd_el, pwd_frame = find_in_frames(driver, By.XPATH, "//input[@type='password']", 4, cancel_event)
            
            if not pwd_el:
                # Если инпута нет, возможно, кошелек уже разблокирован, но URL не изменился (SPA routing)
                self.log_warning("Не нашли поле ввода пароля. Возможно, кошелек уже разблокирован или не настроен.", profile_name)
                return False, "Input not found"
            
            # Если элемент оказался внутри iframe
            if pwd_frame:
                driver.switch_to.frame(pwd_frame)
                pwd_el = wait.until(EC.visibility_of_element_located((By.XPATH, "//input[@type='password']")))
            
            self.check_cancel(cancel_event)
            
            # 3.5. Warm-up Delay (Прогрев React)
            # Даем тяжелому бандлу время на гидратацию DOM-дерева и инициализацию Service Worker'а
            self.log_info("Ожидаем гидратации React (Warm-up 1.0с)...", profile_name)
            _sleep_or_cancel(1.0, cancel_event)
            
            # 4. Фокус и инъекция пароля (обход React)
            driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", pwd_el)
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
                    # Ищем любую кнопку типа submit или содержащую типичные тексты
                    fallback_xpath = "//button[@type='submit' or contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'unlock') or contains(translate(., 'АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ', 'абвгдеёжзийклмнопрстуфхцчшщъыьэюя'), 'разблокировать')]"
                    btn = driver.find_element(By.XPATH, fallback_xpath)
                    
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
            self.log_error(f"Сбой при бурении Backpack: {e}", profile_name)
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