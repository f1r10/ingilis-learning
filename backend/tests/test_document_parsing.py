"""The document reader, tested against files built byte by byte.

The builders live in `doc_fixtures`; what is checked here is that a parser finds the
structure rather than a shortcut - a heading, a page break, a blank cell in the middle of a
row, a compressed text stream, a font whose characters only exist through its own ToUnicode
map. Nothing in these files came from an editor, so nothing could have been hidden.
"""
from __future__ import annotations

import zlib

import pytest
from doc_fixtures import (
    CONTENT_TYPES,
    WINANSI_FONT,
    docx_bytes,
    docx_table,
    member_names,
    paragraph,
    pdf_bytes,
    simple_pdf,
    stream,
    xlsx_bytes,
    zip_bytes,
)

from app.core import doc_types
from app.services import document_parsing as dp

# --------------------------------------------------------------------------- #
# What a file is
# --------------------------------------------------------------------------- #


def test_a_zip_is_only_a_word_document_once_its_members_say_so() -> None:
    archive = zip_bytes({"[Content_Types].xml": CONTENT_TYPES, "word/document.xml": b"<x/>"})
    names = member_names(archive)

    assert doc_types.identify(archive[: doc_types.HEAD_BYTES], zip_names=names) is not None
    assert doc_types.identify(archive, zip_names=names).name == "docx"

    slideshow = zip_bytes({"ppt/presentation.xml": b"<x/>"})
    assert doc_types.identify(slideshow, zip_names=member_names(slideshow)) is None
    assert "PowerPoint" in doc_types.refusal_reason(slideshow, zip_names=member_names(slideshow))


def test_the_formats_the_screen_promises_are_the_formats_that_are_recognised() -> None:
    accepted = {fmt.name for fmt in doc_types.accepted_formats()}
    assert accepted == {"docx", "xlsx", "pdf", "csv", "tsv", "txt", "md"}
    for fmt in doc_types.ACCEPTED:
        assert fmt.label and fmt.extension and fmt.mime


def test_a_file_of_bytes_that_is_not_a_document_is_named_not_guessed() -> None:
    cases = {
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1rest": "old binary Office",
        b"{\\rtf1 hello": "Rich Text",
        b"Rar!\x1a\x07archive": "WinRAR",
        b"7z\xbc\xaf\x27\x1carchive": "7-Zip",
        b"MZ\x90\x00program": "program",
        b"\x89PNG\r\n\x1a\nbytes": None,
    }
    for sample, expected in cases.items():
        reason = doc_types.refusal_reason(sample)
        if expected:
            assert expected.lower() in reason.lower(), (sample, reason)
        assert doc_types.identify(sample) is None
        assert reason  # never empty: the caller turns it into the refusal the teacher reads


def test_binary_that_happens_to_decode_is_not_treated_as_text() -> None:
    sample = bytes(range(0x80, 0x100)) * 4
    assert doc_types.identify(sample) is None


# --------------------------------------------------------------------------- #
# Plain text and Markdown
# --------------------------------------------------------------------------- #


def test_a_plain_text_lesson_splits_into_paragraphs_with_the_encoding_recorded() -> None:
    text = "Past Simple talks about finished time.\n\nSignal words: yesterday, last week."
    parsed = dp.parse(text.encode("utf-8"), doc_types.TXT)
    assert [block.text for block in parsed.blocks] == [
        "Past Simple talks about finished time.",
        "Signal words: yesterday, last week.",
    ]
    assert parsed.encoding == "utf-8-sig" or parsed.encoding == "utf-8"
    assert [block.offset for block in parsed.blocks] == [0, 1]


def test_turkish_text_in_its_own_windows_encoding_still_reads() -> None:
    # cp1254 is what a Turkish teacher's older editor writes; decoding it as UTF-8 would
    # fail, and decoding it with errors="ignore" would silently drop the ı and ş.
    text = "Şehir ve ısı hakkında sorular."
    parsed = dp.parse(text.encode("cp1254"), doc_types.TXT)
    assert parsed.blocks[0].text == text
    assert parsed.encoding == "cp1254"


