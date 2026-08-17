from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations

from .models import Hand, PedalEvent

PIANO_LOWEST_MIDI = 21
PIANO_HIGHEST_MIDI = 108

MAX_SIMULTANEOUS_KEYS_PER_HAND = 5
COMFORTABLE_HAND_SPAN_SEMITONES = 12
EXTREME_HAND_SPAN_SEMITONES = 15
MAX_HAND_SPAN_SEMITONES = 16

# A full-size keyboard is roughly 23.5 mm per white-key step. Published hand-span
# research places a conventional tenth near the practical limit for many adults.
# Physical millimetres are retained as an ergonomic warning; the interval-class
# limit below is the deterministic hard gate.
WHITE_KEY_WIDTH_MM = 23.5
COMFORTABLE_HAND_SPAN_MM = 180.0
# Minor and major tenths are possible for some adult pianists, but they are an
# extreme reach rather than a generally comfortable one.  The physical distance
# threshold supplements the semitone threshold for different black/white-key
# endpoint combinations.
EXTREME_HAND_SPAN_MM = 205.0
# Key-centre distance is reported as an ergonomic hint. Interval spelling remains
# the hard gate because black-key geometry makes some major tenths wider than
# others; 230 mm covers all major tenths while the 16-semitone rule rejects 11ths.
MAX_HAND_SPAN_MM = 230.0

# Maximum adjacent-note distances for a medium adult hand, adapted from the
# open-source PianoPlayer finger-transition model.  These limits are not used
# to invent a fingering; they answer the narrower question "does at least one
# monotonic five-finger assignment exist for this chord shape?".  The thumb is
# deliberately allowed more reach than the inner fingers.
FINGER_PAIR_MAX_SPAN_MM = {
    (1, 2): 120.0,
    (1, 3): 140.0,
    (1, 4): 160.0,
    (1, 5): MAX_HAND_SPAN_MM,
    (2, 3): 60.0,
    (2, 4): 70.0,
    (2, 5): 110.0,
    (3, 4): 50.0,
    (3, 5): 80.0,
    (4, 5): 50.0,
}

# The PianoPlayer pair limits describe a medium reference hand and its keyboard
# geometry is approximate.  Up to 10 mm is treated as model tolerance.  A shape
# that exceeds the reference by no more than roughly one white-key width remains
# physically possible for a larger hand, but is reported as demanding.  Beyond
# that margin the automatic score requires redistribution or human review.
NATURAL_FINGER_SHAPE_TOLERANCE_MM = 10.0
MAX_FINGER_SHAPE_EXCESS_MM = 25.0

# Horizontal key-centre positions in white-key widths. Black-key offsets reflect
# their approximate physical centres, which is more realistic than treating every
# semitone as the same distance.
_KEY_X = {
    0: 0.00,
    1: 0.58,
    2: 1.00,
    3: 1.68,
    4: 2.00,
    5: 3.00,
    6: 3.56,
    7: 4.00,
    8: 4.64,
    9: 5.00,
    10: 5.72,
    11: 6.00,
}


class PedalCoverage:
    """Efficiently answer whether one channel's pedal covers an interval.

    A pedal on another MIDI channel cannot release the player's finger, and a
    pedal that lifts before the written key release cannot safely replace that
    held finger.  The intervals are therefore channel-specific and must cover
    the complete interval requested by the caller.
    """

    def __init__(self, events: list[PedalEvent] | tuple[PedalEvent, ...]) -> None:
        intervals: dict[int, list[tuple[int, int | None]]] = {}
        down_since: dict[int, int] = {}
        for event in sorted(events, key=lambda item: (item.tick, item.channel)):
            if event.down:
                down_since.setdefault(event.channel, event.tick)
                continue
            start = down_since.pop(event.channel, None)
            if start is not None:
                intervals.setdefault(event.channel, []).append((start, event.tick))
        for channel, start in down_since.items():
            intervals.setdefault(channel, []).append((start, None))

        self._intervals = {
            channel: tuple(channel_intervals)
            for channel, channel_intervals in intervals.items()
        }
        self._starts = {
            channel: tuple(start for start, _ in channel_intervals)
            for channel, channel_intervals in self._intervals.items()
        }

    def covers(self, channel: int, start: int, end: int) -> bool:
        """Whether pedal is down continuously from ``start`` through ``end``."""

        intervals = self._intervals.get(channel)
        starts = self._starts.get(channel)
        if not intervals or not starts:
            return False
        index = bisect_right(starts, start) - 1
        if index < 0:
            return False
        interval_start, interval_end = intervals[index]
        return interval_start <= start and (interval_end is None or interval_end >= end)


