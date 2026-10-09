"""What a document actually is, decided from its bytes rather than from its name.

The import screen takes a file a teacher dragged out of a folder. A browser's
`Content-Type`, the extension and the filename are all things the person holding the
keyboard controls, and unlike the media library, an imported document is *read* by this
application: its text becomes question candidates. A wrong guess here is not a broken
thumbnail but a job that spends worker time producing nothing, and then tells the
teacher the document was empty when in fact it was never opened correctly.

So the pipeline identifies the bytes, and the vocabulary of what may be imported is
exactly the vocabulary this module can name:

* **OOXML word processing documents** (`.docx`): a ZIP whose members contain
  `word/document.xml`. A ZIP on its own says nothing - the same container holds a
  PowerPoint file, an EPUB or a folder of photos - so the member list decides.
* **OOXML spreadsheets** (`.xlsx`): a ZIP containing `xl/workbook.xml`.
* **PDF** (`.pdf`): the `%PDF-` header. Whether a given PDF holds a text layer is only
  known once it has been parsed, which is why the parser, not this module, is the one
  that says "this PDF is scanned pages and needs OCR".
* **Plain text and Markdown** (`.txt`, `.md`) and **delimiter-separated tables**
  (`.csv`, `.tsv`, and semicolon files, which are what a spreadsheet in Turkish or
  Russian locale writes): recognised by decoding, and a table only when the delimiter
  count is consistent across lines. A file too short to prove its columns - a two-row
  export, a one-line note - is then labelled by the extension it carries, since its bytes
  have already been shown to be readable text. A name never rescues a binary file: it can
  only choose between the text formats the bytes allow.

Everything else is refused with a sentence naming what the file really is and, where the
teacher can act on it, what to save it as instead. The old binary Office formats
(`.doc`, `.xls`, `.ppt`) share one OLE compound-file signature and are genuinely a
different family of parsers: the honest answer is "save it as .docx", not a silent
attempt. Rich Text, EPUB, archives, and any executable or media container are named the
same way. A file that decodes as neither text nor a known container is refused rather
than guessed at.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from typing import Sequence

#: How many bytes of an upload a caller hands to `identify` and `refusal_reason`. Every
#: signature below sits in the first dozen bytes, but telling a real text file from a
#: binary one needs a sample large enough to contain a few lines of it, and a ZIP's
#: member names are read from the central directory, which this module does not do - the
#: caller supplies them.
HEAD_BYTES = 8192

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MARKS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_RTF = b"{\\rtf"

# The byte a text file is allowed to hold beyond the printable range. Anything outside
# this set is a sign the file is not text at all, however well it happens to decode.
_TEXT_CONTROLS = "\t\n\r\x0c\x1b"

#: Single-byte encodings tried in order after UTF-8: 1254 for Turkish, 1251 for Cyrillic,
#: 1252 for the rest of Western Europe. Each is tried strictly - a lenient decode would
#: drop the bytes it could not read, and a lesson missing half a word is worse than a
#: file this module says it cannot name.
#:
#: UTF-16 is deliberately absent from this list. Two bytes per character means an
#: even-length Windows text file "decodes" as UTF-16 without ever failing, and the
#: result is CJK-looking noise. A UTF-16 file announces itself with a byte-order mark,
#: and only that mark makes this module try it.
_ENCODINGS = ("utf-8-sig", "cp1254", "cp1251", "cp1252")
_UTF16_MARKS = (b"\xff\xfe", b"\xfe\xff")

#: The extensions a text file may carry, used only to choose between text formats that the
#: bytes already proved readable. See `identify`.
_TEXT_HINTS = {
    "csv": "csv",
    "tsv": "tsv",
    "txt": "txt",
    "text": "txt",
    "md": "md",
    "markdown": "md",
}


@dataclass(frozen=True)
class DocFormat:
    """One format the import pipeline can read: the parser key, the labels to store and
    show, and the canonical extension."""

    name: str  # docx | xlsx | pdf | csv | tsv | txt | md
    mime: str
    extension: str
    label: str


DOCX = DocFormat(
    "docx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "docx",
    "Word document (.docx)",
)
XLSX = DocFormat(
    "xlsx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xlsx",
    "Spreadsheet (.xlsx)",
)
PDF = DocFormat("pdf", "application/pdf", "pdf", "PDF document")
CSV = DocFormat("csv", "text/csv", "csv", "Separated values (.csv)")
TSV = DocFormat("tsv", "text/tab-separated-values", "tsv", "Tab-separated values (.tsv)")
TXT = DocFormat("txt", "text/plain", "txt", "Plain text (.txt)")
MD = DocFormat("md", "text/markdown", "md", "Markdown text (.md)")

#: Formats a teacher may hand to the importer, in the order the upload screen lists them.
ACCEPTED: tuple[DocFormat, ...] = (DOCX, XLSX, PDF, CSV, TSV, TXT, MD)

#: Files that get their own sentence instead of the generic refusal, because the teacher
#: made a distinguishable mistake and can usually correct it in their own software.
_REFUSED: tuple[tuple[bytes, str], ...] = (
    (
        _OLE,
        "That is one of the old binary Office files (the .doc, .xls or .ppt format from "
        "before 2007). Save it as .docx or .xlsx and import that.",
    ),
    (
        _RTF,
        "That is a Rich Text file. Save it as a Word document (.docx) or plain text and "
        "import that.",
    ),
    (b"7z\xbc\xaf\x27\x1c", "That is a 7-Zip archive. Import the document inside it."),
    (b"Rar!\x1a\x07", "That is a WinRAR archive. Import the document inside it."),
    (b"PK\x05\x06", "That is an empty ZIP archive; there is no document in it."),
    (b"\x1f\x8b", "That is a compressed gzip file. Import the document inside it."),
    (b"BZh", "That is a bzip2 archive. Import the document inside it."),
    (b"MZ", "That is a program, not a document."),
    (b"\xff\xfe\x00\x00", "That is a UTF-32 file, which this importer does not read."),
    (
        b"BM",
        "That is a bitmap picture. Pictures are added through the media library, and text "
        "inside a picture needs OCR, which is not enabled.",
    ),
)

#: Members that make a ZIP something other than a document this pipeline reads. The
#: member list is what separates a .docx from an EPUB that happens to be a ZIP.
_OTHER_ZIPS: tuple[tuple[str, str], ...] = (
    (
        "ppt/presentation.xml",
        "That is a PowerPoint file. Slides are not a format this importer reads; put the "
        "text on the slides into a Word document or a spreadsheet instead.",
    ),
    (
        "mimetype",
        "That is an e-book (EPUB). Save the pages you want as a document and import that.",
    ),
)


def accepted_formats() -> list[DocFormat]:
    """The formats the import screen may promise, taken from the code that enforces them."""
    return list(ACCEPTED)


def _is_zip(sample: bytes) -> bool:
    return any(sample.startswith(mark) for mark in _ZIP_MARKS)


def ooxml_kind(names: Sequence[str]) -> str | None:
    """`docx`, `xlsx` or None from a ZIP member list.

    The check is on member names, which are plain strings in the archive's own directory,
    so a mislabelled or truncated archive simply fails to name a document type.
    """
    seen = set(names)
    if "word/document.xml" in seen:
        return "docx"
    if "xl/workbook.xml" in seen:
        return "xlsx"
    return None


def decode_text(sample: bytes) -> tuple[str, str] | None:
    """Decode a byte sample as text, or None when no supported encoding fits.

    Returns the text and the encoding that worked, because the parser wants to decode the
    rest of the file the same way and the teacher is told which encoding was read.
    """
    if sample[:2] in _UTF16_MARKS:
        try:
            return sample.decode("utf-16"), "utf-16"
        except (UnicodeDecodeError, LookupError):
            return None
    for encoding in _ENCODINGS:
        try:
            return sample.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
        except LookupError:
            # A codec the host Python was not built with: not this file's fault, and not
            # a reason to call a readable file unreadable.
            continue
    return None


def delimiter_of(lines: list[str], *, minimum: int = 3) -> str | None:
    """The column separator a block of lines shares, when it genuinely has columns.

    One line with a comma is prose. The evidence wanted is one separator that splits every
    line into the same number of columns - at least `minimum` of them - which is what a
    saved spreadsheet looks like and a paragraph of text does not.

    The counting is done by a CSV reader rather than by tallying characters, because a
    saved spreadsheet quotes a cell that contains its own separator: `"...B2, C1"...` is
    one column sitting between two commas, not three columns. Reading the quoting is what
    lets a level like `Upper-Intermediate to Advanced, according to the board` stay inside
    its column instead of making the whole file look like prose.
    """
    sample = [line for line in lines if line.strip()]
    if len(sample) < minimum:
        return None
    for candidate in ("\t", ";", ","):
        counts = _column_counts(sample, candidate)
        if counts is None:
            continue
        if counts[0] >= 2 and len(set(counts)) == 1:
            return candidate
    return None


def _column_counts(lines: list[str], delimiter: str) -> list[int] | None:
    """How many columns each line has under `delimiter`, or None if the shape is unreadable.

    A file this module cannot split is not a table as far as it is concerned, so anything
    the reader objects to - a field over its limit, an unterminated quote spanning the
    sample - becomes a None here rather than an exception reaching an upload.
    """
    try:
        return [len(fields) for fields in csv.reader(lines, delimiter=delimiter)]
    except csv.Error:
        return None


def _looks_like_markdown(text: str) -> bool:
    """A heading, a rule or an emphasis marker: enough for the text to be filed as Markdown.

    Only affects the label and how paragraphs are split; a plain text file that happens to
    start a line with `#` loses nothing, since both parsers read the same characters.
    """
    markers = ("# ", "## ", "### ", "- ", "* ", "> ", "---", "**")
    return any(line.startswith(markers) or "**" in line for line in text.splitlines()[:80])


def _hint_format(hint: str | None) -> str | None:
    """The text format a filename's extension points at, normalised, or None.

    A hint is only ever a tie-breaker between text formats whose bytes already decoded as
    text; it cannot make this module claim a format for a binary file, so anything that is
    not a text extension is dropped here rather than passed on.
    """
    if not hint:
        return None
    return _TEXT_HINTS.get(hint.lower().lstrip(".").strip())


def identify(
    sample: bytes,
    *,
    zip_names: Sequence[str] | None = None,
    hint: str | None = None,
) -> DocFormat | None:
    """The format these bytes belong to, or None when this module cannot name them.

    `zip_names` is the member list of the archive, supplied by the caller once it has the
    whole file: a ZIP header alone does not say whether it holds a Word document or a
    folder of photographs, and the difference decides whether the import can go on.

    `hint` is the filename's extension. It is consulted last and only for text, because a
    two-line `.tsv` from a spreadsheet's "save the selection" command is genuinely a
    tab-separated file that shows too few rows to prove it: the content rule needs three
    consistent lines, and a shorter file is labelled by its name when its bytes are
    already readable text. Where the content does speak, it wins over the name.
    """
    if not sample:
        return None
    if sample.startswith(b"%PDF"):
        return PDF
    if _is_zip(sample):
        kind = ooxml_kind(zip_names or ())
        if kind == "docx":
            return DOCX
        if kind == "xlsx":
            return XLSX
        return None
    if sample.startswith((_OLE, _RTF)):
        return None
    decoded = decode_text(sample)
    if decoded is None:
        return None
    text = decoded[0]
    if sum(1 for ch in text if ord(ch) < 32 and ch not in _TEXT_CONTROLS) > len(text) // 50:
        # A handful of control characters survives any text; a file whose printable body
        # is interrupted by them is a binary file that decoded by luck.
        return None
    lines = text.splitlines()
    delimiter = delimiter_of(lines)
    if delimiter is None and _hint_format(hint) in ("csv", "tsv"):
        # The name says "a table" and the content is too short for the three-line rule to
        # be sure: take the separator the bytes actually show, whichever it is.
        delimiter = delimiter_of(lines, minimum=2)
    if delimiter == "\t":
        return TSV
    if delimiter in (";", ","):
        return CSV
    if _looks_like_markdown(text):
        return MD
    if _hint_format(hint) == "md":
        return MD
    return TXT


def refusal_reason(sample: bytes, *, zip_names: Sequence[str] | None = None) -> str:
    """A teacher-facing sentence naming what the file actually is.

    Never empty: the caller turns this into a refusal, and an unknown file must still be
    described by what the pipeline can say about it.
    """
    for prefix, reason in _REFUSED:
        if sample.startswith(prefix):
            return reason
    if _is_zip(sample):
        names = set(zip_names or ())
        for member, reason in _OTHER_ZIPS:
            if member in names:
                return reason
        if names:
            return (
                "That is a ZIP archive that does not contain a Word document or a "
                "spreadsheet."
            )
        return "That is a ZIP archive; import the document inside it."
    if not sample:
        return "That file is empty."
    if sample.startswith(b"%PDF"):
        return "That PDF could not be read as a document."
    return (
        "That file is not a document this importer can read. Import a Word (.docx), "
        "spreadsheet (.xlsx), PDF, plain text, Markdown or separated-values file."
    )
