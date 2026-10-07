"""Offline tests for the reading and listening rules (Phase 5).

These cover the decisions that need no database: which words a lifecycle endpoint may
accept, what a derived column is derived *from*, which fields a request body is not
allowed to touch, and which fields a learner payload is not allowed to contain. The last
group is the leak guard - a passage row holds a text, its provenance and its file id in
the same columns as the exercise, so a projection that grows a field by accident has to
fail here rather than in front of a class.

The database half (membership moves, filters, per-row bulk refusals, learner visibility
of a whole tree) is in `tests/integration/test_reading_db.py` and
`tests/integration/test_listening_db.py`.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core import enums
from app.schemas import listening as l
from app.schemas import passage as p
from app.schemas import reading as r
from app.services import listening_service as ls
from app.services import passage_service as ps
from app.services import reading_service as rs

ANY_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


# --------------------------------------------------------------------------- #
# The two passage types are one mechanism, described twice
# --------------------------------------------------------------------------- #


def test_each_passage_type_names_its_own_tables_and_keys() -> None:
    """The descriptor is the only place the two types differ, so it is worth pinning.

    `passage_service` joins, counts and files through these names. A descriptor that
    pointed a reading at the listening membership table would still run - and would file
    a text's questions under someone else's sets.
    """
    assert ps.READING.passage is rs.Reading
    assert ps.LISTENING.passage is ls.Listening
    assert (ps.READING.passage_fk, ps.READING.question_fk) == ("reading_id", "reading_id")
    assert (ps.LISTENING.passage_fk, ps.LISTENING.question_fk) == ("listening_id", "listening_id")
    assert ps.READING.context is enums.QuestionContext.READING
    assert ps.LISTENING.context is enums.QuestionContext.LISTENING
    assert ps.KINDS == {"reading": ps.READING, "listening": ps.LISTENING}


def test_only_a_listening_block_can_be_a_slice_of_a_file() -> None:
    """`has_interval` is what makes the two set screens different, in one flag."""
    assert ps.LISTENING.has_interval is True
    assert ps.READING.has_interval is False
    assert "start_seconds" in ps.LISTENING.set_schema.model_fields
    assert "start_seconds" not in ps.READING.set_schema.model_fields


def test_an_unknown_passage_type_is_refused_with_the_names_that_exist() -> None:
    with pytest.raises(ps.PassageError, match="listening, reading"):
        ps.kind_of("speaking")


# --------------------------------------------------------------------------- #
# Lifecycle words, shared by both passage screens
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("raw", ["draft", "ready", "archived"])
def test_a_teacher_may_set_exactly_the_three_lifecycle_states(raw: str) -> None:
    assert ps.status_enum(raw) == enums.ContentStatus(raw)


def test_trash_is_a_deletion_and_never_a_status() -> None:
    """One source of truth for the trash: `deleted_at`.

    A row that was both `ready` and `trash` would leave every screen guessing which one a
    learner is served.
    """
    with pytest.raises(ps.PassageError, match="deletion, not a status"):
        ps.status_enum("trash")


def test_an_unknown_status_is_refused_with_the_list_the_teacher_needs() -> None:
    with pytest.raises(ps.PassageError) as exc:
        ps.status_enum("published")
    assert "draft" in str(exc.value) and "archived" in str(exc.value)


def test_only_a_live_text_may_be_published() -> None:
    """`is_learner_visible` is the whole learner gate, in one line, for both types."""
    ready = SimpleNamespace(status=enums.ContentStatus.READY, deleted_at=None)
    draft = SimpleNamespace(status=enums.ContentStatus.DRAFT, deleted_at=None)
    archived = SimpleNamespace(status=enums.ContentStatus.ARCHIVED, deleted_at=None)
    trashed = SimpleNamespace(status=enums.ContentStatus.READY, deleted_at="yesterday")
    assert ps.is_learner_visible(ready)
    assert not any(ps.is_learner_visible(item) for item in (draft, archived, trashed))


@pytest.mark.parametrize("view", ["bank", "trash", "all"])
def test_the_three_list_views_are_the_only_words_a_list_accepts(view: str) -> None:
    ps.check_list_args(view=view, sort="updated_at", order="desc", sortable=rs.SORTABLE)
    with pytest.raises(ps.PassageError, match="view must be one of"):
        ps.check_list_args(view="everything", sort="updated_at", order="desc", sortable=rs.SORTABLE)


def test_a_sort_name_is_matched_against_the_columns_the_service_offers() -> None:
    """Refusing a sort name here is what keeps a sort out of raw SQL - and out of a
    column one screen does not have: a recording cannot be sorted by word count."""
    with pytest.raises(ps.PassageError, match="sort must be one of"):
        ps.check_list_args(view="bank", sort="word_count", order="desc", sortable=ls.SORTABLE)
    with pytest.raises(ps.PassageError, match="sort must be one of"):
        ps.check_list_args(view="bank", sort="title ", order="desc", sortable=rs.SORTABLE)
    with pytest.raises(ps.PassageError, match="order must be"):
        ps.check_list_args(view="bank", sort="title", order="sideways", sortable=rs.SORTABLE)


def test_a_layout_word_comes_from_the_editor_or_is_refused() -> None:
    """The layout decides where the text sits on a phone, so it is stored, not inferred."""
    assert ps.layout_of(None) == "above"
    assert ps.layout_of("tabbed") == "tabbed"
    with pytest.raises(ps.PassageError, match="layout must be one of"):
        ps.layout_of("sidebar")


# --------------------------------------------------------------------------- #
# What the server derives, and what it refuses to be told
# --------------------------------------------------------------------------- #


def test_the_word_count_counts_the_words_the_teacher_saved() -> None:
    """Runs of spaces, a pasted line break and the edges of the box are not extra words."""
    body = "  The   quick brown fox.\nJumps over the dog.  "
    assert rs.count_words(body) == 8


@pytest.mark.parametrize(
    "body,expected",
    [
        ("one two three", 3),
        ("one\ntwo\n\nthree", 3),
        ("   ", 0),
        ("one", 1),
    ],
)
def test_a_count_survives_the_line_breaks_a_paste_brings_in(body: str, expected: int) -> None:
    assert rs.count_words(body) == expected


def test_an_excerpt_is_one_flat_line_short_enough_for_a_table_row() -> None:
    """The list shows enough of a text to recognise it, without shipping the library."""
    assert rs.excerpt("The  cat\n sat") == "The cat sat"
    long_text = "word " * 200
    cut = rs.excerpt(long_text)
    assert len(cut) <= rs.EXCERPT_CHARACTERS + 1, "the ellipsis is the one extra character"
    assert cut.endswith("…")
    assert "\n" not in cut


def test_a_body_over_the_cap_is_a_refused_request_not_a_truncated_one() -> None:
    """A document longer than a page belongs to the Phase 8 importer, where each part is
    reviewed - a silently cut text is a lesson that lost its ending."""
    with pytest.raises(ValueError):
        r.ReadingCreate(title="Long read", body="x" * (p.MAX_BODY_CHARACTERS + 1))
    with pytest.raises(ValueError):
        l.ListeningCreate(title="Long transcript", transcript="x" * (p.MAX_BODY_CHARACTERS + 1))
    # Exactly at the cap is still a page of text, and is accepted.
    assert r.ReadingCreate(title="Long read", body="x" * p.MAX_BODY_CHARACTERS).body


def test_a_request_body_cannot_declare_a_derived_column() -> None:
    """`word_count`, `transcript_source` and `status` are refused, not ignored.

    A field a client typed and a field the server computed are two different numbers as
    soon as anybody edits the text, and `extra="forbid"` is what makes that a 422 instead
    of a disagreement stored in the database.
    """
    with pytest.raises(ValueError):
        r.ReadingCreate.model_validate(
            {"title": "T", "body": "Some text.", "word_count": 900}
        )
    with pytest.raises(ValueError):
        l.ListeningCreate.model_validate(
            {"title": "T", "transcript": "Hello.", "transcript_source": "imported"}
        )
    for body in (r.ReadingUpdate, l.ListeningUpdate):
        assert "status" not in body.model_fields
        assert "word_count" not in body.model_fields and "transcript_source" not in body.model_fields


@pytest.mark.parametrize("blank", [" ", "\n\n", ""], ids=["space", "newline", "empty"])
def test_a_blank_title_or_body_is_refused_on_the_way_in(blank: str) -> None:
    """`min_length=1` alone passes a title of one space, and the list would sort blanks."""
    with pytest.raises(ValueError):
        r.ReadingCreate(title=blank, body="some words")
    with pytest.raises(ValueError):
        r.ReadingCreate(title="A text", body=blank)
    with pytest.raises(ValueError):
        l.ListeningCreate(title=blank)


def test_a_language_code_is_normalised_before_it_is_compared() -> None:
    """`EN`, ` en ` and `en` are one language to a teacher."""
    assert r.ReadingCreate(title="T", body="words", language=" EN ").language == "en"
    assert l.ListeningCreate(title="T", language=" RU ").language == "ru"
    assert p.normalize_language("  ") is None


def test_a_provenance_says_where_this_endpoint_actually_got_the_words() -> None:
    """Only `manual` and `absent` are claimable here.

    `imported` is the Phase 8 importer's record and `auto` the Phase 12 adapter's: a
    transcript that says it was imported but arrived from a keyboard has no honest
    provenance at all.
    """
    assert ls.transcript_source_for("Hello class.") == enums.TranscriptSource.MANUAL.value
    assert ls.transcript_source_for("   ") == enums.TranscriptSource.ABSENT.value
    assert ls.transcript_source_for(None) == enums.TranscriptSource.ABSENT.value
    assert {source.value for source in enums.TranscriptSource} == {
        "manual",
        "imported",
        "auto",
        "absent",
    }


def test_a_row_written_by_another_module_keeps_the_provenance_it_arrived_with() -> None:
    imported = SimpleNamespace(transcript="Dictated text.", transcript_source="imported")
    assert ls._source(imported) == "imported"
    legacy = SimpleNamespace(transcript="Typed text.", transcript_source=None)
    assert ls._source(legacy) == "manual"
    empty = SimpleNamespace(transcript=None, transcript_source=None)
    assert ls._source(empty) == "absent"


def test_cue_lines_need_words_to_highlight() -> None:
    """A list of timestamps against no transcript is a player pointing at nothing."""
    cues = [{"start": 0, "end": 3, "text": "Hello"}]
    assert ls.cue_lines("Hello class.", cues) == cues
    assert ls.cue_lines("   ", cues) == []
    assert ls.cue_lines(None, cues) == []


def test_a_cue_list_is_refused_for_its_nonsense_and_not_its_format() -> None:
    """The shape a cue takes belongs to the importer and the speech adapter as well as to
    this editor, so unknown keys survive; a negative second does not."""
    assert p.check_timestamps([{"start": 1.5, "text": "Hi", "speaker": "native"}])
    with pytest.raises(ValueError, match="object with a time and a text"):
        p.check_timestamps(["00:12 Hi"])
    with pytest.raises(ValueError, match="transcript start must be a number"):
        p.check_timestamps([{"start": "twelve"}])
    with pytest.raises(ValueError, match="transcript end cannot be negative"):
        p.check_timestamps([{"start": 1, "end": -1}])
    with pytest.raises(ValueError, match="transcript text must be a string"):
        p.check_timestamps([{"start": 1, "text": 12}])
    with pytest.raises(ValueError, match="too many transcript lines"):
        p.check_timestamps([{"start": index} for index in range(5_001)])


def test_a_bad_cue_list_reaches_the_teacher_as_a_service_sentence() -> None:
    """`check_timestamps` raises `ValueError`; the patch route only maps `PassageError`.

    Without this mapping a mistyped cue list is a 500 in front of a teacher.
    """
    with pytest.raises(ps.PassageError, match="object with a time and a text"):
        ls._check_cues(["00:12 Hi"])


def test_an_edit_history_records_the_shape_of_a_change_not_the_text() -> None:
    """An audit table of every draft of every transcript would be a second library."""
    key, value = ls._audit_field("transcript", "a hundred words" * 10)
    assert (key, value) == ("transcript_characters", 150)
    assert ls._audit_field("transcript_timestamps", [{"start": 1}, {"start": 2}]) == ("cue_lines", 2)
    assert ls._audit_field("media_asset_id", ANY_ID) == ("has_audio", True)
    assert ls._audit_field("media_asset_id", None) == ("has_audio", False)
    assert ls._audit_field("replay_limit", 3) == ("replay_limit", 3)


# --------------------------------------------------------------------------- #
# A recording with nothing to hear
# --------------------------------------------------------------------------- #


async def test_an_empty_recording_cannot_be_published() -> None:
    """The refusal is the product rule; the sentence names what to do about it."""
    empty = SimpleNamespace(transcript=None, media_asset_id=None)
    with pytest.raises(ps.PassageError, match="needs audio or a transcript"):
        await ls.refuse_publish(None, empty)

    with_words = SimpleNamespace(transcript="Hello class.", media_asset_id=None)
    await ls.refuse_publish(None, with_words)


class _Assets:
    """A stand-in session that answers `db.get` for a file that is or is not in the trash.

    The publish rule needs to know whether the attached recording can still be played,
    and that is the only thing this fake has to be able to say.
    """

    def __init__(self, *, trashed: bool = False) -> None:
        self.trashed = trashed

    async def get(self, _model, _ident):
        return SimpleNamespace(deleted_at="yesterday" if self.trashed else None)


async def test_the_bulk_action_asks_the_same_rule_as_the_status_button() -> None:
    """Two entry points, one veto - and the bulk one returns a reason instead of raising,
    so one empty recording cannot stop thirty-nine publishes."""
    empty = SimpleNamespace(transcript="  ", media_asset_id=None)
    reason = await ls._refuse_publish_reason(None, empty)
    assert reason and "leave it as a draft" in reason

    playable = SimpleNamespace(transcript=None, media_asset_id=ANY_ID)
    assert await ls._refuse_publish_reason(_Assets(), playable) is None


async def test_a_published_recording_whose_file_was_thrown_out_stays_unpublishable() -> None:
    """The listening is ready and the asset is in the trash: the class would press play on
    nothing, so the veto reads the library rather than trusting the row."""
    orphaned = SimpleNamespace(transcript=None, media_asset_id=ANY_ID)
    reason = await ls._refuse_publish_reason(_Assets(trashed=True), orphaned)
    assert reason and "is in the trash" in reason


# --------------------------------------------------------------------------- #
# Sets: the shared grouping bodies
# --------------------------------------------------------------------------- #


def test_a_set_title_is_trimmed_and_must_be_there() -> None:
    assert p.QuestionSetCreate(title="  True or false  ").title == "True or false"
    with pytest.raises(ValueError):
        p.QuestionSetCreate(title="   ")


def test_a_listening_block_must_end_after_it_starts() -> None:
    assert p.ListeningSetCreate(title="Part 1", start_seconds=40, end_seconds=70).end_seconds == 70
    with pytest.raises(ValueError, match="must end after it starts"):
        p.ListeningSetCreate(title="Part 1", start_seconds=70, end_seconds=40)
    with pytest.raises(ValueError, match="must end after it starts"):
        p.ListeningSetUpdate(start_seconds=70, end_seconds=70)


def test_a_client_cannot_place_a_set_at_a_number_of_its_own() -> None:
    """Order is written by appending and by `reorder`; a body that carried `position`
    could put two sets in slot 3 and make the passage read differently on every reload."""
    for body in (p.QuestionSetCreate, p.ListeningSetCreate, p.QuestionSetUpdate):
        assert "position" not in body.model_fields
    with pytest.raises(ValueError):
        p.QuestionSetCreate.model_validate({"title": "Part 1", "position": 3})


def test_an_assignment_is_a_replacement_that_names_each_question_once() -> None:
    second = uuid.UUID("22222222-2222-2222-2222-222222222222")
    assert p.SetQuestionAssignment(question_ids=[ANY_ID, second]).question_ids == [ANY_ID, second]
    with pytest.raises(ValueError, match="cannot appear twice"):
        p.SetQuestionAssignment(question_ids=[ANY_ID, ANY_ID])
    with pytest.raises(ValueError):
        p.SetReorderRequest.model_validate({"set_ids": []})


def test_a_bulk_request_offers_exactly_the_four_operations() -> None:
    """There is no bulk *edit*: a body or a transcript changes one at a time, where a
    teacher can see it."""
    assert set(p.BULK_ACTIONS) == {"status", "trash", "restore", "set_level"}
    with pytest.raises(ValueError, match="action must be one of"):
        p.PassageBulkRequest(passage_ids=[ANY_ID], action="edit_body")
    with pytest.raises(ValueError):
        p.PassageBulkRequest(passage_ids=[], action="trash")
    with pytest.raises(ValueError, match="cannot appear twice"):
        p.PassageBulkRequest(passage_ids=[ANY_ID, ANY_ID], action="trash")
    assert p.PassageBulkRequest(passage_ids=[ANY_ID], action="set_level", level=" B1 ").level == "B1"


def test_the_replay_ceiling_is_the_number_the_editor_is_told() -> None:
    assert l.ListeningCreate(title="T", replay_limit=p.MAX_REPLAY_LIMIT).replay_limit == 50
    with pytest.raises(ValueError):
        l.ListeningCreate(title="T", replay_limit=p.MAX_REPLAY_LIMIT + 1)
    with pytest.raises(ValueError):
        l.ListeningCreate(title="T", replay_limit=-1)


# --------------------------------------------------------------------------- #
# Learner projections - the leak guard
# --------------------------------------------------------------------------- #


def test_a_learner_reading_carries_no_lifecycle_and_no_provenance() -> None:
    private = {
        "status",
        "deleted_at",
        "source_file_id",
        "set_count",
        "created_at",
        "updated_at",
        "audio_asset_id",
        "media_asset_id",
        "transcript_source",
        "config",
        "teacher_notes",
        "explanation",
    }
    for schema in (r.ReadingLearnerRead, r.ReadingLearnerSummary, p.LearnerSetRead):
        fields = set(schema.model_fields)
        assert not (fields & private), f"{schema.__name__} exposes {fields & private}"


def test_a_learner_listening_carries_the_player_and_not_the_library() -> None:
    """`audio` is the learner's media projection - a student path, not a file id - and the
    teacher's caption, filename, checksum and reference count stay behind."""
    fields = set(l.ListeningLearnerRead.model_fields)
    assert not fields & {
        "media_asset_id",
        "source_file_id",
        "transcript_source",
        "status",
        "deleted_at",
        "created_at",
        "updated_at",
        "config",
    }
    audio = l.ListeningLearnerRead.model_fields["audio"]
    assert "MediaLearnerRead" in str(audio.annotation), (
        "the file is served through the learner's own authorised path, not as a library id"
    )


