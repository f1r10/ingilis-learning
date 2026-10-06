"""Vocabulary bank persistence (Phase 4): the word list teachers maintain by hand.

Three rules shape this module.

* A word is unique while it is live. The case-insensitive check here is the teacher
  facing one ("Run is already in the bank"); the partial unique index from migration
  `0002` is the one that survives two requests at once. Both report the same conflict,
  so a race never becomes a 500 and never becomes a silent duplicate.
* Trash is a soft delete and stays restorable. Because the unique index only covers
  live rows, restoring an entry whose word has since been re-added would violate it -
  so the restore checks first and says so, instead of letting Postgres answer the
  teacher with a stack trace.
* Translations and examples are part of the entry, not separate records. A patch that
  sends them replaces that set; a patch that omits them leaves it alone. Nothing is
  merged by language, because "I fixed one of the three Azerbaijani meanings" has no
  safe automated answer.

Text fields are stored as the teacher typed them (trimmed). No field is filled in
behind their back: an entry with no definition stays an entry with no definition until
Phase 12's optional enrichment or the teacher writes one.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import enums, security
from app.models.content import (
    MediaAsset,
    Tag,
    VocabularyEntry,
    VocabularyExample,
    VocabularyTranslation,
    vocabulary_tag,
)
from app.schemas import vocabulary as v_schemas
from app.services import audit_service, settings_service

SORTABLE = {
    "created_at": VocabularyEntry.created_at,
    "updated_at": VocabularyEntry.updated_at,
    "word": VocabularyEntry.word,
    "level": VocabularyEntry.level,
    "status": VocabularyEntry.status,
    "part_of_speech": VocabularyEntry.part_of_speech,
}

#: What the picker offers. Free text is still accepted, because a teacher's
#: "phrasal verb" is real content and refusing it would only push them into notes.
LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]
PARTS_OF_SPEECH = [
    "noun",
    "verb",
    "adjective",
    "adverb",
    "preposition",
    "conjunction",
    "pronoun",
    "determiner",
    "exclamation",
    "phrase",
]

# Lifecycle states the teacher can set. TRASH is recorded by `deleted_at`, so
# accepting it here would create a second source of truth for one state.
SETTABLE_STATUSES = ["draft", "ready", "archived"]

#: A study card is only ever served from this state: ready, and not in the trash.
LEARNER_STATUS = enums.ContentStatus.READY.name


class VocabularyError(ValueError):
    """A teacher-facing validation problem; the endpoints turn it into a 422."""


class DuplicateWord(Exception):
    """The word is already in the bank for that learning language (409)."""

    def __init__(self, message: str, *, existing_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.existing_id = existing_id


def _label_of(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def status_enum(raw: str) -> enums.ContentStatus:
    try:
        value = enums.ContentStatus(raw)
    except ValueError:
        raise VocabularyError(f"status must be one of: {', '.join(SETTABLE_STATUSES)}") from None
    if value == enums.ContentStatus.TRASH:
        raise VocabularyError("trash is a deletion, not a status - use the trash action")
    return value


# --------------------------------------------------------------------------- #
# Language rules
# --------------------------------------------------------------------------- #


async def language_options(db: AsyncSession) -> dict[str, list[str]]:
    """The languages the platform is configured for - never a client-side list."""
    ui = await settings_service.get_ui_config(db)
    learning = [str(code) for code in ui["learning_languages"]]
    translation = [str(code) for code in ui["translation_languages"]]
    return {
        "learning_languages": learning,
        "translation_languages": translation,
        # An example sentence may be written in either: the target language, or the
        # language the class is taught in when the teacher glosses it.
        "example_languages": sorted(set(learning) | set(translation)),
        "levels": LEVELS,
        "parts_of_speech": PARTS_OF_SPEECH,
        "statuses": SETTABLE_STATUSES,
    }


async def _check_language(
    db: AsyncSession,
    *,
    learning_language: str | None,
    translations: list[Any],
    examples: list[Any],
) -> None:
    options = await language_options(db)
    if learning_language is not None and learning_language not in options["learning_languages"]:
        raise VocabularyError(
            f"'{learning_language}' is not an enabled learning language "
            f"(available: {', '.join(options['learning_languages']) or 'none configured'})"
        )
    allowed = set(options["translation_languages"])
    seen: set[str] = set()
    for row in translations:
        code = row.language
        if code in seen:
            raise VocabularyError(f"two translations for '{code}' - one entry keeps one meaning per language")
        seen.add(code)
        # Checked before the enabled list: "translate this English word into English"
        # is the mistake the teacher made, and it stays the message even if an admin
        # has left the learning language in the translation list as well.
        if learning_language and code == learning_language:
            raise VocabularyError(f"a '{code}' entry cannot be translated into '{code}'")
        if code not in allowed:
            raise VocabularyError(
                f"'{code}' is not an enabled translation language "
                f"(available: {', '.join(sorted(allowed)) or 'none configured'})"
            )
    for row in examples:
        if row.language and row.language not in set(options["example_languages"]):
            raise VocabularyError(
                f"example language '{row.language}' is neither a learning nor a translation language"
            )


# --------------------------------------------------------------------------- #
# Duplicate rules
# --------------------------------------------------------------------------- #


async def _live_duplicate(
    db: AsyncSession, word: str, learning_language: str | None, *, exclude_id: uuid.UUID | None = None
) -> VocabularyEntry | None:
    """The live entry that already owns this word, however the word is capitalised.

    Case-insensitive because 'run' and 'Run' are one card in a learner's word list and
    two rows in the bank; the language is part of the key because 'run' as an English
    word and 'run' as an Azerbaijani loanword are different entries.
    """
    stmt = select(VocabularyEntry).where(
        func.lower(VocabularyEntry.word) == word.lower(),
        VocabularyEntry.deleted_at.is_(None),
    )
    stmt = stmt.where(
        VocabularyEntry.learning_language == learning_language
        if learning_language is not None
        else VocabularyEntry.learning_language.is_(None)
    )
    if exclude_id is not None:
        stmt = stmt.where(VocabularyEntry.id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _reject_duplicate(
    db: AsyncSession, word: str, learning_language: str | None, *, exclude_id: uuid.UUID | None = None
) -> None:
    clash = await _live_duplicate(db, word, learning_language, exclude_id=exclude_id)
    if clash is not None:
        raise DuplicateWord(
            f"The word '{clash.word}' is already in the bank ({learning_language})",
            existing_id=clash.id,
        )


# --------------------------------------------------------------------------- #
# Read payloads
# --------------------------------------------------------------------------- #


async def _tags_of(db: AsyncSession, entry_id: uuid.UUID) -> list[dict]:
    rows = (
        await db.execute(
            select(Tag.id, Tag.name)
            .join(vocabulary_tag, vocabulary_tag.c.tag_id == Tag.id)
            .where(vocabulary_tag.c.vocabulary_entry_id == entry_id)
            .order_by(Tag.name)
        )
    ).all()
    return [{"id": str(row[0]), "name": row[1]} for row in rows]


async def to_read(db: AsyncSession, entry: VocabularyEntry) -> dict:
    translations = (
        await db.execute(
            select(VocabularyTranslation)
            .where(VocabularyTranslation.entry_id == entry.id)
            .order_by(VocabularyTranslation.language, VocabularyTranslation.created_at)
        )
    ).scalars().all()
    examples = (
        await db.execute(
            select(VocabularyExample)
            .where(VocabularyExample.entry_id == entry.id)
            .order_by(VocabularyExample.created_at)
        )
    ).scalars().all()
    return v_schemas.VocabularyRead(
        id=entry.id,
        word=entry.word,
        learning_language=entry.learning_language,
        definition=entry.definition,
        ipa=entry.ipa,
        part_of_speech=entry.part_of_speech,
        level=entry.level,
        status=_label_of(entry.status),
        synonyms=list(entry.synonyms or []),
        antonyms=list(entry.antonyms or []),
        notes=entry.notes,
        audio_asset_id=entry.audio_asset_id,
        source_file_id=entry.source_file_id,
        translations=[
            v_schemas.TranslationRead(id=row.id, language=row.language, value=row.value)
            for row in translations
        ],
        examples=[
            v_schemas.ExampleRead(
                id=row.id, sentence=row.sentence, language=row.language, translation=row.translation
            )
            for row in examples
        ],
        tags=[v_schemas.TaxonomyRef.model_validate(item) for item in await _tags_of(db, entry.id)],
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        deleted_at=entry.deleted_at,
    ).model_dump(mode="json")


def _summary(
    entry: VocabularyEntry,
    translation_languages: list[str],
    example_count: int,
    tag_names: list[str],
) -> dict:
    """Rows go through the schema, so a list item cannot diverge from the detail."""
    return v_schemas.VocabularySummary(
        id=entry.id,
        word=entry.word,
        learning_language=entry.learning_language,
        definition=entry.definition,
        ipa=entry.ipa,
        part_of_speech=entry.part_of_speech,
        level=entry.level,
        status=_label_of(entry.status),
        synonyms=list(entry.synonyms or []),
        translation_languages=translation_languages,
        example_count=example_count,
        tag_names=tag_names,
        has_audio=entry.audio_asset_id is not None,
        has_source=entry.source_file_id is not None,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        deleted_at=entry.deleted_at,
    ).model_dump(mode="json")


async def _bulk_child_counts(
    db: AsyncSession, entry_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, list[str]], dict[uuid.UUID, int], dict[uuid.UUID, list[str]]]:
    """Translations, example counts and tag names for a whole page in three queries."""
    if not entry_ids:
        return {}, {}, {}
    translation_rows = (
        await db.execute(
            select(VocabularyTranslation.entry_id, VocabularyTranslation.language)
            .where(VocabularyTranslation.entry_id.in_(entry_ids))
            .order_by(VocabularyTranslation.entry_id, VocabularyTranslation.language)
        )
    ).all()
    example_rows = (
        await db.execute(
            select(VocabularyExample.entry_id, func.count())
            .where(VocabularyExample.entry_id.in_(entry_ids))
            .group_by(VocabularyExample.entry_id)
        )
    ).all()
    tag_rows = (
        await db.execute(
            select(vocabulary_tag.c.vocabulary_entry_id, Tag.name)
            .join(Tag, Tag.id == vocabulary_tag.c.tag_id)
            .where(vocabulary_tag.c.vocabulary_entry_id.in_(entry_ids))
            .order_by(Tag.name)
        )
    ).all()
    languages: dict[uuid.UUID, list[str]] = {entry_id: [] for entry_id in entry_ids}
    tags: dict[uuid.UUID, list[str]] = {entry_id: [] for entry_id in entry_ids}
    for entry_id, language in translation_rows:
        languages[entry_id].append(language)
    for entry_id, name in tag_rows:
        tags[entry_id].append(name)
    return languages, {entry_id: int(count) for entry_id, count in example_rows}, tags


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


async def get(db: AsyncSession, entry_id: uuid.UUID, *, allow_trash: bool = True) -> VocabularyEntry | None:
    entry = await db.get(VocabularyEntry, entry_id)
    if entry is None:
        return None
    if entry.deleted_at is not None and not allow_trash:
        return None
    return entry


def is_learner_visible(entry: VocabularyEntry) -> bool:
    """Ready and not trashed - the only state a study card is ever served from.

    A draft is a word the teacher has not finished, so a student asking for it gets a
    plain 404 rather than a half-written entry.
    """
    return entry.deleted_at is None and entry.status == enums.ContentStatus.READY


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def _apply_filters(
    stmt,
    *,
    q: str | None,
    q_kind: str,
    learning_language: str | None,
    level: str | None,
    status: str | None,
    part_of_speech: str | None,
    tag_id: uuid.UUID | None,
    translation_language: str | None,
    has_audio: bool | None,
):
    if q:
        if q_kind == "exact":
            # 'exact' is case-insensitive for the same reason the duplicate rule is:
            # a learner typing "Run" means the card that says "run".
            stmt = stmt.where(func.lower(VocabularyEntry.word) == q.lower())
        elif q_kind == "starts_with":
            stmt = stmt.where(VocabularyEntry.word.ilike(f"{q}%"))
        elif q_kind == "word_only":
            stmt = stmt.where(VocabularyEntry.word.ilike(f"%{q}%"))
        else:
            like = f"%{q}%"
            stmt = stmt.where(
                or_(
                    VocabularyEntry.word.ilike(like),
                    VocabularyEntry.definition.ilike(like),
                    VocabularyEntry.notes.ilike(like),
                )
            )
    if learning_language:
        stmt = stmt.where(VocabularyEntry.learning_language == learning_language)
    if level:
        stmt = stmt.where(VocabularyEntry.level == level)
    if status:
        stmt = stmt.where(VocabularyEntry.status == status_enum(status).name)
    if part_of_speech:
        stmt = stmt.where(func.lower(VocabularyEntry.part_of_speech) == part_of_speech.lower())
    if has_audio is not None:
        stmt = stmt.where(
            VocabularyEntry.audio_asset_id.is_not(None) if has_audio else VocabularyEntry.audio_asset_id.is_(None)
        )
    if tag_id:
        stmt = stmt.where(
            VocabularyEntry.id.in_(
                select(vocabulary_tag.c.vocabulary_entry_id).where(vocabulary_tag.c.tag_id == tag_id)
            )
        )
    if translation_language:
        stmt = stmt.where(
            VocabularyEntry.id.in_(
                select(VocabularyTranslation.entry_id).where(
                    VocabularyTranslation.language == translation_language
                )
            )
        )
    return stmt


async def list_entries(
    db: AsyncSession,
    *,
    q: str | None = None,
    q_kind: str = "contains",
    learning_language: str | None = None,
    level: str | None = None,
    status: str | None = None,
    part_of_speech: str | None = None,
    tag_id: uuid.UUID | None = None,
    translation_language: str | None = None,
    has_audio: bool | None = None,
    view: str = "bank",
    sort: str = "word",
    order: str = "asc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """`view=bank` hides the trash, `view=trash` shows only it, `view=all` shows both."""
    if view not in ("bank", "trash", "all"):
        raise VocabularyError("view must be 'bank', 'trash' or 'all'")
    if q_kind not in ("contains", "exact", "starts_with", "word_only"):
        raise VocabularyError("q_kind must be 'contains', 'exact', 'starts_with' or 'word_only'")
    if sort not in SORTABLE:
        raise VocabularyError("sort must be one of: " + ", ".join(sorted(SORTABLE)))
    if order not in ("asc", "desc"):
        raise VocabularyError("order must be 'asc' or 'desc'")
    page = max(1, page)
    page_size = min(max(1, page_size), 200)

    stmt = select(VocabularyEntry)
    if view == "bank":
        stmt = stmt.where(VocabularyEntry.deleted_at.is_(None))
    elif view == "trash":
        stmt = stmt.where(VocabularyEntry.deleted_at.is_not(None))

    stmt = _apply_filters(
        stmt,
        q=q,
        q_kind=q_kind,
        learning_language=learning_language,
        level=level,
        status=status,
        part_of_speech=part_of_speech,
        tag_id=tag_id,
        translation_language=translation_language,
        has_audio=has_audio,
    )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    column = SORTABLE[sort]
    direction = column.desc() if order == "desc" else column.asc()
    rows = (
        (
            await db.execute(
                # A stable tie-break, so page 2 never repeats or skips a row.
                stmt.order_by(direction.nulls_last(), VocabularyEntry.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    languages, example_counts, tag_names = await _bulk_child_counts(db, [row.id for row in rows])
    return {
        "items": [
            _summary(
                row,
                languages.get(row.id, []),
                example_counts.get(row.id, 0),
                tag_names.get(row.id, []),
            )
            for row in rows
        ],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


async def list_for_learner(
    db: AsyncSession,
    *,
    q: str | None = None,
    level: str | None = None,
    learning_language: str | None = None,
    tag_id: uuid.UUID | None = None,
    translation_language: str | None = None,
    sort: str = "word",
    order: str = "asc",
    page: int = 1,
    page_size: int = 30,
) -> dict:
    """Ready, non-trashed entries only - a draft is not a word to study yet."""
    if sort not in SORTABLE:
        raise VocabularyError("sort must be one of: " + ", ".join(sorted(SORTABLE)))
    if order not in ("asc", "desc"):
        raise VocabularyError("order must be 'asc' or 'desc'")
    page = max(1, page)
    page_size = min(max(1, page_size), 100)

    stmt = select(VocabularyEntry).where(
        VocabularyEntry.deleted_at.is_(None),
        VocabularyEntry.status == LEARNER_STATUS,
    )
    stmt = _apply_filters(
        stmt,
        # A learner searching the word list is looking for the word, not the teacher's
        # private notes, so the free-text search stays on the word itself.
        q=q,
        q_kind="word_only",
        learning_language=learning_language,
        level=level,
        status=None,
        part_of_speech=None,
        tag_id=tag_id,
        translation_language=translation_language,
        has_audio=None,
    )
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    column = SORTABLE[sort]
    direction = column.desc() if order == "desc" else column.asc()
    rows = (
        (
            await db.execute(
                stmt.order_by(direction.nulls_last(), VocabularyEntry.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    languages, example_counts, tag_names = await _bulk_child_counts(db, [row.id for row in rows])
    return {
        "items": [
            _summary(
                row,
                languages.get(row.id, []),
                example_counts.get(row.id, 0),
                tag_names.get(row.id, []),
            )
            for row in rows
        ],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Children
# --------------------------------------------------------------------------- #


async def _replace_translations(db: AsyncSession, entry: VocabularyEntry, rows: list[Any]) -> None:
    # Validated before anything is deleted: a set with one blank meaning must not
    # leave the entry with the other meanings already removed.
    cleaned = []
    for row in rows:
        value = (row.value or "").strip()
        if not value:
            raise VocabularyError(f"the '{row.language}' translation is empty")
        cleaned.append((row.language.strip().lower(), value))
    await db.execute(
        VocabularyTranslation.__table__.delete().where(VocabularyTranslation.entry_id == entry.id)
    )
    for language, value in cleaned:
        db.add(VocabularyTranslation(entry_id=entry.id, language=language, value=value))
    await db.flush()


async def _replace_examples(db: AsyncSession, entry: VocabularyEntry, rows: list[Any]) -> None:
    cleaned = []
    for row in rows:
        sentence = (row.sentence or "").strip()
        if not sentence:
            raise VocabularyError("an example needs a sentence")
        translation = (row.translation or "").strip() if row.translation is not None else None
        cleaned.append(
            (
                sentence,
                row.language.strip().lower() if row.language else None,
                translation or None,
            )
        )
    await db.execute(VocabularyExample.__table__.delete().where(VocabularyExample.entry_id == entry.id))
    for sentence, language, translation in cleaned:
        db.add(
            VocabularyExample(
                entry_id=entry.id, sentence=sentence, language=language, translation=translation
            )
        )
    await db.flush()


async def _tag_ids_of(db: AsyncSession, entry_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await db.execute(
                select(vocabulary_tag.c.tag_id).where(vocabulary_tag.c.vocabulary_entry_id == entry_id)
            )
        ).scalars().all()
    )


async def _replace_tags(db: AsyncSession, entry_id: uuid.UUID, tag_ids: list[uuid.UUID]) -> None:
    await db.execute(vocabulary_tag.delete().where(vocabulary_tag.c.vocabulary_entry_id == entry_id))
    for tag_id in dict.fromkeys(tag_ids):
        await db.execute(
            vocabulary_tag.insert().values(vocabulary_entry_id=entry_id, tag_id=tag_id)
        )
    await db.flush()


async def _require_exists(db: AsyncSession, model: type, value: uuid.UUID | None, label: str) -> None:
    if value is None:
        return
    if await db.get(model, value) is None:
        raise VocabularyError(f"{label} '{value}' does not exist")


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


async def create_entry(db: AsyncSession, payload: v_schemas.VocabularyCreate, *, admin_id: uuid.UUID) -> dict:
    word = payload.word.strip()
    if not word:
        raise VocabularyError("a vocabulary entry needs a word")
    await _check_language(
        db, learning_language=payload.learning_language, translations=payload.translations, examples=payload.examples
    )
    await _reject_duplicate(db, word, payload.learning_language)
    await _require_exists(db, MediaAsset, payload.audio_asset_id, "media asset")
    for tag_id in payload.tag_ids:
        await _require_exists(db, Tag, tag_id, "tag")

    entry = VocabularyEntry(
        word=word,
        learning_language=payload.learning_language,
        definition=(payload.definition or "").strip() or None,
        ipa=(payload.ipa or "").strip() or None,
        part_of_speech=(payload.part_of_speech or "").strip() or None,
        level=(payload.level or "").strip() or None,
        status=status_enum(payload.status),
        synonyms=list(payload.synonyms),
        antonyms=list(payload.antonyms),
        notes=(payload.notes or "").strip() or None,
        audio_asset_id=payload.audio_asset_id,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError:
        # The partial unique index settled a race between two identical requests.
        await db.rollback()
        raise DuplicateWord(f"The word '{word}' is already in the bank ({payload.learning_language})") from None

    await _replace_translations(db, entry, payload.translations)
    await _replace_examples(db, entry, payload.examples)
    await _replace_tags(db, entry.id, payload.tag_ids)
    await audit_service.record_audit(
        db,
        action="vocabulary.created",
        actor_id=admin_id,
        target_type="vocabulary",
        target_id=entry.id,
        after={"word": entry.word, "learning_language": entry.learning_language, "status": _label_of(entry.status)},
    )
    return await to_read(db, entry)


async def update_entry(
    db: AsyncSession, entry: VocabularyEntry, payload: v_schemas.VocabularyUpdate, *, admin_id: uuid.UUID
) -> dict:
    """Apply a patch. Absent fields keep their values.

    `translations` and `examples` replace their whole set when the patch mentions
    them: there is no safe automated answer to "I sent one Azerbaijani meaning and the
    entry already has three", so the API never guesses and the UI always sends the set
    it is showing.
    """
    changes = payload.model_dump(exclude_unset=True)

    word = entry.word
    if "word" in changes:
        word = (changes["word"] or "").strip()
        if not word:
            raise VocabularyError("a vocabulary entry needs a word")
    learning_language = entry.learning_language
    if "learning_language" in changes:
        learning_language = (changes["learning_language"] or "").strip()
        # Storage allows a null, but a word without a language cannot be filed,
        # deduplicated or filtered, so the API never creates one.
        if not learning_language:
            raise VocabularyError("a vocabulary entry needs a learning language")

    replacing_translations = "translations" in changes
    replacing_examples = "examples" in changes
    # The validated child objects, not the dumped ones: model_dump turns a translation
    # into a plain dict, and every child rule below reads it as a row with fields.
    translations = (
        (payload.translations or []) if replacing_translations else await _current_children(db, entry.id, "translations")
    )
    examples = (
        (payload.examples or []) if replacing_examples else await _current_children(db, entry.id, "examples")
    )
    await _check_language(
        db, learning_language=learning_language, translations=translations, examples=examples
    )
    if "audio_asset_id" in changes:
        await _require_exists(db, MediaAsset, changes["audio_asset_id"], "media asset")
    if changes.get("tag_ids") is not None:
        for tag_id in changes["tag_ids"]:
            await _require_exists(db, Tag, tag_id, "tag")

    if word.lower() != entry.word.lower() or learning_language != entry.learning_language:
        await _reject_duplicate(db, word, learning_language, exclude_id=entry.id)

    before = {"word": entry.word, "learning_language": entry.learning_language}
    entry.word = word
    entry.learning_language = learning_language
    for field in ("definition", "ipa", "part_of_speech", "level", "notes"):
        if field in changes:
            value = changes[field]
            setattr(entry, field, value.strip() or None if isinstance(value, str) else None)
    if "synonyms" in changes:
        entry.synonyms = list(changes["synonyms"] or [])
    if "antonyms" in changes:
        entry.antonyms = list(changes["antonyms"] or [])
    if "audio_asset_id" in changes:
        entry.audio_asset_id = changes["audio_asset_id"]

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise DuplicateWord(f"The word '{word}' is already in the bank ({learning_language})") from None

    if replacing_translations:
        await _replace_translations(db, entry, translations)
    if replacing_examples:
        await _replace_examples(db, entry, examples)
    if changes.get("tag_ids") is not None:
        await _replace_tags(db, entry.id, changes["tag_ids"])

    await audit_service.record_audit(
        db,
        action="vocabulary.updated",
        actor_id=admin_id,
        target_type="vocabulary",
        target_id=entry.id,
        before=before,
        after={"fields": sorted(changes)},
    )
    return await to_read(db, entry)


async def _current_children(db: AsyncSession, entry_id: uuid.UUID, kind: str) -> list[Any]:
    """The rows a patch leaves untouched, so validation sees the real final state."""
    if kind == "translations":
        return list(
            (
                await db.execute(
                    select(VocabularyTranslation).where(VocabularyTranslation.entry_id == entry_id)
                )
            ).scalars().all()
        )
    return list(
        (await db.execute(select(VocabularyExample).where(VocabularyExample.entry_id == entry_id)))
        .scalars()
        .all()
    )


async def set_status(db: AsyncSession, entry: VocabularyEntry, raw_status: str, *, admin_id: uuid.UUID) -> dict:
    if entry.deleted_at is not None:
        raise VocabularyError("restore the entry from the trash before changing its status")
    target = status_enum(raw_status)
    before = _label_of(entry.status)
    entry.status = target
    await db.flush()
    await audit_service.record_audit(
        db,
        action="vocabulary.status.changed",
        actor_id=admin_id,
        target_type="vocabulary",
        target_id=entry.id,
        before={"status": before},
        after={"status": target.value},
    )
    return await to_read(db, entry)


async def trash(db: AsyncSession, entry: VocabularyEntry, *, admin_id: uuid.UUID) -> dict:
    """Soft delete: `deleted_at` only, so the status survives and restore is exact."""
    if entry.deleted_at is not None:
        return await to_read(db, entry)
    entry.deleted_at = security.utcnow()
    await db.flush()
    await audit_service.record_audit(
        db,
        action="vocabulary.trashed",
        actor_id=admin_id,
        target_type="vocabulary",
        target_id=entry.id,
        after={"word": entry.word, "deleted_at": entry.deleted_at.isoformat()},
    )
    return await to_read(db, entry)


async def restore(db: AsyncSession, entry: VocabularyEntry, *, admin_id: uuid.UUID) -> dict:
    if entry.deleted_at is None:
        return await to_read(db, entry)
    # The unique index covers live rows only, so a word re-added while this entry sat
    # in the trash makes this restore a constraint violation. Say so instead of
    # letting Postgres answer the teacher with a 500.
    clash = await _live_duplicate(
        db, entry.word, entry.learning_language, exclude_id=entry.id
    )
    if clash is not None:
        raise DuplicateWord(
            f"'{clash.word}' was added to the bank while this entry was in the trash - "
            "merge them before restoring",
            existing_id=clash.id,
        )
    was_deleted_at = entry.deleted_at
    entry.deleted_at = None
    await db.flush()
    await audit_service.record_audit(
        db,
        action="vocabulary.restored",
        actor_id=admin_id,
        target_type="vocabulary",
        target_id=entry.id,
        before={"deleted_at": was_deleted_at.isoformat()},
        after={"status": _label_of(entry.status)},
    )
    return await to_read(db, entry)


async def assign_taxonomy(
    db: AsyncSession, entry: VocabularyEntry, payload: v_schemas.VocabularyTaxonomyAssignment, *, admin_id: uuid.UUID
) -> dict:
    for tag_id in payload.tag_ids:
        await _require_exists(db, Tag, tag_id, "tag")
    await _replace_tags(db, entry.id, payload.tag_ids)
    await audit_service.record_audit(
        db,
        action="vocabulary.taxonomy.assigned",
        actor_id=admin_id,
        target_type="vocabulary",
        target_id=entry.id,
        after={"tags": len(payload.tag_ids)},
    )
    return await to_read(db, entry)


# --------------------------------------------------------------------------- #
# Learner view
# --------------------------------------------------------------------------- #


async def learner_view(db: AsyncSession, entry: VocabularyEntry, *, language: str | None = None) -> dict:
    """The entry as a study card. Teacher notes and provenance stay out."""
    translations = (
        await db.execute(
            select(VocabularyTranslation)
            .where(VocabularyTranslation.entry_id == entry.id)
            .order_by(VocabularyTranslation.language)
        )
    ).scalars().all()
    examples = (
        await db.execute(
            select(VocabularyExample).where(VocabularyExample.entry_id == entry.id).order_by(VocabularyExample.created_at)
        )
    ).scalars().all()
    if language:
        wanted = language.strip().lower()
        translations = [row for row in translations if row.language == wanted]
        examples = [
            row
            for row in examples
            if row.language in (wanted, entry.learning_language, None)
        ]
    return v_schemas.VocabularyLearnerRead(
        id=entry.id,
        word=entry.word,
        learning_language=entry.learning_language,
        level=entry.level,
        part_of_speech=entry.part_of_speech,
        ipa=entry.ipa,
        definition=entry.definition,
        synonyms=list(entry.synonyms or []),
        antonyms=list(entry.antonyms or []),
        translations=[
            v_schemas.TranslationRead(id=row.id, language=row.language, value=row.value) for row in translations
        ],
        examples=[
            v_schemas.ExampleRead(
                id=row.id, sentence=row.sentence, language=row.language, translation=row.translation
            )
            for row in examples
        ],
        has_audio=entry.audio_asset_id is not None,
        tags=[v_schemas.TaxonomyRef.model_validate(item) for item in await _tags_of(db, entry.id)],
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Bulk work
# --------------------------------------------------------------------------- #


async def bulk(db: AsyncSession, payload: v_schemas.VocabularyBulkRequest, *, admin_id: uuid.UUID) -> dict:
    """One action over many entries, answered per id - never a silent partial win."""
    found = (
        await db.execute(select(VocabularyEntry).where(VocabularyEntry.id.in_(payload.entry_ids)))
    ).scalars().all()
    by_id = {row.id: row for row in found}
    missing = [str(value) for value in payload.entry_ids if value not in by_id]

    target_status = None
    if payload.action == "status":
        if not payload.status:
            raise VocabularyError("the status action needs a status")
        target_status = status_enum(payload.status)
    if payload.action in ("add_tag", "remove_tag"):
        if payload.tag_id is None:
            raise VocabularyError("the tag actions need a tag_id")
        await _require_exists(db, Tag, payload.tag_id, "tag_id")
    if payload.action == "set_level" and payload.level is None:
        raise VocabularyError("set_level needs a level")

    updated: list[str] = []
    refused: list[dict] = []
    for requested in payload.entry_ids:
        entry = by_id.get(requested)
        if entry is None:
            continue
        try:
            reason = await _apply_bulk(db, entry, payload, target_status)
        except VocabularyError as exc:
            refused.append({"id": str(entry.id), "reason": str(exc)})
            continue
        if reason:
            refused.append({"id": str(entry.id), "reason": reason})
            continue
        if payload.action in ("add_tag", "remove_tag"):
            await _mutate_tag(db, entry, payload)
        updated.append(str(entry.id))

    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"vocabulary.bulk.{payload.action}",
        actor_id=admin_id,
        target_type="vocabulary",
        after={
            "requested": len(payload.entry_ids),
            "updated": len(updated),
            "refused": len(refused),
            "not_found": len(missing),
        },
    )
    return {"action": payload.action, "updated": updated, "refused": refused, "not_found": missing}


async def _apply_bulk(
    db: AsyncSession, entry: VocabularyEntry, payload: v_schemas.VocabularyBulkRequest, target_status
) -> str | None:
    """Apply one action to one entry, or return the reason this entry refuses.

    A returned reason instead of a raised error is deliberate: the unique index can
    veto a single restore in a selection of forty, and the other thirty-nine must
    still go through and be reported honestly.
    """
    action = payload.action
    if action == "status":
        if entry.deleted_at is not None:
            raise VocabularyError("entry is in the trash")
        entry.status = target_status
    elif action == "trash":
        if entry.deleted_at is not None:
            return "already in the trash"
        entry.deleted_at = security.utcnow()
    elif action == "restore":
        if entry.deleted_at is None:
            raise VocabularyError("entry is not in the trash")
        clash = await _live_duplicate(db, entry.word, entry.learning_language, exclude_id=entry.id)
        if clash is not None:
            return f"The word '{clash.word}' is already in the bank - merge them before restoring"
        entry.deleted_at = None
    elif action == "set_level":
        entry.level = (payload.level or "").strip() or None
    return None


async def _mutate_tag(db: AsyncSession, entry: VocabularyEntry, payload: v_schemas.VocabularyBulkRequest) -> None:
    existing = (
        await db.execute(
            select(vocabulary_tag).where(
                and_(
                    vocabulary_tag.c.vocabulary_entry_id == entry.id,
                    vocabulary_tag.c.tag_id == payload.tag_id,
                )
            )
        )
    ).first()
    if payload.action == "add_tag":
        if existing is None:
            await db.execute(
                vocabulary_tag.insert().values(vocabulary_entry_id=entry.id, tag_id=payload.tag_id)
            )
    elif existing is not None:
        await db.execute(
            vocabulary_tag.delete().where(
                and_(
                    vocabulary_tag.c.vocabulary_entry_id == entry.id,
                    vocabulary_tag.c.tag_id == payload.tag_id,
                )
            )
        )
    await db.flush()
