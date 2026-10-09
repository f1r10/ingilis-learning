"""Offline tests for the media library rules (Phase 5).

Everything here is a decision that does not need a database or an object store: what a
`Range` header may ask for, which bytes the library is willing to name, how an uploaded
filename is disinfected before it becomes a label, and - the group that matters most -
which fields a learner payload is allowed to contain. The last group is the leak guard
for the teacher's library: `MediaAsset` keeps the storage key, the checksum, the
original filename and the reference counts on the same row as the file a student is
allowed to play, so a projection that grows a field by accident has to fail here rather
than in a classroom.

The parts that need real rows (dedupe races, trash refusals, restore clashes, learner
authorisation) are in `tests/integration/test_media_db.py`; the parts that need MinIO
are in `tests/integration/test_storage_minio.py`.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core import constants, media_types
from app.core.exceptions import APIError, NotFound
from app.core.storage import ObjectMissing, ObjectStorage, StorageUnavailable
from app.schemas import media as m
from app.services import media_service as ms


class FakeUpload:
    """An `UploadFile` narrow enough to test the streaming loop with.

    `read(size)` hands out at most `size` bytes, which is what the real one does, so
    `_spool` cannot quietly assume it received the whole file on the first call.
    """

    def __init__(self, payload: bytes, filename: str = "lesson.mp3") -> None:
        self.payload = payload
        self.filename = filename
        self.position = 0

    async def read(self, size: int = -1) -> bytes:
        chunk = self.payload[self.position : self.position + size]
        self.position += len(chunk)
        return chunk


def tracked_tempfile():
    """A `tempfile.TemporaryFile` stand-in that remembers whether it was closed."""

    class Tracked:
        def __init__(self) -> None:
            self.data = bytearray()
            self.cursor = 0
            self.closed_ = False
            self.writes: list[int] = []

        def write(self, data: bytes) -> int:
            self.writes.append(len(data))
            self.data.extend(data)
            return len(data)

        def seek(self, offset: int) -> int:
            self.cursor = offset
            return offset

        def read(self, size: int = -1) -> bytes:
            end = len(self.data) if size < 0 else min(self.cursor + size, len(self.data))
            out = bytes(self.data[self.cursor : end])
            self.cursor = end
            return out

        def close(self) -> None:
            self.closed_ = True

    created: list[Tracked] = []

    def factory(**_kwargs):
        handle = Tracked()
        created.append(handle)
        return handle

    factory.created = created  # type: ignore[attr-defined]
    return factory


def png(tail: int = 0) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes(tail)


def mp3(tail: int = 8) -> bytes:
    return b"ID3\x04\x00\x00\x00\x00" + bytes(tail)


def ebml(doctype: bytes, *, pad: int = 240) -> bytes:
    """An EBML container whose doctype sits far behind the magic bytes.

    This is the shape a real WebM/Matroska file has: `\x1a\x45\xdf\xa3` at offset 0 and
    the doctype string only after a variable-length header. The sniffing window has to
    reach that far, so the padding is deliberately much longer than 32 bytes.
    """
    return b"\x1a\x45\xdf\xa3" + b"\x00" * pad + doctype + b"\x00" * 8


def patch_limits(monkeypatch, **ceiling_bytes: int) -> None:
    """Fix the per-kind ceilings so a size rule can be tested without a 700 MB file."""
    monkeypatch.setattr(ms, "upload_limits", lambda: dict(ceiling_bytes))


# --------------------------------------------------------------------------- #
# What a Range header may ask for
# --------------------------------------------------------------------------- #


def test_no_range_is_the_whole_file() -> None:
    piece = ms.parse_range(None, 500)
    assert (piece.start, piece.end, piece.partial, piece.length) == (0, 499, False, 500)


def test_an_explicit_range_is_partial_and_inclusive() -> None:
    piece = ms.parse_range("bytes=100-199", 500)
    assert (piece.start, piece.end, piece.partial, piece.length) == (100, 199, True, 100)


def test_an_open_ended_range_runs_to_the_last_byte() -> None:
    assert ms.parse_range("bytes=400-", 500).end == 499


def test_an_end_past_the_file_is_clamped_rather_than_refused() -> None:
    """Players routinely ask for `bytes=0-9999999999`; refusing that breaks seeking."""
    piece = ms.parse_range("bytes=0-9999999999", 500)
    assert (piece.start, piece.end) == (0, 499)


def test_a_suffix_range_counts_back_from_the_end() -> None:
    piece = ms.parse_range("bytes=-100", 500)
    assert (piece.start, piece.end, piece.length) == (400, 499, 100)


def test_a_suffix_bigger_than_the_file_starts_at_zero() -> None:
    assert ms.parse_range("bytes=-900", 500).start == 0


@pytest.mark.parametrize(
    "header",
    [
        "bytes=0-10,20-30",  # multi-range: not half-implemented, whole file instead
        "items=0-10",  # not a byte range unit
        "bytes=",  # no specification at all
        "bytes=abc-def",  # not numbers
        "bytes=-0",  # a zero-length suffix is meaningless
        "bytes=10-20x",
    ],
)
def test_an_unparseable_range_falls_back_to_the_whole_file(header: str) -> None:
    piece = ms.parse_range(header, 500)
    assert (piece.start, piece.end, piece.partial) == (0, 499, False)


@pytest.mark.parametrize("header", ["bytes=500-", "bytes=500-600", "bytes=300-100"])
def test_a_slice_that_cannot_exist_is_refused(header: str) -> None:
    """A start past the end gets a 416, not an empty 206 pretending to be a seek."""
    with pytest.raises(ms.RangeUnsatisfiable):
        ms.parse_range(header, 500)


def test_an_empty_object_ranges_to_nothing() -> None:
    """`end == -1` is the honest answer: there is no first byte to hand over."""
    assert ms.parse_range("bytes=0-10", 0).length == 0
    assert ms.parse_range(None, 0).length == 0


def test_the_storage_key_never_mentions_what_the_client_called_the_file(monkeypatch) -> None:
    """The key is generated from the sniffed format alone.

    A key built from the upload's own name is a key built from input; `build_key`
    sanitises by accident, and this is the rule that keeps it out of the design.
    """
    seen: list[tuple[str, str]] = []

    class Stub:
        @staticmethod
        def build_key(namespace: str, hint: str) -> str:
            seen.append((namespace, hint))
            return f"{namespace}/0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f.png"

    monkeypatch.setattr(ms, "get_storage", lambda: Stub())
    key = ms._key_for(media_types.identify(png()))
    assert key.startswith("media/") and key.endswith(".png")
    assert seen == [("media", "upload.png")]
    assert ".." not in key


# --------------------------------------------------------------------------- #
# Identifying an upload while it streams
# --------------------------------------------------------------------------- #


async def test_a_small_upload_is_hashed_measured_and_named(monkeypatch) -> None:
    payload = png(40) + b"\x00" * 10
    factory = tracked_tempfile()
    monkeypatch.setattr(ms.tempfile, "TemporaryFile", factory)

    spooled = await ms._spool(FakeUpload(payload))
    assert spooled.size == len(payload)
    assert spooled.checksum == hashlib.sha256(payload).hexdigest()
    assert spooled.fmt.mime == "image/png"
    assert sum(factory.created[0].writes) == len(payload)
    assert spooled.handle.read() == payload, "the file must be rewindable for storage"
    assert factory.created[0].closed_ is False, "it is still needed by the caller"


async def test_a_webm_is_only_named_if_the_window_reaches_its_doctype(monkeypatch) -> None:
    """The magic bytes are 4 long; `webm` lands about 244 bytes in.

    A caller that read 32 bytes would see EBML and refuse a file a browser can play,
    which is why the module publishes one window size instead of leaving it per call.
    The 16-byte chunk below makes that window span a dozen reads.
    """
    factory = tracked_tempfile()
    monkeypatch.setattr(ms.tempfile, "TemporaryFile", factory)
    monkeypatch.setattr(ms, "_SPOOL_CHUNK", 16)

    payload = ebml(b"webm") + mp3(0)  # the doctype is well past byte 32, the head is short
    spooled = await ms._spool(FakeUpload(payload, "clip.bin"))
    assert spooled.fmt.mime == "video/webm"
    assert spooled.fmt.extension == "webm"
    assert spooled.size == len(payload)
    assert len(factory.created[0].writes) > 1, "the upload arrived in pieces"


async def test_a_matroska_and_a_webm_are_not_the_same_file_to_a_player(monkeypatch) -> None:
    factory = tracked_tempfile()
    monkeypatch.setattr(ms.tempfile, "TemporaryFile", factory)

    mkv = await ms._spool(FakeUpload(ebml(b"matroska"), "film.mkv"))
    assert mkv.fmt.mime == "video/x-matroska"
    assert mkv.fmt.extension == "mkv"


async def test_an_ebml_file_that_names_neither_container_is_refused(monkeypatch) -> None:
    factory = tracked_tempfile()
    monkeypatch.setattr(ms.tempfile, "TemporaryFile", factory)

    with pytest.raises(ms.MediaError, match="not supported"):
        await ms._spool(FakeUpload(ebml(b"something-else")))
    assert factory.created[0].closed_, "a refused upload must not leave a temp file open"


async def test_an_empty_upload_is_refused_instead_of_becoming_a_zero_byte_asset(monkeypatch) -> None:
    factory = tracked_tempfile()
    monkeypatch.setattr(ms.tempfile, "TemporaryFile", factory)

    with pytest.raises(ms.MediaError, match="empty"):
        await ms._spool(FakeUpload(b""))
    assert factory.created[0].closed_


@pytest.mark.parametrize(
    "body,phrase",
    [
        (b"%PDF-1.7\n%\xd3\xd3\xd3\xd3", "import screen"),
        (b"\x50\x4b\x03\x04" + b"\x00" * 40, "archive"),
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "Scalable Vector Graphics"),
        (b"<!DOCTYPE html><html><body>hi</body></html>", "text or markup"),
        (b"\x7f\x45\x4c\x46\x02\x01\x01", "program"),
        (b"MZ\x90\x00", "Windows program"),
        (b"Rar!\x1a\x07\x01\x00", "RAR archive"),
    ],
)
async def test_a_mistaken_upload_gets_the_sentence_that_fits_its_mistake(monkeypatch, body, phrase) -> None:
    """A teacher who dropped a PDF into the media bank needs a direction, not "invalid"."""
    factory = tracked_tempfile()
    monkeypatch.setattr(ms.tempfile, "TemporaryFile", factory)

    with pytest.raises(ms.MediaError) as exc:
        await ms._spool(FakeUpload(body, "notes.pdf"))
    assert phrase in str(exc.value)
    assert factory.created[0].closed_


async def test_a_file_bigger_than_every_ceiling_stops_while_it_is_still_streaming(monkeypatch) -> None:
    """The pre-kind guard exists so a 2 GB body never reaches the local disk in full."""
    factory = tracked_tempfile()
    monkeypatch.setattr(ms.tempfile, "TemporaryFile", factory)
    monkeypatch.setattr(ms, "_SPOOL_CHUNK", 64)
    patch_limits(monkeypatch, image=128, audio=128, video=128)

    upload = FakeUpload(b"\x00" * 4096, "huge.bin")
    with pytest.raises(ms.MediaError, match="larger than the biggest upload"):
        await ms._spool(upload)
    assert upload.position < 256, "it stops at the ceiling instead of reading the file"
    assert factory.created[0].closed_


def test_every_refusal_needs_no_second_lookup() -> None:
    """`identify` returning None is only half an answer; the sentence is the other half."""
    for head in (b"", b"random bytes", b"\x00" * 40, b"MZ\x90\x00", ebml(b"nope")):
        if media_types.identify(head) is None:
            assert len(media_types.refusal_reason(head)) > 20, head


def test_a_truncated_header_is_refused_rather_than_guessed() -> None:
    """Three JPEG bytes are not a JPEG, and a name invented here ends up on a player."""
    assert media_types.identify(b"\xff\xd8\xff") is None
    assert media_types.identify(b"BM") is None
    assert media_types.identify(b"\xff\xd8\xff\xe0\x00\x10JFIF").mime == "image/jpeg"


# --------------------------------------------------------------------------- #
# What the library says about itself
# --------------------------------------------------------------------------- #


def test_upload_limits_are_bytes_from_the_configured_megabytes(monkeypatch) -> None:
    monkeypatch.setattr(
        ms,
        "get_settings",
        lambda: SimpleNamespace(max_image_upload_mb=15, max_audio_upload_mb=200, max_video_upload_mb=700),
    )
    assert ms.upload_limits() == {
        "image": 15 * 1024 * 1024,
        "audio": 200 * 1024 * 1024,
        "video": 700 * 1024 * 1024,
    }


async def test_the_upload_screen_is_told_the_truth_about_formats(monkeypatch) -> None:
    """`/media/meta` must be generated from the code that enforces it.

    A second hand-written list of accepted types is a list somebody has to remember to
    update, and the day it drifts the browser rejects a file the server would take.
    """
    monkeypatch.setattr(
        ms,
        "get_settings",
        lambda: SimpleNamespace(max_image_upload_mb=15, max_audio_upload_mb=200, max_video_upload_mb=700),
    )
    options = await ms.library_options(None)  # type: ignore[arg-type]

    assert [f["mime_type"] for f in options["formats"]] == [fmt.mime for fmt in media_types.accepted_formats()]
    assert all(f["kind"] in ms.KINDS for f in options["formats"])
    assert options["kinds"] == list(ms.KINDS)
    assert options["max_upload_mb"] == {"image": 15, "audio": 200, "video": 700}
    assert options["max_upload_bytes"]["audio"] == 200 * 1024 * 1024
    assert options["views"] == list(ms.VIEWS)
    assert options["sortable"] == sorted(ms.SORTABLE)
    assert options["states"] == [ms.STATE_AVAILABLE, ms.STATE_TRASHED]


def test_no_accepted_format_is_a_document_or_a_script() -> None:
    """Phase 8 imports documents; the media bank never holds one.

    SVG in particular is XML with a scripting surface, and every stored file is served
    back with its own mime type, so a storable SVG is a storable script.
    """
    mimes = {fmt.mime for fmt in media_types.accepted_formats()}
    for forbidden in (
        "image/svg+xml",
        "application/pdf",
        "text/html",
        "application/xml",
        "application/zip",
        "application/octet-stream",
        "application/x-msdownload",
    ):
        assert forbidden not in mimes
    assert all(fmt.kind in ms.KINDS for fmt in media_types.accepted_formats())


def test_a_stored_mime_with_no_label_stays_unlabelled() -> None:
    """Better an empty format column than a guessed "MP3 audio" about another file."""
    assert media_types.label_for_mime("audio/mpeg") == "MP3 audio"
    assert media_types.label_for_mime("audio/unknown-to-us") is None
    assert media_types.label_for_mime(None) is None


# --------------------------------------------------------------------------- #
# Listing: the vocabulary of the query is closed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["library", "everything", "", "TRASH"])
async def test_an_unknown_view_is_named_back_to_the_caller(bad: str) -> None:
    with pytest.raises(ms.MediaError, match="view must be one of"):
        await ms.list_assets(None, view=bad)  # type: ignore[arg-type]


async def test_a_column_the_library_does_not_offer_cannot_reach_the_order_by() -> None:
    """Sort names are looked up in a dict, never interpolated into SQL."""
    for bad in ("checksum", "storage_key", "id; DROP TABLE media_asset", "deleted_at"):
        with pytest.raises(ms.MediaError, match="sort must be one of"):
            await ms.list_assets(None, sort=bad)  # type: ignore[arg-type]


async def test_an_unknown_kind_or_order_is_refused_before_any_query_runs() -> None:
    with pytest.raises(ms.MediaError, match="kind must be one of"):
        await ms.list_assets(None, kind="document")  # type: ignore[arg-type]
    with pytest.raises(ms.MediaError, match="order"):
        await ms.list_assets(None, order="sideways")  # type: ignore[arg-type]


def test_the_sortable_columns_are_the_ones_a_library_screen_needs() -> None:
    assert set(ms.SORTABLE) == {
        "created_at", "updated_at", "original_filename", "size_bytes", "duration_seconds", "kind"
    }


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #


def test_a_patch_can_relabel_and_report_measurements_and_nothing_else() -> None:
    patch = m.MediaMetadataPatch.model_validate(
        {"label": "Class recording", "duration_seconds": 41.5, "width": 1280, "height": 720}
    )
    assert patch.label == "Class recording"
    assert patch.duration_seconds == 41.5
    assert patch.width == 1280 and patch.height == 720


@pytest.mark.parametrize(
    "body",
    [
        {"mime_type": "image/png"},
        {"kind": "audio"},
        {"storage_key": "media/whatever.png"},
        {"checksum": "0" * 64},
        {"deleted_at": None},
        {"source_origin": "import"},
    ],
)
def test_a_patch_that_offers_to_rewrite_the_file_is_rejected(body: dict) -> None:
    """`extra="forbid"`, because a 200 that changed nothing would be a lie.

    `mime_type` is derived from bytes, `storage_key` is derived from the format, and
    `deleted_at` is the trash itself. Accepting any of them in a patch body would let a
    client rename a file's type without touching the file.
    """
    with pytest.raises(ValueError):
        m.MediaMetadataPatch.model_validate(body)


def test_an_empty_patch_is_visible_as_empty() -> None:
    """Every field is optional, so the service has to see "nothing was sent"."""
    assert m.MediaMetadataPatch.model_validate({}).model_dump(exclude_unset=True) == {}


@pytest.mark.parametrize("value", [0, -1, 86_400, 1e9])
def test_a_duration_that_cannot_be_a_lesson_is_refused(value: float) -> None:
    with pytest.raises(ValueError):
        m.MediaMetadataPatch.model_validate({"duration_seconds": value})


@pytest.mark.parametrize("value", [0, -4, 30_001])
def test_a_pixel_dimension_outside_browsers_is_refused(value: int) -> None:
    with pytest.raises(ValueError):
        m.MediaMetadataPatch.model_validate({"width": value})


@pytest.mark.parametrize("value", ["", "   ", "x" * 201])
def test_a_caption_needs_text_and_has_a_grammar(value: str) -> None:
    with pytest.raises(ValueError):
        m.MediaMetadataPatch.model_validate({"label": value})


def test_a_bulk_action_is_limited_to_the_two_the_service_implements() -> None:
    for bad in ("delete_forever", "purge", "restore_all", "status"):
        with pytest.raises(ValueError):
            m.MediaBulkRequest(asset_ids=[uuid.uuid4()], action=bad)
    assert m.MediaBulkRequest(asset_ids=[uuid.uuid4()], action="trash").action == "trash"


def test_a_bulk_request_needs_a_selection_and_not_an_army() -> None:
    with pytest.raises(ValueError):
        m.MediaBulkRequest(asset_ids=[], action="trash")
    with pytest.raises(ValueError):
        m.MediaBulkRequest(asset_ids=[uuid.uuid4() for _ in range(m.MAX_BULK_ASSETS + 1)], action="trash")


def test_an_asset_id_has_to_be_an_id() -> None:
    with pytest.raises(ValueError):
        m.MediaBulkRequest(asset_ids=["../../media/1"], action="trash")


# --------------------------------------------------------------------------- #
# Leak guards on the read payloads
# --------------------------------------------------------------------------- #


def test_no_media_payload_exposes_the_object_store() -> None:
    """The storage key, the bucket and any self-authorising link stay server-side.

    A URL that carries its own credentials outlives the lesson it was made for, so this
    project serves every byte itself; the payloads can therefore never contain a key.
    """
    forbidden = {
        "storage_key", "bucket", "endpoint", "checksum", "presigned_url", "storage_url", "meta",
    }
    for model in (m.MediaRead, m.MediaSummary, m.MediaLearnerRead, m.MediaBulkRequest, m.MediaMetadataPatch):
        names = set(model.model_fields)
        assert not (names & forbidden), f"{model.__name__} exposes {names & forbidden}"


def test_a_learner_payload_is_a_player_and_nothing_more() -> None:
    private = {
        "original_filename", "label", "state", "deleted_at", "source_origin", "referenced_by",
        "reference_count", "size_bytes", "created_at", "updated_at", "format_label",
    }
    fields = set(m.MediaLearnerRead.model_fields)
    assert not (fields & private), f"learner payload exposes library fields: {fields & private}"
    assert fields == {
        "id", "kind", "mime_type", "duration_seconds", "width", "height", "content_url"
    }


def test_the_teacher_read_hands_the_editor_everything_it_can_write_back() -> None:
    readable = set(m.MediaRead.model_fields)
    writable = set(m.MediaMetadataPatch.model_fields)
    assert writable <= readable, "a patch field the screen cannot read back would be edited blind"
    assert {"content_url", "referenced_by", "state", "format_label"} <= readable


def test_a_reference_count_totals_across_the_three_shelves() -> None:
    assert m.MediaReferenceRead(words=1, listenings=2, questions=3).total == 6
    assert m.MediaReferenceRead().total == 0


def test_there_is_no_create_schema_because_bytes_are_the_only_way_in() -> None:
    """A client that could declare a kind or mime could declare a lie.

    `media_types.identify` is the sole author of both, from the upload's own bytes.
    """
    for name in ("MediaCreate", "MediaUpdate", "MediaRestoreRequest", "MediaUploadRequest"):
        assert not hasattr(m, name), name


# --------------------------------------------------------------------------- #
# Trash state, labels, and the wording of a refusal
# --------------------------------------------------------------------------- #


def _asset(**overrides):
    row = {
        "id": uuid.uuid4(),
        "kind": "audio",
        "mime_type": "audio/mpeg",
        "original_filename": "lesson.mp3",
        "size_bytes": 1024,
        "duration_seconds": None,
        "width": None,
        "height": None,
        "source_origin": "upload",
        "deleted_at": None,
        "checksum": "a" * 64,
        "storage_key": "media/abc.mp3",
        "meta": {},
        "created_at": datetime(2026, 10, 6),
        "updated_at": datetime(2026, 10, 6),
    }
    row.update(overrides)
    return SimpleNamespace(**row)


def test_the_trash_has_one_source_of_truth() -> None:
    assert ms.state_of(_asset()) == ms.STATE_AVAILABLE
    assert ms.state_of(_asset(deleted_at=datetime(2026, 10, 7))) == ms.STATE_TRASHED


def test_a_caption_is_only_a_label_when_it_is_text() -> None:
    assert ms.teacher_label(_asset(meta={"label": "Chapter 4"})) == "Chapter 4"
    assert ms.teacher_label(_asset()) is None
    assert ms.teacher_label(_asset(meta={"label": "   "})) is None
    assert ms.teacher_label(_asset(meta={"label": None})) is None
    assert ms.teacher_label(_asset(meta={"label": 12})) is None, "a number is not a caption"


def test_a_refusal_names_the_content_and_keeps_it_singular() -> None:
    message = ms._in_use_message(m.MediaReferenceRead(words=1))
    assert "1 vocabulary word" in message
    assert "words" not in message, "one word must not read as a list of them"


def test_a_refusal_plurals_and_lists_every_shelf_in_order() -> None:
    message = ms._in_use_message(m.MediaReferenceRead(words=2, listenings=1, questions=3))
    assert "2 vocabulary words and 1 listening exercise and 3 questions" in message
    assert message.endswith("the exercise would stop working.")


def test_a_refusal_tells_the_teacher_what_to_do() -> None:
    """`asset_in_use` alone would send them looking through every exercise by hand."""
    assert "Remove it from that content first" in ms._in_use_message(m.MediaReferenceRead(questions=1))


class _OneRowDb:
    """Just enough session for `require_usable`, which only ever does one `db.get`."""

    def __init__(self, asset) -> None:
        self.asset = asset

    async def get(self, _model, _asset_id):
        return self.asset


@pytest.mark.parametrize(
    "requested,held,article",
    [("audio", "image", "an"), ("image", "audio", "an"), ("video", "image", "a")],
)
async def test_a_wrong_shelf_refusal_can_be_read_aloud(requested, held, article) -> None:
    """Two of the three shelves start with a vowel, and this sentence is shown to a teacher.

    The plain f-string said "a listening item uses a audio file". The grammar is the
    whole reason `_article` exists, so the wording is pinned here rather than only in
    review.
    """
    db = _OneRowDb(_asset(kind=held))
    with pytest.raises(ms.MediaError) as exc:
        await ms.require_usable(db, uuid.uuid4(), kind=requested, subject="a listening item")
    assert f"uses {article} {requested} file" in str(exc.value)
    assert f"and this one is {held}" in str(exc.value)


async def test_a_trashed_file_is_refused_with_a_way_out() -> None:
    db = _OneRowDb(_asset(deleted_at=datetime(2026, 10, 7)))
    with pytest.raises(ms.MediaError, match="restore it"):
        await ms.require_usable(db, uuid.uuid4(), kind="audio", subject="a vocabulary entry")


# --------------------------------------------------------------------------- #
# Storage faults, and the response that carries the bytes
# --------------------------------------------------------------------------- #


def test_a_missing_object_is_a_404_and_an_unreachable_store_is_a_503() -> None:
    """Telling a teacher "this file does not exist" while MinIO is down sends them to
    re-upload a recording that is sitting there perfectly."""
    missing = ms._storage_failure(ObjectMissing("media/gone.mp3"))
    assert isinstance(missing, NotFound) and missing.http_status == 404

    down = ms._storage_failure(StorageUnavailable("connection refused"))
    assert isinstance(down, APIError) and down.http_status == 503
    assert down.code == "storage_unavailable"
    assert "Try again" in down.message


def test_the_service_errors_are_the_ones_the_endpoints_map() -> None:
    """One `_fail` mapping depends on `MediaError` being a ValueError."""
    assert issubclass(ms.MediaError, ValueError)
    asset_id = uuid.uuid4()
    clash = ms.DuplicateAsset("already here", existing_id=asset_id)
    assert clash.existing_id == asset_id
    busy = ms.AssetInUse("in use", references=m.MediaReferenceRead(questions=2))
    assert busy.references.total == 2


class FakeStream:
    """A store response body that records its own close."""

    def __init__(self, data: bytes) -> None:
        self.buffer = list(data)
        self.cursor = 0
        self.close_calls = 0

    def read(self, size: int = -1) -> bytes:
        end = len(self.buffer) if size < 0 else min(self.cursor + size, len(self.buffer))
        out = bytes(self.buffer[self.cursor : end])
        self.cursor = end
        return out

    def close(self) -> None:
        self.close_calls += 1


class FakeStorage:
    """An object store whose only job is to answer about one known payload.

    `iter_range` is the real `ObjectStorage` method: the chunking and the guaranteed
    close are behaviour worth testing, and neither needs a client.
    """

    iter_range = ObjectStorage.iter_range

    def __init__(self, payload: bytes, *, size: int | None = None, head_content_type: str = "text/plain") -> None:
        self.payload = payload
        self.size = len(payload) if size is None else size
        self.head_content_type = head_content_type
        self.opened: list[tuple[int, int]] = []
        self.streams: list[FakeStream] = []

    def head(self, key: str) -> SimpleNamespace:
        return SimpleNamespace(size=self.size, content_type=self.head_content_type)

    def open_range(self, key: str, start: int, end: int) -> SimpleNamespace:
        self.opened.append((start, end))
        stream = FakeStream(self.payload[start : end + 1])
        self.streams.append(stream)
        return SimpleNamespace(
            stream=stream,
            start=start,
            length=end - start + 1,
            total_size=self.size,
            content_type=self.head_content_type,
        )


def _patch_storage(monkeypatch, storage) -> None:
    monkeypatch.setattr(ms, "get_storage", lambda: storage)


def _request(headers: dict) -> SimpleNamespace:
    return SimpleNamespace(headers=headers)


async def _drain(response) -> bytes:
    """Read a streaming response the way the ASGI server would."""
    return b"".join([chunk async for chunk in response.body_iterator])


async def test_a_range_gets_a_206_with_the_slice_and_the_total(monkeypatch) -> None:
    payload = mp3(90)
    storage = FakeStorage(payload)
    _patch_storage(monkeypatch, storage)

    response = await ms.build_content_response(_request({"range": "bytes=5-14"}), _asset())
    assert response.status_code == 206
    assert await _drain(response) == payload[5:15]
    assert response.headers["content-range"] == f"bytes 5-14/{len(payload)}"
    assert response.headers["content-length"] == "10"
    assert response.headers["accept-ranges"] == "bytes"
    assert storage.opened == [(5, 14)]


async def test_the_served_content_type_is_the_sniffed_one_not_the_stores(monkeypatch) -> None:
    """`media_types` named this file; a stored `Content-Type` is a second opinion."""
    storage = FakeStorage(mp3(4), head_content_type="text/plain")
    _patch_storage(monkeypatch, storage)

    response = await ms.build_content_response(_request({}), _asset(mime_type="audio/mpeg"))
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"


async def test_a_full_read_is_a_200_and_carries_no_content_range(monkeypatch) -> None:
    storage = FakeStorage(mp3(4))
    _patch_storage(monkeypatch, storage)
    response = await ms.build_content_response(_request({}), _asset())
    assert response.status_code == 200
    assert "content-range" not in response.headers
    assert response.headers["content-length"] == "12"


async def test_the_etag_is_the_checksum_so_a_reupload_never_serves_a_stale_copy(monkeypatch) -> None:
    storage = FakeStorage(mp3(4))
    _patch_storage(monkeypatch, storage)
    response = await ms.build_content_response(_request({}), _asset(checksum="c" * 64))
    assert response.headers["etag"] == f'"{"c" * 64}"'


async def test_media_is_never_cacheable_by_a_shared_cache(monkeypatch) -> None:
    """A recording one class was allowed to hear must not sit in a proxy for the next."""
    storage = FakeStorage(mp3(4))
    _patch_storage(monkeypatch, storage)
    response = await ms.build_content_response(_request({}), _asset())
    assert response.headers["cache-control"].startswith("private")


async def test_an_unsatisfiable_range_answers_416_with_the_real_length(monkeypatch) -> None:
    storage = FakeStorage(mp3(4))
    _patch_storage(monkeypatch, storage)
    response = await ms.build_content_response(_request({"range": "bytes=99-120"}), _asset())
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{storage.size}"
    assert storage.opened == [], "a refused slice must not open the object"


async def test_an_empty_object_is_an_honest_empty_200(monkeypatch) -> None:
    storage = FakeStorage(b"", size=0)
    _patch_storage(monkeypatch, storage)
    response = await ms.build_content_response(_request({"range": "bytes=0-10"}), _asset())
    assert response.status_code == 200
    assert response.headers["content-length"] == "0"
    assert storage.opened == []


async def test_a_gone_object_is_a_404_rather_than_an_empty_player(monkeypatch) -> None:
    class Gone(FakeStorage):
        def head(self, key):
            raise ObjectMissing(key)

    _patch_storage(monkeypatch, Gone(mp3(4)))
    with pytest.raises(NotFound):
        await ms.build_content_response(_request({}), _asset())


async def test_a_store_refusing_the_slice_is_not_reported_as_a_missing_file(monkeypatch) -> None:
    class Refusing(FakeStorage):
        def open_range(self, key, start, end):
            raise StorageUnavailable("connection reset")

    _patch_storage(monkeypatch, Refusing(mp3(4)))
    with pytest.raises(APIError) as exc:
        await ms.build_content_response(_request({"range": "bytes=0-3"}), _asset())
    assert exc.value.http_status == 503


async def test_a_slice_is_closed_when_the_response_is_drained(monkeypatch) -> None:
    """A generator abandoned mid-file would hold a connection to MinIO open."""
    storage = FakeStorage(b"z" * 50)
    _patch_storage(monkeypatch, storage)
    response = await ms.build_content_response(_request({"range": "bytes=10-19"}), _asset())
    assert await _drain(response) == b"z" * 10
    assert storage.streams[0].close_calls == 1


async def test_a_learner_view_is_a_url_and_no_library_metadata() -> None:
    asset = _asset(original_filename="staff-room-recording.mp3", meta={"label": "Friday lesson"})
    payload = await ms.learner_view(asset)
    assert payload["content_url"] == f"/student/media/{asset.id}/content"
    assert "staff-room" not in str(payload) and "Friday" not in str(payload)
    assert set(payload) == set(m.MediaLearnerRead.model_fields)

    # The teacher's preview runs this same projection, so the only thing that may change
    # with the audience is the door the bytes come through - and nothing else joins it.
    previewed = await ms.learner_view(asset, served_to_admin=True)
    assert previewed["content_url"] == f"/media/{asset.id}/content"
    assert {k: v for k, v in previewed.items() if k != "content_url"} == {
        k: v for k, v in payload.items() if k != "content_url"
    }


# --------------------------------------------------------------------------- #
# Where a file is read from
# --------------------------------------------------------------------------- #


def test_a_media_url_is_a_path_under_the_api_rather_than_a_link_to_the_store() -> None:
    asset_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    teacher = constants.media_content_url(asset_id, for_student=False)
    student = constants.media_content_url(asset_id, for_student=True)

    assert teacher == f"/media/{asset_id}/content"
    assert student == f"/student/media/{asset_id}/content"
    assert teacher != student, "the two audiences authorise differently"
    for url in (teacher, student):
        assert not url.startswith(("http:", "https:", "//")), url
        assert "X-Amz" not in url and "signature" not in url and "minio" not in url


def test_the_two_audiences_have_two_distinct_content_paths() -> None:
    assert constants.TEACHER_MEDIA_CONTENT_PATH == "/media"
    assert constants.STUDENT_MEDIA_CONTENT_PATH == "/student/media"
