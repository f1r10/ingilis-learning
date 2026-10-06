"""End-to-end flows against real Postgres (+ Redis for one test).

Skips automatically if the services are down (via the client / redis fixtures)."""
from __future__ import annotations

import uuid


async def test_bootstrap_is_public(client):
    r = await client.get("/api/v1/auth/bootstrap")
    assert r.status_code == 200
    body = r.json()
    assert "branding" in body and "ui" in body


async def test_admin_login_me_logout_epoch(client):
    # Invalid credentials rejected.
    bad = await client._c.post("/api/v1/auth/admin/login", json={"username": "admin", "password": "nope"})
    assert bad.status_code == 401

    await client.login_admin()
    me = await client.get("/api/v1/auth/me/admin")
    assert me.status_code == 200
    assert me.json()["username"] == "admin"

    # Logout bumps the admin session_epoch; the OLD cookie must no longer auth.
    out = await client.post("/api/v1/auth/logout")
    assert out.status_code == 200
    me_after = await client.get("/api/v1/auth/me/admin")
    assert me_after.status_code == 401


async def test_stateful_without_csrf_is_blocked(client):
    await client.login_admin()
    # Bypass AuthedClient's CSRF replay to prove the middleware guards real routes.
    r = await client._c.post("/api/v1/groups", json={"name": "No CSRF"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"


async def test_student_lifecycle_and_keys(client):
    await client.login_admin()

    # Create a student; access key is returned once.
    created = await client.post(
        "/api/v1/students",
        json={"name": "Ada", "surname": "Lovelace", "username": "ada"},
    )
    assert created.status_code == 201, created.text
    sid = created.json()["student"]["id"]
    key1 = created.json()["access_key"]

    listed = await client.get("/api/v1/students")
    assert any(i["id"] == sid for i in listed.json()["items"])

    # Rotate: a new key works, the previous key is dead.
    rotated = await client.post(f"/api/v1/students/{sid}/access-key/rotate")
    assert rotated.status_code == 200
    key2 = rotated.json()["access_key"]
    assert key2 != key1

    old_rejected = await client._c.post("/api/v1/auth/student/login", json={"access_key": key1})
    assert old_rejected.status_code == 401

    # Disable the student -> cannot log in even with a valid key.
    await client.post(f"/api/v1/students/{sid}/status", json={"status": "disabled"})
    disabled = await client._c.post("/api/v1/auth/student/login", json={"access_key": key2})
    assert disabled.status_code == 403

    # Reactivate -> clears soft-delete markers and login works again.
    await client.post(f"/api/v1/students/{sid}/status", json={"status": "active"})
    # re-login as admin (previous student login attempt left no admin session change,
    # but the disabled attempt used the public client which shares cookies; relogin to be safe)
    await client.login_admin()
    ok = await client._c.post("/api/v1/auth/student/login", json={"access_key": key2})
    assert ok.status_code == 200


async def test_group_membership_flow(client):
    await client.login_admin()
    student = (await client.post("/api/v1/students", json={"name": "Grace", "surname": "Hopper", "username": "grace"})).json()
    sid = student["student"]["id"]

    group = await client.post("/api/v1/groups", json={"name": "IELTS 2026"})
    assert group.status_code == 201
    gid = group.json()["id"]

    add = await client.post(f"/api/v1/groups/{gid}/members", json={"student_id": sid})
    assert add.status_code == 201, add.text

    # Duplicate add is rejected.
    dup = await client.post(f"/api/v1/groups/{gid}/members", json={"student_id": sid})
    assert dup.status_code == 409

    members = await client.get(f"/api/v1/groups/{gid}/members")
    assert any(m["id"] == sid for m in members.json()["items"])

    remove = await client.delete(f"/api/v1/groups/{gid}/members/{sid}")
    assert remove.status_code == 200
    members_after = await client.get(f"/api/v1/groups/{gid}/members")
    assert all(m["id"] != sid for m in members_after.json()["items"])


async def test_create_student_rejects_unknown_group(client):
    await client.login_admin()
    bogus = str(uuid.uuid4())
    r = await client.post(
        "/api/v1/students",
        json={"name": "E", "surname": "F", "username": "ef", "group_ids": [bogus]},
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


async def test_login_rate_limit_trips(client, redis_ready):
    # Admin login is limited to 8 attempts / 60s per username. Hammer it.
    codes = []
    for _ in range(12):
        r = await client._c.post("/api/v1/auth/admin/login", json={"username": "ratelimited", "password": "x"})
        codes.append(r.status_code)
    assert 429 in codes
