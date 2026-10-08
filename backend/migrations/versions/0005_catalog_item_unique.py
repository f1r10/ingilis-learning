"""one catalog references one piece of content once

Revision ID: 0005_catalog_item_unique
Revises: 0004_media_reference_indexes
Create Date: 2026-10-07

A catalog is a list of references into the central banks, and the whole point of that
shape is that the content stays in one place. Bootstrap indexed `catalog_item` for the
reads it does - by catalog, by position, by `ref_id` - but wrote no rule about the same
content being named twice in one catalog, and Phase 6 needs the rule rather than the
habit: a practice run that asks the same question twice is a run whose step count lied
to the learner, and a learner who answers it once and then sees two steps cannot tell
which answer moved which one.

The service already refuses the duplicate and names the item in the sentence. This index
is the same rule under concurrency: two teachers, or a teacher and the Phase 8 importer,
adding the same content in the same moment cannot both win - the loser gets an integrity
error to translate instead of a second row that no read expects.

Not partial: `catalog_item` has no soft delete, so there is no trashed twin to forgive.
A reference leaves by its row being deleted, and once it is gone it may be added again.

Downgrading drops only this index. No row is rewritten, so an upgrade after a downgrade
succeeds again as long as no duplicate was created in the meantime.
"""

from __future__ import annotations

from alembic import op

revision = "0005_catalog_item_unique"
down_revision = "0004_media_reference_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_catalog_item_reference",
        "catalog_item",
        ["catalog_id", "kind", "ref_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_catalog_item_reference", table_name="catalog_item")
