"""
Модуль: system/logger.py
Назначение: «Черный ящик» приложения. Централизованный, потокобезопасный логгер.
Зона ответственности: Прием, фильтрация (анти-спам, дедупликация), структурирование
                      и безопасная межпоточная маршрутизация системных событий.
Интеграция: Является независимым системным ядром (Foundation Layer). Полностью отвязан
            от GUI и бизнес-логики. Генерирует исключительно чистые DTO (словари),
            передавая их через сигналы PySide6 (паттерн Observer). Не содержит HTML-разметки,
            CSS, стилей или прямых ссылок на окна.
"""

import datetime
import re
import threading
import time
import functools
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, QCoreApplication

# ===================== Константы и регулярные выражения =====================

# Жесткий лимит буфера для предотвращения утечек памяти (Resource Guard)
MAX_BUFFER_SIZE = 10000

# ID‑подобные имена профилей (для детектирования, если имя не найдено)
PROFILE_ID_LIKE_RE = re.compile(r'^[a-z0-9]{6,}$')

# Фразы, после которых нужно срезать хвост вида ": что‑то"
CUT_TAIL_PATTERNS = [
    r"^(Запуск профиля)\s*:",
    r"^(Перезапуск профиля)\s*:",
    r"^(Закрытие профиля)\s*:",
    r"^(Старт массового запуск профилей)\s*:",
    r"^(Старт массового перезапуск профилей)\s*:",
    r"^(Старт массового закрытие профилей)\s*:",
    r"^(Операция запуск успешно выполнена)\s*:",
    r"^(Операция перезапуск успешно выполнена)\s*:",
    r"^(Операция закрытие успешно выполнена)\s*:",
    r"^(Статус профиля —)\s*:",
]
# Предварительная компиляция паттернов срезания хвостов
_CUT_TAIL_REGEXES = [re.compile(p + r'\s*.*$', re.IGNORECASE) for p in CUT_TAIL_PATTERNS]

# Регулярные выражения для нормализации
_PREFIX_REMOVE_RE = re.compile(r'^\s*[^\:]{1,64}\s*:\s*')
_MEANING_CLEAN_RE = re.compile(r'[\[\]🟢⚫🔴⏳✅⚠️ℹ️❌:.,;!?\-\—\–/\\0-9]+')
_WHITESPACE_RE = re.compile(r'\s+')
_ANTISPAM_CLEAN_RE = re.compile(r'[\s.:\-]+')

# Сообщения, для которых увеличиваем анти‑спам таймаут
ANTISPAM_SPECIALS = {
    "Watchdog запущен.",
    "Watchdog остановлен.",
    "Настройки успешно загружены из реестра.",
    "Настройки успешно сохранены в реестре.",
    "Работа вне Windows: используется дефолтные настройки.",
    "Ключ настроек в реестре не найден, используются дефолтные.",
    "Сохранение поддерживается только под Windows!",
    "Тест подключения к API выполнен успешно.",
    "Автоматизация разблокировки кошельков завершена",
}

# Таймауты (сек)
SPECIAL_SPAM_TIMEOUT = 60.0
DEFAULT_SPAM_TIMEOUT = 10.0  # Увеличено для лучшей фильтрации циклов

# === Шаблоны (для обратной совместимости быстрых вызовов) ===
OPERATION_TEMPLATES = {
    'start': "Операция запуск успешно выполнена",
    'restart': "Операция перезапуск успешно выполнена",
    'stop': "Операция закрытие успешно выполнена",
    'status_active': "Статус профиля — 🟢 Активен",
    'status_closed': "Статус профиля — ⚫ Закрыт",
    'status_unknown': "Статус профиля — 🔴 Неизвестно",
    'status_error': "Статус профиля — 🔴 Ошибка",
    'wallet_unlock': "Разблокировка кошелька успешно завершена",
}


# ===================== Сигналы логгера =====================

class LoggerSignals(QObject):
    """
    Канал связи для передачи чистых DTO в главный поток GUI.
    Обеспечивает слабую связанность (Loose Coupling) и потокобезопасность.
    """
    log_signal = Signal(dict)
    clear_signal = Signal()


# ===================== Класс логгера =====================

