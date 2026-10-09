"""What the document's own text can be filed as, decided without inventing anything.

This is the half of the importer a teacher never sees and always feels. `doc_types` says
what a file is, `document_parsing` returns the characters inside it, and this module says
what those characters look like: a table whose first row reads `word | meaning` is a list
of vocabulary entries, a paragraph that starts `12.` and is followed by four lines
beginning `A)` to `D)` is a multiple-choice question.

The line this module will not cross is the one the whole product rests on: **a candidate
never contains an answer the document did not give.** A question paper's options are its
own; which of them is correct usually lives in a separate key, in a different part of the
file, or only in the teacher's head. Guessing it from the first option, from letter
frequency, or from an AI that is not configured here would produce a bank of plausible
wrong answers - the one outcome a language teacher cannot afford, because it is graded
silently and forever. So an option list with no key becomes a candidate that *says* it is
missing the answer, and the review screen cannot approve it until a person supplies one.

Everything this module cannot classify is still kept, as a `note` candidate holding the
document's own words. Dropping a readable paragraph would make the import look empty when
it was not, and a teacher who sees the note can decide it is a passage, a word or nothing.

Kinds are deliberately the three the banks already hold - question, vocabulary, reading -
plus `note`, which is not a content type and cannot be approved as one. Listening is
absent on purpose: a listening is a recording with words attached, and the recording is
added through the media library, not out of a document.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from app.core import doc_types
from app.services.document_parsing import Block, ParsedText

#: The four things a candidate can be. `note` is the honest "readable, unclassified".
KINDS = ("question", "vocabulary", "reading", "note")

#: How much continuous prose counts as a passage rather than a sentence. Below this the
#: importer has seen a paragraph, not a text, and calling it a reading would fill the bank
#: with fragments a learner is then asked to read.
_MIN_PASSAGE_CHARACTERS = 200

#: A question needs this many options to be a choice question at all, and no more than
#: this many: a run of nine lettered lines is a list of something else.
_MIN_OPTIONS = 2
_MAX_OPTIONS = 8

#: Header roles and the column names teachers actually write for them, in the platform's
#: interface languages. Matching is on a folded form of the cell (see `_fold`), so
#: "Answer", "answer key" and "ANSWER_KEY" name one role.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "prompt": ("question", "prompt", "stem", "question text", "soru", "вопрос", "task"),
    "options": ("options", "choices", "alternatives", "variants", "şıklar", "variantlar", "варианты"),
    "answer": (
        "answer",
        "answers",
        "correct",
        "correct answer",
        "key",
        "answer key",
        "doğru",
        "cevap",
        "cavab",
        "ответ",
        "правильный ответ",
    ),
    "word": ("word", "term", "vocabulary", "söz", "слово", "термин"),
    "meaning": (
        "meaning",
        "definition",
        "anlam",
        "izah",
        "tərcümə",
        "значение",
        "определение",
        "explanation",
    ),
    "translation": ("translation", "translations", "tercüme", "перевод"),
    "example": ("example", "examples", "sentence", "cümle", "nümunə", "пример"),
    "title": ("title", "heading", "passage title", "başlık", "ad", "название"),
    "body": ("text", "body", "passage", "reading", "reading text", "metin", "текст"),
    "level": ("level", "seviye", "dərəcə", "уровень"),
    "type": ("type", "question type", "kind", "tip", "тип"),
}

#: A single-letter column heading is a choice list in the shape a teacher types by hand:
#: one option per column, `a`, `b`, `c` in order.
_LETTER_COLUMNS = tuple("ABCDEFGH"[:_MAX_OPTIONS])

#: How an options cell that is one column wide separates its choices. Vertical bars and
#: newlines are unambiguous; a semicolon is, because a comma inside "Rome, Italy" is not.
_OPTION_SPLIT_RE = re.compile(r"\s*(?:\||\n|;)\s*")

_STEM_HINT_RE = re.compile(r"^\s*(?:\d{1,3}[.)\]:]|Q(?:uestion)?\s*\d+[.)\:]?\s)", re.IGNORECASE)
_OPTION_LINE_RE = re.compile(r"^\s*([A-H])[.)\]:]\s*(.+)$", re.DOTALL)
_INLINE_OPTIONS_RE = re.compile(r"([A-H])[.)\]:]\s*")
_ANSWER_LETTER_RE = re.compile(r"^\s*\(?\s*([A-Ha-h])\s*[)\].,:]?\s*$")
_ANSWER_NUMBER_RE = re.compile(r"^\s*\(?\s*(\d{1,2})\s*[)\].,:]?\s*$")
_SENTENCE_END_RE = re.compile(r"[.!?。؟]\s*$")

#: What the review screen needs to know a candidate is incomplete, per kind. The words are
#: codes, not sentences: the browser translates them (`imports.missing.*`).
_REQUIRED: dict[str, tuple[str, ...]] = {
    "question": ("prompt",),
    "vocabulary": ("word",),
    "reading": ("title", "body"),
    "note": (),
}


def _fold(value: str | None) -> str:
    """A header cell in the shape the alias lists are written in.

    Case, spacing, punctuation and diacritics are what a person's keyboard decides, not
    what the column means, so they are removed before comparing: NFKD without combining
    marks, casefolded, with anything that is not a letter or digit left out.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z\u0400-\u04ff]+", "", text)


