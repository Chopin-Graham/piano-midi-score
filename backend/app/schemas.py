from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .core.options import ConversionOptions, TranscriptionOptions


class HealthResponse(BaseModel):
    status: str
    version: str
    engraver: dict[str, Any]
    transcriber: dict[str, Any]
    omr: dict[str, Any]


class OptionsResponse(BaseModel):
    defaults: ConversionOptions
    transcription_defaults: TranscriptionOptions
    max_upload_bytes: int
    max_media_upload_bytes: int
    max_pdf_upload_bytes: int
    supported_extensions: list[str]
    supported_media_extensions: list[str]
    supported_score_extensions: list[str]


class ConversionResponse(BaseModel):
    filename: str
    musicxml: str
    midi_filename: str | None = None
    midi_base64: str | None = None
    pdf_filename: str | None = None
    pdf_base64: str | None = None
    preview_png_base64: str | None = None
    analysis: dict[str, Any]
    warnings: list[str]
