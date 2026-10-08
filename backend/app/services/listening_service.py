"""Listening items (Phase 5): a recording, its transcript, and the sets filed under it.

`passage_service` owns what a listening shares with a reading - the sets, the membership
rules, the lifecycle words, the list gates. What is listening-only lives here:

* **The audio is the library's, not this row's.** A listening names a `MediaAsset`; the
  bytes, the checksum and the storage key stay in the media module, and the payload hands
  out one server-written path to read them from.
* **`transcript_source` is derived, never accepted.** This endpoint can only know that a
  teacher typed the words here or that there are none. `imported` belongs to the Phase 8
  importer and `auto` to the Phase 12 speech adapter, because provenance a client asserts
  is a claim, not a record.
* **Nothing that cannot be heard is published.** Moving a listening to `ready` without a
  live recording *and* without a transcript is refused - on its own or inside a bulk
  action - because an exercise with nothing to play is what a class finds out about.
* **Clearing the words clears the cue list.** Timestamps describe a transcript that is no
  longer there, and a player that highlights lines of an absent text is a broken screen.

The playback rules are stored as the teacher set them and served as delivered: they are
the conditions of the exercise, and a player that decided them itself would quietly
rewrite the lesson.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import constants, enums
from app.models.content import Listening, MediaAsset, Question
from app.schemas import listening as l_schemas
from app.schemas import passage as p_schemas
from app.services import audit_service, media_service, passage_service, question_service
from app.services.passage_service import LISTENING, PassageError

KIND = LISTENING

SORTABLE = {
    "created_at": Listening.created_at,
    "updated_at": Listening.updated_at,
    "title": Listening.title,
    "level": Listening.level,
    "status": Listening.status,
}

_LEARNER_STATUS = enums.ContentStatus.READY

#: What a teacher is told to do when they try to publish an empty exercise.
_NOTHING_TO_HEAR = (
    "a recording needs audio or a transcript before it can be published - add one, "
    "or leave it as a draft"
)


def _label(value: Any) -> str:
    return value.value if hasattr(value, "value") else str(value)


def transcript_source_for(transcript: str | None) -> str:
    """Where these words came from, as far as this endpoint can honestly tell."""
    return (
        enums.TranscriptSource.MANUAL.value
        if (transcript or "").strip()
        else enums.TranscriptSource.ABSENT.value
    )


def _source(row: Listening) -> str:
    """The provenance of the stored transcript.

    The column is nullable because the importer and the speech adapter write it too, so a
    row that arrived without one is described by the only evidence there is: whether the
    words are there.
    """
    return row.transcript_source or transcript_source_for(row.transcript)


# --------------------------------------------------------------------------- #
# Rows and payloads
# --------------------------------------------------------------------------- #


async def get(db: AsyncSession, listening_id: uuid.UUID, *, allow_trash: bool = True) -> Listening | None:
    row = await db.get(Listening, listening_id)
    if row is None or (row.deleted_at is not None and not allow_trash):
        return None
    return row


async def _audio(db: AsyncSession, asset_id: uuid.UUID | None) -> dict | None:
    """The file behind a listening, as the teacher's screen shows it.

    A trashed asset is still described rather than hidden: the listening is on the
    teacher's desk, the file is in the library's trash, and "restore it or attach
    another" is the only useful sentence for that.
    """
    if asset_id is None:
        return None
    asset = await db.get(MediaAsset, asset_id)
    if asset is None:
        return None
    return p_schemas.AudioRead(
        id=asset.id,
        kind=asset.kind,
        mime_type=asset.mime_type,
        label=media_service.teacher_label(asset),
        duration_seconds=asset.duration_seconds,
        state=media_service.state_of(asset),
        content_url=constants.media_content_url(asset.id, for_student=False),
    ).model_dump(mode="json")


def _summary(row: Listening, set_count: int, question_count: int, audio: dict | None) -> dict:
    return l_schemas.ListeningSummary(
        id=row.id,
        title=row.title,
        language=row.language,
        level=row.level,
        status=_label(row.status),
        has_audio=row.media_asset_id is not None,
        audio_state=audio["state"] if audio else None,
        duration_seconds=audio["duration_seconds"] if audio else None,
        has_transcript=bool((row.transcript or "").strip()),
        transcript_source=_source(row),
        show_transcript=row.show_transcript,
        set_count=set_count,
        question_count=question_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    ).model_dump(mode="json")


def _learner_summary(row: Listening, audio: dict | None, question_count: int) -> dict:
    return l_schemas.ListeningLearnerSummary(
        id=row.id,
        title=row.title,
        language=row.language,
        level=row.level,
        duration_seconds=audio["duration_seconds"] if audio else None,
        has_audio=audio is not None,
        replay_limit=row.replay_limit,
        show_transcript=row.show_transcript,
        question_count=question_count,
    ).model_dump(mode="json")


async def to_read(db: AsyncSession, row: Listening) -> dict:
    """The editor payload: playback rules, the file, the transcript, and the sets."""
    sets = await passage_service.list_sets(db, KIND, row.id)
    unfiled = await passage_service.unfiled_questions(db, KIND, row.id)
    set_counts, question_counts = await passage_service.passage_counts(db, KIND, [row.id])
    return l_schemas.ListeningRead(
        id=row.id,
        title=row.title,
        language=row.language,
        level=row.level,
        status=_label(row.status),
        audio=await _audio(db, row.media_asset_id),
        transcript=row.transcript,
        transcript_source=_source(row),
        transcript_timestamps=list(row.transcript_timestamps or []),
        replay_limit=row.replay_limit,
        allow_pause=row.allow_pause,
        allow_seek=row.allow_seek,
        show_transcript=row.show_transcript,
        source_file_id=row.source_file_id,
        set_count=set_counts.get(row.id, 0),
        question_count=question_counts.get(row.id, 0),
        sets=sets,
        unfiled=unfiled,
        created_at=row.created_at,
        updated_at=row.updated_at,
        deleted_at=row.deleted_at,
    ).model_dump(mode="json")


async def meta(db: AsyncSession) -> dict:
    """What the recording screen builds its pickers from, from the code that enforces it."""
    options = passage_service.lifecycle_options(SORTABLE)
    return {
        "learning_languages": await passage_service.enabled_languages(db),
        "levels": constants.CEFR_LEVELS,
        "statuses": options["statuses"],
        "views": options["views"],
        "sortable": options["sortable"],
        "transcript_sources": [s.value for s in enums.TranscriptSource],
        "max_replay_limit": p_schemas.MAX_REPLAY_LIMIT,
        "max_body_characters": p_schemas.MAX_BODY_CHARACTERS,
        "max_sets": p_schemas.MAX_SETS_PER_PASSAGE,
        "max_questions_per_set": p_schemas.MAX_QUESTIONS_PER_SET,
    }


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


async def _check_audio(db: AsyncSession, asset_id: uuid.UUID | None) -> None:
    """The file a listening points at must be a live audio asset."""
    if asset_id is None:
        return
    try:
        await media_service.require_usable(db, asset_id, kind="audio", subject="a listening item")
    except media_service.MediaError as exc:
        raise PassageError(str(exc)) from None


def _check_cues(rows: list) -> list:
    """The cue list, refused in the service's own words.

    `check_timestamps` is a schema rule and raises `ValueError`, which a request body
    would answer with a 422 on its own - but this is called on a patch too, where the same
    sentence has to reach the browser as the teacher-facing error the endpoints map.
    """
    try:
        return p_schemas.check_timestamps(rows)
    except ValueError as exc:
        raise PassageError(str(exc)) from None


def cue_lines(transcript: str | None, rows: list | None) -> list:
    """The cue list a transcript can actually support.

    A highlight list describes words that have to be there to be highlighted: cue lines
    saved against an absent transcript draw a player pointing at nothing, on the way in as
    much as on the way out.
    """
    if not (transcript or "").strip():
        return []
    return list(rows or [])


async def refuse_publish(db: AsyncSession, row: Listening) -> None:
    """Raise if this recording has nothing a class could listen to.

    Used both as the status endpoint's veto (raising, so one click reports one sentence)
    and as the bulk action's (which turns the raise into a per-row refusal).
    """
    if not (row.transcript or "").strip() and row.media_asset_id is None:
        raise PassageError(_NOTHING_TO_HEAR)
    if row.media_asset_id is not None:
        asset = await db.get(MediaAsset, row.media_asset_id)
        if asset is None or asset.deleted_at is not None:
            raise PassageError(
                "the recording attached to this item is in the trash - restore it, "
                "attach another, or publish with the transcript only"
            )


async def _refuse_publish_reason(db: AsyncSession, row: Listening) -> str | None:
    try:
        await refuse_publish(db, row)
    except PassageError as exc:
        return str(exc)
    return None


def _audit_field(field: str, value: Any) -> tuple[str, Any]:
    """The audit row's version of a field.

    A transcript is recorded by how long it is and a cue list by how many lines it holds:
    an editor history that copied both would be a second library nobody maintains, and the
    immutable per-question version rows are where content change is actually tracked. A
    file id becomes whether one is attached, because an id tells whoever reads the history
    nothing they can act on.
    """
    if field == "transcript":
        return "transcript_characters", len(value or "")
    if field == "transcript_timestamps":
        return "cue_lines", len(value or [])
    if field == "media_asset_id":
        return "has_audio", value is not None
    return field, value


async def create(db: AsyncSession, payload: l_schemas.ListeningCreate, *, admin_id: uuid.UUID) -> dict:
    await passage_service.check_language(db, payload.language)
    await _check_audio(db, payload.media_asset_id)
    cues = _check_cues(payload.transcript_timestamps)
    status = passage_service.status_enum(payload.status)
    row = Listening(
        title=payload.title,
        media_asset_id=payload.media_asset_id,
        language=payload.language,
        level=payload.level,
        transcript=payload.transcript,
        transcript_source=transcript_source_for(payload.transcript),
        transcript_timestamps=cue_lines(payload.transcript, cues),
        replay_limit=payload.replay_limit,
        allow_pause=payload.allow_pause,
        allow_seek=payload.allow_seek,
        show_transcript=payload.show_transcript,
        status=status,
    )
    if status == enums.ContentStatus.READY:
        await refuse_publish(db, row)
    db.add(row)
    await db.flush()
    await audit_service.record_audit(
        db,
        action="listening.created",
        actor_id=admin_id,
        target_type="listening",
        target_id=row.id,
        after={
            "title": row.title,
            "status": _label(row.status),
            "has_audio": row.media_asset_id is not None,
            "transcript_characters": len(row.transcript or ""),
            "cue_lines": len(row.transcript_timestamps or []),
        },
    )
    return await to_read(db, row)


async def update(
    db: AsyncSession, row: Listening, payload: l_schemas.ListeningUpdate, *, admin_id: uuid.UUID
) -> dict:
    """Apply a patch. Absent fields are untouched.

    Two fields are the server's rather than the client's. `transcript_source` follows the
    words this request sent: an importer's or a speech adapter's provenance survives an
    edit that did not touch the text, because that text still came from there. The cue list
    follows the transcript that can carry it, so a patch that empties the words cannot leave
    a player highlighting lines of a text that is gone.
    """
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise PassageError("nothing to change")
    await passage_service.check_language(db, changes.get("language"))
    if "media_asset_id" in changes:
        await _check_audio(db, changes["media_asset_id"])
    if "transcript_timestamps" in changes:
        _check_cues(changes["transcript_timestamps"] or [])

    if "title" in changes:
        title = (changes["title"] or "").strip()
        if not title:
            raise PassageError("a recording needs a title")
        changes["title"] = title
    if "transcript" in changes:
        transcript = (changes["transcript"] or "").strip() or None
        changes["transcript"] = transcript
        changes["transcript_source"] = transcript_source_for(transcript)
        changes["transcript_timestamps"] = cue_lines(
            transcript, changes.get("transcript_timestamps", row.transcript_timestamps)
        )

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for field, value in changes.items():
        previous = getattr(row, field)
        if previous == value:
            continue
        setattr(row, field, value)
        key, was = _audit_field(field, previous)
        _, now = _audit_field(field, value)
        before[key] = was
        after[key] = now

    if row.status == enums.ContentStatus.READY and (
        "media_asset_id" in changes or "transcript" in changes
    ):
        # A live exercise cannot be edited into silence. The publish rule is checked on the
        # way back in, so the class that can hear it today still can tomorrow.
        await refuse_publish(db, row)

    await db.flush()
    await audit_service.record_audit(
        db,
        action="listening.updated",
        actor_id=admin_id,
        target_type="listening",
        target_id=row.id,
        before=before,
        after=after,
    )
    return await to_read(db, row)


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


async def set_status(db: AsyncSession, row: Listening, raw_status: str, *, admin_id: uuid.UUID) -> dict:
    await passage_service.change_status(
        db, KIND, row, raw_status, admin_id=admin_id, refuse_publish=_refuse_publish_hook
    )
    return await to_read(db, row)


async def _refuse_publish_hook(db: AsyncSession, row: Listening) -> None:
    """The status endpoint's veto: raise, and the endpoint turns it into a 422 sentence."""
    reason = await _refuse_publish_reason(db, row)
    if reason:
        raise PassageError(reason)


