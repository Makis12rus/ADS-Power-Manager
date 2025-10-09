# =========================
# 📝 Файл: logger.py
# =========================

from PySide6.QtCore import QObject, Signal, QMutex, QMutexLocker, QThread
import datetime
import re
from typing import Any, Dict, List, Optional, Tuple, Union

# ===================== Константы оформления =====================

LEVEL_STYLES: Dict[str, Dict[str, str]] = {
    "ERROR":   {"color": "#FF4F4F",  "emoji": "❌"},
    "WARNING": {"color": "#FFD700",  "emoji": "⚠️"},
    "SUCCESS": {"color": "#40DB78",  "emoji": "✅"},
    "START":   {"color": "#6CB7FF",  "emoji": "⏳"},
    "INFO":    {"color": "#C0C0C0",  "emoji": "ℹ️"},
    "DEFAULT": {"color": "#DADADA",  "emoji": "📝"},
}

# ID‑подобные имена профилей (их не показываем в визуальном выводе)
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

# Сообщения, для которых увеличиваем анти‑спам таймаут
ANTISPAM_SPECIALS = [
    "Watchdog запущен.",
    "Watchdog остановлен.",
    "Настройки успешно загружены из реестра.",
    "Настройки успешно сохранены в реестре.",
    "Работа вне Windows: используется дефолтные настройки.",
    "Ключ настроек в реестре не найден, используются дефолтные.",
    "Сохранение поддерживается только под Windows!",
    "Тест подключения к API выполнен успешно.",
    "Автоматизация разблокировки кошельков завершена",
]

SPECIAL_SPAM_TIMEOUT = 60
DEFAULT_SPAM_TIMEOUT = 8

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
    log_signal = Signal(dict)

# ===================== Класс логгера =====================

