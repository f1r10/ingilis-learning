"""The media library (Phase 5): one file stored once, referenced by many lessons.

Four decisions shape this module, and each exists because the alternative was a
teacher-facing lie of some kind.

* **The bytes decide what the file is.** A browser's `Content-Type`, the filename and
  the extension are all chosen by whoever is uploading. Every upload is read before it
  is stored (`app.core.media_types`), and anything this module cannot name is refused
  with a sentence rather than filed as `application/octet-stream`. The object store is
  private and every byte goes back out through this application, so a mislabelled file
  is not a display bug - it is whatever the next person's browser makes of it.
* **Identical bytes are one asset.** The sha256 is computed while the upload is
  spooling, checked against the live rows before anything is written, and - for the two
  requests that arrive together - settled by the partial unique index from migration
  `0003`. The second teacher gets the existing asset, not an error, because "that
  recording is already in your library" is information, not a failure.
* **A file that content points at cannot be thrown away.** Trash is refused while a
  live word, listening or question references the asset, and the refusal says how many
  and of what kind. The object stays in storage either way: trashing removes a file
  from the library, not from the disk.
* **Learners read files through this application, one request at a time.** No presigned
  URL and no public object is ever handed to a browser, and a learner may only read an
  asset that some *ready* piece of learner-facing content points at
  (`served_to_student`).

Duration and pixel dimensions are never computed here. This backend has no codec
library, and a number invented by a half-parser would end up on a learner's timer, so
both stay null until a player reports what it measured (`update_metadata`).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core import constants, enums, media_types, security
from app.core.config import get_settings
from app.core.exceptions import APIError, NotFound
from app.core.storage import ObjectMissing, StorageUnavailable, get_storage
from app.models.content import Listening, MediaAsset, Question, VocabularyEntry
from app.schemas import media as m_schemas
from app.services import audit_service

logger = logging.getLogger(__name__)

#: The three shelves the library files assets on. `kind` comes from the sniffed mime, so
#: this is a closed set rather than something a client may extend.
KINDS = ("image", "audio", "video")

#: `MediaAsset` has no lifecycle column: a file is in the library or in the trash, and
#: `deleted_at` is the whole truth. These are the two words the UI shows for it.
STATE_AVAILABLE = "available"
STATE_TRASHED = "trashed"

SORTABLE = {
    "created_at": MediaAsset.created_at,
    "updated_at": MediaAsset.updated_at,
    "original_filename": MediaAsset.original_filename,
    "size_bytes": MediaAsset.size_bytes,
    "duration_seconds": MediaAsset.duration_seconds,
    "kind": MediaAsset.kind,
}

VIEWS = ("bank", "trash", "all")

#: The fields a browser may report a measurement for, and nothing else.
MEASURABLE = ("duration_seconds", "width", "height")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: How much of an upload is kept in memory to identify it; the rest goes to disk.
_SPOOL_CHUNK = 1 << 20


class MediaError(ValueError):
    """A teacher-facing problem with an upload or a request; the endpoints 422 it."""


class DuplicateAsset(Exception):
    """These bytes are already in the library (409 when it blocks a restore)."""

    def __init__(self, message: str, *, existing_id: uuid.UUID | None = None) -> None:
        super().__init__(message)
        self.existing_id = existing_id


class AssetInUse(Exception):
    """Live content still points at this file, so it cannot be trashed (409)."""

    def __init__(self, message: str, *, references: m_schemas.MediaReferenceRead) -> None:
        super().__init__(message)
        self.references = references


@dataclass(frozen=True)
class _Spooled:
    """One upload, on local disk, with its digest and identified format in hand."""

    handle: Any
    checksum: str
    size: int
    fmt: media_types.MediaFormat


# --------------------------------------------------------------------------- #
# What the library accepts, and what it says about itself
# --------------------------------------------------------------------------- #


def upload_limits() -> dict[str, int]:
    """The per-kind ceilings, in bytes, exactly as configured."""
    settings = get_settings()
    return {
        "image": settings.max_image_upload_mb * 1024 * 1024,
        "audio": settings.max_audio_upload_mb * 1024 * 1024,
        "video": settings.max_video_upload_mb * 1024 * 1024,
    }


async def library_options(db: AsyncSession) -> dict:
    """Everything the upload screen needs, taken from the code that enforces it.

    The accepted formats come from `media_types` and the ceilings from the settings, so
    what the browser promises cannot drift away from what an upload will actually be
    accepted as.
    """
    del db  # the library's rules are code and configuration, not rows
    limits = upload_limits()
    return {
        "formats": [
            {
                "mime_type": fmt.mime,
                "kind": fmt.kind,
                "extension": fmt.extension,
                "label": fmt.label,
            }
            for fmt in media_types.accepted_formats()
        ],
        "kinds": list(KINDS),
        "max_upload_bytes": limits,
        "max_upload_mb": {kind: value // (1024 * 1024) for kind, value in limits.items()},
        "views": list(VIEWS),
        "sortable": sorted(SORTABLE),
        "states": [STATE_AVAILABLE, STATE_TRASHED],
    }


def _display_name(raw: str | None) -> str | None:
    """The upload's own filename, kept only as a label for the teacher.

    It never reaches a path: separators, control characters and an over-long name are
    gone before the row is written, and the storage key is generated from the sniffed
    format instead (see `_key_for`).
    """
    if not raw:
        return None
    basename = os.path.basename(raw.replace("\\", "/"))
    cleaned = _CONTROL_CHARS.sub(" ", basename).strip()
    return cleaned[:500] or None


def _key_for(fmt: media_types.MediaFormat) -> str:
    """An opaque key whose only hint is the extension the bytes were identified as.

    The uploaded name is deliberately not consulted: it is the one field a client
    controls completely, and a key built from it is a key built from input.
    """
    return get_storage().build_key("media", f"upload.{fmt.extension}")


# --------------------------------------------------------------------------- #
# Upload
# --------------------------------------------------------------------------- #


async def _spool(upload: Any) -> _Spooled:
    """Stream an upload to a temporary file, hashing and identifying it as it arrives.

    Nothing is held whole in memory: a 600 MB classroom recording becomes a file on
    local disk plus a running sha256. The temporary file is re-readable, which is what
    lets the same bytes go to the object store afterwards without asking the client
    again.
    """
    handle = tempfile.TemporaryFile(prefix="llp-media-")
    digest = hashlib.sha256()
    head = bytearray()
    total = 0
    # The largest ceiling of the three kinds: the file's real kind is only known once
    # its bytes have been read, but a request already past every ceiling can stop here.
    ceiling = max(upload_limits().values())
    try:
        while True:
            chunk = await upload.read(_SPOOL_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > ceiling:
                raise MediaError(
                    "That file is larger than the biggest upload this platform accepts "
                    f"({ceiling // (1024 * 1024)} MB)."
                )
            digest.update(chunk)
            remaining = media_types.HEAD_BYTES - len(head)
            if remaining > 0:
                head.extend(chunk[:remaining])
            await run_in_threadpool(handle.write, chunk)
        if total == 0:
            raise MediaError("That file is empty.")
        fmt = media_types.identify(bytes(head))
        if fmt is None:
            raise MediaError(media_types.refusal_reason(bytes(head)))
        await run_in_threadpool(handle.seek, 0)
        return _Spooled(handle=handle, checksum=digest.hexdigest(), size=total, fmt=fmt)
    except Exception:
        # Whatever stopped the upload, a 200 MB temporary file must not stay behind.
        await run_in_threadpool(handle.close)
        raise


async def _live_by_checksum(
    db: AsyncSession, checksum: str | None, *, exclude_id: uuid.UUID | None = None
) -> MediaAsset | None:
    """The live asset holding these exact bytes, if there is one."""
    if not checksum:
        return None
    stmt = select(MediaAsset).where(
        MediaAsset.checksum == checksum, MediaAsset.deleted_at.is_(None)
    )
    if exclude_id is not None:
        stmt = stmt.where(MediaAsset.id != exclude_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_from_upload(
    db: AsyncSession,
    upload: Any,
    *,
    label: str | None = None,
    admin_id: uuid.UUID,
) -> tuple[dict, bool]:
    """Store one upload, or point at the asset that already holds its bytes.

    Returns the asset payload and whether the library already had it. A duplicate is
    not an error: the teacher gets the row they wanted and the screen can say the file
    was already there, while the object store keeps holding the recording once.
    """
    spooled = await _spool(upload)
    try:
        limits = upload_limits()
        if spooled.size > limits[spooled.fmt.kind]:
            raise MediaError(
                f"A {spooled.fmt.label} may be at most "
                f"{limits[spooled.fmt.kind] // (1024 * 1024)} MB; this file is "
                f"{spooled.size // (1024 * 1024)} MB."
            )

        existing = await _live_by_checksum(db, spooled.checksum)
        if existing is not None:
            return await to_read(db, existing), True

        key = _key_for(spooled.fmt)
        try:
            await run_in_threadpool(
                get_storage().put_file, key, spooled.handle, spooled.fmt.mime, spooled.size
            )
        except (ObjectMissing, StorageUnavailable) as exc:
            raise _storage_failure(exc) from exc

        asset = MediaAsset(
            kind=spooled.fmt.kind,
            storage_key=key,
            original_filename=_display_name(getattr(upload, "filename", None)),
            mime_type=spooled.fmt.mime,
            size_bytes=spooled.size,
            checksum=spooled.checksum,
            source_origin="upload",
            meta={"label": label.strip()} if label and label.strip() else {},
        )
        db.add(asset)
        try:
            await db.flush()
        except IntegrityError:
            # The partial unique index settled a race: another request stored the same
            # bytes a moment ago. Ours is the orphan - remove it and hand back the row
            # that won, instead of failing a teacher who did nothing wrong.
            await db.rollback()
            try:
                await run_in_threadpool(get_storage().delete, key)
            except (ObjectMissing, StorageUnavailable) as exc:
                # An object we could not delete is a storage fault, not a reason to
                # fail the request: the asset the teacher asked for exists either way.
                logger.warning("orphan upload %s could not be removed: %s", key, exc)
            winner = await _live_by_checksum(db, spooled.checksum)
            if winner is None:
                # The index rejected us but no live row has that checksum: this is not
                # a duplicate race and must not be reported as one.
                raise
            return await to_read(db, winner), True

        await audit_service.record_audit(
            db,
            action="media.uploaded",
            actor_id=admin_id,
            target_type="media_asset",
            target_id=asset.id,
            after={
                "kind": asset.kind,
                "mime_type": asset.mime_type,
                "size_bytes": asset.size_bytes,
                "filename": asset.original_filename,
            },
        )
        return await to_read(db, asset), False
    finally:
        await run_in_threadpool(spooled.handle.close)


def _storage_failure(exc: Exception) -> APIError:
    """The one translation from a storage fault to what a teacher is told.

    A missing object and an unreachable store are different problems: the first means
    this library row is broken and needs a re-upload, the second means the same request
    can be tried again. Neither may surface as a bare 500, and the detail goes to the
    log rather than the browser.
    """
    logger.warning("media storage failure: %s", exc)
    if isinstance(exc, ObjectMissing):
        return NotFound("The stored file for this asset is missing from storage.")
    return APIError(
        "storage_unavailable",
        "The file storage is not reachable right now. Try again in a moment.",
        503,
    )


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #

#: The tables that can point at an asset, each with the word the teacher sees for it.
#: Both the trash refusal and the learner authorisation read this list, so a new kind
#: of reference is one tuple - it cannot be counted in one place and forgotten in the
#: other.
_REFERENCE_SOURCES: tuple[tuple[str, str, Any, Any], ...] = (
    ("words", "vocabulary word", VocabularyEntry, VocabularyEntry.audio_asset_id),
    ("listenings", "listening exercise", Listening, Listening.media_asset_id),
    ("questions", "question", Question, Question.media_asset_id),
)


async def _bulk_references(
    db: AsyncSession, asset_ids: list[uuid.UUID]
) -> dict[uuid.UUID, m_schemas.MediaReferenceRead]:
    """How much live content each asset is used by, in three grouped queries.

    A reference from the trash does not count: content the teacher has already thrown
    away must not hold a file in the library forever.
    """
    ids = list(dict.fromkeys(asset_ids))
    counts = {asset_id: {"words": 0, "listenings": 0, "questions": 0} for asset_id in ids}
    if not ids:
        return {asset_id: m_schemas.MediaReferenceRead() for asset_id in ids}
    for field, _noun, table, column in _REFERENCE_SOURCES:
        rows = (
            await db.execute(
                select(column, func.count())
                .where(column.in_(ids), table.deleted_at.is_(None))
                .group_by(column)
            )
        ).all()
        for asset_id, count in rows:
            if asset_id in counts:
                counts[asset_id][field] = int(count)
    return {asset_id: m_schemas.MediaReferenceRead(**values) for asset_id, values in counts.items()}


async def references(db: AsyncSession, asset_id: uuid.UUID) -> m_schemas.MediaReferenceRead:
    """How much live content this one file is used by."""
    return (await _bulk_references(db, [asset_id]))[asset_id]


def _in_use_message(refs: m_schemas.MediaReferenceRead) -> str:
    """Name the content that is holding on, in words a teacher can act on."""
    parts = []
    for field, noun, _table, _column in _REFERENCE_SOURCES:
        value = getattr(refs, field)
        if value:
            parts.append(f"{value} {noun}" + ("" if value == 1 else _plural(noun)))
    return (
        "This file is still used by "
        + " and ".join(parts)
        + ". Remove it from that content first, or the exercise would stop working."
    )


def _plural(noun: str) -> str:
    return "s" if not noun.endswith("s") else ""


# --------------------------------------------------------------------------- #
# Read payloads
# --------------------------------------------------------------------------- #


def state_of(asset: MediaAsset) -> str:
    return STATE_TRASHED if asset.deleted_at is not None else STATE_AVAILABLE


def teacher_label(asset: MediaAsset) -> str | None:
    value = (asset.meta or {}).get("label")
    return value if isinstance(value, str) and value.strip() else None


def _summary(asset: MediaAsset, reference_count: int) -> dict:
    """Rows go through the schema, so a list item cannot diverge from the detail."""
    return m_schemas.MediaSummary(
        id=asset.id,
        kind=asset.kind,
        mime_type=asset.mime_type,
        format_label=media_types.label_for_mime(asset.mime_type),
        original_filename=asset.original_filename,
        label=teacher_label(asset),
        size_bytes=asset.size_bytes,
        duration_seconds=asset.duration_seconds,
        width=asset.width,
        height=asset.height,
        source_origin=asset.source_origin,
        state=state_of(asset),
        content_url=constants.media_content_url(asset.id, for_student=False),
        reference_count=reference_count,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
        deleted_at=asset.deleted_at,
    ).model_dump(mode="json")


async def to_read(db: AsyncSession, asset: MediaAsset) -> dict:
    refs = await references(db, asset.id)
    return m_schemas.MediaRead(
        id=asset.id,
        kind=asset.kind,
        mime_type=asset.mime_type,
        format_label=media_types.label_for_mime(asset.mime_type),
        original_filename=asset.original_filename,
        label=teacher_label(asset),
        size_bytes=asset.size_bytes,
        duration_seconds=asset.duration_seconds,
        width=asset.width,
        height=asset.height,
        source_origin=asset.source_origin,
        state=state_of(asset),
        content_url=constants.media_content_url(asset.id, for_student=False),
        referenced_by=refs,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
        deleted_at=asset.deleted_at,
    ).model_dump(mode="json")


async def learner_view(asset: MediaAsset, *, served_to_admin: bool = False) -> dict:
    """The learner's side of an asset: what to play, and nothing about the library.

    No filename, no checksum, no reference count and no teacher caption - those belong
    to the catalogue the teacher maintains. `content_url` is the student path, so the
    file a learner plays is authorised by the learner's own session on every request.

    A teacher's preview runs the same projection so that what they inspect is what the
    class gets. Only the path differs: a browser holding an admin session cannot fetch a
    student path, and asking it to would show the teacher an empty player over a file
    they own. `served_to_admin` says which session is on the other end.
    """
    return m_schemas.MediaLearnerRead(
        id=asset.id,
        kind=asset.kind,
        mime_type=asset.mime_type,
        duration_seconds=asset.duration_seconds,
        width=asset.width,
        height=asset.height,
        content_url=constants.media_content_url(asset.id, for_student=not served_to_admin),
    ).model_dump(mode="json")


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


async def get(db: AsyncSession, asset_id: uuid.UUID, *, allow_trash: bool = True) -> MediaAsset | None:
    asset = await db.get(MediaAsset, asset_id)
    if asset is None:
        return None
    if asset.deleted_at is not None and not allow_trash:
        return None
    return asset


def _article(word: str) -> str:
    """`a` or `an`, because the sentence this feeds is read by a teacher.

    "uses a audio file" is what the plain f-string produced for two of the three kinds.
    """
    return "an" if word[:1].lower() in "aeiou" else "a"


async def require_usable(
    db: AsyncSession, asset_id: uuid.UUID, *, kind: str | None = None, subject: str
) -> MediaAsset:
    """The file some content is about to point at must be in the library, and be playable.

    This is the guard that keeps a lesson honest rather than a foreign key that keeps it
    valid. A trashed file passes the foreign key and then fails in front of a class:
    `served_to_student` will not open a trashed asset, so the recording never plays.
    A file of the wrong shelf - a photograph where a listening expected audio - is the
    same failure, and neither is something a teacher should discover by pressing play.

    `subject` is the consumer's own words ("a listening item"), so the refusal names the
    screen the teacher is standing in.
    """
    asset = await db.get(MediaAsset, asset_id)
    if asset is None:
        raise MediaError("no file with that id is in the library - upload it from the media screen first")
    if asset.deleted_at is not None:
        raise MediaError(
            f"that file is in the trash - restore it, or choose another one for {subject}"
        )
    if kind is not None and asset.kind != kind:
        raise MediaError(
            f"{subject} uses {_article(kind)} {kind} file, and this one is "
            f"{asset.kind} ({asset.original_filename})"
        )
    return asset


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #


async def list_assets(
    db: AsyncSession,
    *,
    q: str | None = None,
    kind: str | None = None,
    source_origin: str | None = None,
    view: str = "bank",
    sort: str = "created_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """`view=bank` hides the trash, `view=trash` shows only it, `view=all` shows both."""
    if view not in VIEWS:
        raise MediaError("view must be one of: " + ", ".join(VIEWS))
    if kind is not None and kind not in KINDS:
        raise MediaError("kind must be one of: " + ", ".join(KINDS))
    if sort not in SORTABLE:
        raise MediaError("sort must be one of: " + ", ".join(sorted(SORTABLE)))
    if order not in ("asc", "desc"):
        raise MediaError("order must be 'asc' or 'desc'")
    page = max(1, page)
    page_size = min(max(1, page_size), 200)

    stmt = select(MediaAsset)
    if view == "bank":
        stmt = stmt.where(MediaAsset.deleted_at.is_(None))
    elif view == "trash":
        stmt = stmt.where(MediaAsset.deleted_at.is_not(None))
    if kind:
        stmt = stmt.where(MediaAsset.kind == kind)
    if source_origin:
        stmt = stmt.where(MediaAsset.source_origin == source_origin)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                MediaAsset.original_filename.ilike(like),
                MediaAsset.mime_type.ilike(like),
                # The teacher's own caption lives in `meta`, and is the text they are
                # most likely to be looking for.
                MediaAsset.meta["label"].astext.ilike(like),
            )
        )

    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    column = SORTABLE[sort]
    direction = column.desc() if order == "desc" else column.asc()
    rows = (
        (
            await db.execute(
                # `id` breaks ties, so page 2 can neither repeat nor skip a row.
                stmt.order_by(direction.nulls_last(), MediaAsset.id.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )

    counts = await _bulk_references(db, [row.id for row in rows])
    return {
        "items": [_summary(row, counts[row.id].total) for row in rows],
        "total": int(total),
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #


async def update_metadata(
    db: AsyncSession,
    asset: MediaAsset,
    payload: m_schemas.MediaMetadataPatch,
    *,
    admin_id: uuid.UUID,
) -> dict:
    """Apply the patch: a caption, and whatever the browser's player just measured.

    `duration_seconds`, `width` and `height` are only ever written from here. A player
    that has not loaded the file reports nothing, so an asset can sit in the library
    with an unknown length, and the library shows "not measured" rather than a number
    nobody took.
    """
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise MediaError("nothing to change")
    if asset.deleted_at is not None:
        raise MediaError("restore the asset from the trash before editing it")

    before: dict[str, Any] = {}
    after: dict[str, Any] = {}

    if "label" in changes:
        label = (changes["label"] or "").strip() or None
        meta = dict(asset.meta or {})
        before["label"] = meta.get("label")
        if label is None:
            meta.pop("label", None)
        else:
            meta["label"] = label
        asset.meta = meta
        after["label"] = label
    for field in MEASURABLE:
        if field in changes:
            before[field] = getattr(asset, field)
            setattr(asset, field, changes[field])
            after[field] = changes[field]

    await db.flush()
    await audit_service.record_audit(
        db,
        action="media.updated",
        actor_id=admin_id,
        target_type="media_asset",
        target_id=asset.id,
        before=before,
        after=after,
    )
    return await to_read(db, asset)


async def trash(db: AsyncSession, asset: MediaAsset, *, admin_id: uuid.UUID) -> dict:
    """Soft delete. The object stays in storage; only the library lets go of the file."""
    if asset.deleted_at is not None:
        return await to_read(db, asset)
    refs = await references(db, asset.id)
    if refs.total:
        raise AssetInUse(_in_use_message(refs), references=refs)
    asset.deleted_at = security.utcnow()
    await db.flush()
    await audit_service.record_audit(
        db,
        action="media.trashed",
        actor_id=admin_id,
        target_type="media_asset",
        target_id=asset.id,
        after={"filename": asset.original_filename, "kind": asset.kind},
    )
    return await to_read(db, asset)


async def restore(db: AsyncSession, asset: MediaAsset, *, admin_id: uuid.UUID) -> dict:
    if asset.deleted_at is None:
        return await to_read(db, asset)
    # The unique index covers live rows only, so the same bytes added while this asset
    # sat in the trash would make the restore a constraint violation. Say so, instead
    # of letting Postgres answer the teacher with a 500.
    clash = await _live_by_checksum(db, asset.checksum, exclude_id=asset.id)
    if clash is not None:
        raise DuplicateAsset(
            "The same file is already in the library under another entry - remove that "
            "one before restoring this",
            existing_id=clash.id,
        )
    was_deleted_at = asset.deleted_at
    asset.deleted_at = None
    await db.flush()
    await audit_service.record_audit(
        db,
        action="media.restored",
        actor_id=admin_id,
        target_type="media_asset",
        target_id=asset.id,
        before={"deleted_at": was_deleted_at.isoformat()},
        after={"filename": asset.original_filename, "kind": asset.kind},
    )
    return await to_read(db, asset)


async def bulk(
    db: AsyncSession, payload: m_schemas.MediaBulkRequest, *, admin_id: uuid.UUID
) -> dict:
    """One action over many assets, answered per id - never a silent partial win.

    A referenced file is refused while its unreferenced neighbours in the same
    selection are trashed: one refusal message per asset is the only honest way to
    report a bulk action that partly could not be done.
    """
    rows = (
        await db.execute(select(MediaAsset).where(MediaAsset.id.in_(payload.asset_ids)))
    ).scalars().all()
    by_id = {row.id: row for row in rows}
    missing = [str(value) for value in payload.asset_ids if value not in by_id]
    counts = await _bulk_references(db, list(by_id))

    updated: list[str] = []
    refused: list[dict] = []
    for requested in payload.asset_ids:
        asset = by_id.get(requested)
        if asset is None:
            continue
        if payload.action == "trash":
            if asset.deleted_at is not None:
                refused.append({"id": str(asset.id), "reason": "already in the trash"})
                continue
            refs = counts[asset.id]
            if refs.total:
                refused.append({"id": str(asset.id), "reason": _in_use_message(refs)})
                continue
            asset.deleted_at = security.utcnow()
        else:
            if asset.deleted_at is None:
                refused.append({"id": str(asset.id), "reason": "not in the trash"})
                continue
            clash = await _live_by_checksum(db, asset.checksum, exclude_id=asset.id)
            if clash is not None:
                refused.append(
                    {
                        "id": str(asset.id),
                        "reason": "the same file is already in the library - remove that one first",
                    }
                )
                continue
            asset.deleted_at = None
        updated.append(str(asset.id))

    await db.flush()
    await audit_service.record_audit(
        db,
        action=f"media.bulk.{payload.action}",
        actor_id=admin_id,
        target_type="media_asset",
        after={
            "requested": len(payload.asset_ids),
            "updated": len(updated),
            "refused": len(refused),
            "not_found": len(missing),
        },
    )
    return {"action": payload.action, "updated": updated, "refused": refused, "not_found": missing}


# --------------------------------------------------------------------------- #
# Serving bytes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ByteRequest:
    """The slice a client asked for, resolved against the object's real length."""

    start: int
    end: int
    partial: bool

    @property
    def length(self) -> int:
        return self.end - self.start + 1


