"""
Модуль: core/_style_colors.py
Назначение: Изолированный цех цветовой палитры (Presentation Utilities).
Зона ответственности: Хранение глобальных цветовых констант (Single Source of Truth).
                      Обеспечивает эталонными HEX-кодами как QSS-стили, так и новый
                      Векторный малярный цех (In-Memory XML Patching) для иконок,
                      круговые индикаторы прогресса (CircularProgress),
                      движок левитации карточек (Frosted Glass Levitation Engine),
                      премиальные 3D-сферы (Crystal Cache Engine), новый
                      аппаратный генератор текстуры печатной платы (PCB Baking)
                      и 3D-глобусы флагов (Bake & Blit).
Интеграция: Является абсолютно автономным модулем нулевого уровня. Не импортирует
            НИЧЕГО (включая PySide6), что гарантирует нулевое потребление ОЗУ
            при импорте в фоновых воркерах (Sandbox) и защиту от циклических зависимостей.
            Реэкспортируется через фасад core/style.py.
"""


class Colors:
    """Базовые цвета приложения (Single Source of Truth для палитры)."""
    
    # --- Основные фоны (Legacy & Fallback) ---
    BG_MAIN: str = "#232629"
    BG_DARK: str = "#1E2124"
    BG_PANEL: str = "#282B2E"
    BG_INPUT: str = "#232629"
    BG_HEADER: str = "#393B3E"
    
    # --- Premium PCB & Matte Glass Engine ---
    # Цвета для JIT-генерации бесшовной текстуры печатной платы
    BG_SAPPHIRE_BASE: str = "#080C1F"    # Глубокий сапфировый бархат (базовая подложка)
    PCB_NEON_BLUE: str = "#1E2E4A"       # Приглушенный неоновый синий для дорожек платы
    GLASS_MATTE_SHIELD: str = "#080C1F"  # Цвет матового щита (альфа-канал накладывается при рендере)
    
    # --- Текст и Иконки ---
    # Используются в том числе для динамической перекраски SVG
    TXT_PRIMARY: str = "#F0F0F0"
    TXT_SECONDARY: str = "#A0A0A0"     # Базовый цвет для неактивных иконок ModeBar
    TXT_DIM: str = "#767676"
    TXT_HINT: str = "#9AA0A6"          # Мягкий серый для инструкций
    TXT_ACCENT: str = "#FFD700"        # Золотой
    
    # --- Акценты и границы ---
    ACCENT: str = "#FFE066"            # Цвет для активных/Hover иконок ModeBar и колец прогресса
    ACCENT_HOVER: str = "#FFB800"
    ACCENT_PRESSED: str = "#FFC300"
    BORDER: str = "#5A5A5A"
    BORDER_LIGHT: str = "#888888"
    
    # ARGB формат (#AARRGGBB): 20 в HEX = 32 в десятичной = ~12.5% прозрачности.
    # Идеально для супер-тонких, едва заметных разделителей.
    BORDER_SEPARATOR: str = "#20FFFFFF"
    
    # --- Неоновые акценты (Premium UI / Recycler Cards) ---
    NEON_GREEN: str = "#00FF5A"        # Яркий зеленый для кнопок "Join/Start"
    NEON_BLUE: str = "#00E5FF"         # Яркий голубой для кнопок "View/Info"
    NEON_PURPLE: str = "#7D5FFF"       # Пурпурный для спецэффектов
    
    # --- Хрустальная оптика (Crystal Cache Engine) ---
    # ARGB цвета для запекания 3D-сфер (Bake & Blit)
    CRYSTAL_HIGHLIGHT: str = "#99FFFFFF"  # Яркий белый блик (Смягчённый, Alpha ~60%)
    CRYSTAL_SHADOW: str = "#4D000000"  # Глубокая тень преломления (Alpha ~30%)
    CRYSTAL_CAUSTIC: str = "#33FFFFFF"  # База для неонового дна/каустики (Alpha ~20%)
    
    # --- Хрустальные сферы управления (Crystal Action Buttons) ---
    # Палитры для нового 3D-дизайна (Alpha Blending Transition).
    
    BTN_ACTION_IDLE_BG: str = "#4F679A"  # Темно-синий фон покоя (база линзы)
    BTN_ACTION_HOVER_ICON: str = "#FFFFFF"  # Белая иконка при наведении
    
    # Start / Play
    BTN_PLAY_IDLE_ICON: str = "#95AEE2"  # Тускло-синий цвет иконки в покое
    BTN_PLAY_HOVER_BG: str = "#6582C1"   # Яркий синий фон при наведении
    
    # Restart
    BTN_RESTART_IDLE_ICON: str = "#95AEE2"
    BTN_RESTART_HOVER_BG: str = "#6582C1"
    
    # Stop / Close
    BTN_STOP_IDLE_ICON: str = "#95AEE2"
    BTN_STOP_HOVER_BG: str = "#6582C1"
    
    # Блокировка (Disabled)
    BTN_DISABLED_BG: str = "#1E2124"          # Глухой темный фон для заблокированных кнопок
    BTN_DISABLED_ICON: str = "#5A5A5A"        # Глухой серый мат для иконки
    
    # --- Интерактивные элементы (Карточки профилей - Frosted Glass) ---
    # ARGB формат (#AARRGGBB) для аппаратного рендеринга матового стекла
    GLASS_CARD_TINT_START: str = "#23FFFFFF"  # Светлый блик на фаске (Alpha ~35)
    GLASS_CARD_TINT_END: str = "#0AFFFFFF"    # Матовая текстура тела (Alpha ~10)
    GLASS_CARD_SHADOW: str = "#64000000"      # Глубокая тень при левитации (Alpha ~100)
    
    BTN_ARROW_DEFAULT: str = "#5A5A5A" # Legacy: стрелки сортировки
    BTN_ARROW_HOVER: str = "#FFE066"
    BTN_ARROW_PRESSED: str = "#FFC300"
    
    # --- Статусы (Логи и индикаторы) ---
    SUCCESS: str = "#40DB78"
    ERROR: str = "#FF4F4F"
    ERROR_DARK: str = "#D9534F"
    WARNING: str = "#FFD700"
    INFO: str = "#C0C0C0"
    
    # --- Редактор кода (Auto Sandbox) ---
    ED_BG_GUTTER: str = "#1C1F22"
    ED_FG_GUTTER: str = "#8A9099"
    ED_LINE_SPLIT: str = "#2D3136"
    ED_CUR_LINE: str = "#23282E"
    ED_ERROR_LINE: str = "#3D1E1E"

    # --- Глассморфизм (Матовое стекло для общих панелей) ---
    GLASS_BG: str = "#2A2D31"           # Базовый тон стеклянной подложки
    GLASS_BORDER_LIGHT: str = "#FFFFFF" # Светлый блик (верхняя/левая фаска)
    GLASS_BORDER_DARK: str = "#000000"  # Темная тень (нижнее/правое ребро)
    
    # --- Неоморфизм (Вдавленные элементы / Debossed) ---
    # Используются как базовые цвета для альфа-блендинга в Four-Gradient Edge Pipeline
    INPUT_SHADOW_DARK: str = "#000000"  # База для темной внутренней тени
    INPUT_SHADOW_LIGHT: str = "#FFFFFF" # База для светлого внутреннего блика

    # --- 3D Флаги (Globe Effect) ---
    # ARGB формат (#AARRGGBB) для композитного наложения светотени на плоские SVG
    FLAG_SHADOW: str = "#99000000"      # Глубокая тень (Ambient Occlusion, Alpha ~60%)
    FLAG_HIGHLIGHT: str = "#B3FFFFFF"   # Яркий блик линзы (Specular Highlight, Alpha ~70%)