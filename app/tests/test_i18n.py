"""The translation framework and the shipped catalogs.

The catalog checks are the important ones: they guard that no translation drops
or renames a ``{placeholder}`` (which would crash ``t`` at format time) and that
every catalog is well-formed - so adding a language can never break the app.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.core import i18n

CATALOG_DIR = Path(i18n.__file__).resolve().parent.parent / "i18n"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


@pytest.fixture(autouse=True)
def _reset_language():
    yield
    i18n.set_language("en")  # global state: don't leak a language into other tests


def test_unknown_language_and_key_fall_back_to_english():
    i18n.set_language("en")
    assert i18n.t("Add URL") == "Add URL"
    i18n.set_language("zz")  # not a shipped language
    assert i18n.current_language() == "en"
    assert i18n.t("A string with no catalog entry") == "A string with no catalog entry"


def test_format_params_and_bad_placeholders_never_raise():
    i18n.set_language("en")
    assert i18n.t("Found {n} file(s)", n=3) == "Found 3 file(s)"
    # Missing kwargs must fall back to the source text, not raise.
    assert i18n.t("Found {n} file(s)") == "Found {n} file(s)"


def test_known_translation_and_rtl():
    i18n.set_language("es")
    assert i18n.t("Pause") == "Pausar"
    assert not i18n.is_rtl()
    i18n.set_language("ar")
    assert i18n.is_rtl()
    assert i18n.t("Settings") != "Settings"  # actually translated


def test_language_registry_is_consistent():
    langs = i18n.available_languages()
    codes = [code for code, _name, _native in langs]
    assert "en" in codes
    assert len(codes) == len(set(codes)) == 14
    assert "fr" in codes and "zz" not in codes
    assert "ur" in codes and i18n.is_rtl("ur")


def test_every_catalog_is_wellformed_and_keeps_placeholders():
    catalogs = sorted(CATALOG_DIR.glob("*.json"))
    assert catalogs, "no catalogs found"
    for path in catalogs:
        data = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), path.name
        for english, translated in data.items():
            assert isinstance(translated, str) and translated, f"{path.name}: empty {english!r}"
            assert set(_PLACEHOLDER.findall(english)) == set(_PLACEHOLDER.findall(translated)), (
                f"{path.name}: placeholders differ for {english!r} -> {translated!r}"
            )


def test_n_marker_is_identity():
    assert i18n.N_("Name") == "Name"


# ------------------------------------------------- full catalogue coverage
#
# Every shipped language covers every translatable string, with nothing left
# over. Untranslated text silently falls back to English, so without a check
# like this a catalogue drifts out of date invisibly - which is exactly what
# had happened.


def _source_strings() -> set[str]:
    """The app's translatable strings, via the extractor the project ships."""
    import importlib.util

    tool = CATALOG_DIR.parent.parent / "tools" / "i18n_extract.py"
    spec = importlib.util.spec_from_file_location("i18n_extract", tool)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return set(module.source_strings())


def test_the_extractor_ignores_blank_literals():
    """The job list's icon column is N_(""), a layout placeholder rather than
    a phrase. Counting it kept every catalogue one string short forever."""
    assert "" not in _source_strings()


@pytest.mark.parametrize("catalog", sorted(CATALOG_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_every_catalog_is_complete_and_carries_nothing_stale(catalog: Path):
    strings = _source_strings()
    data = json.loads(catalog.read_text(encoding="utf-8"))

    missing = sorted(s for s in strings if not data.get(s))
    assert not missing, f"{catalog.stem}: {len(missing)} untranslated, e.g. {missing[:3]}"

    stale = sorted(set(data) - strings)
    assert not stale, f"{catalog.stem}: {len(stale)} strings no longer in the app, e.g. {stale[:3]}"


@pytest.mark.parametrize("catalog", sorted(CATALOG_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_no_translation_is_left_as_the_english_source_verbatim(catalog: Path):
    """A handful of entries legitimately match English (product names, "Video"),
    but a catalogue that is mostly copied English is not a translation."""
    data = json.loads(catalog.read_text(encoding="utf-8"))
    identical = [k for k, v in data.items() if k == v]
    assert len(identical) < len(data) * 0.25, (
        f"{catalog.stem}: {len(identical)}/{len(data)} entries are the English source verbatim"
    )
