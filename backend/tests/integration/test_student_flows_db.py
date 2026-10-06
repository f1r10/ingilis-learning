"""Student lifecycle on a real Postgres built by the migration (spec S7).

Beyond the HTTP round-trips, each step is checked against the rows the server
actually wrote: exactly one ACTIVE access key, sessions terminated when access is
cut, soft-delete markers cleared on reactivation. Skips via `clean_db` when
Postgres is down.
"""
from __future__ import annotations

import uuid

import pytest
from helpers import error_of
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core import enums, security
from app.core.database import SessionLocal
from app.models.identity import Student, StudentAccessKey, StudentSession


async def _student_row(student_id: str) -> Student:
    async with SessionLocal() as db:
        return await db.get(Student, uuid.UUID(student_id))


async def _key_rows(student_id: str) -> list[StudentAccessKey]:
    async with SessionLocal() as db:
        result = await db.execute(
            select(StudentAccessKey).where(StudentAccessKey.student_id == uuid.UUID(student_id))
        )
        return list(result.scalars())


async def _active_keys(student_id: str) -> list[StudentAccessKey]:
    return [
        k for k in await _key_rows(student_id) if k.status == enums.AccessKeyStatus.ACTIVE
    ]


async def _live_sessions(student_id: str) -> list[StudentSession]:
    async with SessionLocal() as db:
        result = await db.execute(
            select(StudentSession).where(
                StudentSession.student_id == uuid.UUID(student_id),
                StudentSession.terminated_at.is_(None),
            )
        )
        return list(result.scalars())


