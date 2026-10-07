"""What a media file actually is, decided from its bytes rather than its name.

Phase 5 accepts teacher uploads for the first time, and a browser's
`Content-Type`, a file extension and a filename are all things the person holding the
keyboard controls. The object storage is private and every read goes back out through
this application, so a mislabelled file is not only a presentation bug: serving a
`text/html` body with an image's name, or an SVG (which is a script in disguise) as a
picture, is how a library turns into an attack on whoever opens it later.

So the rule is: identify the bytes, and refuse anything this module does not know.
`application/octet-stream` is never stored, because it is the label that makes a
browser guess, and a file we cannot name is a file this platform has no business
hosting. Signatures cover the formats a language teacher actually has - photos,
scanned-looking bitmaps, and the audio and video containers phones and recorders
produce. Documents (PDF, Word, Excel) belong to the import pipeline in Phase 8 and are
refused here on purpose, which also keeps a ZIP-based document out of the media bank.

Duration and pixel size are deliberately absent from this module. Reading them out of
a container means a codec library, and none is a dependency of this project; inventing
a parser for one format and leaving the rest blank would be worse than being honest
that they are unknown. The browser that plays the file does know, so the teacher
screen reports what its own player measured (see `media_service.report_metadata`).
"""
from __future__ import annotations

from dataclasses import dataclass

#: How many bytes of an upload a caller should hand to `identify` and `refusal_reason`.
#: Every fixed-offset signature below lives inside the first 32 bytes, but the EBML
#: doctype that tells WebM from Matroska sits behind a variable-length header, so the
#: window has to be wider than the signatures themselves. 4 KiB is that window and the
#: amount a caller reads; nothing in this module scans further.
HEAD_BYTES = 4096


@dataclass(frozen=True)
class MediaFormat:
    """One accepted media format: the label to store, the kind to file it under,
    the canonical extension, and the plain name a teacher recognises."""

    mime: str
    kind: str  # image | audio | video
    extension: str
    label: str


#: Files that get a sentence of their own instead of the generic refusal, because the
#: teacher made a distinguishable mistake: putting a document in the media library is
#: not the same problem as a phone producing an exotic container. The longest signature
#: comes first, since `MZ` prefixes nothing else here while `Rar!` and the ZIP entries
#: are six-byte and four-byte patterns that no shorter one shadows.
_REFUSED: tuple[tuple[bytes, str], ...] = (
    (b"%PDF", "A PDF is a document, and documents are added through the import screen."),
    (
        b"\x50\x4b\x03\x04",
        "This file is an archive or an office document. Documents are added through the "
        "import screen, not the media library.",
    ),
    (b"Rar!\x1a\x07", "This file is a RAR archive, not media."),
    (b"7z\xbc\xaf\x27\x1c", "This file is a 7-Zip archive, not media."),
    (b"\x1f\x8b", "This file is gzip-compressed, not media."),
    (b"\x7f\x45\x4c\x46", "This file is a program, not media."),
    (b"MZ", "This file is a Windows program, not media."),
)


def _starts_with(head: bytes, prefix: bytes) -> bool:
    return head[: len(prefix)] == prefix


def _riff_subtype(head: bytes) -> bytes:
    """The four bytes naming what is inside a RIFF container, or b'' if malformed."""
    return head[8:12]


def _brand(head: bytes) -> bytes:
    """The major brand of an ISO-BMFF (MP4 family) file: bytes 8-12."""
    return head[8:12]


