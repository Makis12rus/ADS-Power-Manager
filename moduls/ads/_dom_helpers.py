"""
Модуль: moduls/ads/_dom_helpers.py
Назначение: Низкоуровневые утилиты для работы с DOM-деревом через Selenium.
Зона ответственности: Инъекция JavaScript для обхода React-форм, рекурсивный поиск
                      видимых элементов в iframe и Shadow DOM, зачистка мусорных вкладок.
Интеграция: Вызывается из плагинов кошельков (папка `wallets/`). Изолирован от GUI
            и бизнес-логики. Не хранит состояния (Stateless).
            Является частью плоского пакета `moduls/ads/`.
"""

import time
import threading
from typing import Any

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException

# Строгие абсолютные импорты ядра
from system.logger import logger

# Строгие относительные импорты внутри плоского пакета ADS
from ._utils import _sleep_or_cancel, OperationCancelled

# ======================= JavaScript Инъекции =======================

# Скрипт для безопасного ввода текста в React-поля (с эмуляцией событий Keyboard/Input).
# Обычный send_keys() часто игнорируется современными SPA-фреймворками.
JS_REACT_SET_VALUE = """
    const el = arguments[0], s = arguments[1];
    el.focus();
    const set = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    // Сброс значения
    set.call(el, '');
    el.dispatchEvent(new Event('input', {bubbles:true}));
    // Посимвольный ввод
    for (const ch of s) {
      el.dispatchEvent(new KeyboardEvent('keydown', {key: ch, bubbles: true}));
      set.call(el, el.value + ch);
      el.dispatchEvent(new InputEvent('input', {data: ch, inputType: 'insertText', bubbles: true, cancelable: true}));
      el.dispatchEvent(new KeyboardEvent('keyup', {key: ch, bubbles: true}));
    }
    // Финальное событие изменения
    el.dispatchEvent(new Event('change', {bubbles:true}));
"""

# Скрипт для рекурсивного поиска ВИДИМЫХ инпутов пароля сквозь теневой DOM (Shadow DOM).
# Незаменим для сложных расширений вроде OKX Wallet.
JS_SHADOW_SEARCH = """
    return (function(){
      function isVisible(e) {
        return e && (e.offsetWidth > 0 || e.offsetHeight > 0 || e.getClientRects().length > 0);
      }
      function deep(root){
        let el = root.querySelector("input[data-testid='okd-input'][type='password']");
        if (isVisible(el)) return el;
        el = root.querySelector("form[data-testid='okd-form'] input[type='password']");
        if (isVisible(el)) return el;
        el = root.querySelector("input[type='password']");
        if (isVisible(el)) return el;
        const nodes = root.querySelectorAll('*');
        for (const n of nodes){
          if (n.shadowRoot){
            const f = deep(n.shadowRoot);
            if (f) return f;
          }
        }
        return null;
      }
      return deep(document);
    })();
"""


# ======================= Утилиты работы с вкладками =======================

def close_unwanted_tabs(driver: Any) -> None:
    """
    Санитайзер вкладок. Закрывает рекламный мусор, который AdsPower
    любит открывать при старте профиля (iplocation, proxy-чекеры и т.д.).
    Гарантированно возвращает фокус на первую (главную) вкладку.
    """
    try:
        if not driver.window_handles:
            return
        
        main_handle = driver.window_handles[0]
        
        # Перебираем все вкладки, кроме первой
        for handle in driver.window_handles[1:]:
            try:
                driver.switch_to.window(handle)
                url = driver.current_url.lower()
                if any(x in url for x in ["iplocation", "browserleak", "adspower.com", "proxy"]):
                    logger.info(
                        "chromedriver опять наоткрывал рекламных вкладок. Закрываем мусор...",
                        profile_names=["GLOBAL"], category="SYSTEM"
                    )
                    driver.close()
            except WebDriverException:
                # Вкладка могла уже закрыться сама (например, скриптом самого AdsPower)
                pass
        
        # Возвращаем фокус домой
        driver.switch_to.window(main_handle)
    except Exception as e:
        logger.warning(
            f"Сбой при зачистке фоновых вкладок: {e}",
            profile_names=["GLOBAL"], category="SYSTEM"
        )


