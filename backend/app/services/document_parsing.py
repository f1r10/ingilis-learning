"""Reading a document's own text out of its bytes, with no third-party parser.

The import pipeline has to work when every optional provider in Settings is set to
`none`, so this module is first-party code, not an adapter: it reads the formats the
product claims, using only what ships with Python. Where a format cannot be read here,
the answer is a refusal naming the missing capability, never an empty success.

What "read" means per format:

* **docx** - the ZIP's `word/document.xml`, walked in document order. Paragraphs and
  table cells are real content; a heading is recognised from its paragraph style, and the
  page number comes from the breaks the file itself contains (`w:br type="page"` and the
  `lastRenderedPageBreak` marker Word writes when it repaginates). A .docx has no fixed
  pagination, so a document without breaks reports page 1 throughout - which is honest,
  and still leaves the block offset as provenance.
* **xlsx** - `xl/workbook.xml` for the sheet names, `xl/sharedStrings.xml` for the text a
  cell points at, and each worksheet for the rows. Cells are addressed by their column
  letter, so an empty cell in the middle of a row stays empty instead of shifting every
  answer one column left.
* **csv / tsv / txt / md** - decoded with the encoding that actually worked (reported in
  the result), split into rows by the `csv` rules for the separated formats and into
  paragraphs on blank lines for the text ones.
* **pdf** - the page objects' content streams, inflated where they are compressed, with
  the text-showing operators collected. A PDF writes glyphs, not characters, so the
  characters come back through the font's own `/ToUnicode` map when it has one (which is
  what every modern embedded-subset font has) and through the font's declared simple
  encoding otherwise. Page objects that live inside a compressed object stream are read
  there too, because that is how PDF 1.5 writes a document. A PDF whose pages hold no
  readable characters is reported as such, which is the state the OCR adapter of Phase
  12 picks up.

Tables and text come back as blocks with their provenance attached. Deciding what counts
as a question, a word or a passage is `import_candidates`, and only a teacher's approval
turns a block into content.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
import zlib
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from app.core.doc_types import DocFormat, decode_text, delimiter_of

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


class DocumentUnreadable(Exception):
    """These bytes are a real file of this type, but no text can be read out of them here.

    `reason` is the sentence the teacher gets: it names the missing capability rather than
    blaming the document.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class Block:
    """One unit of the document's own text, with the place it came from."""

    kind: str  # heading | paragraph | table_row | page_text
    text: str
    page: int | None = None
    sheet: str | None = None
    offset: int = 0
    cells: list[str] = field(default_factory=list)


@dataclass
class ParsedText:
    """Everything the importer needs to know about one document."""

    blocks: list[Block] = field(default_factory=list)
    page_count: int | None = None
    encoding: str | None = None
    sheet_names: list[str] = field(default_factory=list)


def parse(data: bytes, fmt: DocFormat) -> ParsedText:
    """Read the document's own text, or refuse with the reason it could not be read."""
    if fmt.name == "docx":
        return _parse_docx(data)
    if fmt.name == "xlsx":
        return _parse_xlsx(data)
    if fmt.name == "pdf":
        return _parse_pdf(data)
    if fmt.name in ("csv", "tsv"):
        return _parse_delimited(data, fmt)
    return _parse_plain(data, fmt)


# --------------------------------------------------------------------------- #
# docx
# --------------------------------------------------------------------------- #


def _element_text(element: ET.Element) -> str:
    """The characters a paragraph holds, with tabs and breaks kept as spaces.

    `w:t` runs are split by the formatter for its own reasons, so a paragraph is only
    readable once they are joined; a `w:tab` is a gap in a question paper and a `w:br` is
    a line the teacher saw as separate.
    """
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag in (f"{_W}tab", f"{_W}br", f"{_W}cr"):
            parts.append(" ")
    return "".join(parts)


def _heading_style(element: ET.Element) -> bool:
    style = element.find(f"{_W}pPr/{_W}pStyle")
    value = (style.get(f"{_W}val") or "") if style is not None else ""
    return bool(re.match(r"^(heading|title)", value, re.IGNORECASE))