#: `folded header -> role`, built once from the alias table.
_HEADER_LOOKUP: dict[str, str] = {}
for _role, _aliases in _HEADER_ALIASES.items():
    for _alias in _aliases:
        _HEADER_LOOKUP.setdefault(_fold(_alias), _role)


#: Every remark this module can leave on a candidate, in its own words. The candidate stores
#: the code, because the review screen is read in four languages and a sentence written here
#: would be English in front of a teacher in Baku; `imports.note_<code>` is where those
#: languages say it. The English stays for a colleague's script and for the log line.
NOTE_CODES: dict[str, str] = {
    "answer_not_an_option": "The answer does not name any of the options, so none was marked correct.",
    "options_without_answer": "The document lists options but no answer.",
    "no_answer_for_prompt": "Nothing in the document gives an answer for this prompt.",
    "word_without_meaning": "The row gives a word but no meaning for it.",
    "passage_without_heading": "The passage has no heading of its own; its first sentence was used as the title.",
    "unmatched_header": "The header names columns this importer has no rule for, so the row was kept as text.",
    "row_without_role": "This row fills none of the columns this importer files, so it was kept as text.",
}


@dataclass
class Candidate:
    """One thing the document appears to contain, with where it came from.

    `extracted` only ever holds the document's own characters (plus an option list split
    out of them). `missing` names the fields a person still has to supply before this can
    become content, and `confidence` is the importer's honest estimate of how much of the
    candidate came from the file rather than from its shape.
    """

    detected_kind: str
    detected_type: str | None = None
    extracted: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    confidence: float = 0.5
    source_page: int | None = None
    source_sheet: str | None = None
    note: str | None = None


def header_roles(cells: list[str]) -> dict[int, str]:
    """Which column does what, from the first row's words, or `{}` when it says nothing.

    A role appears once: a file with two `answer` columns is a file whose shape this module
    does not understand, and understanding it wrongly is worse than not claiming to.
    """
    out: dict[int, str] = {}
    taken: set[str] = set()
    for index, cell in enumerate(cells):
        role = _HEADER_LOOKUP.get(_fold(cell))
        if role is None or role in taken:
            continue
        out[index] = role
        taken.add(role)
    return out


def split_options(cell: str) -> list[str]:
    """The choices one cell holds, whether they are bar-separated or lettered.

    `A) Rome B) Paris C) Lisbon` in a single cell is the same option list as three
    columns, and a teacher who typed it that way in a spreadsheet gets it read that way.
    Letters that run from A in order are stripped, because the letter only says which
    option it is; letters that skip one are left in the text they mark, because a list
    that reads `A) … C) …` is a transcription mistake, and the only honest reading of it
    is the one that shows the person exactly what they typed.
    """
    text = (cell or "").strip()
    if not text:
        return []
    marks = list(_INLINE_OPTIONS_RE.finditer(text))
    letters = [mark.group(1).upper() for mark in marks]
    if _MIN_OPTIONS <= len(marks) <= _MAX_OPTIONS and len(set(letters)) == len(marks):
        in_order = letters == list(_LETTER_COLUMNS[: len(letters)])
        out: list[str] = []
        for position, mark in enumerate(marks):
            stop = marks[position + 1].start() if position + 1 < len(marks) else len(text)
            start = mark.end() if in_order else mark.start()
            out.append(text[start:stop].strip(" ;,"))
        return [piece for piece in out if piece]
    return [piece.strip() for piece in _OPTION_SPLIT_RE.split(text) if piece.strip()]


