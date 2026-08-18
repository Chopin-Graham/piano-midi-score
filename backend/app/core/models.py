from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import IntEnum
from typing import Literal

CANONICAL_DIVISIONS = 480


class Staff(IntEnum):
    RIGHT = 1
    TREBLE = 1
    LEFT = 2
    BASS = 2


class Hand(IntEnum):
    RIGHT = 1
    LEFT = 2


@dataclass(frozen=True, slots=True)
class ClefChange:
    measure_index: int
    staff: Staff
    kind: Literal["treble", "bass"]
    offset: int = 0

    @property
    def sign(self) -> str:
        return "G" if self.kind == "treble" else "F"

    @property
    def line(self) -> int:
        return 2 if self.kind == "treble" else 4


@dataclass(frozen=True, slots=True)
class RawNote:
    source_id: int
    pitch: int
    start_tick: int
    end_tick: int
    velocity: int
    track: int
    channel: int

    @property
    def duration_tick(self) -> int:
        return self.end_tick - self.start_tick


@dataclass(frozen=True, slots=True)
class TempoEvent:
    tick: int
    microseconds_per_beat: int

    @property
    def bpm(self) -> float:
        return 60_000_000 / self.microseconds_per_beat


@dataclass(frozen=True, slots=True)
class TimeSignatureEvent:
    tick: int
    numerator: int
    denominator: int


@dataclass(frozen=True, slots=True)
class KeySignatureEvent:
    tick: int
    key: str


@dataclass(frozen=True, slots=True)
class PedalEvent:
    tick: int
    channel: int
    down: bool


@dataclass(slots=True)
class ParsedMidi:
    ticks_per_beat: int
    notes: list[RawNote]
    tempos: list[TempoEvent] = field(default_factory=list)
    time_signatures: list[TimeSignatureEvent] = field(default_factory=list)
    key_signatures: list[KeySignatureEvent] = field(default_factory=list)
    pedals: list[PedalEvent] = field(default_factory=list)
    track_names: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    piano_note_on_count: int = 0
    excluded_non_piano_note_count: int = 0
    excluded_percussion_note_count: int = 0
    programs: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class QuantizedNote:
    source_id: int
    pitch: int
    onset: int
    duration: int
    velocity: int
    track: int
    channel: int
    staff: Staff | None = None
    voice: int = 1
    pitch_step: str | None = None
    pitch_alter: int = 0
    pitch_octave: int | None = None
    hand: Hand | None = None
    arpeggiated: bool = False
    trill: bool = False
    grace: bool = False

    @property
    def end(self) -> int:
        return self.onset + self.duration

    def with_staff(self, staff: Staff) -> QuantizedNote:
        return replace(self, staff=staff)

    def with_hand(self, hand: Hand) -> QuantizedNote:
        return replace(self, hand=hand)

    def with_voice(self, voice: int) -> QuantizedNote:
        return replace(self, voice=voice)

    def with_spelling(self, step: str, alter: int, octave: int) -> QuantizedNote:
        return replace(
            self,
            pitch_step=step,
            pitch_alter=alter,
            pitch_octave=octave,
        )

    def with_arpeggiation(self) -> QuantizedNote:
        return replace(self, arpeggiated=True)


@dataclass(frozen=True, slots=True)
class Meter:
    numerator: int = 4
    denominator: int = 4

    @property
    def beat_length(self) -> int:
        return CANONICAL_DIVISIONS * 4 // self.denominator

    @property
    def measure_length(self) -> int:
        return self.beat_length * self.numerator

    @property
    def is_compound(self) -> bool:
        """Whether the written beat is a dotted unit (6/8, 9/8, 12/8, etc.)."""

        return self.numerator >= 6 and self.numerator % 3 == 0 and self.denominator in {8, 16}

    @property
    def beat_groups(self) -> tuple[int, ...]:
        """Metric beat groups in canonical divisions.

        Compound meters group three denominator units. Common irregular eighth-note
        meters use conventional additive groupings when the MIDI carries no explicit
        beat-group metadata.
        """

        unit = self.beat_length
        if self.is_compound:
            return (unit * 3,) * (self.numerator // 3)
        if self.denominator in {8, 16}:
            additive_units = {
                5: (3, 2),
                7: (2, 2, 3),
                8: (3, 3, 2),
                10: (3, 3, 2, 2),
                11: (3, 3, 3, 2),
            }.get(self.numerator)
            if additive_units is not None:
                return tuple(count * unit for count in additive_units)
        return (unit,) * self.numerator

    @property
    def beat_group_boundaries(self) -> tuple[int, ...]:
        boundaries = [0]
        for group in self.beat_groups:
            boundaries.append(boundaries[-1] + group)
        return tuple(boundaries)


@dataclass(frozen=True, slots=True)
class MeasureSpan:
    index: int
    start: int
    duration: int
    meter: Meter
    implicit: bool = False

    @property
    def end(self) -> int:
        return self.start + self.duration


@dataclass(frozen=True, slots=True)
class KeyEstimate:
    tonic_pc: int
    mode: Literal["major", "minor"]
    fifths: int
    confidence: float


@dataclass(frozen=True, slots=True)
class KeyChange:
    measure_index: int
    key: KeyEstimate


@dataclass(frozen=True, slots=True)
class DynamicMark:
    measure_index: int
    mark: str
    velocity_percent: float


@dataclass(frozen=True, slots=True)
class GridDecision:
    measure_index: int
    name: str
    step: int
    score: float
    triplet: bool = False


@dataclass(slots=True)
class ScoreModel:
    title: str
    notes: list[QuantizedNote]
    meter: Meter
    key: KeyEstimate
    tempo_bpm: float
    pedals: list[PedalEvent]
    grid_decisions: list[GridDecision]
    measure_count: int
    engraving_style: str = "classic"
    measures: list[MeasureSpan] = field(default_factory=list)
    clef_changes: list[ClefChange] = field(default_factory=list)
    key_changes: list[KeyChange] = field(default_factory=list)
    dynamics: list[DynamicMark] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ConversionResult:
    musicxml: str
    analysis: dict[str, object]
    warnings: list[str]
