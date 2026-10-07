"""Content domain: topics, tags, source files, import pipeline, media library,
the flexible question engine (+ immutable versioning), vocabulary bank, reading
and listening first-class entities.

Design note (QUESTION ENGINE / versioning): question-type specific structure is
kept in JSONB (`Question.config`), while cross-cutting attributes are normalized
columns. New question types are added by registering a type descriptor + a JSON
config shape - no database redesign required. Published attempts always read the
frozen `QuestionVersion.config` snapshot, never the live editable question.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import enums
from app.core.database import Base
from app.models.base import SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #
class Topic(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Hierarchical, teacher-defined topics (Grammar -> Tenses -> Past -> ...).

    A question may belong to zero, one or many topics.
    """

    __tablename__ = "topic"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("topic.id", ondelete="CASCADE"), index=True
    )
    level_path: Mapped[str] = mapped_column(String(700), default="", nullable=False)  # materialised path
    language: Mapped[str | None] = mapped_column(String(16))

    parent: Mapped["Topic | None"] = relationship(remote_side="Topic.id", back_populates="children")
    children: Mapped[list["Topic"]] = relationship(back_populates="parent", cascade="all")

    __table_args__ = (Index("ix_topic_parent_name", "parent_id", "name"),)


class Tag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Optional free-form tag."""

    __tablename__ = "tag"

    name: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    color: Mapped[str | None] = mapped_column(String(32))


# --------------------------------------------------------------------------- #
# Source provenance & import pipeline
# --------------------------------------------------------------------------- #
class SourceFile(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A document/source collection. A source document is NOT a question.

    Provenance metadata (filename, page, sheet, method) is preserved on extracted
    content even if the physical file is deleted."""

    __tablename__ = "source_file"

    title: Mapped[str] = mapped_column(String(400), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(500))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    storage_key: Mapped[str | None] = mapped_column(String(500))  # null when original not kept
    keep_original: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    language: Mapped[str | None] = mapped_column(String(16))
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)


class ImportJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_job"

    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[enums.JobStatus] = mapped_column(
        SAEnum(enums.JobStatus, name="job_status", native_enum=False),
        default=enums.JobStatus.QUEUED,
        nullable=False,
    )
    profile: Mapped[str | None] = mapped_column(String(120))
    auto_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    progress: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list["ImportItem"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class ImportItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One extracted candidate result awaiting teacher review/approval."""

    __tablename__ = "import_item"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_job.id", ondelete="CASCADE"), index=True, nullable=False
    )
    detected_kind: Mapped[str | None] = mapped_column(String(64))  # question/vocabulary/reading/...
    detected_type: Mapped[str | None] = mapped_column(String(64))
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_sheet: Mapped[str | None] = mapped_column(String(200))
    crop_storage_key: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float | None] = mapped_column(Float)
    decision: Mapped[enums.ImportItemDecision] = mapped_column(
        SAEnum(enums.ImportItemDecision, name="import_decision", native_enum=False),
        default=enums.ImportItemDecision.PENDING,
        nullable=False,
    )
    extracted: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    corrected: Mapped[dict | None] = mapped_column(JSONB)
    result_ref_type: Mapped[str | None] = mapped_column(String(64))
    result_ref_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))

    job: Mapped[ImportJob] = relationship(back_populates="items")


# --------------------------------------------------------------------------- #
# Media library
# --------------------------------------------------------------------------- #
class MediaAsset(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Reusable image/audio/video. Physical file stored once; referenced many.

    `checksum` is the sha256 of the bytes as they were uploaded. The partial unique
    index below is what makes "stored once" true even when a teacher uploads the same
    recording twice in the same second; the service check in front of it is only there
    to give the second request the existing asset instead of an error.
    """

    __tablename__ = "media_asset"

    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # image|audio|video
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(500))
    mime_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    checksum: Mapped[str | None] = mapped_column(String(128))
    source_origin: Mapped[str | None] = mapped_column(String(32))  # upload|youtube|extracted
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    __table_args__ = (
        # Live rows only: a trashed asset must keep holding its file, and the same
        # bytes may legitimately be re-added after it was removed from the library.
        Index(
            "uq_media_asset_checksum",
            "checksum",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


# --------------------------------------------------------------------------- #
# Question engine
# --------------------------------------------------------------------------- #
class Question(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    """A flexible, typed question. The live, editable version.

    Context dependency is explicit (reading_/listening_ ids + context_kind) so
    dependent questions can never silently become detached random questions.
    """

    __tablename__ = "question"

    type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text)  # question text / instructions
    status: Mapped[enums.ContentStatus] = mapped_column(
        SAEnum(enums.ContentStatus, name="content_status", native_enum=False),
        default=enums.ContentStatus.READY,
        nullable=False,
    )
    context_kind: Mapped[enums.QuestionContext] = mapped_column(
        SAEnum(enums.QuestionContext, name="question_context", native_enum=False),
        default=enums.QuestionContext.INDEPENDENT,
        nullable=False,
    )
    reading_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("reading.id", ondelete="CASCADE"), index=True
    )
    listening_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("listening.id", ondelete="CASCADE"), index=True
    )

    # scoring
    score: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    partial_scoring: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    negative_scoring: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    # classification (all optional / teacher-controlled)
    level: Mapped[str | None] = mapped_column(String(32), index=True)  # e.g. CEFR A2/B1
    difficulty: Mapped[int | None] = mapped_column(Integer)
    learning_language: Mapped[str | None] = mapped_column(String(16), index=True)

    # provenance
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL"), index=True
    )
    source_page: Mapped[int | None] = mapped_column(Integer)
    source_sheet: Mapped[str | None] = mapped_column(String(200))
    extraction_method: Mapped[str | None] = mapped_column(String(64))
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_job.id", ondelete="SET NULL")
    )

    explanation: Mapped[str | None] = mapped_column(Text)
    teacher_notes: Mapped[str | None] = mapped_column(Text)
    media_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_asset.id", ondelete="SET NULL")
    )
    # type-specific structured payload (options, blanks, pairs, accepted answers,
    # normalization config ...). Validated against a per-type JSON shape.
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    topics: Mapped[list["QuestionTopic"]] = relationship(back_populates="question", cascade="all, delete-orphan")
    tags: Mapped[list["QuestionTag"]] = relationship(back_populates="question", cascade="all, delete-orphan")
    versions: Mapped[list["QuestionVersion"]] = relationship(
        back_populates="question", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_question_type_status", "type", "status"),
        Index("ix_question_level_lang", "level", "learning_language"),
        # A question's picture is looked up from the media side twice per request: once
        # to decide whether a file may be trashed, once to decide whether a learner may
        # play it. Neither is a scan of the question bank. (migration 0004)
        Index("ix_question_media_asset_id", "media_asset_id"),
    )


class QuestionVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable snapshot created on every meaningful edit. Attempts reference this."""

    __tablename__ = "question_version"

    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), index=True, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)  # full frozen question + config
    change_note: Mapped[str | None] = mapped_column(Text)

    question: Mapped[Question] = relationship(back_populates="versions")

    __table_args__ = (Index("ix_question_version_qv", "question_id", "version", unique=True),)


class QuestionTopic(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "question_topic"

    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), index=True, nullable=False
    )
    topic_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("topic.id", ondelete="CASCADE"), index=True, nullable=False
    )

    question: Mapped[Question] = relationship(back_populates="topics")
    topic: Mapped[Topic] = relationship()


class QuestionTag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "question_tag"

    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tag.id", ondelete="CASCADE"), index=True, nullable=False
    )

    question: Mapped[Question] = relationship(back_populates="tags")
    tag: Mapped[Tag] = relationship()


# --------------------------------------------------------------------------- #
# Vocabulary bank
# --------------------------------------------------------------------------- #
class VocabularyEntry(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "vocabulary_entry"

    word: Mapped[str] = mapped_column(String(300), index=True, nullable=False)
    learning_language: Mapped[str | None] = mapped_column(String(16), index=True)
    definition: Mapped[str | None] = mapped_column(Text)
    ipa: Mapped[str | None] = mapped_column(String(200))
    part_of_speech: Mapped[str | None] = mapped_column(String(64))
    level: Mapped[str | None] = mapped_column(String(32))
    audio_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_asset.id", ondelete="SET NULL")
    )
    synonyms: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    antonyms: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL")
    )
    status: Mapped[enums.ContentStatus] = mapped_column(
        SAEnum(enums.ContentStatus, name="content_status", native_enum=False),
        default=enums.ContentStatus.READY,
        nullable=False,
    )

    translations: Mapped[list["VocabularyTranslation"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    examples: Mapped[list["VocabularyExample"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    tags: Mapped[list["Tag"]] = relationship(secondary="vocabulary_tag")

    __table_args__ = (
        # One live entry per word per learning language (migration 0002). A teacher
        # editing by hand and the Phase 8 importer can reach the same word at the
        # same moment, and only the database can see the other one.
        Index(
            "uq_vocabulary_word_language",
            "word",
            "learning_language",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        # Migration 0004: a word's pronunciation is looked up from the media library
        # twice over - the trash refusal and a learner's audio button.
        Index("ix_vocabulary_entry_audio_asset_id", "audio_asset_id"),
    )


class VocabularyTranslation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Retains values for MULTIPLE target languages (AZ/RU/TR...)."""

    __tablename__ = "vocabulary_translation"

    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vocabulary_entry.id", ondelete="CASCADE"), index=True, nullable=False
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    entry: Mapped[VocabularyEntry] = relationship(back_populates="translations")

    __table_args__ = (Index("ix_vocab_translation_entry_lang", "entry_id", "language"),)


