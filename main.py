"""
Модуль: main.py
Назначение: Точка входа (Bootstrapper) приложения ADSProfile Manager.
Зона ответственности: Динамическое выравнивание путей (sys.path), инициализация
                      главного цикла событий Qt (QApplication), применение стартовой
                      темы (защита от белого мерцания), прогрев графического кэша,
                      сканирование плагинов и запуск системного сторожевого пса (Watchdog).
Интеграция: Является корнем проекта (Composition Root). Гарантирует работоспособность
            абсолютных импортов для всех вложенных модулей (core, system, moduls, gui)
            независимо от рабочей директории запуска.
"""

import sys
import traceback
from pathlib import Path

# =========================================================================
# 🚀 BOOTSTRAPPER (DYNAMIC PATH ALIGNMENT)
# =========================================================================
# Вычисляем абсолютный путь к корню проекта и принудительно ставим его
# в начало sys.path. Это гарантирует железобетонную работу абсолютных
# импортов (например, `from core.core import ...`) при любом способе запуска.
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Теперь безопасно импортируем сторонние библиотеки и наши модули.
# Импорт Graphics и CachedLedPainter безопасен, так как core.style использует
# ленивую загрузку (PEP 562) и не инстанцирует Qt-объекты до явного вызова.
from PySide6.QtWidgets import QApplication, QMessageBox
from core.style import Graphics, CachedLedPainter


def main() -> None:
    # 1. Создаем QApplication ПЕРЕД любыми другими импортами GUI.
    # Это фундаментальное требование Qt: цикл событий должен существовать до виджетов.
    app = QApplication(sys.argv)

    # 2. Применяем "темную тему" немедленно через централизованный стиль.
    # Это предотвращает "белое мигание" (White Flash) при старте тяжелых окон.
    Graphics.apply_boot_theme(app)
    
    # 2.5. Прогрев кэша (Pre-warming the Cache)
    # Выпекаем растры для умных светодиодов статуса. Это нужно делать СТРОГО ПОСЛЕ
    # инициализации QApplication, иначе Qt выбросит Segmentation Fault.
    CachedLedPainter.bake_all()

    try:
        # 3. Отложенные импорты (Lazy Import)
        # Предотвращает ошибку создания QObject/сигналов до инициализации приложения.
        # Также разрывает возможные циклические зависимости на этапе загрузки.
        from core.core import start_watchdog, stop_watchdog, plugin_manager
        from system.logger import logger
        from gui.main_window_gui import MainWindow

        logger.info(
            "Инициализация систем ADSProfile Manager...",
            profile_names=["GLOBAL"], category="SYSTEM"
        )

        # 4. Инициализация плагинов (Cold Start)
        # Сканируем папку /wallets до построения GUI, чтобы динамически
        # инжектировать ключи кошельков в системные константы (SSOT) и сейф паролей.
        plugin_manager.scan_plugins()

        # 5. Запускаем watchdog (перезапуск при глухом зависании)
        # Сторож будет следить за тем, чтобы главный поток Qt не залипал.
        # Пульс (ping) будет отправлять MainWindowPresenter.
        start_watchdog(interval=30)

        # 6. Создаем главное окно
        # Оно само восстановит геометрию из реестра, соберет изолированные панели
        # и запустит фоновые треды (Оракул газа, Телеметрия).
        main_window = MainWindow()
        main_window.show()

        logger.success(
            "Интерфейс успешно загружен. Конвейер готов к работе.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )

        # 7. Запускаем цикл событий (Event Loop)
        # Поток блокируется здесь до момента закрытия приложения.
        exit_code = app.exec()

        # 8. Корректно завершаем фоновые потоки (Graceful Shutdown)
        # MainWindowPresenter уже остановил таймеры и треды внутри closeEvent,
        # здесь мы гасим сам системный поток-монитор.
        stop_watchdog()
        
        logger.info(
            "Работа приложения штатно завершена. До связи.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        
        sys.exit(exit_code)

    except Exception:
        # Если произошла критическая ошибка при старте (например, опечатка в импортах),
        # показываем её пользователю, а не просто молча закрываемся (Anti-Silent Crash).
        error_msg = traceback.format_exc()
        try:
            # Пытаемся вывести в консоль (если приложение запущено из терминала)
            print(error_msg, file=sys.stderr)
            # Показываем диалоговое окно с трейсбеком
            QMessageBox.critical(
                None,
                "Критическая ошибка запуска",
                f"Не удалось запустить ADSProfile Manager:\n\n{error_msg}"
            )
        except Exception:
            # Если даже QMessageBox не может показаться (например, рухнул сам Qt), просто выходим
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()