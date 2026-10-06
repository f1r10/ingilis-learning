"""one live vocabulary word per learning language

Revision ID: 0002_vocabulary_word_unique
Revises: 0001_bootstrap
Create Date: 2026-10-06

Phase 4 promotes the word list to a central bank that teachers maintain by hand and
the document importer (Phase 8) fills in through a worker. Both paths can arrive at
the same word twice, so the rule "one live entry per word per learning language"
belongs in the database, not only in the request handler: a service-level check
cannot see a request that is still in flight.

The index is partial on purpose. Trash is a soft delete, and a teacher expects a
trashed word to stop blocking the correct spelling while still being restorable;
`deleted_at IS NULL` keeps the live list clean without touching history.

Downgrading drops only this index. No row is rewritten, so an upgrade after a
downgrade succeeds again as long as no duplicate was created in the meantime.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_vocabulary_word_unique"
down_revision = "0001_bootstrap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_vocabulary_word_language",
        "vocabulary_entry",
        ["word", "learning_language"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_vocabulary_word_language", table_name="vocabulary_entry")