async def _create_student(client, name: str, surname: str, username: str) -> tuple[str, str]:
    r = await client.post(
        "/api/v1/students", json={"name": name, "surname": surname, "username": username}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return body["student"]["id"], body["access_key"]


async def test_create_read_update_and_list_a_student(client):
    student_id, access_key = await _create_student(client, "Ada", "Lovelace", "ada")

    created = (await client.get(f"/api/v1/students/{student_id}")).json()
    assert created["username"] == "ada"
    assert created["status"] == enums.StudentStatus.ACTIVE.value
    assert created["active_key_prefix"] == security.access_key_fingerprint(access_key), (
        "the read model must expose the same non-secret fingerprint that is stored"
    )
    assert access_key not in str(created), "a plaintext key must never come back on read"

    listed = await client.get("/api/v1/students", params={"q": "ada", "page_size": 10})
    assert listed.status_code == 200, listed.text
    envelope = listed.json()
    assert {"items", "total", "page", "page_size"} <= set(envelope)
    assert any(i["id"] == student_id for i in envelope["items"])

    updated = await client.patch(
        f"/api/v1/students/{student_id}", json={"surname": "Byron", "ui_language": "en"}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["surname"] == "Byron"
    assert (await _student_row(student_id)).surname == "Byron", "PATCH must persist"

    other_id, _key = await _create_student(client, "Other", "Student", "other-student")
    clash = await client.patch(f"/api/v1/students/{other_id}", json={"username": "ada"})
    assert clash.status_code == 409, clash.text
    assert error_of(clash)["code"] == "username_taken"


async def test_duplicate_username_conflicts_instead_of_500(client):
    await _create_student(client, "First", "Person", "dup-user")
    second = await client.post(
        "/api/v1/students", json={"name": "Second", "surname": "Person", "username": "dup-user"}
    )
    assert second.status_code == 409, second.text
    assert error_of(second)["code"] == "username_taken"
    assert not (await _student_row(str(uuid.uuid4()))), "sanity: absent id reads as None"


async def test_unknown_or_malformed_ids_use_the_right_status(client):
    missing = str(uuid.uuid4())
    routes = [
        ("get", f"/api/v1/students/{missing}", None),
        ("post", f"/api/v1/students/{missing}/status", {"status": "disabled"}),
        ("post", f"/api/v1/students/{missing}/access-key/rotate", None),
        ("post", f"/api/v1/students/{missing}/access-key/revoke", None),
    ]
    for method, path, json_body in routes:
        if method == "get":
            r = await client.get(path)
        else:
            r = await client.post(path, json=json_body or {})
        assert r.status_code == 404, f"{method.upper()} {path} -> {r.status_code} {r.text}"
        assert error_of(r)["code"] == "not_found", path

    malformed = await client.get("/api/v1/students/not-a-uuid")
    assert malformed.status_code == 422, malformed.text
    assert error_of(malformed)["code"] == "validation_failed"

    # An unknown id is a 404 before the status vocabulary is even consulted.
    bad = await client.post(f"/api/v1/students/{missing}/status", json={"status": "on-hold"})
    assert bad.status_code == 404, bad.text


async def test_rotation_leaves_no_window_where_both_keys_are_valid(client, session_factory):
    student_id, old_key = await _create_student(client, "Grace", "Hopper", "grace")

    student_session = session_factory()
    await student_session.login_student(old_key)
    assert (await student_session.get("/api/v1/auth/me/student")).status_code == 200

    rotated = await client.post(f"/api/v1/students/{student_id}/access-key/rotate")
    assert rotated.status_code == 200, rotated.text
    new_key = rotated.json()["access_key"]
    assert new_key != old_key

    # The revocation and the insert commit together: the moment the response is
    # observable the old key is dead and exactly one key is ACTIVE.
    dead = await session_factory()._c.post(
        "/api/v1/auth/student/login", json={"access_key": old_key}
    )
    assert dead.status_code == 401, "old key survived a committed rotation"
    active = await _active_keys(student_id)
    assert len(active) == 1, f"rotation must leave exactly one ACTIVE key, found {len(active)}"
    assert active[0].key_prefix == security.access_key_fingerprint(new_key)

    revoked = [k for k in await _key_rows(student_id) if k.status == enums.AccessKeyStatus.REVOKED]
    assert len(revoked) == 1 and revoked[0].revoked_at is not None

    # Rotation also ends the session established with the old key. This is checked
    # before the fresh login below, because that login legitimately creates a new
    # live session and would mask the termination.
    live = await client.get(f"/api/v1/students/{student_id}/sessions")
    assert live.status_code == 200, live.text
    assert live.json()["items"] == [], "rotation must terminate the old student session"
    assert (await _live_sessions(student_id)) == []
    assert (await student_session.get("/api/v1/auth/me/student")).status_code == 401

    fresh = session_factory()
    await fresh.login_student(new_key)
    assert (await fresh.get("/api/v1/auth/me/student")).status_code == 200

    tracked = await client.get(f"/api/v1/students/{student_id}/sessions")
    assert len(tracked.json()["items"]) == 1, (
        "the new login is the only live session after rotation"
    )


async def test_revocation_removes_the_key_and_ends_sessions(client, session_factory):
    student_id, key = await _create_student(client, "Alan", "Turing", "alan")
    student_session = session_factory()
    await student_session.login_student(key)

    out = await client.post(f"/api/v1/students/{student_id}/access-key/revoke")
    assert out.status_code == 200, out.text
    assert out.json() == {"ok": True, "revoked_keys": 1}
    assert await _active_keys(student_id) == []

    denied = await session_factory()._c.post(
        "/api/v1/auth/student/login", json={"access_key": key}
    )
    assert denied.status_code == 401, denied.text
    assert (await student_session.get("/api/v1/auth/me/student")).status_code == 401
    assert await _live_sessions(student_id) == []

    # idempotent: nothing left to revoke
    again = await client.post(f"/api/v1/students/{student_id}/access-key/revoke")
    assert again.status_code == 200, again.text
    assert again.json()["revoked_keys"] == 0


async def test_disable_terminates_sessions_and_reactivation_restores_access(
    client, session_factory
):
    student_id, key = await _create_student(client, "Katherine", "Johnson", "katherine")
    student_session = session_factory()
    await student_session.login_student(key)
    assert (await student_session.get("/api/v1/auth/me/student")).status_code == 200

    off = await client.post(f"/api/v1/students/{student_id}/status", json={"status": "disabled"})
    assert off.status_code == 200, off.text
    assert await _live_sessions(student_id) == [], "disabling must terminate live sessions"
    assert (await student_session.get("/api/v1/auth/me/student")).status_code == 401

    # A valid key on a disabled account is forbidden, not merely unauthenticated.
    blocked = await session_factory()._c.post(
        "/api/v1/auth/student/login", json={"access_key": key}
    )
    assert blocked.status_code == 403, blocked.text
    assert error_of(blocked)["code"] == "forbidden"

    on = await client.post(f"/api/v1/students/{student_id}/status", json={"status": "active"})
    assert on.status_code == 200, on.text
    row = await _student_row(student_id)
    assert row.deleted_at is None and row.archived_at is None, (
        "reactivation must clear the soft-delete markers or the student stays hidden"
    )
    revived = session_factory()
    await revived.login_student(key)
    assert (await revived.get("/api/v1/auth/me/student")).status_code == 200


async def test_archiving_hides_a_student_without_deleting_rows(client, session_factory):
    student_id, key = await _create_student(client, "Jean", "Bartik", "jean")
    r = await client.post(f"/api/v1/students/{student_id}/status", json={"status": "archived"})
    assert r.status_code == 200, r.text

    row = await _student_row(student_id)
    assert row.deleted_at is not None and row.archived_at is not None
    assert await _key_rows(student_id), "archiving is a soft delete: the key history stays"

    gone = await client.get(f"/api/v1/students/{student_id}")
    assert gone.status_code == 404, gone.text
    listed = (await client.get("/api/v1/students")).json()["items"]
    assert student_id not in [i["id"] for i in listed]

    denied = await session_factory()._c.post(
        "/api/v1/auth/student/login", json={"access_key": key}
    )
    assert denied.status_code == 403, denied.text


async def test_suggest_username_avoids_existing_collisions(client):
    first = await client.get("/api/v1/students/suggest-username", params={"name": "Ada", "surname": "L"})
    assert first.status_code == 200, first.text
    assert first.json()["username"] == "adal"

    await _create_student(client, "Ada", "L", "adal")
    second = await client.get("/api/v1/students/suggest-username", params={"name": "Ada", "surname": "L"})
    assert second.json()["username"] == "adal1"

    # The sequence has no gaps: `adal1` is offered before `adal2` is ever reached.
    await _create_student(client, "Ada", "L", "adal1")
    third = await client.get("/api/v1/students/suggest-username", params={"name": "Ada", "surname": "L"})
    assert third.json()["username"] == "adal2"


async def test_unauthenticated_admin_routes_require_a_session(session_factory):
    """401 - never a 403 CSRF result or a 500 - when no session cookie is present."""
    c = session_factory()
    for path in ("/api/v1/students", "/api/v1/groups", "/api/v1/auth/me/admin"):
        r = await c.get(path)
        assert r.status_code == 401, f"{path} -> {r.status_code} {r.text}"
        assert error_of(r)["code"] == "unauthorized", path

    created = await c._c.post(
        "/api/v1/students", json={"name": "N", "surname": "S", "username": "no-session"}
    )
    assert created.status_code == 401, created.text


async def test_one_active_key_per_student_is_enforced_by_the_database(db_ready):
    """S8: the invariant lives in the schema, so no Python path can bypass it."""
    async with SessionLocal() as db:
        student = Student(name="Db", surname="Guard", username="db-guard-key")
        db.add(student)
        await db.flush()

        def _row() -> StudentAccessKey:
            return StudentAccessKey(
                student_id=student.id,
                key_hash=security.hash_secret("whatever"),
                key_prefix="db-guard@abcdef",
                status=enums.AccessKeyStatus.ACTIVE,
            )

        db.add(_row())
        await db.flush()
        assert (
            await db.execute(
                select(func.count())
                .select_from(StudentAccessKey)
                .where(
                    StudentAccessKey.student_id == student.id,
                    StudentAccessKey.status == enums.AccessKeyStatus.ACTIVE,
                )
            )
        ).scalar_one() == 1

        db.add(_row())
        with pytest.raises(IntegrityError):
            await db.flush()
