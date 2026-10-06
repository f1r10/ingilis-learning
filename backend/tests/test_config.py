"""Config / Settings parsing tests: pure, no DB."""
from __future__ import annotations

import pydantic
import pytest

from app.core.config import Settings


def _base(**overrides) -> Settings:
    # Secrets are required; give valid defaults so each test only varies one thing.
    data = {
        "session_secret": "a-very-long-session-secret",
        "csrf_secret": "a-very-long-csrf-secret",
    }
    data.update(overrides)
    return Settings(_env_file=None, **data)


def test_language_list_parsing_strips_and_drops_empty():
    s = _base(enabled_ui_languages="az, en ,,ru")
    assert s.enabled_ui_language_list == ["az", "en", "ru"]


def test_learning_and_translation_lists():
    s = _base(learning_languages="en", translation_languages="az,ru,tr")
    assert s.learning_language_list == ["en"]
    assert s.translation_language_list == ["az", "ru", "tr"]


def test_empty_cookie_domain_becomes_none():
    # The field_validator coerces "" -> None so cookies are host-only.
    assert _base(cookie_domain="").cookie_domain is None
    assert _base(cookie_domain=None).cookie_domain is None
    assert _base(cookie_domain="example.org").cookie_domain == "example.org"


def test_is_production_flag():
    assert _base(app_env="production").is_production is True
    assert _base(app_env="development").is_production is False


def test_short_secret_rejected():

    with pytest.raises(pydantic.ValidationError):
        _base(session_secret="tooshort")
