"""Concrete + disabled adapter implementations.

Only LocalAI (any OpenAI-compatible endpoint such as Ollama) and optional Gemini
are implemented here; the others return a null provider until their backends are
configured. Every null provider reports enabled()=False so the platform keeps
running with zero AI dependency.
"""
from __future__ import annotations

import httpx

from app.adapters.base import (
    AdapterUnavailable,
    AIClassification,
    GradeSuggestion,
    OCRResult,
    ParsedDocument,
    Transcript,
)


class DisabledProvider:
    """Base for any provider the teacher has not enabled."""

    name = "none"

    def enabled(self) -> bool:
        return False

    def _raise(self, what: str) -> None:
        raise AdapterUnavailable(f"{what} provider is not enabled")


class NullOCR(DisabledProvider):
    async def extract_text(self, image_bytes: bytes, language: str | None = None) -> OCRResult:
        self._raise("OCR")


class NullDocumentParser(DisabledProvider):
    async def parse(self, file_bytes: bytes, mime_type: str) -> ParsedDocument:
        self._raise("Document parser")


class NullSTT(DisabledProvider):
    async def transcribe(self, audio_bytes: bytes, language: str | None = None) -> Transcript:
        self._raise("Speech-to-text")


class NullTranslation(DisabledProvider):
    async def translate(self, text: str, source: str, target: str) -> str:
        self._raise("Translation")


class NullDictionary(DisabledProvider):
    async def lookup(self, word: str, language: str) -> dict:
        self._raise("Dictionary")


class NullAI(DisabledProvider):
    async def classify_question(self, text: str, image_hint: str | None = None) -> AIClassification:
        self._raise("AI")

    async def suggest_answer(self, question: dict, context: str | None = None) -> dict:
        self._raise("AI")

    async def grade_open_answer(self, question, expected, response, max_score) -> GradeSuggestion:
        self._raise("AI")

    async def enrich_vocabulary(self, word: str, language: str) -> dict:
        self._raise("AI")


class OpenAICompatibleAI:
    """Works with Ollama and any local OpenAI-compatible /v1 chat endpoint.

    Deterministic tasks must NOT call this; only explicitly teacher-enabled AI
    assisted tasks do. Returns structured, review-ready suggestions.
    """

    name = "local"

    def __init__(self, base_url: str, model: str, api_key: str = "ollama") -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key

    def enabled(self) -> bool:
        return bool(self._base)

    async def _chat(self, system: str, user: str) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self._base}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def classify_question(self, text: str, image_hint: str | None = None) -> AIClassification:
        import json

        content = await self._chat(
            "You classify language-learning questions. Reply JSON: "
            '{"question_type, level(CEFR), topics[], confidence}',
            text,
        )
        data = json.loads(content)
        return AIClassification(
            question_type=data.get("question_type"),
            level=data.get("level"),
            topics=data.get("topics", []),
            confidence=data.get("confidence"),
            raw=data,
        )

    async def suggest_answer(self, question: dict, context: str | None = None) -> dict:
        import json

        content = await self._chat(
            "Propose the most likely correct answer for this language question. "
            "Reply JSON: {accepted_answers[], confidence, reasoning}",
            str({"question": question, "context": context}),
        )
        return json.loads(content)

    async def grade_open_answer(self, question, expected, response, max_score) -> GradeSuggestion:
        import json

        content = await self._chat(
            "Grade a language-learning free-text answer. Reply JSON: "
            "{verdict(correct|partially_correct|incorrect), suggested_score, confidence, reason}. "
            "Be conservative; when unsure prefer partially_correct.",
            str({"question": question, "expected": expected, "student_answer": response, "max_score": max_score}),
        )
        data = json.loads(content)
        return GradeSuggestion(
            verdict=data.get("verdict", "incorrect"),
            suggested_score=float(data.get("suggested_score", 0) or 0),
            max_score=max_score,
            confidence=float(data.get("confidence", 0) or 0),
            reason=data.get("reason", ""),
        )

    async def enrich_vocabulary(self, word: str, language: str) -> dict:
        import json

        content = await self._chat(
            "Return vocabulary info as JSON: {definition, ipa, part_of_speech, synonyms[], examples[]}",
            f"{language}: {word}",
        )
        return json.loads(content)


class GeminiAI(OpenAICompatibleAI):
    """Optional paid provider; only used when the teacher supplies a key.

    Uses the OpenAI-compatible endpoint Google exposes for Gemini."""

    name = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        super().__init__(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            model=model,
            api_key=api_key,
        )

    def enabled(self) -> bool:
        return bool(self._api_key)
