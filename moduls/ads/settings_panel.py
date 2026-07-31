"""
Модуль: moduls/ads/settings_panel.py
Назначение: Панель настроек режима ADS (Presentation Layer).
Зона ответственности: Отрисовка формы конфигурации (API URL, тайминги, лимиты потоков),
                      динамическая генерация сетки паролей на основе загруженных плагинов
                      кошельков, управление реактивным пультом горячей автоматизации,
                      кастомизация визуального движка (Premium PCB Engine) и
                      настройка физики плавного скроллинга (Smooth Scroll Engine).
Интеграция: Взаимодействует с `core.core` для транзакционного сохранения настроек в реестр
            и безопасное хранилище (WCM). Реализует паттерн AutoSave-on-the-fly с
            использованием Debouncing. Слушает радар телеметрии для O(1) обновления чипсов,
            используя гидратированный кэш метаданных профилей.
            Интерфейс построен на базе премиальных компонентов GlassTile, DebossedLineEdit,
            SmoothScrollArea и EngravedLabel.
            Является частью плоского пакета `moduls/ads/`.
"""

from typing import Any

from PySide6.QtCore import Qt, Signal, QSignalBlocker, QTimer, QEvent, QObject, Slot
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QGridLayout, QCheckBox, QFrame, QMessageBox, QLayout
)

# Строгие абсолютные импорты ядра (Monorepo Style)
from core.core import (
    load_settings_from_registry,
    save_settings_to_registry,
    delete_settings_from_registry,
    open_registry_in_regedit,
    REG_PATH_CONFIG,
    REG_PATH_STATE,
    plugin_manager,
    get_profile_metadata,
    export_cache_dict,
    load_ui_geometry,
    save_ui_geometry
)
from system.logger import logger, log_action
from core.style import (
    Styles, Colors, AutoSaveIndicator, Graphics, GlassTile,
    DebossedLineEdit, SecureDebossedLineEdit, SmoothScrollArea,
    EngravedLabel
)

# Строгие относительные импорты внутри плоского пакета ADS
from .flow_layout import FlowLayout


