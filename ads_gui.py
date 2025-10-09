# =========================
# 📝 Файл: ads_gui.py
# =========================
# Кодировка: UTF-8

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QAbstractItemView, QHeaderView, QSizePolicy, QSpacerItem,
    QProgressBar, QLineEdit, QGridLayout, QMessageBox, QTreeView,
    QStyledItemDelegate, QStyleOptionViewItem, QStyle
)
from PySide6.QtCore import Qt, QTimer, Signal, QSize, QModelIndex, QRect
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor, QStandardItemModel, QStandardItem, QFont
from typing import Any, Dict, List, Optional, Tuple
import time
import sys

from ads_logic import (
    open_profile, close_profile, get_profile_status, parse_profile_status, restart_profile,
    load_settings_from_registry, save_settings_to_registry, delete_settings_from_registry, open_registry_in_regedit,
    estimate_steps_for_open, estimate_steps_for_close, estimate_steps_for_status, estimate_steps_for_restart,
    build_group_index
)
from core import ping_watchdog
from logger import logger


# ====================== Работа с Windows Credential Manager ======================
# Храним пароли кошельков в Credential Manager (Generic). Поддерживаем две схемы имён:
#   1) Главная:  ADSProfileManager_<wallet_key>   (например ADSProfileManager_rabby_pass)
#   2) Альт.:    ADSProfileManager/<PrettyName>   (например ADSProfileManager/Rabby)

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    class CREDENTIAL_ATTRIBUTEW(ctypes.Structure):
        _fields_ = [
            ("Keyword", wintypes.LPWSTR),
            ("Flags", wintypes.DWORD),
            ("ValueSize", wintypes.DWORD),
            ("Value", ctypes.c_void_p),
        ]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.c_void_p),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.POINTER(CREDENTIAL_ATTRIBUTEW)),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    _advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    _CredReadW = _advapi32.CredReadW
    _CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    _CredReadW.restype = wintypes.BOOL

    _CredWriteW = _advapi32.CredWriteW
    _CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    _CredWriteW.restype = wintypes.BOOL

    _CredFree = _advapi32.CredFree
    _CredFree.argtypes = [ctypes.c_void_p]
    _CredFree.restype = None

    _CredDeleteW = _advapi32.CredDeleteW
    _CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    _CredDeleteW.restype = wintypes.BOOL

    def _cred_error() -> str:
        code = ctypes.get_last_error()
        return f"WinError {code}"

    def cred_write_generic(target: str, secret: str) -> bool:
        data = secret.encode("utf-16-le")
        cred = CREDENTIALW()
        cred.Flags = 0
        cred.Type = CRED_TYPE_GENERIC
        cred.TargetName = ctypes.c_wchar_p(target)
        cred.Comment = None
        cred.CredentialBlobSize = len(data)
        blob = ctypes.create_string_buffer(data)
        cred.CredentialBlob = ctypes.cast(blob, ctypes.c_void_p)
        cred.Persist = CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = ctypes.c_wchar_p("")
        ok = _CredWriteW(ctypes.byref(cred), 0)
        if not ok:
            logger.error(
                f"Не удалось записать пароль в Credential Manager: {target} ({_cred_error()})",
                profile_names=["GLOBAL"], category="SETTINGS"
            )
        return bool(ok)

    def cred_read_generic(target: str) -> Optional[str]:
        pcred = ctypes.POINTER(CREDENTIALW)()
        ok = _CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
        if not ok:
            return None
        try:
            size = pcred.contents.CredentialBlobSize
            ptr = pcred.contents.CredentialBlob
            buf = ctypes.string_at(ptr, size)
            return buf.decode("utf-16-le")
        finally:
            _CredFree(pcred)

    def cred_delete_generic(target: str) -> bool:
        ok = _CredDeleteW(target, CRED_TYPE_GENERIC, 0)
        return bool(ok)
else:
    # Заглушки для non-Windows
    def cred_write_generic(target: str, secret: str) -> bool:
        return True

    def cred_read_generic(target: str) -> Optional[str]:
        return None

    def cred_delete_generic(target: str) -> bool:
        return True

ALT_CRED_PREFIX = "ADSProfileManager/"
WALLET_TARGETS = {
    "rabby": ALT_CRED_PREFIX + "Rabby",
    "okx": ALT_CRED_PREFIX + "OKX",
    "keplr": ALT_CRED_PREFIX + "Keplr",
    "backpack": ALT_CRED_PREFIX + "Backpack",
    "phantom": ALT_CRED_PREFIX + "Phantom",
}
MAIN_CRED_PREFIX = "ADSProfileManager_"
MAIN_WALLET_TARGETS = {
    "rabby": MAIN_CRED_PREFIX + "rabby_pass",
    "okx": MAIN_CRED_PREFIX + "okx_pass",
    "keplr": MAIN_CRED_PREFIX + "keplr_pass",
    "backpack": MAIN_CRED_PREFIX + "backpack_pass",
    "phantom": MAIN_CRED_PREFIX + "phantom_pass",
}

def _read_with_fallback(wallet_key: str) -> str:
    main_target = MAIN_WALLET_TARGETS.get(wallet_key)
    alt_target = WALLET_TARGETS.get(wallet_key)
    if main_target:
        val = cred_read_generic(main_target) or ""
        if val:
            return val
    if alt_target:
        val = cred_read_generic(alt_target) or ""
        if val:
            return val
    return ""

def _write_both_targets(wallet_key: str, secret: str) -> bool:
    ok_any = True
    wrote = False
    if secret:
        target = MAIN_WALLET_TARGETS.get(wallet_key)
        if target:
            ok = cred_write_generic(target, secret)
            wrote = wrote or ok
            ok_any = ok_any and ok
        alt = WALLET_TARGETS.get(wallet_key)
        if alt:
            ok = cred_write_generic(alt, secret)
            wrote = wrote or ok
            ok_any = ok_any and ok
    return wrote or ok_any

def _delete_both_targets(wallet_key: str) -> None:
    target = MAIN_WALLET_TARGETS.get(wallet_key)
    if target:
        cred_delete_generic(target)
    alt = WALLET_TARGETS.get(wallet_key)
    if alt:
        cred_delete_generic(alt)


# ========= Флаги стран (рисуем на лету, чтобы не тащить файлы) =========
_FLAG_ICON_CACHE: Dict[str, Optional[QIcon]] = {}

def _draw_tricolor(stripes: List[QColor], w: int, h: int, vertical: bool = False) -> QPixmap:
    pm = QPixmap(w, h)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    try:
        if vertical:
            sw = w // len(stripes)
            x = 0
            for col in stripes:
                p.fillRect(x, 0, sw, h, col)
                x += sw
            if x < w:
                p.fillRect(x, 0, w - x, h, stripes[-1])
        else:
            sh = h // len(stripes)
            y = 0
            for col in stripes:
                p.fillRect(0, y, w, sh, col)
                y += sh
            if y < h:
                p.fillRect(0, y, w, h - y, stripes[-1])
    finally:
        p.end()
    return pm

def _fixed_icon_from_pixmap(pm: QPixmap) -> QIcon:
    icon = QIcon()
    for mode in (QIcon.Normal, QIcon.Disabled, QIcon.Active, QIcon.Selected):
        icon.addPixmap(pm, mode, QIcon.Off)
        icon.addPixmap(pm, mode, QIcon.On)
    return icon