class Logger:
    _instance: 'Logger | None' = None
    _init_lock: threading.RLock = threading.RLock()
    
    def __new__(cls) -> "Logger":
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init_basic()
            return cls._instance
    
    def _init_basic(self) -> None:
        """
        Базовая инициализация. Используем threading.RLock вместо QMutex,
        чтобы не зависеть от инициализации QApplication на ранних этапах.
        """
        self._log_buffer: list[dict[str, Any]] = []
        self.signals: LoggerSignals | None = None  # Ленивая инициализация Qt-сигналов
        self._state_lock: threading.RLock = threading.RLock()
        
        # Функция для разрешения имен профилей (внедряется извне)
        self._profile_resolver: Callable[[str], str] | None = None
        
        # Память последнего сообщения
        self._last_messages: dict[tuple, datetime.datetime] = {}
        self._profile_log_memory: dict[tuple, str] = {}
        
        # Кешированные нормализованные сообщения для антиспама
        self._antispam_cache: dict[str, str] = {}
        for msg in ANTISPAM_SPECIALS:
            self._antispam_cache[msg] = self._normalize_antispam_msg(msg)
    
    def _ensure_qt_initialized(self) -> None:
        """
        Ленивая инициализация Qt-объектов. Вызывается при первом логировании.
        Гарантирует, что QObject (сигналы) не будет создан до старта QCoreApplication.
        """
        if self.signals is None:
            with self._state_lock:
                if self.signals is None:
                    # Проверяем, что ядро Qt уже поднято, иначе создание QObject уронит приложение
                    if QCoreApplication.instance() is not None:
                        self.signals = LoggerSignals()
    
    def set_profile_resolver(self, resolver_func: Callable[[str], str]) -> None:
        """Устанавливает функцию для преобразования ID профиля в Имя."""
        self._profile_resolver = resolver_func
    
    # =================== Публичный API логирования ===================
    
    def log(
            self,
            message: str,
            level: str = "INFO",
            force: bool = False,
            profile_names: list[str] | None = None,
            category: str | None = None,
            extra: dict[str, Any] | None = None,
            timestamp: str | None = None,
    ) -> None:
        
        # Инициализация сигналов при первом вызове (безопасно)
        self._ensure_qt_initialized()
        
        # ==== Нормализация входных параметров ====
        if not profile_names:
            resolved_profiles = ["GLOBAL"]
        else:
            resolved_profiles = []
            for p in profile_names:
                if p:
                    p_str = str(p)
                    if p_str == "GLOBAL":
                        resolved_profiles.append("GLOBAL")
                    else:
                        if self._profile_resolver:
                            resolved_profiles.append(self._profile_resolver(p_str))
                        else:
                            resolved_profiles.append(p_str)
            
            if not resolved_profiles:
                resolved_profiles = ["GLOBAL"]
        
        category = str(category) if category else "SYSTEM"
        level = (level or "INFO").upper()
        extra = extra or {}
        
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        
        # ==== Санитизация текста ====
        raw_msg = (message or "").strip()
        raw_msg = self._strip_profile_prefixes(raw_msg, resolved_profiles)
        raw_msg = self._cut_known_tails(raw_msg)
        raw_msg = self._attach_extra_to_message(raw_msg, extra)
        
        # ==== Решение показывать профиль или GLOBAL ====
        visible_profiles = self._visible_profiles(resolved_profiles)
        profile_key = tuple(sorted(visible_profiles))
        
        # ==== Подготовка данных для проверок ====
        meaning_norm = self._normalize_meaning(raw_msg)
        norm_for_antispam = self._normalize_antispam_msg(raw_msg)
        spam_timeout = self._get_antispam_timeout_norm(norm_for_antispam)
        now = datetime.datetime.now()
        
        # ==== Критическая секция (проверка дублей и запись) ====
        with self._state_lock:
            # 1. Анти‑дубликаты по смыслу (для конкретного профиля/категории)
            if not force:
                if self._should_skip_duplicate_meaning_locked(meaning_norm, visible_profiles, category, level):
                    return
            
            # 2. Анти‑спам по времени (глобальный для сообщения)
            key = (norm_for_antispam, level, category, profile_key)
            last_time = self._last_messages.get(key)
            
            if not force and last_time:
                if (now - last_time).total_seconds() < spam_timeout:
                    return
            
            self._last_messages[key] = now
            
            # 3. Сборка чистого DTO
            log_entry = {
                "timestamp": timestamp,
                "level": level,
                "message": raw_msg,
                "profile_names": list(visible_profiles),
                "category": category,
                "extra": extra,
            }
            
            self._log_buffer.append(log_entry)
            
            # Resource Guard: Защита от утечки памяти при долгом аптайме
            if len(self._log_buffer) > MAX_BUFFER_SIZE:
                # Срезаем старые логи, оставляя запас
                self._log_buffer = self._log_buffer[-int(MAX_BUFFER_SIZE * 0.8):]
        
        # ==== Вывод (Трансляция в эфир) ====
        # Qt Signals автоматически разруливают потоки. Если лог пришел из Selenium-воркера,
        # сигнал будет доставлен в GUI-поток через QueuedConnection.
        if self.signals:
            self.signals.log_signal.emit(log_entry)
    
    # ===== Упрощённые методы-обёртки =====
    
    def info(self, message: str, profile_names: list[str] | None = None,
             category: str | None = None, extra: dict[str, Any] | None = None, force: bool = False) -> None:
        self.log(message, "INFO", force, profile_names, category, extra)
    
    def success(self, message: str, profile_names: list[str] | None = None,
                category: str | None = None, extra: dict[str, Any] | None = None, force: bool = False) -> None:
        self.log(message, "SUCCESS", force, profile_names, category, extra)
    
    def warning(self, message: str, profile_names: list[str] | None = None,
                category: str | None = None, extra: dict[str, Any] | None = None, force: bool = False) -> None:
        self.log(message, "WARNING", force, profile_names, category, extra)
    
    def error(self, message: str, profile_names: list[str] | None = None,
              category: str | None = None, extra: dict[str, Any] | None = None, force: bool = False) -> None:
        self.log(message, "ERROR", force, profile_names, category, extra)
    
    def start(self, message: str, profile_names: list[str] | None = None,
              category: str | None = None, extra: dict[str, Any] | None = None, force: bool = False) -> None:
        self.log(message, "START", force, profile_names, category, extra)
    
    def default(self, message: str, profile_names: list[str] | None = None,
                category: str | None = None, extra: dict[str, Any] | None = None, force: bool = False) -> None:
        self.log(message, "DEFAULT", force, profile_names, category, extra)
    
    # ===== Логи по шаблонам =====
    
    def profile_log(self, operation: str, level: str = "SUCCESS", profile_name: str | None = None, category: str = "PROFILE") -> None:
        msg = OPERATION_TEMPLATES.get(operation, operation)
        profiles = [profile_name] if profile_name else ["GLOBAL"]
        self.log(msg, level=level, profile_names=profiles, category=category)
    
    def status_log(self, status: str | None, profile_name: str | None = None) -> None:
        mapping = {
            "Active": OPERATION_TEMPLATES['status_active'],
            "Closed": OPERATION_TEMPLATES['status_closed'],
            "Inactive": OPERATION_TEMPLATES['status_closed'],
            "": OPERATION_TEMPLATES['status_unknown'],
            None: OPERATION_TEMPLATES['status_unknown'],
        }
        msg = mapping.get(status, f"{OPERATION_TEMPLATES['status_error']} ({status})")
        profiles = [profile_name] if profile_name else ["GLOBAL"]
        self.log(msg, level="INFO", profile_names=profiles, category="PROFILE")
    
    def wallet_log(self, wallet_action: str, level: str = "SUCCESS", profile_name: str | None = None) -> None:
        msg = OPERATION_TEMPLATES.get(wallet_action, wallet_action)
        profiles = [profile_name] if profile_name else ["GLOBAL"]
        self.log(msg, level=level, profile_names=profiles, category="WALLET")
    
    # ===== Context Manager Helper =====
    
    def block(self, message: str, profile_names: list[str] | None = None, category: str = "SYSTEM") -> "LogBlock":
        return LogBlock(self, message, profile_names, category)
    
    # =================== Управление буфером ===================
    
    def clear(self) -> None:
        """
        Очищает внутренний буфер логов и транслирует сигнал очистки в эфир.
        Не взаимодействует с GUI напрямую, исключая RecursionError.
        """
        with self._state_lock:
            self._log_buffer.clear()
            
        self._ensure_qt_initialized()
        if self.signals:
            self.signals.clear_signal.emit()
    
    def get_buffer(self) -> list[dict[str, Any]]:
        """Возвращает копию текущего буфера (используется GUI при инициализации)."""
        with self._state_lock:
            return list(self._log_buffer)
    
    # =================== Санация текста и анти‑дубли ===================
    
    def _strip_profile_prefixes(self, msg: str, profiles: list[str]) -> str:
        res = msg
        for p in profiles:
            if not p or p == "GLOBAL":
                continue
            pat = re.compile(rf'^\s*{re.escape(str(p))}\s*:\s*', flags=re.IGNORECASE)
            res = pat.sub('', res)
        return res
    
    def _cut_known_tails(self, msg: str) -> str:
        res = msg
        for pattern_re in _CUT_TAIL_REGEXES:
            res = pattern_re.sub(r'\1', res)
        res = _WHITESPACE_RE.sub(' ', res).strip()
        return res
    
    def _normalize_meaning(self, msg: str) -> str:
        txt = msg
        txt = _PREFIX_REMOVE_RE.sub('', txt)
        txt = _MEANING_CLEAN_RE.sub('', txt)
        txt = PROFILE_ID_LIKE_RE.sub('', txt)
        txt = _WHITESPACE_RE.sub('', txt).strip().lower()
        return txt
    
    def _should_skip_duplicate_meaning_locked(self, meaning_norm: str, profiles: list[str], category: str, level: str) -> bool:
        skip = False
        profiles = profiles or ["GLOBAL"]
        for prof in profiles:
            key = (prof, category, level)
            last = self._profile_log_memory.get(key)
            if last == meaning_norm:
                skip = True
            else:
                self._profile_log_memory[key] = meaning_norm
        return skip
    
    # =================== Анти‑спам по времени ===================
    
    def _get_antispam_timeout_norm(self, normalized_msg: str) -> float:
        if normalized_msg in self._antispam_cache.values():
            return SPECIAL_SPAM_TIMEOUT
        return DEFAULT_SPAM_TIMEOUT
    
    def _normalize_antispam_msg(self, msg: str) -> str:
        s = msg.strip().lower()
        s = _ANTISPAM_CLEAN_RE.sub('', s)
        return s
    
    # =================== Утилиты ===================
    
    def _is_profile_id_like(self, name: str) -> bool:
        if not name or name == "GLOBAL":
            return False
        return bool(PROFILE_ID_LIKE_RE.fullmatch(str(name)))
    
    def _attach_extra_to_message(self, msg: str, extra: dict[str, Any]) -> str:
        if not extra:
            return msg
        
        parts = [msg]
        if "trace" in extra and extra["trace"]:
            trace_val = extra["trace"]
            if isinstance(trace_val, (list, tuple)):
                trace_val = "\n".join(str(t) for t in trace_val)
            parts.append(f"\nТрассировка:\n{trace_val}")
        
        if "details" in extra and extra["details"]:
            details_val = extra["details"]
            if isinstance(details_val, (list, tuple, dict)):
                details_val = str(details_val)
            parts.append(f"\nДетали: {details_val}")
        
        return "".join(parts)
    
    def _visible_profiles(self, profile_names: list[str]) -> list[str]:
        names = [str(p) for p in (profile_names or []) if p]
        visible = [p for p in names if p != "GLOBAL"]
        if visible:
            return visible
        return ["GLOBAL"]
    
    # =================== Методы фильтрации/выборок ===================
    
    def get_unique_values(self, field: str) -> list[str]:
        with self._state_lock:
            buffer_copy = list(self._log_buffer)
        
        values = set()
        for log_entry in buffer_copy:
            if field == "profile_names":
                for p in log_entry.get("profile_names", ["GLOBAL"]):
                    values.add(p)
            else:
                v = log_entry.get(field)
                if v:
                    values.add(v)
        
        if field == "profile_names" and not values:
            values.add("GLOBAL")
        return sorted(values)
    
    def filter_logs(
            self,
            level: str | None = None,
            profile: str | None = None,
            category: str | None = None
    ) -> list[dict[str, Any]]:
        with self._state_lock:
            buffer_copy = list(self._log_buffer)
        
        result = []
        target_level = level.upper() if level and level != "ALL" else None
        target_profile = profile if profile and profile != "ALL" else None
        target_category = category if category and category != "ALL" else None
        
        for log_entry in buffer_copy:
            if target_level:
                if (log_entry.get("level") or "INFO").upper() != target_level:
                    continue
            if target_profile:
                if target_profile not in log_entry.get("profile_names", ["GLOBAL"]):
                    continue
            if target_category:
                if (log_entry.get("category") or "SYSTEM") != target_category:
                    continue
            result.append(log_entry)
        return result


