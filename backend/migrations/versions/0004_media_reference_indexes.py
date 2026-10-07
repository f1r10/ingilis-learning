"""indexes for the three columns the media library reads in reverse

Revision ID: 0004_media_reference_indexes
Revises: 0003_membership_and_checksum
Create Date: 2026-10-07

Bootstrap put a foreign key from `question.media_asset_id`, `listening.media_asset_id`
and `vocabulary_entry.audio_asset_id` onto `media_asset.id`, and no index on any of
them - which is fine when the only question ever asked is "which file does this
exercise use?", because the primary key already answers that.

Phase 5 asks the opposite question, twice on ordinary requests: how much live content
points at this file (before the library may trash it, and to print the count on the
asset's card), and whether at least one *ready* listening, question or word points at it
(before a learner is allowed to play it). Postgres does not index a foreign key on the
referrer's side, so each of those would be a sequential scan of a table that grows for
the life of the school.

Three single-column indexes, no unique constraints: many exercises may share one file,
and that sharing is the point of the library.

Downgrade drops exactly these three and rewrites no row.
"""

from __future__ import annotations

from alembic import op

revision = "0004_media_reference_indexes"
down_revision = "0003_membership_and_checksum"
branch_labels = None
depends_on = None

#: (index name, table, column) - the reverse lookups Phase 5 performs.
_REFERENCE_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("ix_question_media_asset_id", "question", "media_asset_id"),
    ("ix_listening_media_asset_id", "listening", "media_asset_id"),
    ("ix_vocabulary_entry_audio_asset_id", "vocabulary_entry", "audio_asset_id"),
)


def upgrade() -> None:
    for name, table, column in _REFERENCE_INDEXES:
        op.create_index(name, table, [column], unique=False)


def downgrade() -> None:
    for name, table, _column in reversed(_REFERENCE_INDEXES):
        op.drop_index(name, table_name=table)
