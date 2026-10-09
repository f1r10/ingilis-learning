"""What the importer decides a document contains, and what it refuses to decide.

Each rule here is tested against a real file built by `doc_fixtures` rather than against a
dictionary: a semicolon-separated export, a workbook with two differently shaped sheets, a
Word paper with numbered stems and lettered options, a PDF whose sentences are drawn across
several lines.

The tests are ordered by the risk they carry. The first group guards the one rule the
product cannot break - a candidate never holds an answer the document did not give, and
never contains words the document did not contain. The rest cover how a shape is recognised
and what happens to everything the importer cannot name: it is kept, as a note, because a
readable paragraph that was dropped looks exactly like a document that contained nothing.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from doc_fixtures import docx_bytes, docx_table, paragraph, simple_pdf, xlsx_bytes

from app.core import doc_types
from app.services import import_candidates as ic
from app.services.document_parsing import Block, ParsedText
from app.services.document_parsing import parse as read_document

CSV = doc_types.CSV
TSV = doc_types.TSV
DOCX = doc_types.DOCX
XLSX = doc_types.XLSX
PDF = doc_types.PDF

LONG_PASSAGE = (
    "Yesterday morning my sister went to the market near our house to buy fruit for the "
    "weekend. The stalls were already full when she arrived, and the seller she knows gave "
    "her a better price than the others did. She came back with two bags and a story about "
    "a man who sold her three oranges for the price of two."
)


def table(text: str, fmt=CSV) -> list[ic.Candidate]:
    return ic.candidates(read_document(text.encode("utf-8"), fmt), fmt)


def docx(body: str) -> list[ic.Candidate]:
    return ic.candidates(read_document(docx_bytes(body), DOCX), DOCX)


def pdf(lines: list[str]) -> list[ic.Candidate]:
    return ic.candidates(read_document(simple_pdf(lines), PDF), PDF)


# --------------------------------------------------------------------------- #
# The rule the whole module rests on
# --------------------------------------------------------------------------- #


def test_options_without_a_key_arrive_unfinished_instead_of_guessing_one() -> None:
    rows = table("question;options;answer\nShe _ every day.;goes | going;\n")
    question = rows[0]
    assert [option["text"] for option in question.extracted["options"]] == ["goes", "going"]
    assert not any(option["correct"] for option in question.extracted["options"])
    assert question.missing == ["answer"]
    assert question.confidence < 0.9, "an unanswered question is not a finished question"


def test_an_answer_that_names_no_option_marks_none_of_them_correct() -> None:
    rows = table("question;options;answer\nWho came?;A) Ann B) Ben C) Cem;D\n")
    question = rows[0]
    assert question.detected_type == "multiple_choice"
    assert not any(option["correct"] for option in question.extracted["options"])
    assert question.missing == ["answer"]
    assert question.note == "answer_not_an_option", "the review screen has to say why the key was not believed"


def test_a_prompt_with_no_options_and_no_answer_is_still_missing_its_answer() -> None:
    rows = table("question;level\nWho is she?;A1\n")
    question = rows[0]
    assert (question.detected_kind, question.detected_type) == ("question", "short_answer")
    assert question.missing == ["answer"]
    assert question.note == "no_answer_for_prompt"
    assert "accepted" not in question.extracted
    assert question.extracted["level"] == "A1"


def test_a_word_without_a_meaning_is_kept_and_named_as_incomplete() -> None:
    rows = table("word;meaning\napple;\n")
    word = rows[0]
    assert word.detected_kind == "vocabulary"
    assert word.missing == ["definition"]
    assert word.note == "word_without_meaning"
    assert word.extracted["word"] == "apple"
    assert word.extracted["definition"] == ""


def test_nothing_in_a_candidate_is_absent_from_the_document_itself() -> None:
    """The honesty check, run over every shape the importer reads."""
    fixtures = [
        ("question;options;answer\nShe _ every day.;goes | going;1\n".encode("utf-8"), CSV),
        ("word;meaning;example\nancient;very old;an ancient city\n".encode("utf-8"), CSV),
        (
            docx_bytes(
                paragraph("Reading: At the Market")
                + paragraph(LONG_PASSAGE)
                + paragraph("1. Where did she go?")
                + paragraph("A) To the market")
                + paragraph("B) To the school")
            ),
            DOCX,
        ),
        (
            simple_pdf(
                [
                    "The teacher asked the class to read the",
                    "passage aloud before answering any",
                    "questions. Nobody volunteered at first.",
                    "1. What did the class do?",
                    "A) Read aloud",
                    "B) Left the room",
                ]
            ),
            PDF,
        ),
    ]
    for data, fmt in fixtures:
        parsed = read_document(data, fmt)
        forms = _document_forms(parsed)
        for candidate in ic.candidates(parsed, fmt):
            for leaf in _leaves(candidate.extracted):
                for line in leaf.split("\n"):
                    folded = _folded(line)
                    if folded:
                        assert any(folded in form for form in forms), (fmt.name, leaf)


def _document_forms(parsed: ParsedText) -> list[str]:
    """The document's own words in the shapes a candidate may legitimately quote."""
    forms = [_folded(block.text) for block in parsed.blocks]
    forms.extend(_folded(cell) for block in parsed.blocks for cell in block.cells)
    # A passage is the document read on: paragraphs a person wrote one after another.
    forms.append(_folded(" ".join(block.text for block in parsed.blocks)))
    for page in {block.page for block in parsed.blocks}:
        forms.append(
            _folded(" ".join(block.text for block in parsed.blocks if block.page == page))
        )
    return forms


