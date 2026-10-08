"""The question engine: type registry, config validation, grading, answer hiding.

A `Question` stores cross-cutting attributes in columns and everything
type-specific in `config` (JSONB), so adding a question type never means a schema
change (see the design note in `app/models/content.py`). This module is the single
place that knows what each type means:

* `_REGISTRY` maps a type key to a descriptor: a pydantic model that validates the
  config, the grader for that type, and a projector that turns the authored config
  into the student-facing view.
* Scoring knobs are NOT in the config: they live in the normalized `Question.score`,
  `Question.partial_scoring` and `Question.negative_scoring` columns, because they
  cut across types. The grader receives them explicitly.
* `public_config` is the only supported way to show a question to someone who must
  not see the answer key. Which element an answer refers to is the part that must
  survive a reshuffle, so answers never reference display order: choice types answer
  with the stored option index (correctness is a flag on the option, not its place in
  the list), while matching and ordering answer with an opaque per-element `ref`.
  Publishing their authored indexes would publish the key, because for those two
  types the structure itself is the answer.

Nothing here touches the database: `app.services.question_service` does that, and
Phase 7 (attempts) grades from a frozen `QuestionVersion` snapshot.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

# --------------------------------------------------------------------------- #
# Shared pieces
# --------------------------------------------------------------------------- #


class _Config(BaseModel):
    """Every type config forbids unknown keys: config drift must fail loudly."""

    model_config = ConfigDict(extra="forbid")


class Normalization(_Config):
    """How a typed answer is compared with the accepted answers."""

    case_insensitive: bool = True
    trim: bool = True
    collapse_spaces: bool = True
    ignore_punctuation: bool = False
    ignore_articles: bool = False
    ignore_diacritics: bool = False


_PUNCT = re.compile(r"[!\"#$%&'()*+,\-./:;<=>?@[\\\]^_`{|}~«»„“”‘’¿¡‽]")
_ARTICLES = re.compile(r"\b(a|an|the|der|die|das|el|la|los|las|bir)\b", re.IGNORECASE)
_MARKER = re.compile(r"_{3,}|\{\{\s*\}\}")


def normalize_text(value: str, rules: Normalization) -> str:
    text = value
    if rules.trim:
        text = text.strip()
    if rules.collapse_spaces:
        text = re.sub(r"\s+", " ", text)
    if rules.case_insensitive:
        text = text.lower()
    if rules.ignore_diacritics:
        decomposed = unicodedata.normalize("NFKD", text)
        text = "".join(c for c in decomposed if not unicodedata.combining(c))
    if rules.ignore_punctuation:
        text = _PUNCT.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
    if rules.ignore_articles:
        text = _ARTICLES.sub(" ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text


class Option(_Config):
    text: str = Field(min_length=1, max_length=4000)


class ChoiceOption(Option):
    correct: bool = False


class GapBlank(_Config):
    accepted: list[str] = Field(min_length=1)
    label: str | None = Field(default=None, max_length=120)


class MatchingPair(_Config):
    left: str = Field(min_length=1, max_length=4000)
    right: str = Field(min_length=1, max_length=4000)


class RubricCriterion(_Config):
    name: str = Field(min_length=1, max_length=200)
    max_score: float | None = Field(default=None, gt=0)
    guidance: str | None = Field(default=None, max_length=2000)


# --------------------------------------------------------------------------- #
# Per-type configs
# --------------------------------------------------------------------------- #


class MultipleChoiceConfig(_Config):
    options: list[ChoiceOption] = Field(min_length=2, max_length=26)

    @field_validator("options")
    @classmethod
    def _exactly_one_correct(cls, v: list[ChoiceOption]) -> list[ChoiceOption]:
        correct = [o for o in v if o.correct]
        if len(correct) != 1:
            raise ValueError("multiple_choice needs exactly one correct option")
        return v


class MultiSelectConfig(_Config):
    options: list[ChoiceOption] = Field(min_length=2, max_length=26)

    @field_validator("options")
    @classmethod
    def _at_least_one_correct(cls, v: list[ChoiceOption]) -> list[ChoiceOption]:
        if not any(o.correct for o in v):
            raise ValueError("multi_select needs at least one correct option")
        if len(v) == sum(1 for o in v if o.correct):
            raise ValueError("multi_select needs at least one wrong option to select from")
        return v


class TrueFalseConfig(_Config):
    statement: str | None = Field(default=None, max_length=8000)
    correct: bool


class ShortAnswerConfig(_Config):
    accepted: list[str] = Field(min_length=1, max_length=40)
    normalization: Normalization = Field(default_factory=Normalization)
    max_characters: int | None = Field(default=None, ge=1, le=8000)
    hint: str | None = Field(default=None, max_length=500)


class GapFillConfig(_Config):
    """Cloze text: every `___` (or `{{}}`) marks one blank, in reading order."""

    text: str = Field(min_length=1, max_length=20000)
    blanks: list[GapBlank] = Field(min_length=1, max_length=40)
    normalization: Normalization = Field(default_factory=Normalization)
    word_bank: list[str] = Field(default_factory=list, max_length=60)

    @field_validator("blanks")
    @classmethod
    def _blank_answers_not_empty(cls, v: list[GapBlank]) -> list[GapBlank]:
        for blank in v:
            if not [a for a in blank.accepted if a.strip()]:
                raise ValueError("a gap blank needs at least one non-empty accepted answer")
        return v

    @model_validator(mode="after")
    def _markers_match_blanks(self):
        markers = len(_MARKER.findall(self.text))
        if markers != len(self.blanks):
            raise ValueError(
                f"the text has {markers} blank marker(s) but {len(self.blanks)} blank(s) are defined"
            )
        stray = [b for b in self.word_bank if not b.strip()]
        if stray:
            raise ValueError("word_bank entries must not be blank")
        return self


class MatchingConfig(_Config):
    pairs: list[MatchingPair] = Field(min_length=2, max_length=40)
    extra_rights: list[str] = Field(default_factory=list, max_length=40)

    @field_validator("pairs")
    @classmethod
    def _lefts_unique(cls, v: list[MatchingPair]) -> list[MatchingPair]:
        lefts = [p.left.strip().lower() for p in v]
        if len(lefts) != len(set(lefts)):
            raise ValueError("matching lefts must be distinct")
        return v


class OrderingConfig(_Config):
    """`items` are the elements in their correct sequence, position 0 first."""

    items: list[str] = Field(min_length=2, max_length=60)
    separator: str = Field(default=" ", max_length=10)
    show_separator: bool = False


class TranslationConfig(_Config):
    source: str = Field(min_length=1, max_length=8000)
    accepted: list[str] = Field(min_length=1, max_length=40)
    normalization: Normalization = Field(default_factory=Normalization)
    source_language: str | None = Field(default=None, max_length=16)
    target_language: str | None = Field(default=None, max_length=16)
    max_words: int | None = Field(default=None, ge=1, le=2000)


class EssayConfig(_Config):
    min_words: int | None = Field(default=None, ge=0, le=20000)
    max_words: int | None = Field(default=None, ge=1, le=50000)
    rubric: list[RubricCriterion] = Field(default_factory=list, max_length=20)
    guidance: str | None = Field(default=None, max_length=4000)

    @field_validator("max_words")
    @classmethod
    def _range_sane(cls, v: int | None, info):
        minimum = info.data.get("min_words")
        if v is not None and minimum is not None and v < minimum:
            raise ValueError("max_words must not be below min_words")
        return v


# --------------------------------------------------------------------------- #
# Grading results
# --------------------------------------------------------------------------- #


@dataclass
class GradeResult:
    score: float
    max_score: float
    correct: bool | None
    requires_manual: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "max_score": self.max_score,
            "correct": self.correct,
            "requires_manual": self.requires_manual,
            "detail": self.detail,
        }


PARTIAL_TYPES = {"multi_select", "gap_fill", "matching", "ordering"}
MANUAL_TYPES = {"essay"}

PartialMode = Literal["all_or_nothing", "partial"]


def _clamp(value: float, max_score: float) -> float:
    return max(0.0, min(max_score, round(value, 6)))


def _partial_mode(partial_scoring: dict | None) -> PartialMode:
    mode = (partial_scoring or {}).get("mode", "partial")
    return mode if mode in ("all_or_nothing", "partial") else "partial"


def _as_number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- #
# Element references
# --------------------------------------------------------------------------- #


def _ref(role: str, index: int, value: Any) -> str:
    """A stable, opaque handle for one element of a question.

    Derived from the element itself, so the same config always yields the same refs
    and a stored answer can be re-mapped without keeping any extra state. It is not
    the index, so showing it to a learner cannot show them where the element belongs.
    """
    digest = hashlib.sha256(f"{role}\x1f{index}\x1f{value}".encode()).hexdigest()
    return digest[:12]


def _matching_maps(config: dict) -> tuple[dict[str, int], dict[str, int]]:
    pairs = config["pairs"]
    lefts = {_ref("matching.left", i, pair["left"]): i for i, pair in enumerate(pairs)}
    rights = {_ref("matching.right", i, pair["right"]): i for i, pair in enumerate(pairs)}
    for offset, text in enumerate(config.get("extra_rights") or []):
        rights[_ref("matching.right", len(pairs) + offset, text)] = len(pairs) + offset
    return lefts, rights


def _ordering_map(config: dict) -> dict[str, int]:
    return {_ref("ordering.item", i, item): i for i, item in enumerate(config["items"])}


def _index_of(mapping: dict[str, int], value: Any) -> int | None:
    """A hand-crafted answer body can hold any JSON value, including unhashable ones;
    an unknown or malformed reference is a wrong answer, not a server error."""
    return mapping.get(value) if isinstance(value, str) else None


def _least_aligned(target: list[int], candidate: list[int]) -> list[int]:
    """Rotate `candidate` so it lines up with `target` as little as possible.

    Both lists are authored indexes in display order. A learner who pairs the columns
    (or reads them) straight down scores one mark per position where the two agree, so
    the published order is chosen to make that as close to nothing as the data allows.
    A rotation is used rather than a shuffle because the published order has to be the
    same on every request: answers are stored as refs, and randomising the display per
    request would make a shared or re-opened question behave inconsistently.
    """
    best = candidate
    best_hits = min(len(target), len(candidate)) + 1
    for shift in range(len(candidate)):
        rotated = candidate[shift:] + candidate[:shift]
        hits = sum(1 for left, right in zip(target, rotated, strict=False) if left == right)
        if hits < best_hits:
            best, best_hits = rotated, hits
        if best_hits == 0:
            break
    return best


# --------------------------------------------------------------------------- #
# Graders (pure functions: type config + student response -> score)
# --------------------------------------------------------------------------- #


def _is_index(value: Any) -> bool:
    """A JSON `true` is a Python `int`, but it is not an option index."""
    return isinstance(value, int) and not isinstance(value, bool)


def _grade_multiple_choice(config: dict, response: Any, max_score: float, *_: Any) -> GradeResult:
    options = config["options"]
    answer = _response_value(response, "option_index")
    if not _is_index(answer) or not 0 <= answer < len(options):
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
    correct_index = next(i for i, o in enumerate(options) if o["correct"])
    return GradeResult(
        max_score if answer == correct_index else 0.0,
        max_score,
        answer == correct_index,
        detail={"correct_index": correct_index, "given_index": answer},
    )


def _grade_multi_select(
    config: dict, response: Any, max_score: float, partial_scoring: dict, negative_scoring: dict
) -> GradeResult:
    options = config["options"]
    given = _response_list(response, "option_indexes")
    if given is None or not all(_is_index(index) for index in given):
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
    if len(set(given)) != len(given) or any(not 0 <= index < len(options) for index in given):
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer", "given": given})
    chosen = set(given)
    right = {i for i, o in enumerate(options) if o["correct"]}
    hits = len(chosen & right)
    misses = len(right - chosen)
    wrong = len(chosen - right)

    if _partial_mode(partial_scoring) == "all_or_nothing":
        ok = chosen == right
        return GradeResult(
            max_score if ok else 0.0, max_score, ok, detail={"hits": hits, "misses": misses, "wrong": wrong}
        )

    ratio = hits / len(right)
    # The default cancels one correct tick per wrong tick, because a learner who ticks
    # every box in a 'select all that apply' question must not walk away with full
    # marks. A teacher who wants pure positive marking sets the penalty to 0.
    penalty = _as_number((negative_scoring or {}).get("penalty"), 1.0)
    if penalty > 0 and len(options) - len(right) > 0:
        ratio -= penalty * wrong / (len(options) - len(right))
    score = _clamp(ratio * max_score, max_score)
    return GradeResult(score, max_score, chosen == right, detail={"hits": hits, "misses": misses, "wrong": wrong})


def _grade_true_false(config: dict, response: Any, max_score: float, *_: Any) -> GradeResult:
    answer = _response_value(response, "value")
    if not isinstance(answer, bool):
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
    ok = answer is bool(config["correct"])
    return GradeResult(max_score if ok else 0.0, max_score, ok, detail={"correct": config["correct"]})


def _matches(text: str, accepted: list[str], rules: Normalization) -> bool:
    if not text.strip():
        return False
    target = normalize_text(text, rules)
    return any(target == normalize_text(a, rules) for a in accepted if a.strip())


def _grade_short_answer(config: dict, response: Any, max_score: float, *_: Any) -> GradeResult:
    text = _response_text(response)
    if text is None:
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
    limit = config.get("max_characters")
    if limit and len(text) > limit:
        return GradeResult(0.0, max_score, False, detail={"reason": "too_long", "limit": limit})
    rules = Normalization.model_validate(config.get("normalization") or {})
    ok = _matches(text, config["accepted"], rules)
    return GradeResult(max_score if ok else 0.0, max_score, ok, detail={"given": text})


def _grade_gap_fill(
    config: dict, response: Any, max_score: float, partial_scoring: dict, _: Any
) -> GradeResult:
    given = _response_list(response, "blanks")
    if given is None:
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
    rules = Normalization.model_validate(config.get("normalization") or {})
    blanks = config["blanks"]
    padded = [str(given[i]) if i < len(given) else "" for i in range(len(blanks))]
    marks = [
        _matches(value, blank["accepted"], rules)
        for value, blank in zip(padded, blanks, strict=False)
    ]
    hits = sum(marks)
    if _partial_mode(partial_scoring) == "all_or_nothing":
        ok = hits == len(blanks)
        return GradeResult(
            max_score if ok else 0.0, max_score, ok, detail={"marks": marks, "hits": hits, "given": padded}
        )
    return GradeResult(
        _clamp(hits / len(blanks) * max_score, max_score),
        max_score,
        hits == len(blanks),
        detail={"marks": marks, "hits": hits, "given": padded},
    )


def _grade_matching(
    config: dict, response: Any, max_score: float, partial_scoring: dict, negative_scoring: dict
) -> GradeResult:
    pairs = config["pairs"]
    left_refs, right_refs = _matching_maps(config)
    given = _response_pairs(response)
    if given is None:
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
    if len(given) != len(pairs):
        return GradeResult(
            0.0, max_score, False, detail={"reason": "answer_incomplete", "expected": len(pairs), "given": len(given)}
        )
    resolved: dict[int, int] = {}
    for entry in given:
        li = _index_of(left_refs, entry.get("left_ref"))
        ri = _index_of(right_refs, entry.get("right_ref"))
        if li is None or ri is None:
            return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
        resolved[li] = ri
    if len(set(resolved.values())) != len(resolved):
        return GradeResult(0.0, max_score, False, detail={"reason": "answer_conflict"})
    # Pair i is authored so that its right element holds index i, and an extra
    # distractor index is beyond every left index, so a match is exactly ri == li.
    hits = sum(1 for li, ri in resolved.items() if ri == li)
    wrong = len(resolved) - hits

    if _partial_mode(partial_scoring) == "all_or_nothing":
        ok = hits == len(pairs)
        return GradeResult(max_score if ok else 0.0, max_score, ok, detail={"hits": hits, "wrong": wrong})

    ratio = hits / len(pairs)
    # A wrong link already lost its own mark through `hits`, so nothing is deducted
    # unless the teacher asks for it: unlike a multi-select there is no way to claim
    # extra credit by filling in more than the key.
    penalty = _as_number((negative_scoring or {}).get("penalty"), 0.0)
    if penalty > 0:
        ratio -= penalty * wrong / len(pairs)
    return GradeResult(
        _clamp(ratio * max_score, max_score), max_score, hits == len(pairs), detail={"hits": hits, "wrong": wrong}
    )


def _grade_ordering(
    config: dict, response: Any, max_score: float, partial_scoring: dict, _: Any
) -> GradeResult:
    items = config["items"]
    refs = _ordering_map(config)
    given = _response_list(response, "order")
    if given is None:
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
    placed: list[int] = []
    for ref in given:
        index = _index_of(refs, ref)
        if index is None:
            return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
        placed.append(index)
    if len(placed) != len(items) or sorted(placed) != list(range(len(items))):
        return GradeResult(
            0.0, max_score, False, detail={"reason": "answer_incomplete", "expected": len(items), "given": given}
        )
    hits = sum(1 for position, item_index in enumerate(placed) if item_index == position)
    if _partial_mode(partial_scoring) == "all_or_nothing":
        ok = hits == len(items)
        return GradeResult(max_score if ok else 0.0, max_score, ok, detail={"hits": hits})
    return GradeResult(
        _clamp(hits / len(items) * max_score, max_score), max_score, hits == len(items), detail={"hits": hits}
    )


def _grade_translation(config: dict, response: Any, max_score: float, *_: Any) -> GradeResult:
    text = _response_text(response)
    if text is None:
        return GradeResult(0.0, max_score, False, detail={"reason": "no_valid_answer"})
    rules = Normalization.model_validate(config.get("normalization") or {})
    limit = config.get("max_words")
    if limit and len([w for w in re.split(r"\s+", text.strip()) if w]) > limit:
        return GradeResult(0.0, max_score, False, detail={"reason": "too_many_words", "limit": limit})
    ok = _matches(text, config["accepted"], rules)
    return GradeResult(max_score if ok else 0.0, max_score, ok, detail={"given": text})


def _grade_essay(config: dict, response: Any, max_score: float, *_: Any) -> GradeResult:
    text = _response_text(response) or ""
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    detail: dict[str, Any] = {"word_count": len(words), "given": text}
    if not text.strip():
        detail["flagged"] = "empty"
    elif config.get("min_words") and len(words) < config["min_words"]:
        detail["flagged"] = "below_minimum"
    if config.get("max_words") and len(words) > config["max_words"]:
        detail["flagged"] = "above_maximum"
    # Nothing automatic about an essay: the teacher owns the score.
    return GradeResult(0.0, max_score, None, requires_manual=True, detail=detail)


# --------------------------------------------------------------------------- #
# Response readers (tolerant of both {"value": ...} and a bare value)
# --------------------------------------------------------------------------- #


def _response_value(response: Any, key: str) -> Any:
    if isinstance(response, dict):
        return response.get(key, response.get("value"))
    if key == "option_index" and isinstance(response, int):
        return response
    if key == "value" and isinstance(response, bool):
        return response
    return None


def _response_list(response: Any, key: str) -> list | None:
    raw = response.get(key) if isinstance(response, dict) else response
    if not isinstance(raw, list):
        return None
    return raw


def _response_text(response: Any) -> str | None:
    raw = response.get("text", response.get("value")) if isinstance(response, dict) else response
    return raw if isinstance(raw, str) else None


def _response_pairs(response: Any) -> list[dict] | None:
    raw = response.get("pairs") if isinstance(response, dict) else response
    if not isinstance(raw, list) or not all(isinstance(p, dict) for p in raw):
        return None
    return raw


# --------------------------------------------------------------------------- #
# Student-view projectors (answer key removed)
# --------------------------------------------------------------------------- #


def _public_multiple_choice(config: dict) -> dict:
    return {
        "kind": "options",
        "options": [{"index": i, "text": o["text"]} for i, o in enumerate(config["options"])],
    }


def _public_multi_select(config: dict) -> dict:
    public = _public_multiple_choice(config)
    public["multi"] = True
    return public


def _public_true_false(config: dict) -> dict:
    return {"kind": "boolean", "statement": config.get("statement")}


def _public_short_answer(config: dict) -> dict:
    return {
        "kind": "text",
        "max_characters": config.get("max_characters"),
        "hint": config.get("hint"),
        "accepted_count": len(config["accepted"]),
    }


def _public_gap_fill(config: dict) -> dict:
    return {
        "kind": "blanks",
        "text": config["text"],
        "blanks": [{"index": i, "label": b.get("label")} for i, b in enumerate(config["blanks"])],
        "word_bank": list(config.get("word_bank") or []),
    }


def _public_matching(config: dict) -> dict:
    # Which right element belongs to which left one *is* the answer, so neither column
    # is published in authored order and no position survives as a hint: each element
    # carries only its opaque ref, and the learner answers with refs.
    pairs = config["pairs"]
    extras = list(config.get("extra_rights") or [])
    left_refs, right_refs = _matching_maps(config)
    # The refs are unique per authored element, so inverting the maps is lossless.
    ref_of_left = {index: ref for ref, index in left_refs.items()}
    ref_of_right = {index: ref for ref, index in right_refs.items()}

    left_order = sorted(ref_of_left, key=lambda index: ref_of_left[index])
    right_order = _least_aligned(left_order, sorted(ref_of_right, key=lambda index: ref_of_right[index]))

    def right_text(index: int) -> str:
        return pairs[index]["right"] if index < len(pairs) else extras[index - len(pairs)]

    return {
        "kind": "matching",
        "lefts": [{"ref": ref_of_left[index], "text": pairs[index]["left"]} for index in left_order],
        # A distractor is published exactly like a real option: saying which entries are
        # the spare ones would narrow the key down for free.
        "rights": [{"ref": ref_of_right[index], "text": right_text(index)} for index in right_order],
    }


def _public_ordering(config: dict) -> dict:
    # The authored order IS the answer, so the tokens are published in a deterministic
    # order derived from their refs and then rotated off the key. A learner answers with
    # the refs in the sequence they believe to be correct.
    items = [str(item) for item in config["items"]]
    ref_of = {index: _ref("ordering.item", index, item) for index, item in enumerate(items)}
    order = _least_aligned(list(range(len(items))), sorted(ref_of, key=lambda index: ref_of[index]))
    return {
        "kind": "ordering",
        "separator": config.get("separator", " "),
        "show_separator": bool(config.get("show_separator")),
        "tokens": [{"ref": ref_of[index], "text": items[index]} for index in order],
    }


def _public_translation(config: dict) -> dict:
    return {
        "kind": "text",
        "source": config["source"],
        "source_language": config.get("source_language"),
        "target_language": config.get("target_language"),
        "max_words": config.get("max_words"),
        "accepted_count": len(config["accepted"]),
    }


def _public_essay(config: dict) -> dict:
    return {
        "kind": "essay",
        "min_words": config.get("min_words"),
        "max_words": config.get("max_words"),
        "rubric": [dict(c) for c in config.get("rubric") or []],
        "guidance": config.get("guidance"),
    }


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QuestionTypeDescriptor:
    key: str
    group: str
    answer_widget: str
    config_model: type[BaseModel]
    grader: Callable[..., GradeResult]
    public: Callable[[dict], dict]
    gradable_automatically: bool
    supports_partial: bool

    def validate(self, config: Any) -> dict:
        """Return the config as a plain dict, or raise ValueError with teacher-readable text."""
        try:
            return self.config_model.model_validate(config).model_dump(mode="json")
        except ValidationError as exc:
            raise ValueError(_humanize(exc)) from None

    def as_info(self) -> dict:
        return {
            "type": self.key,
            "group": self.group,
            "answer_widget": self.answer_widget,
            "gradable_automatically": self.gradable_automatically,
            "supports_partial": self.supports_partial,
            "config_schema": self.config_model.model_json_schema(),
        }


def _humanize(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = " › ".join(str(p) for p in err["loc"]) or "config"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


_REGISTRY: dict[str, QuestionTypeDescriptor] = {
    "multiple_choice": QuestionTypeDescriptor(
        key="multiple_choice",
        group="choice",
        answer_widget="single_option",
        config_model=MultipleChoiceConfig,
        grader=_grade_multiple_choice,
        public=_public_multiple_choice,
        gradable_automatically=True,
        supports_partial=False,
    ),
    "multi_select": QuestionTypeDescriptor(
        key="multi_select",
        group="choice",
        answer_widget="multi_option",
        config_model=MultiSelectConfig,
        grader=_grade_multi_select,
        public=_public_multi_select,
        gradable_automatically=True,
        supports_partial=True,
    ),
    "true_false": QuestionTypeDescriptor(
        key="true_false",
        group="choice",
        answer_widget="boolean",
        config_model=TrueFalseConfig,
        grader=_grade_true_false,
        public=_public_true_false,
        gradable_automatically=True,
        supports_partial=False,
    ),
    "short_answer": QuestionTypeDescriptor(
        key="short_answer",
        group="text",
        answer_widget="text",
        config_model=ShortAnswerConfig,
        grader=_grade_short_answer,
        public=_public_short_answer,
        gradable_automatically=True,
        supports_partial=False,
    ),
    "gap_fill": QuestionTypeDescriptor(
        key="gap_fill",
        group="text",
        answer_widget="blanks",
        config_model=GapFillConfig,
        grader=_grade_gap_fill,
        public=_public_gap_fill,
        gradable_automatically=True,
        supports_partial=True,
    ),
    "matching": QuestionTypeDescriptor(
        key="matching",
        group="text",
        answer_widget="pairs",
        config_model=MatchingConfig,
        grader=_grade_matching,
        public=_public_matching,
        gradable_automatically=True,
        supports_partial=True,
    ),
    "ordering": QuestionTypeDescriptor(
        key="ordering",
        group="text",
        answer_widget="order",
        config_model=OrderingConfig,
        grader=_grade_ordering,
        public=_public_ordering,
        gradable_automatically=True,
        supports_partial=True,
    ),
    "translation": QuestionTypeDescriptor(
        key="translation",
        group="text",
        answer_widget="text",
        config_model=TranslationConfig,
        grader=_grade_translation,
        public=_public_translation,
        gradable_automatically=True,
        supports_partial=False,
    ),
    "essay": QuestionTypeDescriptor(
        key="essay",
        group="manual",
        answer_widget="essay",
        config_model=EssayConfig,
        grader=_grade_essay,
        public=_public_essay,
        gradable_automatically=False,
        supports_partial=False,
    ),
}


def descriptor(question_type: str) -> QuestionTypeDescriptor:
    desc = _REGISTRY.get(question_type)
    if desc is None:
        raise ValueError(
            f"Unknown question type '{question_type}'. Known types: "
            + ", ".join(sorted(_REGISTRY))
        )
    return desc


def is_known(question_type: str) -> bool:
    return question_type in _REGISTRY


def type_keys() -> list[str]:
    return sorted(_REGISTRY)


def registry_info() -> list[dict]:
    return [_REGISTRY[key].as_info() for key in type_keys()]


def validate_config(question_type: str, config: Any) -> dict:
    """Validate and canonicalise a type payload; ValueError is the caller's to show."""
    if not isinstance(config, dict):
        raise ValueError("config must be a JSON object")
    return descriptor(question_type).validate(config)


