<h1 align="center">🛡️ ADSProfile Manager</h1>
<h3 align="center">Модульная десктопная платформа на PySide6 для комплексного управления, автоматизации и скриптинга антидетект-профилей AdsPower</h3>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.13+-3776AB?style=for-the-badge&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/PySide6-Qt6-41CD52?style=for-the-badge&logo=qt&logoColor=white" />
  <img src="https://img.shields.io/badge/Selenium-4.0+-43B02A?style=for-the-badge&logo=selenium&logoColor=white" />
  <img src="https://img.shields.io/badge/Web3-Automation-F16822?style=for-the-badge&logo=ethereum&logoColor=white" />
  <img src="https://img.shields.io/badge/Security-WinAPI_DPAPI-0078D6?style=for-the-badge&logo=windows&logoColor=white" />
</p>

---

## 📌 Описание проекта

**ADSProfile Manager** — это высоконадежная локальная десктопная платформа (**PySide6**), спроектированная по модульному принципу для автоматизации парка антидетект-браузеров AdsPower. 

Приложение объединяет многопоточную параллельную работу веб-драйверов, интеллектуальный фоновый разблокировщик Web3-кошельков с обходом теневого DOM (Shadow DOM) и встроенную среду разработки (IDE) для написания и отладки пользовательских Selenium-сценариев.

---

## 🚀 Ключевые возможности и Архитектура

### 🖥️ 1. Реактивный GUI и Панель Управления (PySide6)
* **Архитектура:** Использование паттерна **Mediator** (`main_window_gui.py`) и пассивных представлений (Passive View) для полной изоляции UI от тяжелогрузных процессов.
* **Виртуальная карусель профилей:** Эффективный виртуальный скролл (`RecyclerScrollArea`) с O(1) обходом элементов и встроенной 3D-левитацией карточек без нагрузки на CPU.
* **Централизованное логирование:** Потокобезопасный `Logger` с фильтрацией дубликатов, нормализацией ошибок и поддержкой динамических HTML-тем (Regular, Matrix, Neon).

### 🤖 2. Автоматизация Web3-кошельков (Plug-and-Play)
* **Поддержка кошельков:** Динамические картриджи-плагины для **MetaMask**, **Rabby Wallet** и **OKX**.
* **Обход Shadow DOM:** Сканирование и работа с элементами внутри теневых деревьев и iframe-фреймов.
* **React Input Fix (`JS_REACT_SET_VALUE`):** Кастомный JavaScript-инжектор, генерирующий события `KeyboardEvent` и `InputEvent` для гарантированного обхода защиты современных SPA-форм при вводе паролей.

### 🔒 3. Безопасность и Хранение Ключей (WinAPI)
* **Windows Credential Manager:** Прямая интеграция с системным сейфом Windows через `pywin32` и `ctypes`. Пароли от кошельков и ключи API шифруются на уровне ОС и никогда не хранятся в открытом виде на диске или в реестре.
* **Автоматическая миграция:** Бесшовный перенос устаревших ключей из реестра в зашифрованное хранилище WCM при первом запуске.

### ⚙️ 4. Встроенная IDE (Режим AUTO)
* **Среда отладки:** Интегрированный редактор кода с подсветкой синтаксиса Python на базе `Pygments` и интеллектуальной обработкой автоотступов.
* **Изолированная песочница:** Запуск пользовательских Selenium-скриптов в отдельных дочерних подпроцессах (`subprocess.Popen`) с межпроцессным обменом данными (IPC), исключающий падение основного GUI при ошибках в скрипте.

### 🛡️ 5. Отказоустойчивость и Охрана ОЗУ (Resource Guard)
* **Strict Rate Limiting (1 RPS):** Глобальный замок и асинхронный семафор (`AdsAsyncClient`) для соблюдения жестких лимитов AdsPower API без банов и таймаутов.
* **Watchdog Heartbeat Protocol:** Фоновый сторожевой таймер (`_watchdog.py`), контролирующий отзывчивость главного Qt-потока и предотвращающий зависания приложения.
* **Resource Guard:** Автоматическая зачистка заброшенных процессов `chromedriver` (`emergency_stop_selenium`), принудительный вызов `gc.collect()` и закрытие сессий `driver.quit()` в блоках `finally`.

---

## 🏗️ Структура проекта

<pre>
ads_profile_manager/
┣━━ 📄 main.py                  # Точка входа и инициализация приложения
┣━━ 📂 core/                    # Ядро системы (Константы, Реестр, WinAPI, Стили)
┃   ┣━━ 📄 _constants.py        # Единый источник истины (SSOT)
┃   ┣━━ 📄 _credentials.py      # Безопасный сейф Windows Credential Manager
┃   ┗━━ 📄 _watchdog.py         # Сторожевой процесс мониторинга GUI
┣━━ 📂 system/                  # Потокобезопасная служба логирования
┣━━ 📂 gui/                     # Главный интерфейс и компоновщики окон
┣━━ 📂 moduls/                  # Изолированные модули
┃   ┣━━ 📂 ads/                 # Режим ADS (Карусель, Автоматизация, Логика, API)
┃   ┗━━ 📂 auto/                # Режим AUTO (IDE, Редактор Pygments, Песочница)
┗━━ 📂 wallets/                 # Автономные плагины разблокировки кошельков
</pre>

---
---

## 🛠️ Установка и Запуск

### Требования
* **ОС:** Windows 10/11 (для нативной работы с Windows Credential Manager и реестром).
* **Python:** 3.13+

### Запуск из исходного кода
1. Клонировать репозиторий:
   git clone https://github.com/Makis12rus/ADS-Power-Manager.git
   cd ADS-Power-Manager

2. Установить зависимости:
   pip install -r requirements.txt

3. Запустить приложение:
   python main.py