def _page_breaks(element: ET.Element) -> int:
    """The page breaks written inside one paragraph.

    `w:br type="page"` is an explicit break its author put there. `lastRenderedPageBreak`
    is the boundary Word recorded when it last repaginated the file: not a command, but the
    only evidence a .docx carries about where its pages fell.
    """
    count = 0
    for node in element.iter():
        if node.tag == f"{_W}br" and node.get(f"{_W}type") == "page":
            count += 1
        elif node.tag == f"{_W}lastRenderedPageBreak":
            count += 1
    return count


def _table_row(row: ET.Element) -> list[str]:
    cells: list[str] = []
    for cell in row.findall(f"{_W}tc"):
        paragraphs = [_element_text(paragraph).strip() for paragraph in cell.iter(f"{_W}p")]
        cells.append(" ".join(part for part in paragraphs if part))
    return cells


def _parse_docx(data: bytes) -> ParsedText:
    damaged = "That Word document is damaged: its text could not be read."
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document = archive.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise DocumentUnreadable(damaged) from exc
    try:
        root = ET.fromstring(document)
    except ET.ParseError as exc:
        raise DocumentUnreadable(damaged) from exc

    body = root.find(f"{_W}body")
    if body is None:
        raise DocumentUnreadable("That Word document has no body to read.")

    blocks: list[Block] = []
    page = 1
    for child in body:
        if child.tag == f"{_W}p":
            text = _element_text(child).strip()
            if text:
                blocks.append(
                    Block(
                        kind="heading" if _heading_style(child) else "paragraph",
                        text=text,
                        page=page,
                        offset=len(blocks),
                    )
                )
            page += _page_breaks(child)
        elif child.tag == f"{_W}tbl":
            for row in child.findall(f"{_W}tr"):
                cells = _table_row(row)
                if any(cells):
                    blocks.append(
                        Block(
                            kind="table_row",
                            text=" | ".join(cells),
                            page=page,
                            offset=len(blocks),
                            cells=cells,
                        )
                    )
    if not blocks:
        raise DocumentUnreadable("That Word document contains no text.")
    return ParsedText(blocks=blocks, page_count=page)


# --------------------------------------------------------------------------- #
# xlsx
# --------------------------------------------------------------------------- #

_COLUMN_RE = re.compile(r"([A-Za-z]+)")


def _column_index(reference: str) -> int:
    """`B7` -> 1: the zero-based column a cell reference points at.

    Without this, a row whose first cell was left blank would have everything after it
    moved one place left, which is how an option ends up recorded as the answer.
    """
    letters = _COLUMN_RE.match(reference or "")
    if not letters:
        return 0
    index = 0
    for char in letters.group(1).upper():
        index = index * 26 + (ord(char) - 64)
    return index - 1


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    except ET.ParseError as exc:
        raise DocumentUnreadable("That spreadsheet is damaged: its text could not be read.") from exc
    out: list[str] = []
    for item in root.findall(f"{_S}si"):
        out.append("".join(node.text or "" for node in item.iter(f"{_S}t")))
    return out


def _sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    """Sheet name and the path of its XML, in workbook order."""
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except KeyError as exc:
        raise DocumentUnreadable("That spreadsheet has no sheets to read.") from exc
    except ET.ParseError as exc:
        raise DocumentUnreadable(
            "That spreadsheet is damaged: its sheet list could not be read."
        ) from exc

    targets = {
        rel.get("Id"): rel.get("Target") for rel in rels if rel.get("Id") and rel.get("Target")
    }
    out: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{_S}sheet"):
        target = targets.get(sheet.get(f"{_R}id"))
        if not target:
            continue
        path = target.lstrip("/")
        if not path.startswith("xl/"):
            path = "xl/" + path
        out.append((sheet.get("name") or f"Sheet{len(out) + 1}", path))
    if not out:
        raise DocumentUnreadable("That spreadsheet has no sheets to read.")
    return out