def test_markdown_headings_are_headings_and_the_markup_does_not_survive() -> None:
    text = "# Present Perfect\n\nShe has **already finished** the exercise.\n\n- read\n- write"
    parsed = dp.parse(text.encode("utf-8"), doc_types.MD)
    assert parsed.blocks[0].kind == "heading"
    assert parsed.blocks[0].text == "Present Perfect"
    assert parsed.blocks[1].text == "She has already finished the exercise."
    assert parsed.blocks[2].text.startswith("read")


def test_a_paper_saved_by_a_windows_editor_still_splits_into_paragraphs() -> None:
    """CRLF is not a rare encoding here - it is what Notepad and WordPad write.

    The paragraph boundary is a blank line, and with CRLF the two newlines have a `\r` between
    them. A reader that only recognises `\n\n` sees one block containing the whole paper, and a
    teacher gets a single unnamed note where a heading and two passages should have been.
    """
    text = "Past Simple talks about finished time.\r\n\r\nSignal words: yesterday, last week.\r\n"
    parsed = dp.parse(text.encode("utf-8"), doc_types.TXT)
    assert [block.text for block in parsed.blocks] == [
        "Past Simple talks about finished time.",
        "Signal words: yesterday, last week.",
    ]
    assert not any("\r" in block.text for block in parsed.blocks)


def test_a_markdown_lesson_saved_with_crlf_keeps_its_heading_as_a_heading() -> None:
    text = "# Present Perfect\r\n\r\nShe has **already finished** the exercise.\r\n\r\n- read\r\n- write"
    parsed = dp.parse(text.encode("utf-8"), doc_types.MD)
    assert [block.kind for block in parsed.blocks] == ["heading", "paragraph", "paragraph"]
    assert parsed.blocks[0].text == "Present Perfect"
    assert parsed.blocks[1].text == "She has already finished the exercise."


def test_a_file_that_is_not_readable_text_is_refused_not_emptied() -> None:
    with pytest.raises(dp.DocumentUnreadable):
        dp.parse(b"", doc_types.TXT)


# --------------------------------------------------------------------------- #
# Separated values
# --------------------------------------------------------------------------- #


def test_semicolon_is_a_table_because_a_turkish_excel_writes_it_that_way() -> None:
    text = "word;meaning\napple;bir meyve\nriver;akan su\n"
    identified = doc_types.identify(text.encode("utf-8"))
    assert identified is not None and identified.name == "csv"
    parsed = dp.parse(text.encode("utf-8"), identified)
    assert parsed.blocks[0].cells == ["word", "meaning"]
    assert parsed.blocks[2].cells == ["river", "akan su"]
    assert all(block.kind == "table_row" for block in parsed.blocks)


def test_a_prose_file_with_one_comma_is_not_mistaken_for_a_table() -> None:
    text = "Read the text, then answer the questions below.\n\nYesterday I went to the market."
    identified = doc_types.identify(text.encode("utf-8"))
    assert identified is not None
    assert identified.name == "txt"


def test_a_quoted_comma_stays_inside_its_column_instead_of_becoming_a_third_column() -> None:
    """A spreadsheet that quotes a cell containing its own separator is still a table.

    Counting separator characters would see four commas on the middle line and three on the
    others, call the file prose, and hand the whole import a single unbroken passage.
    """
    text = (
        "word,meaning,level\n"
        'cat,"small animal, domestic",A1\n'
        "river,akan su,B2\n"
    )
    identified = doc_types.identify(text.encode("utf-8"))
    assert identified is not None and identified.name == "csv"
    parsed = dp.parse(text.encode("utf-8"), identified)
    assert [block.cells for block in parsed.blocks] == [
        ["word", "meaning", "level"],
        ["cat", "small animal, domestic", "A1"],
        ["river", "akan su", "B2"],
    ]


def test_an_unquoted_prose_file_does_not_become_a_table_by_accident() -> None:
    """The quoting rule reads columns; it must not start seeing them where there are none.

    Three sentences that each happen to hold one comma are still prose only when the commas
    do not line up into the same column shape on every line.
    """
    even = "I went, home.\nShe stayed, in.\nHe left, now.\n"
    assert doc_types.identify(even.encode("utf-8")).name == "csv"
    uneven = "I went, quite early, home.\nShe stayed in.\nHe left, now.\n"
    assert doc_types.identify(uneven.encode("utf-8")).name == "txt"