class RangeUnsatisfiable(Exception):
    """The requested slice lies outside the file: the answer is 416, not a partial 200."""


def parse_range(header: str | None, total: int) -> ByteRequest:
    """Resolve one `Range` header against a known object length.

    Only a single `bytes=` range is honoured, which is all a media player asks for; a
    multi-range request is answered with the whole file rather than half-implemented.
    An end past the file is clamped (players routinely ask for `bytes=0-9999999999`); a
    start past the file is refused, because inventing an empty success there is how a
    broken seek stays invisible.
    """
    if not header or total <= 0:
        # No range asked for, or nothing to range over: the whole file, which for an
        # empty object is honestly zero bytes rather than a first byte that is not there.
        return ByteRequest(start=0, end=max(total - 1, -1), partial=False)
    if not header.startswith("bytes="):
        return ByteRequest(start=0, end=total - 1, partial=False)
    spec = header[len("bytes=") :].strip()
    if "," in spec or "-" not in spec:
        return ByteRequest(start=0, end=total - 1, partial=False)
    left, _, right = spec.partition("-")
    try:
        if not left:
            count = int(right)
            if count <= 0:
                raise ValueError
            start, end = max(total - count, 0), total - 1
        else:
            start = int(left)
            end = int(right) if right else total - 1
    except ValueError:
        return ByteRequest(start=0, end=total - 1, partial=False)
    if start >= total or start > end:
        raise RangeUnsatisfiable(f"bytes {start}-{end} of {total}")
    return ByteRequest(start=start, end=min(end, total - 1), partial=True)