def _answer_index(answer: str | None, options: list[str]) -> int | None:
    """Which option the document's own answer names, or None when it names none.

    A bare letter, a 1-based number and the option's own text are all things teachers put
    in an answer column, so all three are honoured - and nothing is chosen when they
    disagree, because the closest match is still a guess.
    """
    raw = (answer or "").strip()
    if not raw or not options:
        return None
    letter = _ANSWER_LETTER_RE.match(raw)
    if letter:
        index = ord(letter.group(1).upper()) - 65
        return index if 0 <= index < len(options) else None
    number = _ANSWER_NUMBER_RE.match(raw)
    if number:
        index = int(number.group(1)) - 1
        return index if 0 <= index < len(options) else None
    folded = _fold(raw)
    hits = [position for position, option in enumerate(options) if _fold(option) == folded]
    return hits[0] if len(hits) == 1 else None


def _question_candidate(
    prompt: str,
    options: list[str],
    answer: str | None,
    *,
    level: str | None = None,
    explanation: str | None = None,
    question_type: str | None = None,
    page: int | None = None,
    sheet: str | None = None,
) -> Candidate:
    """One question, with the answer only when the document gave one.

    The type is what the document supports rather than what would look better: options and
    an answer that names one of them is `multiple_choice`, an answer with no options is
    `short_answer`, and options with no answer stay `multiple_choice` with the correct flag
    missing - which is a candidate a person has to finish, not a question.
    """
    options = [option.strip() for option in options if option and option.strip()]
    chosen = _answer_index(answer, options) if options else None
    extracted: dict = {"prompt": prompt.strip()}
    missing: list[str] = []
    note: str | None = None

    if options:
        extracted["options"] = [
            {"text": option, "correct": position == chosen} for position, option in enumerate(options)
        ]
        detected_type = question_type or "multiple_choice"
        if chosen is None:
            missing.append("answer")
            note = (
                "answer_not_an_option"
                if (answer or "").strip()
                else "options_without_answer"
            )
    elif (answer or "").strip():
        detected_type = question_type or "short_answer"
        extracted["accepted"] = [answer.strip()]
    else:
        detected_type = question_type or "short_answer"
        missing.append("answer")
        note = "no_answer_for_prompt"

    if level:
        extracted["level"] = level.strip()
    if explanation:
        extracted["explanation"] = explanation.strip()
    if (answer or "").strip():
        extracted["answer_text"] = answer.strip()

    confidence = 0.9 if options and chosen is not None else 0.55 if options else 0.45
    return Candidate(
        detected_kind="question",
        detected_type=detected_type,
        extracted=extracted,
        missing=missing,
        confidence=confidence,
        source_page=page,
        source_sheet=sheet,
        note=note,
    )


def _vocabulary_candidate(
    word: str,
    *,
    meaning: str | None = None,
    example: str | None = None,
    level: str | None = None,
    page: int | None = None,
    sheet: str | None = None,
) -> Candidate:
    extracted: dict = {"word": word.strip(), "definition": (meaning or "").strip()}
    if example:
        extracted["examples"] = [{"sentence": example.strip()}]
    if level:
        extracted["level"] = level.strip()
    # A word with no meaning is still the document's word; it just is not a bank entry
    # until a person says what it means.
    return Candidate(
        detected_kind="vocabulary",
        extracted=extracted,
        missing=[] if meaning else ["definition"],
        confidence=0.85 if meaning else 0.5,
        source_page=page,
        source_sheet=sheet,
        note=None if meaning else "word_without_meaning",
    )


def _reading_candidate(
    title: str | None,
    paragraphs: list[str],
    *,
    level: str | None = None,
    page: int | None = None,
    sheet: str | None = None,
) -> Candidate:
    body = "\n\n".join(piece.strip() for piece in paragraphs if piece.strip())
    extracted = {"title": (title or "").strip() or _first_sentence(body), "body": body}
    if level:
        extracted["level"] = level.strip()
    return Candidate(
        detected_kind="reading",
        extracted=extracted,
        confidence=0.8 if title else 0.6,
        source_page=page,
        source_sheet=sheet,
        note=None if title else "passage_without_heading",
    )