def test_tab_separated_rows_keep_their_empty_column() -> None:
    text = "question\toptions\tanswer\nWhich one?\ta | b\t\n"
    identified = doc_types.identify(text.encode("utf-8"), hint="tsv")
    assert identified is not None and identified.name == "tsv"
    parsed = dp.parse(text.encode("utf-8"), identified)
    assert parsed.blocks[1].cells == ["Which one?", "a | b", ""]


def test_a_two_row_export_is_a_table_because_its_name_says_so() -> None:
    """Three consistent lines prove columns on their own; a saved two-row selection cannot.

    The bytes are already known to be text, so the extension decides the label - and the
    parser then splits the columns on the separator the content actually uses.
    """
    semicolon = "word;meaning\napple;bir meyve\n"
    assert doc_types.identify(semicolon.encode("utf-8")).name == "txt"
    assert doc_types.identify(semicolon.encode("utf-8"), hint="csv").name == "csv"
    parsed = dp.parse(semicolon.encode("utf-8"), doc_types.CSV)
    assert [block.cells for block in parsed.blocks] == [
        ["word", "meaning"],
        ["apple", "bir meyve"],
    ]


def test_a_name_never_promises_more_than_the_bytes_support() -> None:
    """The hint only picks between text formats the content already allowed.

    A program renamed `.csv` stays refused, and a paragraph named `.csv` is not turned into
    a table by its extension when nothing separates its lines.
    """
    program = b"\x7fELF\x02\x01\x01\x00" + bytes(range(0x80, 0x100)) * 4
    assert doc_types.identify(program, hint="csv") is None
    prose = "Read the text.\nThen answer the questions.\n"
    assert doc_types.identify(prose.encode("utf-8"), hint="csv").name == "txt"


def test_the_content_beats_the_name_when_the_two_disagree() -> None:
    tabbed = "a\tb\nc\td\ne\tf\n"
    assert doc_types.identify(tabbed.encode("utf-8"), hint="csv").name == "tsv"
    marked = "# Lesson\n\nA sentence.\n"
    assert doc_types.identify(marked.encode("utf-8"), hint="txt").name == "md"
    assert doc_types.identify("Just a sentence.".encode("utf-8"), hint="md").name == "md"


# --------------------------------------------------------------------------- #
# docx
# --------------------------------------------------------------------------- #


def test_a_word_document_reads_in_order_with_its_headings_and_breaks() -> None:
    body = (
        paragraph("Grammar Test 4", style="Heading1")
        + paragraph("Choose the correct option.")
        + paragraph("1. She _ to school every day.", break_page=True)
        + paragraph("goes")
    )
    parsed = dp.parse(docx_bytes(body), doc_types.DOCX)
    kinds = [block.kind for block in parsed.blocks]
    assert kinds == ["heading", "paragraph", "paragraph", "paragraph"]
    assert parsed.blocks[0].text == "Grammar Test 4"
    assert parsed.blocks[0].page == 1
    # The break belongs to the paragraph it sits in, so the text after it is page 2.
    assert parsed.blocks[2].page == 1
    assert parsed.blocks[3].page == 2
    assert parsed.page_count == 2


def test_split_runs_are_one_sentence_again() -> None:
    """A formatter may break one sentence across several runs; the teacher sees one line."""
    runs = (
        "<w:p><w:r><w:t>She has</w:t></w:r><w:r><w:t> lived </w:t></w:r>"
        "<w:r><w:t>here since 2019.</w:t></w:r></w:p>"
    )
    parsed = dp.parse(docx_bytes(runs), doc_types.DOCX)
    assert parsed.blocks[0].text == "She has lived here since 2019."


def test_a_word_table_keeps_its_cells_as_cells() -> None:
    parsed = dp.parse(docx_bytes(docx_table([["city", "", "Baku"]])), doc_types.DOCX)
    assert parsed.blocks[0].kind == "table_row"
    assert parsed.blocks[0].cells == ["city", "", "Baku"]


