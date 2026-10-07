"""Live MinIO/S3 verification through the application's own storage abstraction.

Everything here goes through `app.core.storage.ObjectStorage` (the class the rest
of the backend will use for media/source files), not raw HTTP against the MinIO
endpoint: bucket init/check, upload, retrieve + byte comparison, presigned read,
delete and confirmed deletion. It skips cleanly when the bucket is unreachable
and is a hard requirement inside `make verify-phase12`.
"""
from __future__ import annotations

import contextlib
import io
import os
from secrets import token_bytes

import pytest

from app.core.storage import ObjectMissing, ObjectStorage, get_storage

NAMESPACE = "integration-verify"


@pytest.fixture
def store(storage_ready: None) -> ObjectStorage:
    del storage_ready  # skip when MinIO/S3 is unreachable
    storage = get_storage()
    storage.ensure_bucket()
    return storage


def _drop_scratch_bucket(client, bucket: str) -> None:
    """Delete a bucket this test created, objects and all.

    Only ever called with a name this test invented: the application bucket holds other
    tests' objects and real lesson files, and nothing here is allowed to remove it.
    """
    with contextlib.suppress(Exception):
        listed = client.list_objects_v2(Bucket=bucket).get("Contents", [])
        if listed:
            client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": o["Key"]} for o in listed]})
        client.delete_bucket(Bucket=bucket)


def test_ensure_bucket_initialises_and_is_visible(store: ObjectStorage) -> None:
    store.ensure_bucket()  # idempotent: existing bucket must not error
    assert store.bucket_exists() is True