async def trash(db: AsyncSession, row: Listening, *, admin_id: uuid.UUID) -> dict:
    await passage_service.trash_passage(db, KIND, row, admin_id=admin_id)
    return await to_read(db, row)


async def restore(db: AsyncSession, row: Listening, *, admin_id: uuid.UUID) -> dict:
    await passage_service.restore_passage(db, KIND, row, admin_id=admin_id)
    return await to_read(db, row)


async def bulk(db: AsyncSession, payload: p_schemas.PassageBulkRequest, *, admin_id: uuid.UUID) -> dict:
    return await passage_service.bulk_passages(
        db, KIND, payload, admin_id=admin_id, refuse_publish=_refuse_publish_reason
    )


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


def _filters(
    stmt,
    *,
    q: str | None,
    language: str | None,
    level: str | None,
    status: str | None,
    has_audio: bool | None,
    show_transcript: bool | None,
):
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(Listening.title.ilike(like), Listening.transcript.ilike(like)))
    if language:
        stmt = stmt.where(Listening.language == language.strip().lower())
    if level:
        stmt = stmt.where(Listening.level == level)
    if status:
        stmt = stmt.where(Listening.status == passage_service.status_enum(status))
    if has_audio is not None:
        stmt = stmt.where(
            Listening.media_asset_id.is_not(None) if has_audio else Listening.media_asset_id.is_(None)
        )
    if show_transcript is not None:
        stmt = stmt.where(Listening.show_transcript.is_(show_transcript))
    return stmt