def _file_headers(asset: MediaAsset, mime: str) -> dict[str, str]:
    """Headers that describe the file rather than the slice.

    `private` because a shared cache must not hold a recording one class was allowed to
    hear, and an ETag tied to the checksum because re-uploaded bytes are a different
    asset - an old copy must never answer for a new one.
    """
    headers = {
        "Content-Type": mime,
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0, must-revalidate",
    }
    if asset.checksum:
        headers["ETag"] = f'"{asset.checksum}"'
    return headers


async def served_to_student(db: AsyncSession, asset_id: uuid.UUID) -> bool:
    """May a learner read this file? Only if ready content points at it.

    Three tables answer that, and they are the three a learner can actually be shown: a
    ready listening's own recording, a ready question's picture, a ready word's audio.
    A draft, an archived or a trashed referrer is not permission - otherwise an
    unfinished exercise would leak its recording before the class is meant to hear it.

    What a learner *assigned* to an exam may open is narrowed further in Phase 7. This
    is the floor, not the ceiling, and it is not "any logged-in student".
    """
    for _field, _noun, table, column in _REFERENCE_SOURCES:
        allowed = await db.execute(
            select(table.id)
            .where(
                column == asset_id,
                table.deleted_at.is_(None),
                table.status == enums.ContentStatus.READY,
            )
            .limit(1)
        )
        if allowed.first() is not None:
            return True
    return False


