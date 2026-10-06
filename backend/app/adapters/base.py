"""Adapter contracts (typing.Protocol) + shared dataclasses.

Business code depends on these interfaces only. Concrete providers live in the
sibling modules and are selected via the registry / Admin Settings.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class AIClassification:
    question_type: str | None = None
    level: str | None = None
    topics: list[str] = field(default_factory=list)
    confidence: float | None = None
    raw: dict = field(default_factory=dict)


@dataclass
class GradeSuggestion:
    verdict: str  # correct | partially_correct | incorrect
    suggested_score: float
    max_score: float
    confidence: float
    reason: str


@runtime_checkable
class AIProvider(Protocol):
    """OpenAI-compatible local endpoint (Ollama) or Gemini. Never required."""

    name: str

    def enabled(self) -> bool: ...

    async def classify_question(self, text: str, image_hint: str | None = None) -> AIClassification: ...

    async def suggest_answer(self, question: dict, context: str | None = None) -> dict: ...

    async def grade_open_answer(
        self, question: dict, expected: list[str], response: str, max_score: float
    ) -> GradeSuggestion: ...

    async def enrich_vocabulary(self, word: str, language: str) -> dict: ...


@runtime_checkable
class OCRProvider(Protocol):
    """PaddleOCR / Tesseract compatible. Returns plain text + confidence."""

    name: str

    def enabled(self) -> bool: ...

    async def extract_text(self, image_bytes: bytes, language: str | None = None) -> "OCRResult": ...


@dataclass
class OCRResult:
    text: str
    confidence: float
    blocks: list[dict] = field(default_factory=list)


@runtime_checkable
class DocumentParser(Protocol):
    """Docling-compatible: parse a document into ordered sections/blocks."""

    name: str

    def enabled(self) -> bool: ...

    async def parse(self, file_bytes: bytes, mime_type: str) -> "ParsedDocument": ...


@dataclass
class ParsedDocument:
    pages: list[dict] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    sections: list[dict] = field(default_factory=list)
    plain_text: str = ""


@runtime_checkable
class SpeechToTextProvider(Protocol):
    """Local Whisper-compatible transcription."""

    name: str

    def enabled(self) -> bool: ...

    async def transcribe(self, audio_bytes: bytes, language: str | None = None) -> "Transcript": ...


@dataclass
class Transcript:
    text: str
    language: str | None
    segments: list[dict] = field(default_factory=list)  # [{start, end, text}]


@runtime_checkable
class TranslationProvider(Protocol):
    """LibreTranslate/Argos-compatible."""

    name: str

    def enabled(self) -> bool: ...

    async def translate(self, text: str, source: str, target: str) -> str: ...


@runtime_checkable
class DictionaryProvider(Protocol):
    """Wiktionary/Wiktextract/Kaikki-compatible enrichment data."""

    name: str

    def enabled(self) -> bool: ...

    async def lookup(self, word: str, language: str) -> dict: ...


class AdapterUnavailable(RuntimeError):
    """Raised when a task needs a provider that is disabled in settings."""
