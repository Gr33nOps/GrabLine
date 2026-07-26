"""Lightweight app-wide translation (i18n).

Every user-facing string is written in English in the source and wrapped in
:func:`t`; a per-language JSON catalog (``app/i18n/<code>.json``) maps the English
text to its translation. Anything missing from a catalog - or an unsupported
language - falls back to the English source, so the app is always fully usable.

Why a custom ``t`` instead of Qt's ``tr``: strings come from non-Qt code too
(engine error messages, the CLI), and one function that works everywhere keeps
wrapping uniform. Catalogs are plain JSON, so they need no compile step.

The active language is chosen once at startup (from the saved setting, or the OS
locale on first run) and applied by rebuilding the UI - i.e. a restart. Live
switching is deliberately not attempted here.
"""

from __future__ import annotations

import contextlib
import json
import locale
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

#: (code, English name, endonym) for every language GrabLine ships. English is
#: the source, so it has no catalog. Keep this list and the JSON files in
#: app/i18n in sync (tools/i18n_extract.py reports gaps).
LANGUAGES: tuple[tuple[str, str, str], ...] = (
    ("en", "English", "English"),
    ("es", "Spanish", "Español"),
    ("fr", "French", "Français"),
    ("de", "German", "Deutsch"),
    ("pt", "Portuguese", "Português"),
    ("it", "Italian", "Italiano"),
    ("ru", "Russian", "Русский"),
    ("ar", "Arabic", "العربية"),
    ("hi", "Hindi", "हिन्दी"),
    ("zh", "Chinese (Simplified)", "简体中文"),
    ("ja", "Japanese", "日本語"),
    ("ko", "Korean", "한국어"),
    ("tr", "Turkish", "Türkçe"),
    ("ur", "Urdu", "اردو"),
)

#: Right-to-left languages: the app sets Qt's layout direction for these.
_RTL_CODES = frozenset({"ar", "he", "fa", "ur"})

_CODES = frozenset(code for code, _name, _native in LANGUAGES)
_CATALOG_DIR = Path(__file__).resolve().parent.parent / "i18n"

_current = "en"
_catalog: dict[str, str] = {}


def available_languages() -> tuple[tuple[str, str, str], ...]:
    """(code, English name, endonym) for the language picker."""
    return LANGUAGES


def is_supported(code: str) -> bool:
    return code in _CODES


def current_language() -> str:
    return _current


def is_rtl(code: str | None = None) -> bool:
    return (code or _current) in _RTL_CODES


def set_language(code: str) -> None:
    """Activate a language for the rest of the process. Unknown codes and any
    load failure fall back to English (no catalog)."""
    global _current, _catalog
    _current = code if code in _CODES else "en"
    _catalog = _load_catalog(_current)


def t(text: str, /, **params: object) -> str:
    """Translate ``text`` (the English source) into the active language, then
    fill any ``{name}`` placeholders from ``params``. A missing translation
    keeps the English source; a bad placeholder set never raises."""
    template = _catalog.get(text, text)
    if not params:
        return template
    try:
        return template.format(**params)
    except (KeyError, IndexError, ValueError):
        # A translator mangled a placeholder: fall back to the English source so
        # the value still shows, rather than crashing the UI.
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            return text


def N_(text: str) -> str:
    """Mark a string for extraction but don't translate it now - for constants
    defined at import time (before a language is loaded) and translated later at
    the point of use with :func:`t`. Returns the text unchanged."""
    return text


def system_language() -> str:
    """The OS's language as a supported code, else 'en' - the first-run default
    before the user has chosen. Reads the standard locale env vars (Linux/macOS)
    and falls back to Python's locale detection (Windows)."""
    candidates: list[str] = []
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(var, "")
        if value:
            candidates.append(value.split(":")[0])
    if not candidates:
        with contextlib.suppress(ValueError, TypeError):
            candidates.append(locale.getlocale()[0] or "")
    for raw in candidates:
        code = raw.replace("-", "_").split("_")[0].split(".")[0].lower()
        if code in _CODES:
            return code
    return "en"


def _load_catalog(code: str) -> dict[str, str]:
    if code == "en":
        return {}
    path = _CATALOG_DIR / f"{code}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.info("i18n: no usable catalog for %s (%s); using English", code, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): str(value) for key, value in data.items() if value}
