"""Real files, built byte by byte, for the document tests.

These are not sample documents copied from somewhere: each builder makes the smallest
genuine file of its format that carries the thing the reader claims to find - a heading, a
page break, a blank cell in the middle of a row, a compressed text stream, a font whose
characters only exist through its own ToUnicode map. A parser that passes on a file built
by hand has to have found the structure, because nothing here came from an editor that
could have hidden a shortcut.

Both halves of the importer use them: `test_document_parsing` checks what a file contains,
`test_import_candidates` checks what the importer decides to do with it, and the import
service tests check what a teacher can then approve.
"""
from __future__ import annotations

import io
import zipfile
import zlib

CONTENT_TYPES = b'<?xml version="1.0"?><Types xmlns="x"></Types>'
RELS = (
    b'<?xml version="1.0"?><Relationships xmlns="x">'
    b'<Relationship Id="rId1" Type="officeDocument" Target="word/document.xml"/>'
    b"</Relationships>"
)

_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def member_names(data: bytes) -> list[str]:
    """The archive's own list of members, which is what turns a ZIP into a document type."""
    return zipfile.ZipFile(io.BytesIO(data)).namelist()


def docx_bytes(body: str) -> bytes:
    document = (
        '<?xml version="1.0"?>'
        f'<w:document xmlns:w="{_W}">'
        f"<w:body>{body}</w:body></w:document>"
    ).encode("utf-8")
    return zip_bytes(
        {
            "[Content_Types].xml": CONTENT_TYPES,
            "_rels/.rels": RELS,
            "word/document.xml": document,
        }
    )


def paragraph(text: str, *, style: str | None = None, break_page: bool = False) -> str:
    """One `<w:p>`, optionally styled as a heading and optionally carrying a page break."""
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    marks = '<w:r><w:lastRenderedPageBreak/></w:r>' if break_page else ""
    return f"<w:p>{properties}{marks}<w:r><w:t xml:space='preserve'>{text}</w:t></w:r></w:p>"


def docx_table(rows: list[list[str]]) -> str:
    """A `<w:tbl>` of the given rows, one `<w:tc>` per cell."""
    body = []
    for cells in rows:
        entries = "".join(
            f"<w:tc><w:p><w:r><w:t xml:space='preserve'>{cell}</w:t></w:r></w:p></w:tc>"
            for cell in cells
        )
        body.append(f"<w:tr>{entries}</w:tr>")
    return f'<w:tbl>{"".join(body)}</w:tbl>'


def xlsx_bytes(sheets: dict[str, list[list[str]]], shared: list[str]) -> bytes:
    """A workbook in the shape Excel writes: strings in one place, cells pointing at them."""
    names = list(sheets)
    workbook = (
        '<?xml version="1.0"?><workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets></workbook>'
        % (
            _S_NS,
            _R_NS,
            "".join(
                f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>'
                for index, name in enumerate(names, start=1)
            ),
        )
    ).encode("utf-8")
    rels = (
        '<?xml version="1.0"?><Relationships xmlns="x">%s</Relationships>'
        % "".join(
            f'<Relationship Id="rId{index}" Type="worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
            for index in range(1, len(names) + 1)
        )
    ).encode("utf-8")
    strings = (
        '<?xml version="1.0"?><sst xmlns="%s">%s</sst>'
        % (
            _S_NS,
            "".join(
                f"<si><t>{value}</t></si>"
                if "\n" not in value
                else f'<si><t xml:space="preserve">{value}</t></si>'
                for value in shared
            ),
        )
    ).encode("utf-8")
    members = {
        "[Content_Types].xml": CONTENT_TYPES,
        "_rels/.rels": b'<?xml version="1.0"?><Relationships xmlns="x"/>',
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": rels,
        "xl/sharedStrings.xml": strings,
    }
    for index, name in enumerate(names, start=1):
        rows = []
        for row_number, cells in enumerate(sheets[name], start=1):
            entries = []
            for column_number, value in enumerate(cells):
                if value == "":
                    continue
                letter = chr(65 + column_number)
                position = shared.index(value) if value in shared else -1
                if position >= 0:
                    entries.append(f'<c r="{letter}{row_number}" t="s"><v>{position}</v></c>')
                else:
                    entries.append(
                        f'<c r="{letter}{row_number}" t="inlineStr"><is><t>{value}</t></is></c>'
                    )
            rows.append(f'<row r="{row_number}">{"".join(entries)}</row>')
        members[f"xl/worksheets/sheet{index}.xml"] = (
            '<?xml version="1.0"?><worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
            % (_S_NS, "".join(rows))
        ).encode("utf-8")
    return zip_bytes(members)