def _first_sentence(body: str) -> str:
    """The opening of a passage, for a title slot a document left empty."""
    head = re.split(r"(?<=[.!?。؟])\s+", body.strip(), maxsplit=1)[0]
    return (head[:120] or "Imported text").strip()


def _note(text: str, page: int | None = None, sheet: str | None = None) -> Candidate:
    return Candidate(
        detected_kind="note",
        extracted={"text": text},
        confidence=0.3,
        source_page=page,
        source_sheet=sheet,
    )


# --------------------------------------------------------------------------- #
# Tables: csv, tsv, xlsx
# --------------------------------------------------------------------------- #


def _rows_of_table(parsed: ParsedText) -> list[tuple[int | None, str | None, list[str]]]:
    """(row number, sheet, cells) for every row block that has cells.

    A delimited file has one implicit sheet and numbers its rows; a workbook names its
    sheets and restarts its numbering. Both arrive here as the same shape so the column
    rules run once.
    """
    return [
        (block.page, block.sheet, block.cells)
        for block in parsed.blocks
        if block.kind == "table_row"
    ]


def _letter_option_columns(cells: list[str]) -> dict[int, str]:
    """`{column: "option"}` when the header is a row of bare letters.

    `question | a | b | c | answer` is how a printed paper looks when someone types it
    into a spreadsheet, and it says the same thing as one `options` column. The letters
    have to run from A without a gap, because one missing column would silently shift
    every answer in the file.
    """
    letters = [
        (position, value.strip().upper())
        for position, value in enumerate(cells)
        if len(value.strip()) == 1 and value.strip().upper() in _LETTER_COLUMNS
    ]
    if len(letters) < _MIN_OPTIONS:
        return {}
    if [letter for _, letter in letters] != list(_LETTER_COLUMNS[: len(letters)]):
        return {}
    return {position: "option" for position, _ in letters}


def _table_candidates(rows: list[tuple[int | None, str | None, list[str]]]) -> list[Candidate]:
    """Every candidate a column-shaped document holds.

    Each sheet is read on its own terms: its first row is its header when that row names at
    least one known role. A workbook whose `Words` sheet lists vocabulary and whose
    `Questions` sheet lists a paper would otherwise file the second sheet's headings as
    words, which is the one thing a per-file header cannot get wrong.

    When a sheet's first row names no role at all, the whole sheet is still kept - as notes -
    because a file whose headings are in a language this module has never seen is a file full
    of content, not an empty one. Those notes say which header they came from, because a queue
    of unexplained text cards reads like a crash rather than like a file this importer could
    not shape.
    """
    out: list[Candidate] = []
    for sheet, its_rows in _by_sheet(rows):
        roles = header_roles(its_rows[0][2])
        roles.update(_letter_option_columns(its_rows[0][2]))
        if not roles:
            for page, _ignored, cells in its_rows:
                row = _note(" | ".join(cell for cell in cells if cell), page, sheet)
                row.note = "unmatched_header"
                out.append(row)
            continue
        for page, _ignored, cells in its_rows[1:]:
            values = {
                role: (cells[position].strip() if position < len(cells) else "")
                for position, role in roles.items()
                if role != "option"
            }
            options = [
                cells[position].strip()
                for position, role in sorted(roles.items())
                if role == "option" and position < len(cells) and cells[position].strip()
            ]
            if not any(values.values()) and not options:
                continue
            out.append(_row_candidate(values, options, page=page, sheet=sheet))
    return out


def _by_sheet(
    rows: list[tuple[int | None, str | None, list[str]]]
) -> list[tuple[str | None, list[tuple[int | None, str | None, list[str]]]]]:
    """The rows grouped by sheet, in the order the sheets first appear."""
    order: list[str | None] = []
    groups: dict[str | None, list[tuple[int | None, str | None, list[str]]]] = {}
    for row in rows:
        if row[1] not in groups:
            order.append(row[1])
            groups[row[1]] = []
        groups[row[1]].append(row)
    return [(sheet, groups[sheet]) for sheet in order]