class VocabularyExample(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "vocabulary_example"

    entry_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("vocabulary_entry.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sentence: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16))
    translation: Mapped[str | None] = mapped_column(Text)

    entry: Mapped[VocabularyEntry] = relationship(back_populates="examples")


# vocabulary <-> tag secondary table
vocabulary_tag = Table(
    "vocabulary_tag",
    Base.metadata,
    Column("vocabulary_entry_id", ForeignKey("vocabulary_entry.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True),
)


# --------------------------------------------------------------------------- #
# Reading (first-class entity)
# --------------------------------------------------------------------------- #
class Reading(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "reading"

    title: Mapped[str] = mapped_column(String(400), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(16), index=True)
    level: Mapped[str | None] = mapped_column(String(32))
    word_count: Mapped[int | None] = mapped_column(Integer)
    layout: Mapped[str] = mapped_column(String(32), default="above", nullable=False)  # above|split|tabbed
    status: Mapped[enums.ContentStatus] = mapped_column(
        SAEnum(enums.ContentStatus, name="content_status", native_enum=False),
        default=enums.ContentStatus.READY,
        nullable=False,
    )
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL")
    )
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    question_sets: Mapped[list["ReadingQuestionSet"]] = relationship(
        back_populates="reading", cascade="all, delete-orphan"
    )


class ReadingQuestionSet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A grouped, ordered set of reading-dependent questions.

    Which questions belong to the set is recorded in `ReadingSetQuestion`, not on the
    question itself: filing a question under a heading is not a content edit and must
    never append a `QuestionVersion`."""

    __tablename__ = "reading_question_set"

    reading_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reading.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(300))
    instructions: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    reading: Mapped[Reading] = relationship(back_populates="question_sets")


class ReadingSetQuestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One question filed in one reading question set, at one position.

    Two rules make the grouping safe without touching `Question`:

    * `question_id` is unique across the table, so a question cannot appear under two
      headings of the same passage (or in two passages) at once.
    * Both foreign keys cascade on delete, and deleting a *set* therefore removes the
      membership rows while leaving the questions bound to the passage - a set is
      deleted far more often than its questions are.
    """

    __tablename__ = "reading_set_question"

    set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reading_question_set.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("uq_reading_set_question_question", "question_id", unique=True),
        Index("ix_reading_set_question_set", "set_id"),
    )


# --------------------------------------------------------------------------- #
# Listening (first-class entity)
# --------------------------------------------------------------------------- #
class Listening(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "listening"

    title: Mapped[str] = mapped_column(String(400), nullable=False)
    media_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("media_asset.id", ondelete="SET NULL")
    )
    language: Mapped[str | None] = mapped_column(String(16), index=True)
    level: Mapped[str | None] = mapped_column(String(32))
    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_source: Mapped[str | None] = mapped_column(String(32))  # manual|imported|auto|absent
    transcript_timestamps: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # playback rules
    replay_limit: Mapped[int | None] = mapped_column(Integer)
    allow_pause: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    allow_seek: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    show_transcript: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[enums.ContentStatus] = mapped_column(
        SAEnum(enums.ContentStatus, name="content_status", native_enum=False),
        default=enums.ContentStatus.READY,
        nullable=False,
    )
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_file.id", ondelete="SET NULL")
    )
    meta: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    question_sets: Mapped[list["ListeningQuestionSet"]] = relationship(
        back_populates="listening", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # Migration 0004. The recording of a listening item is the file a whole class
        # plays, so two checks run against this column on ordinary requests: whether a
        # ready listening lets a learner read the asset, and whether live content still
        # needs it before the library can trash it.
        Index("ix_listening_media_asset_id", "media_asset_id"),
    )


class ListeningQuestionSet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One block of questions under a listening item, optionally a slice of the audio.

    Membership is recorded in `ListeningSetQuestion`, for the same reason as reading:
    grouping is not a content edit."""

    __tablename__ = "listening_question_set"

    listening_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listening.id", ondelete="CASCADE"), index=True, nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(300))
    instructions: Mapped[str | None] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # question-specific audio interval
    start_seconds: Mapped[float | None] = mapped_column(Float)
    end_seconds: Mapped[float | None] = mapped_column(Float)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    listening: Mapped[Listening] = relationship(back_populates="question_sets")


class ListeningSetQuestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One question filed in one listening question set, at one position.

    Mirrors `ReadingSetQuestion`: unique `question_id` (a question answers one block of
    one recording), and a deleted set takes the memberships with it, not the questions."""

    __tablename__ = "listening_set_question"

    set_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("listening_question_set.id", ondelete="CASCADE"), nullable=False
    )
    question_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("question.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    __table_args__ = (
        Index("uq_listening_set_question_question", "question_id", unique=True),
        Index("ix_listening_set_question_set", "set_id"),
    )
