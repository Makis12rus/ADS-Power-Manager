# =========================
# 📝 Файл: ads_logic.py
# =========================

import time
import json
import urllib.request
import urllib.error
import sys
import threading
import math
import re
from typing import Any, Dict, List, Optional, Tuple, Union, Callable

from core import (
    ping_watchdog,
    load_settings_from_registry,
    save_settings_to_registry,
    delete_settings_from_registry,
    open_registry_in_regedit,
    save_api_url,
)
from logger import logger  # централизованный логгер

# Тип колбэка прогресса: один вызов == один микрошаг
ProgressCB = Optional[Callable[[str], None]]  # progress_cb("текст шага")

# ========== Новое: «мягкая отмена» ==========
class OperationCancelled(Exception):
    """Исключение для мягкой отмены операций пользователем."""
    pass


# ======================= Вспомогательные =======================

def parse_profile_status(status: Optional[str]) -> Tuple[str, str]:
    if status == "Active":
        return "🟢 Активен", "INFO"
    elif status in ("Closed", "Inactive"):
        return "⚫ Закрыт", "INFO"
    elif status is None or status == "":
        return "🔴 Неизвестно", "ERROR"
    elif isinstance(status, str) and status.startswith("Error"):
        return "🔴 Ошибка", "ERROR"
    else:
        return f"🔴 Ошибка ({status})", "ERROR"


# --------- ДОБАВЛЕНО: безопасные ключи сортировки профилей ---------
def _natural_int(text: Any) -> Optional[int]:
    """
    Пытается извлечь последнее число из строки.
    Нужен как самый крайний fallback для profile_id.
    """
    try:
        if isinstance(text, (int, float)):
            return int(text)
        s = str(text or "")
        m = re.search(r'(\d+)(?!.*\d)', s)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _parse_created_ts(v: Any) -> Optional[float]:
    """
    Универсальный парсер времени создания в секундах эпохи.
    Поддерживает int/float (сек или мс), цифровые строки 10/13 знаков и
    строку вида 'YYYY-MM-DD...' (приблизительно).
    Возвращает None, если распарсить нельзя.
    """
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            val = float(v)
            # эвристика: 13-значные миллисекунды
            if val > 1e12:
                val = val / 1000.0
            return val
        s = str(v).strip()
        if not s:
            return None
        if s.isdigit():
            val = float(s)
            if len(s) >= 13:
                val = val / 1000.0
            return val
        # очень мягкая попытка распарсить ISO-подобную дату
        if re.match(r'^\d{4}-\d{2}-\d{2}', s):
            import time as _t
            try:
                st = _t.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                try:
                    st = _t.strptime(s[:10], "%Y-%m-%d")
                except Exception:
                    return None
            return float(_t.mktime(st))
    except Exception:
        return None
    return None


