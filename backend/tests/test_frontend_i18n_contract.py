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

import ast
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

#: The services whose refusals a teacher or a learner meets on a screen.
_REFUSAL_SOURCES = (
    REPO_ROOT / "backend/app/services/exam_service.py",
    REPO_ROOT / "backend/app/services/attempt_service.py",
)
_REFUSAL_CLASSES = {
    "ExamError",
    "ExamNotFound",
    "NameTaken",
    "DuplicateReference",
    "LockedComposition",
    "AttemptError",
    "AttemptNotFound",
    "AttemptClosed",
    "NotAssigned",
}

#: A refusal the router itself makes, rather than one a service raised and the router mapped.
#: The exam routers are where a boundary rule lives - "this id names no paper of yours" - and
#: such a rule needs the same code-and-locale treatment as a service rule.
_ENDPOINT_SOURCES = (
    REPO_ROOT / "backend/app/api/v1/endpoints/exams.py",
    REPO_ROOT / "backend/app/api/v1/endpoints/student_exams.py",
)
_ENDPOINT_REFUSALS = {"NotFound", "Conflict", "ValidationFailed"}

#: The one refusal the screen makes without a server: nothing answered, so no code arrived and
#: the copy still cannot be the browser's English sentence. `refusalText("code", …)` is where the
#: client names such a refusal, and the scanner reads the same list so an orphan check can pass it.
_CLIENT_SOURCE = FRONTEND_SRC / "api" / "client.ts"
_CLIENT_REFUSAL = re.compile(r'refusalText\(\s*"([a-z_]+)"')


def _client_refusals() -> dict[str, list[str]]:
    text = _CLIENT_SOURCE.read_text(encoding="utf-8")
    return {code: [f"{_CLIENT_SOURCE.name}"] for code in sorted(set(_CLIENT_REFUSAL.findall(text)))}



def _endpoint_refusals() -> tuple[dict[str, list[str]], list[str]]:
    codes: dict[str, list[str]] = {}
    uncoded: list[str] = []
    for path in _ENDPOINT_SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in _ENDPOINT_REFUSALS:
                continue
            given = next((kw for kw in node.exc.keywords if kw.arg == "code"), None)
            where = f"{path.name}:{node.lineno} {name}"
            if given is None:
                uncoded.append(where)
            elif isinstance(given.value, ast.Constant):
                codes.setdefault(given.value.value, []).append(where)
    return codes, uncoded



def _refusal_ast() -> tuple[dict[str, list[str]], list[str]]:
    """Every code the exam services can answer with, and every refusal that names none.

    A code reaches the vocabulary from a `code=` at the raise site, from the default of a refusal
    class, or from a refusal table (`_START_REFUSALS`) that picks the pair out of the reason the
    engine was given. Each raise site has to name its own code: a class default is a fallback for a
    rule nobody thought about, not a sentence to translate.
    """
    codes: dict[str, list[str]] = {}
    uncoded: list[str] = []
    indirect: list[str] = []

    def record(code: str, where: str) -> None:
        codes.setdefault(code, []).append(where)

    for path in _REFUSAL_SOURCES:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for statement in tree.body:
            if isinstance(statement, ast.ClassDef) and statement.name in _REFUSAL_CLASSES:
                for item in statement.body:
                    if (
                        isinstance(item, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "code" for t in item.targets)
                        and isinstance(item.value, ast.Constant)
                    ):
                        record(item.value.value, f"{path.name}:{item.lineno} {statement.name} default")
            if isinstance(statement, ast.Assign) and any(
                isinstance(t, ast.Name) and "REFUSAL" in t.id.upper() for t in statement.targets
            ):
                for pair in ast.walk(statement.value):
                    if isinstance(pair, ast.Tuple) and pair.elts:
                        head = pair.elts[0]
                        if isinstance(head, ast.Constant) and isinstance(head.value, str):
                            record(head.value, f"{path.name}:{statement.lineno} table")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            func = node.exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name not in _REFUSAL_CLASSES:
                continue
            given = [kw for kw in node.exc.keywords if kw.arg == "code"]
            where = f"{path.name}:{node.lineno} {name}"
            if not given:
                uncoded.append(where)
            elif isinstance(given[0].value, ast.Constant):
                record(given[0].value.value, where)
            else:
                indirect.append(where)
    return codes, uncoded + [f"indirect: {site}" for site in indirect]



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


