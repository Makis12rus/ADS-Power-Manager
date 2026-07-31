"""
Модуль: moduls/ads/_api_client.py
Назначение: Сетевой шлюз для взаимодействия с локальным API AdsPower и внешними сервисами.
Зона ответственности: Синхронные и асинхронные HTTP-запросы, строгое соблюдение
                      лимита в 1 RPS (Rate Limiting) для AdsPower и высокопроизводительное
                      асинхронное зондирование прокси-каналов (Proxy Probe Engine).
                      Реализует политику "Чистый Радар" — полное недоверие к ГЕО-данным AdsPower
                      и точный замер HTTP RTT (Round-Trip Time) для оценки качества тоннеля.
                      Включает Auto-Discovery Engine для мгновенного поиска портов AdsPower.
Интеграция: Вызывается из фасада ads_logic.py и оркестратора. Полностью изолирован
            от Selenium и графического интерфейса. Возвращает сырые DTO без
            привязки к UI-статусам.
"""

import time
import json
import urllib.request
import urllib.error
import urllib.parse
import threading
import asyncio
from typing import Any

# Ленивая загрузка асинхронных сетевых библиотек
try:
    import aiohttp
    from aiohttp_socks import ProxyConnector
except ImportError:
    aiohttp = None
    ProxyConnector = None

# Строгие абсолютные импорты ядра
from system.logger import logger, log_action
from core.core import (
    GEOIP_PRIMARY,
    GEOIP_SECONDARY,
    GEOIP_FALLBACK,
    PROXY_PROBE_TIMEOUT
)

# Строгие относительные импорты внутри плоского пакета ADS
from ._utils import _sort_profiles_items

# ======================= Константы и Глобальные блокировки =======================

# Лимит запросов к AdsPower API (строго 1 запрос в секунду по документации)
_ADSPOWER_RPS: float = 1.0

# Глобальный замок и таймер для обеспечения Rate Limit между всеми потоками
_api_lock = threading.Lock()
_next_allowed_ts = 0.0


# ======================= Утилиты нормализации =======================