def _sort_profiles_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Стабильная сортировка профилей: по возрастанию времени создания.
    Ключи, которые пробуем:
      1) created_time / create_time / createdAt / created / createdTime / gmt_create
      2) serial_number (как число)
      3) числовой хвост profile_id (как число)
    Пустые значения уезжают в конец.
    """
    # Подбираем поля времени создания в разумном порядке
    ts_fields = ["created_time", "create_time", "createdAt", "created", "createdTime", "gmt_create"]

    def key_fn(p: Dict[str, Any]) -> Tuple[int, int, int]:
        # время создания
        ts: Optional[float] = None
        for f in ts_fields:
            ts = _parse_created_ts(p.get(f))
            if ts is not None:
                break
        # serial_number
        sn = _natural_int(p.get("serial_number"))
        # profile_id (или user_id) как последний резерв
        pid = p.get("profile_id")
        if pid is None:
            pid = p.get("user_id")
        pid_num = _natural_int(pid)

        # Для стабильности и «пустые в конец» используем большие значения по умолчанию
        BIG = 9_223_372_036_854_775_807  # псевдо "inf" для int-сортировки
        ts_key = int(ts) if ts is not None else BIG
        sn_key = sn if sn is not None else BIG
        pid_key = pid_num if pid_num is not None else BIG
        return (ts_key, sn_key, pid_key)

    # Python sorted() стабильна — одинаковые ключи сохранят порядок от API
    return sorted(items, key=key_fn)
# --------- КОНЕЦ добавленного блока ---------


# ========== Планировщик шагов (оценка микродействий) ==========

def _wallet_steps_count(settings: Dict[str, Any]) -> int:
    """4 шага на каждый кошелёк с заданным паролем."""
    keys = ["rabby_pass", "okx_pass", "keplr_pass", "backpack_pass", "phantom_pass"]
    count = 0
    for k in keys:
        if str(settings.get(k, "")).strip():
            count += 4
    return count

def estimate_steps_for_open(settings: Optional[Dict[str, Any]] = None) -> int:
    """
    Запуск профиля:
      1) Формирование URL
      2) HTTP-запрос
      3) Парсинг JSON
      4) Проверка кода
      5+) Selenium (по 4 шага на кошелёк с паролем)
    """
    settings = settings or load_settings_from_registry()
    return 4 + _wallet_steps_count(settings)

def estimate_steps_for_close() -> int:
    """
    Закрытие профиля:
      1) HTTP-запрос
      2) Парсинг JSON
      3) Проверка кода
    """
    return 3

def estimate_steps_for_status() -> int:
    """
    Проверка статуса:
      1) HTTP-запрос
      2) Парсинг JSON
      3) Интерпретация
    """
    return 3

def estimate_steps_for_restart(delay: Optional[float] = None, settings: Optional[Dict[str, Any]] = None) -> int:
    """
    Перезапуск = Закрыть + Ожидание (ceil(delay) минимум 1) + Открыть(+кошельки)
    """
    settings = settings or load_settings_from_registry()
    if delay is None:
        try:
            delay = float(settings.get("delay_stop", "1"))
        except Exception:
            delay = 1.0
    wait_steps = max(1, int(math.ceil(max(0.0, float(delay)))))
    return estimate_steps_for_close() + wait_steps + estimate_steps_for_open(settings)


# ========== Глобальный троттлинг AdsPower API (ЖЁСТКО 1 RPS) ==========

# ВАЖНО: по требованию — без исключений фиксируем лимит на уровне модуля.
_ADSPOWER_RPS: float = 1.0

_api_lock = threading.Lock()
_next_allowed_ts = 0.0

def _get_rps_from_settings() -> float:
    """
    Ранее значение брали из реестра (adspower_rps). По ТЗ теперь ЗАЖИМАЕМ 1.0 RPS
    вне зависимости от настроек, чтобы строго соответствовать лимиту AdsPower.
    """
    return _ADSPOWER_RPS

def _respect_rps() -> None:
    """Общий троттлинг для всех HTTP методов (строго 1 запрос/сек)."""
    global _next_allowed_ts
    rps = _get_rps_from_settings()
    min_interval = 1.0 / max(0.0001, rps)
    with _api_lock:
        now = time.monotonic()
        if now < _next_allowed_ts:
            time.sleep(_next_allowed_ts - now)
        _next_allowed_ts = time.monotonic() + min_interval

def _api_get(full_url: str, timeout: float = 30.0) -> Tuple[int, bytes]:
    """
    ЕДИНАЯ точка входа для GET-запросов к AdsPower API.
    Гарантирует глобальный лимит RPS.
    Возвращает (status_code, raw_bytes).
    """
    _respect_rps()
    req = urllib.request.Request(full_url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        code = resp.status
        data = resp.read()
        return code, data

def _api_post(full_url: str, payload: Dict[str, Any], timeout: float = 30.0) -> Tuple[int, bytes]:
    """
    ЕДИНАЯ точка входа для POST-запросов к AdsPower API (JSON).
    Также учитывает глобальный RPS.
    """
    _respect_rps()
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(full_url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        code = resp.status
        data = resp.read()
        return code, data


# ===================== ГРУППЫ: ЗАГРУЗКА И ОБРАБОТКА =====================

def _normalize_base_url(api_url: str) -> str:
    base = (api_url or "").strip().rstrip("/")
    if not base:
        return ""
    if not base.startswith("http://") and not base.startswith("https://"):
        base = "http://" + base
    return base

def get_groups_and_log(api_url: str) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """
    Загружает список групп из AdsPower: GET /api/v1/group/list
    Учитывает пагинацию: page_size по умолчанию = 1, максимум = 2000. Тянем все страницы.
    Возвращает:
      - словарь {str(group_id): group_name}
      - список лог-сообщений [(msg, level)]
    Стойко переносит разные формы JSON: data: { list: [...] } или data: [...]
    """
    base = _normalize_base_url(api_url)
    if not base:
        return {}, [("Адрес API не указан.", "ERROR")]

    groups_map: Dict[str, str] = {}
    page = 1
    page_size = 2000  # максимум, чтобы не бегать по 100 страницам
    try:
        while True:
            url = f"{base}/api/v1/group/list?page={page}&page_size={page_size}"
            code, data = _api_get(url, timeout=15.0)
            if code != 200:
                return groups_map, [(f"Код ответа при запросе групп не 200: {code}", "ERROR")]
            try:
                js = json.loads(data.decode("utf-8"))
            except Exception as ex:
                return groups_map, [(f"Ошибка парсинга JSON групп: {ex}", "ERROR")]

            if not isinstance(js, dict) or js.get("code") != 0:
                return groups_map, [(js.get("msg", "Ошибка при запросе групп"), "ERROR")]

            raw = js.get("data", None)
            if isinstance(raw, dict):
                groups_list = raw.get("list", []) or []
            elif isinstance(raw, list):
                groups_list = raw
            else:
                groups_list = []

            for g in groups_list:
                try:
                    gid = g.get("group_id", g.get("id", 0))
                    name = g.get("group_name", g.get("name", "")) or ""
                    gid_s = str(int(gid)) if isinstance(gid, (int, float)) else str(gid or "0")
                    if gid_s not in groups_map:
                        groups_map[gid_s] = name
                except Exception:
                    continue

            # если пришло меньше, чем page_size — это последний блок
            if len(groups_list) < page_size:
                break
            page += 1

        if not groups_map:
            # Даже если пусто — не ругаемся, просто работаем без названий
            logger.warning("Список групп пуст или не распознан.", profile_names=["GLOBAL"], category="PROFILE")

        return groups_map, []
    except urllib.error.HTTPError as e:
        return {}, [(f"HTTP ошибка при запросе групп: {e.code} — {e.reason}", "ERROR")]
    except urllib.error.URLError as e:
        return {}, [(f"Ошибка подключения при запросе групп: {e.reason}", "ERROR")]
    except Exception as ex:
        return {}, [(f"Непредвиденная ошибка при запросе групп: {ex}", "ERROR")]

def build_group_index(profiles: List[Dict[str, str]]) -> Dict[str, List[Dict[str, str]]]:
    """
    Строит индекс {group_name: [профили...]}. Если group_name пустой — кладём в "(Без группы)".
    UI сможет использовать это как источник для QTreeView/QTableWidget со сворачиванием.
    """
    idx: Dict[str, List[Dict[str, str]]] = {}
    for p in profiles:
        gname = str(p.get("group_name", "") or "").strip() or "(Без группы)"
        idx.setdefault(gname, []).append(p)
    return idx


# ========== Работа с AdsPower API и профилями ==========

def get_profiles_and_log(api_url: str) -> Tuple[List[Dict[str, str]], List[Tuple[str, str]]]:
    """
    ВЕРСИЯ v2: POST /api/v2/browser-profile/list
    Возвращает список профилей со следующими полями:
      - user_id (profile_id)
      - name
      - ip (внешний IP от IP-чекера)
      - ip_country (двухбуквенный код страны, например 'RU', 'UA')
      - group_id (строкой, если доступно в ответе API; иначе '0')      [NEW]
      - group_name (из справочника групп; если не найдено — "(Без группы)")  [NEW]

    Логи пишутся в стиле, совместимом с текущей системой.
    """
    base = _normalize_base_url(api_url)
    if not base:
        return [], [("Адрес API не указан.", "ERROR")]

    url = base + "/api/v2/browser-profile/list"
    payload = {
        "page": 1,
        "limit": 1000,
        # без фильтров — по всем группам; сортировку оставим по умолчанию
    }

    # Заранее пытаемся получить справочник групп. Даже если не получится — не валимся.
    groups_map: Dict[str, str] = {}
    g_logs: List[Tuple[str, str]] = []
    try:
        groups_map, g_logs = get_groups_and_log(base)
        if g_logs:
            # прокинем предупреждения наверх, но профили всё равно отдаём
            for msg, lvl in g_logs:
                if lvl == "ERROR":
                    logger.warning(msg, profile_names=["GLOBAL"], category="PROFILE")
                else:
                    logger.info(msg, profile_names=["GLOBAL"], category="PROFILE")
    except Exception as e:
        logger.warning(f"Не удалось загрузить группы: {e}", profile_names=["GLOBAL"], category="PROFILE")

    try:
        code, data = _api_post(url, payload, timeout=15.0)
        if code != 200:
            msg = f"Код ответа не 200: {code}"
            return [], [(msg, "ERROR")]
        try:
            json_data = json.loads(data.decode("utf-8"))
        except Exception as ex:
            return [], [(f"Ошибка парсинга JSON: {ex}", "ERROR")]

        if not isinstance(json_data, dict):
            return [], [("Некорректный JSON-ответ (ожидался dict).", "ERROR")]

        if json_data.get("code") != 0:
            msg = json_data.get("msg", "Unknown error")
            return [], [(msg, "ERROR")]

        items = json_data.get("data", {}).get("list", []) or []

        # --------- Стабильная сортировка старые -> новые ---------
        items = _sort_profiles_items(items)
        # --------- КОНЕЦ сортировки ---------

        profiles: List[Dict[str, str]] = []
        for prof in items:
            pid = str(prof.get("profile_id", prof.get("user_id", "")) or "")
            name = str(prof.get("name", "") or "")
            ip = str(prof.get("ip", "") or "")
            ip_country = str(prof.get("ip_country", "") or "")
            # NEW: группировка
            gid_raw = prof.get("group_id", prof.get("gid", 0))
            try:
                gid = str(int(gid_raw)) if isinstance(gid_raw, (int, float)) else str(gid_raw or "0")
            except Exception:
                gid = "0"
            gname = groups_map.get(gid, "(Без группы)")
            profiles.append({
                "user_id": pid,
                "name": name,
                "ip": ip,
                "ip_country": ip_country.upper() if ip_country else "",
                "group_id": gid,
                "group_name": gname,
            })
        return profiles, []
    except urllib.error.HTTPError as e:
        msg = f"HTTP ошибка: {e.code} — {e.reason}"
        return [], [(msg, "ERROR")]
    except urllib.error.URLError as e:
        msg = f"Ошибка подключения: {e.reason} (Проверьте соединение с API!)"
        return [], [(msg, "ERROR")]
    except Exception as ex:
        msg = f"Непредвиденная ошибка: {ex}"
        return [], [(msg, "ERROR")]

def _progress(progress_cb: ProgressCB, text: str, cancel_event: Optional[threading.Event] = None) -> None:
    """Безопасный вызов колбэка прогресса (один вызов == один шаг) + проверка мягкой отмены."""
    try:
        if progress_cb:
            progress_cb(text)
    except Exception:
        pass
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled("Отменено пользователем")

def open_profile(
    user_id: Union[str, int],
    name: str,
    logger_func: Optional[Any] = None,
    api_url: Optional[str] = None,
    progress_cb: ProgressCB = None,
    cancel_event: Optional[threading.Event] = None
) -> Tuple[bool, str]:
    settings = load_settings_from_registry()
    api_url = api_url or settings.get("api_url", "http://local.adspower.com:50395")
    api_url = api_url.strip().rstrip("/")
    profile_name = name.strip() if name else "Профиль"
    endpoint = "/api/v1/browser/start"
    params = {
        "user_id": user_id,
        "open_tabs": "0",
        "ip_tab": "0"
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"{api_url}{endpoint}?{query}"

    logger.start(
        "Запуск профиля",
        profile_names=[profile_name],
        category="PROFILE"
    )

    try:
        _progress(progress_cb, f"{profile_name}: формирование URL для запуска", cancel_event)

        code, data = _api_get(url, timeout=30.0)
        _progress(progress_cb, f"{profile_name}: HTTP запрос /browser/start", cancel_event)

        try:
            json_data = json.loads(data.decode("utf-8"))
            _progress(progress_cb, f"{profile_name}: парсинг ответа", cancel_event)
        except Exception as ex:
            logger.error(
                "Ошибка JSON",
                profile_names=[profile_name],
                category="PROFILE",
                extra={"trace": str(ex)}
            )
            return False, f"Ошибка JSON: {ex}"

        if code == 200 and json_data.get("code") == 0:
            try:
                _progress(progress_cb, f"{profile_name}: проверка кода: успех", cancel_event)
            except OperationCancelled:
                return True, "Открыт"
            logger.success(
                "Профиль успешно запущен",
                profile_names=[profile_name],
                category="PROFILE"
            )
            # ==== Selenium-автоматизация кошельков (мягкая деградация) ====
            try:
                ws_url = json_data.get("data", {}).get("ws", {}).get("selenium")
                chrome_driver_path = json_data.get("data", {}).get("webdriver")
                if ws_url and chrome_driver_path:
                    if _SELENIUM_AVAILABLE:
                        try:
                            unlock_wallets_for_profile(
                                ws_url, chrome_driver_path, profile_name, settings,
                                progress_cb=progress_cb
                            )
                        except Exception as e:
                            logger.warning(
                                "Ошибка при запуске Selenium-автоматизации",
                                profile_names=[profile_name],
                                category="WALLET",
                                extra={"trace": str(e)}
                            )
                    else:
                        logger.info(
                            "Selenium не установлен — пропускаем автоматическую разблокировку кошельков.",
                            profile_names=[profile_name],
                            category="WALLET"
                        )
            except Exception as e:
                logger.warning(
                    "Ошибка при запуске Selenium-автоматизации",
                    profile_names=[profile_name],
                    category="WALLET",
                    extra={"trace": str(e)}
                )
            return True, "Открыт"
        else:
            _progress(progress_cb, f"{profile_name}: проверка кода: ошибка", cancel_event)
            msg = json_data.get("msg", f"HTTP Error {code}")
            logger.error(
                f"Не удалось открыть профиль: {msg}",
                profile_names=[profile_name],
                category="PROFILE"
            )
            return False, msg
    except OperationCancelled:
        return False, "Отменено пользователем"
    except Exception as ex:
        logger.error(
            f"Ошибка открытия профиля: {ex}",
            profile_names=[profile_name],
            category="PROFILE",
            extra={"trace": str(ex)}
        )
        return False, str(ex)

def close_profile(
    user_id: Union[str, int],
    name: str,
    logger_func: Optional[Any] = None,
    api_url: Optional[str] = None,
    progress_cb: ProgressCB = None,
    cancel_event: Optional[threading.Event] = None
) -> Tuple[bool, str]:
    settings = load_settings_from_registry()
    api_url = api_url or settings.get("api_url", "http://local.adspower.com:50395")
    api_url = api_url.strip().rstrip("/")
    profile_name = name.strip() if name else "Профиль"
    endpoint = "/api/v1/browser/stop"
    url = f"{api_url}{endpoint}?user_id={user_id}"

    logger.start(
        "Закрытие профиля",
        profile_names=[profile_name],
        category="PROFILE"
    )

    try:
        code, data = _api_get(url, timeout=30.0)
        _progress(progress_cb, f"{profile_name}: HTTP запрос /browser/stop", cancel_event)

        try:
            json_data = json.loads(data.decode("utf-8"))
            _progress(progress_cb, f"{profile_name}: парсинг ответа", cancel_event)
        except Exception as ex:
            logger.error(
                "Ошибка JSON",
                profile_names=[profile_name],
                category="PROFILE",
                extra={"trace": str(ex)}
            )
            return False, f"Ошибка JSON: {ex}"

        if code == 200 and json_data.get("code") == 0:
            _progress(progress_cb, f"{profile_name}: проверка кода: успех", cancel_event)
            logger.info(
                "Профиль закрыт",
                profile_names=[profile_name],
                category="PROFILE"
            )
            return True, "Закрыт"
        else:
            _progress(progress_cb, f"{profile_name}: проверка кода: ошибка", cancel_event)
            msg = json_data.get("msg", f"HTTP Error {code}")
            logger.error(
                f"Не удалось закрыть профиль: {msg}",
                profile_names=[profile_name],
                category="PROFILE"
            )
            return False, msg
    except OperationCancelled:
        return False, "Отменено пользователем"
    except Exception as ex:
        logger.error(
            f"Ошибка закрытия профиля: {ex}",
            profile_names=[profile_name],
            category="PROFILE",
            extra={"trace": str(ex)}
        )
        return False, str(ex)

def get_profile_status(
    user_id: Union[str, int],
    profile_name: Optional[str] = None,
    logger_func: Optional[Any] = None,
    api_url: Optional[str] = None,
    progress_cb: ProgressCB = None,
    cancel_event: Optional[threading.Event] = None
) -> str:
    """
    Возвращает статус профиля. Информационные логи статуса (успех) — не пишем здесь,
    чтобы не дублировать с GUI. Логируем только ошибки.
    Микрошаги: запрос -> парсинг -> интерпретация.
    """
    settings = load_settings_from_registry()
    api_url = api_url or settings.get("api_url", "http://local.adspower.com:50395")
    api_url = api_url.strip().rstrip("/")
    endpoint = "/api/v1/browser/active"
    url = f"{api_url}{endpoint}?user_id={user_id}"
    profile_name = profile_name or str(user_id)
    try:
        code, data = _api_get(url, timeout=10.0)
        _progress(progress_cb, f"{profile_name}: HTTP запрос /browser/active", cancel_event)
        try:
            json_data = json.loads(data.decode("utf-8"))
            _progress(progress_cb, f"{profile_name}: парсинг ответа", cancel_event)
        except Exception as ex:
            logger.error(
                "Ошибка JSON",
                profile_names=[profile_name],
                category="PROFILE",
                extra={"trace": str(ex)}
            )
            return f"Error JSON: {ex}"
        if code == 200 and json_data.get("code") == 0:
            status = json_data.get("data", {}).get("status", "")
            _progress(progress_cb, f"{profile_name}: интерпретация статуса", cancel_event)
            return status
        msg = json_data.get("msg", "Unknown error")
        logger.error(
            f"Ошибка получения статуса профиля: {msg}",
            profile_names=[profile_name],
            category="PROFILE"
        )
        _progress(progress_cb, f"{profile_name}: интерпретация статуса (ошибка)", cancel_event)
        return f"Error API: code={json_data.get('code')} msg={msg}"
    except OperationCancelled:
        return "Cancelled"
    except Exception as ex:
        logger.error(
            f"Ошибка получения статуса профиля: {ex}",
            profile_names=[profile_name],
            category="PROFILE",
            extra={"trace": str(ex)}
        )
        _progress(progress_cb, f"{profile_name}: ошибка запроса статуса", cancel_event)
        return f"Error: {ex}"

def restart_profile(
    user_id: Union[str, int],
    name: str,
    logger_func: Optional[Any] = None,
    api_url: Optional[str] = None,
    progress_cb: ProgressCB = None,
    cancel_event: Optional[threading.Event] = None
) -> Tuple[bool, str]:
    profile_name = name.strip() if name else "Профиль"
    logger.start(
        "Перезапуск профиля",
        profile_names=[profile_name],
        category="PROFILE"
    )

    # 1) Закрыть
    closed_success, closed_status = close_profile(
        user_id, profile_name, logger_func=logger_func, api_url=api_url, progress_cb=progress_cb, cancel_event=cancel_event
    )
    if closed_success:
        logger.info(
            "Профиль успешно закрыт перед запуском",
            profile_names=[profile_name],
            category="PROFILE"
        )
    else:
        if closed_status == "Отменено пользователем":
            return False, closed_status
        logger.warning(
            f"Ошибка при закрытии профиля перед запуском: {closed_status}",
            profile_names=[profile_name],
            category="PROFILE"
        )

    # 2) Ожидание — разбиваем на шаги (ceil(delay)), чтобы каждый тик учитывался
    try:
        settings = load_settings_from_registry()
        delay = float(settings.get("delay_stop", "1"))
    except Exception:
        delay = 1.0
    steps = max(1, int(math.ceil(max(0.0, float(delay)))))
    per_step_sleep = delay / steps if steps > 0 else 0
    for i in range(1, steps + 1):
        time.sleep(per_step_sleep)
        _progress(progress_cb, f"{profile_name}: ожидание перезапуска {i}/{steps}", cancel_event)

    # 3) Открыть
    open_success, open_status = open_profile(
        user_id, profile_name, logger_func=logger_func, api_url=api_url, progress_cb=progress_cb, cancel_event=cancel_event
    )
    if open_success:
        logger.success(
            "Профиль успешно перезапущен",
            profile_names=[profile_name],
            category="PROFILE"
        )
        return True, "Перезапущен"
    else:
        if open_status == "Отменено пользователем":
            return False, open_status
        logger.error(
            f"Ошибка перезапуска профиля: {open_status}",
            profile_names=[profile_name],
            category="PROFILE"
        )
        return False, open_status


# ======= Selenium-автоматизация кошельков (ленивые импорты) =======

# Флаг доступности selenium и связанные сущности.
_SELENIUM_AVAILABLE: bool = True
_SELENIUM_IMPORT_ERROR: Optional[str] = None

try:
    from selenium import webdriver  # type: ignore
    from selenium.webdriver.chrome.options import Options  # type: ignore
    from selenium.webdriver.chrome.service import Service  # type: ignore
    from selenium.webdriver.common.by import By  # type: ignore
    from selenium.webdriver.support.ui import WebDriverWait  # type: ignore
    from selenium.webdriver.support import expected_conditions as EC  # type: ignore
    from selenium.common.exceptions import (  # type: ignore
        TimeoutException, ElementClickInterceptedException, NoSuchElementException, WebDriverException, NoSuchWindowException
    )
except Exception as _e:
    # Selenium отсутствует — продолжаем работу без автоматизации кошельков.
    _SELENIUM_AVAILABLE = False
    _SELENIUM_IMPORT_ERROR = str(_e)
    webdriver = None  # type: ignore
    Options = None  # type: ignore
    Service = None  # type: ignore
    By = None  # type: ignore
    WebDriverWait = None  # type: ignore
    EC = None  # type: ignore
    # Подменяем исключения на базовые, чтобы блоки except работали как ожидалось.
    TimeoutException = Exception  # type: ignore
    ElementClickInterceptedException = Exception  # type: ignore
    NoSuchElementException = Exception  # type: ignore
    WebDriverException = Exception  # type: ignore
    NoSuchWindowException = Exception  # type: ignore

WALLET_CONFIGS = [
    {
        "name": "Rabby",
        "extension_url": "chrome-extension://acmacodkjbdgmoleebolmdjonilkdbch/popup.html",
        "password_key": "rabby_pass",
        "password_xpath": "//input[@type='password']",
        "unlock_button_xpath": "//button[.//span[text()='Unlock']]",
        "has_update_modal": True,
    },
    {
        "name": "OKX",
        "extension_url": "chrome-extension://mcohilncbfahbmgdjkbpemcciiolgcge/popup.html",
        "password_key": "okx_pass",
        "password_xpath": '//*[@id="app"]/div/div[1]/div/div[3]/form/div[1]/div/div/div/div/div/input',
        "unlock_button_xpath": '//*[@id="app"]/div/div[2]/button',
        "has_update_modal": False,
    },
    {
        "name": "Keplr",
        "extension_url": "chrome-extension://dmkamcknogkgcdfhhbddcghachkejeap/popup.html",
        "password_key": "keplr_pass",
        "password_xpath": '//*[@id="app"]/div/div/div/div[1]/div[2]/div/div/div/div/form/div[3]/div[3]/div/div[3]/div/div[1]/div/input',
        "unlock_button_xpath": '//*[@id="app"]/div/div/div/div[1]/div[2]/div/div/div/div/form/div[5]/button',
        "has_update_modal": False,
    },
    {
        "name": "Backpack",
        "extension_url": "chrome-extension://aflkmfhebedbjioipglgcbcmnbpgliof/popup.html",
        "password_key": "backpack_pass",
        "password_xpath": '//*[@id="root"]/span[1]/div[1]/div/div[2]/div[2]/div[1]/div/div[2]/form/div[1]/span/input',
        "unlock_button_xpath": "//span[text()='Разблокировать']",
        "has_update_modal": False,
    },
    {
        "name": "Phantom",
        "extension_url": "chrome-extension://bfnaelmomeimhlpmgjnjophhpkkoljpa/popup.html",
        "password_key": "phantom_pass",
        "password_xpath": '//*[@id="unlock-form"]/div/div/input',
        "unlock_button_xpath": '//*[@id="root"]/div/div/div[1]/div/div[2]/div/div/button',
        "has_update_modal": False,
    },
]

SELENIUM_WAIT_TIMEOUT = 10

def unlock_wallets_for_profile(
    ws_url: str, chrome_driver_path: str, profile_name: str, settings: Dict[str, Any],
    progress_cb: ProgressCB = None
) -> None:
    """
    Запуск selenium-подключения к уже открытому браузеру AdsPower и попытка
    автоматической разблокировки кошельков. Если Selenium недоступен — просто логируем и выходим.
    """
    if not _SELENIUM_AVAILABLE:
        logger.info(
            "Selenium не установлен — пропускаем автоматическую разблокировку кошельков.",
            profile_names=[profile_name],
            category="WALLET",
        )
        return

    logger.start(
        "Автоматизация разблокировки кошельков",
        profile_names=[profile_name],
        category="WALLET"
    )
    chrome_options = Options()
    chrome_options.add_experimental_option("debuggerAddress", ws_url)
    service = Service(executable_path=chrome_driver_path)
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.implicitly_wait(SELENIUM_WAIT_TIMEOUT)
    time.sleep(1.5)
    close_unwanted_tabs(driver)

    # Здесь settings уже развернуты и содержат реальные пароли (если доступны).
    passwords = {
        "rabby_pass": settings.get("rabby_pass", ""),
        "okx_pass": settings.get("okx_pass", ""),
        "keplr_pass": settings.get("keplr_pass", ""),
        "backpack_pass": settings.get("backpack_pass", ""),
        "phantom_pass": settings.get("phantom_pass", "")
    }
    try:
        retries = int(str(settings.get("wallet_retry_count", "3")).strip() or "3")
    except Exception:
        retries = 3
    retries = max(1, min(10, retries))

    unlocked, failed = unlock_wallets_robust(
        driver, ws_url, chrome_driver_path, profile_name, passwords,
        progress_cb=progress_cb, retry_count=retries
    )
    try:
        driver.quit()
    except Exception:
        pass

    if unlocked:
        logger.success(
            f"Разблокированы кошельки: {', '.join(unlocked)}",
            profile_names=[profile_name],
            category="WALLET"
        )
    if failed:
        logger.warning(
            f"Не удалось разблокировать кошельки: {', '.join(failed)}",
            profile_names=[profile_name],
            category="WALLET"
        )
    logger.info(
        "Автоматизация разблокировки кошельков завершена",
        profile_names=[profile_name],
        category="WALLET"
    )

def close_unwanted_tabs(driver: Any) -> None:
    if not driver:
        return
    try:
        base_handle = driver.window_handles[0]
        close_handles = []
        for handle in driver.window_handles[1:]:
            driver.switch_to.window(handle)
            url = driver.current_url.lower()
            if (
                "iplocation" in url or
                "browserleak" in url or
                "adspower.com" in url or
                "proxy" in url
            ):
                close_handles.append(handle)
        for handle in close_handles:
            try:
                driver.switch_to.window(handle)
                driver.close()
            except Exception:
                pass
        driver.switch_to.window(base_handle)
    except Exception:
        pass

def recreate_driver(ws_url: str, chrome_driver_path: str) -> Optional[Any]:
    if not _SELENIUM_AVAILABLE:
        return None
    try:
        chrome_options = Options()
        chrome_options.add_experimental_option("debuggerAddress", ws_url)
        service = Service(executable_path=chrome_driver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.implicitly_wait(SELENIUM_WAIT_TIMEOUT)
        time.sleep(1.5)
        return driver
    except Exception:
        return None

def unlock_wallet_single_attempt(
    driver: Any, wallet_conf: Dict[str, Any], profile_name: str, passwords: Dict[str, str],
    progress_cb: ProgressCB = None, count_steps: bool = True
) -> Tuple[bool, str]:
    """
    count_steps=True означает: этот проход учитывается в прогрессе (первый проход).
    Повторные попытки (retry) идут с count_steps=False и не двигают прогресс.
    """
    if not _SELENIUM_AVAILABLE or not driver:
        return False, "Selenium недоступен"

    wallet_name = wallet_conf["name"]
    password_key = wallet_conf["password_key"]
    extension_url = wallet_conf["extension_url"]
    password_xpath = wallet_conf["password_xpath"]
    unlock_button_xpath = wallet_conf["unlock_button_xpath"]
    has_update_modal = wallet_conf.get("has_update_modal", False)
    password = passwords.get(password_key, "")
    if not password:
        logger.info(
            f"{wallet_name}: пропущено — нет пароля",
            profile_names=[profile_name],
            category="WALLET"
        )
        return False, "Пропущено: нет пароля"
    try:
        driver.switch_to.new_window('tab')
        driver.get(extension_url)
        wait = WebDriverWait(driver, SELENIUM_WAIT_TIMEOUT)
        if count_steps:
            _progress(progress_cb, f"{profile_name}: {wallet_name} — открытие расширения")
        password_field = wait.until(EC.element_to_be_clickable((By.XPATH, password_xpath)))
        password_field.clear()
        password_field.send_keys(password)
        if count_steps:
            _progress(progress_cb, f"{profile_name}: {wallet_name} — ввод пароля")
        unlock_btn = wait.until(EC.element_to_be_clickable((By.XPATH, unlock_button_xpath)))
        driver.execute_script("arguments[0].click();", unlock_btn)
        if count_steps:
            _progress(progress_cb, f"{profile_name}: {wallet_name} — клик разблокировки")
        if wallet_name == "Backpack":
            time.sleep(3.0)
        else:
            time.sleep(1.0)
        if has_update_modal:
            try:
                update_close = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "button.ant-modal-close")))
                update_close.click()
            except Exception:
                pass
        driver.close()
        time.sleep(0.7)
        if driver.window_handles:
            driver.switch_to.window(driver.window_handles[0])
        time.sleep(0.5)
        if count_steps:
            _progress(progress_cb, f"{profile_name}: {wallet_name} — закрытие вкладки")
        logger.success(
            f"Кошелёк {wallet_name} разблокирован",
            profile_names=[profile_name],
            category="WALLET"
        )
        return True, "ok"
    except (TimeoutException, ElementClickInterceptedException, NoSuchElementException, WebDriverException, NoSuchWindowException) as ex:
        logger.info(
            f"Модальное окно {wallet_name} не появилось",
            profile_names=[profile_name],
            category="WALLET"
        )
        try:
            if len(driver.window_handles) > 1:
                driver.close()
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])
        except Exception:
            pass
        time.sleep(1.2)
        return False, str(ex)
    except Exception as ex:
        logger.error(
            f"Ошибка разблокировки кошелька {wallet_name}: {ex}",
            profile_names=[profile_name],
            category="WALLET",
            extra={"trace": str(ex)}
        )
        try:
            if len(driver.window_handles) > 1:
                driver.close()
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])
        except Exception:
            pass
        time.sleep(1.2)
        return False, str(ex)

def unlock_wallets_robust(
    driver: Any, ws_url: str, chrome_driver_path: str, profile_name: str, passwords: Dict[str, str],
    progress_cb: ProgressCB = None, retry_count: int = 3
) -> Tuple[List[str], List[str]]:
    unlocked_wallets: List[str] = []
    failed_wallets: List[str] = []
    for wallet_conf in WALLET_CONFIGS:
        wallet_name = wallet_conf["name"]
        password_key = wallet_conf["password_key"]
        password = passwords.get(password_key, "")
        if not password:
            continue
        ok = False
        driver_valid = driver is not None
        for attempt in range(1, retry_count + 1):
            logger.start(
                f"Попытка {attempt}/{retry_count} разблокировки {wallet_name}",
                profile_names=[profile_name],
                category="WALLET"
            )
            if not driver_valid:
                driver = recreate_driver(ws_url, chrome_driver_path)
                if driver is None:
                    logger.error(
                        "Не удалось пересоздать драйвер браузера",
                        profile_names=[profile_name],
                        category="WALLET"
                    )
                    break
                driver_valid = True
            time.sleep(0.8)
            try:
                success, msg = unlock_wallet_single_attempt(
                    driver, wallet_conf, profile_name, passwords,
                    progress_cb=progress_cb, count_steps=(attempt == 1)
                )
                if success:
                    unlocked_wallets.append(wallet_name)
                    ok = True
                    break
                else:
                    driver_valid = False
                    logger.warning(
                        f"Неудачная попытка разблокировки {wallet_name} ({msg})",
                        profile_names=[profile_name],
                        category="WALLET"
                    )
            except (WebDriverException, NoSuchWindowException) as ex:
                logger.warning(
                    f"WebDriverException — {ex}",
                    profile_names=[profile_name],
                    category="WALLET"
                )
                driver = recreate_driver(ws_url, chrome_driver_path)
                if driver is None:
                    logger.error(
                        "Не удалось пересоздать драйвер после ошибки",
                        profile_names=[profile_name],
                        category="WALLET"
                    )
                    break
                driver_valid = True
                time.sleep(1.5)
            except Exception as ex:
                logger.error(
                    f"Критическая ошибка при разблокировке {wallet_name}: {ex}",
                    profile_names=[profile_name],
                    category="WALLET",
                    extra={"trace": str(ex)}
                )
                driver_valid = False
                time.sleep(1)
        if not ok:
            failed_wallets.append(wallet_name)
            time.sleep(1)
    return unlocked_wallets, failed_wallets
