"""S3/MinIO-compatible object storage adapter.

Uses generated storage keys only - never trusts or stores user filenames as the
object key (see MEDIA AND SOURCE SECURITY in the spec).

Two read paths exist on purpose. `get_bytes` is for small objects the server needs in
full (a checksum, a crop); `open_range` is for playing media, where an answer has to be
streamed and a client that asks for bytes 4-7 million must not make the server fetch the
whole file. The application, not the browser, stays in front of every object: there is
no public bucket and no shared link anywhere in here, because a URL that carries
credentials would outlive the lesson it was made for.
"""
from __future__ import annotations

import io
import os
import re
import secrets
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import BinaryIO, Iterator

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings

_settings = get_settings()

_SAFE_EXT = re.compile(r"^[A-Za-z0-9]{1,10}$")
_SAFE_NAMESPACE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: How much of an object a single streamed read asks for. Small enough that a stalled
#: download never holds a connection on a huge slice, large enough to keep the request
#: count down for a two-minute recording.
_CHUNK_BYTES = 1 << 20

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

#: How long a stored display name may be, which is the width of the column holding it.
DISPLAY_NAME_LIMIT = 500


def safe_display_name(raw: str | None, *, limit: int = DISPLAY_NAME_LIMIT) -> str | None:
    """The uploaded filename, kept only as a label a teacher can recognise.

    One rule for every upload this platform stores, because the name is the field a
    client controls completely: it never reaches a path, so separators, control
    characters and an over-long name are gone before a row is written. Callers that
    store a file use this and get their key from `ObjectStorage.build_key` instead.
    """
    if not raw:
        return None
    basename = os.path.basename(raw.replace("\\", "/"))
    cleaned = _CONTROL_CHARS.sub(" ", basename).strip()
    return cleaned[:limit] or None


class ObjectMissing(Exception):
    """The bucket has no object at this key.

    A typed error instead of botocore's, because the code that answers a media request
    has to tell "this asset's file is gone" apart from "the store is down" without
    importing a cloud SDK, and the first one is a 404 for the teacher while the second
    is a server fault they must not be shown as a missing file.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"no object stored at {key!r}")
        self.key = key


class StorageUnavailable(Exception):
    """The bucket could not be reached, or refused the call.

    Not the same as `ObjectMissing`: telling a teacher "this file does not exist" when
    MinIO is down would send them to re-upload a file that is sitting there perfectly.
    """


def _is_missing(exc: ClientError) -> bool:
    """Whether a botocore error is the store's 404 rather than a real fault."""
    return str(exc.response.get("Error", {}).get("Code", "")) in {
        "404",
        "NoSuchKey",
        "NotFound",
    }


@contextmanager
def _translated(key: str) -> Iterator[None]:
    """Raise this module's two outcomes, never the SDK's exception types."""
    try:
        yield
    except ClientError as exc:
        if _is_missing(exc):
            raise ObjectMissing(key) from exc
        raise StorageUnavailable(f"storage rejected {key!r}: {exc}") from exc
    except BotoCoreError as exc:
        raise StorageUnavailable(f"storage unreachable for {key!r}: {exc}") from exc


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    content_type: str
    size: int


@dataclass(frozen=True)
class ObjectHead:
    """What the store says about an object without transferring it."""

    size: int
    content_type: str | None


@dataclass(frozen=True)
class OpenedRange:
    """One slice of an object, ready to be handed to a response.

    `total_size` is the whole object's length, which is what a `Content-Range` header
    needs and cannot be derived from the slice itself."""

    stream: BinaryIO
    start: int
    length: int
    total_size: int
    content_type: str | None


