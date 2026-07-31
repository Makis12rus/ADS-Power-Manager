"""
Модуль: moduls/ads/profile_model_manager.py
Назначение: Изолированный менеджер плоской модели данных (Data Engine).
Зона ответственности: Инкапсуляция списка профилей (DTO), группировка,
                      пересчет плоских индексов (flat_idx), инъекция контекстных флагов
                      (is_last_in_group), сквозная нумерация (display_num), транзакционное
                      сохранение пользовательской сортировки (DND / Up / Down) в реестр и
                      точечное обновление данных прокси (Proxy Probe Engine).
Интеграция: Слой Presentation Logic. Вызывается из `profile_presenter.py`.
            Полностью отвязан от графических виджетов и тяжелых моделей Qt.
            Обеспечивает O(1) доступ к данным для движка виртуальной карусели (Recycler View)
            и математики свайп-выделения (Sweep Selection).
            Оперирует строгими состояниями (ProfileState) вместо магических строк.
"""

import json
import base64
from typing import Any

from PySide6.QtCore import QObject

# Строгие абсолютные импорты ядра
from core.core import load_ui_geometry, save_ui_geometry
from core._constants import ProfileState
from system.logger import logger

# Строгие относительные импорты внутри плоского пакета ADS
from .ads_logic import build_group_index


