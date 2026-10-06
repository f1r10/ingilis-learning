"""Race-sensitivity checks on real Postgres (spec S8).

Each test drives two transactions at the same instant and asserts the invariant
survives, which is only possible because the database - not a Python `if` - holds
it: a single-use claim guarded by `used_at IS NULL`, a partial unique index for
"one ACTIVE access key per student", and `uq_group_student`.
"""
from __future__ import annotations

import asyncio
import uuid

from helpers import error_of
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core import enums, security
from app.core.database import SessionLocal
from app.models.identity import (
    AdminRecoveryCode,
    Group,
    GroupMembership,
    Student,
    StudentAccessKey,
    StudentSession,
)


async def _count(db, model, *conditions) -> int:
    stmt = select(func.count()).select_from(model)
    for c in conditions:
        stmt = stmt.where(c)
    return (await db.execute(stmt)).scalar_one()


async def _active_key_count(student_id: str) -> int:
    async with SessionLocal() as db:
        return await _count(
            db,
            StudentAccessKey,
            StudentAccessKey.student_id == uuid.UUID(student_id),
            StudentAccessKey.status == enums.AccessKeyStatus.ACTIVE,
        )


async def _live_session_count(student_id: str) -> int:
    async with SessionLocal() as db:
        return await _count(
            db,
            StudentSession,
            StudentSession.student_id == uuid.UUID(student_id),
            StudentSession.terminated_at.is_(None),
        )


async def test_two_concurrent_recovery_code_claims_yield_exactly_one_success(
    client, session_factory, clean_db, admin_credentials
):
    """S8: the same code, claimed simultaneously, must work once and only once."""
    username, _password = admin_credentials
    await client.login_admin()
    generated = await client.post("/api/v1/admin/recovery-codes")
    assert generated.status_code == 200, generated.text
    code = generated.json()["codes"][0]

    first, second = session_factory(), session_factory()
    outcomes = await asyncio.gather(
        first._c.post("/api/v1/auth/admin/recovery/login", json={"username": username, "code": code}),
        second._c.post("/api/v1/auth/admin/recovery/login", json={"username": username, "code": code}),
    )
    statuses = sorted(r.status_code for r in outcomes)
    assert statuses == [200, 401], (
        "exactly one concurrent claim may win: "
        f"{[(r.status_code, r.text) for r in outcomes]}"
    )

    winner = next(r for r in outcomes if r.status_code == 200)
    loser = next(r for r in outcomes if r.status_code == 401)
    assert error_of(loser)["code"] == "unauthorized"
    assert winner.json()["subject_type"] == "admin"

    async with SessionLocal() as db:
        used = await _count(db, AdminRecoveryCode, AdminRecoveryCode.used_at.is_not(None))
        total = await _count(db, AdminRecoveryCode)
    assert used == 1, f"one code was claimed, but {used} rows are marked used"
    assert total == 5, "the other codes must still be unused"


async def test_concurrent_rotations_can_never_leave_two_active_keys(client, clean_db):
    """S8: rotation is revoke-then-insert in one transaction, protected by a
    partial unique index - so no committed state has two live keys, and the loser
    gets a clean conflict instead of a 500."""
    created = await client.post(
        "/api/v1/students", json={"name": "Race", "surname": "Rotate", "username": "race-rotate"}
    )
    assert created.status_code == 201, created.text
    student_id = created.json()["student"]["id"]

    outcomes = await asyncio.gather(
        client.post(f"/api/v1/students/{student_id}/access-key/rotate"),
        client.post(f"/api/v1/students/{student_id}/access-key/rotate"),
    )
    observed = [(r.status_code, r.text) for r in outcomes]
    assert all(r.status_code in (200, 409) for r in outcomes), observed
    assert any(r.status_code == 200 for r in outcomes), "at least one rotation must succeed"

    for r in outcomes:
        if r.status_code == 409:
            assert error_of(r)["code"] == "rotation_in_progress", r.text

    assert await _active_key_count(student_id) == 1, (
        "exactly one ACTIVE key may exist after concurrent rotations"
    )

    # The winner's plaintext key is the one the database now points at.
    winner = next(r for r in outcomes if r.status_code == 200)
    survivor = (await client.get(f"/api/v1/students/{student_id}")).json()["active_key_prefix"]
    assert survivor == security.access_key_fingerprint(winner.json()["access_key"])


