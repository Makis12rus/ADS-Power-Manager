"""
Модуль: core/_profiles_reg.py
Назначение: Глобальный потокобезопасный реестр метаданных профилей (SSOT).
Зона ответственности: Хранение соответствий идентификаторов AdsPower их
                      человекочитаемым названиям, кодам стран и задержкам (ping).
                      Используется логгером для подмены безликих ID на понятные имена в UI,
                      а также пультом горячей автоматизации для мгновенного (O(1)) получения
                      данных для отрисовки SVG-флагов и индикаторов скорости.
                      Обеспечивает механизм гидратации (State Hydration) для
                      решения проблемы "Холодного старта" и синхронизацию кэша
                      (Event-Driven Cache Coherence) при проверке прокси.
Интеграция: Изолированный модуль (L0). Не имеет внешних зависимостей внутри
            проекта. Инжектируется в логгер и UI-панели через фасад core.py.
"""

import threading
from typing import Any, NamedTuple, Dict

# =================== GLOBAL PROFILE REGISTRY ===================
# Реестр для маппинга ID профилей в их метаданные (Имя, Страна, Пинг).
# Работает как кэш в оперативной памяти, избавляя нас от необходимости
# дергать API или парсить тяжелые таблицы при каждом чихе интерфейса.


class ProfileMetadata(NamedTuple):
    """Строгий контракт данных для метаданных профиля."""
    name: str
    country: str
    latency: int = -1


_PROFILE_METADATA_MAP: Dict[str, ProfileMetadata] = {}
_PROFILE_MAP_LOCK = threading.Lock()


def register_profile_names(profiles: list[dict[str, Any]]) -> None:
    """
    Обновляет глобальный реестр метаданных профилей.
    Принимает список словарей, где ожидаются ключи 'user_id'/'profile_id', 'name' и 'ip_country'.
    Операция потокобезопасна.
    """
    with _PROFILE_MAP_LOCK:
        for p in profiles:
            # Поддерживаем оба варианта ключей AdsPower API (v1 и v2)
            uid = str(p.get("user_id") or p.get("profile_id") or "").strip()
            name = str(p.get("name") or "").strip()
            country = str(p.get("ip_country") or p.get("country") or "").strip().upper()
            
            if uid and name:
                # Если профиль уже есть в кэше, сохраняем его пинг, чтобы не затереть
                existing_latency = -1
                if uid in _PROFILE_METADATA_MAP:
                    existing_latency = _PROFILE_METADATA_MAP[uid].latency
                    
                _PROFILE_METADATA_MAP[uid] = ProfileMetadata(
                    name=name,
                    country=country,
                    latency=existing_latency
                )


def update_profile_country(profile_id: str, country: str, latency: int = -1) -> None:
    """
    Точечное обновление кода страны и пинга для конкретного профиля.
    Вызывается асинхронными зондами (Proxy Probe Engine) после успешной проверки прокси.
    Обеспечивает синхронизацию кэша (Event-Driven Cache Coherence) для пульта горячей автоматизации.
    Операция потокобезопасна.
    """
    pid_str = str(profile_id).strip()
    with _PROFILE_MAP_LOCK:
        if pid_str in _PROFILE_METADATA_MAP:
            old_meta = _PROFILE_METADATA_MAP[pid_str]
            # Пересоздаем NamedTuple с новыми данными (иммутабельность)
            _PROFILE_METADATA_MAP[pid_str] = ProfileMetadata(
                name=old_meta.name,
                country=str(country).strip().upper(),
                latency=latency
            )


def get_profile_name(profile_id: str) -> str:
    """
    Возвращает имя профиля по его ID.
    Если имя не найдено в кэше, безопасно возвращает сам ID (fallback).
    Операция потокобезопасна. Сохраняет 100% обратную совместимость для логгера.
    """
    pid_str = str(profile_id).strip()
    with _PROFILE_MAP_LOCK:
        meta = _PROFILE_METADATA_MAP.get(pid_str)
        return meta.name if meta else pid_str


def get_profile_metadata(profile_id: str) -> ProfileMetadata:
    """
    Возвращает полные метаданные профиля (имя, код страны, пинг) по его ID.
    Используется пультом горячей автоматизации для мгновенной отрисовки чипсов.
    Операция потокобезопасна.
    """
    pid_str = str(profile_id).strip()
    with _PROFILE_MAP_LOCK:
        meta = _PROFILE_METADATA_MAP.get(pid_str)
        # Если профиль-призрак (нет в кэше), отдаем заглушку, чтобы UI не упал с AttributeError
        return meta if meta else ProfileMetadata(name=pid_str, country="", latency=-1)


# =================== STATE HYDRATION (COLD START FIX) ===================

def export_cache_dict() -> dict[str, dict[str, Any]]:
    """
    Сериализует текущий слепок метаданных в простой словарь.
    Используется для последующего сжатия в JSON/Base64 и сохранения в реестр.
    Операция потокобезопасна.
    """
    with _PROFILE_MAP_LOCK:
        return {
            uid: {
                "name": meta.name,
                "country": meta.country,
                "latency": meta.latency
            }
            for uid, meta in _PROFILE_METADATA_MAP.items()
        }


def import_cache_dict(data: dict[str, Any]) -> None:
    """
    Десериализует сырой словарь (из реестра) обратно в типизированный кэш NamedTuple.
    Используется при старте приложения для мгновенного восстановления имен, флагов и пингов.
    Операция потокобезопасна.
    """
    with _PROFILE_MAP_LOCK:
        for uid, val in data.items():
            if isinstance(val, dict):
                # Безопасный fallback для старых кэшей, где еще не было поля latency
                try:
                    latency_val = int(val.get("latency", -1))
                except (ValueError, TypeError):
                    latency_val = -1
                    
                _PROFILE_METADATA_MAP[str(uid)] = ProfileMetadata(
                    name=str(val.get("name", uid)),
                    country=str(val.get("country", "")),
                    latency=latency_val
                )