# ======================= Утилиты поиска и взаимодействия =======================

def react_set_val(driver: Any, el: Any, val: str) -> None:
    """
    Программная эмуляция ввода текста для обхода защиты React/Vue форм.
    """
    driver.execute_script(JS_REACT_SET_VALUE, el, val)


def wait_okx_btn(driver: Any, timeout: int = 6, cancel_event: threading.Event | None = None) -> bool:
    """
    Специфичное ожидание активации кнопки разблокировки в OKX Wallet.
    Кнопка часто заблокирована атрибутами disabled/aria-disabled до окончания анимаций.
    """
    end_time = time.time() + timeout
    while time.time() < end_time:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Прервано пользователем")
            
        try:
            btn = driver.find_element(By.XPATH, "//button[@data-testid='okd-button']")
            if not btn.get_attribute("disabled") and btn.get_attribute("aria-disabled") != "true":
                return True
        except Exception:
            pass
        
        _sleep_or_cancel(0.15, cancel_event)
    
    return False


def find_in_frames(
        driver: Any,
        by: str,
        loc: str,
        timeout: int = 8,
        cancel_event: threading.Event | None = None
) -> tuple[Any | None, Any | None]:
    """
    Интеллектуальный поиск ВИДИМОГО элемента (обычно поля пароля).
    Алгоритм:
    1. Быстрый поиск в основном документе (visibility_of_element_located).
    2. Перебор всех iframe.
    3. Поиск по специфичному локатору.
    4. Глубокое сканирование Shadow DOM через JS-инъекцию с проверкой offsetWidth.

    Возвращает кортеж: (Найденный_Элемент, Фрейм_в_котором_найден)
    """
    start_time = time.monotonic()
    
    # 1. Быстрый поиск стандартного инпута в основном документе
    try:
        el = WebDriverWait(driver, 1.5).until(
            EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
        )
        return el, None
    except Exception:
        pass
    
    # Собираем все фреймы на странице
    try:
        frames = driver.find_elements(By.TAG_NAME, "iframe")
    except WebDriverException:
        frames = []
    
    # 2. Поиск стандартного инпута внутри фреймов
    for fr in frames:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Прервано пользователем")
        if time.monotonic() - start_time > timeout:
            break
        
        try:
            driver.switch_to.frame(fr)
            el = WebDriverWait(driver, 1.0).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, "input[type='password']"))
            )
            driver.switch_to.default_content()
            return el, fr
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    
    if time.monotonic() - start_time > timeout:
        return None, None
    
    # 3. Поиск по специфичному локатору (переданному в аргументах) в основном документе
    try:
        el = WebDriverWait(driver, 1.5).until(EC.visibility_of_element_located((by, loc)))
        return el, None
    except Exception:
        pass
    
    # 4. Поиск по специфичному локатору внутри фреймов
    for fr in frames:
        if cancel_event and cancel_event.is_set():
            raise OperationCancelled("Прервано пользователем")
        if time.monotonic() - start_time > timeout:
            break
        
        try:
            driver.switch_to.frame(fr)
            el = WebDriverWait(driver, 1.5).until(EC.visibility_of_element_located((by, loc)))
            driver.switch_to.default_content()
            return el, fr
        except Exception:
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
    
    # 5. Тяжелая артиллерия: рекурсивный поиск сквозь Shadow DOM с проверкой видимости
    try:
        el = driver.execute_script(JS_SHADOW_SEARCH)
        if el:
            logger.info(
                "Нашли упрямый видимый инпут в глубинах Shadow DOM!",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            return el, None
    except Exception:
        pass
    
    return None, None