def test_a_learner_set_loses_the_authoring_bucket() -> None:
    assert set(p.LearnerSetRead.model_fields) < set(p.QuestionSetRead.model_fields)
    assert "config" in p.QuestionSetRead.model_fields and "config" not in p.LearnerSetRead.model_fields
    # A listening block keeps its interval: it is what the player is told to play.
    assert {"start_seconds", "end_seconds"} <= set(p.ListeningLearnerSetRead.model_fields)


def test_a_question_stub_never_carries_an_answer() -> None:
    assert not set(p.QuestionStubRead.model_fields) & {
        "config",
        "answer",
        "correct_answer",
        "explanation",
        "teacher_notes",
    }


def test_the_editable_fields_are_all_in_the_read_payload() -> None:
    """A patch that sends back what the form never saw would silently delete it."""
    for create_body, read_body in ((r.ReadingCreate, r.ReadingRead), (l.ListeningCreate, l.ListeningRead)):
        writable = set(create_body.model_fields)
        readable = set(read_body.model_fields)
        # A recording is named by `media_asset_id` on the way in and described by `audio`
        # on the way out - the same file, told from the two ends.
        assert writable - readable == ({"media_asset_id"} if create_body is l.ListeningCreate else set())
        assert {f for f in writable if f != "media_asset_id"} <= set(
            r.ReadingUpdate.model_fields if create_body is r.ReadingCreate else l.ListeningUpdate.model_fields
        ) | {"status"}


def test_the_summary_a_teacher_sorts_by_is_the_summary_the_list_builds() -> None:
    """A sort name that is not a column on the row's own table is a 500 waiting for data."""
    for service, summary in ((rs, r.ReadingSummary), (ls, l.ListeningSummary)):
        assert set(service.SORTABLE) - {"created_at", "updated_at", "title", "level", "status"} == (
            {"word_count"} if service is rs else set()
        )
        assert set(service.SORTABLE) <= set(summary.model_fields)
