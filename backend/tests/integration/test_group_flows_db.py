"""Groups, memberships and relational integrity on real Postgres (spec S7).

The point of these tests is the *relationship*: a membership row must never
outlive its group or its student, an archived student must disappear from
listings that join through the membership, and a member count must reflect the
rows rather than a cached field. Skips via `clean_db` when Postgres is down.
"""
from __future__ import annotations

import uuid

import pytest
from helpers import error_of
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.models.identity import Group, GroupMembership, Student


async def _create_group(client, name: str) -> str:
    r = await client.post("/api/v1/groups", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _create_student(client, name: str, surname: str, username: str) -> str:
    r = await client.post(
        "/api/v1/students", json={"name": name, "surname": surname, "username": username}
    )
    assert r.status_code == 201, r.text
    return r.json()["student"]["id"]


async def _membership_rows(group_id: str) -> list[GroupMembership]:
    async with SessionLocal() as db:
        result = await db.execute(
            select(GroupMembership).where(GroupMembership.group_id == uuid.UUID(group_id))
        )
        return list(result.scalars())


async def test_group_crud_and_member_count(client):
    gid = await _create_group(client, "IELTS 2026")
    listed = await client.get("/api/v1/groups")
    assert listed.status_code == 200, listed.text
    assert gid in [g["id"] for g in listed.json()["items"]]

    updated = await client.patch(f"/api/v1/groups/{gid}", json={"description": "spring cohort"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["description"] == "spring cohort"

    student_id = await _create_student(client, "Rosa", "Parks", "rosa")
    add = await client.post(f"/api/v1/groups/{gid}/members", json={"student_id": student_id})
    assert add.status_code == 201, add.text

    after = await client.get("/api/v1/groups")
    mine = next(g for g in after.json()["items"] if g["id"] == gid)
    assert mine["member_count"] == 1, "member_count must be computed from the rows"

    removed = await client.delete(f"/api/v1/groups/{gid}/members/{student_id}")
    assert removed.status_code == 200, removed.text
    final = next(g for g in (await client.get("/api/v1/groups")).json()["items"] if g["id"] == gid)
    assert final["member_count"] == 0


async def test_membership_lifecycle_and_duplicate_rejection(client):
    gid = await _create_group(client, "Speaking Club")
    sid = await _create_student(client, "Malala", "Yousafzai", "malala")

    assert (await client.post(f"/api/v1/groups/{gid}/members", json={"student_id": sid})).status_code == 201

    dup = await client.post(f"/api/v1/groups/{gid}/members", json={"student_id": sid})
    assert dup.status_code == 409, dup.text
    assert error_of(dup)["code"] == "already_member"
    assert len(await _membership_rows(gid)) == 1, "a rejected add must not leave a row behind"

    members = await client.get(f"/api/v1/groups/{gid}/members")
    assert members.status_code == 200, members.text
    assert [m["id"] for m in members.json()["items"]] == [sid]

    missing_group = await client.post(
        f"/api/v1/groups/{uuid.uuid4()}/members", json={"student_id": sid}
    )
    assert missing_group.status_code == 404, missing_group.text

    missing_student = await client.post(
        f"/api/v1/groups/{gid}/members", json={"student_id": str(uuid.uuid4())}
    )
    assert missing_student.status_code == 422, missing_student.text
    assert error_of(missing_student)["code"] == "validation_failed"

    non_uuid = await client.post(f"/api/v1/groups/{gid}/members", json={"student_id": "zzz"})
    assert non_uuid.status_code == 422, non_uuid.text

    bad_group_path = await client.get("/api/v1/groups/not-a-uuid/members")
    assert bad_group_path.status_code == 422, bad_group_path.text

    absent = await client.delete(f"/api/v1/groups/{gid}/members/{uuid.uuid4()}")
    assert absent.status_code == 404, absent.text


async def test_archived_student_leaves_the_membership_listing(client):
    gid = await _create_group(client, "Archive Watch")
    sid = await _create_student(client, "Ada", "Secondary", "ada-sec")
    assert (await client.post(f"/api/v1/groups/{gid}/members", json={"student_id": sid})).status_code == 201

    assert (await client.post(f"/api/v1/students/{sid}/status", json={"status": "archived"})).status_code == 200
    members = (await client.get(f"/api/v1/groups/{gid}/members")).json()["items"]
    assert members == [], "an archived student must not be listed as an active member"

    # The join row still exists (soft delete), so reactivating restores the group.
    assert (await client.post(f"/api/v1/students/{sid}/status", json={"status": "active"})).status_code == 200
    assert [m["id"] for m in (await client.get(f"/api/v1/groups/{gid}/members")).json()["items"]] == [sid]

    listed = (await client.get("/api/v1/students", params={"group_id": gid})).json()["items"]
    assert sid in [i["id"] for i in listed], "group filtering must use the membership rows"


async def test_creating_a_student_with_group_ids_populates_membership(client):
    gid = await _create_group(client, "Direct Assignment")
    sid = (
        await client.post(
            "/api/v1/students",
            json={"name": "Self", "surname": "Yuraku", "username": "selfy", "group_ids": [gid]},
        )
    ).json()["student"]
    assert sid["group_ids"] == [gid]
    assert [str(m.group_id) for m in await _membership_rows(gid)] == [gid]

    bogus = await client.post(
        "/api/v1/students",
        json={"name": "No", "surname": "Group", "username": "no-group", "group_ids": [str(uuid.uuid4())]},
    )
    assert bogus.status_code == 422, bogus.text
    assert error_of(bogus)["code"] == "validation_failed"
    assert bogus.json()["error"].get("fields") is None, "a domain 422 need not fake field errors"


async def test_deleting_a_group_or_student_cascades_the_join_table(client):
    """ON DELETE CASCADE is schema behaviour, so the join rows really do go."""
    gid = await _create_group(client, "Cascade Group")
    sid = await _create_student(client, "Anita", "Diaz", "anita")
    assert (await client.post(f"/api/v1/groups/{gid}/members", json={"student_id": sid})).status_code == 201

    async with SessionLocal() as db:
        await db.execute(text("DELETE FROM student WHERE id = :i"), {"i": uuid.UUID(sid)})
        await db.commit()
    assert await _membership_rows(gid) == [], "student delete must cascade its memberships"

    assert (await client.delete(f"/api/v1/groups/{gid}")).status_code == 200
    async with SessionLocal() as db:
        await db.execute(text('DELETE FROM "group" WHERE id = :i'), {"i": uuid.UUID(gid)})
        await db.commit()
    assert await _membership_rows(gid) == [], "group delete must cascade its memberships"

    gone = await client.get(f"/api/v1/groups/{gid}/members")
    assert gone.status_code == 404, gone.text


async def test_the_database_refuses_orphan_and_duplicate_memberships(db_ready):
    """S8: FK + uq_group_student are enforced by Postgres, not only by the endpoint."""
    async with SessionLocal() as db:
        student = Student(name="Raw", surname="Insert", username="raw-insert")
        group = Group(name="Raw Group")
        db.add_all([student, group])
        await db.commit()
        student_id, group_id = student.id, group.id

    dup_sql = text(
        "INSERT INTO group_membership (group_id, student_id) VALUES (:g, :s)"
    )
    params = {"g": str(group_id), "s": str(student_id)}

    # A membership pointing at a group that does not exist cannot be written.
    async with SessionLocal() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                text(
                    "INSERT INTO group_membership (group_id, student_id) "
                    "VALUES (gen_random_uuid(), :s)"
                ),
                {"s": str(student_id)},
            )

    async with SessionLocal() as db:
        await db.execute(dup_sql, params)
        await db.commit()
        with pytest.raises(IntegrityError):
            await db.execute(dup_sql, params)
        await db.rollback()
        survived = (
            await db.execute(
                select(func.count())
                .select_from(GroupMembership)
                .where(GroupMembership.student_id == student_id)
            )
        ).scalar_one()
    assert survived == 1, "exactly one membership may survive the rejected duplicate"