def test_a_word_file_without_text_is_refused() -> None:
    with pytest.raises(dp.DocumentUnreadable) as excinfo:
        dp.parse(docx_bytes("<w:p><w:r><w:t>   </w:t></w:r></w:p>"), doc_types.DOCX)
    assert "no text" in str(excinfo.value).lower()


def test_a_truncated_word_file_names_the_damage() -> None:
    archive = zip_bytes({"[Content_Types].xml": CONTENT_TYPES})
    with pytest.raises(dp.DocumentUnreadable) as excinfo:
        dp.parse(archive, doc_types.DOCX)
    assert "damaged" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# xlsx
# --------------------------------------------------------------------------- #


def test_a_spreadsheet_reads_its_sheets_by_name_and_its_cells_by_column() -> None:
    workbook = xlsx_bytes(
        {
            "Words": [["word", "meaning", "level"], ["apple", "a fruit", "A1"], ["", "only here", "A2"]],
            "Notes": [["bring the workbook"]],
        },
        shared=["word", "meaning", "level", "apple", "a fruit", "A1", "only here", "A2", "bring the workbook"],
    )
    identified = doc_types.identify(workbook, zip_names=member_names(workbook))
    assert identified is not None and identified.name == "xlsx"

    parsed = dp.parse(workbook, identified)
    assert parsed.sheet_names == ["Words", "Notes"]
    first = parsed.blocks[0]
    assert (first.sheet, first.page, first.cells) == ("Words", 1, ["word", "meaning", "level"])
    assert parsed.blocks[1].cells == ["apple", "a fruit", "A1"]
    # The blank first cell must not pull the rest of the row one column left.
    blank_first = next(block for block in parsed.blocks if block.page == 3)
    assert blank_first.cells == ["", "only here", "A2"]
    note = next(block for block in parsed.blocks if block.sheet == "Notes")
    assert note.text == "bring the workbook"


def test_a_spreadsheet_with_nothing_in_it_is_refused() -> None:
    workbook = xlsx_bytes({"Empty": [["", "", ""]]}, shared=[])
    with pytest.raises(dp.DocumentUnreadable) as excinfo:
        dp.parse(workbook, doc_types.XLSX)
    assert "no rows" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# pdf
# --------------------------------------------------------------------------- #


def test_a_pdf_page_reads_back_as_written_lines() -> None:
    parsed = dp.parse(
        simple_pdf(["What is the capital of France?", "A) Rome  B) Paris  C) Lisbon"]),
        doc_types.PDF,
    )
    assert parsed.page_count == 1
    # The line breaks and the option letters are the document's; the run of spaces between
    # them is a layout detail, and one space is what a teacher wants to read back.
    assert [block.text for block in parsed.blocks] == [
        "What is the capital of France?",
        "A) Rome B) Paris C) Lisbon",
    ]
    assert all(block.kind == "page_text" for block in parsed.blocks)
    assert all(block.page == 1 for block in parsed.blocks)


def test_a_page_written_by_stepping_down_the_page_keeps_its_lines_apart() -> None:
    """`Td` steps from the start of the line before it, so five steps are five lines.

    Read as absolute positions instead, every line after the first lands on the same height
    and a whole paragraph comes back as one run-on string - which is how this fixture once
    failed, and why it is worth keeping.
    """
    parsed = dp.parse(simple_pdf(["One", "Two", "Three", "Four", "Five"]), doc_types.PDF)
    assert [block.text for block in parsed.blocks] == ["One", "Two", "Three", "Four", "Five"]


def test_a_move_sideways_on_one_line_is_the_space_between_two_words() -> None:
    content = b"BT /F1 12 Tf 72 720 Td (What) Tj 40 0 Td (is) Tj 40 0 Td (Baku) Tj ET"
    document = pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            4: stream(b"", content, compress=False),
            5: WINANSI_FONT,
        },
        root_pages=1,
    )
    parsed = dp.parse(document, doc_types.PDF)
    assert [block.text for block in parsed.blocks] == ["What is Baku"]