def validate_scoring(question_type: str, score: float, partial_scoring: dict, negative_scoring: dict) -> None:
    """Score knobs live on the question row, so they are checked against the type here."""
    if score <= 0:
        raise ValueError("score must be greater than zero")
    mode = (partial_scoring or {}).get("mode")
    if mode is not None and mode not in ("all_or_nothing", "partial"):
        raise ValueError("partial_scoring.mode must be 'all_or_nothing' or 'partial'")
    if mode is not None and question_type not in PARTIAL_TYPES:
        raise ValueError(f"{question_type} has no partial credit, so partial_scoring must stay empty")
    penalty = (negative_scoring or {}).get("penalty")
    if penalty is not None:
        if not isinstance(penalty, (int, float)) or penalty < 0 or penalty > 1:
            raise ValueError("negative_scoring.penalty must be a number between 0 and 1")
        if question_type not in ("multi_select", "matching"):
            raise ValueError(f"{question_type} has no wrong answers to penalise")
    unknown = (set(partial_scoring or {}) - {"mode"}) | (set(negative_scoring or {}) - {"penalty"})
    if unknown:
        raise ValueError(f"unsupported scoring keys: {', '.join(sorted(unknown))}")


def public_config(question_type: str, config: Any) -> dict:
    """Student-facing view of a question: the answer key is never included."""
    return descriptor(question_type).public(dict(config or {}))


def grade(
    question_type: str,
    config: Any,
    response: Any,
    *,
    score: float = 1.0,
    partial_scoring: dict | None = None,
    negative_scoring: dict | None = None,
) -> GradeResult:
    """Grade one student response for one question, in isolation.

    `config` must already be validated (creation and editing do that), because a
    stored config that no longer validates is a data problem to surface, not to
    grade around.
    """
    desc = descriptor(question_type)
    payload = dict(config or {})
    return desc.grader(payload, response, float(score), partial_scoring or {}, negative_scoring or {})


def grade_snapshot(snapshot: dict, response: Any) -> GradeResult:
    """Grade through a frozen `QuestionVersion` snapshot (what Phase 7 will call).

    The snapshot carries the question exactly as it was when it was versioned, so a
    later edit can never change how a past answer is scored.
    """
    question = snapshot.get("question", snapshot)
    return grade(
        question["type"],
        question.get("config"),
        response,
        score=question.get("score", 1.0),
        partial_scoring=question.get("partial_scoring"),
        negative_scoring=question.get("negative_scoring"),
    )


def expects_answer(question_type: str) -> bool:
    """True when the engine can decide correctness by itself."""
    return descriptor(question_type).gradable_automatically
