"""
Модуль: moduls/ads/profile_presenter.py
Назначение: Смотрящий презентер (Mediator) для панели профилей ADS.
Зона ответственности: Координация взаимодействия между пассивным интерфейсом (View),
                      плоской моделью данных (ModelManager) и многопоточным движком (ExecutionEngine).
                      Реализует защиту преходящих состояний (Transient State Protection),
                      сглаживание вывода логов (Debounce), гидратацию кэша профилей,
                      маршрутизацию сигналов асинхронного зондирования прокси с
                      синхронизацией глобального кэша (Event-Driven Cache Coherence) и
                      безопасную обработку транзакций Drag-and-Drop с гарантированным
                      снятием блокировок.
Интеграция: Слой Presentation Logic. Вызывается из `profile_panel.py`.
            Не имеет прямых зависимостей от графических виджетов (Duck Typing).
            Общается с внешним миром исключительно через сигналы и абстрактные вызовы.
            Адаптирован для работы с высокопроизводительной виртуальной каруселью (Recycler View)
            и поддерживает архитектуру Sweep Selection (Свайп-выделение) и DND.
            Оперирует строгими состояниями (ProfileState) вместо магических строк.
"""

import time
import json
import base64
from typing import Any

from PySide6.QtCore import QObject, Slot, QTimer

# Строгие абсолютные импорты ядра
from core.core import (
    register_profile_names,
    export_cache_dict,
    save_ui_geometry,
    update_profile_country
)
from core._constants import ProfileState, TRANSIT_STATES
from system.logger import logger