def test_a_two_page_pdf_numbers_its_blocks_by_page() -> None:
    """Page order comes from the page tree, not from the order the objects were written."""
    one = b"BT /F1 12 Tf 72 720 Td (First page sentence.) Tj ET"
    two = b"BT /F1 12 Tf 72 720 Td (Second page sentence.) Tj ET"
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    document = pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            # The second page's object comes first in the file: the tree still reads in order.
            2: b"<< /Type /Pages /Kids [4 0 R 3 0 R] /Count 2 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 5 0 R /Resources << /Font << /F1 7 0 R >> >> >>",
            4: b"<< /Type /Page /Parent 2 0 R /Contents 6 0 R /Resources << /Font << /F1 7 0 R >> >> >>",
            5: stream(b"", two, compress=False),
            6: stream(b"", one, compress=False),
            7: font,
        },
        root_pages=1,
    )
    parsed = dp.parse(document, doc_types.PDF)
    assert parsed.page_count == 2
    assert [(block.page, block.text) for block in parsed.blocks] == [
        (1, "First page sentence."),
        (2, "Second page sentence."),
    ]


def test_octal_escapes_and_balanced_parentheses_survive_a_literal_string() -> None:
    content = (
        b"BT /F1 12 Tf 72 720 Td (C\\(a\\) is 100\\% and \\351.) Tj ET"
    )
    document = pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            4: stream(b"", content, compress=False),
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        },
        root_pages=1,
    )
    parsed = dp.parse(document, doc_types.PDF)
    assert parsed.blocks[0].text == "C(a) is 100% and é."


def test_a_tj_array_with_wide_gaps_comes_back_as_words() -> None:
    """A kerning nudge stays inside the word; a gap the width of a space joins two of them."""
    content = b"BT /F1 12 Tf 72 720 Td [(Com)-40(plete)-600(this)-600(task)] TJ ET"
    document = pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            4: stream(b"", content, compress=False),
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        },
        root_pages=1,
    )
    parsed = dp.parse(document, doc_types.PDF)
    assert parsed.blocks[0].text == "Complete this task"


def test_a_subset_font_reads_through_its_own_tounicode_map() -> None:
    """What a modern exporter writes: two-byte codes whose meaning only the font knows.

    The three ways a ToUnicode CMap declares those codes appear together here - a one-to-one
    `bfchar`, a `bfrange` whose destinations run in order, and a `bfrange` that lists them -
    because a reader that handles only the first reads half of a real paper as blanks.
    """
    cmap = (
        b"/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
        b"1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
        b"3 beginbfchar\n<0001> <0053>\n<0002> <0075>\n<0005> <0079>\nendbfchar\n"
        b"1 beginbfrange\n<0003> <0004> <006E>\nendbfrange\n"
        b"1 beginbfrange\n<0006> <0006> [<006E>]\nendbfrange\n"
    )
    content = b"BT /F2 11 Tf 72 700 Td <00010002000300060005> Tj ET"
    document = pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R /Resources << /Font << /F2 6 0 R >> >> >>",
            4: stream(b"", content, compress=False),
            5: b"<< /Type /Font /Subtype /Type0 /BaseFont /AAaaaa+Calibri "
            b"/Encoding /Identity-H /DescendantFonts [7 0 R] /ToUnicode 8 0 R >>",
            6: b"<< /Type /Font /Subtype /Type0 /BaseFont /BBbbbb+Calibri "
            b"/Encoding /Identity-H /DescendantFonts [7 0 R] /ToUnicode 8 0 R >>",
            7: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /AAaaaa+Calibri >>",
            8: stream(b"", cmap, compress=False),
        },
        root_pages=1,
    )
    parsed = dp.parse(document, doc_types.PDF)
    assert parsed.blocks[0].text == "Sunny"


def test_a_pdf_of_pictures_only_says_so_and_names_the_missing_capability() -> None:
    document = pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: stream(b"", b"q 578 0 0 790 0 0 cm /Im1 Do Q"),
        },
        root_pages=1,
    )
    with pytest.raises(dp.DocumentUnreadable) as excinfo:
        dp.parse(document, doc_types.PDF)
    assert "OCR" in str(excinfo.value)
    assert "pictures" in str(excinfo.value)


