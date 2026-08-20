from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ConversionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    style: Literal["clean", "balanced", "faithful"] = "clean"
    engraving_style: Literal["classic", "modern", "compact"] = "classic"
    minimum_note: Literal["eighth", "sixteenth", "thirty_second", "auto"] = "auto"
    allow_triplets: bool = True
    hand_split: int | Literal["auto"] = "auto"
    prefer_track_hints: bool = True
    max_voices_per_staff: int = Field(default=2, ge=1, le=2)
    include_pedal: bool = True
    include_dynamics: bool = True
    infer_key: bool = True
    time_numerator: int | None = Field(default=None, ge=1, le=12)
    time_denominator: Literal[2, 4, 8, 16] | None = None
    title: str | None = Field(default=None, max_length=120)
    author: str | None = Field(default=None, max_length=120)
    output_filename: str | None = Field(default=None, max_length=120)
    audio_transcription: bool = False

    @field_validator("title", "author", "output_filename", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("hand_split")
    @classmethod
    def validate_hand_split(cls, value: int | str) -> int | str:
        if isinstance(value, int) and not 21 <= value <= 108:
            raise ValueError("hand_split must be a MIDI pitch from 21 to 108")
        return value

    @model_validator(mode="after")
    def validate_meter_pair(self):
        if (self.time_numerator is None) != (self.time_denominator is None):
            raise ValueError("time_numerator and time_denominator must be set together")
        return self


class TranscriptionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["auto", "transkun", "basic_pitch"] = "auto"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    align_beats: bool = True
    minimum_note_ms: float = Field(default=55.0, ge=20.0, le=500.0)
    onset_threshold: float = Field(default=0.50, ge=0.05, le=0.95)
    frame_threshold: float = Field(default=0.30, ge=0.05, le=0.95)