class Logger:
    _instance: Optional["Logger"] = None
    _mutex: QMutex = QMutex()

    def __new__(cls) -> "Logger":
        with QMutexLocker(cls._mutex):
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self) -> None:
        self._log_buffer: List[Dict[str, Any]] = []
        self._log_window: Optional[Any] = None
        self.signals: LoggerSignals = LoggerSignals()
        self.signals.log_signal.connect(self._handle_log)
        self._thread: QThread = QThread.currentThread()

        # Память последнего сообщения (для анти‑спама времени)
        # key = (normalized_msg, level, category, profile_key) -> last_time
        self._last_messages: Dict[Tuple, datetime.datetime] = {}

        # Память последнего "смыслового" сообщения на профиль/категорию/уровень
        # key = (profile, category, level) -> normalized_meaning
        self._profile_log_memory: Dict[Tuple, str] = {}

    # =================== Публичный API логирования ===================

    def log(
        self,
        message: str,
        level: str = "INFO",
        force: bool = False,
        profile_names: Optional[List[str]] = None,
        category: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        """
        Главный вход: принимает сырое сообщение и метаданные,
        приводит к нужному формату, подавляет дубли и отдаёт в окно.
        """
        # ==== Нормализация входных параметров ====
        if profile_names is None or not profile_names:
            profile_names = ["GLOBAL"]
        profile_names = [str(p) for p in profile_names if p] or ["GLOBAL"]
        category = str(category) if category else "SYSTEM"
        level = (level or "INFO").upper()
        extra = extra or {}

        # Метка времени
        if not timestamp:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")

        # ==== Санитизация текста ====
        raw_msg = (message or "").strip()
        # Вырезаем префиксы "<имя профиля>: " если автор лога их подставил в текст.
        raw_msg = self._strip_profile_prefixes(raw_msg, profile_names)
        # Срезаем хвосты ": что‑то" для известных фраз (чтобы "…: M35" == "…")
        raw_msg = self._cut_known_tails(raw_msg)
        # Добавляем extra (трейс/детали), переносы строк превращаем в <br>
        raw_msg = self._attach_extra_to_message(raw_msg, extra)
        html_msg = raw_msg.replace("\n", "<br>")

        # ==== Решение показывать профиль или GLOBAL ====
        visible_profiles = self._visible_profiles(profile_names)

        # ==== Анти‑дубликаты по смыслу (на профиль/категорию/уровень) ====
        meaning_norm = self._normalize_meaning(raw_msg)
        if not force and self._should_skip_duplicate_meaning(meaning_norm, visible_profiles, category, level):
            return

        # ==== Анти‑спам по времени для точной фразы ====
        profile_key = tuple(sorted(visible_profiles)) if visible_profiles else ("GLOBAL",)
        norm_for_antispam = self._normalize_antispam_msg(raw_msg)
        key = (norm_for_antispam, level, category, profile_key)
        now = datetime.datetime.now()
        last_time = self._last_messages.get(key)
        spam_timeout = self._get_antispam_timeout(raw_msg)
        if not force and last_time and (now - last_time).total_seconds() < spam_timeout:
            return
        self._last_messages[key] = now

        # ==== Сборка единого лог‑объекта ====
        log_entry = {
            "timestamp": timestamp,
            "level": level,
            "message": html_msg,
            "profile_names": list(visible_profiles) if visible_profiles else ["GLOBAL"],
            "category": str(category),
            "extra": dict(extra) if extra else {},
        }
        self._log_buffer.append(log_entry)

        # ==== Вывод ====
        if QThread.currentThread() == self._thread:
            self._handle_log(log_entry)
        else:
            self.signals.log_signal.emit(log_entry)

    # ===== Упрощённые методы-обёртки (совместимость) =====

    def info(self, message: str, profile_names: Optional[List[str]] = None,
             category: Optional[str] = None, extra: Optional[Dict[str, Any]] = None, force: bool = False) -> None:
        self.log(message, "INFO", force, profile_names, category, extra)

    def success(self, message: str, profile_names: Optional[List[str]] = None,
                category: Optional[str] = None, extra: Optional[Dict[str, Any]] = None, force: bool = False) -> None:
        self.log(message, "SUCCESS", force, profile_names, category, extra)

    def warning(self, message: str, profile_names: Optional[List[str]] = None,
                category: Optional[str] = None, extra: Optional[Dict[str, Any]] = None, force: bool = False) -> None:
        self.log(message, "WARNING", force, profile_names, category, extra)

    def error(self, message: str, profile_names: Optional[List[str]] = None,
              category: Optional[str] = None, extra: Optional[Dict[str, Any]] = None, force: bool = False) -> None:
        self.log(message, "ERROR", force, profile_names, category, extra)

    def start(self, message: str, profile_names: Optional[List[str]] = None,
              category: Optional[str] = None, extra: Optional[Dict[str, Any]] = None, force: bool = False) -> None:
        self.log(message, "START", force, profile_names, category, extra)

    def default(self, message: str, profile_names: Optional[List[str]] = None,
                category: Optional[str] = None, extra: Optional[Dict[str, Any]] = None, force: bool = False) -> None:
        self.log(message, "DEFAULT", force, profile_names, category, extra)

    # ===== Логи по шаблонам (оставляем для совместимости с кодом) =====

    def profile_log(self, operation: str, level: str = "SUCCESS", profile_name: Optional[str] = None, category: Optional[str] = "PROFILE"):
        msg = OPERATION_TEMPLATES.get(operation, operation)
        profiles = [profile_name] if profile_name else ["GLOBAL"]
        self.log(msg, level=level, profile_names=profiles, category=category)

    def status_log(self, status: str, profile_name: Optional[str] = None):
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

    def wallet_log(self, wallet_action: str, level: str = "SUCCESS", profile_name: Optional[str] = None):
        msg = OPERATION_TEMPLATES.get(wallet_action, wallet_action)
        profiles = [profile_name] if profile_name else ["GLOBAL"]
        self.log(msg, level=level, profile_names=profiles, category="WALLET")

    # =================== Работа с окном логов ===================

    def set_log_window(self, log_window: Any) -> None:
        self._log_window = log_window
        for log_entry in self._log_buffer:
            self._emit_to_log_window(log_entry)

    def clear(self) -> None:
        self._log_buffer.clear()
        if self._log_window:
            self._log_window.clear_logs()

    def get_buffer(self) -> List[Dict[str, Any]]:
        return list(self._log_buffer)

    # =================== Внутренняя обработка ===================

    def _handle_log(self, log_entry: Union[Dict[str, Any], str, tuple]) -> None:
        # Поддержка старых путей
        if isinstance(log_entry, str):
            log_entry = {
                "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
                "level": "INFO",
                "message": log_entry.replace("\n", "<br>"),
                "profile_names": ["GLOBAL"],
                "category": "SYSTEM",
                "extra": {},
            }
        elif isinstance(log_entry, tuple):
            timestamp = log_entry[0] if len(log_entry) > 0 else datetime.datetime.now().strftime("%H:%M:%S")
            level = log_entry[1] if len(log_entry) > 1 else "INFO"
            message = log_entry[2] if len(log_entry) > 2 else ""
            profile_names = log_entry[3] if len(log_entry) > 3 else ["GLOBAL"]
            log_entry = {
                "timestamp": timestamp,
                "level": level,
                "message": str(message).replace("\n", "<br>"),
                "profile_names": profile_names,
                "category": "SYSTEM",
                "extra": {},
            }

        # Запасные значения
        for field, default in [
            ("timestamp", datetime.datetime.now().strftime("%H:%M:%S")),
            ("level", "INFO"),
            ("message", ""),
            ("profile_names", ["GLOBAL"]),
            ("category", "SYSTEM"),
            ("extra", {}),
        ]:
            if field not in log_entry or log_entry[field] is None:
                log_entry[field] = default

        # Приведение профилей
        if not isinstance(log_entry["profile_names"], list) or not log_entry["profile_names"]:
            log_entry["profile_names"] = ["GLOBAL"]

        # Отдать в окно
        self._emit_to_log_window(log_entry)

    def _emit_to_log_window(self, log_entry: Dict[str, Any]) -> None:
        if self._log_window:
            html = self.make_log_html(log_entry)
            self.log_text_append(html, log_entry)

    def log_text_append(self, html, log_entry):
        # Проксируем в log_window
        if hasattr(self._log_window, "append_log_html"):
            self._log_window.append_log_html(html, log_entry)

    # =================== Рендер строки лога ===================

    def make_log_html(self, log_entry: Dict[str, Any]) -> str:
        ts = log_entry.get("timestamp", datetime.datetime.now().strftime("%H:%M:%S"))
        level = (log_entry.get("level", "INFO") or "INFO").upper()
        style = LEVEL_STYLES.get(level, LEVEL_STYLES["DEFAULT"])
        color = style["color"]
        emoji = style["emoji"]
        profiles_raw = log_entry.get("profile_names", ["GLOBAL"]) or ["GLOBAL"]
        category = log_entry.get("category", "SYSTEM") or "SYSTEM"
        msg = log_entry.get("message", "") or ""

        # Отфильтровать ID‑подобные имена из визуального списка
        profiles_visible = [p for p in profiles_raw if not self._is_profile_id_like(p)]
        if not profiles_visible:
            profiles_visible = ["GLOBAL"]

        # Формат по ТЗ:
        # [время] эмодзи [УРОВЕНЬ] [ПРОФИЛЬ1] [ПРОФИЛЬ2] ... [КАТЕГОРИЯ] : Сам лог
        profiles_block = " ".join([f"[{p}]" for p in profiles_visible])
        # — время всегда серым, эмодзи без окраски, а текст после эмодзи — цвет уровня
        html = (
            f'<span style="color:#8A8A8A;">[{ts}]</span> '
            f'{emoji} '
            f'<span style="color:{color};">[{level}] {profiles_block} [{category}] : {msg}</span>'
        )
        return html

    # =================== Санация текста и анти‑дубли ===================

    def _strip_profile_prefixes(self, msg: str, profiles: List[str]) -> str:
        """
        Удаляет в тексте ведущие префиксы вида "<PROFILE>:" для всех известных профилей,
        чтобы имя не дублировалось в тексте. Сравнение — нечувствительно к регистру.
        """
        res = msg
        for p in profiles:
            if not p or p == "GLOBAL":
                continue
            pat = re.compile(rf'^\s*{re.escape(str(p))}\s*:\s*', flags=re.IGNORECASE)
            res = pat.sub('', res)
        return res

    def _cut_known_tails(self, msg: str) -> str:
        """
        Для известных "заголовков" сообщений срезает хвост после двоеточия.
        Пример: "Закрытие профиля: M35" -> "Закрытие профиля"
        """
        res = msg
        for pattern in CUT_TAIL_PATTERNS:
            res = re.sub(pattern + r'\s*.*$', r'\1', res, flags=re.IGNORECASE)
        # Убираем случайные двойные пробелы
        res = re.sub(r'\s{2,}', ' ', res).strip()
        return res

    def _normalize_meaning(self, msg: str) -> str:
        """
        Нормализация сообщения для сравнения "по смыслу":
        - убрать HTML <br> (переводы строк),
        - убрать эмодзи и квадратные скобки,
        - убрать пунктуацию,
        - убрать числа и ID‑подобные токены,
        - убрать пробелы, привести к нижнему регистру.
        """
        txt = msg
        txt = txt.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
        txt = re.sub(r'^\s*[^\:]{1,64}\s*:\s*', '', txt)  # защитное удаление префикса "X: "
        txt = re.sub(r'[\[\]🟢⚫🔴⏳✅⚠️ℹ️❌]', '', txt)
        txt = re.sub(r'[\:\.\,\;\!\?\-\—\–\/\\]+', '', txt)
        txt = re.sub(r'\b[0-9]+\b', '', txt)
        txt = PROFILE_ID_LIKE_RE.sub('', txt)
        txt = re.sub(r'\s+', '', txt).strip().lower()
        return txt

    def _should_skip_duplicate_meaning(self, meaning_norm: str, profiles: List[str], category: str, level: str) -> bool:
        """
        Подавляет подряд идущие одинаковые по смыслу сообщения для одного и того же
        профиля(ей)/категории/уровня.
        """
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

    def _get_antispam_timeout(self, message: str) -> int:
        norm = self._normalize_antispam_msg(message)
        for special in ANTISPAM_SPECIALS:
            if norm == self._normalize_antispam_msg(special):
                return SPECIAL_SPAM_TIMEOUT
        return DEFAULT_SPAM_TIMEOUT

    def _normalize_antispam_msg(self, msg: str) -> str:
        s = msg.strip().lower()
        s = s.replace("<br>", " ")
        s = re.sub(r'[\s\.\:\-]+', '', s)
        return s

    # =================== Утилиты ===================

    def _is_profile_id_like(self, name: str) -> bool:
        if not name or name == "GLOBAL":
            return False
        return bool(PROFILE_ID_LIKE_RE.fullmatch(str(name)))

    def _attach_extra_to_message(self, msg: str, extra: Dict[str, Any]) -> str:
        parts = [msg]
        if isinstance(extra, dict):
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

    def _visible_profiles(self, profile_names: List[str]) -> List[str]:
        """
        Возвращает список профилей для отображения:
        - исключает ID‑подобные;
        - если после исключения пусто — возвращает ["GLOBAL"].
        """
        names = [str(p) for p in (profile_names or []) if p]
        visible = [p for p in names if not self._is_profile_id_like(p) and p != "GLOBAL"]
        if visible:
            return visible
        return ["GLOBAL"]

    # =================== Методы фильтрации/выборок ===================

    def get_unique_values(self, field: str) -> List[str]:
        values = set()
        for log in self._log_buffer:
            if field == "profile_names":
                for p in log.get("profile_names", ["GLOBAL"]):
                    if self._is_profile_id_like(p):
                        continue
                    values.add(p)
            else:
                v = log.get(field)
                if v:
                    values.add(v)
        if field == "profile_names" and not values:
            values.add("GLOBAL")
        return sorted(values)

    def filter_logs(
        self,
        level: Optional[str] = None,
        profile: Optional[str] = None,
        category: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        result = []
        for log in self._log_buffer:
            if level and level != "ALL":
                if (log.get("level") or "INFO").upper() != level.upper():
                    continue
            if profile and profile != "ALL":
                if profile not in log.get("profile_names", ["GLOBAL"]):
                    continue
            if category and category != "ALL":
                if (log.get("category") or "SYSTEM") != category:
                    continue
            result.append(log)
        return result


# Глобальный экземпляр
logger: Logger = Logger()
