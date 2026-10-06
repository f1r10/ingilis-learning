"""Live MinIO/S3 verification through the application's own storage abstraction.

Everything here goes through `app.core.storage.ObjectStorage` (the class the rest
of the backend will use for media/source files), not raw HTTP against the MinIO
endpoint: bucket init/check, upload, retrieve + byte comparison, presigned read,
delete and confirmed deletion. It skips cleanly when the bucket is unreachable
and is a hard requirement inside `make verify-phase12`.
"""
from __future__ import annotations

import os
from secrets import token_bytes

import pytest
from botocore.exceptions import ClientError

from app.core.storage import ObjectStorage, get_storage

NAMESPACE = "integration-verify"


@pytest.fixture
def store(storage_ready: None) -> ObjectStorage:
    del storage_ready  # skip when MinIO/S3 is unreachable
    storage = get_storage()
    storage.ensure_bucket()
    return storage


def test_ensure_bucket_initialises_and_is_visible(store: ObjectStorage) -> None:
    store.ensure_bucket()  # idempotent: existing bucket must not error
    assert store.bucket_exists() is True


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
    with pytest.raises(ClientError):
        store.get_bytes(obj.key)


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