def _flag_pixmap_for_code(code: str, w: int = 18, h: int = 12) -> Optional[QPixmap]:
    c = (code or "").upper()
    if not c or len(c) != 2:
        return None

    if c == "RU":
        return _draw_tricolor([QColor("#FFFFFF"), QColor("#0039A6"), QColor("#D52B1E")], w, h)
    if c == "NL":
        return _draw_tricolor([QColor("#AE1C28"), QColor("#FFFFFF"), QColor("#21468B")], w, h)
    if c == "UA":
        return _draw_tricolor([QColor("#0057B7"), QColor("#FFD700")], w, h)
    if c == "DE":
        return _draw_tricolor([QColor("#000000"), QColor("#DD0000"), QColor("#FFCE00")], w, h)
    if c == "AT":
        return _draw_tricolor([QColor("#ED2939"), QColor("#FFFFFF"), QColor("#ED2939")], w, h)
    if c == "EE":
        return _draw_tricolor([QColor("#0072CE"), QColor("#000000"), QColor("#FFFFFF")], w, h)
    if c == "LV":
        return _draw_tricolor([QColor("#9E1B34"), QColor("#FFFFFF"), QColor("#9E1B34")], w, h)
    if c == "LT":
        return _draw_tricolor([QColor("#FDB913"), QColor("#006A44"), QColor("#C1272D")], w, h)
    if c == "FR":
        return _draw_tricolor([QColor("#0055A4"), QColor("#FFFFFF"), QColor("#EF4135")], w, h, vertical=True)
    if c == "IT":
        return _draw_tricolor([QColor("#009246"), QColor("#FFFFFF"), QColor("#CE2B37")], w, h, vertical=True)
    if c == "IE":
        return _draw_tricolor([QColor("#169B62"), QColor("#FFFFFF"), QColor("#FF883E")], w, h, vertical=True)
    if c == "RO":
        return _draw_tricolor([QColor("#002B7F"), QColor("#FCD116"), QColor("#CE1126")], w, h, vertical=True)
    if c == "BG":
        return _draw_tricolor([QColor("#FFFFFF"), QColor("#00966E"), QColor("#D62612")], w, h)
    if c == "PL":
        return _draw_tricolor([QColor("#FFFFFF"), QColor("#DC143C")], w, h)
    if c == "CZ":
        return _draw_tricolor([QColor("#FFFFFF"), QColor("#D7141A")], w, h)
    if c == "ES":
        return _draw_tricolor([QColor("#AA151B"), QColor("#F1BF00"), QColor("#AA151B")], w, h)
    if c == "PT":
        return _draw_tricolor([QColor("#006600"), QColor("#FF0000")], w, h, vertical=True)
    if c == "GB":
        pm = QPixmap(w, h); pm.fill(QColor("#012169")); return pm
    if c == "US":
        return _draw_tricolor([QColor("#B22234"), QColor("#FFFFFF"), QColor("#B22234"), QColor("#FFFFFF"),
                               QColor("#B22234"), QColor("#FFFFFF")], w, h)
    if c == "TR":
        pm = QPixmap(w, h); pm.fill(QColor("#E30A17")); return pm
    if c == "CN":
        pm = QPixmap(w, h); pm.fill(QColor("#DE2910")); return pm
    if c == "JP":
        pm = QPixmap(w, h); pm.fill(QColor("#FFFFFF"))
        p = QPainter(pm); p.setBrush(QColor("#BC002D")); p.setPen(Qt.NoPen)
        d = min(w, h) * 0.6
        p.drawEllipse(int(w/2 - d/2), int(h/2 - d/2), int(d), int(d)); p.end()
        return pm

    return None

def _flag_icon_cached(code: str, w: int = 18, h: int = 12) -> Optional[QIcon]:
    c = (code or "").upper()
    if not c or len(c) != 2:
        return None
    cache_key = f"{c}:{w}x{h}"
    if cache_key in _FLAG_ICON_CACHE:
        return _FLAG_ICON_CACHE[cache_key]
    pm = _flag_pixmap_for_code(c, w, h)
    if pm is None:
        _FLAG_ICON_CACHE[cache_key] = None
        return None
    icon = _fixed_icon_from_pixmap(pm)
    _FLAG_ICON_CACHE[cache_key] = icon
    return icon

def _country_item_values(code: str) -> Tuple[Optional[QIcon], str]:
    c = (code or "").upper()
    if not c:
        return None, "🌐 N/A"
    icon = _flag_icon_cached(c)
    text = f"{c}"
    return icon, text


# ======== Роли модели ========
ROLE_IS_GROUP    = Qt.UserRole + 1
ROLE_USER_ID     = Qt.UserRole + 2
ROLE_NAME        = Qt.UserRole + 3
ROLE_GROUP_NAME  = Qt.UserRole + 4
ROLE_FLAT_ROW    = Qt.UserRole + 5


# ====================== Делегат: паддинги и «чистый» контент ======================
class PaddedItemDelegate(QStyledItemDelegate):
    """
    Делегат отвечает ТОЛЬКО за паддинги и «чистую» отрисовку контента.
    Любые штатные фоны (selected/hover/alternate) глушим, чтобы их
    рисовал только view.drawRow() на всю ширину строки.
    """
    def __init__(self, pad=5, parent=None):
        super().__init__(parent)
        self.pad = int(pad)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        # важное: глушим фон выделения/hover, чтобы он не перекрыл нашу плашку строки
        opt.state &= ~QStyle.State_Selected
        opt.state &= ~QStyle.State_MouseOver
        try:
            # на всякий случай убираем «шахматку» на выбранной/hover строке
            opt.features &= ~QStyleOptionViewItem.Alternate
        except Exception:
            pass

        # паддинги
        opt.rect = opt.rect.adjusted(self.pad, self.pad, -self.pad, -self.pad)
        super().paint(painter, opt, index)

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        return QSize(sz.width() + self.pad * 2, sz.height() + self.pad * 2)