class ObjectStorage:
    def __init__(self) -> None:
        self._client = boto3.client(
            "s3",
            endpoint_url=_settings.object_storage_endpoint,
            region_name=_settings.object_storage_region,
            aws_access_key_id=_settings.object_storage_access_key,
            aws_secret_access_key=_settings.object_storage_secret_key,
            use_ssl=_settings.object_storage_secure,
            config=BotoConfig(signature_version="s3v4"),
        )
        self._bucket = _settings.object_storage_bucket
        self._bucket_ready = False
        self._bucket_lock = threading.Lock()

    def _prepare_write(self) -> None:
        """Create the bucket on the first write, because a fresh install has none.

        MinIO starts empty and the compose stack deliberately creates no bucket: without
        this, the first upload on a new installation is answered with a 503 saying the
        store is unreachable, which is not what happened - the store answered perfectly,
        there was just nowhere to put the file. One check per object, and a store that
        refuses the create still raises, so the caller answers for it.
        """
        with self._bucket_lock:
            if not self._bucket_ready:
                self.ensure_bucket()
                self._bucket_ready = True

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    def bucket_exists(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except ClientError:
            return False

    @staticmethod
    def build_key(namespace: str, original_filename: str) -> str:
        """Generate an opaque storage key; a plain alphanumeric extension is kept
        only as a content-type hint.

        The filename never contributes path separators or arbitrary text: an
        upload called ``../../etc/passwd`` must not steer the object key.
        """
        if not _SAFE_NAMESPACE.match(namespace):
            raise ValueError(f"unsafe storage namespace: {namespace!r}")
        ext = ""
        basename = original_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if "." in basename:
            candidate = basename.rsplit(".", 1)[-1]
            if _SAFE_EXT.match(candidate):
                ext = "." + candidate.lower()
        return f"{namespace}/{secrets.token_hex(16)}{ext}"

    def put_bytes(
        self, namespace: str, original_filename: str, data: bytes, content_type: str
    ) -> StoredObject:
        key = self.build_key(namespace, original_filename)
        with _translated(key):
            self._prepare_write()
            self._client.upload_fileobj(
                io.BytesIO(data),
                self._bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        return StoredObject(self._bucket, key, content_type, len(data))

    def put_file(
        self, key: str, source: BinaryIO, content_type: str, size: int
    ) -> StoredObject:
        """Store an already-opened file under a key chosen by the caller.

        The caller resolved the key (and proved the bytes are what it claims) so the
        upload never has to sit in memory: `upload_fileobj` copies in parts. `size` is
        passed through for the return value only - the store trusts its own count.
        """
        source.seek(0)
        with _translated(key):
            self._prepare_write()
            self._client.upload_fileobj(
                source,
                self._bucket,
                key,
                ExtraArgs={"ContentType": content_type},
            )
        return StoredObject(self._bucket, key, content_type, size)

    def get_bytes(self, key: str) -> bytes:
        buf = io.BytesIO()
        with _translated(key):
            self._client.download_fileobj(self._bucket, key, buf)
        return buf.getvalue()

    def head(self, key: str) -> ObjectHead:
        """What the store says about an object, without transferring it.

        Raises `ObjectMissing` when the key names nothing: a media row whose file has
        gone is a broken library entry, and the reader has to be able to say that.
        """
        with _translated(key):
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        return ObjectHead(
            size=int(response.get("ContentLength") or 0),
            content_type=response.get("ContentType"),
        )

    def open_range(self, key: str, start: int, end: int) -> OpenedRange:
        """Open bytes ``start..end`` inclusive, as a readable stream.

        A missing object raises `ObjectMissing`, which the caller tells apart from "the
        range runs past the end"; the store clamps nothing and reports no invented
        length.
        """
        with _translated(key):
            response = self._client.get_object(
                Bucket=self._bucket, Key=key, Range=f"bytes={start}-{end}"
            )
        content_range = str(response.get("ContentRange") or "")
        total = 0
        if "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[-1])
            except ValueError:
                total = 0
        body = response["Body"]
        length = int(response.get("ContentLength") or 0)
        if not total:
            total = start + length
        return OpenedRange(
            stream=body,
            start=start,
            length=length,
            total_size=total,
            content_type=response.get("ContentType"),
        )

    def iter_range(self, opened: OpenedRange) -> Iterator[bytes]:
        """Yield an opened slice in fixed-size chunks and always close its socket.

        Chunking happens here rather than in the endpoint so a caller cannot forget the
        close and leave a connection from the response generator onto MinIO.
        """
        try:
            while True:
                chunk = opened.stream.read(_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        finally:
            opened.stream.close()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            if _is_missing(exc):
                return False
            raise

    def presigned_get(self, key: str, expires_seconds: int = 900) -> str:
        """A link that carries its own authorisation for a while.

        Kept for server-side jobs that must hand a file to another service; no
        browser-facing response ever returns one, because a media URL outlives the tab
        it was opened in and a link that needs no login is not a lesson's worth of access
        but a permanent one."""
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    def delete(self, key: str) -> None:
        with _translated(key):
            self._client.delete_object(Bucket=self._bucket, Key=key)


_storage: ObjectStorage | None = None


def get_storage() -> ObjectStorage:
    global _storage
    if _storage is None:
        _storage = ObjectStorage()
    return _storage