class ProfileChip(QPushButton):
    """
    Интерактивный виджет профиля (Чипс) для пульта горячей автоматизации.
    Наследуется от QPushButton для нативной поддержки состояний :checked и :hover.
    """
    def __init__(self, uid: str, name: str, country: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.uid = uid
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        
        # Декларативная стилизация псевдосостояний (BigTech UI Pattern)
        self.setStyleSheet(f"""
            QPushButton {{
                background-color: #35393C;
                color: {Colors.TXT_PRIMARY};
                border: 1px solid #5A5A5A;
                border-radius: 12px;
                padding: 4px 12px;
                font-size: 12px;
                
            }}
            QPushButton:hover {{
                border: 1px solid #888888;
                background-color: #3E4346;
            }}
            QPushButton:checked {{
                background-color: rgba(255, 215, 0, 0.1);
                border: 2px solid #FFD700;
                color: #FFD700;
            }}
            QPushButton:checked:hover {{
                background-color: rgba(255, 215, 0, 0.2);
                border: 2px solid #FFE066;
            }}
        """)
        
        # Получаем иконку флага. Буквенный код страны игнорируем для чистоты UI.
        icon, _ = Graphics.get_country_icon(country)
        self.setText(f"  {name}")
        
        if icon:
            self.setIcon(icon)


class AdsSettingsPanel(QWidget):
    """
    Панель настроек режима ADS.
    Обеспечивает интерфейс для ввода системных параметров, паролей кошельков,
    кастомизации визуального движка (PCB Engine) и физики скролла.
    Работает в режиме реактивного автосохранения (AutoSave-on-the-fly).
    """
    # Сигнал для маршрутизации пакетного горячего запуска через MainWindow
    # Передает: (список ID плагинов, список ID целевых профилей)
    hotUnlockBatchRequested = Signal(list, list)
    
    # Сигнал для передачи статуса автосохранения в ModeBar (Mediator Pattern)
    saveStatusChanged = Signal(int, str)
    
    # Сигнал для уведомления главного окна о необходимости перепечь текстуру фона
    themeUpdated = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.parent_widget = parent

        # Словарь для хранения динамически созданных виджетов кошельков
        # Формат: { plugin_id: {"chk": QCheckBox, "inp": SecureDebossedLineEdit, "pwd_key": str, "name": str, "container": QWidget} }
        self._dynamic_inputs: dict[str, dict[str, Any]] = {}
        
        # Словарь для Diffing Engine (State Reconciliation)
        self._rendered_profile_chips: dict[str, ProfileChip] = {}

        # Таймер дебаунса для защиты реестра и WCM от спама при вводе текста
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(750)  # 750 мс задержки после последнего нажатия клавиши
        self._save_timer.timeout.connect(self._execute_save)

        self._setup_ui()
        self.load_settings()

    def _create_tile(self, title_text: str) -> tuple[GlassTile, QVBoxLayout]:
        """Хелпер для создания стандартизированной стеклянной карточки с гравированным заголовком."""
        tile = GlassTile(self)
        lay = QVBoxLayout(tile)
        lay.setContentsMargins(24, 20, 24, 24)
        lay.setSpacing(16)
        
        if title_text:
            # Используем премиальную типографику для заголовков карточек
            lbl = EngravedLabel(title_text)
            lbl.setStyleSheet(f"font-size: 15px; font-weight: bold; color: {Colors.TXT_PRIMARY};")
            lay.addWidget(lbl)
            
        return tile, lay

    def _setup_ui(self) -> None:
        """Инициализация пользовательского интерфейса на базе GlassTile."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Интеграция Smooth Scroll Engine
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        # Прозрачный фон критически важен для корректного отображения теней GlassTile
        scroll.setStyleSheet("background: transparent; border: none;")

        content_widget = QWidget()
        content_widget.setStyleSheet("background: transparent;")

        layout = QVBoxLayout(content_widget)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(24)  # Увеличенный отступ между карточками для теней

        # --- 1. Карточка: Интеграция API ---
        api_tile, api_lay = self._create_tile("🌐 Интеграция API")
        self.api_url = self._add_input(api_lay, "Адрес локального API AdsPower:", "http://local.adspower.com:50395")
        layout.addWidget(api_tile)

        # --- 2. Карточка: Безопасное хранилище кошельков ---
        wallets_tile, wallets_lay = self._create_tile("🔐 Безопасное хранилище кошельков")

        self.master_chk = QCheckBox("🔓 Авто-разблокировка кошельков (Глобальный рубильник)")
        self.master_chk.setStyleSheet(Styles.CHECKBOX + f" font-weight: bold; color: {Colors.ACCENT}; margin-bottom: 8px;")
        self.master_chk.setTristate(True)
        self.master_chk.stateChanged.connect(self._on_master_toggled)
        wallets_lay.addWidget(self.master_chk)

        self.wallets_grid = QGridLayout()
        self.wallets_grid.setHorizontalSpacing(24)
        self.wallets_grid.setVerticalSpacing(16)

        # Динамическое построение сетки кошельков
        self._build_dynamic_wallet_grid()

        wallets_lay.addLayout(self.wallets_grid)
        layout.addWidget(wallets_tile)

        # --- 3. Карточка: Пульт горячей автоматизации ---
        hot_tile, hot_lay = self._create_tile("⚡ Пульт горячей автоматизации")

        # Профили (Строка заголовка со статистикой и кнопкой)
        prof_header_lay = QHBoxLayout()
        prof_header_lay.setContentsMargins(0, 0, 0, 0)
        prof_header_lay.setSpacing(10)

        prof_lbl = EngravedLabel("📍 Активные профили:")
        prof_lbl.setStyleSheet(f"color: {Colors.TXT_SECONDARY}; font-size: 13px; font-weight: bold;")
        prof_header_lay.addWidget(prof_lbl)

        self.prof_stats_lbl = QLabel("|  Всего: 0  •  Активно: 0  •  Выделено: 0")
        self.prof_stats_lbl.setStyleSheet(f"color: {Colors.TXT_DIM}; font-size: 12px; font-weight: bold;")
        prof_header_lay.addWidget(self.prof_stats_lbl)

        prof_header_lay.addStretch(1)

        self.btn_toggle_select = QPushButton("Выделить все")
        self.btn_toggle_select.setStyleSheet(Styles.BTN_LOG_MINI)
        self.btn_toggle_select.setFixedWidth(120)
        self.btn_toggle_select.setFixedHeight(24)
        self.btn_toggle_select.setEnabled(False)
        self.btn_toggle_select.clicked.connect(self._on_toggle_select_clicked)
        prof_header_lay.addWidget(self.btn_toggle_select)

        hot_lay.addLayout(prof_header_lay)
        
        self.prof_placeholder = QLabel("Ожидание: Нет активных профилей. Запустите профили в AdsPower...")
        self.prof_placeholder.setStyleSheet(f"color: {Colors.TXT_DIM}; font-size: 13px;")
        hot_lay.addWidget(self.prof_placeholder)

        # Резиновый контейнер для чипсов профилей (Size Invalidation Pipeline)
        self.prof_container = QWidget()
        self.prof_container.setStyleSheet("background: transparent;")
        self.prof_layout = FlowLayout(self.prof_container, margin=0, hSpacing=8, vSpacing=8)
        self.prof_layout.heightChanged.connect(self.prof_container.setMinimumHeight)
        self.prof_container.hide()
        hot_lay.addWidget(self.prof_container)

        # Кошельки
        wall_lbl = EngravedLabel("🔑 Готовые кошельки:")
        wall_lbl.setStyleSheet(f"color: {Colors.TXT_SECONDARY}; font-size: 13px; font-weight: bold; margin-top: 8px;")
        hot_lay.addWidget(wall_lbl)

        self.wall_container = QWidget()
        self.wall_container.setStyleSheet("background: transparent;")
        self.wall_layout = FlowLayout(self.wall_container, margin=0, hSpacing=8, vSpacing=8)
        self.wall_layout.heightChanged.connect(self.wall_container.setMinimumHeight)
        hot_lay.addWidget(self.wall_container)

        self.btn_run_hot = QPushButton("⚡ Запустить разблокировку")
        self.btn_run_hot.setProperty("class", "btn-hot-run")
        self.btn_run_hot.setStyleSheet(Styles.BTN_HOT_RUN)
        self.btn_run_hot.setEnabled(False)
        self.btn_run_hot.clicked.connect(self._on_run_hot_clicked)
        hot_lay.addWidget(self.btn_run_hot)

        layout.addWidget(hot_tile)

        # --- 4. Карточка: Системные параметры конвейера ---
        params_tile, params_lay = self._create_tile("⚙️ Системные параметры конвейера")
        
        params_grid = QGridLayout()
        params_grid.setHorizontalSpacing(40)
        params_grid.setVerticalSpacing(16)

        self.retry_cnt = self._create_mini_input("🔁 Попыток разблокировки:", "3")
        self.d_start = self._create_mini_input("⏳ Задержка запуска (сек):", "5")
        self.d_stop = self._create_mini_input("⏱️ Задержка закрытия (сек):", "1")
        self.sel_pool = self._create_mini_input("🧵 Потоков Selenium:", "3")

        self._add_to_grid(params_grid, self.retry_cnt, 0, 0)
        self._add_to_grid(params_grid, self.d_start, 0, 1)
        self._add_to_grid(params_grid, self.d_stop, 1, 0)
        self._add_to_grid(params_grid, self.sel_pool, 1, 1)

        params_lay.addLayout(params_grid)
        layout.addWidget(params_tile)
        
        # --- 5. Карточка: Оформление интерфейса (Premium PCB) ---
        theme_tile, theme_lay = self._create_tile("🎨 Оформление интерфейса (Premium PCB)")
        
        theme_grid = QGridLayout()
        theme_grid.setHorizontalSpacing(40)
        theme_grid.setVerticalSpacing(16)

        self.inp_bg_base = self._create_mini_input("🌌 Базовый цвет (HEX):", Colors.BG_SAPPHIRE_BASE, width=80)
        self.inp_bg_pcb = self._create_mini_input("⚡ Цвет дорожек (HEX):", Colors.PCB_NEON_BLUE, width=80)
        self.inp_pcb_opacity = self._create_mini_input("🌫️ Плотность матового щита (%):", "85", width=60)
        self.inp_pcb_thickness = self._create_mini_input("📏 Толщина дорожек (px):", "2", width=60)
        self.inp_pcb_seed = self._create_mini_input("🎲 Зерно узора (Seed):", "42", width=60)
        self.inp_pcb_complexity = self._create_mini_input("🕸️ Плотность (1-10):", "5", width=60)

        self._add_to_grid(theme_grid, self.inp_bg_base, 0, 0)
        self._add_to_grid(theme_grid, self.inp_bg_pcb, 0, 1)
        self._add_to_grid(theme_grid, self.inp_pcb_opacity, 1, 0)
        self._add_to_grid(theme_grid, self.inp_pcb_thickness, 1, 1)
        self._add_to_grid(theme_grid, self.inp_pcb_seed, 2, 0)
        self._add_to_grid(theme_grid, self.inp_pcb_complexity, 2, 1)

        theme_lay.addLayout(theme_grid)
        layout.addWidget(theme_tile)
        
        # --- 6. Карточка: Физика прокрутки (Smooth Scroll) ---
        scroll_tile, scroll_lay = self._create_tile("🛼 Физика прокрутки (Smooth Scroll)")
        
        scroll_grid = QGridLayout()
        scroll_grid.setHorizontalSpacing(40)
        scroll_grid.setVerticalSpacing(16)

        self.inp_scroll_duration = self._create_mini_input("⏱️ Длительность (мс):", "200", width=60)
        self.inp_scroll_step = self._create_mini_input("📏 Шаг колеса (px):", "120", width=60)

        self._add_to_grid(scroll_grid, self.inp_scroll_duration, 0, 0)
        self._add_to_grid(scroll_grid, self.inp_scroll_step, 0, 1)

        scroll_lay.addLayout(scroll_grid)
        layout.addWidget(scroll_tile)

        layout.addSpacing(10)

        # --- 7. Action Buttons (Вне карточек) ---
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        
        self.del_btn = QPushButton('🗑️ Удалить из реестра')
        self.reg_btn = QPushButton('📂 Посмотреть реестр')

        for b in [self.reg_btn, self.del_btn]:
            b.setProperty("class", "mass-action")
            b.setStyleSheet(Styles.BTN_ACTION)
            b.setMinimumHeight(36)
            btn_row.addWidget(b)

        layout.addLayout(btn_row)

        # --- 8. Info Label ---
        reg_info = (
            f"<html><head/><body><p align='center' style='line-height:1.3;'>"
            f"📍 <b>Реестр (Настройки):</b> HKEY_CURRENT_USER\\{REG_PATH_CONFIG}<br>"
            f"📍 <b>Реестр (Интерфейс):</b> HKEY_CURRENT_USER\\{REG_PATH_STATE}<br>"
            f"🔐 <b>Безопасность:</b> Пароли зашифрованы в Windows Credential Manager<br>"
            f"<span style='color:#666;'>Данные хранятся локально и привязаны к текущему пользователю Windows.</span>"
            f"</p></body></html>"
        )
        desc = QLabel(reg_info)
        desc.setStyleSheet("color:#767676; font-size:12px; margin-top: 8px;")
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(desc)

        layout.addStretch(1)

        scroll.setWidget(content_widget)
        main_layout.addWidget(scroll)

        self.del_btn.clicked.connect(self.delete_settings)
        self.reg_btn.clicked.connect(self.open_reg)

    # ===================== AUTOSAVE MECHANICS =====================

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """
        Перехватчик событий (On-Blur Flush).
        Если текстовое поле теряет фокус, а таймер дебаунса еще тикает,
        мы принудительно сбрасываем буфер в реестр. Это гарантирует, что пароль
        сохранится до того, как пользователь нажмет кнопку запуска профиля.
        """
        if event.type() == QEvent.Type.FocusOut:
            if self._save_timer.isActive():
                self._save_timer.stop()
                self._execute_save()
        return super().eventFilter(obj, event)

    def _on_text_changed(self, *args: Any) -> None:
        """Обработчик непрерывного ввода (Debounced)."""
        self.saveStatusChanged.emit(AutoSaveIndicator.SAVING, "Сохранение...")
        self._save_timer.start()
        self._recalculate_hot_automation_state()

    def _on_discrete_changed(self) -> None:
        """Обработчик дискретных событий (чекбоксы). Сохраняет мгновенно."""
        self.saveStatusChanged.emit(AutoSaveIndicator.SAVING, "Сохранение...")
        self._save_timer.stop()
        self._execute_save()

    def force_save(self) -> None:
        """
        Принудительный сброс буфера (Zero-Loss Flush).
        Вызывается из MainWindow.closeEvent перед уничтожением приложения.
        """
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._execute_save()

    def _execute_save(self) -> None:
        """Сбор данных с формы и транзакционное сохранение в реестр и WCM."""
        s = {
            "api_url": self.api_url.text().strip(),
            "wallet_retry_count": self.retry_cnt.text().strip(),
            "delay_start": self.d_start.text().strip(),
            "delay_stop": self.d_stop.text().strip(),
            "selenium_pool": self.sel_pool.text().strip(),
            "auto_unlock_wallets": "1" if self.master_chk.checkState() != Qt.CheckState.Unchecked else "0",
        }

        # Динамическое сохранение состояний плагинов
        for p_id, data in self._dynamic_inputs.items():
            chk = data["chk"]
            inp = data["inp"]
            pwd_key = data["pwd_key"]

            s[f"unlock_{p_id}_enabled"] = "1" if chk.isChecked() else "0"
            s[pwd_key] = inp.text()

        # Сохранение бизнес-настроек в ветку Config
        ok, msg = save_settings_to_registry(s)
        
        # Сохранение визуальных настроек в ветку State
        ui_ok, ui_msg = save_ui_geometry(
            bg_base_color=self.inp_bg_base.text().strip(),
            bg_pcb_color=self.inp_bg_pcb.text().strip(),
            bg_pcb_opacity=self.inp_pcb_opacity.text().strip(),
            bg_pcb_thickness=self.inp_pcb_thickness.text().strip(),
            bg_pcb_seed=self.inp_pcb_seed.text().strip(),
            bg_pcb_complexity=self.inp_pcb_complexity.text().strip(),
            smooth_scroll_duration=self.inp_scroll_duration.text().strip(),
            smooth_scroll_step=self.inp_scroll_step.text().strip()
        )

        if ok and ui_ok:
            self.saveStatusChanged.emit(AutoSaveIndicator.IDLE, "Настройки сохранены")
            # Успешные сохранения логируются тихо, анти-спам фильтр логгера предотвратит флуд
            logger.success("Настройки успешно сохранены.", profile_names=["GLOBAL"], category="SETTINGS")
            # Уведомляем главное окно о необходимости перепечь текстуру фона
            self.themeUpdated.emit()
        else:
            err_msg = msg if not ok else ui_msg
            self.saveStatusChanged.emit(AutoSaveIndicator.ERROR, err_msg)
            logger.error(f"Ошибка автосохранения: {err_msg}", profile_names=["GLOBAL"], category="SETTINGS")

    # ===================== UI LOGIC & BINDINGS =====================

    def _build_dynamic_wallet_grid(self) -> None:
        """
        Динамически запрашивает у PluginManager список доступных кошельков
        и строит сетку чекбоксов и полей ввода.
        """
        # Очистка старых виджетов (если метод вызывается повторно)
        for data in self._dynamic_inputs.values():
            data["chk"].deleteLater()
            data["inp"].deleteLater()
            if "container" in data:
                data["container"].deleteLater()
        self._dynamic_inputs.clear()

        manifests = plugin_manager.get_all_manifests()

        row, col = 0, 0
        max_cols = 3

        for manifest in manifests:
            p_id = manifest.get("id", "")
            name = manifest.get("name", "Unknown")
            pwd_key = manifest.get("password_key", "")

            if not p_id or not pwd_key:
                continue

            container, chk, inp = self._create_pwd_widget(name, f"Пароль {name}")
            self.wallets_grid.addWidget(container, row, col)

            self._dynamic_inputs[p_id] = {
                "chk": chk,
                "inp": inp,
                "pwd_key": pwd_key,
                "name": name,
                "container": container
            }

            col += 1
            if col >= max_cols:
                col = 0
                row += 1

    def _update_master_checkbox_state(self) -> None:
        """Синхронизирует состояние мастер-тумблера с дочерними чекбоксами."""
        if not self._dynamic_inputs:
            return

        checked_count = sum(1 for d in self._dynamic_inputs.values() if d["chk"].isChecked())
        total = len(self._dynamic_inputs)

        with QSignalBlocker(self.master_chk):
            if checked_count == 0:
                self.master_chk.setCheckState(Qt.CheckState.Unchecked)
            elif checked_count == total:
                self.master_chk.setCheckState(Qt.CheckState.Checked)
            else:
                self.master_chk.setCheckState(Qt.CheckState.PartiallyChecked)

    def _on_master_toggled(self, state: int) -> None:
        """Обработчик глобального рубильника (Мастер-чекбокс)."""
        is_checked = (state == 2)
        if state == 1:
            is_checked = True
            with QSignalBlocker(self.master_chk):
                self.master_chk.setCheckState(Qt.CheckState.Checked)

        for data in self._dynamic_inputs.values():
            chk = data["chk"]
            with QSignalBlocker(chk):
                chk.setChecked(is_checked)

        self._recalculate_hot_automation_state()
        self._on_discrete_changed()

    def _on_child_toggled(self, state: int) -> None:
        """Обработчик индивидуальных чекбоксов кошельков."""
        self._update_master_checkbox_state()
        self._recalculate_hot_automation_state()
        self._on_discrete_changed()

    def _on_toggle_select_clicked(self) -> None:
        """Слот для пакетного выделения/снятия выделения активных профилей."""
        active_chips = list(self._rendered_profile_chips.values())
        if not active_chips:
            return

        # Считаем, сколько чипсов выделено сейчас
        checked_count = sum(1 for chip in active_chips if chip.isChecked())
        target_state = checked_count < len(active_chips)  # Если выделены не все — выделяем все, иначе снимаем

        # Пакетное изменение состояний без триггера промежуточных сигналов (Signal Gate)
        for chip in active_chips:
            chip.blockSignals(True)
            chip.setChecked(target_state)
            chip.blockSignals(False)

        # Финальный принудительный пересчет состояния пульта и статистики
        self._recalculate_hot_automation_state()

    def _add_input(self, layout: QVBoxLayout, label_text: str, placeholder: str) -> DebossedLineEdit:
        """Хелпер для создания стандартного поля ввода с лейблом."""
        lbl = QLabel(label_text)
        lbl.setStyleSheet(Styles.LABEL_SETTING)
        inp = DebossedLineEdit()
        inp.setPlaceholderText(placeholder)
        
        inp.textChanged.connect(self._on_text_changed)
        inp.installEventFilter(self)
        
        layout.addWidget(lbl)
        layout.addWidget(inp)
        return inp

    def _create_pwd_widget(self, label_text: str, placeholder: str) -> tuple[QWidget, QCheckBox, SecureDebossedLineEdit]:
        """Создает контейнер с чекбоксом и безопасным полем ввода пароля."""
        container = QWidget()
        lay = QVBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        chk = QCheckBox(label_text)
        chk.setStyleSheet(Styles.CHECKBOX)
        chk.stateChanged.connect(self._on_child_toggled)

        inp = SecureDebossedLineEdit()
        inp.setPlaceholderText(placeholder)
        
        inp.textChanged.connect(self._on_text_changed)
        inp.installEventFilter(self)

        lay.addWidget(chk)
        lay.addWidget(inp)

        return container, chk, inp

    def _clear_flow_layout(self, layout: QLayout) -> None:
        """
        Resource Guard: Гарантированное удаление C++ объектов виджетов из памяти.
        Предотвращает утечки памяти при перестроении динамических списков.
        """
        if layout is not None:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    layout.removeWidget(widget)
                    widget.deleteLater()

    # ===================== DIFFING ENGINE & HOT AUTOMATION =====================

    @Slot(object)
    def on_telemetry_update(self, active_ids_set: set[str] | None) -> None:
        """
        Слот для приема данных от автономного радара телеметрии (ATP).
        Реализует алгоритм State Reconciliation (Diffing Engine) для O(N) обновления
        чипсов профилей без мерцания UI и сброса выделения.
        """
        if active_ids_set is None:
            return
            
        current_rendered = set(self._rendered_profile_chips.keys())
        
        stale_ids = current_rendered - active_ids_set
        new_ids = active_ids_set - current_rendered
        
        if not stale_ids and not new_ids:
            return  # Изменений нет, экономим CPU
            
        # 1. Deletions (Zero-RAM Leak Guard)
        for uid in stale_ids:
            chip = self._rendered_profile_chips.pop(uid)
            self.prof_layout.removeWidget(chip)
            chip.deleteLater()  # Обязательно выжигаем C++ объект
            
        # 2. Additions (Lazy Rendering)
        for uid in new_ids:
            meta = get_profile_metadata(uid)
            chip = ProfileChip(uid, meta.name, meta.country)
            
            # State Hydration: Подхватываем закэшированный пинг при холодном старте
            if meta.latency >= 0:
                chip.setToolTip(f"Пинг: {meta.latency} мс")
                
            chip.clicked.connect(self._recalculate_hot_automation_state)
            self.prof_layout.addWidget(chip)
            self._rendered_profile_chips[uid] = chip
            
        # 3. Toggle Placeholders
        has_chips = len(self._rendered_profile_chips) > 0
        self.prof_placeholder.setVisible(not has_chips)
        self.prof_container.setVisible(has_chips)
        
        # 4. Validation & Geometry Update
        self._recalculate_hot_automation_state()
        self.prof_container.updateGeometry()

    @Slot(int, str, str, str, int)
    def on_proxy_updated(self, flat_idx: int, uid: str, ip: str, country: str, latency: int) -> None:
        """
        Слот для точечного обновления иконки флага и тултипа на чипсе профиля.
        Вызывается по сигналу от асинхронного зонда (через мост в главном окне).
        Обеспечивает O(1) перекраску флагов без Layout Reflow.
        """
        chip = self._rendered_profile_chips.get(uid)
        if chip:
            icon, _ = Graphics.get_country_icon(country)
            if icon:
                chip.setIcon(icon)
            
            # Добавляем информацию о пинге в тултип чипса
            if latency >= 0:
                chip.setToolTip(f"IP: {ip} | Пинг: {latency} мс")
            else:
                chip.setToolTip(f"IP: {ip} | Пинг: Ошибка")

    def _recalculate_hot_automation_state(self, *args: Any) -> None:
        """Реактивное обновление состояния пульта на основе паролей и выделения."""
        ready_wallets = []

        for plugin_id, data in self._dynamic_inputs.items():
            if data["chk"].isChecked() and data["inp"].text().strip():
                ready_wallets.append((plugin_id, data["name"]))

        checked_uids = [uid for uid, chip in self._rendered_profile_chips.items() if chip.isChecked()]

        # 1. Расчет статистики за O(1) без блокировок
        total_cached = len(export_cache_dict())
        total_active = len(self._rendered_profile_chips)
        total_checked = len(checked_uids)

        # 2. Обновление текстового табло
        self.prof_stats_lbl.setText(
            f"|  Всего: {total_cached}  •  "
            f"Активно: {total_active}  •  "
            f"Выделено: {total_checked}"
        )

        # 3. Интеллектуальный контроль кнопки массового выбора
        self.btn_toggle_select.setEnabled(total_active > 0)
        if total_active > 0 and total_checked == total_active:
            self.btn_toggle_select.setText("Снять выделение")
        else:
            self.btn_toggle_select.setText("Выделить все")

        # Очищаем старые бейджи кошельков
        self._clear_flow_layout(self.wall_layout)

        if not ready_wallets:
            lbl_w = QLabel("Ожидание: Выберите кошельки с паролями...")
            lbl_w.setStyleSheet(f"color: {Colors.TXT_DIM}; font-size: 13px;")
            self.wall_layout.addWidget(lbl_w)
        else:
            # Рендерим бейджи кошельков
            for pid, wname in ready_wallets:
                lbl = QLabel(f"🔑 {wname}")
                lbl.setProperty("class", "chip-wallet")
                lbl.setStyleSheet(Styles.CHIP_WALLET)
                self.wall_layout.addWidget(lbl)

        # 4. Обновление главной кнопки запуска
        if checked_uids and ready_wallets:
            self.btn_run_hot.setEnabled(True)
            self.btn_run_hot.setText(f"⚡ Запустить разблокировку ({len(ready_wallets)} кошельков на {len(checked_uids)} профилях)")
        else:
            self.btn_run_hot.setEnabled(False)
            self.btn_run_hot.setText("⚡ Запустить разблокировку")
            
        # Форсируем перерасчет геометрии контейнера кошельков
        self.wall_container.updateGeometry()

    def _on_run_hot_clicked(self) -> None:
        """Сбор данных и отправка сигнала на запуск пакета."""
        # Принудительно сохраняем черновик перед запуском автоматизации
        self.force_save()
        
        ready_plugin_ids = []
        for plugin_id, data in self._dynamic_inputs.items():
            if data["chk"].isChecked() and data["inp"].text().strip():
                ready_plugin_ids.append(plugin_id)

        target_uids = [uid for uid, chip in self._rendered_profile_chips.items() if chip.isChecked()]

        if ready_plugin_ids and target_uids:
            self.hotUnlockBatchRequested.emit(ready_plugin_ids, target_uids)

    # ===================== UTILS =====================

    def _create_mini_input(self, label_text: str, default_val: str, width: int = 60) -> DebossedLineEdit:
        """Создает компактное поле ввода для числовых параметров и HEX-кодов."""
        container = QWidget()
        lay = QHBoxLayout(container)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        lbl = QLabel(label_text)
        lbl.setStyleSheet("color: #F0F0F0; font-size: 13px;")

        inp = DebossedLineEdit()
        inp.setText(default_val)
        inp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        inp.setFixedWidth(width)
        
        inp.textChanged.connect(self._on_text_changed)
        inp.installEventFilter(self)

        lay.addWidget(lbl)
        lay.addStretch(1)
        lay.addWidget(inp)

        # Сохраняем ссылку на контейнер для корректного добавления в QGridLayout
        setattr(inp, '_container_widget', container)
        return inp

    def _add_to_grid(self, grid: QGridLayout, input_widget: DebossedLineEdit, row: int, col: int) -> None:
        """Добавляет мини-инпут (вместе с его контейнером) в сетку."""
        container = getattr(input_widget, '_container_widget', None)
        if container is not None:
            grid.addWidget(container, row, col)

    def load_settings(self) -> None:
        """Загрузка настроек из реестра и применение их к UI без триггера автосохранения."""
        s = load_settings_from_registry()
        ui_s = load_ui_geometry()
        
        # КРИТИЧНО: Блокируем сигналы именно на внутреннем QLineEdit,
        # так как DebossedLineEdit пробрасывает textChanged от него.
        with QSignalBlocker(self.api_url.inner_input):
            self.api_url.setText(s.get("api_url", "http://local.adspower.com:50395"))
        with QSignalBlocker(self.retry_cnt.inner_input):
            self.retry_cnt.setText(s.get("wallet_retry_count", "3"))
        with QSignalBlocker(self.d_start.inner_input):
            self.d_start.setText(s.get("delay_start", "5"))
        with QSignalBlocker(self.d_stop.inner_input):
            self.d_stop.setText(s.get("delay_stop", "1"))
        with QSignalBlocker(self.sel_pool.inner_input):
            self.sel_pool.setText(s.get("selenium_pool", "3"))

        # Загрузка визуальных параметров (PCB Engine & Smooth Scroll)
        with QSignalBlocker(self.inp_bg_base.inner_input):
            self.inp_bg_base.setText(ui_s.get("bg_base_color", Colors.BG_SAPPHIRE_BASE))
        with QSignalBlocker(self.inp_bg_pcb.inner_input):
            self.inp_bg_pcb.setText(ui_s.get("bg_pcb_color", Colors.PCB_NEON_BLUE))
        with QSignalBlocker(self.inp_pcb_opacity.inner_input):
            self.inp_pcb_opacity.setText(ui_s.get("bg_pcb_opacity", "85"))
        with QSignalBlocker(self.inp_pcb_thickness.inner_input):
            self.inp_pcb_thickness.setText(ui_s.get("bg_pcb_thickness", "2"))
        with QSignalBlocker(self.inp_pcb_seed.inner_input):
            self.inp_pcb_seed.setText(ui_s.get("bg_pcb_seed", "42"))
        with QSignalBlocker(self.inp_pcb_complexity.inner_input):
            self.inp_pcb_complexity.setText(ui_s.get("bg_pcb_complexity", "5"))
        with QSignalBlocker(self.inp_scroll_duration.inner_input):
            self.inp_scroll_duration.setText(ui_s.get("smooth_scroll_duration", "200"))
        with QSignalBlocker(self.inp_scroll_step.inner_input):
            self.inp_scroll_step.setText(ui_s.get("smooth_scroll_step", "120"))

        # Динамическая загрузка состояний плагинов
        for p_id, data in self._dynamic_inputs.items():
            chk = data["chk"]
            inp = data["inp"]
            pwd_key = data["pwd_key"]

            with QSignalBlocker(chk):
                chk.setChecked(s.get(f"unlock_{p_id}_enabled", "1") == "1")

            with QSignalBlocker(inp.inner_input):
                inp.setText(s.get(pwd_key, ""))

        self._update_master_checkbox_state()
        self._recalculate_hot_automation_state()

    @log_action("Удаление настроек ADS", category="SETTINGS")
    def delete_settings(self) -> None:
        """Удаление ветки реестра и очистка сейфа паролей."""
        if QMessageBox.question(
            self,
            "Удаление",
            "Удалить настройки?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        ) == QMessageBox.StandardButton.Yes:
            ok, msg = delete_settings_from_registry()
            if ok:
                logger.success("Настройки удалены", profile_names=["GLOBAL"], category="SETTINGS")
                self.load_settings()
            else:
                logger.error(msg, profile_names=["GLOBAL"], category="SETTINGS")

    def open_reg(self) -> None:
        """Открытие системного редактора реестра (regedit)."""
        open_registry_in_regedit()

    def get_api_url(self) -> str:
        """Возвращает текущий URL API для внешних вызовов (например, теста соединения)."""
        return self.api_url.text().strip()