class ProfilePresenter(QObject):
    """
    Мозг панели профилей. Принимает радиограммы от кнопок, отдает приказы
    мотористам (ExecutionEngine) и запрашивает данные у бухгалтера (ModelManager).
    Работает исключительно с плоскими DTO и строгими Enum, обеспечивая O(1) производительность.
    """
    
    def __init__(
            self,
            view: Any,
            model_manager: Any,
            execution_engine: Any,
            parent: QObject | None = None
    ) -> None:
        """
        Инициализация презентера с внедрением зависимостей (Dependency Injection).

        :param view: Пассивный графический интерфейс (AdsProfilePanel).
        :param model_manager: Менеджер плоской модели (ProfileModelManager).
        :param execution_engine: Движок многопоточности (ProfileExecutionEngine).
        """
        super().__init__(parent)
        self.view = view
        self.model_manager = model_manager
        self.engine = execution_engine
        
        self._mass_action_running: bool = False
        
        # Таймер для сглаживания вывода текста стадий (защита от мерцания UI и перегрузки Event Loop)
        self._stage_timer = QTimer(self)
        self._stage_timer.setSingleShot(True)
        self._stage_timer.timeout.connect(self._flush_pending_stage)
        self._stage_pending_text: str | None = None
        self._stage_last_update: float = 0.0
        
        self._connect_signals()
    
    def _connect_signals(self) -> None:
        """Маршрутизация сигналов от движка и вьюпорта к презентеру."""
        self.engine.updateStatusSignal.connect(self.on_engine_status_update)
        self.engine.updateProxySignal.connect(self.on_engine_proxy_update)
        self.engine.progressSignal.connect(self.on_engine_progress)
        self.engine.stageSignal.connect(self.on_engine_stage)
        self.engine.updateStatsSignal.connect(self.on_engine_stats)
        self.engine.allTasksFinished.connect(self.on_engine_finished)
        
        # Подключаем сигнал DND от вьюпорта карусели напрямую, чтобы не засорять View
        if hasattr(self.view, 'recycler') and hasattr(self.view.recycler, 'rowDropped'):
            self.view.recycler.rowDropped.connect(self.on_row_dropped)
    
    # ===================== DATA HYDRATION & TELEMETRY =====================
    
    def update_profiles(self, profiles: list[dict[str, str]]) -> None:
        """
        Обновление плоской модели профилей на основе данных от API.
        Вызывается из главного окна после успешного теста соединения.
        """
        # Регистрируем имена профилей в глобальном реестре для логгера
        register_profile_names(profiles)
        
        # --- STATE HYDRATION (COLD START FIX) ---
        # Сохраняем слепок метаданных в реестр для мгновенного восстановления при следующем запуске
        try:
            fresh_cache_dict = export_cache_dict()
            fresh_cache_json = json.dumps(fresh_cache_dict)
            fresh_cache_b64 = base64.b64encode(fresh_cache_json.encode('utf-8')).decode('ascii')
            save_ui_geometry(profile_metadata_cache_b64=fresh_cache_b64)
        except Exception as e:
            logger.error(
                f"Сбой атомарного сохранения кэша метаданных: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )
        
        # Делегируем построение модели бухгалтеру
        self.model_manager.build_model(profiles)
        
        # Внимание: View (AdsProfilePanel) само заберет новую модель и передаст ее в карусель
    
    @Slot(object)
    def on_telemetry_update(self, active_ids: set[str] | None) -> None:
        """
        Слот для приема данных от автономного радара телеметрии (ATP).
        Обновляет статусы профилей за O(1), защищая преходящие состояния (Transient State Protection).
        """
        if active_ids is None:
            # API недоступен или выключен, игнорируем обновление, чтобы не вызывать мерцание
            return
        
        flat_model = self.model_manager.get_model()
        
        for dto in flat_model:
            if dto.get("is_group"):
                continue
                
            uid = dto.get("user_id", "")
            current_state = dto.get("state", ProfileState.UNKNOWN)
            flat_idx = dto.get("flat_idx", -1)
            
            is_active_now = uid in active_ids
            new_state = current_state
            new_tooltip = dto.get("status_tooltip", "")
            
            # Transient State Protection (БРОНЯ СОСТОЯНИЙ)
            # Если воркер сейчас работает с профилем, радар не имеет права перетирать статус
            if current_state == ProfileState.AWAITING_CLOSE:
                if not is_active_now:
                    # Процесс наконец-то умер в ОС. Снимаем броню!
                    new_state = ProfileState.CLOSED
                    new_tooltip = "Профиль закрыт"
                else:
                    # Процесс еще жив, продолжаем крутить спинер
                    continue
            elif current_state in TRANSIT_STATES:
                # Остальные транзитные состояния (запуск, прогрев) радар не трогает
                continue
            
            # Если висит ошибка, а профиль закрыт - оставляем ошибку для юзера.
            # Но если профиль вдруг оказался активен (например, юзер запустил руками) - обновляем на Активен.
            if current_state in (ProfileState.ERR_API, ProfileState.ERR_APP) and not is_active_now:
                continue
            
            if is_active_now and current_state != ProfileState.ACTIVE:
                new_state = ProfileState.ACTIVE
                new_tooltip = "Профиль активен (подтверждено радаром)"
            elif not is_active_now and current_state == ProfileState.ACTIVE:
                new_state = ProfileState.CLOSED
                new_tooltip = "Профиль закрыт"
            elif not is_active_now and current_state == ProfileState.UNKNOWN:
                new_state = ProfileState.CLOSED
                new_tooltip = "Профиль закрыт"
                
            if new_state != current_state:
                # Точечное обновление через движок карусели (Zero-CPU если карточка вне экрана)
                self.view.recycler.update_item_status(flat_idx, new_state, new_tooltip)
    
    # ===================== USER ACTIONS (VIEW -> PRESENTER) =====================
    
    @Slot(str, list)
    def on_launch_requested(self, mode: str, targets: list[tuple[int, str, str, str]]) -> None:
        """Обработка запроса на массовую операцию (open, close, restart)."""
        if not targets:
            logger.warning(
                "Не выбраны профили для операции. Конвейер простаивает.",
                profile_names=["GLOBAL"], category="PROFILE"
            )
            return
        
        self._mass_action_running = True
        self.view.set_buttons_enabled(False)
        self.view.update_stats(0, 0)
        self.view.set_progress(0)
        
        # Передаем задачу мотористам
        self.engine.run_mass_action(mode, targets)

    @Slot(list)
    def on_proxy_check_requested(self, targets: list[tuple[int, str, str, str]]) -> None:
        """
        Обработка запроса на массовую или точечную проверку прокси.
        Если список targets пуст (ничего не выделено), забираем в работу все профили.
        """
        if not targets:
            # Fallback: берем все профили из плоской модели
            flat_model = self.model_manager.get_model()
            for dto in flat_model:
                if not dto.get("is_group"):
                    targets.append((
                        dto.get("flat_idx", -1),
                        dto.get("user_id", ""),
                        dto.get("name", ""),
                        dto.get("proxy_url", "")
                    ))
                    
        if not targets:
            logger.warning(
                "Нет профилей для проверки прокси.",
                profile_names=["GLOBAL"], category="PROFILE"
            )
            return
            
        self._mass_action_running = True
        self.view.set_buttons_enabled(False)
        self.view.update_stats(0, 0)
        self.view.set_progress(0)
        
        # Передаем задачу мотористам в режиме "check_proxy"
        self.engine.run_mass_action("check_proxy", targets)
    
    @Slot(list, list)
    def on_hot_unlock_requested(self, plugin_ids: list[str], target_uids: list[str]) -> None:
        """Снайперский режим: точечный запуск автоматизации для переданных профилей."""
        targets: list[tuple[int, str, str, str]] = []
        flat_model = self.model_manager.get_model()
        
        # Матчинг сырых ID с плоской моделью (O(N))
        for dto in flat_model:
            if not dto.get("is_group") and dto.get("user_id") in target_uids:
                flat_idx = dto.get("flat_idx", -1)
                if flat_idx >= 0:
                    targets.append((
                        flat_idx,
                        dto.get("user_id", ""),
                        dto.get("name", ""),
                        dto.get("proxy_url", "")
                    ))
        
        if not targets:
            logger.warning(
                "Не удалось сопоставить целевые профили с моделью. Возможно, они были удалены.",
                profile_names=["GLOBAL"], category="PROFILE"
            )
            return
        
        self._mass_action_running = True
        self.view.set_buttons_enabled(False)
        self.view.update_stats(0, 0)
        self.view.set_progress(0)
        
        self.engine.run_mass_action("hot_unlock", targets, plugin_ids)
    
    @Slot()
    def on_stop_requested(self) -> None:
        """Обработка нажатия кнопки СТОП (Panic Stop)."""
        if self._mass_action_running:
            self.view.set_buttons_enabled(False)
            self.engine.stop_all()
    
    @Slot(int, str)
    def on_row_move_requested(self, flat_idx: int, direction: str) -> None:
        """
        Обработка запроса на перемещение строки (Вверх/Вниз) по клику на стрелочки.
        Работает напрямую с плоскими индексами.
        """
        self.model_manager.move_row(flat_idx, direction)
        
        # ГАРАНТИРОВАННОЕ СНЯТИЕ ЗАМКА:
        # Безусловный вызов set_model необходим для того, чтобы вьюпорт перерисовался
        # и гарантированно сбросил флаг _is_transacting, даже если перемещение было отклонено.
        self.view.recycler.set_model(self.model_manager.get_model(), self.view.recycler._selected_ids)

    @Slot(int, int)
    def on_row_dropped(self, start_flat_idx: int, target_flat_idx: int) -> None:
        """
        Обработка завершения Drag-and-Drop операции.
        Делегирует транзакцию бухгалтеру и синхронизирует вьюпорт.
        """
        # Пытаемся применить изменения в модели и записать их в реестр
        self.model_manager.drag_drop_row(start_flat_idx, target_flat_idx)
        
        # ГАРАНТИРОВАННОЕ СНЯТИЕ ЗАМКА:
        # В ЛЮБОМ СЛУЧАЕ (успех или отказ из-за нарушения границ папки) мы обязаны
        # вызвать set_model, чтобы вьюпорт перерисовался (откатил визуальные изменения при отказе)
        # и ГАРАНТИРОВАННО снял транзакционный замок (_is_transacting = False).
        self.view.recycler.set_model(self.model_manager.get_model(), self.view.recycler._selected_ids)
    
    # ===================== ENGINE CALLBACKS (ENGINE -> PRESENTER) =====================
    
    @Slot(int, object, str)
    def on_engine_status_update(self, flat_idx: int, state: object, tooltip: str) -> None:
        """
        Слот для обновления состояния профиля.
        Делегирует точечную отрисовку движку карусели.
        """
        self.view.recycler.update_item_status(flat_idx, state, tooltip)

    @Slot(int, str, str, str, int)
    def on_engine_proxy_update(self, flat_idx: int, uid: str, ip: str, country: str, latency: int) -> None:
        """
        Слот для точечного обновления данных прокси.
        Обновляет глобальный кэш (SSOT), сохраняет слепок на диск,
        обновляет плоскую модель (бухгалтера) и делегирует отрисовку движку карусели.
        """
        # 1. Обновляем Глобальный Сейф (SSOT)
        update_profile_country(uid, country, latency)
        
        # 2. Атомарно сбрасываем свежий слепок на диск (Cold Start Fix)
        try:
            fresh_cache = export_cache_dict()
            fresh_cache_json = json.dumps(fresh_cache)
            b64 = base64.b64encode(fresh_cache_json.encode('utf-8')).decode('ascii')
            save_ui_geometry(profile_metadata_cache_b64=b64)
        except Exception as e:
            logger.warning(
                f"Не удалось сохранить кэш метаданных после проверки прокси: {e}",
                profile_names=["GLOBAL"], category="SYSTEM"
            )

        # 3. Проливаем данные в локальную базу (O(1))
        self.model_manager.update_proxy_info(flat_idx, ip, country, latency)
        # 4. Дергаем перерисовку конкретной ячейки в UI (O(1))
        self.view.recycler.update_item_proxy(flat_idx, ip, country, latency)
    
    @Slot(int)
    def on_engine_progress(self, val: int) -> None:
        """Слот для обновления глобального прогресс-бара."""
        self.view.set_progress(val)
    
    @Slot(str)
    def on_engine_stage(self, text: str) -> None:
        """Слот для обновления текстового статуса этапа с защитой от мерцания (Debounce)."""
        self._stage_pending_text = text
        now = time.time()
        
        # Если с прошлого обновления прошло больше 100мс, обновляем мгновенно
        if now - self._stage_last_update > 0.1:
            self.view.set_progress_stage(text)
            self._stage_last_update = now
        # Иначе взводим таймер, чтобы не спамить UI-поток
        elif not self._stage_timer.isActive():
            self._stage_timer.start(100)
    
    @Slot()
    def _flush_pending_stage(self) -> None:
        """Принудительный вывод отложенного текста этапа."""
        if self._stage_pending_text:
            self.view.set_progress_stage(self._stage_pending_text)
            self._stage_last_update = time.time()
    
    @Slot(int, int, int)
    def on_engine_stats(self, done_steps: int, suc_count: int, err_count: int) -> None:
        """Слот для обновления счетчиков успеха и ошибок."""
        self.view.update_stats(suc_count, err_count)
    
    @Slot()
    def on_engine_finished(self) -> None:
        """Слот завершения массовой операции."""
        self._mass_action_running = False
        self.view.set_buttons_enabled(True)