# ====================== View: фон ряда в drawRow ======================
class ProfilesTreeView(QTreeView):
    """
    Рисует фон выбранной/hover строки ОДИН раз на всю ширину viewport.
    Порядок важен: сначала базовая отрисовка Qt, затем наша полупрозрачная
    плашка поверх — так текст и иконки остаются читаемыми.
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_index = QModelIndex()
        self._sel_color = QColor("#4D5B6B")    # цвет выделения
        self._hover_color = QColor("#3B4652")  # цвет hover
        self.setMouseTracking(True)

    # трекинг hover по целой строке
    def mouseMoveEvent(self, e):
        idx = self.indexAt(e.pos())
        if idx != self._hover_index:
            old = self._hover_index
            self._hover_index = idx
            if old.isValid():
                r = self.visualRect(old)
                self.viewport().update(0, r.top(), self.viewport().width(), r.height())
            if idx.isValid():
                r = self.visualRect(idx)
                self.viewport().update(0, r.top(), self.viewport().width(), r.height())
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        if self._hover_index.isValid():
            r = self.visualRect(self._hover_index)
            self._hover_index = QModelIndex()
            self.viewport().update(0, r.top(), self.viewport().width(), r.height())
        super().leaveEvent(e)

    def drawRow(self, painter, option, index):
        # 1) даём Qt нарисовать ветки, ячейки, текст и т.д.
        super().drawRow(painter, option, index)

        # 2) считаем выделение и hover для ВСЕЙ строки
        is_group = bool(index.siblingAtColumn(0).data(ROLE_IS_GROUP))
        if is_group:
            return

        sm = self.selectionModel()
        is_selected = False
        if sm is not None:
            # проверяем по стабильной «опорной» колонке (Имя = 3)
            ref = index.siblingAtColumn(3)
            if ref.isValid():
                is_selected = sm.isSelected(ref)

        hi = self._hover_index
        is_hovered = hi.isValid() and hi.parent() == index.parent() and hi.row() == index.row()

        if not (is_selected or is_hovered):
            return

        # 3) накрываем всю строку полупрозрачным прямоугольником поверх
        full = QRect(option.rect)
        full.setLeft(0)
        full.setRight(self.viewport().width() - 1)

        painter.save()
        if is_selected:
            c = QColor(self._sel_color); c.setAlpha(170)
        else:
            c = QColor(self._hover_color); c.setAlpha(110)
        painter.fillRect(full, c)
        painter.restore()



# ====================== Панель профилей (ADS) ======================
class AdsProfilePanel(QWidget):
    updateStatusSignal = Signal(int, str)
    setButtonsEnabledSignal = Signal(bool)
    progressSignal = Signal(int)
    stageSignal = Signal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__()
        self.setObjectName("AdsProfilePanel")
        self.parent: QWidget = parent
        self.updateStatusSignal.connect(self.update_profile_status)
        self.setButtonsEnabledSignal.connect(self.set_buttons_enabled)
        self.progressSignal.connect(self.set_progress)
        self.stageSignal.connect(self.set_progress_stage)

        self._profile_items: List[Dict[str, Any]] = []   # плоский индекс «строка профиля» -> ссылки на ячейки
        self._group_items: Dict[str, QStandardItem] = {}  # имя группы -> item первой колонки группы

        layout: QVBoxLayout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        # ====== Кнопки массовых операций ======
        btn_layout: QHBoxLayout = QHBoxLayout()
        self.launch_btn: QPushButton = QPushButton("▶️ Запустить")
        self.launch_btn.setObjectName("btn_launch")
        self.launch_btn.setToolTip("Запустить выбранные профили")

        self.restart_btn: QPushButton = QPushButton("♻️ Перезапустить")
        self.restart_btn.setObjectName("btn_restart")
        self.restart_btn.setToolTip("Перезапустить выбранные профили")

        self.close_btn: QPushButton = QPushButton("❌ Закрыть")
        self.close_btn.setObjectName("btn_close")
        self.close_btn.setToolTip("Закрыть выбранные профили")

        self.status_btn: QPushButton = QPushButton("📊 Проверить статусы")
        self.status_btn.setObjectName("btn_status")
        self.status_btn.setToolTip("Проверить статусы всех профилей в списке")

        for btn in [self.launch_btn, self.restart_btn, self.close_btn, self.status_btn]:
            btn.setProperty("class", "mass-action")
            btn.setMinimumHeight(26)
            btn.setMaximumHeight(36)
            btn.setStyleSheet("""
                QPushButton[class="mass-action"] {
                    background: #232629;
                    color: #F0F0F0;
                    border: 0.5px groove #5A5A5A;
                    border-radius: 8px;
                    padding: 3px 12px;
                    font-size: 15px;
                    min-width: 110px;
                    min-height: 26px;
                    max-height: 36px;
                }
                QPushButton[class="mass-action"]:hover:!disabled {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFE066, stop:1 #FFB800);
                    color: #232629;
                    border: 1px solid #FFE066;
                }
                QPushButton[class="mass-action"]:pressed {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFC300, stop:1 #FFD700);
                }
                QPushButton[class="mass-action"]:disabled {
                    background: #35393C;
                    color: #767676;
                    border: 1px solid #434343;
                }
            """)
        btn_layout.addWidget(self.launch_btn)
        btn_layout.addWidget(self.restart_btn)
        btn_layout.addWidget(self.close_btn)
        btn_layout.addWidget(self.status_btn)
        btn_layout.addStretch(1)
        layout.addLayout(btn_layout)

        title_label = QLabel("Список Профилей AdsPower")
        title_label.setObjectName("profiles_title")
        title_label.setStyleSheet("color: #F0F0F0; font-weight: bold; font-size: 16px;")
        layout.addWidget(title_label)

        instruction_label = QLabel(
            "ℹ️ <span style='color:#A0A0A0;'>Выделяйте профили простым кликом, "
            "или зажимайте Ctrl/Shift для мультивыделения, либо проведите мышью по нескольким строкам, удерживая ЛКМ.</span>"
        )
        instruction_label.setObjectName("profiles_hint")
        instruction_label.setTextFormat(Qt.TextFormat.RichText)
        instruction_label.setStyleSheet("font-size: 12px; margin-bottom: 2px; color: #F0F0F0;")
        layout.addWidget(instruction_label)

        # ====== ДЕРЕВО ПРОФИЛЕЙ С ГРУППАМИ ======
        self.table = ProfilesTreeView(self)
        self.table.setObjectName("profiles_tree")
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setRootIsDecorated(True)
        self.table.setItemsExpandable(True)
        self.table.setExpandsOnDoubleClick(True)
        self.table.setUniformRowHeights(True)
        self.table.setIndentation(14)
        self.table.setIconSize(QSize(18, 12))

        # В стилях убираем пер-ячеечные заливки selection/hover: фон ряда рисуем сами
        self.table.setStyleSheet("""
            QTreeView {
                background: #282B2E;
                color: #F0F0F0;
                font-size: 12px;
                alternate-background-color: #232629;
                border-radius: 10px;
                border: 1px solid #393B3E;
                show-decoration-selected: 1;
                selection-background-color: rgba(0,0,0,0);
                selection-color: #F0F0F0;
                outline: 0;
            }
            /* важное: сами ячейки — прозрачные в selected/hover */
            QTreeView::item { background-color: transparent; }
            QTreeView::item:selected,
            QTreeView::item:hover { background-color: transparent; }
            QTreeView::branch:selected,
            QTreeView::branch:hover { background: transparent; }

            QHeaderView::section {
                border: none;
                background: #393B3E;
                color: #FFFFFF;
                font-weight: bold;
                border-right: 1px solid #232629;
                padding: 3px 2px;
                font-size: 10px;
                min-height: 14px; max-height: 16px; height: 14px;
            }
        """)
        
        # Модель и заголовки
        self.model = QStandardItemModel(0, 7, self)
        self.model.setHorizontalHeaderLabels(["", "№", "ID", "Имя", "Страна", "Прокси", "Статус"])
        self.table.setModel(self.model)

        # Делегат с паддингами и гашением фоновой заливки на выбранных строках
        self.table.setItemDelegate(PaddedItemDelegate(5, self.table))

        # Раскладка ширин и логика двойного клика — как было
        self._apply_legacy_header_layout()
        self.table.doubleClicked.connect(self._on_tree_double_clicked)

        layout.addWidget(self.table, stretch=1)

        # ====== Нижняя строка: выделенные/прогресс ======
        self.selected_profiles_label = QLabel()
        self.selected_profiles_label.setObjectName("selected_label")
        self.selected_profiles_label.setStyleSheet(
            "color: #A0A0A0; font-size: 12px; margin-top: 2px; margin-bottom: 2px;"
        )
        self.selected_profiles_label.setText("Выделено профилей: 0")
        layout.addWidget(self.selected_profiles_label)

        status_block = QVBoxLayout()
        status_block.setContentsMargins(0, 0, 0, 0)
        status_block.setSpacing(3)

        top_status_line = QHBoxLayout()
        top_status_line.setContentsMargins(0, 0, 0, 0)
        top_status_line.setSpacing(6)

        self.progress_stage_label = QLabel("")
        self.progress_stage_label.setObjectName("progress_stage")
        self.progress_stage_label.setStyleSheet("font-size: 13px; color: #39a1ff; min-height: 19px;")
        self.progress_stage_label.setText("")
        top_status_line.addWidget(self.progress_stage_label, stretch=1)

        spacer = QSpacerItem(10, 10, QSizePolicy.Expanding, QSizePolicy.Minimum)
        top_status_line.addItem(spacer)

        self.success_label = QLabel("Успешно: 0")
        self.success_label.setObjectName("success_label")
        self.success_label.setStyleSheet("font-size: 12px; color: #40DB78; min-width: 78px; margin-left: 10px;")
        top_status_line.addWidget(self.success_label, alignment=Qt.AlignRight)

        self.error_label = QLabel("Ошибок: 0")
        self.error_label.setObjectName("error_label")
        self.error_label.setStyleSheet("font-size: 12px; color: #FF4F4F; min-width: 70px; margin-left: 16px;")
        top_status_line.addWidget(self.error_label, alignment=Qt.AlignRight)

        status_block.addLayout(top_status_line)

        # Прогресс-бар
        progress_line = QHBoxLayout()
        progress_line.setContentsMargins(0, 0, 0, 0)
        progress_line.setSpacing(4)

        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("progress_bar")
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)
        self.progress_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.progress_bar.setStyleSheet("""
            QProgressBar { background-color: #2F3438; border-radius: 4px; height: 8px; }
            QProgressBar::chunk { background-color: #FFE066; border-radius: 4px; }
        """)
        progress_line.addWidget(self.progress_bar, stretch=1)

        self.percent_label = QLabel("0%")
        self.percent_label.setObjectName("percent_label")
        self.percent_label.setStyleSheet("font-size: 12px; min-width: 38px; color: #BBBBBB; qproperty-alignment: AlignRight | AlignVCenter;")
        progress_line.addWidget(self.percent_label)
        status_block.addLayout(progress_line)
        layout.addLayout(status_block)

        # ====== Инфо + кнопка Стоп ======
        bottom_line = QHBoxLayout()
        bottom_line.setContentsMargins(0, 0, 0, 0)
        bottom_line.setSpacing(8)

        self.info_label = QLabel(
            "Выполняйте массовые операции только если уверены в параметрах! "
            "Все логи действий — справа, успехи и ошибки отмечаются цветом."
        )
        self.info_label.setObjectName("info_label")
        self.info_label.setStyleSheet(
            "color: #888888; font-size: 10px; margin-top: 3px; margin-bottom: 0px; padding: 0;"
        )
        self.info_label.setContentsMargins(0, 4, 0, 2)
        bottom_line.addWidget(self.info_label, stretch=1)

        self.stop_btn: QPushButton = QPushButton("⏹ Стоп")
        self.stop_btn.setObjectName("btn_stop")
        self.stop_btn.setProperty("class", "stop-action")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setMinimumHeight(18)
        self.stop_btn.setMaximumHeight(24)
        self.stop_btn.setMinimumWidth(80)
        self.stop_btn.setMaximumWidth(80)
        self.stop_btn.setToolTip("Остановить текущую массовую операцию")
        self.stop_btn.setStyleSheet("""
            QPushButton[class="stop-action"] {
                background: #232629;
                color: #F0F0F0;
                border: 0.5px groove #5A5A5A;
                border-radius: 6px;
                padding: 2px 7px;
                font-size: 11px;
                min-height: 18px;
                max-height: 24px;
                min-width: 80px;
                max-width: 80px;
            }
            QPushButton[class="stop-action"]:hover:!disabled {
                background: #D9534F;
                border: 1px solid #D9534F;
                color: #232629;
                border: 1px solid #FF0000;
            }
            QPushButton[class="stop-action"]:pressed {
                background: #CC0000;
            }
            QPushButton[class="stop-action"]:disabled {
                background: #35393C;
                color: #767676;
                border: 1px solid #434343;
            }
        """)
        self.stop_btn.clicked.connect(self.handle_stop_clicked)
        bottom_line.addWidget(self.stop_btn, alignment=Qt.AlignRight)
        layout.addLayout(bottom_line)

        # === Сигналы/слоты кнопок ===
        self.table.selectionModel().selectionChanged.connect(self.update_selected_profiles_label)
        self.launch_btn.clicked.connect(self.handle_open_selected_profiles)
        self.restart_btn.clicked.connect(self.handle_restart_selected_profiles)
        self.close_btn.clicked.connect(self.handle_close_selected_profiles)
        self.status_btn.clicked.connect(self.handle_check_status_all)

        # --- счётчики операции ---
        self._op_total: int = 0
        self._op_success: int = 0
        self._op_errors: int = 0

        # --- внутренняя метка текущей операции и план микрошагов ---
        self._current_action_label: str = ""
        self._current_total: int = 0

        # Микро-прогресс
        self._micro_total_steps: int = 0
        self._micro_done_steps: int = 0

        # Флаг «Стоп»
        self._cancel_requested: bool = False
        self._mass_action_running: bool = False

        # === Плавное обновление строки статуса (троттлинг ~8 Гц) ===
        self._stage_min_interval_sec: float = 0.12
        self._stage_last_update: float = 0.0
        self._stage_pending_text: Optional[str] = None
        self._stage_timer: QTimer = QTimer(self)
        self._stage_timer.setSingleShot(True)
        self._stage_timer.timeout.connect(self._flush_pending_stage)

    # ---------- «Как раньше» раскладка ширин ----------
    def _apply_legacy_header_layout(self) -> None:
        header: QHeaderView = self.table.header()

        # Заголовки по центру
        header.setDefaultAlignment(Qt.AlignCenter)
        for col in range(self.model.columnCount()):
            self.model.setHeaderData(col, Qt.Horizontal, Qt.AlignCenter, Qt.TextAlignmentRole)

        # Не растягиваем последнюю секцию
        header.setStretchLastSection(False)

        # Колонка 0 скрыта (служебная для дерева)
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 0)
        self.table.setColumnHidden(0, True)

        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)  # №
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)  # ID
        header.setSectionResizeMode(3, QHeaderView.Stretch)           # Имя
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)  # Страна
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)  # Прокси
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)  # Статус

        self._status_min_width = 60
        self.table.setColumnWidth(6, self._status_min_width)

        try:
            if hasattr(self, "_status_resize_guard"):
                header.sectionResized.disconnect(self._status_resize_guard)
        except Exception:
            pass

        def _guard(logical: int, old: int, new: int) -> None:
            if logical == 6 and new < self._status_min_width:
                header.resizeSection(6, self._status_min_width)

        self._status_resize_guard = _guard
        header.sectionResized.connect(self._status_resize_guard)

        for col in (1, 2, 4, 5, 6):
            self.table.resizeColumnToContents(col)
        if self.table.columnWidth(6) < self._status_min_width:
            header.resizeSection(6, self._status_min_width)

    # ============ Вспомогательные ============
    def _set_stop_enabled(self, enabled: bool) -> None:
        self.stop_btn.setEnabled(enabled)

    def handle_stop_clicked(self) -> None:
        if not self._mass_action_running:
            return
        if self._cancel_requested:
            return
        self._cancel_requested = True
        self._set_stop_enabled(False)
        logger.warning(
            "Операция прервана пользователем",
            profile_names=["GLOBAL"],
            category="PROFILE"
        )

    # ======== Конструирование строк дерева ========
    def _make_group_items(self, group_name: str, count: int) -> List[QStandardItem]:
        cols = [QStandardItem("") for _ in range(7)]
        for it in cols:
            it.setEditable(False)
            it.setSelectable(False)
            it.setEnabled(True)
            it.setData(True, ROLE_IS_GROUP)
            it.setData(group_name, ROLE_GROUP_NAME)
        name_item = cols[3]
        name_item.setText(f"Группа: {group_name}  ({count})")
        name_item.setSelectable(False)
        f = QFont(); f.setBold(True)
        name_item.setFont(f)
        return cols

    def _make_profile_items(self, flat_idx: int, profile: Dict[str, str]) -> List[QStandardItem]:
        user_id = str(profile.get("user_id", "") or "")
        name    = str(profile.get("name", "") or "")
        ip      = str(profile.get("ip", "") or "")
        country = (profile.get("ip_country", "") or "").upper()
        c0 = QStandardItem("")
        c1 = QStandardItem(str(flat_idx + 1))
        c2 = QStandardItem(user_id)
        c3 = QStandardItem(name)
        c4 = QStandardItem("")
        c5 = QStandardItem(ip if ip else "No IP")
        c6 = QStandardItem("⏳ Ожидание")
        for it in (c1, c2, c3, c4, c5, c6):
            it.setEditable(False)
            it.setSelectable(True)
            it.setEnabled(True)
            it.setData(False, ROLE_IS_GROUP)
            it.setData(user_id, ROLE_USER_ID)
            it.setData(name, ROLE_NAME)
            it.setData(profile.get("group_name", ""), ROLE_GROUP_NAME)
            it.setData(flat_idx, ROLE_FLAT_ROW)
        c1.setTextAlignment(Qt.AlignCenter)
        c2.setTextAlignment(Qt.AlignCenter)
        c5.setTextAlignment(Qt.AlignCenter)
        c6.setTextAlignment(Qt.AlignCenter)

        icon, text = _country_item_values(country)
        c4.setText(f" {text}")
        c4.setTextAlignment(Qt.AlignCenter)
        if icon:
            c4.setIcon(icon)

        return [c0, c1, c2, c3, c4, c5, c6]

    def _rebuild_flat_index(self) -> None:
        items: List[Dict[str, Any]] = []
        root = self.model.invisibleRootItem()
        for g_row in range(root.rowCount()):
            g_first = root.child(g_row, 0)
            for p_row in range(g_first.rowCount()):
                row_items = [g_first.child(p_row, col) for col in range(self.model.columnCount())]
                flat_idx = int(row_items[1].data(ROLE_FLAT_ROW))
                items.append({"flat_idx": flat_idx, "items": row_items})
        items.sort(key=lambda d: d["flat_idx"])
        self._profile_items = items

    # === Методы для взаимодействия ===
    def update_profiles(self, profiles: List[Dict[str, str]]) -> None:
        """
        Ожидаемые поля каждого профиля:
          - user_id
          - name
          - ip
          - ip_country
          - group_name
        """
        self.model.removeRows(0, self.model.rowCount())
        self._profile_items.clear()
        self._group_items.clear()

        grouped: Dict[str, List[Dict[str, str]]] = build_group_index(profiles)

        flat = 0
        for gname in sorted(grouped.keys(), key=lambda s: s.lower()):
            profs = grouped[gname]
            g_items = self._make_group_items(gname, len(profs))
            self.model.appendRow(g_items)
            g_first = g_items[0]
            self._group_items[gname] = g_first

            for p in profs:
                p_items = self._make_profile_items(flat, p)
                g_first.appendRow(p_items)
                flat += 1

        self._rebuild_flat_index()

        # По умолчанию раскрываем все группы
        root_index = self.model.indexFromItem(self.model.invisibleRootItem())
        for row in range(self.model.rowCount()):
            idx = self.model.index(row, 0, root_index)
            self.table.setExpanded(idx, True)

        # Вернуть схему ширин
        self._apply_legacy_header_layout()

        # Очистка выделения и счётчиков
        self.table.clearSelection()
        self.update_selected_profiles_label()
        self.set_success_count(0)
        self.set_error_count(0)
        ping_watchdog()

    # Сворачивание/разворачивание группы двойным кликом
    def _on_tree_double_clicked(self, index: QModelIndex) -> None:
        try:
            is_group = bool(index.siblingAtColumn(0).data(ROLE_IS_GROUP))
            if not is_group:
                return
            parent_idx = self.model.index(index.row(), 0, index.parent())
            expanded = self.table.isExpanded(parent_idx)
            self.table.setExpanded(parent_idx, not expanded)
        except Exception:
            pass

    def _selected_profile_indexes(self) -> List[QModelIndex]:
        sel = self.table.selectionModel().selectedRows(3)  # колонка «Имя»
        result: List[QModelIndex] = []
        for idx in sel:
            is_group = bool(idx.siblingAtColumn(0).data(ROLE_IS_GROUP))
            if not is_group:
                result.append(idx)
        return result

    def update_selected_profiles_label(self) -> None:
        names = []
        for idx in self._selected_profile_indexes():
            name = idx.data(Qt.DisplayRole) or ""
            if name:
                names.append(str(name))
        count = len(names)
        if count > 0:
            names_str = ", ".join(names)
            self.selected_profiles_label.setText(f"Выделено профилей: {count} — Имена: {names_str}")
        else:
            self.selected_profiles_label.setText("Выделено профилей: 0")

    def set_success_count(self, count: int) -> None:
        self.success_label.setText(f"Успешно: {count}")

    def set_error_count(self, count: int) -> None:
        self.error_label.setText(f"Ошибок: {count}")

    def set_progress(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        self.progress_bar.setValue(percent)
        self.percent_label.setText(f"{percent}%")
        ping_watchdog()

    def set_progress_stage(self, text: str) -> None:
        now = time.monotonic()
        elapsed = now - self._stage_last_update
        if elapsed >= self._stage_min_interval_sec:
            self.progress_stage_label.setText(text)
            self._stage_last_update = now
            self._stage_pending_text = None
        else:
            self._stage_pending_text = text
            remaining = max(0.0, self._stage_min_interval_sec - elapsed)
            if not self._stage_timer.isActive():
                self._stage_timer.start(int(remaining * 1000))

    def _flush_pending_stage(self) -> None:
        if self._stage_pending_text is not None:
            self.progress_stage_label.setText(self._stage_pending_text)
            self._stage_pending_text = None
            self._stage_last_update = time.monotonic()

    # === Микропрогресс ===
    def _progress_cb(self, text: str):
        self._micro_done_steps += 1
        percent = int(100 * self._micro_done_steps / max(1, self._micro_total_steps))
        self.progressSignal.emit(percent)
        self.stageSignal.emit(text)
        ping_watchdog()

    # === Массовые операции ===
    def handle_open_selected_profiles(self) -> None:
        self._run_mass_action_open_close_restart(mode="open")

    def handle_close_selected_profiles(self) -> None:
        self._run_mass_action_open_close_restart(mode="close")

    def handle_restart_selected_profiles(self) -> None:
        self._run_mass_action_open_close_restart(mode="restart")

    def _run_mass_action_open_close_restart(self, mode: str) -> None:
        selected = self._selected_profile_indexes()
        action_word = {"open": "Запуск", "close": "Закрытие", "restart": "Перезапуск"}[mode]
        category = "PROFILE"
        if not selected:
            logger.warning(
                f"Не выбраны профили для {action_word.lower()}.",
                profile_names=[],
                category=category
            )
            return

        profile_names = [idx.data(Qt.DisplayRole) for idx in selected]

        settings = load_settings_from_registry()
        try:
            delay_start = float(settings.get("delay_start", "5"))
        except Exception:
            delay_start = 5.0
        try:
            delay_stop = float(settings.get("delay_stop", "1"))
        except Exception:
            delay_stop = 1.0

        self._op_total = len(selected)
        self._op_success = 0
        self._op_errors = 0
        self.set_success_count(0)
        self.set_error_count(0)
        self.setButtonsEnabledSignal.emit(False)
        self.progressSignal.emit(0)
        self._current_action_label = action_word
        self._current_total = self._op_total

        self._cancel_requested = False
        self._mass_action_running = True
        self._set_stop_enabled(True)

        self.set_progress_stage(f"{action_word} профилей...")

        logger.start(
            f"Старт массового {action_word.lower()} профилей...",
            profile_names=profile_names,
            category=category
        )

        # задачи: (flat_row_idx, user_id, name)
        tasks: List[Tuple[int, str, str]] = []
        for idx in selected:
            user_id = str(idx.siblingAtColumn(2).data(Qt.DisplayRole))
            name    = str(idx.data(Qt.DisplayRole))
            flat_row = int(idx.siblingAtColumn(1).data(ROLE_FLAT_ROW))
            tasks.append((flat_row, user_id, name))

        # оценка числа микрошагов
        if mode == "open":
            per_profile_steps = estimate_steps_for_open(settings)
        elif mode == "close":
            per_profile_steps = estimate_steps_for_close()
        elif mode == "restart":
            per_profile_steps = estimate_steps_for_restart(settings=settings)
        else:
            per_profile_steps = 1

        self._micro_total_steps = per_profile_steps * len(tasks)
        self._micro_done_steps = 0
        self.progressSignal.emit(0)

        done_count = [0]
        total = len(tasks)

        def mark_cancelled_rows(start_idx: int):
            for r in range(start_idx, len(tasks)):
                flat_row = tasks[r][0]
                self.updateStatusSignal.emit(flat_row, "⏹ Прерван")
                try:
                    self._get_item(flat_row, 6).setTextAlignment(Qt.AlignCenter)
                    self._get_item(flat_row, 6).setToolTip("Операция прервана пользователем")
                except Exception:
                    pass

        def worker(flat_row: int, user_id: str, name: str, idxn: int) -> None:
            if self._cancel_requested:
                self.updateStatusSignal.emit(flat_row, "⏹ Прерван")
                try:
                    self._get_item(flat_row, 6).setTextAlignment(Qt.AlignCenter)
                except Exception:
                    pass
                return

            profile_tag = name.strip()
            running_stage_text = f"{self._current_action_label} профиля: {profile_tag} (№{idxn + 1} из {total})..."
            self.stageSignal.emit(running_stage_text)
            logger.start(
                f"{self._current_action_label} профиля: {profile_tag}",
                profile_names=[profile_tag],
                category=category
            )
            self.updateStatusSignal.emit(flat_row, f"⏳ {self._current_action_label}...")
            try:
                if mode == "open":
                    success, status = open_profile(user_id, name, logger_func=None, progress_cb=self._progress_cb)
                elif mode == "close":
                    success, status = close_profile(user_id, name, logger_func=None, progress_cb=self._progress_cb)
                else:
                    success, status = restart_profile(user_id, name, logger_func=None, progress_cb=self._progress_cb)

                if mode == "restart":
                    display_status, log_level = parse_profile_status("Active" if success else status)
                else:
                    display_status, log_level = parse_profile_status(
                        "Active" if (success and mode == "open") else ("Closed" if (success and mode == "close") else status)
                    )

                if success:
                    self._op_success += 1
                    self.updateStatusSignal.emit(flat_row, display_status)
                    self._get_item(flat_row, 6).setTextAlignment(Qt.AlignCenter)
                    self._get_item(flat_row, 6).setToolTip("")
                    logger.success(
                        f"{profile_tag}: операция {self._current_action_label.lower()} успешно выполнена",
                        profile_names=[profile_tag],
                        category=category
                    )
                else:
                    self._op_errors += 1
                    self.updateStatusSignal.emit(flat_row, display_status)
                    self._get_item(flat_row, 6).setTextAlignment(Qt.AlignCenter)
                    self._get_item(flat_row, 6).setToolTip(str(status))
                    logger.error(
                        f"{profile_tag}: ошибка при {self._current_action_label.lower()}. {status}",
                        profile_names=[profile_tag],
                        category=category
                    )
            except Exception as ex:
                self._op_errors += 1
                display_status, log_level = parse_profile_status(f"Error: {ex}")
                self.updateStatusSignal.emit(flat_row, display_status)
                try:
                    self._get_item(flat_row, 6).setTextAlignment(Qt.AlignCenter)
                    self._get_item(flat_row, 6).setToolTip(str(ex))
                except Exception:
                    pass
                logger.error(
                    f"{profile_tag}: неожиданная ошибка: {ex}",
                    profile_names=[profile_tag],
                    category=category,
                    extra={"trace": str(ex)}
                )
            finally:
                done_count[0] += 1
                self.set_success_count(self._op_success)
                self.set_error_count(self._op_errors)
                ping_watchdog()

        def on_all_done():
            self.setButtonsEnabledSignal.emit(True)
            self._mass_action_running = False
            self._set_stop_enabled(False)
            logger.warning(
                f"Завершено массовое {self._current_action_label.lower()} {self._op_total} профилей. Успешно: {self._op_success}, ошибок: {self._op_errors}",
                profile_names=profile_names,
                category=category
            )
            self._micro_done_steps = self._micro_total_steps
            self.progressSignal.emit(100)
            self.stageSignal.emit(f"Готово: {self._micro_total_steps}/{self._micro_total_steps} действий.")
            QTimer.singleShot(1000, lambda: self.progressSignal.emit(0))
            QTimer.singleShot(1000, lambda: self.stageSignal.emit(""))
            ping_watchdog()

        import threading, time as _t
        def thread_run() -> None:
            threads = []
            for idxn, (flat_row, user_id, name) in enumerate(tasks):
                if self._cancel_requested:
                    mark_cancelled_rows(idxn)
                    break
                t = threading.Thread(target=worker, args=(flat_row, user_id, name, idxn), daemon=True)
                threads.append(t)
                t.start()
                if idxn < len(tasks) - 1:
                    if self._cancel_requested:
                        mark_cancelled_rows(idxn + 1)
                        break
                    if mode == "restart":
                        _t.sleep(delay_stop)
                    else:
                        _t.sleep(delay_start if mode == "open" else delay_stop)
            for t in threads:
                t.join()
            on_all_done()

        import threading
        threading.Thread(target=thread_run, daemon=True).start()

    def handle_check_status_all(self) -> None:
        self._current_action_label = "Проверка статуса"
        self.setButtonsEnabledSignal.emit(False)

        self._cancel_requested = False
        self._mass_action_running = True
        self._set_stop_enabled(True)

        logger.info(
            "Старт проверки статусов всех профилей...",
            profile_names=self._get_all_profile_names(),
            category="PROFILE"
        )
        n: int = len(self._profile_items)
        self.progressSignal.emit(0)
        self._op_total = n
        self._op_success = 0
        self._op_errors = 0
        self.set_success_count(0)
        self.set_error_count(0)
        self.stageSignal.emit("Старт проверки статусов профилей...")

        per_prof_steps = estimate_steps_for_status()
        self._micro_total_steps = per_prof_steps * max(0, n)
        self._micro_done_steps = 0
        self.progressSignal.emit(0)

        DELAY_BETWEEN_STATUS = 0.8

        def worker(flat_row: int, user_id: str, name: str) -> None:
            if self._cancel_requested:
                self.updateStatusSignal.emit(flat_row, "⏹ Прерван")
                try:
                    self._get_item(flat_row, 6).setTextAlignment(Qt.AlignCenter)
                except Exception:
                    pass
                return

            profile_tag = name.strip()
            running_stage_text = f"{self._current_action_label}: {profile_tag} (№{flat_row + 1} из {n})..."
            self.stageSignal.emit(running_stage_text)
            try:
                status = get_profile_status(user_id, profile_name=name, progress_cb=self._progress_cb)
                display_status, log_level = parse_profile_status(status)
                self.updateStatusSignal.emit(flat_row, display_status)
                self._get_item(flat_row, 6).setTextAlignment(Qt.AlignCenter)
                self._get_item(flat_row, 6).setToolTip("" if log_level == "INFO" else str(status))
                if display_status in ["🟢 Активен", "⚫ Закрыт"] or display_status.startswith("🔴"):
                    logger.log(
                        f"{profile_tag}: статус профиля — {display_status}",
                        log_level,
                        profile_names=[profile_tag],
                        category="PROFILE"
                    )
                if log_level == "INFO":
                    self._op_success += 1
                else:
                    self._op_errors += 1
            except Exception as ex:
                display_status, log_level = parse_profile_status(f"Error: {ex}")
                self.updateStatusSignal.emit(flat_row, display_status)
                try:
                    self._get_item(flat_row, 6).setTextAlignment(Qt.AlignCenter)
                    self._get_item(flat_row, 6).setToolTip(str(ex))
                except Exception:
                    pass
                logger.error(
                    f"{profile_tag}: ошибка статуса (исключение): {ex}",
                    profile_names=[profile_tag],
                    category="PROFILE",
                    extra={"trace": str(ex)}
                )
                self._op_errors += 1
            finally:
                self.set_success_count(self._op_success)
                self.set_error_count(self._op_errors)
                ping_watchdog()

        def thread_run() -> None:
            if n == 0:
                self.setButtonsEnabledSignal.emit(True)
                self._mass_action_running = False
                self._set_stop_enabled(False)
                logger.warning("Нет профилей для проверки статуса.", profile_names=[], category="PROFILE")
                return
            import time as _t
            for flat_row, user_id, name in self._iter_all_profiles_triplets():
                if self._cancel_requested:
                    for fr, _, _ in self._iter_all_profiles_triplets(start_flat=flat_row):
                        try:
                            self.updateStatusSignal.emit(fr, "⏹ Прерван")
                            self._get_item(fr, 6).setTextAlignment(Qt.AlignCenter)
                        except Exception:
                            pass
                    break
                try:
                    worker(flat_row, user_id, name)
                except Exception as ex:
                    logger.error(
                        f"Ошибка чтения данных профиля в дереве: {ex}",
                        profile_names=[],
                        category="PROFILE",
                        extra={"trace": str(ex)}
                    )
                _t.sleep(DELAY_BETWEEN_STATUS)

            self._micro_done_steps = self._micro_total_steps
            self.progressSignal.emit(100)
            self.stageSignal.emit(f"Готово: {self._micro_total_steps}/{self._micro_total_steps} действий.")
            self.setButtonsEnabledSignal.emit(True)
            self._mass_action_running = False
            self._set_stop_enabled(False)
            QTimer.singleShot(1000, lambda: self.progressSignal.emit(0))
            QTimer.singleShot(1000, lambda: self.stageSignal.emit(""))
            ping_watchdog()

        import threading
        threading.Thread(target=thread_run, daemon=True).start()

    def _iter_all_profiles_triplets(self, start_flat: int = 0):
        for d in self._profile_items:
            if d["flat_idx"] < start_flat:
                continue
            items = d["items"]
            flat = d["flat_idx"]
            user_id = str(items[2].text())
            name    = str(items[3].text())
            yield flat, user_id, name

    def _get_item(self, flat_row: int, col: int) -> QStandardItem:
        return self._profile_items[flat_row]["items"][col]

    def _get_all_profile_names(self) -> List[str]:
        return [str(d["items"][3].text()) for d in self._profile_items]

    def set_buttons_enabled(self, enabled: bool) -> None:
        self.launch_btn.setEnabled(enabled)
        self.close_btn.setEnabled(enabled)
        self.restart_btn.setEnabled(enabled)
        self.status_btn.setEnabled(enabled)

    def update_profile_status(self, row_idx: int, status: str) -> None:
        if 0 <= row_idx < len(self._profile_items):
            item = self._get_item(row_idx, 6)
            item.setText(status)
            item.setTextAlignment(Qt.AlignCenter)

    def update_profile_status_threadsafe(self, row_idx: int, status: str) -> None:
        self.updateStatusSignal.emit(row_idx, status)


# ====================== Панель настроек (ADS) ======================
class AdsSettingsPanel(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__()
        self.setObjectName("AdsSettingsPanel")
        self.parent: QWidget = parent
        layout: QVBoxLayout = QVBoxLayout(self)
        layout.setContentsMargins(32, 32, 32, 32)
        layout.setSpacing(22)

        # --- API адрес ---
        api_label = QLabel('Adspower API address:')
        api_label.setObjectName("api_label")
        api_label.setStyleSheet("font-size:16px; font-weight:bold; color:#F0F0F0; margin-bottom:0px;")
        api_label.setAlignment(Qt.AlignLeft)
        self.api_url = QLineEdit()
        self.api_url.setObjectName("api_url")
        self.api_url.setMinimumHeight(26)
        self.api_url.setMaximumHeight(32)
        self.api_url.setMinimumWidth(280)
        self.api_url.setMaximumWidth(380)
        self.api_url.setProperty("class", "mass-action-input")
        self.api_url.setPlaceholderText("http://local.adspower.com:50395")
        self.api_url.setAlignment(Qt.AlignCenter)
        self.api_url.setStyleSheet("""
            QLineEdit[class="mass-action-input"] {
                min-width: 250px;
                max-width: 380px;
                background: #232629;
                color: #F0F0F0;
                border: 1px groove #888888;
                border-radius: 8px;
                font-size: 14px;
                padding: 5px 14px;
                min-height: 26px;
                max-height: 32px;
            }
            QLineEdit[class="mass-action-input"]:focus {
                border: 1px groove #FFE066;
                background: #24282c;
            }
        """)
        api_row = QVBoxLayout()
        api_row.addWidget(api_label)
        api_row.addWidget(self.api_url)
        layout.addLayout(api_row)

        # Хелпер для единообразных полей паролей
        def make_pwd_edit(placeholder: str, obj_name: str) -> QLineEdit:
            e = QLineEdit()
            e.setObjectName(obj_name)
            e.setEchoMode(QLineEdit.Password)
            e.setMinimumHeight(26)
            e.setMaximumHeight(32)
            e.setMinimumWidth(140)
            e.setMaximumWidth(170)
            e.setProperty("class", "mass-action-input")
            e.setPlaceholderText(placeholder)
            e.setAlignment(Qt.AlignCenter)
            e.setStyleSheet("""
                QLineEdit[class="mass-action-input"] {
                    min-width: 140px;
                    max-width: 170px;
                    background: #232629;
                    color: #F0F0F0;
                    border: 1px groove #888888;
                    border-radius: 8px;
                    font-size: 14px;
                    padding: 5px 10px;
                    min-height: 26px;
                    max-height: 32px;
                }
                QLineEdit[class="mass-action-input"]:focus {
                    border: 1px groove #FFE066;
                    background: #24282c;
                }
            """)
            return e

        # --- Пароли кошельков ---
        wallets_grid = QGridLayout()
        wallets_grid.setHorizontalSpacing(38)
        wallets_grid.setVerticalSpacing(12)
        label_width = 100
        label_style = "font-size:14px; font-weight:bold; color:#F0F0F0;"

        rabby_label = QLabel('🦝 Rabby');     rabby_label.setObjectName("lbl_rabby")
        rabby_label.setFixedWidth(label_width);
        rabby_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter);
        rabby_label.setStyleSheet(label_style)
        self.rabby_pass = make_pwd_edit("Пароль Rabby", "pwd_rabby")

        backpack_label = QLabel('🎒 Backpack'); backpack_label.setObjectName("lbl_backpack")
        backpack_label.setFixedWidth(label_width);
        backpack_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter);
        backpack_label.setStyleSheet(label_style)
        self.backpack_pass = make_pwd_edit("Пароль Backpack", "pwd_backpack")

        okx_label = QLabel('🟦 OKX');         okx_label.setObjectName("lbl_okx")
        okx_label.setFixedWidth(label_width);
        okx_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter);
        okx_label.setStyleSheet(label_style)
        self.okx_pass = make_pwd_edit("Пароль OKX", "pwd_okx")

        phantom_label = QLabel('👻 Phantom');  phantom_label.setObjectName("lbl_phantom")
        phantom_label.setFixedWidth(label_width);
        phantom_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter);
        phantom_label.setStyleSheet(label_style)
        self.phantom_pass = make_pwd_edit("Пароль Phantom", "pwd_phantom")

        keplr_label = QLabel('🟣 Keplr');     keplr_label.setObjectName("lbl_keplr")
        keplr_label.setFixedWidth(label_width);
        keplr_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter);
        keplr_label.setStyleSheet(label_style)
        self.keplr_pass = make_pwd_edit("Пароль Keplr", "pwd_keplr")

        wallets_grid.addWidget(rabby_label, 0, 0);
        wallets_grid.addWidget(self.rabby_pass, 0, 1)
        wallets_grid.addWidget(backpack_label, 0, 2);
        wallets_grid.addWidget(self.backpack_pass, 0, 3)
        wallets_grid.addWidget(okx_label, 1, 0);
        wallets_grid.addWidget(self.okx_pass, 1, 1)
        wallets_grid.addWidget(phantom_label, 1, 2);
        wallets_grid.addWidget(self.phantom_pass, 1, 3)
        wallets_grid.addWidget(keplr_label, 2, 0);
        wallets_grid.addWidget(self.keplr_pass, 2, 1)
        layout.addLayout(wallets_grid)

        # --- Задержки и число попыток ---
        delay_vbox = QVBoxLayout()
        delay_vbox.setSpacing(10)

        retry_label = QLabel('🔁 Попыток разблокировки кошельков:')
        retry_label.setObjectName("lbl_retry")
        retry_label.setStyleSheet("font-size:14px; font-weight:bold; color:#F0F0F0;")
        retry_label.setFixedWidth(280)
        self.wallet_retry_count = QLineEdit()
        self.wallet_retry_count.setObjectName("inp_retry")
        self.wallet_retry_count.setMinimumHeight(26)
        self.wallet_retry_count.setMaximumHeight(32)
        self.wallet_retry_count.setProperty("class", "mini-input")
        self.wallet_retry_count.setAlignment(Qt.AlignCenter)
        self.wallet_retry_count.setStyleSheet("""
            QLineEdit[class="mini-input"] {
                min-width: 40px; max-width: 70px; background: #232629; color: #F0F0F0;
                border: 1px groove #888888; border-radius: 8px; font-size: 13px; padding: 4px 8px;
            }
            QLineEdit[class="mini-input"]:focus { border: 1px groove #FFE066; background: #24282c; }
        """)
        row_retry = QHBoxLayout();
        row_retry.addWidget(retry_label);
        row_retry.addWidget(self.wallet_retry_count);
        row_retry.addStretch(1)
        delay_vbox.addLayout(row_retry)

        delay_start_label = QLabel('⏳ Задержка между запусками (сек):')
        delay_start_label.setObjectName("lbl_delay_start")
        delay_start_label.setStyleSheet("font-size:14px; font-weight:bold; color:#F0F0F0;")
        delay_start_label.setFixedWidth(280)
        self.delay_start = QLineEdit();
        self.delay_start.setObjectName("inp_delay_start")
        self.delay_start.setMinimumHeight(26);
        self.delay_start.setMaximumHeight(32)
        self.delay_start.setProperty("class", "mini-input");
        self.delay_start.setAlignment(Qt.AlignCenter)
        self.delay_start.setStyleSheet("""
            QLineEdit[class="mini-input"] {
                min-width: 40px; max-width: 70px; background: #232629; color: #F0F0F0;
                border: 1px groove #888888; border-radius: 8px; font-size: 13px; padding: 4px 8px;
            }
            QLineEdit[class="mini-input"]:focus { border: 1px groove #FFE066; background: #24282c; }
        """)
        row2 = QHBoxLayout();
        row2.addWidget(delay_start_label);
        row2.addWidget(self.delay_start);
        row2.addStretch(1)
        delay_vbox.addLayout(row2)

        delay_stop_label = QLabel('⏱️ Задержка при закрытии (сек):')
        delay_stop_label.setObjectName("lbl_delay_stop")
        delay_stop_label.setStyleSheet("font-size:14px; font-weight:bold; color:#F0F0F0;")
        delay_stop_label.setFixedWidth(280)
        self.delay_stop = QLineEdit();
        self.delay_stop.setObjectName("inp_delay_stop")
        self.delay_stop.setMinimumHeight(26);
        self.delay_stop.setMaximumHeight(32)
        self.delay_stop.setProperty("class", "mini-input");
        self.delay_stop.setAlignment(Qt.AlignCenter)
        self.delay_stop.setStyleSheet("""
            QLineEdit[class="mini-input"] {
                min-width: 40px; max-width: 70px; background: #232629; color: #F0F0F0;
                border: 1px groove #888888; border-radius: 8px; font-size: 13px; padding: 4px 8px;
            }
            QLineEdit[class="mini-input"]:focus { border: 1px groove #FFE066; background: #24282c; }
        """)
        row3 = QHBoxLayout();
        row3.addWidget(delay_stop_label);
        row3.addWidget(self.delay_stop);
        row3.addStretch(1)
        delay_vbox.addLayout(row3)
        layout.addLayout(delay_vbox)
        layout.addSpacing(16)

        # --- Кнопки сохранить/удалить/реестр ---
        buttons_row = QHBoxLayout()
        buttons_row.setSpacing(16)
        buttons_row.setAlignment(Qt.AlignCenter)

        self.save_btn = QPushButton('💾  Сохранить настройки')
        self.save_btn.setObjectName("btn_save")
        self.save_btn.setMinimumHeight(32)
        self.save_btn.setMaximumHeight(36)
        self.save_btn.setProperty("class", "mass-action")
        self.save_btn.setToolTip("Сохранить настройки в реестре/Credential Manager")
        self.save_btn.setStyleSheet("""
            QPushButton[class="mass-action"] {
                background: #232629; color: #F0F0F0; border: 0.5px groove #5A5A5A; border-radius: 8px;
                padding: 3px 12px; font-size: 15px; min-width: 110px; min-height: 32px; max-height: 36px;
            }
            QPushButton[class="mass-action"]:hover:!disabled {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFE066, stop:1 #FFB800);
                color: #232629; border: 1px solid #FFE066;
            }
            QPushButton[class="mass-action"]:pressed {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #FFC300, stop:1 #FFD700);
            }
            QPushButton[class="mass-action"]:disabled { background: #35393C; color: #767676; border: 1px solid #434343; }
        """)

        self.delete_registry_btn = QPushButton('🗑️ Удалить из реестра')
        self.delete_registry_btn.setObjectName("btn_delete")
        self.delete_registry_btn.setMinimumHeight(32)
        self.delete_registry_btn.setMaximumHeight(36)
        self.delete_registry_btn.setProperty("class", "mass-action")
        self.delete_registry_btn.setToolTip("Удалить ветку настроек и пароли (Credential Manager)")
        self.delete_registry_btn.setStyleSheet(self.save_btn.styleSheet())

        self.open_registry_btn = QPushButton('📂 Посмотреть реестр')
        self.open_registry_btn.setObjectName("btn_regedit")
        self.open_registry_btn.setMinimumHeight(32)
        self.open_registry_btn.setMaximumHeight(36)
        self.open_registry_btn.setProperty("class", "mass-action")
        self.open_registry_btn.setToolTip("Открыть редактор реестра Windows (RegEdit)")
        self.open_registry_btn.setStyleSheet(self.save_btn.styleSheet())

        buttons_row.addStretch(1)
        buttons_row.addWidget(self.save_btn);
        buttons_row.addSpacing(6)
        buttons_row.addWidget(self.delete_registry_btn);
        buttons_row.addSpacing(6)
        buttons_row.addWidget(self.open_registry_btn)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        description = QLabel(
            "<span style='color:#BBBBBB; font-size:12px;'>"
            "Все настройки программы хранятся локально в реестре Windows по адресу <b>HKEY_CURRENT_USER\\Software\\ADSProfileManager</b>.<br>"
            "Пароли кошельков сохраняются безопасно в <b>Windows Credential Manager</b> как Generic-учётные данные."
            "</span>"
        )
        description.setObjectName("desc_label")
        description.setWordWrap(True)
        description.setAlignment(Qt.AlignCenter)
        description.setContentsMargins(12, 12, 12, 0)
        layout.addWidget(description)

        self.save_btn.clicked.connect(self.save_settings)
        self.delete_registry_btn.clicked.connect(self.confirm_and_delete_registry)
        self.open_registry_btn.clicked.connect(self.open_registry)

        layout.addStretch(1)
        self.load_settings_from_registry()

    def load_settings_from_registry(self) -> None:
        try:
            settings = load_settings_from_registry()
            self.api_url.setText(settings.get("api_url", "http://local.adspower.com:50395"))
            self.rabby_pass.setText(_read_with_fallback("rabby"))
            self.okx_pass.setText(_read_with_fallback("okx"))
            self.keplr_pass.setText(_read_with_fallback("keplr"))
            self.backpack_pass.setText(_read_with_fallback("backpack"))
            self.phantom_pass.setText(_read_with_fallback("phantom"))
            self.wallet_retry_count.setText(str(settings.get("wallet_retry_count", "3")))
            self.delay_start.setText(str(settings.get("delay_start", "5")))
            self.delay_stop.setText(str(settings.get("delay_stop", "1")))
            if sys.platform != "win32":
                logger.info("Работа вне Windows: используется дефолтные настройки.", profile_names=["GLOBAL"], category="SETTINGS")
            else:
                logger.info("Настройки успешно загружены из реестра.", profile_names=["GLOBAL"], category="SETTINGS")
        except Exception as ex:
            self.api_url.setText("http://local.adspower.com:50395")
            self.rabby_pass.setText(""); self.okx_pass.setText(""); self.keplr_pass.setText("")
            self.backpack_pass.setText(""); self.phantom_pass.setText("")
            self.wallet_retry_count.setText("3"); self.delay_start.setText("5"); self.delay_stop.setText("1")
            logger.warning(f"⚠️ Не удалось загрузить настройки из реестра: {ex}", profile_names=["GLOBAL"], category="SETTINGS", extra={"trace": str(ex)})

    def save_settings(self) -> None:
        settings: Dict[str, Any] = {
            "api_url": self.api_url.text().strip(),
            "wallet_retry_count": self.wallet_retry_count.text().strip(),
            "delay_start": self.delay_start.text().strip(),
            "delay_stop": self.delay_stop.text().strip(),
            "rabby_pass": self.rabby_pass.text(),
            "okx_pass": self.okx_pass.text(),
            "keplr_pass": self.keplr_pass.text(),
            "backpack_pass": self.backpack_pass.text(),
            "phantom_pass": self.phantom_pass.text(),
        }
        success, msg = save_settings_to_registry(settings)
        if success:
            logger.success(msg, profile_names=["GLOBAL"], category="SETTINGS", force=True)
        else:
            logger.error(msg, profile_names=["GLOBAL"], category="SETTINGS")

        def write_or_delete_both(wallet_key: str, value: str):
            if sys.platform != "win32":
                return True
            if value:
                return _write_both_targets(wallet_key, value)
            else:
                _delete_both_targets(wallet_key)
                return True

        ok_all = True
        ok_all &= write_or_delete_both("rabby", self.rabby_pass.text())
        ok_all &= write_or_delete_both("okx", self.okx_pass.text())
        ok_all &= write_or_delete_both("keplr", self.keplr_pass.text())
        ok_all &= write_or_delete_both("backpack", self.backpack_pass.text())
        ok_all &= write_or_delete_both("phantom", self.phantom_pass.text())
        if ok_all:
            logger.success("Пароли успешно сохранены в Credential Manager.", profile_names=["GLOBAL"], category="SETTINGS")
        else:
            logger.error("Часть паролей не удалось сохранить в Credential Manager.", profile_names=["GLOBAL"], category="SETTINGS")

    def confirm_and_delete_registry(self) -> None:
        reply = QMessageBox.question(
            self,
            "Удаление настроек",
            "Вы действительно хотите удалить все настройки программы из реестра Windows?\n\n"
            "Это действие невозможно отменить.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            success, msg = delete_settings_from_registry()
            if success:
                logger.success("Все настройки программы успешно удалены из реестра.", profile_names=["GLOBAL"], category="SETTINGS")
                self.reset_fields_to_default()
            else:
                logger.error(msg, profile_names=["GLOBAL"], category="SETTINGS")
                QMessageBox.critical(self, "Ошибка удаления", f"Не удалось удалить настройки из реестра:\n{msg}")

    def reset_fields_to_default(self):
        self.api_url.setText("http://local.adspower.com:50395")
        self.rabby_pass.setText(""); self.okx_pass.setText(""); self.keplr_pass.setText("")
        self.backpack_pass.setText(""); self.phantom_pass.setText("")
        self.wallet_retry_count.setText("3"); self.delay_start.setText("5"); self.delay_stop.setText("1")

    def open_registry(self):
        success, msg = open_registry_in_regedit()
        if not success:
            logger.error(msg, profile_names=["GLOBAL"], category="SETTINGS")
            QMessageBox.critical(self, "Ошибка", f"Не удалось открыть редактор реестра:\n{msg}")

    def get_api_url(self) -> str:
        return self.api_url.text().strip()