def test_every_question_type_is_named_in_every_locale(locale_maps):
    """The engine's type codes are the vocabulary; the screens say them in words.

    `/questions/types` deliberately sends codes, so a type the locales do not name would
    print `questions.type_gap_fill` in front of a teacher. Adding a type to the registry
    without adding its name is caught here rather than on a screen.
    """
    from app.services import question_engine

    named = set(question_engine.type_keys())
    for locale, flat in locale_maps.items():
        unnamed = [key for key in sorted(named) if f"questions.type_{key}" not in flat]
        assert not unnamed, f"{locale}.json has no name for {unnamed}"
        orphans = sorted(
            key[len("questions.type_") :] for key in flat if key.startswith("questions.type_")
        )
        extra = [name for name in orphans if name not in named]
        assert not extra, f"{locale}.json names types the engine does not offer: {extra}"


def test_ui_language_menu_offers_exactly_the_shipped_locales(source_files):
    index = (FRONTEND_SRC / "i18n" / "index.ts").read_text(encoding="utf-8")
    registered = set(re.findall(r"(\w+)\s*:\s*\{\s*translation\s*:", index))
    assert registered == set(LOCALES), f"i18n/index.ts registers {sorted(registered)}, expected {list(LOCALES)}"
    # a locale file nobody imports would be invisible to the switcher
    for locale in LOCALES:
        assert f"./locales/{locale}.json" in index, f"{locale}.json is not imported by i18n/index.ts"
    switchers = [p for p in source_files if "LanguageSwitcher" in p.read_text(encoding="utf-8")]
    assert switchers, "no screen offers a language switch"


# --------------------------------------------------------------------------- #
# Refusals: the server names the rule, the screen finds the words
# --------------------------------------------------------------------------- #


def test_every_exam_refusal_names_the_rule_it_broke() -> None:
    """A refusal without a code can only be shown as the server's own English.

    The screens print `e.message`, so an English sentence from a service lands in front of a
    teacher reading Azerbaijani. `code=` is what lets the locale answer instead, and it is what a
    learner's screen branches on when it has to do something rather than say something.
    """
    _codes, uncoded = _refusal_ast()
    _endpoint_codes, uncoded_at_the_boundary = _endpoint_refusals()
    real = [site for site in uncoded if not site.startswith("indirect:")]
    assert not real, f"refusals that raise without naming a code: {real}"
    assert not uncoded_at_the_boundary, (
        f"router refusals that answer with an English sentence only: {uncoded_at_the_boundary}"
    )


def test_every_refusal_code_is_named_in_every_locale(locale_maps) -> None:
    """`errors.<code>` is the contract between a refusal and a screen.

    A code the services can answer with but the locales do not name falls back to the server's
    English sentence, which is readable and still the leak this section exists to close. The
    reverse - copy for a code nobody raises - is caught too, so a rename cannot leave four
    orphaned sentences behind.
    """
    codes, _uncoded = _refusal_ast()
    endpoint_codes, _none = _endpoint_refusals()
    codes = {**codes, **endpoint_codes, **_client_refusals()}
    assert len(codes) > 50, f"the scanner found only {len(codes)} refusal codes - it stopped working"
    for locale, flat in locale_maps.items():
        unnamed = sorted(f"errors.{code}" for code in codes if f"errors.{code}" not in flat)
        assert not unnamed, f"{locale}.json has no copy for {unnamed[:8]}"
        orphans = sorted(
            key[len("errors.") :]
            for key in flat
            if key.startswith("errors.") and key[len("errors.") :] not in codes
        )
        assert not orphans, f"{locale}.json names refusals no exam service raises: {orphans[:8]}"


def test_a_refusal_sentence_carries_the_numbers_the_server_sent(locale_maps) -> None:
    """{{n}} is why the count is in `params` rather than inside an English sentence.

    A locale that drops the placeholder silently loses the number, so the copy for the language
    the interface is read in has to keep the ones the backend sends for that code.
    """
    codes, _uncoded = _refusal_ast()
    sent: dict[str, set[str]] = {}
    for path in _REFUSAL_SOURCES:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
                continue
            code_kw = next((kw for kw in node.exc.keywords if kw.arg == "code"), None)
            params_kw = next((kw for kw in node.exc.keywords if kw.arg == "params"), None)
            if not (code_kw and isinstance(code_kw.value, ast.Constant)):
                continue
            keys = set()
            if params_kw and isinstance(params_kw.value, ast.Dict):
                keys = {k.value for k in params_kw.value.keys if isinstance(k, ast.Constant)}
            sent.setdefault(code_kw.value.value, set()).update(keys)

    for code, params in sorted(sent.items()):
        for locale, flat in locale_maps.items():
            text = flat.get(f"errors.{code}", "")
            missing = [key for key in sorted(params) if "{{" + key + "}}" not in text]
            assert not missing, f"{locale}.json errors.{code} loses {missing}"

