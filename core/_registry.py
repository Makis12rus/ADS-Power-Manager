"""
Модуль: core/_registry.py
Назначение: Транзакционный менеджер системного реестра Windows (HKCU).
Зона ответственности: Атомарное чтение и запись конфигурации приложения,
                      геометрии окон, кэша метаданных (State Hydration), кастомизации
                      фона (PCB Engine), параметров плавного скроллинга (Smooth Scroll)
                      и интеграция с хранилищем паролей. Обеспечивает миграцию старых
                      паролей в Credential Manager и строгое разделение веток Config и State.
Интеграция: Слой L2. Зависит от _constants.py (дефолты) и _credentials.py (сейф).
            Предоставляет безопасные заглушки при работе вне Windows.
            Адаптирован для работы с динамическими ключами плагинов.
"""

import os
import sys
from typing import Any

from system.logger import logger, log_action
from core._constants import (
    APP_DEFAULTS,
    SETTINGS_DEFAULTS,
    REG_PATH_CONFIG,
    REG_PATH_STATE
)
from core._credentials import (
    WALLETS_KEYS,
    _cm_available,
    _cred_write,
    _cred_read,
    _cred_delete
)

# =================== ИНИЦИАЛИЗАЦИЯ WINREG ===================

# Корневой путь (используется для миграции и полного удаления)
REG_PATH_ROOT: str = r"Software\ADSProfileManager"
winreg: Any = None

if sys.platform == "win32":
    try:
        import winreg
    except ImportError:
        pass


# =================== ВНУТРЕННИЕ ХЕЛПЕРЫ ===================

def _open_key(sub_path: str, create: bool = False, access: int | None = None) -> Any:
    """
    Хелпер для безопасного открытия ключа реестра по указанному пути.
    Возвращает объект ключа или None. Требует обязательного CloseKey в finally.
    """
    if not winreg:
        return None
    if access is None:
        access = winreg.KEY_ALL_ACCESS
    try:
        if create:
            return winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub_path)
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_path, 0, access)
    except FileNotFoundError:
        if create:
            return winreg.CreateKey(winreg.HKEY_CURRENT_USER, sub_path)
        return None
    except Exception as e:
        logger.warning(f"Ошибка доступа к реестру [{sub_path}]: {e}", profile_names=["GLOBAL"], category="SYSTEM")
        return None


def _read_all_values(reg_key: Any, target_dict: dict[str, str]) -> None:
    """Читает все значения из открытого ключа реестра и пишет их в словарь."""
    i = 0
    while True:
        try:
            name, value, _ = winreg.EnumValue(reg_key, i)
            target_dict[name] = str(value)
            i += 1
        except OSError:
            break  # Достигнут конец списка значений


