"""Cookie and header name constants shared across backend and documented for the frontend."""

SESSION_COOKIE = "llp_session"
CSRF_COOKIE = "llp_csrf"
CSRF_HEADER = "X-CSRF-Token"
LANG_HEADER = "Accept-Language"

#: The CEFR bands every content screen offers - a word, a question, a reading text and a
#: recording are all filed on the same scale, so the picker is one list rather than four
#: that drift apart. Free text is still accepted for a band this list has not caught up
#: with, because refusing a teacher's real content is worse than a longer filter list.
CEFR_LEVELS = ["A1", "A2", "B1", "B2", "C1", "C2"]

#: Where the bytes of a media asset are read from, as a path under the API root.
#: Two paths, one per audience, because the two surfaces authorise differently: a
#: teacher may open any asset in the library, a learner may only open the ones a
#: published exercise points at (see `media_service.served_to_student`).
TEACHER_MEDIA_CONTENT_PATH = "/media"
STUDENT_MEDIA_CONTENT_PATH = "/student/media"


def media_content_url(asset_id: object, *, for_student: bool) -> str:
    """The one place a media URL is written, so a payload can never invent its own.

    Every browser-facing response carries this path rather than a storage key and rather
    than a link that authorises itself: the object store's address, bucket and
    credentials stay on the server, and access to a file is decided per request by the
    session that asks for it.
    """
    base = STUDENT_MEDIA_CONTENT_PATH if for_student else TEACHER_MEDIA_CONTENT_PATH
    return f"{base}/{asset_id}/content"