def test_pages_inside_a_compressed_object_stream_are_still_pages() -> None:
    """PDF 1.5 keeps page objects in an ObjStm; reading only direct objects finds no pages.

    The page here is object 6, and object 6 exists only inside object 5's compressed
    stream - which is exactly how a modern exporter writes a document, with the tree still
    pointing at the number rather than at a direct object.
    """
    inner = (
        b"<< /Type /Page /Parent 2 0 R /Contents 3 0 R "
        b"/Resources << /Font << /F1 4 0 R >> >> >>"
    )
    header = b"6 0\n"  # object 6, at offset 0 after /First
    payload = header + inner
    first = len(header)
    objstm = (
        b"<< /Type /ObjStm /N 1 /First %d /Length %d /Filter /FlateDecode >>\nstream\n"
        % (first, len(zlib.compress(payload)))
        + zlib.compress(payload)
        + b"\nendstream"
    )
    content = b"BT /F1 12 Tf 72 720 Td (Inside an object stream.) Tj ET"
    document = pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [6 0 R] /Count 1 >>",
            3: stream(b"", content, compress=False),
            4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            5: objstm,
        },
        root_pages=1,
    )
    parsed = dp.parse(document, doc_types.PDF)
    assert parsed.blocks[0].text == "Inside an object stream."
    assert parsed.page_count == 1


def test_a_file_that_is_not_a_pdf_at_all_is_refused() -> None:
    with pytest.raises(dp.DocumentUnreadable):
        dp.parse(b"not a pdf", doc_types.PDF)


def test_a_content_stream_that_is_not_inflatable_refuses_rather_than_invents() -> None:
    document = pdf_bytes(
        {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /Contents 4 0 R >>",
            4: b"<< /Length 20 /Filter /FlateDecode >>\nstream\nGGGGGGGGGGGGGGGGGGGG\nendstream",
        },
        root_pages=1,
    )
    with pytest.raises(dp.DocumentUnreadable) as excinfo:
        dp.parse(document, doc_types.PDF)
    assert "OCR" in str(excinfo.value) or "no text" in str(excinfo.value)


def test_every_parser_returns_blocks_with_provenance_and_no_invented_text() -> None:
    """The contract the review screen rests on: a block is the document's own sentence."""
    fixtures = {
        doc_types.DOCX: docx_bytes(paragraph("Rewrite the sentence.")),
        doc_types.XLSX: xlsx_bytes({"Sheet1": [["Rewrite the sentence."]]}, shared=[]),
        doc_types.TXT: "Rewrite the sentence.".encode("utf-8"),
        doc_types.PDF: simple_pdf(["Rewrite the sentence."]),
    }
    for fmt, data in fixtures.items():
        parsed = dp.parse(data, fmt)
        assert len(parsed.blocks) >= 1
        for block in parsed.blocks:
            assert "Rewrite the sentence." in block.text
            assert block.kind in ("heading", "paragraph", "table_row", "page_text")
            assert block.offset >= 0


def test_identify_and_parse_agree_on_every_accepted_format() -> None:
    samples = {
        doc_types.DOCX: docx_bytes(paragraph("Text")),
        doc_types.XLSX: xlsx_bytes({"S": [["a"]]}, shared=["a"]),
        doc_types.PDF: simple_pdf(["Text"]),
        doc_types.TXT: "Just a sentence.".encode("utf-8"),
        doc_types.MD: "# Title\n\nA sentence.".encode("utf-8"),
        doc_types.CSV: "a,b\nc,d\ne,f\n".encode("utf-8"),
        doc_types.TSV: "a\tb\nc\td\ne\tf\n".encode("utf-8"),
    }
    for expected, data in samples.items():
        names = member_names(data) if data[:2] == b"PK" else None
        identified = doc_types.identify(
            data[: doc_types.HEAD_BYTES], zip_names=names, hint=expected.extension
        )
        assert identified is not None, expected
        assert identified.name == expected.name, (expected, identified)
        assert dp.parse(data, identified).blocks