def _recursive_delete_key(base_key: Any, sub_path: str) -> None:
    """
    Рекурсивное удаление ключа реестра со всеми вложенными подпапками.
    Предотвращает OSError [WinError 39] (The directory is not empty).
    """
    try:
        open_key = winreg.OpenKey(base_key, sub_path, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    except Exception:
        return

    try:
        while True:
            try:
                subkey_name = winreg.EnumKey(open_key, 0)
                _recursive_delete_key(open_key, subkey_name)
            except OSError:
                break
    finally:
        winreg.CloseKey(open_key)

    try:
        winreg.DeleteKey(base_key, sub_path)
    except OSError:
        pass


def _merge_business_defaults() -> dict[str, str]:
    """Объединяет глобальные флаги и настройки автоматизации (БЕЗ UI-геометрии)."""
    merged = dict(APP_DEFAULTS)
    merged.update(SETTINGS_DEFAULTS)
    return merged


def _save_settings_labels_only(settings: dict[str, str], target_path: str) -> None:
    """
    Сохраняет в реестр только «видимые» значения и безопасные метки cred:.
    Никогда не пишет реальные пароли, если доступен Credential Manager.
    """
    if not winreg:
        return
    
    reg = _open_key(target_path, create=True)
    if not reg:
        return
    
    try:
        # Итерируемся по переданным настройкам, чтобы не потерять динамические ключи плагинов
        for key, value in settings.items():
            if key in WALLETS_KEYS:
                v = str(value or "")
                # Если пароль есть, но это не метка, и доступен CM — ставим метку
                if v and not v.startswith("cred:"):
                    v = f"cred:{key}" if _cm_available() else ""
                winreg.SetValueEx(reg, key, 0, winreg.REG_SZ, v)
            else:
                winreg.SetValueEx(reg, key, 0, winreg.REG_SZ, str(value))
    except Exception as ex:
        logger.warning(
            f"Сбой транзакции записи в реестр: {ex}",
            profile_names=["GLOBAL"], category="SETTINGS"
        )
    finally:
        # Resource Guard: Гарантированное освобождение дескриптора ОС
        winreg.CloseKey(reg)


def _migrate_plain_passwords_to_cred(settings: dict[str, str]) -> dict[str, str]:
    """
    Миграция старых паролей из открытого реестра в Credential Manager.
    Выполняется прозрачно при загрузке настроек.
    """
    if not _cm_available():
        return settings
    
    changed = False
    for k in WALLETS_KEYS:
        v = str(settings.get(k, "") or "")
        if not v or v.startswith("cred:"):
            continue
        
        # Нашли открытый пароль -> прячем в бронированный сейф
        if _cred_write(k, v):
            settings[k] = f"cred:{k}"
            changed = True
    
    if changed:
        _save_settings_labels_only(settings, REG_PATH_CONFIG)
        logger.success(
            "Обнаружены открытые пароли. Успешно перенесены в бронированный сейф Windows.",
            profile_names=["GLOBAL"], category="SETTINGS"
        )
    return settings


def _expand_credentials_in_settings(settings: dict[str, str]) -> dict[str, str]:
    """
    Разворачивает метки cred:<key> в реальные пароли для использования в логике.
    """
    for k in WALLETS_KEYS:
        v = str(settings.get(k, "") or "")
        if v.startswith("cred:"):
            wallet_key = v.split(":", 1)[1]
            if _cm_available():
                real = _cred_read(wallet_key)
                settings[k] = real or ""
            else:
                settings[k] = ""
    return settings


# =================== ПУБЛИЧНЫЕ API НАСТРОЕК ===================

def load_settings_from_registry() -> dict[str, str]:
    """
    Загружает настройки из ветки Config, применяет дефолты, мигрирует старые пароли
    и разворачивает метки Credential Manager в реальные значения.
    Включает механизм Silent Migration из старого корня реестра.
    """
    settings = _merge_business_defaults().copy()
    
    if not winreg:
        logger.info("Работа вне Windows: используются дефолтные настройки.", profile_names=["GLOBAL"], category="SYSTEM")
        return _expand_credentials_in_settings(settings)
    
    reg_config = _open_key(REG_PATH_CONFIG, create=False)
    if reg_config:
        try:
            _read_all_values(reg_config, settings)
        finally:
            winreg.CloseKey(reg_config)
    else:
        # МИГРАЦИЯ: Ищем в старом корне, если Config еще не существует
        reg_root = _open_key(REG_PATH_ROOT, create=False)
        if reg_root:
            try:
                _read_all_values(reg_root, settings)
            finally:
                winreg.CloseKey(reg_root)
            
            # Тихо перегоняем данные в новую изолированную ветку
            logger.info("Выполняется миграция настроек в изолированную ветку Config...", profile_names=["GLOBAL"], category="SYSTEM")
            save_settings_to_registry(settings)
    
    # Проводим гигиенические процедуры с паролями
    settings = _migrate_plain_passwords_to_cred(settings)
    settings = _expand_credentials_in_settings(settings)
    
    return settings


@log_action("Сохранение настроек в реестр", category="SETTINGS")
def save_settings_to_registry(settings: dict[str, str]) -> tuple[bool, str]:
    """
    Сохраняет бизнес-настройки строго в ветку Config.
    Пароли маршрутизируются в Credential Manager.
    """
    if not winreg:
        return False, "Сохранение поддерживается только под Windows!"
    
    try:
        to_save_labels = dict(settings)
        merged = _merge_business_defaults()
        
        # Восстанавливаем недостающие ключи из дефолтов
        for k, v in merged.items():
            if k not in to_save_labels:
                to_save_labels[k] = v
        
        # Обработка паролей
        for k in WALLETS_KEYS:
            val = str(settings.get(k, "") or "")
            if val:
                if _cm_available():
                    if _cred_write(k, val):
                        to_save_labels[k] = f"cred:{k}"
                    else:
                        to_save_labels[k] = ""
                else:
                    to_save_labels[k] = ""
            else:
                _cred_delete(k)
                to_save_labels[k] = ""
        
        _save_settings_labels_only(to_save_labels, REG_PATH_CONFIG)
        return True, "Настройки успешно сохранены."
    except Exception as ex:
        return False, f"Ошибка сохранения: {ex}"


@log_action("Удаление настроек из реестра", category="SETTINGS")
def delete_settings_from_registry() -> tuple[bool, str]:
    """
    Полная зачистка следов приложения: рекурсивное удаление веток реестра и паролей из сейфа.
    """
    if not winreg:
        return False, "Удаление поддерживается только под Windows!"
    
    try:
        # Рекурсивно выжигаем ветки, чтобы избежать WinError 39
        _recursive_delete_key(winreg.HKEY_CURRENT_USER, REG_PATH_CONFIG)
        _recursive_delete_key(winreg.HKEY_CURRENT_USER, REG_PATH_STATE)
        _recursive_delete_key(winreg.HKEY_CURRENT_USER, REG_PATH_ROOT)
        
        # Вычищаем сейф
        if _cm_available():
            for k in WALLETS_KEYS:
                _cred_delete(k)
        
        return True, "Настройки и пароли безвозвратно удалены."
    except Exception as ex:
        return False, f"Ошибка удаления: {ex}"


def open_registry_in_regedit() -> tuple[bool, str]:
    """Открывает системный редактор реестра на ветке приложения."""
    if sys.platform != "win32":
        return False, "Только для Windows!"
    try:
        import ctypes
        regedit = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'regedit.exe')
        ShellExecute = ctypes.windll.shell32.ShellExecuteW
        # 1 = SW_SHOWNORMAL
        ret = ShellExecute(None, "runas", regedit, None, None, 1)
        if ret <= 32:
            return False, "Не удалось запустить regedit (недостаточно прав?)."
        return True, "Редактор реестра открыт."
    except Exception as ex:
        return False, str(ex)


