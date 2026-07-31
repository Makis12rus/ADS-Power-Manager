"""
Модуль: moduls/ads/profile_panel.py
Назначение: Главная панель управления профилями ADS (Passive View).
Зона ответственности: Отрисовка виртуальной карусели профилей (Recycler View),
                      кнопок управления, прогресс-баров и маршрутизация команд
                      для асинхронного зондирования прокси (Proxy Probe Engine).
                      Выступает в роли "Пассивного Вида" (Passive View) в паттерне MVP.
                      Не содержит бизнес-логики, потоков или работы с реестром.
Интеграция: Слой Presentation. Является корнем композиции (Composition Root) для
            MVP-триады: инициализирует ModelManager, ExecutionEngine и Presenter.
            Обеспечивает 100% обратную совместимость с `main_window_gui.py` через Duck Typing.
            Адаптирована для работы с новым движком Frosted Glass Levitation и Vector Glow Buttons.
            Использует семантически чистый Bridge Pattern (get_multi_state_icon) для иконок.
            Импортирует карусель строго через фасад `profile_card_view.py`.
"""

from typing import Any

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar,
    QTextEdit, QFrame
)
from PySide6.QtCore import Qt, Signal, QModelIndex

# Строгие абсолютные импорты ядра
from core.core import ping_watchdog
from core.style import Styles, Colors, Graphics

# Импорты изолированных MVP-компонентов и фасада карусели
from moduls.ads.profile_card_view import RecyclerScrollArea
from moduls.ads.profile_model_manager import ProfileModelManager
from moduls.ads.profile_execution_engine import ProfileExecutionEngine
from moduls.ads.profile_presenter import ProfilePresenter


