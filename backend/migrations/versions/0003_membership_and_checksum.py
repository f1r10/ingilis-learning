"""question-set membership, and one media asset per file

Revision ID: 0003_membership_and_checksum
Revises: 0002_vocabulary_word_unique
Create Date: 2026-10-07

Phase 5 makes reading and listening passages editable objects rather than columns of
a future feature, and two gaps in the bootstrap schema become visible at that moment.

`reading_question_set` / `listening_question_set` already existed with a title, an
instruction line and a position, but nothing recorded which questions belonged to a
set. Membership goes in its own table instead of a column on `question`, because
filing a question under a heading is not a content edit: it must not append a
`QuestionVersion`, and a set is deleted far more often than the questions in it are.
Both new foreign keys cascade, so removing a passage's set removes the grouping and
leaves the questions bound to the passage through `question.reading_id` /
`listening_id` - the explicit context relationship bootstrap already enforces. The
unique index on `question_id` is the rule that grouping is a tree, not a many-to-many:
one question answers one block of one text.

`media_asset.checksum` gains a partial unique index. "Physical file stored once;
referenced many" is a promise about storage cost and about a teacher's library not
filling with the same recording four times, and a request handler cannot see an upload
that is still in flight. The check is partial because trash is a soft delete: a trashed
asset still holds its file, and the same bytes may legitimately be re-added once it is
gone. The plain bootstrap index is dropped in the same breath - it indexes the same
column, and the unique index answers every read it did.

Downgrade reverses exactly these four statements and rewrites no row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_membership_and_checksum"
down_revision = "0002_vocabulary_word_unique"
branch_labels = None
depends_on = None


def _membership_table(name: str, set_table: str) -> None:
    op.create_table(
        name,
        sa.Column("set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("question_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["set_id"],
            [f"{set_table}.id"],
            name=f"fk_{name}_set_id_{set_table}",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["question.id"],
            name=f"fk_{name}_question_id_question",
            ondelete="CASCADE",
        ),
    )
    op.create_index(f"uq_{name}_question", name, ["question_id"], unique=True)
    op.create_index(f"ix_{name}_set", name, ["set_id"])


def upgrade() -> None:
    _membership_table("reading_set_question", "reading_question_set")
    _membership_table("listening_set_question", "listening_question_set")

    op.drop_index("ix_media_asset_checksum", table_name="media_asset")
    op.create_index(
        "uq_media_asset_checksum",
        "media_asset",
        ["checksum"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_media_asset_checksum", table_name="media_asset")
    op.create_index("ix_media_asset_checksum", "media_asset", ["checksum"])

    for name in ("listening_set_question", "reading_set_question"):
        op.drop_index(f"ix_{name}_set", table_name=name)
        op.drop_index(f"uq_{name}_question", table_name=name)
        op.drop_table(name)