def save_api_url(api_url: str) -> None:
    """Точечное обновление URL API AdsPower."""
    settings = load_settings_from_registry()
    settings["api_url"] = api_url
    save_settings_to_registry(settings)


# =================== UI GEOMETRY (ИЗОЛИРОВАННОЕ СОХРАНЕНИЕ) ===================

_UI_DEFAULTS: dict[str, str] = {
    "main_geometry": "",
    "logdock_geometry": "",
    "logdock_side": "right",
    "logdock_visible": "1",
    "log_theme": "REGULAR",
    "log_font_size": "10",
    "profile_order_map": "{}",  # Base64-encoded JSON для DND-сортировки профилей
    "profile_metadata_cache_b64": "",  # Base64-encoded JSON для гидратации профилей (Cold Start)
    
    # --- Premium PCB & Matte Glass Engine ---
    "bg_base_color": "#080C1F",
    "bg_pcb_color": "#1E2E4A",
    "bg_pcb_opacity": "85",
    "bg_pcb_thickness": "2",
    "bg_pcb_seed": "42",         # Детерминированность узора (Seed)
    "bg_pcb_complexity": "5",    # Плотность дорожек (Complexity)
    
    # --- Smooth Scroll Engine ---
    "smooth_scroll_duration": "200",
    "smooth_scroll_step": "120",
}


def load_ui_geometry() -> dict[str, str]:
    """Загрузка исключительно визуальных параметров интерфейса из ветки State."""
    result = dict(_UI_DEFAULTS)
    if not winreg:
        return result
    
    reg = _open_key(REG_PATH_STATE, create=False)
    if not reg:
        # Fallback: пытаемся прочитать из старого корня, если State еще не создан
        reg = _open_key(REG_PATH_ROOT, create=False)
        if not reg:
            return result
    
    try:
        for key in _UI_DEFAULTS:
            try:
                val, _ = winreg.QueryValueEx(reg, key)
                result[key] = str(val or "")
            except FileNotFoundError:
                pass
    finally:
        winreg.CloseKey(reg)
    
    return result