def _folded(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _leaves(extracted: dict) -> list[str]:
    out: list[str] = []
    for value in extracted.values():
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    out.append(item)
                elif isinstance(item, dict):
                    out.extend(_leaves(item))
    return out


# --------------------------------------------------------------------------- #
# Tables, by column
# --------------------------------------------------------------------------- #


def test_a_word_and_meaning_table_becomes_vocabulary_entries() -> None:
    rows = table("word;meaning\napple;bir meyve\nriver;akan su\n")
    assert [item.detected_kind for item in rows] == ["vocabulary", "vocabulary"]
    assert rows[0].extracted == {"word": "apple", "definition": "bir meyve"}
    assert rows[0].missing == []
    assert rows[0].confidence == 0.85
    # The row number is the provenance the review screen shows next to the candidate.
    assert [item.source_page for item in rows] == [2, 3]
    assert rows[0].source_sheet is None


def test_a_question_table_with_its_own_key_marks_the_option_the_document_named() -> None:
    rows = table("question;options;answer\nWho came?;A) Ann B) Ben C) Cem;B\n")
    question = rows[0]
    assert question.detected_type == "multiple_choice"
    assert [o["text"] for o in question.extracted["options"]] == ["Ann", "Ben", "Cem"]
    assert [o["correct"] for o in question.extracted["options"]] == [False, True, False]
    assert question.missing == []
    assert question.confidence == 0.9
    assert question.extracted["answer_text"] == "B"


def test_the_answer_column_accepts_a_letter_a_number_or_the_option_s_own_words() -> None:
    lettered = table("question;options;answer\nIs it?;yes | no;A\n")
    assert lettered[0].extracted["options"][0]["correct"] is True
    numbered = table("question;options;answer\nIs it?;yes | no;2\n")
    assert numbered[0].extracted["options"][1]["correct"] is True
    written = table("question;options;answer\nIs it?;the city | the sea;The Sea\n")
    assert written[0].extracted["options"][1]["correct"] is True


def test_an_answer_matching_two_identical_options_names_no_option() -> None:
    rows = table("question;options;answer\nWhich?;yes | yes;yes\n")
    assert not any(option["correct"] for option in rows[0].extracted["options"])
    assert rows[0].missing == ["answer"]


def test_one_option_per_column_is_the_same_question_as_one_cell_of_them() -> None:
    spread = table("question;a;b;c;answer\nWho came?;Ann;Ben;Cem;C\n")
    assert [o["text"] for o in spread[0].extracted["options"]] == ["Ann", "Ben", "Cem"]
    assert spread[0].extracted["options"][2]["correct"] is True

    together = table("question;options;answer\nWho came?;A) Ann B) Ben C) Cem;C\n")
    assert [o["text"] for o in together[0].extracted["options"]] == ["Ann", "Ben", "Cem"]
    assert together[0].extracted["options"][2]["correct"] is True


def test_lettered_columns_only_count_when_they_run_from_a_without_a_gap() -> None:
    """A missing option column would silently shift every answer in the file."""
    assert ic._letter_option_columns(["question", "a", "c", "answer"]) == {}
    assert ic._letter_option_columns(["question", "a", "b", "answer"]) == {
        1: "option",
        2: "option",
    }


def test_an_options_cell_with_a_gap_in_its_letters_keeps_the_letters_in_the_text() -> None:
    rows = table("question;options;answer\nWhich?;A) Rome C) Lisbon;\n")
    assert [o["text"] for o in rows[0].extracted["options"]] == ["A) Rome", "C) Lisbon"]


def test_a_tab_separated_export_is_read_the_same_way_as_a_semicolon_one() -> None:
    rows = table("word\tmeaning\texample\nbrave\tcesaretli\ta brave dog\n", TSV)
    word = rows[0]
    assert word.detected_kind == "vocabulary"
    assert word.extracted["examples"] == [{"sentence": "a brave dog"}]


def test_the_header_is_read_in_the_language_the_teacher_wrote_it_in() -> None:
    cases = {
        "soru;şıklar;doğru": {"prompt", "options", "answer"},
        "söz;anlam": {"word", "meaning"},
        "вопрос;варианты;ответ": {"prompt", "options", "answer"},
        "başlık;metin": {"title", "body"},
        "word;definition;example": {"word", "meaning", "example"},
        "title;reading;text": {"title", "body"},  # two cells want the same role, one of them loses
    }
    for header, expected in cases.items():
        assert set(ic.header_roles(header.split(";")).values()) == expected, header


def test_a_turkish_export_files_its_rows_by_its_own_headings() -> None:
    rows = table("soru;şıklar;doğru\nO her gün okula _.;A) goes B) going;A\n")
    assert rows[0].detected_kind == "question"
    assert [o["text"] for o in rows[0].extracted["options"]] == ["goes", "going"]
    assert rows[0].extracted["options"][0]["correct"] is True


def test_each_sheet_of_a_workbook_is_filed_by_its_own_header() -> None:
    workbook = xlsx_bytes(
        {
            "Words": [["word", "meaning"], ["apple", "a fruit"]],
            "Paper": [["question", "options", "answer"], ["Who came?", "A) Ann B) Ben", "B"]],
        },
        shared=[],
    )
    rows = ic.candidates(read_document(workbook, XLSX), XLSX)
    assert [item.detected_kind for item in rows] == ["vocabulary", "question"]
    assert rows[0].source_sheet == "Words"
    assert rows[1].source_sheet == "Paper"
    assert [o["text"] for o in rows[1].extracted["options"]] == ["Ann", "Ben"]
    assert rows[1].extracted["options"][1]["correct"] is True


def test_the_type_column_is_kept_as_the_document_wrote_it() -> None:
    rows = table("question;options;answer;type\nIs it?;A) yes B) no;A;true_false\n")
    assert rows[0].detected_type == "true_false"
    assert rows[0].extracted["options"][0]["correct"] is True


def test_a_row_that_fills_nothing_is_not_a_candidate() -> None:
    rows = table("word;meaning\napple;a fruit\n;;\n")
    assert len(rows) == 1


def test_headings_this_module_has_never_seen_keep_their_rows_as_text() -> None:
    rows = table("ürün;açıklama\nkalem;yazı aracı\nsilgi;iz siler\n")
    assert [item.detected_kind for item in rows] == ["note", "note", "note"]
    assert rows[1].extracted["text"] == "kalem | yazı aracı"
    # A queue of unexplained text cards reads like a crash, so the reader says which header
    # it could not shape - and the header row is a card like any other, because a person
    # decides whether those words belong in the bank.
    assert [item.note for item in rows] == ["unmatched_header"] * 3


def test_a_row_under_a_known_header_that_files_nothing_is_kept_as_text() -> None:
    """`level` and `type` are columns this reader knows, but neither one is content."""
    rows = table("level;type\nA1;multiple_choice\n")
    assert [item.detected_kind for item in rows] == ["note"]
    assert rows[0].extracted["text"] == "A1 | multiple_choice"
    assert rows[0].note == "row_without_role"
    assert rows[0].confidence == 0.35


def test_a_partial_header_still_files_the_rows_it_can() -> None:
    rows = table("word;notes about it\napple;seen in shops\n")
    assert rows[0].detected_kind == "vocabulary"
    assert rows[0].missing == ["definition"]


def test_a_word_with_only_a_level_still_waits_for_its_meaning() -> None:
    rows = table("word;level\napple;A1\n")
    assert rows[0].detected_kind == "vocabulary"
    assert rows[0].extracted["level"] == "A1"
    assert rows[0].missing == ["definition"]


def test_a_reading_row_needs_both_its_title_slot_and_its_body() -> None:
    rows = table("title;text\nA Day at the Market;" + LONG_PASSAGE + "\n")
    reading = rows[0]
    assert reading.detected_kind == "reading"
    assert reading.extracted["title"] == "A Day at the Market"
    assert reading.missing == []


# --------------------------------------------------------------------------- #
# Prose, by shape
# --------------------------------------------------------------------------- #


def test_a_numbered_stem_and_the_lettered_lines_under_it_are_one_question() -> None:
    rows = docx(
        paragraph("12. She _ to school every day.")
        + paragraph("A) goes")
        + paragraph("B) going")
        + paragraph("C) went")
        + paragraph("D) gone")
    )
    question = rows[0]
    assert (question.detected_kind, question.detected_type) == ("question", "multiple_choice")
    # The number told the importer a stem began; it is not part of the sentence.
    assert question.extracted["prompt"] == "She _ to school every day."
    assert [o["text"] for o in question.extracted["options"]] == ["goes", "going", "went", "gone"]
    assert question.missing == ["answer"]
    assert len(rows) == 1, "the options belong to the stem and are not candidates of their own"


def test_options_stop_where_the_next_stem_begins() -> None:
    rows = docx(
        paragraph("1. First stem?")
        + paragraph("A) one")
        + paragraph("B) two")
        + paragraph("2. Second stem?")
        + paragraph("A) three")
        + paragraph("B) four")
    )
    assert [item.detected_kind for item in rows] == ["question", "question"]
    assert [o["text"] for o in rows[0].extracted["options"]] == ["one", "two"]
    assert [o["text"] for o in rows[1].extracted["options"]] == ["three", "four"]


def test_a_stem_followed_by_prose_is_not_a_choice_question() -> None:
    rows = docx(paragraph("1. Answer the questions below.") + paragraph("They were late."))
    assert [item.detected_kind for item in rows] == ["note", "note"]


def test_a_heading_and_the_prose_under_it_are_a_reading() -> None:
    rows = docx(paragraph("Reading: A Day at the Market", style="Heading1") + paragraph(LONG_PASSAGE))
    reading = rows[0]
    assert reading.detected_kind == "reading"
    assert reading.extracted["title"] == "Reading: A Day at the Market"
    assert reading.missing == []
    assert reading.confidence == 0.8
    assert reading.note is None
    assert "two bags and a story" in reading.extracted["body"]


def test_a_markdown_paper_saved_on_windows_is_still_a_heading_and_a_passage() -> None:
    """The shape rule needs the paragraph break, and CRLF hides it from a naive split.

    Read as one block, this file would become a single unnamed note holding every word of the
    paper - which is the failure the reader exists not to have: a dropped passage and a document
    that contained nothing look the same from the queue.
    """
    text = "# A Day at the Market\r\n\r\n" + LONG_PASSAGE + "\r\n"
    rows = ic.candidates(read_document(text.encode("utf-8"), doc_types.MD), doc_types.MD)
    reading = next(row for row in rows if row.detected_kind == "reading")
    assert reading.extracted["title"] == "A Day at the Market"
    assert reading.note is None
    assert reading.missing == []
    assert "two bags and a story" in reading.extracted["body"]
    assert "\r" not in reading.extracted["body"], "a line ending is not one of the paper's words"


def test_a_passage_with_no_heading_of_its_own_takes_its_first_sentence() -> None:
    text = (
        "Learning a language takes more time than most people promise themselves. A learner "
        "who studies fifteen minutes every day ends the term with more words than one who "
        "studies three hours on Sunday, because the words had time to settle."
    )
    rows = docx(paragraph(text))
    reading = rows[0]
    assert reading.detected_kind == "reading"
    assert reading.extracted["title"] == text.split(". ")[0] + "."
    assert reading.confidence == 0.6
    assert reading.note == "passage_without_heading"


def test_a_paragraph_too_short_to_be_a_text_is_kept_as_a_note() -> None:
    rows = docx(paragraph("Past Simple talks about finished time."))
    assert rows[0].detected_kind == "note"
    assert rows[0].extracted["text"] == "Past Simple talks about finished time."
    assert rows[0].missing == []


def test_a_passage_and_the_stem_under_it_are_two_candidates_not_one() -> None:
    rows = docx(
        paragraph(
            "Read the text and answer the questions that follow it. The text is about a "
            "family who moved to a new city and had to find a school, a doctor and a bus "
            "route they had never used before in their lives. Read it twice first."
        )
        + paragraph("1. What did the family need to find?")
        + paragraph("A) A school")
        + paragraph("B) A car")
    )
    assert [item.detected_kind for item in rows] == ["reading", "question"]
    assert rows[0].extracted["body"].endswith("Read it twice first.")
    assert "What did the family need" not in rows[0].extracted["body"]


# --------------------------------------------------------------------------- #
# Tables inside prose documents
# --------------------------------------------------------------------------- #


def test_a_two_column_word_table_is_read_as_a_vocabulary_list() -> None:
    rows = docx(docx_table([["apple", "a fruit"], ["river", "akan su"]]))
    assert [item.detected_kind for item in rows] == ["vocabulary", "vocabulary"]
    assert rows[0].extracted["definition"] == "a fruit"


def test_a_table_row_whose_last_cell_names_the_others_is_an_answer_key() -> None:
    rows = docx(docx_table([["Which city?", "Rome", "Paris", "Lisbon", "B"]]))
    question = rows[0]
    assert question.detected_kind == "question"
    assert [o["text"] for o in question.extracted["options"]] == ["Rome", "Paris", "Lisbon"]
    assert question.extracted["options"][1]["correct"] is True


def test_a_table_row_that_says_nothing_about_its_columns_is_kept_whole() -> None:
    rows = docx(docx_table([["Monday", "Room 4", "Bring workbook"]]))
    assert rows[0].detected_kind == "note"
    assert rows[0].extracted["text"] == "Monday | Room 4 | Bring workbook"


# --------------------------------------------------------------------------- #
# A PDF's lines
# --------------------------------------------------------------------------- #


def test_pdf_lines_that_run_on_are_joined_and_option_lines_are_not() -> None:
    rows = pdf(
        [
            "The teacher asked the class to read the",
            "passage aloud before answering any",
            "questions. Nobody volunteered at first.",
            "1. What did the class do?",
            "A) Read aloud",
            "B) Left the room",
        ]
    )
    assert [item.detected_kind for item in rows] == ["note", "question"]
    assert rows[0].extracted["text"] == (
        "The teacher asked the class to read the passage aloud before answering any "
        "questions. Nobody volunteered at first."
    )
    question = rows[1]
    assert question.extracted["prompt"] == "What did the class do?"
    assert [o["text"] for o in question.extracted["options"]] == ["Read aloud", "Left the room"]
    assert question.source_page == 1


def test_a_finished_line_closes_its_paragraph_so_two_sentences_stay_apart() -> None:
    rows = pdf(["Yesterday we went out.", "A film was on at the cinema."])
    assert [item.detected_kind for item in rows] == ["note", "note"]


# --------------------------------------------------------------------------- #
# The module's own vocabulary
# --------------------------------------------------------------------------- #


def test_the_kinds_are_the_three_banks_plus_the_honest_note() -> None:
    assert ic.KINDS == ("question", "vocabulary", "reading", "note")
    assert "listening" not in ic.KINDS, "a listening is a recording, and a document has none"
    for kind in ic.KINDS:
        assert set(ic.required_fields(kind)) <= {"prompt", "word", "title", "body"}


def test_the_missing_fields_a_candidate_reports_are_codes_the_screen_can_translate() -> None:
    """Every reason a candidate is incomplete must be a key the browser already holds."""
    produced: set[str] = set()
    for rows in (
        table("question;options\nWho?;A) x B) y\n"),
        table("word;meaning\napple;\n"),
        table("question;level\nWho is she?;A1\n"),
    ):
        for row in rows:
            produced.update(row.missing)
    assert produced == {"answer", "definition"}
    assert produced <= {"prompt", "word", "title", "body", "answer", "definition", "options"}


def test_every_remark_the_reader_leaves_is_a_declared_code() -> None:
    """A remark is stored as a code, because the queue is read in four languages.

    The table is read out of this module's own source: every string written into a `note`, by
    assignment or as the `note=` of a candidate. A remark invented at a call site would print
    English in front of a teacher in Baku, and a declaration nobody writes would leave a
    sentence in four locale files that no screen renders. Either way fails here.
    """
    tree = ast.parse(Path(ic.__file__).read_text(encoding="utf-8"))

    def codes(node: ast.expr) -> set[str]:
        """The remark written here: a code, nothing, or a choice between two codes.

        A bare name carries a remark an assignment above already wrote, so it adds nothing new.
        Anything else - a call, a sentence built at the site - is the failure this rule exists to
        catch, so it is named rather than silently collected.
        """
        if isinstance(node, ast.Name):
            return set()
        if isinstance(node, ast.Constant):
            if node.value is None:
                return set()
            if isinstance(node.value, str):
                return {node.value}
            raise AssertionError(f"a remark is a code, not {node.value!r}")
        if isinstance(node, ast.IfExp):
            return codes(node.body) | codes(node.orelse)
        raise AssertionError(f"a remark is a declared code, not an expression: {ast.dump(node)[:80]}")

    written: set[str] = set()
    for statement in ast.walk(tree):
        if isinstance(statement, ast.Assign) and any(
            (isinstance(target, ast.Name) and target.id == "note")
            or (isinstance(target, ast.Attribute) and target.attr.endswith("note"))
            for target in statement.targets
        ):
            written.update(codes(statement.value))
        if isinstance(statement, ast.keyword) and statement.arg == "note":
            written.update(codes(statement.value))
    assert written, "no remark is written anywhere - the scanner stopped working"
    assert written == set(ic.NOTE_CODES), f"undeclared or unused remarks: {sorted(written ^ set(ic.NOTE_CODES))}"
    for code, sentence in ic.NOTE_CODES.items():
        assert sentence.strip().endswith("."), f"{code}: a remark shown in a log should read as a sentence"


def test_a_header_row_names_each_role_only_once() -> None:
    assert ic.header_roles(["answer", "question", "answer key"]) == {0: "answer", 1: "prompt"}
    assert ic.header_roles(["ürün", "açıklama"]) == {}


def test_split_options_leaves_alone_what_is_not_a_list() -> None:
    assert ic.split_options("") == []
    assert ic.split_options("yes | no") == ["yes", "no"]
    assert ic.split_options("Rome, Italy") == ["Rome, Italy"]


def test_a_heading_that_begins_with_a_number_is_not_read_as_a_stem() -> None:
    """A week title is not a question, whatever its first characters look like."""
    blocks = [Block(kind="heading", text="1. Week One", page=1, offset=0)]
    rows = ic._prose_candidates(blocks)
    assert [item.detected_kind for item in rows] == ["note"]
    assert rows[0].extracted["text"] == "1. Week One"
