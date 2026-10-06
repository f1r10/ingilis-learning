"""S3/MinIO-compatible object storage adapter.

Uses generated storage keys only - never trusts or stores user filenames as the
object key (see MEDIA AND SOURCE SECURITY in the spec).
"""
from __future__ import annotations

import io
import re
import secrets
from dataclasses import dataclass

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from app.core.config import get_settings

_settings = get_settings()

_SAFE_EXT = re.compile(r"^[A-Za-z0-9]{1,10}$")
_SAFE_NAMESPACE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    key: str
    content_type: str
    size: int


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
        self._client.upload_fileobj(
            io.BytesIO(data),
            self._bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )
        return StoredObject(self._bucket, key, content_type, len(data))

    def get_bytes(self, key: str) -> bytes:
        buf = io.BytesIO()
        self._client.download_fileobj(self._bucket, key, buf)
        return buf.getvalue()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def presigned_get(self, key: str, expires_seconds: int = 900) -> str:
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_seconds,
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)


_storage: ObjectStorage | None = None


def get_storage() -> ObjectStorage:
    global _storage
    if _storage is None:
        _storage = ObjectStorage()
    return _storage
