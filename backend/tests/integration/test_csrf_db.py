"""CSRF protection under real authenticated requests (spec S7).

Every case here goes through a live session cookie, so it exercises the actual
double-submit contract instead of a stubbed header: missing token, wrong token,
another session's token, correct token, safe methods, and the public login that
must stay reachable without a token.
"""
from __future__ import annotations

from helpers import error_of

GROUPS = "/api/v1/groups"
CSRF_HEADER = "X-CSRF-Token"


async def test_missing_token_blocks_a_state_changing_request(client):
    await client.login_admin()
    assert client._c.cookies.get("llp_csrf"), "login must issue a readable CSRF cookie"

    blocked = await client._c.post(GROUPS, json={"name": "No CSRF header"})
    assert blocked.status_code == 403, blocked.text
    assert error_of(blocked)["code"] == "csrf_failed"
    assert blocked.json()["error"]["message"], "the envelope must stay uniform"

    # the request never reached the handler
    items = (await client.get(GROUPS)).json()["items"]
    assert "No CSRF header" not in [g["name"] for g in items]


async def test_wrong_token_and_foreign_token_are_both_rejected(client, session_factory):
    await client.login_admin()

    bogus = await client._c.post(
        GROUPS, json={"name": "Bogus token"}, headers={CSRF_HEADER: "not-a-real-token"}
    )
    assert bogus.status_code == 403, bogus.text
    assert error_of(bogus)["code"] == "csrf_failed"

    # A token minted for a different subject must not pass for this session: the
    # double-submit value is bound to the session id, not merely "a valid token".
    student = session_factory()
    created = await client.post(
        "/api/v1/students", json={"name": "Csrf", "surname": "Other", "username": "csrf-other"}
    )
    assert created.status_code == 201, created.text
    await student.login_student(created.json()["access_key"])

    foreign = student._c.cookies.get("llp_csrf")
    assert foreign and foreign != client._c.cookies.get("llp_csrf")

    replayed = await client._c.post(
        GROUPS, json={"name": "Foreign token"}, headers={CSRF_HEADER: foreign}
    )
    assert replayed.status_code == 403, replayed.text
    assert error_of(replayed)["code"] == "csrf_failed"

    # ...while the admin's own token still works.
    assert (await client.post(GROUPS, json={"name": "Own token"})).status_code == 201


async def test_correct_token_lets_the_request_through(client):
    await client.login_admin()
    created = await client.post(GROUPS, json={"name": "CSRF OK"})
    assert created.status_code == 201, created.text
    gid = created.json()["id"]

    renamed = await client.patch(f"{GROUPS}/{gid}", json={"name": "CSRF OK 2"})
    assert renamed.status_code == 200, renamed.text

    removed = await client.delete(f"{GROUPS}/{gid}/members/11111111-1111-1111-1111-111111111111")
    assert removed.status_code == 404, removed.text  # guarded, then handled normally


async def test_safe_methods_and_public_login_stay_usable(client, session_factory):
    await client.login_admin()
    # GET needs no token.
    assert (await client._c.get(GROUPS, headers={})).status_code == 200

    # The login route is exempt by design: it is how a token is obtained.
    anonymous = session_factory()
    r = await anonymous._c.post(
        "/api/v1/auth/admin/login", json={"username": "nobody", "password": "wrong-pass-1"}
    )
    assert r.status_code == 401, r.text
    assert error_of(r)["code"] == "unauthorized", "a login attempt must not be CSRF-blocked"


async def test_a_sessionless_state_changing_request_is_unauthorized_not_csrf_blocked(
    client, session_factory
):
    """Order matters: no session -> 401 from auth, not a 403 from the CSRF guard."""
    await client.login_admin()
    out = await client.post("/api/v1/auth/logout")
    assert out.status_code == 200, out.text

    bare = session_factory()
    no_cookie = await bare._c.post(GROUPS, json={"name": "Anonymous write"})
    assert no_cookie.status_code == 401, no_cookie.text
    assert error_of(no_cookie)["code"] == "unauthorized"


async def test_student_sessions_are_guarded_the_same_way(client, session_factory):
    created = await client.post(
        "/api/v1/students", json={"name": "Csrf", "surname": "Student", "username": "csrf-student"}
    )
    assert created.status_code == 201, created.text
    key = created.json()["access_key"]

    student = session_factory()
    await student.login_student(key)
    assert (await student.get("/api/v1/auth/me/student")).status_code == 200

    blocked = await student._c.post("/api/v1/auth/logout")
    assert blocked.status_code == 403, blocked.text
    assert error_of(blocked)["code"] == "csrf_failed"

    allowed = await student.post("/api/v1/auth/logout")
    assert allowed.status_code == 200, allowed.text
