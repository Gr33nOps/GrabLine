"""The GrabLine design system: one source of truth for colors, spacing, radius,
type, and the global Qt stylesheet.

The accent is the GrabLine brand blue from the app logo (`#2563EB`, shared with
the website and the Android app), a touch brighter in dark mode so it pops.
Dark surfaces follow the website's blue-tinted neutrals; type is IBM Plex.
Every screen and dialog reads its look from here: change a token and the whole
app follows. ``stylesheet(theme)`` returns a QSS string that restyles the
standard Qt widgets (buttons, inputs, tabs, tables, menus, scrollbars,
dialogs) to match, so even screens we haven't hand-built yet look right.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QPalette


@dataclass(frozen=True)
class Palette:
    dark: bool
    # surfaces
    bg: str
    surface: str
    surface2: str
    sidebar: str
    toolbar: str
    row_alt: str
    row_hover: str
    row_sel: str
    # lines
    border: str
    border2: str
    # text
    text: str
    text2: str
    text3: str
    # brand accent (the logo blue)
    accent: str
    accent_h: str
    accent_dim: str
    #: Accent used as *text* (links, selected tab): darker in light mode,
    #: lighter in dark mode so it keeps contrast on the surface.
    accent_text: str
    accent_on: str
    # status
    st_downloading: str
    st_queued: str
    st_paused: str
    st_done: str
    st_failed: str
    st_cancelled: str
    # advisory levels
    ok: str
    warn: str
    # graph series
    g_dl: str
    g_ul: str
    g_ndown: str
    g_nup: str
    g_cpu: str
    g_disk: str


LIGHT = Palette(
    dark=False,
    bg="#f3f5f9",
    surface="#ffffff",
    surface2="#f5f7fa",
    sidebar="#eaeef4",
    toolbar="#f7f9fc",
    row_alt="#f8fafc",
    row_hover="#eef3fb",
    row_sel="#e4edfd",
    border="#dbe0ea",
    border2="#edf0f6",
    text="#161b24",
    text2="#5a6473",
    text3="#98a1b2",
    accent="#2563eb",
    accent_h="#1d4ed8",
    accent_dim="rgba(37,99,235,0.12)",
    accent_text="#1d4ed8",
    accent_on="#ffffff",
    st_downloading="#2563eb",
    st_queued="#4b6bb0",
    st_paused="#8a8a8a",
    st_done="#1f9d55",
    st_failed="#cf222e",
    st_cancelled="#8a8a8a",
    ok="#1f9d55",
    warn="#cf222e",
    g_dl="#2563eb",
    g_ul="#db6d28",
    g_ndown="#0ea5e9",
    g_nup="#a371f7",
    g_cpu="#db3c3c",
    g_disk="#9e6a03",
)

DARK = Palette(
    dark=True,
    bg="#07090e",
    surface="#121821",
    surface2="#182030",
    sidebar="#0c1018",
    toolbar="#0f1520",
    row_alt="#0e131b",
    row_hover="#172033",
    row_sel="#14284f",
    border="#1e2736",
    border2="#161e2b",
    text="#e8eef8",
    text2="#a4b0c4",
    text3="#7f8ba0",
    accent="#2563eb",
    accent_h="#1d4ed8",
    accent_dim="rgba(37,99,235,0.18)",
    accent_text="#86aaff",
    accent_on="#ffffff",
    st_downloading="#2563eb",
    st_queued="#6b93e8",
    st_paused="#6b7280",
    st_done="#3fb950",
    st_failed="#f85149",
    st_cancelled="#6b7280",
    ok="#3fb950",
    warn="#f85149",
    g_dl="#2563eb",
    g_ul="#db6d28",
    g_ndown="#38bdf8",
    g_nup="#bc8cff",
    g_cpu="#f85149",
    g_disk="#d4a72c",
)

# ---------------------------------------------------------------- scale

RADIUS = {"sm": 4, "md": 6, "lg": 8, "pill": 999}
#: Font point sizes. Body ~13px at 96dpi; Qt uses points, so keep modest.
FONT = {"caption": 8, "small": 9, "body": 10, "h2": 11, "h1": 13, "display": 16}


SANS_FAMILY = "IBM Plex Sans"
MONO_FAMILY = "IBM Plex Mono"
_FONT_FILES = (
    "ibm_plex_sans_regular.ttf",
    "ibm_plex_sans_medium.ttf",
    "ibm_plex_sans_semibold.ttf",
    "ibm_plex_mono_regular.ttf",
)
_fonts_loaded: bool | None = None


def load_brand_fonts() -> bool:
    """Register the bundled IBM Plex fonts (OFL, ``app/ui/assets/fonts``) with
    Qt. Needs a QGuiApplication. Safe to call repeatedly; returns whether Plex
    Sans is available (False means callers keep the system font)."""
    global _fonts_loaded
    if _fonts_loaded is not None:
        return _fonts_loaded
    import sys
    from pathlib import Path

    from PySide6.QtGui import QFontDatabase

    fonts_dir = Path(__file__).resolve().parent / "assets" / "fonts"
    if not fonts_dir.is_dir():  # frozen build: assets live under sys._MEIPASS
        fonts_dir = Path(getattr(sys, "_MEIPASS", "")) / "app" / "ui" / "assets" / "fonts"
    for name in _FONT_FILES:
        QFontDatabase.addApplicationFont(str(fonts_dir / name))
    _fonts_loaded = SANS_FAMILY in QFontDatabase.families()
    return _fonts_loaded


def numeric_font(base: QFont | None = None) -> QFont:
    """A copy of ``base`` (or the default font) in IBM Plex Mono (when loaded)
    with tabular figures enabled, so live numbers (speeds, sizes, ETAs,
    percentages) keep every digit the same width and columns stop twitching as
    values tick."""
    import contextlib

    from PySide6.QtGui import QFontDatabase

    font = QFont(base) if base is not None else QFont()
    if MONO_FAMILY in QFontDatabase.families():
        font.setFamily(MONO_FAMILY)
    with contextlib.suppress(AttributeError, ValueError):  # Qt < 6.7
        font.setFeature(QFont.Tag("tnum"), 1)
    return font


#: Accent presets offered in Settings → Appearance ("" = the brand blue).
ACCENT_PRESETS: tuple[tuple[str, str], ...] = (
    ("GrabLine Blue", ""),
    ("Violet", "#7c3aed"),
    ("Green", "#059669"),
    ("Orange", "#ea580c"),
    ("Rose", "#e11d48"),
    ("Teal", "#0d9488"),
)


def with_accent(palette: Palette, accent: str) -> Palette:
    """The palette re-tinted around a different accent. Hover shifts darker in
    light mode and lighter in dark mode; the dim wash and the downloading /
    download-graph colors follow the accent."""
    from dataclasses import replace

    base = QColor(accent)
    hover = base.lighter(125) if palette.dark else base.darker(120)
    dim = f"rgba({base.red()},{base.green()},{base.blue()},{0.18 if palette.dark else 0.12})"
    return replace(
        palette,
        accent=base.name(),
        accent_h=hover.name(),
        accent_dim=dim,
        accent_text=base.name(),
        st_downloading=base.name(),
        g_dl=base.name(),
    )


def status_color(palette: Palette, status: str) -> str:
    return {
        "downloading": palette.st_downloading,
        "queued": palette.st_queued,
        "waiting": palette.st_queued,
        "paused": palette.st_paused,
        "completed": palette.st_done,
        "failed": palette.st_failed,
        "cancelled": palette.st_cancelled,
    }.get(status, palette.text2)


def qpalette(p: Palette) -> QPalette:
    """A QPalette so native-drawn bits (tooltips, selections, disabled text)
    match the stylesheet."""
    pal = QPalette()
    R = QPalette.ColorRole
    pal.setColor(R.Window, QColor(p.bg))
    pal.setColor(R.WindowText, QColor(p.text))
    pal.setColor(R.Base, QColor(p.surface))
    pal.setColor(R.AlternateBase, QColor(p.row_alt))
    pal.setColor(R.ToolTipBase, QColor(p.surface2))
    pal.setColor(R.ToolTipText, QColor(p.text))
    pal.setColor(R.Text, QColor(p.text))
    pal.setColor(R.Button, QColor(p.surface2))
    pal.setColor(R.ButtonText, QColor(p.text))
    pal.setColor(R.Highlight, QColor(p.accent))
    pal.setColor(R.HighlightedText, QColor(p.accent_on))
    pal.setColor(R.Link, QColor(p.accent_text))
    pal.setColor(R.PlaceholderText, QColor(p.text3))
    disabled = QPalette.ColorGroup.Disabled
    for role in (R.Text, R.ButtonText, R.WindowText):
        pal.setColor(disabled, role, QColor(p.text3))
    return pal


def asset_path(name: str) -> str:
    """Absolute, forward-slashed path of a file under ``app/ui/assets`` (source
    tree first, then the PyInstaller bundle) for use in a QSS ``url()``."""
    import sys
    from pathlib import Path

    for base in (
        Path(__file__).resolve().parent / "assets",
        Path(getattr(sys, "_MEIPASS", "")) / "app" / "ui" / "assets",
    ):
        if (base / name).is_file():
            return (base / name).as_posix()
    return (Path(__file__).resolve().parent / "assets" / name).as_posix()


def stylesheet(p: Palette) -> str:
    """The global QSS. Kept flat and calm (borders over shadows) so it reads
    the same on Windows, macOS and Linux."""
    return f"""
    * {{
        outline: none;
    }}
    QWidget {{
        background: {p.bg};
        color: {p.text};
        font-size: {FONT["body"]}pt;
        selection-background-color: {p.accent};
        selection-color: {p.accent_on};
    }}
    QToolTip {{
        background: {p.surface2};
        color: {p.text};
        border: 1px solid {p.border};
        border-radius: {RADIUS["sm"]}px;
        padding: 4px 7px;
    }}

    /* --- buttons --- */
    QPushButton {{
        background: {p.surface2};
        color: {p.text};
        border: 1px solid {p.border};
        border-radius: {RADIUS["md"]}px;
        padding: 5px 12px;
    }}
    QPushButton:hover {{ background: {p.row_hover}; border-color: {p.accent}; }}
    QPushButton:pressed {{ background: {p.row_sel}; }}
    QPushButton:disabled {{ color: {p.text3}; border-color: {p.border2}; }}
    QPushButton:default {{
        background: {p.accent}; color: {p.accent_on}; border: 1px solid {p.accent};
    }}
    QPushButton:default:hover {{ background: {p.accent_h}; border-color: {p.accent_h}; }}
    QPushButton[accent="true"] {{
        background: {p.accent}; color: {p.accent_on}; border: 1px solid {p.accent};
    }}
    QPushButton[accent="true"]:hover {{ background: {p.accent_h}; }}
    QPushButton[flat="true"] {{
        background: transparent; border: none; color: {p.text2}; padding: 5px 9px;
    }}
    QPushButton[flat="true"]:hover {{ background: {p.row_hover}; color: {p.text}; }}
    /* flat disclosure buttons (the Phone page's Advanced toggle): no native
       frame, a hover wash like flat push buttons, and a visible focus ring */
    QToolButton {{
        background: transparent; border: 1px solid transparent; color: {p.text2};
        border-radius: {RADIUS["md"]}px; padding: 3px 8px;
    }}
    QToolButton:hover {{ background: {p.row_hover}; color: {p.text}; }}
    QToolButton:focus {{ border: 1px solid {p.accent}; }}
    QToolButton:disabled {{ color: {p.text3}; }}
    QPushButton[danger="true"]:hover {{ border-color: {p.warn}; color: {p.warn}; }}
    /* the detail drawer's tab strip + its in-tab action buttons */
    QPushButton[role="tab"] {{
        background: transparent; border: none; border-bottom: 2px solid transparent;
        color: {p.text3}; padding: 6px 8px; font-weight: 600;
    }}
    QPushButton[role="tab"]:hover {{ color: {p.text}; }}
    QPushButton[role="tab"]:checked {{
        color: {p.accent_text}; border-bottom: 2px solid {p.accent};
    }}

    /* --- inputs --- */
    QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox,
    QTimeEdit, QDateTimeEdit {{
        background: {p.surface};
        color: {p.text};
        border: 1px solid {p.border};
        border-radius: {RADIUS["md"]}px;
        padding: 4px 8px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_on};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus,
    QDoubleSpinBox:focus, QComboBox:focus, QTimeEdit:focus, QDateTimeEdit:focus {{
        border-color: {p.accent};
    }}
    /* A spin box owns an inner QLineEdit that would otherwise pick up the
       QLineEdit frame and padding above, double-padding the value and clipping
       it in narrow fields (the Phone page's port). */
    QAbstractSpinBox QLineEdit {{
        background: transparent; border: none; padding: 0; margin: 0;
        border-radius: 0;
    }}
    QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ color: {p.text3}; }}
    QComboBox:drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {p.surface};
        color: {p.text};
        border: 1px solid {p.border};
        selection-background-color: {p.accent_dim};
        selection-color: {p.text};
        outline: none;
    }}

    /* --- checkboxes / radios --- */
    QCheckBox, QRadioButton {{ spacing: 8px; color: {p.text}; }}
    QCheckBox:indicator, QRadioButton:indicator {{
        width: 15px; height: 15px;
        border: 1px solid {p.text3}; background: {p.surface};
    }}
    QCheckBox:indicator {{ border-radius: 3px; }}
    QRadioButton:indicator {{ border-radius: 8px; }}
    QCheckBox:indicator:hover, QRadioButton:indicator:hover {{ border-color: {p.accent}; }}
    QCheckBox:indicator:checked, QRadioButton:indicator:checked {{
        background: {p.accent}; border-color: {p.accent};
    }}
    QCheckBox:indicator:checked {{ image: url("{asset_path("check.svg")}"); }}

    /* --- tabs --- */
    QTabWidget:pane {{ border: 1px solid {p.border}; border-radius: {RADIUS["md"]}px; top: -1px; }}
    QTabBar:tab {{
        background: transparent; color: {p.text2};
        padding: 7px 14px; border: none; margin-right: 2px;
    }}
    QTabBar:tab:hover {{ color: {p.text}; }}
    QTabBar:tab:selected {{
        color: {p.accent_text}; border-bottom: 2px solid {p.accent};
    }}

    /* --- tables / lists / trees --- */
    QTableWidget, QTableView, QTreeWidget, QTreeView, QListWidget, QListView {{
        background: {p.surface};
        alternate-background-color: {p.row_alt};
        border: 1px solid {p.border};
        border-radius: {RADIUS["md"]}px;
        gridline-color: {p.border2};
        outline: none;
    }}
    QTableWidget:item, QTreeWidget:item, QListWidget:item {{
        padding: 3px 4px; border: none;
    }}
    /* The downloads table is the content plane, not a floating card: no
       border of its own (the filter bar above and sidebar beside it already
       draw the edges), square corners, and no doubled lines. */
    QTableWidget#JobsTable {{ border: none; border-radius: 0; }}
    /* A layout-only container must never paint. QWidget's base rule gives it
       the page background, which punches a dark hole through whatever it sits
       on - row hover and selection in the table, the card behind it on the
       Queue page. Name any bare wrapper this and it stays out of the way. */
    QWidget#BareContainer {{ background: transparent; }}
    QTableView:item:hover, QTreeView:item:hover, QListView:item:hover {{
        background: {p.row_hover};
    }}
    QTableView:item:selected, QTreeView:item:selected, QListView:item:selected {{
        background: {p.row_sel}; color: {p.text};
    }}
    QHeaderView:section {{
        background: {p.surface2};
        color: {p.text3};
        border: none;
        border-bottom: 1px solid {p.border};
        padding: 6px 10px;
        font-size: {FONT["small"]}pt;
    }}

    /* --- scrollbars --- */
    QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
    QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
    QScrollBar:handle:vertical, QScrollBar:handle:horizontal {{
        background: {p.border}; border-radius: 4px; min-height: 24px; min-width: 24px;
    }}
    QScrollBar:handle:hover {{ background: {p.text3}; }}
    QScrollBar:add-line, QScrollBar:sub-line {{ height: 0; width: 0; }}
    QScrollBar:add-page, QScrollBar:sub-page {{ background: transparent; }}

    /* --- menus --- */
    QMenuBar {{ background: {p.toolbar}; color: {p.text}; }}
    QMenuBar:item {{ background: transparent; padding: 5px 10px; }}
    QMenuBar:item:selected {{ background: {p.row_hover}; border-radius: {RADIUS["sm"]}px; }}
    QMenu {{
        background: {p.surface}; color: {p.text};
        border: 1px solid {p.border}; border-radius: {RADIUS["md"]}px; padding: 4px;
    }}
    QMenu:item {{ padding: 6px 22px 6px 12px; border-radius: {RADIUS["sm"]}px; }}
    QMenu:item:selected {{ background: {p.accent_dim}; color: {p.text}; }}
    QMenu:item:disabled {{ color: {p.text3}; }}
    QMenu:separator {{ height: 1px; background: {p.border2}; margin: 4px 8px; }}

    /* --- progress --- */
    QProgressBar {{
        background: {p.border}; border: none; border-radius: 2px;
        height: 5px; text-align: center; color: {p.text2};
    }}
    QProgressBar:chunk {{ background: {p.accent}; border-radius: 2px; }}

    /* --- group boxes --- */
    QGroupBox {{
        border: 1px solid {p.border}; border-radius: {RADIUS["md"]}px;
        margin-top: 10px; padding: 10px 12px 8px;
    }}
    QGroupBox:title {{
        subcontrol-origin: margin; left: 10px; padding: 0 4px;
        color: {p.text2};
    }}

    QDialog, QMainWindow {{ background: {p.bg}; }}
    QStatusBar {{ background: {p.toolbar}; color: {p.text2}; }}
    QStatusBar:item {{ border: none; }}
    QLabel {{ background: transparent; }}

    /* --- app chrome (object names so a theme swap re-applies them) --- */
    QFrame#TitleBar {{ background: {p.sidebar}; border-bottom: 1px solid {p.border}; }}
    QFrame#Sidebar {{ background: {p.sidebar}; border-right: 1px solid {p.border}; }}
    QFrame#Toolbar {{ background: {p.toolbar}; border-bottom: 1px solid {p.border}; }}
    QFrame#FilterBar {{ background: {p.surface}; border-bottom: 1px solid {p.border}; }}
    QFrame#Separator {{ background: {p.border}; }}
    QFrame#Drawer {{ background: {p.surface}; border-left: 1px solid {p.border}; }}
    QFrame#Drawer QStackedWidget, QFrame#Drawer QScrollArea,
    QFrame#Drawer QScrollArea > QWidget, QFrame#DrawerTabs {{ background: {p.surface}; }}
    QFrame#DrawerHeader {{ background: transparent; border-bottom: 1px solid {p.border}; }}
    QFrame#DrawerFooter {{ background: transparent; border-top: 1px solid {p.border}; }}
    QFrame#SettingsNav {{ background: {p.surface2}; border-right: 1px solid {p.border}; }}
    QFrame#SettingsFooter {{ background: transparent; border-top: 1px solid {p.border}; }}
    QFrame#DevicesNav {{ background: {p.surface2}; border-right: 1px solid {p.border}; }}
    QListWidget#DevicesList {{ border: none; background: transparent; }}
    QListWidget#DevicesList:item {{ border-radius: {RADIUS["md"]}px; margin: 1px 0; }}
    QListWidget#DevicesList:item:selected {{ background: {p.accent_dim}; }}
    QListWidget#DevicesList:item:hover {{ background: {p.row_hover}; }}
    QListWidget#DevicesList:focus {{
        border: 1px solid {p.accent}; border-radius: {RADIUS["md"]}px;
    }}
    QListWidget#SettingsList {{ border: none; background: transparent; }}
    QListWidget#SettingsList:item {{
        padding: 8px 10px; border-radius: {RADIUS["sm"]}px; color: {p.text2}; margin: 1px 0;
    }}
    QListWidget#SettingsList:item:selected {{ background: {p.accent_dim}; color: {p.accent_text}; }}
    QListWidget#SettingsList:item:hover {{ background: {p.row_hover}; }}
    QLabel#AppLogo {{ background: {p.accent}; border-radius: {RADIUS["md"]}px; }}

    /* --- cards + semantic labels (property selectors, re-apply on theme swap) --- */
    QFrame[card="true"] {{
        background: {p.surface}; border: 1px solid {p.border};
        border-radius: {RADIUS["md"]}px;
    }}
    QFrame[card="true"][selected="true"] {{ border: 1px solid {p.accent}; }}
    QFrame[panel="true"] {{
        background: {p.surface2}; border: 1px solid {p.border};
        border-radius: {RADIUS["md"]}px;
    }}
    QLabel#QueueBadge {{
        background: transparent; color: {p.accent_text};
        font-size: {FONT["h1"]}pt; font-weight: 700;
    }}
    QLabel#VpnBanner {{
        color: {p.text2}; background: {p.surface2}; border: 1px solid {p.border};
        border-radius: {RADIUS["md"]}px; padding: 8px 12px;
    }}
    QLabel[role="caption"] {{ color: {p.text3}; font-weight: 700; }}
    QLabel[role="muted"] {{ color: {p.text3}; }}
    QLabel[role="dim"] {{ color: {p.text2}; }}
    QLabel[role="value"] {{ color: {p.text}; }}
    QLabel[role="strong"] {{ color: {p.text}; font-weight: 600; }}
    QLabel[role="accent"] {{ color: {p.accent_text}; font-weight: 600; }}
    QLabel[role="ok"] {{ color: {p.ok}; }}
    QLabel[chip="true"] {{
        color: {p.text2}; background: {p.surface2}; border: 1px solid {p.border};
        border-radius: 3px; padding: 1px 7px;
    }}
    """