def test_a_first_write_creates_the_bucket_it_needs(
    storage_ready: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty MinIO must accept the application's first upload.

    The compose stack ships a store with no buckets and nothing outside this module
    creates one, so on a fresh install the very first upload is what has to make room for
    itself. When it does not, the teacher's first file of the day comes back as a 503
    saying the store is unreachable - which is a lie: the store answered perfectly, there
    was simply nowhere to put the file. A fixture that creates the bucket first would
    hide exactly this, so this test builds its client on a bucket name nothing else uses.
    """
    del storage_ready  # skip when MinIO/S3 is unreachable

    from app.core import storage as storage_module
    from app.core.config import get_settings

    scratch = f"verify-fresh-{token_bytes(5).hex()}"
    monkeypatch.setattr(
        storage_module,
        "_settings",
        get_settings().model_copy(update={"object_storage_bucket": scratch}),
    )
    fresh = storage_module.ObjectStorage()
    assert fresh.bucket_exists() is False, f"{scratch!r} already exists: not a fresh install"

    try:
        payload = token_bytes(512)
        obj = fresh.put_bytes(NAMESPACE, "first.bin", payload, "application/octet-stream")
        assert obj.bucket == scratch
        assert fresh.bucket_exists() is True, "the write did not create its own bucket"
        assert fresh.get_bytes(obj.key) == payload

        # The other write path, on a second client, the way a restarted process finds the
        # store: the bucket already exists there, and the upload still has to go through.
        restarted = storage_module.ObjectStorage()
        key = restarted.build_key(NAMESPACE, "clip.mp3")
        restarted.put_file(key, io.BytesIO(payload), "audio/mpeg", len(payload))
        assert restarted.head(key).size == len(payload)
    finally:
        _drop_scratch_bucket(fresh._client, scratch)


def test_upload_retrieve_compare_delete(store: ObjectStorage, storage_keys: list[str]) -> None:
    payload = token_bytes(1024 * 1024) + b"\x00\xff binary tail"
    obj = store.put_bytes(NAMESPACE, "sample audio.MP3", payload, "audio/mpeg")
    storage_keys.append(obj.key)

    assert obj.size == len(payload)
    assert obj.content_type == "audio/mpeg"
    assert obj.bucket == store._bucket
    assert obj.key.startswith(f"{NAMESPACE}/")

    fetched = store.get_bytes(obj.key)
    assert len(fetched) == len(payload)
    assert fetched == payload, "retrieved bytes differ from uploaded bytes"
    assert store.exists(obj.key) is True

    store.delete(obj.key)
    assert store.exists(obj.key) is False
    # The module's own error, not botocore's: the media code has to branch on
    # "this file is gone" without importing a cloud SDK.
    with pytest.raises(ObjectMissing):
        store.get_bytes(obj.key)
    with pytest.raises(ObjectMissing):
        store.head(obj.key)
    with pytest.raises(ObjectMissing):
        store.open_range(obj.key, 0, 10)


def test_presigned_url_serves_the_same_bytes(
    store: ObjectStorage, storage_keys: list[str]
) -> None:
    import httpx

    payload = token_bytes(4096)
    obj = store.put_bytes(NAMESPACE, "clip.png", payload, "image/png")
    storage_keys.append(obj.key)

    url = store.presigned_get(obj.key, expires_seconds=120)
    assert url.startswith(("http://", "https://"))
    response = httpx.get(url, timeout=30.0, follow_redirects=True)
    assert response.status_code == 200, response.text
    assert response.content == payload
    assert response.headers["content-type"] == "image/png", (
        "content type was not stored with the object"
    )


def test_object_survives_client_recreation(store: ObjectStorage, storage_keys: list[str]) -> None:
    """A brand-new client must read what the previous one wrote.

    This is the storage half of restart persistence: nothing lives in process
    memory, the bucket is the only state.
    """
    payload = b"durable-write-" + token_bytes(64)
    obj = store.put_bytes(NAMESPACE, "durable.txt", payload, "text/plain")
    storage_keys.append(obj.key)

    fresh = ObjectStorage()  # emulates a restarted process
    assert fresh.get_bytes(obj.key) == payload
    assert fresh.exists(obj.key) is True


def test_hostile_filename_cannot_steer_the_object_key(
    store: ObjectStorage, storage_keys: list[str]
) -> None:
    payload = token_bytes(256)
    obj = store.put_bytes(NAMESPACE, "../../../../etc/passwd.png", payload, "image/png")
    storage_keys.append(obj.key)

    suffix = obj.key.split("/", 1)[1]
    assert "/" not in suffix and ".." not in suffix, f"key escaped its namespace: {obj.key}"
    assert store.get_bytes(obj.key) == payload


def test_empty_and_large_payloads_roundtrip(
    store: ObjectStorage, storage_keys: list[str]
) -> None:
    for size in (0, 1, 5 * 1024 * 1024):
        payload = token_bytes(size)
        obj = store.put_bytes(NAMESPACE, f"blob-{size}.bin", payload, "application/octet-stream")
        storage_keys.append(obj.key)
        assert store.get_bytes(obj.key) == payload, f"mismatch at {size} bytes"


def test_bucket_settings_are_read_from_configuration() -> None:
    """No hardcoded bucket/endpoint: the abstraction must follow the app settings."""
    from app.core.config import get_settings

    s = get_settings()
    storage = ObjectStorage()
    assert storage._bucket == s.object_storage_bucket
    assert os.environ.get("OBJECT_STORAGE_ENDPOINT") in (None, s.object_storage_endpoint)


def test_delete_of_missing_key_is_not_an_error(store: ObjectStorage) -> None:
    # S3/MinIO deletes are idempotent; the app relies on that for retry-safe cleanup
    store.delete(f"{NAMESPACE}/{token_bytes(16).hex()}")


def test_range_streaming_returns_exactly_the_slice(
    store: ObjectStorage, storage_keys: list[str]
) -> None:
    """`put_file` + `open_range` + `iter_range`, which is how a lesson is played back.

    The media responses are built from these three calls, so the slice boundaries and
    the reported total have to be right here rather than only in a unit test with a
    fake store: MinIO is the one that decides what `bytes=1000-1999` means.
    """
    payload = bytes(range(256)) * 64  # 16 KiB of predictable bytes
    key = store.build_key(NAMESPACE, "clip.mp3")
    storage_keys.append(key)
    store.put_file(key, io.BytesIO(payload), "audio/mpeg", len(payload))

    assert store.head(key).size == len(payload)

    opened = store.open_range(key, 1000, 1999)
    try:
        served = b"".join(store.iter_range(opened))
    finally:
        opened.stream.close()

    assert served == payload[1000:2000]
    assert opened.length == 1000
    # The total comes from the store's `Content-Range`, not from the slice: a response
    # header has to be able to say how long the whole file is.
    assert opened.total_size == len(payload)
    assert opened.content_type == "audio/mpeg"

    whole = store.open_range(key, 0, len(payload) - 1)
    try:
        assert b"".join(store.iter_range(whole)) == payload
    finally:
        whole.stream.close()


def test_a_slice_past_the_end_is_not_silently_empty(
    store: ObjectStorage, storage_keys: list[str]
) -> None:
    """An out-of-range read must fail, not answer with a zero-length success.

    The application clamps ranges itself before asking for one; if it ever gets this
    wrong the store's refusal is what stops a player from being handed an empty 206 and
    a teacher from being told a file is fine.
    """
    import io

    payload = token_bytes(2048)
    key = store.build_key(NAMESPACE, "short.mp3")
    storage_keys.append(key)
    store.put_file(key, io.BytesIO(payload), "audio/mpeg", len(payload))

    from app.core.storage import StorageUnavailable

    # MinIO answers an out-of-range read with `InvalidRange`, which is neither a
    # missing object nor a success: it arrives as the store being unwilling, so no
    # caller can mistake it for "this file is empty".
    with pytest.raises(StorageUnavailable):
        store.open_range(key, 4096, 8192)
