"""the rules an import queue needs: one source per file, one content item per candidate

Revision ID: 0007_import_integrity
Revises: 0006_exam_attempt_integrity
Create Date: 2026-10-08

Phase 8 lets a teacher hand the platform a document and decide, row by row, what of it
becomes content. Two of those decisions are rules the database has to hold, because both
are reached by more than one pair of hands at once, and three columns are what the review
screen actually reads. The third rule of the pair below is named here too, because it is
the one no index can hold and a reader of this revision would otherwise assume it was.

* **One source per file of bytes.** `source_file.checksum` is the sha256 of what was
  uploaded, and the partial unique index says the same paper sent twice is one source with
  one review queue - not two queues whose candidates a teacher then has to approve twice.
  Partial because trash is a soft delete: a trashed source still holds its file, and the
  same bytes may legitimately be imported again after it is gone.
* **One content row has one candidate behind it.** `import_item` records what a candidate
  turned into, and this index makes that pair unique once it is written: two rows of one
  queue cannot claim to have filed the same question. Approving the *same* candidate twice
  is the other half of "one candidate becomes one piece of content", and no index can hold
  it - each request mints its own content id, so the index has nothing to disagree about.
  That half belongs to `import_service._locked`, which reads the row under a lock, into the
  object the decision is made from, and refuses the teacher who arrives second.
* **`ix_import_item_job_decision`** serves the queue itself: "what is still waiting in this
  job" is asked on every visit to the review screen, and bootstrap indexes only `job_id`,
  so the answer would come from reading every candidate the job produced, approved and
  rejected included.
* **`ix_import_item_job_position`** is the same argument about order. `import_item.id` is a
  random UUID, so a queue read without an ordering column arrives in a different sequence
  on every page - and a teacher works through a paper question 1, then 2, then 3.

`missing`, `filing`, `note` and `position` describe the candidate rather than the
document's words, so they are columns of their own instead of keys inside `extracted`: what
a teacher approved must stay provable as the document's own text. `missing`, `filing` and
`position` carry a server default because a row already written by a previous version of
this code has to gain a value, not a NULL in a NOT NULL column.

Nothing here rewrites a row, so an upgrade over data that already breaks one of the two
unique rules reports the violation instead of choosing which duplicate to keep. Downgrading
drops exactly what was added, columns included, and returns to the previous revision's
shape.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_import_integrity"
down_revision = "0006_exam_attempt_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "source_file", sa.Column("checksum", sa.String(length=128), nullable=True)
    )
    op.create_index(
        "uq_source_file_checksum",
        "source_file",
        ["checksum"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.add_column(
        "import_item",
        sa.Column(
            "missing",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "import_item",
        sa.Column(
            "filing",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("import_item", sa.Column("note", sa.Text(), nullable=True))
    op.add_column(
        "import_item",
        sa.Column(
            "position", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )

    op.create_index("ix_import_item_job_decision", "import_item", ["job_id", "decision"])
    op.create_index("ix_import_item_job_position", "import_item", ["job_id", "position"])
    op.create_index(
        "uq_import_item_result",
        "import_item",
        ["result_ref_type", "result_ref_id"],
        unique=True,
        postgresql_where=sa.text("result_ref_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_import_item_result", table_name="import_item")
    op.drop_index("ix_import_item_job_position", table_name="import_item")
    op.drop_index("ix_import_item_job_decision", table_name="import_item")

    op.drop_column("import_item", "position")
    op.drop_column("import_item", "note")
    op.drop_column("import_item", "filing")
    op.drop_column("import_item", "missing")

    op.drop_index("uq_source_file_checksum", table_name="source_file")
    op.drop_column("source_file", "checksum")