def save_ui_geometry(
        main_geometry_b64: str | None = None,
        logdock_geometry_b64: str | None = None,
        logdock_side: str | None = None,
        logdock_visible: bool | None = None,
        log_theme: str | None = None,
        log_font_size: str | None = None,
        profile_order_map_b64: str | None = None,
        profile_metadata_cache_b64: str | None = None,
        bg_base_color: str | None = None,
        bg_pcb_color: str | None = None,
        bg_pcb_opacity: str | None = None,
        bg_pcb_thickness: str | None = None,
        bg_pcb_seed: str | None = None,
        bg_pcb_complexity: str | None = None,
        smooth_scroll_duration: str | None = None,
        smooth_scroll_step: str | None = None
) -> tuple[bool, str]:
    """
    Атомарное сохранение визуальных параметров строго в ветку State.
    Физически отделено от save_settings_to_registry для предотвращения коррупции бизнес-данных.
    """
    if not winreg:
        return False, "Только для Windows!"
    
    reg = _open_key(REG_PATH_STATE, create=True)
    if not reg:
        return False, "Не удалось открыть реестр"
    
    try:
        if main_geometry_b64 is not None:
            winreg.SetValueEx(reg, "main_geometry", 0, winreg.REG_SZ, str(main_geometry_b64))
        if logdock_geometry_b64 is not None:
            winreg.SetValueEx(reg, "logdock_geometry", 0, winreg.REG_SZ, str(logdock_geometry_b64))
        if logdock_side is not None:
            side = "left" if str(logdock_side).lower() == "left" else "right"
            winreg.SetValueEx(reg, "logdock_side", 0, winreg.REG_SZ, side)
        if logdock_visible is not None:
            vis = "1" if bool(logdock_visible) else "0"
            winreg.SetValueEx(reg, "logdock_visible", 0, winreg.REG_SZ, vis)
        if log_theme is not None:
            winreg.SetValueEx(reg, "log_theme", 0, winreg.REG_SZ, str(log_theme))
        if log_font_size is not None:
            winreg.SetValueEx(reg, "log_font_size", 0, winreg.REG_SZ, str(log_font_size))
        if profile_order_map_b64 is not None:
            winreg.SetValueEx(reg, "profile_order_map", 0, winreg.REG_SZ, str(profile_order_map_b64))
        if profile_metadata_cache_b64 is not None:
            winreg.SetValueEx(reg, "profile_metadata_cache_b64", 0, winreg.REG_SZ, str(profile_metadata_cache_b64))
            
        # --- Premium PCB & Matte Glass Engine ---
        if bg_base_color is not None:
            winreg.SetValueEx(reg, "bg_base_color", 0, winreg.REG_SZ, str(bg_base_color))
        if bg_pcb_color is not None:
            winreg.SetValueEx(reg, "bg_pcb_color", 0, winreg.REG_SZ, str(bg_pcb_color))
        if bg_pcb_opacity is not None:
            winreg.SetValueEx(reg, "bg_pcb_opacity", 0, winreg.REG_SZ, str(bg_pcb_opacity))
        if bg_pcb_thickness is not None:
            winreg.SetValueEx(reg, "bg_pcb_thickness", 0, winreg.REG_SZ, str(bg_pcb_thickness))
        if bg_pcb_seed is not None:
            winreg.SetValueEx(reg, "bg_pcb_seed", 0, winreg.REG_SZ, str(bg_pcb_seed))
        if bg_pcb_complexity is not None:
            winreg.SetValueEx(reg, "bg_pcb_complexity", 0, winreg.REG_SZ, str(bg_pcb_complexity))
            
        # --- Smooth Scroll Engine ---
        if smooth_scroll_duration is not None:
            winreg.SetValueEx(reg, "smooth_scroll_duration", 0, winreg.REG_SZ, str(smooth_scroll_duration))
        if smooth_scroll_step is not None:
            winreg.SetValueEx(reg, "smooth_scroll_step", 0, winreg.REG_SZ, str(smooth_scroll_step))
        
        return True, "OK"
    except Exception as ex:
        return False, str(ex)
    finally:
        winreg.CloseKey(reg)