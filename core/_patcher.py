"""
Модуль: core/_patcher.py
Назначение: Эфемерный транзакционный патчер кэша расширений (LavaMoat Scuttling JIT Bypass).
Зона ответственности: Динамическое сканирование директории MetaMask, точечная временная
                      модификация всех JS-файлов, содержащих конфигурацию песочницы SES,
                      и гарантированный откат (Rollback) при завершении сессии Selenium.
Интеграция: Слой L1. Оборачивает фазу запуска браузера в moduls/ads/ads_logic.py.
            Полностью изолирован от GUI, не хранит состояние в реестре.
            Поддерживает режим "призрака" (enabled=False) для экономии I/O.
"""

import os
import re
import sys
import shutil
import threading
from pathlib import Path
from typing import Any

from system.logger import logger

# =================== LAVAMOAT JIT PATCHER ===================

class LavaMoatJITPatcher:
    """
    Транзакционный менеджер контекста для хирургического обхода защиты LavaMoat (SES).
    Алгоритм "Эфемерный ниндзя v2.0 (Multi-Target)":
    1. Находим корень расширения MetaMask для конкретного профиля.
    2. Auto-Heal: ищем и восстанавливаем старые .bak файлы (если прошлый запуск крашнулся).
    3. Сканируем все .js файлы. Если находим `scuttleGlobalThis`:
       - Делаем бэкап (.bak).
       - Атомарно меняем `enabled:!0` на `enabled:!1` (выключаем scuttling).
       - Запоминаем путь в карту сессии.
    4. Отдаем управление Selenium.
    5. При выходе из блока `with` — проходим по карте сессии и восстанавливаем оригиналы.
    
    Если `enabled=False`, работает как nullcontext (ничего не делает).
    """

    def __init__(self, user_id: str, enabled: bool = True) -> None:
        self.user_id: str = str(user_id).strip()
        self.enabled: bool = enabled
        self._patched_files: list[Path] = []
        self._lock = threading.Lock()
        
        # Константа расширения MetaMask (Chrome Web Store ID)
        self._mm_id: str = "nkbihfbeogaeaoehlefnkodbefgpgknn"

    def _locate_extension_dir(self) -> Path | None:
        """
        Умный поиск папки кэша AdsPower и корневой директории расширения.
        Возвращает Path или None, если расширение не установлено.
        """
        possible_bases: list[Path] = [
            Path.home() / ".adspower_global",
            Path.home() / ".ADSPOWER_GLOBAL"
        ]
        
        # Гасим системные исключения Windows при поллинге дисков
        if sys.platform == "win32":
            for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                possible_bases.append(Path(f"{letter}:/.adspower_global"))
                possible_bases.append(Path(f"{letter}:/.ADSPOWER_GLOBAL"))
        
        base_dir: Path | None = None
        for pb in possible_bases:
            try:
                if pb.exists() and pb.is_dir():
                    base_dir = pb
                    break
            except OSError:
                continue
                
        if not base_dir:
            return None
            
        cache_dir = base_dir / "cache"
        target_ext_dir: Path | None = None
        
        # Точечный поиск папки профиля по ID
        try:
            if cache_dir.exists() and cache_dir.is_dir():
                for profile_folder in cache_dir.iterdir():
                    try:
                        if profile_folder.is_dir() and profile_folder.name.startswith(self.user_id):
                            target_ext_dir = profile_folder / "Default" / "Extensions" / self._mm_id
                            break
                    except OSError:
                        pass
        except OSError:
            return None
            
        if target_ext_dir and target_ext_dir.exists():
            return target_ext_dir
            
        return None

    def _auto_heal(self, ext_dir: Path) -> None:
        """
        Протокол самолечения. Ищет сиротские .bak файлы от прошлых аварийных
        завершений (например, отключение питания) и восстанавливает их.
        """
        try:
            orphans = list(ext_dir.rglob("*.js.bak"))
            for bak_file in orphans:
                original_js = bak_file.with_suffix("")  # Убираем .bak
                try:
                    os.replace(bak_file, original_js)
                    logger.info(
                        f"[LavaPatcher] Auto-Heal: Восстановлен сиротский файл {original_js.name}",
                        profile_names=[self.user_id], category="SYSTEM"
                    )
                except OSError as e:
                    logger.warning(
                        f"[LavaPatcher] Auto-Heal: Не удалось восстановить {bak_file.name}: {e}",
                        profile_names=[self.user_id], category="SYSTEM"
                    )
        except Exception as e:
            logger.warning(
                f"[LavaPatcher] Ошибка при выполнении протокола Auto-Heal: {e}",
                profile_names=[self.user_id], category="SYSTEM"
            )

    def __enter__(self) -> 'LavaMoatJITPatcher':
        """
        Вход в транзакцию: сканирование, бэкап и атомарное отключение LavaMoat во всех средах.
        Если патчер отключен (enabled=False), мгновенно возвращает управление.
        """
        if not self.enabled:
            return self

        with self._lock:
            try:
                if not self.user_id:
                    return self
                    
                ext_dir = self._locate_extension_dir()
                
                if not ext_dir:
                    logger.info(
                        f"[LavaPatcher] Кэш MetaMask для {self.user_id} не найден. Пропускаем патчинг.",
                        profile_names=[self.user_id], category="SYSTEM"
                    )
                    return self

                # Шаг 1: Лечим последствия возможных прошлых крашей
                self._auto_heal(ext_dir)
                
                # Шаг 2: Рекурсивное сканирование всех JS-файлов расширения
                js_files = ext_dir.rglob("*.js")
                
                for js_file in js_files:
                    if not js_file.is_file():
                        continue
                        
                    try:
                        content = js_file.read_text(encoding="utf-8")
                        
                        # Fast-Path: пропускаем файлы без конфигурации LavaMoat (экономия CPU)
                        if "scuttleGlobalThis" not in content:
                            continue
                            
                        # Символьно-симметричная замена (!0 на !1), сохраняющая структуру бандла
                        new_content, count = re.subn(
                            r'(scuttleGlobalThis["\']?\s*:\s*\{\s*enabled["\']?\s*:\s*)(?:!0|true)',
                            r'\g<1>!1',
                            content,
                            flags=re.IGNORECASE
                        )
                        
                        if count > 0:
                            bak_js = js_file.with_suffix(".js.bak")
                            tmp_js = js_file.with_suffix(".js.tmp")
                            
                            # Бэкап оригинала
                            shutil.copy2(js_file, bak_js)
                            
                            # Атомарная запись измененного файла
                            tmp_js.write_text(new_content, encoding="utf-8")
                            os.replace(tmp_js, js_file)
                            
                            # Запоминаем путь для отката
                            self._patched_files.append(js_file)
                            
                    except Exception as file_err:
                        logger.warning(
                            f"[LavaPatcher] Ошибка при обработке файла {js_file.name}: {file_err}",
                            profile_names=[self.user_id], category="SYSTEM"
                        )
                
                if self._patched_files:
                    logger.info(
                        f"[LavaPatcher] Операция 'Эфемерный ниндзя': обезврежено {len(self._patched_files)} файлов. LavaMoat спит.",
                        profile_names=[self.user_id], category="SYSTEM"
                    )
                
            except Exception as e:
                logger.warning(
                    f"[LavaPatcher] Сбой при подготовке к мульти-патчингу: {e}",
                    profile_names=[self.user_id], category="SYSTEM"
                )
                
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """
        Выход из транзакции: 100% гарантированный откат к оригинальному коду.
        Вызывается автоматически при завершении Selenium воркера или экстренном СТОПе.
        Если патчер отключен (enabled=False), мгновенно возвращает управление.
        """
        if not self.enabled:
            return None

        with self._lock:
            restored_count = 0
            try:
                for js_file in self._patched_files:
                    bak_js = js_file.with_suffix(".js.bak")
                    if bak_js.exists():
                        try:
                            # Атомарно возвращаем оригинал на законное место
                            os.replace(bak_js, js_file)
                            restored_count += 1
                        except OSError as e:
                            logger.error(
                                f"[LavaPatcher] КРИТИЧЕСКИЙ СБОЙ ОТКАТА! Не удалось восстановить {js_file.name}: {e}",
                                profile_names=[self.user_id], category="SYSTEM"
                            )
                            
                if restored_count > 0:
                    logger.success(
                        f"[LavaPatcher] Следы заметены: {restored_count} оригинальных файлов восстановлено из бэкапов.",
                        profile_names=[self.user_id], category="SYSTEM"
                    )
            except Exception as e:
                logger.error(
                    f"[LavaPatcher] Неизвестная ошибка при откате изменений: {e}",
                    profile_names=[self.user_id], category="SYSTEM"
                )
            finally:
                # Resource Guard: Очищаем карту сессии
                self._patched_files.clear()
                
        # Мы НЕ подавляем исключения (OperationCancelled или WebDriverException),
        # чтобы они корректно передались в оркестратор и обновили статус в GUI.
        return None