_M4A_BRANDS = {b"M4A ", b"M4B ", b"M4P ", b"mp4a"}
# `qt` is its own container with its own mime type; calling a .mov an .mp4 would be a
# label the browser's player has to work around.
_MP4_BRANDS = {b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V "}
_QUICKTIME = b"qt  "

_SVG_MARKERS = (b"<svg", b"\xef\xbb\xbf<svg")
_TEXT_MARKERS = (b"<html", b"<!doctype", b"<?xml", b"{\rtf", b"\xef\xbb\xbf<html")

_PNG = MediaFormat("image/png", "image", "png", "PNG image")
_JPEG = MediaFormat("image/jpeg", "image", "jpg", "JPEG image")
_GIF = MediaFormat("image/gif", "image", "gif", "GIF image")
_BMP = MediaFormat("image/bmp", "image", "bmp", "BMP image")
_WEBP = MediaFormat("image/webp", "image", "webp", "WebP image")
_TIFF = MediaFormat("image/tiff", "image", "tif", "TIFF image")
_MP3 = MediaFormat("audio/mpeg", "audio", "mp3", "MP3 audio")
_WAV = MediaFormat("audio/wav", "audio", "wav", "WAV audio")
_AIFF = MediaFormat("audio/aiff", "audio", "aif", "AIFF audio")
_FLAC = MediaFormat("audio/flac", "audio", "flac", "FLAC audio")
_OGG = MediaFormat("audio/ogg", "audio", "ogg", "Ogg audio")
_M4A = MediaFormat("audio/mp4", "audio", "m4a", "M4A audio")
_MP4 = MediaFormat("video/mp4", "video", "mp4", "MP4 video")
_MOV = MediaFormat("video/quicktime", "video", "mov", "QuickTime video")
_WEBM = MediaFormat("video/webm", "video", "webm", "WebM video")
_MKV = MediaFormat("video/x-matroska", "video", "mkv", "Matroska video")
_AVI = MediaFormat("video/avi", "video", "avi", "AVI video")

#: Everything this module is willing to store, in one list, so the upload screen can
#: print the truth instead of a second list someone has to remember to update.
ACCEPTED: tuple[MediaFormat, ...] = (
    _PNG, _JPEG, _GIF, _BMP, _WEBP, _TIFF,
    _MP3, _WAV, _AIFF, _FLAC, _OGG, _M4A,
    _MP4, _MOV, _WEBM, _MKV, _AVI,
)


def accepted_formats() -> list[MediaFormat]:
    return list(ACCEPTED)


def identify(head: bytes) -> MediaFormat | None:
    """Name the format of the first bytes of a file, or `None` when unknown.

    `head` should be the first `HEAD_BYTES` of the upload (shorter is fine: a real file
    of that size cannot carry any of these signatures, and the function stays total
    instead of raising on a truncated input).
    """
    if len(head) < 4:
        return None

    # --- images -------------------------------------------------------------
    if _starts_with(head, b"\x89PNG\r\n\x1a\n"):
        return _PNG
    if _starts_with(head, b"\xff\xd8\xff"):
        return _JPEG
    if _starts_with(head, b"GIF87a") or _starts_with(head, b"GIF89a"):
        return _GIF
    if _starts_with(head, b"BM"):
        return _BMP
    if _starts_with(head, b"RIFF") and _riff_subtype(head) == b"WEBP":
        return _WEBP
    if _starts_with(head, b"II*\x00") or _starts_with(head, b"MM\x00*"):
        return _TIFF

    # --- audio --------------------------------------------------------------
    if _starts_with(head, b"ID3"):
        return _MP3
    if _is_mpeg_frame(head):
        # An MP3 with no ID3 tag: the file opens straight on an audio frame.
        return _MP3
    if _starts_with(head, b"RIFF") and _riff_subtype(head) == b"WAVE":
        return _WAV
    if _starts_with(head, b"FORM") and _riff_subtype(head) == b"AIFF":
        return _AIFF
    if _starts_with(head, b"fLaC"):
        return _FLAC
    if _starts_with(head, b"OggS"):
        return _OGG
    if _starts_with(head, b"\x1a\x45\xdf\xa3"):
        return _ebml_video(head)
    if _starts_with(head[4:8], b"ftyp"):
        brand = _brand(head)
        if brand in _M4A_BRANDS:
            return _M4A
        if brand == _QUICKTIME:
            return _MOV
        if brand in _MP4_BRANDS:
            return _MP4
        return None

    # --- video (everything else is refused) ---------------------------------
    if _starts_with(head, b"RIFF") and _riff_subtype(head) in (b"AVI ", b"AVIX"):
        return _AVI
    return None


def _is_mpeg_frame(head: bytes) -> bool:
    """True when the file opens on an MPEG audio frame rather than some other bytes.

    The check is the frame header's own: an 11-bit sync pattern, then a version and a
    layer field that each have reserved values. Those two extra bits are what stop a
    binary file that happens to start with `FF E0` from being filed as music.
    """
    if len(head) < 2 or head[0] != 0xFF or (head[1] & 0xE0) != 0xE0:
        return False
    version = head[1] & 0x18  # 0x08 is the reserved version
    layer = head[1] & 0x06  # 0x00 is the reserved layer
    return version != 0x08 and layer != 0x00


def _ebml_video(head: bytes) -> MediaFormat | None:
    """EBML container: Matroska or WebM, told apart by their doctype string.

    The doctype is not at a fixed offset (it sits behind a variable-length EBML
    header), so it is searched for inside a bounded window instead of by parsing the
    element tree. A file whose doctype is not in that window is refused rather than
    guessed at: both answers here are video with sound, so this only ever decides
    which of the two the player is built for.
    """
    window = head[:HEAD_BYTES]
    if b"webm" in window:
        return _WEBM
    if b"matroska" in window:
        return _MKV
    return None


def refusal_reason(head: bytes) -> str:
    """Why this upload is not media, in the words the teacher sees.

    `identify()` returning None covers both "we do not know this file" and "this is a
    document, an archive or a program", and only the second group deserves a sentence of
    its own - a teacher who dropped a PDF into the media library needs to be sent to the
    import screen, while an unrecognised container just needs to know it is not accepted.
    """
    lowered = head[:HEAD_BYTES].lower()
    if any(_starts_with(lowered, marker) for marker in _SVG_MARKERS):
        return (
            "Scalable Vector Graphics cannot be added to the media library. An SVG can "
            "carry scripts, and this platform has no way to make one safe to open."
        )
    if any(_starts_with(lowered, marker) for marker in _TEXT_MARKERS):
        return "This file is text or markup, not an image, audio or video file."
    for prefix, reason in _REFUSED:
        if _starts_with(head, prefix):
            return reason
    return "This file type is not supported. Images, audio and video files can be added."


def kind_of(mime: str | None) -> str | None:
    """The library shelf a stored mime belongs on."""
    if not mime:
        return None
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    return None


def label_for_mime(mime: str | None) -> str | None:
    """The plain name of a stored mime, or `None` if the library has a label from
    somewhere else (a Phase 12 adapter's output, say).

    A row whose mime is not in `ACCEPTED` gets no label rather than a guessed one, so
    the screen never says "MP3 audio" about a file this module has not seen.
    """
    for fmt in ACCEPTED:
        if fmt.mime == mime:
            return fmt.label
    return None
