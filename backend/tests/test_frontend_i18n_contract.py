"""The four UI locales must stay a single contract (front-end copy guard).

Every phase adds teacher- and learner-facing sentences, and the product ships all four
languages at once. A key that exists only in `en.json` renders as the raw dotted key in
Baku; a key whose value is an empty string renders as a silent gap in a sentence. Neither
is visible in a type-check, so both are asserted here.

The test reads the real source tree: `frontend/src/i18n/locales/*.json` for the copy and
`frontend/src/**` for every `t("…")` call. A missing front-end directory is a failure,
not a skip - the gate always copies the whole repository.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
LOCALES_DIR = FRONTEND_SRC / "i18n" / "locales"
LOCALES = ("az", "en", "ru", "tr")

_STATIC_CALL = re.compile(r"\bt\(\s*\"([^\"]+)\"")
_TEMPLATE_CALL = re.compile(r"\bt\(\s*`([^`]*)`\s*\)")


def _flatten(node: dict, prefix: str = "") -> dict[str, str]:
    flat: dict[str, str] = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{path}."))
        else:
            flat[path] = value if isinstance(value, str) else json.dumps(value)
    return flat


@pytest.fixture(scope="module")
def locale_maps() -> dict[str, dict[str, str]]:
    assert LOCALES_DIR.is_dir(), f"front-end locales missing at {LOCALES_DIR}"
    maps = {}
    for locale in LOCALES:
        path = LOCALES_DIR / f"{locale}.json"
        assert path.is_file(), f"locale {locale} missing"
        maps[locale] = _flatten(json.loads(path.read_text(encoding="utf-8")))
    return maps


@pytest.fixture(scope="module")
def source_files() -> list[Path]:
    assert FRONTEND_SRC.is_dir(), f"front-end source missing at {FRONTEND_SRC}"
    files = [p for ext in ("*.ts", "*.tsx") for p in FRONTEND_SRC.rglob(ext)]
    assert len(files) > 10, "front-end source tree looks truncated"
    return files


def test_every_locale_carries_the_same_keys(locale_maps):
    reference = set(locale_maps["en"])
    assert reference, "en.json carries no copy at all"
    for locale, flat in locale_maps.items():
        missing = sorted(reference - set(flat))
        extra = sorted(set(flat) - reference)
        assert not missing, f"{locale}.json is missing {len(missing)} keys, first: {missing[:5]}"
        assert not extra, f"{locale}.json has {len(extra)} keys the others do not, first: {extra[:5]}"


def test_no_translation_is_empty(locale_maps):
    blank = {
        locale: sorted(key for key, value in flat.items() if not value.strip())
        for locale, flat in locale_maps.items()
    }
    offenders = {locale: keys for locale, keys in blank.items() if keys}
    assert not offenders, f"empty values render as gaps in a sentence: {offenders}"


def test_every_static_translation_key_exists(locale_maps, source_files):
    known = set(locale_maps["en"])
    used: dict[str, str] = {}
    for path in source_files:
        for key in _STATIC_CALL.findall(path.read_text(encoding="utf-8")):
            used.setdefault(key, str(path.relative_to(REPO_ROOT)))
    assert used, "no t(\"…\") call found - the scanner stopped working"
    unknown = {key: where for key, where in used.items() if key not in known}
    assert not unknown, f"keys used by the UI but absent from the locales: {unknown}"


def test_every_dynamic_translation_family_exists(locale_maps, source_files):
    known = set(locale_maps["en"])
    prefixes: dict[str, str] = {}
    for path in source_files:
        for raw in _TEMPLATE_CALL.findall(path.read_text(encoding="utf-8")):
            prefix = raw.split("${", 1)[0]
            if prefix:
                prefixes.setdefault(prefix, str(path.relative_to(REPO_ROOT)))
    assert prefixes, "no t(`…`) call found - the scanner stopped working"
    dead = {prefix: where for prefix, where in prefixes.items() if not any(k.startswith(prefix) for k in known)}
    assert not dead, f"dynamic key families with no matching copy: {dead}"


def test_ui_language_menu_offers_exactly_the_shipped_locales(source_files):
    index = (FRONTEND_SRC / "i18n" / "index.ts").read_text(encoding="utf-8")
    registered = set(re.findall(r"(\w+)\s*:\s*\{\s*translation\s*:", index))
    assert registered == set(LOCALES), f"i18n/index.ts registers {sorted(registered)}, expected {list(LOCALES)}"
    # a locale file nobody imports would be invisible to the switcher
    for locale in LOCALES:
        assert f"./locales/{locale}.json" in index, f"{locale}.json is not imported by i18n/index.ts"
    switchers = [p for p in source_files if "LanguageSwitcher" in p.read_text(encoding="utf-8")]
    assert switchers, "no screen offers a language switch"
