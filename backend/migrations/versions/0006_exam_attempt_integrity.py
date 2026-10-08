"""the rules an exam run needs: assign once, attempt once, review once

Revision ID: 0006_exam_attempt_integrity
Revises: 0005_catalog_item_unique
Create Date: 2026-10-08

Phase 7 puts students behind these tables, and three of the promises the exam screens make
are only real if the database refuses the duplicate. Each one is a rule the service already
enforces in the teacher's own words; the index is what keeps that true when two writes
arrive in the same moment - a teacher double-clicking "Assign", a learner opening the same
exam in two tabs, a submit and the worker's expiry sweep finishing the same attempt.

* **A student is assigned to an exam once, and a group is assigned once.** Two partial
  unique indexes rather than one on `(exam_id, student_id, group_id)`: the target pair is
  deliberately nullable (a row names either a student or a group), and a plain unique index
  over nullable columns lets any number of `(exam A, student S, NULL)` rows through,
  because a row containing NULL is never equal to anything. Partitioning the rule by which
  half is filled is the version PostgreSQL can enforce, and it still allows one exam to be
  assigned to a student and to that student's group - which is a single assignment to them
  in every read, because the learner's list is deduplicated by exam.
* **An exam has one attempt number per student.** `attempt_number` is the learner's own
  sequence ("attempt 2 of 3"), and a second row numbered 1 would be a run nobody can
  describe; the unique index is what makes the duplicate-start guard a fact rather than a
  habit.
* **An answer sits in the review queue once.** The queue is a teacher's worklist; two rows
  for one answer means the same essay marked twice and a total that moved under the learner.

`ix_attempt_open_expiry` serves the worker's sweep, which asks one question every fifteen
seconds: which attempts are still open and already past their server-issued expiry.
Bootstrap indexes `status` and `expires_at` separately, so answering it from those means
reading a table that is mostly submitted rows.

Nothing here rewrites a row, so an upgrade over data that already breaks one of these rules
reports the violation instead of quietly choosing which duplicate to keep. Downgrading drops
only these four indexes.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_exam_attempt_integrity"
down_revision = "0005_catalog_item_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_assignment_student",
        "exam_assignment",
        ["exam_id", "student_id"],
        unique=True,
        postgresql_where=sa.text("student_id IS NOT NULL"),
    )
    op.create_index(
        "uq_assignment_group",
        "exam_assignment",
        ["exam_id", "group_id"],
        unique=True,
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )
    op.create_index(
        "uq_attempt_number",
        "exam_attempt",
        ["exam_id", "student_id", "attempt_number"],
        unique=True,
    )
    op.create_index(
        "uq_manual_review_answer", "manual_review", ["answer_id"], unique=True
    )
    op.create_index("ix_attempt_open_expiry", "exam_attempt", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_attempt_open_expiry", table_name="exam_attempt")
    op.drop_index("uq_manual_review_answer", table_name="manual_review")
    op.drop_index("uq_attempt_number", table_name="exam_attempt")
    op.drop_index("uq_assignment_group", table_name="exam_assignment")
    op.drop_index("uq_assignment_student", table_name="exam_assignment")