def _row_candidate(
    values: dict[str, str],
    options: list[str],
    *,
    page: int | None,
    sheet: str | None,
) -> Candidate:
    """One row, filed by which roles it filled.

    A row can name a question, a word and a text all at once - a reading table with a
    `question` column does exactly that. The order below is the order of information:
    a prompt or an option list says a question, a word says a vocabulary entry, a body
    says a passage. A row that says none of those is kept as a note.
    """
    level = values.get("level") or None
    if values.get("prompt") or options:
        return _question_candidate(
            values.get("prompt", ""),
            options or split_options(values.get("options", "")),
            values.get("answer"),
            level=level,
            explanation=values.get("meaning") or None,
            question_type=(values.get("type") or "").strip() or None,
            page=page,
            sheet=sheet,
        )
    if values.get("word"):
        return _vocabulary_candidate(
            values["word"],
            meaning=values.get("meaning") or values.get("translation") or None,
            example=values.get("example") or None,
            level=level,
            page=page,
            sheet=sheet,
        )
    if values.get("body"):
        return _reading_candidate(
            values.get("title") or None, [values["body"]], level=level, page=page, sheet=sheet
        )
    joined = " | ".join(piece for piece in values.values() if piece)
    row = _note(joined, page, sheet)
    row.confidence = 0.35
    row.note = "row_without_role"
    return row


# --------------------------------------------------------------------------- #
# Prose: docx, pdf, txt, md
# --------------------------------------------------------------------------- #


def _prose_candidates(blocks: list[Block]) -> list[Candidate]:
    """Questions, passages and notes out of a stream of paragraphs, in reading order.

    Three shapes are recognised because three shapes are what a language paper is made
    of: a numbered stem followed by lettered options, a heading followed by enough
    paragraphs to be a text, and anything else, which is kept as a note. Notes are
    emitted last in a run so a passage that turns out to be too short still appears in
    the order the document shows it.
    """
    out: list[Candidate] = []
    position = 0
    while position < len(blocks):
        block = blocks[position]
        if block.kind == "table_row" and block.cells:
            out.append(_cells_candidate(block))
            position += 1
            continue
        if block.kind != "table_row" and _STEM_HINT_RE.match(block.text):
            options, consumed = _following_options(blocks, position + 1)
            if len(options) >= _MIN_OPTIONS:
                out.append(_prose_question(block.text, options, page=block.page))
                position += 1 + consumed
                continue
        if block.kind == "heading":
            passage, consumed = _passage_after_heading(blocks, position)
            if passage is not None:
                out.append(passage)
                position += consumed
                continue
        if _is_long(block.text):
            passage, consumed = _passage_without_heading(blocks, position)
            if passage is not None:
                out.append(passage)
                position += consumed
                continue
        out.append(_note(block.text, block.page))
        position += 1
    return out


def _is_long(text: str) -> bool:
    return len(text.strip()) >= _MIN_PASSAGE_CHARACTERS


def _following_options(blocks: list[Block], start: int) -> tuple[list[str], int]:
    """The lettered lines that follow a stem, and how many blocks they took.

    Options stop at the first line that is not the next letter in order, so the following
    question's stem is never swallowed as option `E)`.
    """
    options: list[str] = []
    consumed = 0
    for block in blocks[start:]:
        if block.kind == "table_row":
            break
        match = _OPTION_LINE_RE.match(block.text)
        if not match or match.group(1).upper() != chr(65 + len(options)):
            break
        options.append(match.group(2).strip())
        consumed += 1
        if len(options) >= _MAX_OPTIONS:
            break
    return options, consumed


def _prose_question(prompt: str, options: list[str], *, page: int | None) -> Candidate:
    """A stem with options and no answer: the paper's own words, unfinished on purpose."""
    bare = re.sub(r"^\s*(?:\d{1,3}[.)\]:]|Q(?:uestion)?\s*\d+[.)\:]?)\s*", "", prompt, count=1)
    return _question_candidate(bare.strip() or prompt.strip(), options, None, page=page)


def _passage_after_heading(blocks: list[Block], start: int) -> tuple[Candidate | None, int]:
    """A heading and the paragraphs under it, when they add up to a text."""
    title = blocks[start].text.strip()
    paragraphs, consumed = _paragraph_run(blocks, start + 1)
    if sum(len(piece) for piece in paragraphs) < _MIN_PASSAGE_CHARACTERS:
        return None, 0
    return _reading_candidate(title, paragraphs, page=blocks[start].page), 1 + consumed