@dataclass(frozen=True, slots=True)
class ChordReach:
    unique_keys: int
    span_semitones: int
    span_mm: float

    @property
    def exceeds_finger_count(self) -> bool:
        return self.unique_keys > MAX_SIMULTANEOUS_KEYS_PER_HAND

    @property
    def exceeds_maximum_span(self) -> bool:
        return self.span_semitones > MAX_HAND_SPAN_SEMITONES

    @property
    def is_stretched(self) -> bool:
        return (
            self.span_semitones > COMFORTABLE_HAND_SPAN_SEMITONES
            or self.span_mm > COMFORTABLE_HAND_SPAN_MM
        ) and not self.exceeds_maximum_span

    @property
    def is_extreme_stretch(self) -> bool:
        """Whether the hand is at a minor/major-tenth level of extension."""

        return (
            self.span_semitones >= EXTREME_HAND_SPAN_SEMITONES
            or self.span_mm >= EXTREME_HAND_SPAN_MM
        ) and not self.exceeds_maximum_span

    @property
    def is_extended_stretch(self) -> bool:
        """Whether the reach is beyond an octave but short of a tenth."""

        return self.is_stretched and not self.is_extreme_stretch

    @property
    def playable(self) -> bool:
        return not self.exceeds_finger_count and not self.exceeds_maximum_span


@lru_cache(maxsize=128)
def key_position_mm(pitch: int) -> float:
    octave, pitch_class = divmod(pitch, 12)
    return (octave * 7 + _KEY_X[pitch_class]) * WHITE_KEY_WIDTH_MM


def chord_reach(pitches: list[int] | set[int] | tuple[int, ...]) -> ChordReach:
    return _chord_reach_cached(tuple(sorted(set(pitches))))


@lru_cache(maxsize=16_384)
def _chord_reach_cached(ordered: tuple[int, ...]) -> ChordReach:
    if not ordered:
        return ChordReach(0, 0, 0.0)
    return ChordReach(
        unique_keys=len(ordered),
        span_semitones=ordered[-1] - ordered[0],
        span_mm=round(key_position_mm(ordered[-1]) - key_position_mm(ordered[0]), 2),
    )


def chord_fingering_excess_mm(
    pitches: list[int] | set[int] | tuple[int, ...],
    hand: Hand,
) -> float:
    """Return the smallest adjacent-finger excess for a chord shape.

    Zero means that at least one monotonic assignment to distinct fingers fits
    the medium-hand transition limits.  A positive value is the smallest total
    excess, in millimetres, among all candidate finger subsets.  Infinite means
    that the chord already violates the five-key or major-tenth hard gate.

    Only adjacent notes in the ordered chord are compared, matching the local
    transition constraints used by PianoPlayer.  Testing every pair would
    incorrectly reject ordinary octave chords such as C-E-G-C.
    """

    ordered = tuple(sorted(set(pitches)))
    return _chord_fingering_excess_mm_cached(ordered, hand)


@lru_cache(maxsize=16_384)
def _chord_fingering_excess_mm_cached(
    ordered: tuple[int, ...],
    hand: Hand,
) -> float:
    reach = chord_reach(ordered)
    if not reach.playable:
        return float("inf")
    if len(ordered) <= 1:
        return 0.0

    best = float("inf")
    for finger_subset in combinations(range(1, 6), len(ordered)):
        fingers = (
            finger_subset
            if hand == Hand.RIGHT
            else tuple(reversed(finger_subset))
        )
        excess = 0.0
        for lower_pitch, upper_pitch, lower_finger, upper_finger in zip(
            ordered,
            ordered[1:],
            fingers,
            fingers[1:],
            strict=False,
        ):
            pair = tuple(sorted((lower_finger, upper_finger)))
            limit = FINGER_PAIR_MAX_SPAN_MM[pair]
            distance = key_position_mm(upper_pitch) - key_position_mm(lower_pitch)
            excess += max(0.0, distance - limit)
        best = min(best, excess)

    return round(best, 2)


def chord_fingering_feasible(
    pitches: list[int] | set[int] | tuple[int, ...],
    hand: Hand,
) -> bool:
    """Whether a chord fits at least one plausible monotonic five-finger shape."""

    return chord_fingering_excess_mm(pitches, hand) <= MAX_FINGER_SHAPE_EXCESS_MM


def chord_fingering_natural(
    pitches: list[int] | set[int] | tuple[int, ...],
    hand: Hand,
) -> bool:
    """Whether a chord fits the medium-hand model within geometry tolerance."""

    return (
        chord_fingering_excess_mm(pitches, hand)
        <= NATURAL_FINGER_SHAPE_TOLERANCE_MM
    )


def pitches_are_within_piano(pitches: list[int] | set[int] | tuple[int, ...]) -> bool:
    return all(PIANO_LOWEST_MIDI <= pitch <= PIANO_HIGHEST_MIDI for pitch in pitches)
