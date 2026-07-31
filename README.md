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
ADS-Power-Manager/
┣━━ 📄 main.py                  # Точка входа и инициализация приложения
┣━━ 📄 requirements.txt         # Зависимости проекта (PySide6, Selenium, PyWin32)
┣━━ 📄 README.md                # Документация проекта
┣━━ 📄 .gitignore               # Исключения Git
┃
┣━━ 📂 core/                    # Ядро системы (Константы, Реестр, WinAPI, Стили)
┃   ┣━━ 📄 __init__.py
┃   ┣━━ 📄 core.py              # Фасад ядра и реэкспорт системных API
┃   ┣━━ 📄 _constants.py        # Единый источник истины (SSOT)
┃   ┣━━ 📄 _plugin_manager.py   # Диспетчер ленивой загрузки плагинов кошельков
┃   ┣━━ 📄 _profiles_reg.py     # Реестр маппинга профилей
┃   ┣━━ 📄 _registry.py         # Транзакционная работа с реестром Windows (HKCU)
┃   ┣━━ 📄 _credentials.py      # Безопасный сейф Windows Credential Manager
┃   ┣━━ 📄 _watchdog.py         # Сторожевой процесс мониторинга GUI
┃   ┣━━ 📄 _gas.py              # Сетевой оракул газа и комиссий
┃   ┣━━ 📄 _patcher.py          # Патчер кэша расширений
┃   ┣━━ 📄 style.py             # Фасад стилей и графики (PEP 562 Gateway)
┃   ┣━━ 📄 _style_colors.py     # Палитра цветов
┃   ┣━━ 📄 _style_qss.py        # QSS-стили виджетов
┃   ┣━━ 📄 _style_backdrop.py   # Объемный фон с процедурным шумом (Dithering)
┃   ┣━━ 📄 _style_texts.py      # HTML-шаблоны текстов
┃   ┣━━ 📄 _style_graphics.py   # Векторный генератор иконок и флагов
┃   ┣━━ 📄 _style_widgets.py    # Кастомные Qt-виджеты
┃   ┣━━ 📄 _style_glow_button.py # Векторные кнопки с эффектом вдавливания
┃   ┗━━ 📄 _style_circular_progress.py # Векторные кольца прогресса
┃
┣━━ 📂 system/                  # Системные службы
┃   ┣━━ 📄 __init__.py
┃   ┗━━ 📄 logger.py            # Потокобезопасный логгер с фильтрацией дубликатов
┃
┣━━ 📂 gui/                     # Презентационный слой и главное окно
┃   ┣━━ 📄 __init__.py
┃   ┣━━ 📄 main_window_gui.py   # Главное окно MainWindow (Mediator)
┃   ┣━━ 📄 main_window_presenter.py # Контроллер окон и фоновые треды
┃   ┣━━ 📄 mode_bar.py          # Пульт переключения режимов
┃   ┣━━ 📄 sticky_dock.py       # Липкий док панели логов
┃   ┗━━ 📄 info_panel.py        # Информационная панель
┃
┣━━ 📂 moduls/                  # Бизнес-модули
┃   ┣━━ 📄 __init__.py
┃   ┣━━ 📂 ads/                 # Режим ADS (Управление профилями AdsPower)
┃   ┃   ┣━━ 📄 __init__.py
┃   ┃   ┣━━ 📄 ads_gui.py       # Фасад UI элементов ADS
┃   ┃   ┣━━ 📄 ads_logic.py     # Диспетчер конвейера ADS
┃   ┃   ┣━━ 📄 ads_log_gui.py   # Панель логов и фильтрация
┃   ┃   ┣━━ 📄 flow_layout.py   # Математика переноса чипсов
┃   ┃   ┣━━ 📄 profile_card_view.py # Виртуальный скролл карусели
┃   ┃   ┣━━ 📄 profile_panel.py # Панель управления профилями
┃   ┃   ┣━━ 📄 profile_presenter.py # Медиатор управления состояниями
┃   ┃   ┣━━ 📄 profile_model_manager.py # Модель данных профилей
┃   ┃   ┣━━ 📄 profile_execution_engine.py # Многопоточный движок выполнения
┃   ┃   ┣━━ 📄 settings_panel.py# Настройки и форма параметров
┃   ┃   ┣━━ 📄 _base_adapter.py # Контракт BaseWalletAdapter
┃   ┃   ┣━━ 📄 _api_client.py   # Async HTTP клиент AdsPower API (1 RPS)
┃   ┃   ┣━━ 📄 _telemetry.py    # Автономный радар мониторинга O(1)
┃   ┃   ┣━━ 📄 _process_manager.py # Реестр воркеров и Kill Switch
┃   ┃   ┣━━ 📄 _wallet_unlocker.py # Координатор плагинов разблокировки
┃   ┃   ┗━━ 📄 _dom_helpers.py  # Поиск в Shadow DOM и JS-инъекции React
┃   ┗━━ 📂 auto/                # Режим AUTO (IDE & Скриптинг)
┃       ┣━━ 📄 __init__.py
┃       ┣━━ 📄 auto_gui.py      # Редактор кода с подсветкой Pygments
┃       ┗━━ 📄 auto_logic.py    # Песочница выполнения сценариев (Subprocess IPC)
┃
┗━━ 📂 wallets/                 # Автономные плагины разблокировки кошельков
    ┣━━ 📄 __init__.py
    ┣━━ 📄 metamask.json        # Паспорт MetaMask
    ┣━━ 📄 metamask.py          # Модуль разблокировки MetaMask
    ┣━━ 📄 rabby.json           # Паспорт Rabby Wallet
    ┣━━ 📄 rabby.py             # Модуль разблокировки Rabby Wallet
    ┣━━ 📄 okx.json              # Паспорт OKX Wallet
    ┣━━ 📄 okx.py                # Модуль разблокировки OKX Wallet
    ┣━━ 📄 phantom.json          # Паспорт Phantom Wallet
    ┣━━ 📄 phantom.py            # Модуль разблокировки Phantom Wallet
    ┣━━ 📄 keplr.json            # Паспорт Keplr Wallet
    ┣━━ 📄 keplr.py              # Модуль разблокировки Keplr Wallet
    ┣━━ 📄 backpack.json         # Паспорт Backpack Wallet
    ┗━━ 📄 backpack.py           # Модуль разблокировки Backpack Wallet
</pre>

---

## 🛠️ Установка и Запуск

### Требования
* **ОС:** Windows 10/11 (для нативной работы с Windows Credential Manager и реестром).
* **Python:** 3.13+

### Запуск из исходного кода

1. **Клонировать репозиторий и перейти в папку:**
<pre><code>git clone https://github.com/Makis12rus/ADS-Power-Manager.git
cd ADS-Power-Manager</code></pre>

2. **Установить зависимости:**
<pre><code>pip install -r requirements.txt</code></pre>

3. **Запустить приложение:**
<pre><code>python main.py</code></pre>