def _row_cells(row: ET.Element, shared: list[str]) -> list[str]:
    cells: list[str] = []
    for cell in row.findall(f"{_S}c"):
        position = _column_index(cell.get("r") or "")
        while len(cells) < position:
            cells.append("")
        kind = cell.get("t")
        if kind == "inlineStr":
            value = "".join(node.text or "" for node in cell.iter(f"{_S}t"))
        else:
            node = cell.find(f"{_S}v")
            value = node.text or "" if node is not None else ""
            if kind == "s" and value.isdigit():
                index = int(value)
                value = shared[index] if index < len(shared) else ""
        cells.append(value)
    return cells


def _parse_xlsx(data: bytes) -> ParsedText:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise DocumentUnreadable("That spreadsheet is damaged: it could not be opened.") from exc
    blocks: list[Block] = []
    with archive:
        sheets = _sheet_targets(archive)
        shared = _shared_strings(archive)
        for name, path in sheets:
            try:
                root = ET.fromstring(archive.read(path))
            except KeyError:
                continue
            row_number = 0
            for row in root.iter(f"{_S}row"):
                row_number += 1
                cells = _row_cells(row, shared)
                if not any(cell.strip() for cell in cells):
                    continue
                blocks.append(
                    Block(
                        kind="table_row",
                        text=" | ".join(cells),
                        page=row_number,
                        sheet=name,
                        offset=len(blocks),
                        cells=cells,
                    )
                )
    if not blocks:
        raise DocumentUnreadable("That spreadsheet contains no rows with anything in them.")
    return ParsedText(
        blocks=blocks, sheet_names=[name for name, _ in sheets], page_count=len(sheets)
    )


# --------------------------------------------------------------------------- #
# text, markdown, csv, tsv
# --------------------------------------------------------------------------- #


def _decoded(data: bytes) -> tuple[str, str]:
    decoded = decode_text(data)
    if decoded is None:
        raise DocumentUnreadable(
            "That text file could not be read as text in any supported encoding."
        )
    return decoded


def _csv_delimiter(text: str) -> str:
    """The separator a `.csv` is written with, which is the locale's, not necessarily a comma's.

    A spreadsheet saved in Turkish or Russian separates its columns with semicolons, and
    one saved row may show too few lines for the shared-count rule to be sure of anything.
    With a single line there is no evidence left, so the punctuation that appears wins,
    comma first because that is what the format is named after.
    """
    lines = text.splitlines()
    found = delimiter_of(lines) or delimiter_of(lines, minimum=2)
    if found:
        return found
    for candidate in (",", ";", "\t"):
        if any(candidate in line for line in lines):
            return candidate
    return ","


def _parse_delimited(data: bytes, fmt: DocFormat) -> ParsedText:
    text, encoding = _decoded(data)
    delimiter = "\t" if fmt.name == "tsv" else _csv_delimiter(text)
    blocks: list[Block] = []
    for number, raw in enumerate(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter), 1):
        cells = [cell.strip() for cell in raw]
        if not any(cells):
            continue
        blocks.append(
            Block(
                kind="table_row",
                text=" | ".join(cells),
                page=number,
                offset=len(blocks),
                cells=cells,
            )
        )
    if not blocks:
        raise DocumentUnreadable("That file has no rows with anything in them.")
    return ParsedText(blocks=blocks, encoding=encoding, page_count=len(blocks))


def _parse_plain(data: bytes, fmt: DocFormat) -> ParsedText:
    text, encoding = _decoded(data)
    # A paper saved on Windows ends its lines with CRLF, so its blank line is `\r\n\r\n` and a
    # split that expects two newlines with only spaces between them finds nothing at all: the
    # whole document arrives as a single paragraph, its headings and its passages merged. The
    # line ending is not one of the document's words, so it is settled before the split.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks: list[Block] = []
    for paragraph in re.split(r"\n[ \t]*\n", text):
        stripped = paragraph.strip()
        if not stripped:
            continue
        heading = bool(re.match(r"^#{1,6}\s", stripped))
        if fmt.name == "md":
            stripped = _strip_markdown(stripped)
        if stripped:
            blocks.append(
                Block(
                    kind="heading" if heading and fmt.name == "md" else "paragraph",
                    text=stripped,
                    offset=len(blocks),
                )
            )
    if not blocks:
        raise DocumentUnreadable("That file contains no text.")
    return ParsedText(blocks=blocks, encoding=encoding)