async def test_concurrent_duplicate_membership_adds_exactly_one_row(client, clean_db):
    """S8: `uq_group_student` decides the winner, so a double add cannot persist."""
    group = await client.post("/api/v1/groups", json={"name": "Race Group"})
    assert group.status_code == 201, group.text
    gid = group.json()["id"]
    student = await client.post(
        "/api/v1/students", json={"name": "Race", "surname": "Member", "username": "race-member"}
    )
    sid = student.json()["student"]["id"]

    outcomes = await asyncio.gather(
        client.post(f"/api/v1/groups/{gid}/members", json={"student_id": sid}),
        client.post(f"/api/v1/groups/{gid}/members", json={"student_id": sid}),
    )
    statuses = sorted(r.status_code for r in outcomes)
    assert statuses == [201, 409], [(r.status_code, r.text) for r in outcomes]
    assert error_of(next(r for r in outcomes if r.status_code == 409))["code"] == "already_member"

    async with SessionLocal() as db:
        rows = await _count(
            db,
            GroupMembership,
            GroupMembership.group_id == uuid.UUID(gid),
            GroupMembership.student_id == uuid.UUID(sid),
        )
    assert rows == 1, f"duplicate add persisted {rows} rows"


async def test_raw_concurrent_inserts_are_settled_by_the_indexes(db_ready):
    """The invariants hold even with no application code in the loop.

    Two independent transactions write the identical membership pair and two
    ACTIVE key rows for one student; each time exactly one must commit.
    """
    async with SessionLocal() as db:
        student = Student(name="Raw", surname="Race", username="raw-race")
        group = Group(name="Raw Race Group")
        db.add_all([student, group])
        await db.commit()
        student_id, group_id = student.id, group.id

    async def _insert(stmt: text, params: dict) -> str:
        async with SessionLocal() as db:
            try:
                await db.execute(stmt, params)
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return "rejected"
            return "committed"

    membership_sql = text(
        "INSERT INTO group_membership (group_id, student_id) VALUES (:g, :s)"
    )
    membership_args = {"g": str(group_id), "s": str(student_id)}
    membership_results = await asyncio.gather(
        _insert(membership_sql, membership_args), _insert(membership_sql, membership_args)
    )
    assert sorted(membership_results) == ["committed", "rejected"], membership_results

    key_sql = text(
        "INSERT INTO student_access_key (student_id, key_hash, key_prefix, status) "
        "VALUES (:s, :h, :p, 'ACTIVE')"
    )
    key_results = await asyncio.gather(
        _insert(key_sql, {"s": str(student_id), "h": "$argon2id$one", "p": "raw-race@aaaaaa"}),
        _insert(key_sql, {"s": str(student_id), "h": "$argon2id$two", "p": "raw-race@bbbbbb"}),
    )
    assert sorted(key_results) == ["committed", "rejected"], (
        f"the partial unique index failed to serialise two ACTIVE keys: {key_results}"
    )

    async with SessionLocal() as db:
        memberships = await _count(
            db,
            GroupMembership,
            GroupMembership.group_id == group_id,
            GroupMembership.student_id == student_id,
        )
    assert memberships == 1
    assert await _active_key_count(str(student_id)) == 1


async def test_concurrent_disable_and_revoke_leave_no_live_session(
    client, session_factory, clean_db
):
    """Termination is a set-based UPDATE: no session slips through a Python loop."""
    created = await client.post(
        "/api/v1/students", json={"name": "Many", "surname": "Sessions", "username": "many-sessions"}
    )
    assert created.status_code == 201, created.text
    student_id, key = created.json()["student"]["id"], created.json()["access_key"]

    sessions = [session_factory() for _ in range(3)]
    await asyncio.gather(*(s.login_student(key) for s in sessions))
    assert await _live_session_count(student_id) == 3

    outcomes = await asyncio.gather(
        client.post(f"/api/v1/students/{student_id}/status", json={"status": "disabled"}),
        client.post(f"/api/v1/students/{student_id}/access-key/revoke"),
    )
    assert [r.status_code for r in outcomes] == [200, 200], [r.text for r in outcomes]

    assert await _live_session_count(student_id) == 0, (
        "concurrent disable+revoke must leave no live session"
    )
    assert await _active_key_count(student_id) == 0
    for s in sessions:
        assert (await s.get("/api/v1/auth/me/student")).status_code == 401
