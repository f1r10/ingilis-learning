"""Storage key generation is a security boundary: unit tests need no MinIO.

`ObjectStorage.build_key` is pure, so the opaque-key contract (and the
path-traversal defect it previously had) is covered in the fast suite rather than
only in the live MinIO round-trip test.
"""
from __future__ import annotations

import re

import pytest

from app.core.storage import ObjectStorage

_TOKEN_RE = re.compile(r"^[a-z_]+/[0-9a-f]{32}(\.[a-z0-9]{1,10})?$")


def test_key_is_opaque_and_never_contains_the_filename() -> None:
    key = ObjectStorage.build_key("media", "final exam AUDIO.mp3")
    assert key.startswith("media/")
    assert "exam" not in key
    assert key.endswith(".mp3")
    assert _TOKEN_RE.match(key), key


def test_keys_are_unique_across_calls() -> None:
    keys = {ObjectStorage.build_key("media", "a.png") for _ in range(200)}
    assert len(keys) == 200


@pytest.mark.parametrize(
    "filename",
    [
        "../../etc/passwd",
        "..\\..\\windows\\system32\\config",
        "a/../../b.png",
        "/absolute/path.png",
        "weird name .png",
        "trailing.",
        "",
    ],
)
def test_hostile_filenames_cannot_inject_path_separators(filename: str) -> None:
    key = ObjectStorage.build_key("media", filename)
    body = key.split("/", 1)[1]
    assert "/" not in body, f"path separator leaked into key: {key}"
    assert ".." not in body, f"traversal leaked into key: {key}"
    assert _TOKEN_RE.match(key), key


def test_overlong_extension_is_dropped_instead_of_trusted() -> None:
    key = ObjectStorage.build_key("media", "file.averyveryveryverylongext")
    assert re.match(r"^media/[0-9a-f]{32}$", key), key


@pytest.mark.parametrize("namespace", ["", "../etc", "a/b", "media ; rm", "x" * 40])
def test_unsafe_namespace_is_rejected(namespace: str) -> None:
    with pytest.raises(ValueError):
        ObjectStorage.build_key(namespace, "photo.png")