def _strip_markdown(text: str) -> str:
    """The words a Markdown paragraph is making, without its markup.

    A teacher importing a lesson written in Markdown wants the sentences; the markers say
    which block was a heading, which is recorded separately before this runs.
    """
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`+", "", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    return text.strip()


# --------------------------------------------------------------------------- #
# pdf
# --------------------------------------------------------------------------- #

_OBJ_START_RE = re.compile(rb"(?<![0-9])(\d+)\s+0\s+obj\b")
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\n?endstream", re.DOTALL)
_CONTENT_REF_RE = re.compile(rb"/Contents\s+(\d+)\s+0\s+R")
_CONTENT_ARRAY_RE = re.compile(rb"/Contents\s*\[(.*?)]", re.DOTALL)
_REF_RE = re.compile(rb"(\d+)\s+0\s+R")
_KIDS_RE = re.compile(rb"/Kids\s*\[(.*?)]", re.DOTALL)
_PAGE_TYPE_RE = re.compile(rb"/Type\s*/Page(?![s/])")
_PAGES_TYPE_RE = re.compile(rb"/Type\s*/Pages\b")
_FONT_DICT_RE = re.compile(rb"/Font\s*<<(.*?)>>", re.DOTALL)
_FONT_REF_RE = re.compile(rb"/([A-Za-z0-9._\-]+)\s+(\d+)\s+0\s+R")
_TOUNICODE_RE = re.compile(rb"/ToUnicode\s+(\d+)\s+0\s+R")
_ENCODING_RE = re.compile(rb"/Encoding\s*/([A-Za-z0-9._\-]+)")
_TYPE0_RE = re.compile(rb"/Subtype\s*/Type0\b")
_OBJSTM_RE = re.compile(rb"/Type\s*/ObjStm\b")
_FIRST_RE = re.compile(rb"/First\s+(\d+)")
_COUNT_RE = re.compile(rb"/N\s+(\d+)")
_PAIR_RE = re.compile(rb"(\d+)\s+(\d+)")
_BFCHAR_RE = re.compile(rb"beginbfchar(.*?)endbfchar", re.DOTALL)
_BFRANGE_RE = re.compile(rb"beginbfrange(.*?)endbfrange", re.DOTALL)
_HEX_RE = re.compile(rb"<([0-9A-Fa-f\s]*)>")
_HEX_PAIR_RE = re.compile(rb"<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>")
_BFRANGE_LINE_RE = re.compile(
    rb"<([0-9A-Fa-f\s]*)>\s*(?:<([0-9A-Fa-f\s]*)>\s*<([0-9A-Fa-f\s]*)>|\[(.*?)])", re.DOTALL
)
_OCTAL_RE = re.compile(rb"\\([0-7]{1,3})")
_ESCAPE_MAP = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}
_NUMBER_RE = re.compile(rb"[-+]?(?:\d+\.?\d*|\.\d+)")
_DELIMITER_BYTES = b"\x00\t\n\r \f[]()<>/{}%"
_WHITESPACE_BYTES = b"\x00\t\n\r \f"


def _inflate(payload: bytes, dictionary: bytes) -> bytes:
    """A stream's bytes, inflated when its own dictionary says they are compressed.

    Only FlateDecode is promised here; an encoding this module does not know returns the
    bytes unchanged, which the caller then fails to read text out of - the honest outcome
    being a refusal that names OCR, not invented characters.
    """
    if b"/FlateDecode" not in dictionary:
        return payload
    try:
        return zlib.decompress(payload)
    except zlib.error:
        pass
    try:
        # Some writers end a stream a byte early; what decompressed before that is still
        # the document's own text.
        return zlib.decompressobj().decompress(payload)
    except zlib.error:
        return b""


def _object_bodies(data: bytes) -> dict[int, bytes]:
    """Every object in the file by number: the direct ones, plus those inside object streams."""
    bodies: dict[int, bytes] = {}
    marks = list(_OBJ_START_RE.finditer(data))
    for position, match in enumerate(marks):
        start = match.end()
        end = marks[position + 1].start() if position + 1 < len(marks) else len(data)
        bodies[int(match.group(1))] = data[start:end]
    for body in list(bodies.values()):
        header = body[: body.find(b"stream")] if b"stream" in body else body
        if _OBJSTM_RE.search(header):
            for number, inner in _embedded_objects(body).items():
                bodies.setdefault(number, inner)
    return bodies


def _embedded_objects(body: bytes) -> dict[int, bytes]:
    """The objects a compressed object stream carries.

    PDF 1.5 puts most page objects here, so a reader that skips this sees a modern file as
    a document with no pages in it.
    """
    stream = _STREAM_RE.search(body)
    first = _FIRST_RE.search(body)
    count = _COUNT_RE.search(body)
    if not stream or not first or not count:
        return {}
    payload = _inflate(stream.group(1), body[: stream.start()])
    if not payload:
        return {}
    header_end = int(first.group(1))
    pairs = _PAIR_RE.findall(payload[:header_end])
    numbers = [int(number) for number, _ in pairs][: int(count.group(1))]
    offsets = [int(offset) for _, offset in pairs][: int(count.group(1))]
    out: dict[int, bytes] = {}
    for index, number in enumerate(numbers):
        start = header_end + offsets[index]
        end = header_end + offsets[index + 1] if index + 1 < len(offsets) else len(payload)
        out[number] = payload[start:end]
    return out


def _unescape_literal(raw: bytes) -> bytes:
    """A PDF literal string's bytes, with its escapes and line continuations applied."""
    out = bytearray()
    position = 0
    while position < len(raw):
        char = raw[position : position + 1]
        if char != b"\\":
            out += char
            position += 1
            continue
        following = raw[position + 1 : position + 2]
        if following in _ESCAPE_MAP:
            out += _ESCAPE_MAP[following]
            position += 2
        elif following in (b"\n", b"\r", b"\t", b"\f"):
            position += 2
        elif not following:
            out += b"\\"
            position += 1
        else:
            octal = _OCTAL_RE.match(raw, position)
            if octal:
                out.append(int(octal.group(1), 8) & 0xFF)
                position = octal.end()
            else:
                out += following
                position += 2
    return bytes(out)


def _hex_bytes(raw: bytes) -> bytes:
    digits = re.sub(rb"[^0-9A-Fa-f]", b"", raw)
    if len(digits) % 2:
        digits += b"0"
    try:
        return bytes.fromhex(digits.decode("ascii"))
    except ValueError:
        return b""


def _characters(value: bytes) -> str:
    """A CMap destination, which is written as UTF-16BE."""
    data = _hex_bytes(value)
    if not data:
        return ""
    if len(data) % 2:
        data += b"\x00"
    return data.decode("utf-16-be", errors="replace").replace("\x00", "")


def _to_unicode_map(payload: bytes) -> dict[int, str]:
    """code -> character, from one font's `/ToUnicode` CMap."""
    mapping: dict[int, str] = {}
    for chunk in _BFCHAR_RE.findall(payload):
        for source, target in _HEX_PAIR_RE.findall(chunk):
            text = _characters(target)
            digits = re.sub(rb"\s", b"", source)
            if text and digits:
                mapping[int(digits, 16)] = text
    for chunk in _BFRANGE_RE.findall(payload):
        for low, high, destination, listed in _BFRANGE_LINE_RE.findall(chunk):
            start_digits = re.sub(rb"\s", b"", low)
            if not start_digits:
                continue
            start = int(start_digits, 16)
            if listed:
                for step, value in enumerate(_HEX_RE.findall(listed)):
                    text = _characters(value)
                    if text:
                        mapping[start + step] = text
            elif high and destination:
                end = int(re.sub(rb"\s", b"", high) or b"0", 16)
                base = _characters(destination)
                if base:
                    first = ord(base[0])
                    for step in range(end - start + 1):
                        mapping[start + step] = chr(first + step)
    return mapping


class _Font:
    """How one font's string bytes become characters."""

    def __init__(self, body: bytes, bodies: dict[int, bytes]) -> None:
        self.unicode_map: dict[int, str] = {}
        reference = _TOUNICODE_RE.search(body)
        if reference:
            target = bodies.get(int(reference.group(1)))
            stream = _STREAM_RE.search(target) if target else None
            if stream:
                payload = _inflate(stream.group(1), target[: stream.start()])
                self.unicode_map = _to_unicode_map(payload)
        # A Type0 (CID) font addresses its glyphs with two bytes per code, and a ToUnicode
        # map whose keys only appear above 0xFF says the same thing on its own.
        self.two_byte = bool(_TYPE0_RE.search(body)) or any(key > 0xFF for key in self.unicode_map)
        encoding = _ENCODING_RE.search(body)
        name = encoding.group(1).lower() if encoding else b""
        if b"macroman" in name:
            self.encoding = "mac_roman"
        elif b"winansi" in name or b"ansi" in name:
            self.encoding = "cp1252"
        else:
            self.encoding = "latin-1"

    def decode(self, raw: bytes) -> str:
        if self.two_byte or self.unicode_map:
            if not self.unicode_map:
                # A CID font with no map: the codes are glyph numbers, and any character
                # this module claimed for them would be invented.
                return ""
            return "".join(
                self.unicode_map.get(int.from_bytes(raw[start : start + 2], "big"), "")
                for start in range(0, len(raw) - 1, 2)
            )
        return raw.decode(self.encoding, errors="replace")


_EMPTY_FONT = _Font(b"", {})


def _page_fonts(bodies: dict[int, bytes], page_body: bytes) -> dict[str, _Font]:
    """The fonts addressable while drawing one page, by the name the page uses."""
    fonts: dict[str, _Font] = {}
    dictionary = _FONT_DICT_RE.search(page_body)
    if not dictionary:
        return fonts
    for name, number in _FONT_REF_RE.findall(dictionary.group(1)):
        body = bodies.get(int(number))
        if body is None:
            continue
        try:
            fonts[name.decode("ascii")] = _Font(body, bodies)
        except UnicodeDecodeError:
            continue
    return fonts


def _read_string(data: bytes, start: int) -> int:
    """The index just past a literal string, honouring its escapes and balanced brackets.

    A PDF string may contain unescaped parentheses as long as they are balanced, so the
    end of the string is not the next `)` it sees.
    """
    position = start + 1
    depth = 1
    total = len(data)
    while position < total:
        char = data[position : position + 1]
        if char == b"\\":
            position += 2
            continue
        if char == b"(":
            depth += 1
        elif char == b")":
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    return total


def _tokens(content: bytes) -> list[tuple[str, bytes]]:
    """The operands and operators of one content stream, in order.

    A content stream is postfix - operands, then the operator that uses them - and the
    only thing that matters to a text reader is which tokens are strings, which are
    numbers, and which are the instructions. Everything else (a graphics dictionary, a
    comment, an inline image's raw data) is stepped over rather than guessed at.
    """
    out: list[tuple[str, bytes]] = []
    position = 0
    total = len(content)
    while position < total:
        char = content[position : position + 1]
        if char in _WHITESPACE_BYTES:
            position += 1
            continue
        if char == b"(":
            end = _read_string(content, position)
            out.append(("str", content[position:end]))
            position = end
            continue
        if char == b"<":
            if content[position + 1 : position + 2] == b"<":
                end = content.find(b">>", position)
                position = total if end == -1 else end + 2
                continue
            end = content.find(b">", position)
            if end == -1:
                break
            out.append(("hex", content[position : end + 1]))
            position = end + 1
            continue
        if char == b"[":
            depth = 0
            end = position
            while end < total:
                step = content[end : end + 1]
                if step == b"\\":
                    end += 2
                    continue
                if step == b"(":
                    end = _read_string(content, end) - 1
                elif step == b"[":
                    depth += 1
                elif step == b"]":
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            out.append(("array", content[position : end + 1]))
            position = end + 1
            continue
        if char == b"/":
            end = position + 1
            while end < total and content[end : end + 1] not in _DELIMITER_BYTES:
                end += 1
            out.append(("name", content[position:end]))
            position = end
            continue
        if char == b"%":
            end = content.find(b"\n", position)
            position = total if end == -1 else end + 1
            continue
        end = position
        while end < total and content[end : end + 1] not in _DELIMITER_BYTES:
            end += 1
        word = content[position:end]
        if not word:
            position += 1
            continue
        out.append(("num" if _NUMBER_RE.fullmatch(word) else "op", word))
        position = end
    return out


def _show_string(token: bytes, font: _Font) -> str:
    if token.startswith(b"("):
        return font.decode(_unescape_literal(token[1:-1]))
    if token.startswith(b"<"):
        return font.decode(_hex_bytes(token[1:-1]))
    return ""


def _show_array(token: bytes, font: _Font) -> str:
    """The text a `TJ` array draws, with a wide gap read back as the space it looks like.

    The numbers in the array are kerning adjustments in thousandths of an em; pushing two
    pieces more than about a space apart is how a PDF writes a word boundary.
    """
    out: list[str] = []
    position = 0
    while position < len(token):
        char = token[position : position + 1]
        if char == b"(":
            end = position
            depth = 0
            while end < len(token):
                step = token[end : end + 1]
                if step == b"\\":
                    end += 2
                    continue
                if step == b"(":
                    depth += 1
                elif step == b")":
                    depth -= 1
                    if depth == 0:
                        break
                end += 1
            out.append(_show_string(token[position : end + 1], font))
            position = end + 1
        elif char == b"<":
            end = token.find(b">", position)
            if end == -1:
                break
            out.append(_show_string(token[position : end + 1], font))
            position = end + 1
        else:
            match = re.match(rb"[-+]?(?:\d+\.?\d*|\.\d+)", token[position:])
            if match:
                number = match.group(0)
                if number and float(number) < -100:
                    out.append(" ")
                position += len(number)
            else:
                position += 1
    return "".join(out)


def _page_texts(content: bytes, fonts: dict[str, _Font]) -> list[str]:
    """The lines one page's content stream draws, in the order it draws them.

    Text position is what turns a stream of drawn strings back into a page: a move to a
    new vertical position is a new line, while a move sideways on the same line is the gap
    between two words. The gap is measured against the size the page asked the font to draw
    at, because a PDF records positions in text space, not characters - and a reader that
    ignored it would return "Whatisthenameofyourcity" for a line the teacher can see.
    """
    lines: list[str] = []
    current: list[str] = []
    font: _Font = _EMPTY_FONT
    size = 12.0
    row: float | None = None
    gap = False
    # The origin of the line being drawn, in text space. `Td` steps it and `Tm` sets it, so
    # tracking it is what tells five `0 -16 Td` moves apart from one move repeated.
    line_x = 0.0
    line_y = 0.0
    operands: list[tuple[str, bytes]] = []

    def flush() -> None:
        if current:
            lines.append("".join(current).strip())
            current.clear()

    def last_of(*kinds: str) -> bytes:
        for kind, value in reversed(operands):
            if kind in kinds:
                return value
        return b""

    def numbers() -> list[float]:
        out: list[float] = []
        for kind, value in operands:
            if kind != "num":
                continue
            try:
                out.append(float(value))
            except ValueError:
                continue
        return out

    def place(new_x: float, new_y: float) -> None:
        """Record where the next drawn string sits, and what that means for the line.

        The sideways distance from the previous position is a gap between two words; a
        change of vertical position is a new line, and the line being closed is flushed.
        """
        nonlocal row, gap, line_x, line_y
        step_x = new_x - line_x
        if row is not None and abs(new_y - row) > 0.5:
            flush()
            gap = False
        elif abs(step_x) > size * 0.2:
            gap = True
        row = new_y
        line_x, line_y = new_x, new_y

    for kind, value in _tokens(content):
        if kind != "op":
            operands.append((kind, value))
            continue
        if value in (b"Tj", b"'", b'"'):
            drawn = _show_string(last_of("str", "hex"), font)
            if drawn:
                if current and gap:
                    current.append(" ")
                current.append(drawn)
                gap = False
            if value in (b"'", b'"'):
                flush()
        elif value == b"TJ":
            drawn = _show_array(last_of("array"), font)
            if drawn:
                if current and gap:
                    current.append(" ")
                current.append(drawn)
                gap = False
        elif value == b"Tf":
            name = last_of("name")
            found = fonts.get(name[1:].decode("ascii", "replace")) if name else None
            if found is not None:
                font = found
            values = numbers()
            if values:
                size = values[-1] or size
        elif value in (b"Td", b"TD"):
            values = numbers()
            if len(values) >= 2:
                # `Td` steps down from the start of the line before it, which is how a
                # writer moves down a page: five `0 -16 Td` are five lines, not one line
                # drawn five times at the same height.
                place(line_x + values[0], line_y + values[1])
        elif value == b"Tm":
            # The absolute text matrix, which is how most writers place the first line.
            values = numbers()
            if len(values) >= 6:
                place(values[4], values[5])
        elif value == b"T*":
            flush()
        elif value in (b"BT", b"ET"):
            flush()
            row = None
            gap = False
            line_x = line_y = 0.0
        operands = []
    flush()
    return [line for line in lines if line]


def _page_content(bodies: dict[int, bytes], page_body: bytes) -> bytes:
    """The drawing commands of one page, which is where its text is written."""
    targets: list[int] = []
    reference = _CONTENT_REF_RE.search(page_body)
    if reference:
        targets.append(int(reference.group(1)))
    else:
        array = _CONTENT_ARRAY_RE.search(page_body)
        if array:
            targets.extend(int(value) for value in _REF_RE.findall(array.group(1)))
    out = bytearray()
    for target in targets:
        body = bodies.get(target)
        if body is None:
            continue
        stream = _STREAM_RE.search(body)
        if not stream:
            continue
        payload = _inflate(stream.group(1), body[: stream.start()])
        if payload:
            out += payload
            out += b"\n"
    return bytes(out)


def _page_order(bodies: dict[int, bytes]) -> list[int]:
    """Every page object id, in the order the document reads.

    The tree is walked from the node no other node lists as a child, so pages that arrive
    out of order in the file still come back top to bottom.
    """
    listed: set[int] = set()
    for body in bodies.values():
        if _PAGES_TYPE_RE.search(body):
            kids = _KIDS_RE.search(body)
            if kids:
                listed.update(int(value) for value in _REF_RE.findall(kids.group(1)))
    roots = [
        object_id
        for object_id, body in bodies.items()
        if _PAGES_TYPE_RE.search(body) and object_id not in listed
    ]
    ordered: list[int] = []

    def walk(object_id: int, depth: int) -> None:
        body = bodies.get(object_id)
        if body is None or depth > 32:
            return
        kids = _KIDS_RE.search(body)
        if _PAGES_TYPE_RE.search(body) and kids:
            for child in _REF_RE.findall(kids.group(1)):
                walk(int(child), depth + 1)
        elif _PAGE_TYPE_RE.search(body) and object_id not in ordered:
            ordered.append(object_id)

    for root in roots:
        walk(root, 0)
    if not ordered:
        ordered = [object_id for object_id, body in bodies.items() if _PAGE_TYPE_RE.search(body)]
    return ordered


def _parse_pdf(data: bytes) -> ParsedText:
    if not data.startswith(b"%PDF"):
        raise DocumentUnreadable("That PDF could not be read as a document.")
    bodies = _object_bodies(data)
    pages = _page_order(bodies)
    if not pages:
        raise DocumentUnreadable("That PDF has no pages that could be read.")
    blocks: list[Block] = []
    for number, object_id in enumerate(pages, start=1):
        body = bodies[object_id]
        content = _page_content(bodies, body)
        if not content:
            continue
        fonts = _page_fonts(bodies, body)
        for line in _page_texts(content, fonts):
            stripped = re.sub(r"\s+", " ", line).strip()
            if stripped:
                blocks.append(
                    Block(kind="page_text", text=stripped, page=number, offset=len(blocks))
                )
    if not blocks:
        raise DocumentUnreadable(
            "That PDF has no text to read - its pages are pictures. Turning pictures into "
            "text needs OCR, which is not enabled in Settings."
        )
    return ParsedText(blocks=blocks, page_count=len(pages))