def _passage_without_heading(blocks: list[Block], start: int) -> tuple[Candidate | None, int]:
    """Consecutive long paragraphs as one text.

    Without a heading the passage gets its first sentence as a title, which is why a
    passage found this way carries a lower confidence: the importer guessed where it began.
    """
    paragraphs, consumed = _paragraph_run(blocks, start)
    if sum(len(piece) for piece in paragraphs) < _MIN_PASSAGE_CHARACTERS:
        return None, 0
    return _reading_candidate(None, paragraphs, page=blocks[start].page), consumed


def _paragraph_run(blocks: list[Block], start: int) -> tuple[list[str], int]:
    """The prose paragraphs from `start`, stopping at a heading, a stem or a table.

    A question stem interrupts a passage because a paper puts its text and its questions
    in that order; the paragraph that ends with a full stop and is followed by a short
    line is where a text stops being one.
    """
    paragraphs: list[str] = []
    position = start
    while position < len(blocks):
        block = blocks[position]
        if block.kind in ("heading", "table_row") or _STEM_HINT_RE.match(block.text):
            break
        text = block.text.strip()
        if not text:
            break
        paragraphs.append(text)
        position += 1
        if _SENTENCE_END_RE.search(text) and position < len(blocks) and len(blocks[position].text.strip()) < 40:
            break
    return paragraphs, position - start


def _cells_candidate(block: Block) -> Candidate:
    """A table row inside a document, filed by how many cells it has.

    A two-column Word table is how a vocabulary list reaches a .docx, so it is read as
    one. Three or more cells under no header say too little about which column is which,
    and the row is kept whole as a note instead of a guess - unless its last cell names
    one of the ones before it, which is an answer key a document wrote for itself.
    """
    cells = [cell.strip() for cell in block.cells]
    while cells and not cells[-1]:
        cells.pop()
    if len(cells) == 2 and all(cells):
        return _vocabulary_candidate(cells[0], meaning=cells[1], page=block.page, sheet=block.sheet)
    if len(cells) >= 3 and cells[0] and _answer_index(cells[-1], cells[1:-1]) is not None:
        return _question_candidate(cells[0], cells[1:-1], cells[-1], page=block.page, sheet=block.sheet)
    return _note(" | ".join(cell for cell in cells if cell), block.page, block.sheet)


# --------------------------------------------------------------------------- #
# Grouping and entry point
# --------------------------------------------------------------------------- #


def _group_pdf_lines(blocks: list[Block]) -> list[Block]:
    """One page's drawn lines back into paragraphs, for a PDF only.

    A PDF's content stream says where each fragment sat, and `document_parsing` returns
    one block per visual line. Joining two lines is not a guess when the first one stops
    mid-sentence: a full stop, a colon or a short line closes a paragraph, and a line that
    starts with a number or a letter option stays on its own so the question rules still
    see it as its own item.
    """
    out: list[Block] = []
    for block in blocks:
        text = block.text.strip()
        if not text:
            continue
        starts_new = bool(_STEM_HINT_RE.match(text) or _OPTION_LINE_RE.match(text))
        previous = out[-1] if out else None
        if (
            previous is not None
            and previous.page == block.page
            and not starts_new
            and not _closes_run(previous.text)
        ):
            previous.text = f"{previous.text} {text}"
            continue
        out.append(Block(kind="paragraph", text=text, page=block.page, sheet=block.sheet))
    return out


def _closes_run(text: str) -> bool:
    """Whether a finished line ends the paragraph it belongs to."""
    return bool(_SENTENCE_END_RE.search(text)) or _STEM_HINT_RE.match(text) is not None


def candidates(parsed: ParsedText, fmt: doc_types.DocFormat) -> list[Candidate]:
    """Every candidate the document holds, in the order the document shows it.

    Tables are read by columns and prose by shape, which is the difference between a
    vocabulary export and a printed paper; both paths keep what they cannot classify.
    """
    if fmt.name in ("csv", "tsv", "xlsx"):
        return _table_candidates(_rows_of_table(parsed))
    blocks = parsed.blocks
    if fmt.name == "pdf":
        blocks = _group_pdf_lines(blocks)
    return _prose_candidates(blocks)


def required_fields(kind: str) -> tuple[str, ...]:
    """The fields a candidate of this kind needs before it can become content."""
    return _REQUIRED.get(kind, ())