class ProfileModelManager(QObject):
    """
    Бухгалтер нашей базы данных. Управляет исключительно структурой DTO.
    Не знает о существовании кнопок, прогресс-баров, таблиц и потоков Selenium.
    Хранит данные в виде плоского Python-списка для максимальной производительности.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # Плоский список всех элементов (группы и профили) для O(1) доступа
        self._flat_model: list[dict[str, Any]] = []

    def get_model(self) -> list[dict[str, Any]]:
        """Возвращает готовую плоскую модель для привязки к RecyclerScrollArea."""
        return self._flat_model

    def _rebuild_indices_and_flags(self) -> None:
        """
        O(N) пересчет плоских индексов и флагов 'is_last_in_group'.
        Гарантирует абсолютную консистентность данных после любых мутаций списка
        (сборка, Drag-and-Drop, удаление).
        """
        total = len(self._flat_model)
        for i, row in enumerate(self._flat_model):
            row["flat_idx"] = i
            if not row.get("is_group"):
                is_last = True
                # Проверяем следующий элемент: если это конец списка или начало новой группы, значит текущий - последний
                if i + 1 < total:
                    next_row = self._flat_model[i + 1]
                    if not next_row.get("is_group") and next_row.get("group_name") == row.get("group_name"):
                        is_last = False
                row["is_last_in_group"] = is_last

    def _reindex_profile_numbers(self) -> None:
        """
        O(N) пересчет сквозных порядковых номеров для профилей (Model-Driven Indexing).
        Игнорирует папки-группы. Вызывается при любой мутации модели,
        чтобы UI мог забирать готовые номера за O(1) при скроллинге.
        """
        counter = 1
        for dto in self._flat_model:
            if not dto.get("is_group"):
                dto["display_num"] = counter
                counter += 1

    def build_model(self, profiles: list[dict[str, str]]) -> list[dict[str, Any]]:
        """
        Полное перестроение плоского списка профилей на основе данных от API.
        Включает сортировочный мост (Restore Pipeline) для восстановления порядка из реестра.
        
        :param profiles: Плоский список профилей от AdsPower API.
        :return: Плоский индекс (список словарей) для быстрого доступа O(1) к строкам.
        """
        # Resource Guard: Очищаем старую модель, Python GC сделает остальное
        self._flat_model.clear()
        
        # Загружаем сохраненную мапу сортировки из изолированной ветки State
        ui_prefs = load_ui_geometry()
        order_map_b64 = ui_prefs.get("profile_order_map", "{}")
        order_map: dict[str, list[str]] = {}
        
        if order_map_b64 and order_map_b64 != "{}":
            try:
                order_map = json.loads(base64.b64decode(order_map_b64).decode('utf-8'))
            except Exception as e:
                logger.warning(
                    f"Бухгалтер не смог расшифровать карту сортировки: {e}. Строим по дефолту.",
                    profile_names=["GLOBAL"], category="SYSTEM"
                )
        
        grouped = build_group_index(profiles)
        
        for gname in sorted(grouped.keys(), key=str.lower):
            profs = grouped[gname]
            
            # --- Сортировочный мост (Restore Pipeline) ---
            if gname in order_map:
                saved_order = order_map[gname]
                # Сортируем: известные ID по индексу, неизвестные улетают в конец (inf)
                profs.sort(
                    key=lambda p: saved_order.index(p.get("user_id", ""))
                    if p.get("user_id", "") in saved_order else float('inf')
                )
            
            # 1. Создание DTO для строки-группы (Папки)
            self._flat_model.append({
                "flat_idx": 0,  # Будет перезаписано в _rebuild_indices_and_flags
                "is_group": True,
                "group_name": gname,
                "group_count": len(profs)
            })
            
            # 2. Добавление дочерних профилей
            for p in profs:
                self._flat_model.append({
                    "flat_idx": 0,  # Будет перезаписано в _rebuild_indices_and_flags
                    "is_group": False,
                    "user_id": p.get("user_id", ""),
                    "name": p.get("name", ""),
                    "ip": p.get("ip", ""),
                    "ip_country": p.get("ip_country", ""),
                    "proxy_url": p.get("proxy_url", ""),  # Инъекция строки для Proxy Probe Engine
                    "latency": -1,                        # Дефолтный пинг до проверки радаром
                    "group_name": gname,
                    "state": ProfileState.UNKNOWN,
                    "status_tooltip": "Ожидание данных от радара телеметрии...",
                    "is_last_in_group": False,            # Будет перезаписано в _rebuild_indices_and_flags
                    "display_num": 0                      # Будет перезаписано в _reindex_profile_numbers
                })
        
        # 3. Финальная разметка индексов, флагов и сквозной нумерации
        self._rebuild_indices_and_flags()
        self._reindex_profile_numbers()
        
        return self._flat_model

    def update_proxy_info(self, flat_idx: int, new_ip: str, new_country: str, latency: int = -1) -> None:
        """
        Точечное (O(1)) обновление данных прокси (IP, ГЕО и Пинг) для конкретного профиля.
        Вызывается после успешного асинхронного зондирования (Proxy Probe Engine).
        """
        if 0 <= flat_idx < len(self._flat_model):
            dto = self._flat_model[flat_idx]
            if not dto.get("is_group"):
                dto["ip"] = new_ip
                dto["ip_country"] = new_country
                dto["latency"] = latency

    def drag_drop_row(self, start_flat_idx: int, target_flat_idx: int) -> bool:
        """
        Прецизионное перемещение строки (Drag-and-Drop) с автоматической
        корректировкой флагов и сохранением нового порядка в реестр.
        
        :param start_flat_idx: Исходный индекс профиля.
        :param target_flat_idx: Целевой индекс, куда профиль был сброшен.
        :return: True, если перемещение успешно выполнено.
        """
        if not (0 <= start_flat_idx < len(self._flat_model)) or not (0 <= target_flat_idx < len(self._flat_model)):
            return False
        if start_flat_idx == target_flat_idx:
            return False

        start_item = self._flat_model[start_flat_idx]
        target_item = self._flat_model[target_flat_idx]

        # Запрещаем двигать папки или перетаскивать профили на место папок
        if start_item.get("is_group") or target_item.get("is_group"):
            return False

        group_name = start_item.get("group_name")
        if group_name != target_item.get("group_name"):
            logger.warning(
                f"Попытка перетащить профиль {start_item.get('name')} в чужую папку пресечена.",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
            return False

        # S-Tier паттерн: извлекаем и вставляем (работает как для сдвига вниз, так и вверх)
        item = self._flat_model.pop(start_flat_idx)
        self._flat_model.insert(target_flat_idx, item)

        # Пересчитываем всю математику индексов и флагов за O(N)
        self._rebuild_indices_and_flags()
        self._reindex_profile_numbers()

        # Атомарно фиксируем новый расклад в реестре
        self._save_group_order(group_name)

        logger.info(
            f"Бухгалтер зафиксировал перестановку: профиль {item.get('name')} переехал на слот {target_flat_idx}.",
            profile_names=["GLOBAL"], category="SYSTEM"
        )
        return True

    def move_row(self, flat_idx: int, direction: str) -> int | None:
        """
        Безопасно перемещает строку на одну позицию (Вверх/Вниз) по клику на стрелочки.
        Делегирует фактическое перемещение универсальному движку drag_drop_row.
        
        :param flat_idx: Индекс перемещаемой строки в плоской модели.
        :param direction: "UP" или "DOWN".
        :return: Новый flat_idx перемещенной строки (для восстановления фокуса) или None.
        """
        if not (0 <= flat_idx < len(self._flat_model)):
            return None
            
        item = self._flat_model[flat_idx]
        
        # Группы двигать нельзя
        if item.get("is_group"):
            return None
            
        group_name = item.get("group_name", "")
        
        # 1. Ищем границы текущей группы в плоском списке
        group_start = -1
        group_end = -1
        
        for i, row in enumerate(self._flat_model):
            if row.get("is_group") and row.get("group_name") == group_name:
                group_start = i + 1
            elif group_start != -1 and row.get("is_group"):
                group_end = i - 1
                break
                
        if group_start != -1 and group_end == -1:
            # Если это последняя группа в списке
            group_end = len(self._flat_model) - 1
            
        # 2. Вычисляем целевой индекс
        target_idx = flat_idx - 1 if direction == "UP" else flat_idx + 1
        
        # Защита границ: нельзя выйти за пределы своей папки
        if target_idx < group_start or target_idx > group_end:
            return None
            
        # 3. Делегируем перемещение универсальному DND-движку
        if self.drag_drop_row(flat_idx, target_idx):
            return target_idx
            
        return None

    def _save_group_order(self, group_name: str) -> None:
        """
        Собирает актуальный порядок ID профилей в группе и атомарно сохраняет в реестр.
        Использует save_ui_geometry для изоляции от бизнес-настроек.
        """
        if not group_name:
            return
            
        new_order = []
        for row in self._flat_model:
            if not row.get("is_group") and row.get("group_name") == group_name:
                uid = row.get("user_id")
                if uid:
                    new_order.append(uid)
                    
        # Загружаем текущую мапу из ветки State, чтобы не затереть сортировку других групп
        ui_prefs = load_ui_geometry()
        order_map_b64 = ui_prefs.get("profile_order_map", "{}")
        order_map: dict[str, list[str]] = {}
        
        if order_map_b64 and order_map_b64 != "{}":
            try:
                order_map = json.loads(base64.b64decode(order_map_b64).decode('utf-8'))
            except Exception:
                pass
                
        order_map[group_name] = new_order
        
        try:
            new_json = json.dumps(order_map).encode('utf-8')
            new_b64 = base64.b64encode(new_json).decode('ascii')
            save_ui_geometry(profile_order_map_b64=new_b64)
        except Exception as e:
            logger.warning(
                f"Бухгалтер споткнулся при сохранении порядка профилей: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )