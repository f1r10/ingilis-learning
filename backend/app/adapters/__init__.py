"""Replaceable AI / OCR / STT / translation adapters.

The main platform works WITHOUT any paid API. Providers are selected at runtime
from Admin Settings; every adapter has a disabled/null implementation so business
logic never couples to a specific model (FREE/SELF-HOSTED COMPONENT STRATEGY).
"""
from app.adapters.registry import (
    get_ai_provider,
    get_dictionary_provider,
    get_document_parser,
    get_ocr_provider,
    get_stt_provider,
    get_translation_provider,
)

__all__ = [
    "get_ai_provider",
    "get_document_parser",
    "get_ocr_provider",
    "get_stt_provider",
    "get_translation_provider",
    "get_dictionary_provider",
]