async def _audios(db: AsyncSession, rows: list[Listening], *, include_trashed: bool = True) -> dict[uuid.UUID, dict]:
    """One query for the page's recordings, so fifty rows do not become fifty reads.

    The teacher's list describes a trashed file rather than hiding it, because the
    listening is on their desk and still names it. A learner's list has no such story to
    tell: an unplayable file is not reported, and `has_audio` says so.
    """
    ids = [row.media_asset_id for row in rows if row.media_asset_id is not None]
    if not ids:
        return {}
    stmt = select(MediaAsset).where(MediaAsset.id.in_(ids))
    if not include_trashed:
        stmt = stmt.where(MediaAsset.deleted_at.is_(None))
    assets = (await db.execute(stmt)).scalars().all()
    built: dict[uuid.UUID, dict] = {}
    for asset in assets:
        built[asset.id] = {
            "state": media_service.state_of(asset),
            "duration_seconds": asset.duration_seconds,
        }
    return built


async def list_listenings(
    db: AsyncSession,
    *,
    q: str | None = None,
    language: str | None = None,
    level: str | None = None,
    status: str | None = None,
    has_audio: bool | None = None,
    show_transcript: bool | None = None,
    view: str = "bank",
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    passage_service.check_list_args(view=view, sort=sort, order=order, sortable=SORTABLE)
    stmt = passage_service.trash_view(KIND, select(Listening), view=view)
    stmt = _filters(
        stmt,
        q=q,
        language=language,
        level=level,
        status=status,
        has_audio=has_audio,
        show_transcript=show_transcript,
    )
    rows, total = await passage_service.paginate(
        db, stmt, model=Listening, sort=sort, sortable=SORTABLE, order=order, page=page, page_size=page_size
    )
    set_counts, question_counts = await passage_service.passage_counts(db, KIND, [row.id for row in rows])
    audios = await _audios(db, rows)
    return {
        "items": [
            _summary(
                row,
                set_counts.get(row.id, 0),
                question_counts.get(row.id, 0),
                audios.get(row.media_asset_id),
            )
            for row in rows
        ],
        "total": total,
        "page": max(1, page),
        "page_size": min(max(1, page_size), 200),
    }


async def list_for_learner(
    db: AsyncSession,
    *,
    q: str | None = None,
    language: str | None = None,
    level: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Ready, non-trashed recordings, with only the answerable questions counted."""
    if sort not in SORTABLE:
        raise PassageError("sort must be one of: " + ", ".join(sorted(SORTABLE)))
    if order not in ("asc", "desc"):
        raise PassageError("order must be 'asc' or 'desc'")
    stmt = _filters(
        select(Listening).where(Listening.deleted_at.is_(None), Listening.status == _LEARNER_STATUS),
        q=q,
        language=language,
        level=level,
        status=None,
        has_audio=None,
        show_transcript=None,
    )
    rows, total = await passage_service.paginate(
        db, stmt, model=Listening, sort=sort, sortable=SORTABLE, order=order, page=page, page_size=page_size
    )
    _, question_counts = await passage_service.passage_counts(
        db, KIND, [row.id for row in rows], ready_only=True
    )
    audios = await _audios(db, rows, include_trashed=False)
    return {
        "items": [
            _learner_summary(row, audios.get(row.media_asset_id), question_counts.get(row.id, 0))
            for row in rows
        ],
        "total": total,
        "page": max(1, page),
        "page_size": min(max(1, page_size), 100),
    }


# --------------------------------------------------------------------------- #
# The learner's listening
# --------------------------------------------------------------------------- #


async def _question_payloads(
    db: AsyncSession, ids: list[uuid.UUID], *, served_to_admin: bool = False
) -> dict[str, dict]:
    """Each question as an exercise, without the recording repeated inside it."""
    if not ids:
        return {}
    rows = (await db.execute(select(Question).where(Question.id.in_(ids)))).scalars().all()
    return {
        str(row.id): await question_service.student_view(
            db, row, include_context=False, served_to_admin=served_to_admin
        )
        for row in rows
    }


async def learner_detail(
    db: AsyncSession,
    row: Listening,
    *,
    served_to_admin: bool = False,
    only_set_ids: list[uuid.UUID] | None = None,
) -> dict:
    """The player, the words if the teacher allowed them, and the answerable questions."""

    async def project(session, ids):
        return await _question_payloads(session, ids, served_to_admin=served_to_admin)

    sets, unfiled = await passage_service.learner_tree(
        db, KIND, row.id, project=project, only_set_ids=only_set_ids
    )
    audio = None
    duration = None
    if row.media_asset_id is not None:
        asset = await media_service.get(db, row.media_asset_id, allow_trash=False)
        if asset is not None:
            audio = await media_service.learner_view(asset, served_to_admin=served_to_admin)
            duration = asset.duration_seconds
    return l_schemas.ListeningLearnerRead(
        id=row.id,
        title=row.title,
        language=row.language,
        level=row.level,
        audio=audio,
        duration_seconds=duration,
        replay_limit=row.replay_limit,
        allow_pause=row.allow_pause,
        allow_seek=row.allow_seek,
        show_transcript=row.show_transcript,
        transcript=row.transcript if row.show_transcript else None,
        transcript_timestamps=list(row.transcript_timestamps or []) if row.show_transcript else [],
        sets=sets,
        unfiled=unfiled,
    ).model_dump(mode="json")