def pdf_bytes(objects: dict[int, bytes], root_pages: int) -> bytes:
    """A valid PDF in the smallest honest shape: header, objects, trailer.

    The reader walks the object tree, so the cross-reference table below is written for
    the file to remain a file a real viewer could open, not because the parser needs it.
    """
    parts = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(b"".join(parts))
        parts.append(f"{number} 0 obj\n".encode("latin-1"))
        parts.append(objects[number])
        parts.append(b"\nendobj\n")
    start = len(b"".join(parts))
    entries = "".join(f"{offsets[number]:010d} 00000 n \n" for number in sorted(objects))
    parts.append(
        (
            f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n{entries}"
            f"trailer\n<< /Size {len(objects) + 1} /Root {root_pages} 0 R >>\n"
            f"startxref\n{start}\n%%EOF\n"
        ).encode("latin-1")
    )
    return b"".join(parts)


def stream(body: bytes, payload: bytes, *, compress: bool = True) -> bytes:
    """A stream object: its dictionary, then its bytes, compressed the way PDF does it."""
    if compress:
        payload = zlib.compress(payload)
        return (
            b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(payload)
            + payload
            + b"\nendstream"
        )
    return b"<< /Length %d >>\nstream\n" % len(payload) + payload + b"\nendstream"


WINANSI_FONT = (
    b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
)


def simple_pdf(text_lines: list[str]) -> bytes:
    """One page, one WinAnsi font, and the text drawn line by line."""
    content = "BT\n/F1 12 Tf\n72 720 Td\n".encode("latin-1")
    for index, line in enumerate(text_lines):
        # A literal string is only well-formed when its own parentheses are escaped, which
        # is exactly what an exporter does - an option list is full of them.
        literal = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content += f"({literal}) Tj\n".encode("latin-1")
        if index + 1 < len(text_lines):
            content += b"0 -16 Td\n"
    content += b"ET\n"
    return pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            4: stream(b"", content),
            5: WINANSI_FONT,
        },
        root_pages=1,
    )


def pdf_pages(pages: list[list[str]]) -> bytes:
    """A document of several pages, each drawn with the same one WinAnsi font."""
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        5: WINANSI_FONT,
    }
    kids: list[str] = []
    number = 6
    for index, lines in enumerate(pages, start=1):
        content = "BT /F1 12 Tf 72 720 Td ".encode("latin-1")
        for position, line in enumerate(lines):
            if position:
                content += b" 0 -16 Td"
            literal = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            content += f" ({literal}) Tj".encode("latin-1")
        content += b" ET"
        page_object = number
        content_object = number + 1
        objects[page_object] = (
            f"<< /Type /Page /Parent 2 0 R /Contents {content_object} 0 R "
            f"/Resources << /Font << /F1 5 0 R >> >> >>"
        ).encode("latin-1")
        objects[content_object] = stream(b"", content, compress=False)
        kids.append(f"{page_object} 0 R")
        number += 2
    objects[2] = (
        f"<< /Type /Pages /Kids [{'] '.join(kids)}] /Count {len(pages)} >>"
    ).encode("latin-1")
    return pdf_bytes(objects, root_pages=1)