class AdsProfilePanel(QWidget):
    """
    Пассивный диспетчерский пульт.
    Отображает данные через виртуальную карусель и передает клики пользователя Смотрящему (Presenter).
    """
    
    # Сигнал оставлен для обратной совместимости с main_window_gui.py,
    # чтобы предотвратить AttributeError при инициализации.
    forceTelemetrySignal = Signal()
    
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("AdsProfilePanel")
        self.parent_widget = parent
        
        # КРИТИЧНО: Разрешаем кастомному QWidget транслировать QSS-стили и корректно
        # участвовать в композиции слоев, чтобы не обрезать тени и неоновые свечения карточек.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        
        # 1. Инициализация визуального каркаса
        self._setup_ui()
        
        # 2. COMPOSITION ROOT (Сборка MVP-триады)
        self.model_manager = ProfileModelManager(self)
        self.execution_engine = ProfileExecutionEngine(self)
        self.presenter = ProfilePresenter(
            view=self,
            model_manager=self.model_manager,
            execution_engine=self.execution_engine,
            parent=self
        )
        
        # 3. Маршрутизация сигналов от UI к Презентеру
        self._connect_signals()

    def _setup_ui(self) -> None:
        """Инициализация пользовательского интерфейса панели."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        
        # --- Панель кнопок ---
        btn_layout = QHBoxLayout()
        self.launch_btn = self._make_btn("Запустить", "play", "Запустить выбранные")
        self.restart_btn = self._make_btn("Перезапустить", "rotate-cw", "Перезапустить выбранные")
        self.close_btn = self._make_btn("Закрыть", "square-x", "Закрыть выбранные")
        self.proxy_btn = self._make_btn("Проверить прокси", "globe", "Асинхронное зондирование прокси-каналов (выделенные или все)")
        
        btn_layout.addWidget(self.launch_btn)
        btn_layout.addWidget(self.restart_btn)
        btn_layout.addWidget(self.close_btn)
        btn_layout.addWidget(self.proxy_btn)
        btn_layout.addStretch(1)
        layout.addLayout(btn_layout)
        
        # --- Заголовок и подсказка ---
        layout.addWidget(QLabel("Список Профилей AdsPower", styleSheet=f"color: {Colors.TXT_PRIMARY}; font-weight: bold; font-size: 16px;"))
        hint = QLabel(f"ℹ️ <span style='color:{Colors.TXT_SECONDARY};'>Используйте протяжку мыши для массового выделения. Зажмите Shift для выбора диапазона, Ctrl — для точечного выбора.</span>")
        hint.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(hint)
        
        # --- Виртуальная карусель профилей (Recycler View) ---
        self.recycler = RecyclerScrollArea(self)
        layout.addWidget(self.recycler, 1)
        
        # --- Буфер выделенных имен (Gutter-Safe Label Buffer) ---
        # Используем QTextEdit вместо QLabel для поддержки неограниченного списка имен
        # без риска разрыва макета (Layout Break).
        self.selected_profiles_buffer = QTextEdit()
        self.selected_profiles_buffer.setObjectName("SelectedNamesBuffer")
        self.selected_profiles_buffer.setReadOnly(True)
        self.selected_profiles_buffer.setFrameShape(QFrame.Shape.NoFrame)
        self.selected_profiles_buffer.setMaximumHeight(45)
        self.selected_profiles_buffer.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.selected_profiles_buffer.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.selected_profiles_buffer.setPlainText("Выделено профилей: 0")
        layout.addWidget(self.selected_profiles_buffer)
        
        # --- Статус бар и статистика ---
        status_line = QHBoxLayout()
        self.progress_stage_label = QLabel("")
        self.progress_stage_label.setStyleSheet("font-size: 13px; color: #39a1ff;")
        status_line.addWidget(self.progress_stage_label, 1)
        
        self.success_label = QLabel("Успешно: 0")
        self.success_label.setStyleSheet(f"color: {Colors.SUCCESS}; margin-left: 10px;")
        self.error_label = QLabel("Ошибок: 0")
        self.error_label.setStyleSheet(f"color: {Colors.ERROR}; margin-left: 10px;")
        status_line.addWidget(self.success_label)
        status_line.addWidget(self.error_label)
        layout.addLayout(status_line)
        
        # --- Прогресс бар ---
        prog_line = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setStyleSheet(Styles.PROGRESS_BAR)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        prog_line.addWidget(self.progress_bar, 1)
        
        self.percent_label = QLabel("0%")
        self.percent_label.setStyleSheet("color: #BBBBBB; min-width: 38px; qproperty-alignment: AlignRight;")
        prog_line.addWidget(self.percent_label)
        layout.addLayout(prog_line)
        
        # --- Подвал и кнопка СТОП ---
        bot_line = QHBoxLayout()
        info = QLabel("Выполняйте массовые операции только если уверены в параметрах!")
        info.setStyleSheet("color: #888888; font-size: 10px;")
        bot_line.addWidget(info, 1)
        
        self.stop_btn = QPushButton(" Стоп")
        self.stop_btn.setProperty("class", "stop-action")
        self.stop_btn.setStyleSheet(Styles.BTN_STOP)
        self.stop_btn.setEnabled(False)
        
        # Векторная иконка для кнопки СТОП с синхронизацией цветов QSS
        # Используем семантически правильный метод get_multi_state_icon
        stop_icon = Graphics.get_multi_state_icon(
            "square-x",
            normal_hex=Colors.ERROR_DARK,
            hover_hex=Colors.ERROR,
            disabled_hex="#555555",
            size=12
        )
        self.stop_btn.setIcon(stop_icon)
        
        bot_line.addWidget(self.stop_btn)
        layout.addLayout(bot_line)

    def _make_btn(self, text: str, icon_name: str, tooltip: str) -> QPushButton:
        """Хелпер для создания стандартной кнопки действия с векторной иконкой."""
        btn = QPushButton(f" {text}")
        btn.setProperty("class", "mass-action")
        btn.setStyleSheet(Styles.BTN_ACTION)
        btn.setToolTip(tooltip)
        
        # Синхронизируем цвета иконки с QSS-стилем BTN_ACTION (#CCCCCC -> #FFE066)
        # Используем семантически правильный метод get_multi_state_icon
        icon = Graphics.get_multi_state_icon(
            icon_name,
            normal_hex="#CCCCCC",
            hover_hex=Colors.ACCENT,
            disabled_hex="#555555",
            size=16
        )
        btn.setIcon(icon)
        return btn

    def _connect_signals(self) -> None:
        """Маршрутизация сигналов от UI к Презентеру."""
        # Кнопки массовых операций
        self.launch_btn.clicked.connect(lambda: self.presenter.on_launch_requested("open", self.get_selected_data()))
        self.restart_btn.clicked.connect(lambda: self.presenter.on_launch_requested("restart", self.get_selected_data()))
        self.close_btn.clicked.connect(lambda: self.presenter.on_launch_requested("close", self.get_selected_data()))
        
        # Кнопка асинхронного зондирования прокси
        self.proxy_btn.clicked.connect(lambda: self.presenter.on_proxy_check_requested(self.get_selected_data()))
        
        # Индивидуальные действия с карточек (Снайперский запуск и точечный шмон прокси)
        self.recycler.actionRequested.connect(self._on_card_action_requested)
        
        # Обновление счетчика выделения
        self.recycler.selectionChanged.connect(self.update_selection_label)
        
        # Кнопка СТОП
        self.stop_btn.clicked.connect(self.presenter.on_stop_requested)

    def _on_card_action_requested(self, mode: str, flat_idx: int) -> None:
        """
        Обработка клика по кнопкам (▶️, ♻️, ❌, 🌐) на конкретной карточке профиля.
        Формирует DTO-таргет и отправляет его в конвейер.
        """
        flat_model = self.model_manager.get_model()
        if 0 <= flat_idx < len(flat_model):
            dto = flat_model[flat_idx]
            if not dto.get("is_group"):
                target = [(
                    dto.get("flat_idx", -1),
                    dto.get("user_id", ""),
                    dto.get("name", ""),
                    dto.get("proxy_url", "")
                )]
                
                if mode == "check_proxy":
                    self.presenter.on_proxy_check_requested(target)
                else:
                    self.presenter.on_launch_requested(mode, target)

    # ===================== DUCK TYPING (BACKWARD COMPATIBILITY) =====================
    # Эти свойства необходимы для того, чтобы `main_window_gui.py` мог корректно
    # выполнить протокол Graceful Shutdown при закрытии окна, не зная о новой архитектуре.

    @property
    def _mass_action_running(self) -> bool:
        """Проброс флага активности конвейера из презентера."""
        return getattr(self.presenter, '_mass_action_running', False)

    @property
    def _stop_event(self) -> Any:
        """Проброс события отмены из движка для экстренной остановки."""
        return self.execution_engine._stop_event

    # ===================== PUBLIC API (DELEGATION) =====================

    def update_profiles(self, profiles: list[dict[str, str]]) -> None:
        """
        Внешний вызов от MainWindow. Делегируется презентеру,
        после чего обновленная плоская модель загружается в карусель.
        """
        self.presenter.update_profiles(profiles)
        
        # Синхронизируем карусель с новой плоской моделью
        flat_model = self.model_manager.get_model()
        self.recycler.set_model(flat_model, self.recycler._selected_ids)
        
        # Resource Guard: Подтверждаем сторожевому псу, что тяжелая отрисовка не повесила поток
        ping_watchdog()

    def on_telemetry_update(self, active_ids: set[str] | None) -> None:
        """Внешний вызов от TelemetryThread. Делегируется презентеру."""
        self.presenter.on_telemetry_update(active_ids)

    def run_hot_unlock(self, plugin_ids: list[str], target_uids: list[str]) -> None:
        """Внешний вызов от AdsSettingsPanel. Делегируется презентеру."""
        self.presenter.on_hot_unlock_requested(plugin_ids, target_uids)

    # ===================== UI HELPERS (CALLED BY PRESENTER) =====================

    def get_selected_data(self) -> list[tuple[int, str, str, str]]:
        """
        Возвращает список данных выделенных профилей: (flat_idx, uid, name, proxy_url).
        Работает за O(N) по плоской модели, сверяясь с set() выделенных ID.
        """
        selected_ids = self.recycler._selected_ids
        res: list[tuple[int, str, str, str]] = []
        
        for dto in self.model_manager.get_model():
            uid = dto.get("user_id", "")
            if uid in selected_ids and not dto.get("is_group"):
                res.append((
                    dto.get("flat_idx", -1),
                    uid,
                    dto.get("name", ""),
                    dto.get("proxy_url", "")
                ))
                
        return res

    def update_selection_label(self) -> None:
        """Обновляет текстовый буфер с полным списком выделенных профилей."""
        selected_ids = self.recycler._selected_ids
        count = len(selected_ids)
        
        names: list[str] = []
        for dto in self.model_manager.get_model():
            if dto.get("user_id") in selected_ids and not dto.get("is_group"):
                names.append(dto.get("name", ""))
                
        txt = f"Выделено профилей: {count}"
        if names:
            txt += f" — {', '.join(names)}"
            
        self.selected_profiles_buffer.setPlainText(txt)

    def set_buttons_enabled(self, enabled: bool) -> None:
        """Блокировка/разблокировка кнопок управления во время работы конвейера."""
        self.launch_btn.setEnabled(enabled)
        self.restart_btn.setEnabled(enabled)
        self.close_btn.setEnabled(enabled)
        self.proxy_btn.setEnabled(enabled)
        self.stop_btn.setEnabled(not enabled)

    def set_progress(self, val: int) -> None:
        """Обновление прогресс-бара."""
        self.progress_bar.setValue(val)
        self.percent_label.setText(f"{val}%")

    def set_progress_stage(self, txt: str) -> None:
        """Обновление текстового статуса этапа."""
        self.progress_stage_label.setText(txt)

    def update_stats(self, suc_count: int, err_count: int) -> None:
        """Обновление счетчиков успеха и ошибок."""
        self.success_label.setText(f"Успешно: {suc_count}")
        self.error_label.setText(f"Ошибок: {err_count}")

    # --- Заглушки для обратной совместимости со старым Presenter ---
    
    def expand_all(self) -> None:
        """Заглушка. В карусели нет скрытых узлов дерева."""
        pass

    def restore_selection(self, index: QModelIndex | None = None) -> None:
        """Заглушка. Выделение теперь хранится в set() и не сбрасывается при сортировке."""
        pass