# ===================== Context Manager & Decorator =====================

class LogBlock:
    """
    Контекстный менеджер для автоматического логирования начала и конца операции.
    Автоматически замеряет время выполнения и ловит исключения.
    """
    
    def __init__(self, logger_instance: Logger, message: str,
                 profile_names: list[str] | None = None,
                 category: str = "SYSTEM"):
        self.logger = logger_instance
        self.msg = message
        self.profiles = profile_names
        self.category = category
        self.start_time = 0.0
    
    def __enter__(self) -> "LogBlock":
        self.start_time = time.time()
        self.logger.log(f"{self.msg}...", level="START",
                        profile_names=self.profiles, category=self.category)
        return self
    
    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        duration = time.time() - self.start_time
        if exc_type:
            err_txt = str(exc_val)
            trace = "".join(traceback.format_exception(exc_type, exc_val, exc_tb))
            self.logger.log(f"Ошибка: {self.msg} ({err_txt})", level="ERROR",
                            profile_names=self.profiles, category=self.category,
                            extra={"trace": trace})
            return False
        else:
            self.logger.log(f"{self.msg} завершено ({duration:.2f}s)", level="SUCCESS",
                            profile_names=self.profiles, category=self.category)
            return False


def log_action(message: str, category: str = "SYSTEM") -> Callable:
    """
    Декоратор для автоматического логирования выполнения функции.
    Использует глобальный экземпляр logger.
    """
    
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with LogBlock(logger, message, category=category):
                return func(*args, **kwargs)
        
        return wrapper
    
    return decorator


# Глобальный экземпляр (Singleton)
logger: Logger = Logger()