async def build_content_response(request: Request, asset: MediaAsset) -> Response:
    """Stream an asset's bytes, honouring one range, for whichever audience was allowed.

    One function serves both routers on purpose: the response is the same file either
    way, and who may ask is decided by the caller before this runs. The application
    proxies the slice instead of handing out a link to the object store, so access is
    checked on every request rather than baked into a URL that outlives the lesson.
    """
    storage = get_storage()
    mime = asset.mime_type or "application/octet-stream"
    try:
        head = await run_in_threadpool(storage.head, asset.storage_key)
    except (ObjectMissing, StorageUnavailable) as exc:
        raise _storage_failure(exc) from exc

    total = head.size
    if total == 0:
        # Nothing to send, and no slice of it either: an empty object is reported as an
        # empty 200 rather than a 416 or a fabricated length.
        return Response(status_code=200, headers={**_file_headers(asset, mime), "Content-Length": "0"})

    try:
        piece = parse_range(request.headers.get("range"), total)
    except RangeUnsatisfiable:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
        )

    try:
        opened = await run_in_threadpool(
            storage.open_range, asset.storage_key, piece.start, piece.end
        )
    except (ObjectMissing, StorageUnavailable) as exc:
        raise _storage_failure(exc) from exc

    headers = _file_headers(asset, mime)
    headers["Content-Length"] = str(piece.length)
    if piece.partial:
        headers["Content-Range"] = f"bytes {piece.start}-{piece.end}/{total}"
    return StreamingResponse(
        storage.iter_range(opened),
        status_code=206 if piece.partial else 200,
        headers=headers,
    )
