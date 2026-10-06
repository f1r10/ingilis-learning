"""Provider registry: resolve active adapters from configuration/settings.

The teacher chooses the provider in Admin Settings. Defaults to disabled/null so
the platform is fully usable without any AI/OCR service."""
from __future__ import annotations

from app.adapters.base import (
    AIProvider,
    DictionaryProvider,
    DocumentParser,
    OCRProvider,
    SpeechToTextProvider,
    TranslationProvider,
)
from app.adapters.implementations import (
    GeminiAI,
    NullAI,
    NullDictionary,
    NullDocumentParser,
    NullOCR,
    NullSTT,
    NullTranslation,
    OpenAICompatibleAI,
)
from app.core.config import get_settings


def get_ai_provider() -> AIProvider:
    s = get_settings()
    if s.ai_provider == "local" and s.local_ai_base_url:
        return OpenAICompatibleAI(s.local_ai_base_url, s.local_ai_model)  # type: ignore[arg-type]
    if s.ai_provider == "gemini" and s.gemini_api_key:
        return GeminiAI(s.gemini_api_key, s.gemini_model)
    return NullAI()


def get_ocr_provider() -> OCRProvider:
    s = get_settings()
    if s.ocr_provider in ("paddleocr", "tesseract"):
        # Backend service should be integrated here (PaddleOCR/Tesseract adapter).
        # Until a service endpoint is wired, behave as disabled rather than fake.
        return NullOCR()
    return NullOCR()


def get_document_parser() -> DocumentParser:
    # Docling-compatible adapter plugs in here when enabled in settings.
    return NullDocumentParser()


def get_stt_provider() -> SpeechToTextProvider:
    # Whisper-compatible adapter plugs in here when enabled in settings.
    return NullSTT()


def get_translation_provider() -> TranslationProvider:
    s = get_settings()
    if s.translation_provider == "libretranslate" and s.translation_base_url:
        return NullTranslation()  # plug LibreTranslate client here
    return NullTranslation()


def get_dictionary_provider() -> DictionaryProvider:
    # Kaikki/Wiktextract data adapter plugs in here when enabled.
    return NullDictionary()