def _normalize_base_url(api_url: str | None) -> str:
    """
    Приводит URL к стандартному виду, добавляя http:// если необходимо.
    """
    base = (api_url or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.startswith(("http://", "https://")) else "http://" + base


# ======================= Синхронный API Клиент =======================

def _request_ads_api(
        url: str,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 30.0
) -> tuple[int, bytes]:
    """
    Единая точка входа в API с учетом глобального лимита RPS (Синхронная версия).
    ВНИМАНИЕ: Ожидание (time.sleep) вынесено за пределы критической секции _api_lock,
    чтобы не блокировать другие потоки и GUI.
    """
    global _next_allowed_ts
    min_interval = 1.0 / max(0.0001, _ADSPOWER_RPS)
    wait_time = 0.0
    
    # 1. Быстро захватываем замок, вычисляем время ожидания и обновляем таймер
    with _api_lock:
        now = time.monotonic()
        if now < _next_allowed_ts:
            wait_time = _next_allowed_ts - now
            _next_allowed_ts += min_interval
        else:
            _next_allowed_ts = now + min_interval
    
    # 2. Спим вне замка, позволяя другим потокам тоже встать в очередь
    if wait_time > 0:
        time.sleep(wait_time)
    
    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")
    
    req = urllib.request.Request(url, data=data_bytes, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    
    try:
        # Resource Guard: Гарантированное закрытие сокета
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        # AdsPower может вернуть 400/500 с полезным JSON-телом ошибки
        return e.code, e.read()
    except Exception as e:
        # Сетевые сбои (ConnectionRefused, Timeout)
        raise RuntimeError(f"Сетевая ошибка при обращении к AdsPower API: {e}")


# ======================= Асинхронный API Клиент =======================

class AdsAsyncClient:
    """
    Асинхронный клиент для работы с API AdsPower.
    Реализует Rate Limiting (совместимый с синхронным кодом) и управление сессией.
    Используется строго как Context Manager: async with AdsAsyncClient() as client: ...
    """
    
    def __init__(self) -> None:
        self.session: Any = None
    
    async def __aenter__(self) -> "AdsAsyncClient":
        if not aiohttp:
            raise RuntimeError("Критическая ошибка: модуль aiohttp не установлен.")
        
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self
    
    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
    
    async def request(
            self,
            url: str,
            method: str = "GET",
            json_data: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        """
        Выполняет запрос с учетом глобального Rate Limit без блокировки Event Loop.
        """
        if not self.session:
            raise RuntimeError("Сессия клиента не инициализирована. Используйте 'async with'.")
        
        global _next_allowed_ts
        min_interval = 1.0 / max(0.0001, _ADSPOWER_RPS)
        wait_time = 0.0
        
        with _api_lock:
            now = time.monotonic()
            if now < _next_allowed_ts:
                wait_time = _next_allowed_ts - now
                _next_allowed_ts += min_interval
            else:
                _next_allowed_ts = now + min_interval
        
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        
        try:
            async with self.session.request(method, url, json=json_data) as resp:
                try:
                    data = await resp.json()
                except Exception:
                    data = {}
                return resp.status, data
        except Exception as e:
            return 0, {"code": -1, "msg": str(e)}
    
    async def check_profile_status(self, user_id: str, api_url: str) -> str:
        """Асинхронная проверка статуса профиля."""
        base = _normalize_base_url(api_url)
        url = f"{base}/api/v1/browser/active?user_id={user_id}"
        code, data = await self.request(url)
        
        if code == 200 and data.get("code") == 0:
            return data.get("data", {}).get("status", "Unknown")
        return f"Error: {data.get('msg', 'HTTP ' + str(code))}"
    
    async def close_profile(self, user_id: str, api_url: str) -> tuple[bool, str]:
        """Асинхронное закрытие профиля."""
        base = _normalize_base_url(api_url)
        url = f"{base}/api/v1/browser/stop?user_id={user_id}"
        
        try:
            with logger.block(f"Async Close {user_id}", profile_names=[user_id], category="API"):
                code, data = await self.request(url)
                if code == 200 and data.get("code") == 0:
                    return True, "Закрыт"
                return False, data.get("msg", f"HTTP {code}")
        except Exception as e:
            return False, str(e)


# ======================= Auto-Discovery Engine =======================

async def async_scan_local_ports(ports: tuple[int, ...]) -> str | None:
    """
    Асинхронный сканер локальных портов (Auto-Discovery Engine).
    Простукивает переданные порты на localhost (127.0.0.1) через чистые TCP-сокеты.
    Работает в обход HTTP-протокола и глобального лимитера 1 RPS.
    """
    logger.info(
        f"Запускаем асинхронную разведку портов: {ports}. Ищем живой AdsPower...",
        profile_names=["GLOBAL"], category="SYSTEM"
    )
    
    async def check_port(port: int) -> str | None:
        try:
            # Жесткий таймаут 100мс для мгновенной разведки
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection('127.0.0.1', port),
                timeout=0.1
            )
            # Resource Guard: Гарантированное закрытие сокета
            writer.close()
            await writer.wait_closed()
            return f"http://local.adspower.com:{port}"
        except Exception:
            return None

    # Запускаем простукивание всех портов параллельно
    tasks = [check_port(p) for p in ports]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    for res in results:
        if isinstance(res, str):
            logger.success(
                f"Разведка успешна! AdsPower обнаружен на порту: {res.split(':')[-1]}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            return res
            
    logger.warning(
        "Разведка вернулась ни с чем. Ни один из стандартных портов AdsPower не отвечает.",
        profile_names=["GLOBAL"], category="SYSTEM"
    )
    return None


# ======================= Proxy Probe Engine =======================

async def check_proxy_connection(proxy_url: str) -> tuple[bool, str, str, int]:
    """
    Асинхронный трехконтурный зонд для проверки жизнеспособности прокси, определения ГЕО
    и замера HTTP RTT (Round-Trip Time) задержки.
    Не использует глобальный лимитер AdsPower, так как стучится во внешние сервисы.
    
    Возвращает: (is_alive: bool, ip_address: str, country_code: str, latency_ms: int)
    """
    if not aiohttp or not ProxyConnector:
        return False, "aiohttp-socks missing", "XX", -1
        
    if not proxy_url:
        return False, "No proxy configured", "XX", -1
        
    try:
        connector = ProxyConnector.from_url(proxy_url)
    except Exception as e:
        logger.warning(f"Кривой формат прокси-строки: {e}", profile_names=["GLOBAL"], category="API")
        return False, "Invalid proxy format", "XX", -1
        
    timeout = aiohttp.ClientTimeout(total=PROXY_PROBE_TIMEOUT)
    
    try:
        # Resource Guard: Изолированная сессия с жестким таймаутом
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            
            # Контур 1: api.country.is (Быстрый, безлимитный)
            try:
                start_time = time.perf_counter()
                async with session.get(GEOIP_PRIMARY) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        latency = int((time.perf_counter() - start_time) * 1000)
                        return True, str(data.get("ip", "")), str(data.get("country", "XX")).upper(), latency
            except Exception:
                pass
                
            # Контур 2: ip-api.com (Надежный, но с лимитами)
            try:
                start_time = time.perf_counter()
                async with session.get(GEOIP_SECONDARY) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        latency = int((time.perf_counter() - start_time) * 1000)
                        return True, str(data.get("query", "")), str(data.get("countryCode", "XX")).upper(), latency
            except Exception:
                pass
                
            # Контур 3: Google 204 (Железобетонный fallback на случай падения GeoIP)
            try:
                start_time = time.perf_counter()
                async with session.get(GEOIP_FALLBACK) as resp:
                    if resp.status == 204:
                        latency = int((time.perf_counter() - start_time) * 1000)
                        return True, "Hidden IP", "XX", latency
            except Exception:
                pass
                
            return False, "All probes failed", "XX", -1
            
    except Exception as e:
        # Ошибка на уровне TCP/SOCKS (ConnectionRefused, ProxyConnectionError, Timeout)
        return False, str(e), "XX", -1


# ======================= Бизнес-логика API (Группы и Профили) =======================

@log_action("Загрузка групп профилей", category="API")
def get_groups_and_log(api_url: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """
    Загрузка групп AdsPower (с поддержкой пагинации).
    Возвращает словарь {group_id: group_name} и список логов для UI.
    """
    base = _normalize_base_url(api_url)
    if not base:
        return {}, [("Адрес API не указан в настройках.", "ERROR")]
    
    groups_map: dict[str, str] = {}
    page = 1
    page_size = 2000
    
    try:
        while True:
            url = f"{base}/api/v1/group/list?page={page}&page_size={page_size}"
            code, data = _request_ads_api(url, timeout=15.0)
            
            if code != 200:
                return groups_map, [(f"Ошибка API групп: HTTP {code}", "ERROR")]
            
            js = json.loads(data.decode("utf-8"))
            if js.get("code") != 0:
                return groups_map, [(js.get("msg", "Api Error"), "ERROR")]
            
            raw = js.get("data", None)
            groups_list = []
            if isinstance(raw, dict):
                groups_list = raw.get("list", [])
            elif isinstance(raw, list):
                groups_list = raw
            
            if not groups_list:
                break
            
            for g in groups_list:
                gid = str(g.get("group_id") or g.get("id", 0))
                name = g.get("group_name") or g.get("name", "")
                if gid not in groups_map:
                    groups_map[gid] = name
            
            if len(groups_list) < page_size:
                break
            page += 1
        
        return groups_map, []
    
    except Exception as ex:
        return {}, [(f"Ошибка получения групп: {ex}", "ERROR")]


def build_group_index(profiles: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    """
    Группирует плоский список профилей по именам групп для отображения в дереве UI.
    """
    idx: dict[str, list[dict[str, str]]] = {}
    for p in profiles:
        gname = p.get("group_name", "").strip() or "(Без группы)"
        idx.setdefault(gname, []).append(p)
    return idx


@log_action("Загрузка списка профилей", category="API")
def get_profiles_and_log(api_url: str) -> tuple[list[dict[str, str]], list[tuple[str, str]]]:
    """
    Получение полного списка профилей (API v2).
    Автоматически подтягивает имена групп, извлекает настройки прокси и сортирует результат.
    Реализует политику "Чистый Радар": игнорирует ГЕО-данные от AdsPower.
    """
    base = _normalize_base_url(api_url)
    if not base:
        return [], [("Адрес API не указан в настройках.", "ERROR")]
    
    # Сначала получаем маппинг групп
    groups_map, g_logs = get_groups_and_log(api_url)
    logs = []
    
    # Пробрасываем ошибки групп в основной лог, но не прерываем работу
    for msg, level in g_logs:
        if level == "ERROR":
            logger.warning(msg, profile_names=["GLOBAL"], category="PROFILE")
    
    url = f"{base}/api/v2/browser-profile/list"
    payload = {"page": 1, "limit": 1000}
    
    try:
        code, data = _request_ads_api(url, method="POST", payload=payload, timeout=15.0)
        if code != 200:
            return [], [(f"Ошибка API профилей: HTTP {code}", "ERROR")]
        
        js = json.loads(data.decode("utf-8"))
        if js.get("code") != 0:
            return [], [(js.get("msg", "Unknown Error"), "ERROR")]
        
        items = js.get("data", {}).get("list", [])
        
        # Сортируем профили через утилиту (по дате создания и ID)
        items = _sort_profiles_items(items)
        
        profiles = []
        for p in items:
            pid = str(p.get("profile_id") or p.get("user_id", "") or "")
            gid = str(p.get("group_id") or p.get("gid", "0"))
            
            # Извлекаем настройки прокси для формирования SOCKS/HTTP строки
            proxy_config = p.get("user_proxy_config", {})
            proxy_type = str(proxy_config.get("proxy_type", "")).lower()
            proxy_host = str(proxy_config.get("proxy_host", ""))
            proxy_port = str(proxy_config.get("proxy_port", ""))
            proxy_user = str(proxy_config.get("proxy_user", ""))
            proxy_password = str(proxy_config.get("proxy_password", ""))
            
            proxy_url = ""
            if proxy_type in ("socks5", "socks4", "http", "https") and proxy_host and proxy_port:
                auth = ""
                if proxy_user or proxy_password:
                    # Безопасное URL-кодирование спецсимволов в логине/пароле
                    u = urllib.parse.quote(proxy_user, safe="")
                    pw = urllib.parse.quote(proxy_password, safe="")
                    auth = f"{u}:{pw}@"
                proxy_url = f"{proxy_type}://{auth}{proxy_host}:{proxy_port}"
            
            profiles.append({
                "user_id": pid,
                "name": str(p.get("name", "")),
                # Операция "Чистый Радар": игнорируем дезинформацию от AdsPower
                "ip": "Проверяется...",
                "ip_country": "XX",
                "group_id": gid,
                "group_name": groups_map.get(gid, "(Без группы)"),
                "proxy_url": proxy_url,
            })
        
        return profiles, logs
    except Exception as ex:
        return [], [(f"Критическая ошибка получения профилей: {ex}", "ERROR")]