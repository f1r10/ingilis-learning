"""App smoke tests against the real create_app(): route wiring, health endpoint,
and the middleware ordering guarantee that CORS headers wrap even a CSRF 403.

None of these hit Postgres/Redis: /healthz is DB-free, and CSRF short-circuits
before any get_db dependency resolves."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core import constants, security
from app.core.config import get_settings
from app.main import create_app


def test_healthz_and_openapi_routes_present():
    c = TestClient(create_app())
    assert c.get("/healthz").status_code == 200

    paths = c.get("/openapi.json").json()["paths"]
    for p in [
        "/api/v1/auth/bootstrap",
        "/api/v1/auth/admin/login",
        "/api/v1/auth/student/login",
        "/api/v1/auth/logout",
        "/api/v1/students",
        "/api/v1/groups",
        "/api/v1/groups/{group_id}/members",
        "/api/v1/groups/{group_id}/members/{student_id}",
        "/api/v1/settings/branding",
        # Phase 3: the question bank and its taxonomy.
        "/api/v1/questions",
        "/api/v1/questions/{question_id}",
        "/api/v1/topics",
        "/api/v1/tags",
        # Phase 4: both vocabulary surfaces. A router that is written but never
        # included looks identical in a grep, so this is where it gets caught.
        "/api/v1/vocabulary",
        "/api/v1/vocabulary/{entry_id}",
        "/api/v1/vocabulary/{entry_id}/preview",
        "/api/v1/vocabulary/{entry_id}/restore",
        "/api/v1/vocabulary/{entry_id}/status",
        "/api/v1/vocabulary/{entry_id}/taxonomy",
        "/api/v1/vocabulary/bulk",
        "/api/v1/vocabulary/meta",
        "/api/v1/student/vocabulary",
        "/api/v1/student/vocabulary/meta",
        "/api/v1/student/vocabulary/{entry_id}",
        # Phase 5: the media library and the one route a learner may read a file from.
        "/api/v1/media",
        "/api/v1/media/{asset_id}",
        "/api/v1/media/{asset_id}/content",
        "/api/v1/media/{asset_id}/restore",
        "/api/v1/media/bulk",
        "/api/v1/media/meta",
        "/api/v1/student/media/{asset_id}/content",
        # Phase 5: both passage editors, their set routes, and both learner surfaces.
        "/api/v1/reading",
        "/api/v1/reading/{reading_id}",
        "/api/v1/reading/{reading_id}/preview",
        "/api/v1/reading/{reading_id}/restore",
        "/api/v1/reading/{reading_id}/status",
        "/api/v1/reading/{reading_id}/sets",
        "/api/v1/reading/{reading_id}/sets/reorder",
        "/api/v1/reading/sets/{set_id}",
        "/api/v1/reading/sets/{set_id}/questions",
        "/api/v1/reading/bulk",
        "/api/v1/reading/meta",
        "/api/v1/student/reading",
        "/api/v1/student/reading/meta",
        "/api/v1/student/reading/{reading_id}",
        "/api/v1/listening",
        "/api/v1/listening/{listening_id}",
        "/api/v1/listening/{listening_id}/preview",
        "/api/v1/listening/{listening_id}/restore",
        "/api/v1/listening/{listening_id}/status",
        "/api/v1/listening/{listening_id}/sets",
        "/api/v1/listening/{listening_id}/sets/reorder",
        "/api/v1/listening/sets/{set_id}",
        "/api/v1/listening/sets/{set_id}/questions",
        "/api/v1/listening/bulk",
        "/api/v1/listening/meta",
        "/api/v1/student/listening",
        "/api/v1/student/listening/meta",
        "/api/v1/student/listening/{listening_id}",
    ]:
        assert p in paths, p


def test_a_set_is_addressed_by_its_own_id():
    """The set routes hang off `/reading/sets/{set_id}`, not off a passage id.

    A browser dragging one question between blocks knows the set it is holding, and the
    passage key is on the set row. Making it name the text above the set as well would be
    a second way to say the same thing - and a route shaped `/reading/{id}/sets/{set_id}`
    would let it say two different things.
    """
    paths = TestClient(create_app()).get("/openapi.json").json()["paths"]
    for name in ("reading", "listening"):
        set_routes = [p for p in paths if p.startswith(f"/api/v1/{name}/sets/")]
        assert set(set_routes) == {
            f"/api/v1/{name}/sets/{{set_id}}",
            f"/api/v1/{name}/sets/{{set_id}}/questions",
        }, sorted(set_routes)
        assert {"patch", "delete"} <= set(paths[f"/api/v1/{name}/sets/{{set_id}}"])
        assert {"post"} == set(paths[f"/api/v1/{name}/sets/{{set_id}}/questions"])


def test_a_listening_block_carries_its_slice_of_the_recording():
    """The interval is the shape that makes a listening block different from a reading one.

    It is created and patched through the shared set routes, so the two request bodies -
    and not some client-side guess - are what a teacher's "play 0:40 to 1:10" arrives as.
    """
    schemas = TestClient(create_app()).get("/openapi.json").json()["components"]["schemas"]
    assert {"start_seconds", "end_seconds"} <= set(schemas["ListeningSetCreate"]["properties"])
    assert {"start_seconds", "end_seconds"} <= set(schemas["ListeningSetUpdate"]["properties"])
    assert "start_seconds" not in schemas["QuestionSetCreate"]["properties"]


def test_the_student_vocabulary_surface_is_read_only():
    """A learner may browse cards and nothing else.

    The student routers must not grow a write method: the word bank and the passage
    library belong to the teacher, and a route that lets a student edit them would be a
    permission bug shipped as an API.
    """
    paths = TestClient(create_app()).get("/openapi.json").json()["paths"]
    read_only = ("/api/v1/student/vocabulary", "/api/v1/student/reading", "/api/v1/student/listening")
    for path, operations in paths.items():
        if not path.startswith("/api/v1/student/"):
            continue
        for method in operations:
            assert method in ("get", "post"), f"{method.upper()} {path} is not a read for a student"
        if path.startswith(read_only):
            assert set(operations) == {"get"}, f"{path} exposes {sorted(operations)} to a learner"


def test_a_learner_may_only_open_a_file_a_published_exercise_points_at():
    """The student media surface is one route, and it is a read of bytes.

    There is deliberately no `/api/v1/student/media` listing: the teacher's library -
    its filenames, its captions, its reference counts and how many recordings exist at
    all - is not learner-facing. A route that added one would hand out the shape of the
    bank to anyone with a student key.
    """
    paths = TestClient(create_app()).get("/openapi.json").json()["paths"]
    student_media = {path: operations for path, operations in paths.items()
                     if path.startswith("/api/v1/student/media")}
    assert set(student_media) == {"/api/v1/student/media/{asset_id}/content"}, sorted(student_media)
    assert set(student_media["/api/v1/student/media/{asset_id}/content"]) == {"get"}


def test_the_media_upload_accepts_multipart_and_nothing_else():
    """An upload route wired for JSON would answer every teacher with a 422.

    `multipart/form-data` is the only media type the route documents, because the bytes
    arrive as a file part: the type of a stored file is read from them, never declared
    by the browser that sent them.
    """
    paths = TestClient(create_app()).get("/openapi.json").json()["paths"]
    body = paths["/api/v1/media"]["post"]["requestBody"]["content"]
    assert set(body) == {"multipart/form-data"}, sorted(body)


def test_cors_wraps_csrf_403():
    c = TestClient(create_app())
    sid = "33333333-3333-3333-3333-333333333333"
    token = security.create_session_token(security.SubjectType.ADMIN, sid, epoch=0)
    frontend_origin = get_settings().frontend_origin
    r = c.put(
        "/api/v1/settings/branding",
        cookies={constants.SESSION_COOKIE: token},
        headers={constants.CSRF_HEADER: "bad", "Origin": frontend_origin},
        json={"values": {"system_name": "X"}},
    )
    # CSRF blocks it (403) BEFORE the DB dependency, and CORS still decorates it.
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "csrf_failed"
    assert r.headers.get("access-control-allow-origin") == frontend_origin
    assert r.headers.get("access-control-allow-credentials") == "true"
