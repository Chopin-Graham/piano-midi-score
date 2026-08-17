from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .models import CANONICAL_DIVISIONS, QuantizedNote, Staff


@dataclass(frozen=True, slots=True)
class OttavaSpan:
    staff: Staff
    start: int
    end: int
    direction: str
    size: int


@dataclass(frozen=True, slots=True)
class _PitchEvent:
    onset: int
    end: int
    pitches: tuple[int, ...]


def detect_ottava_spans(notes: list[QuantizedNote]) -> list[OttavaSpan]:
    """Find phrase-level 8va/8vb (and rare 15ma/15mb) candidates.

    A span is accepted only when every sounding note on the staff during the
    interval belongs to the same extreme register. This prevents an ottava line
    from fixing one high voice while pushing a simultaneous inner voice into a
    new field of ledger lines.
    """

    spans: list[OttavaSpan] = []
    for staff in (Staff.RIGHT, Staff.LEFT):
        staff_notes = sorted(
            (note for note in notes if note.staff == staff),
            key=lambda note: (note.onset, note.end, note.pitch),
        )
        events = _pitch_events(staff_notes)
        spans.extend(_candidate_runs(staff, staff_notes, events, direction="high"))
        spans.extend(_candidate_runs(staff, staff_notes, events, direction="low"))
    ordered = sorted(spans, key=lambda span: (span.start, int(span.staff), span.direction))
    return _without_repeated_short_fragments(ordered)


def _without_repeated_short_fragments(spans: list[OttavaSpan]) -> list[OttavaSpan]:
    """Prefer ledger lines over a row of tiny repeated ottava brackets.

    A single short extreme gesture can benefit from an ottava sign.  Three or
    more one-and-a-half-beat-or-shorter gestures repeated in adjacent measures
    produce more labels than information, however.  Published piano scores
    normally keep those isolated peaks on ledger lines while retaining ottavas
    for the longer phrase that follows.
    """

    maximum_duration = CANONICAL_DIVISIONS * 3 // 2
    maximum_start_gap = CANONICAL_DIVISIONS * 4
    short_indices = {
        index
        for index, span in enumerate(spans)
        if span.end - span.start <= maximum_duration
    }
    suppressed: set[int] = set()
    for index in sorted(short_indices):
        if index in suppressed:
            continue
        seed = spans[index]
        cluster = [index]
        previous = seed
        for following_index in range(index + 1, len(spans)):
            following = spans[following_index]
            if (
                following.staff != seed.staff
                or following.direction != seed.direction
                or following.size != seed.size
            ):
                continue
            if following.start - previous.start > maximum_start_gap:
                break
            if following_index not in short_indices:
                break
            cluster.append(following_index)
            previous = following
        if len(cluster) >= 3:
            suppressed.update(cluster)
    return [span for index, span in enumerate(spans) if index not in suppressed]


def _pitch_events(notes: list[QuantizedNote]) -> list[_PitchEvent]:
    grouped: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        grouped[note.onset].append(note)
    return [
        _PitchEvent(
            onset=onset,
            end=max(note.end for note in event_notes),
            pitches=tuple(sorted(note.pitch for note in event_notes)),
        )
        for onset, event_notes in sorted(grouped.items())
    ]


def _candidate_runs(
    staff: Staff,
    notes: list[QuantizedNote],
    events: list[_PitchEvent],
    *,
    direction: str,
) -> list[OttavaSpan]:
    candidate = _is_high_event if direction == "high" else _is_low_event
    bridge = _is_high_band_event if direction == "high" else _is_low_band_event
    runs: list[list[_PitchEvent]] = []
    current: list[_PitchEvent] = []
    leading_bridge: list[_PitchEvent] = []
    last_core_index = 0
    for event in events:
        close_enough = not current or event.onset - current[-1].end <= CANONICAL_DIVISIONS
        if current and bridge(event) and close_enough:
            current.append(event)
            if candidate(event):
                last_core_index = len(current)
            continue
        if current:
            runs.append(current[:last_core_index])
            current = []
            last_core_index = 0

        if candidate(event):
            # Professional editions normally place the ottava sign at the
            # beginning of the high/low-register gesture, not directly on the
            # first peak.  Retain up to one beat of safe same-register lead-in
            # notes so MuseScore has horizontal room for the label and the
            # player reads one coherent phrase.  _qualifies() still examines
            # every sounding note, so an overlapping inner voice prevents an
            # unsafe extension.
            leading_bridge = [
                item
                for item in leading_bridge
                if event.onset - item.onset <= CANONICAL_DIVISIONS
            ]
            current = [*leading_bridge, event]
            leading_bridge = []
            last_core_index = len(current)
        elif bridge(event):
            if (
                leading_bridge
                and event.onset - leading_bridge[-1].end > CANONICAL_DIVISIONS
            ):
                leading_bridge = []
            leading_bridge.append(event)
            leading_bridge = [
                item
                for item in leading_bridge
                if event.onset - item.onset <= CANONICAL_DIVISIONS
            ]
        else:
            leading_bridge = []
    if current:
        runs.append(current[:last_core_index])

    spans: list[OttavaSpan] = []
    for run in runs:
        start = run[0].onset
        end = max(event.end for event in run)
        sounding = [note for note in notes if note.onset < end and note.end > start]
        first_core = next(index for index, event in enumerate(run) if candidate(event))
        # The lead-in may reposition a span that already qualifies, but must
        # not manufacture a new ottava merely by inflating its note count.
        if not _qualifies(run[first_core:], sounding, direction):
            continue
        pitches = [note.pitch for note in sounding]
        size = _ottava_size(pitches, direction)
        spans.append(
            OttavaSpan(
                staff=staff,
                start=start,
                end=end,
                direction="down" if direction == "high" else "up",
                size=size,
            )
        )
    return spans


def _is_high_event(event: _PitchEvent) -> bool:
    return min(event.pitches) >= 74 and max(event.pitches) >= 84


def _is_high_band_event(event: _PitchEvent) -> bool:
    """A safe written-treble bridge inside an otherwise genuine 8va phrase."""

    return min(event.pitches) >= 74


def _is_low_event(event: _PitchEvent) -> bool:
    return max(event.pitches) <= 52 and min(event.pitches) <= 36


def _is_low_band_event(event: _PitchEvent) -> bool:
    """A safe written-bass bridge inside an otherwise genuine 8vb phrase."""

    return max(event.pitches) <= 52


def _qualifies(
    run: list[_PitchEvent],
    sounding: list[QuantizedNote],
    direction: str,
) -> bool:
    if not sounding:
        return False
    noteheads = sum(len(event.pitches) for event in run)
    duration = max(event.end for event in run) - run[0].onset
    pitches = [note.pitch for note in sounding]

    # Short ottavas need enough horizontal room for a readable label, dashed
    # line, and hook.  MuseScore collapses an eighth-note ottava to a bare
    # ``8va`` label, which is too easy to misread in a mixed-register chord.
    # Limit one-event spans to at least a quarter note at a genuinely extreme
    # register; shorter ornaments are clearer and semantically safer with
    # ledger lines. Phrase-level runs continue through the rule below.
    if len(run) == 1:
        short_span = CANONICAL_DIVISIONS <= duration <= CANONICAL_DIVISIONS * 2
        compact_event = len(pitches) <= 2
        if not (short_span and compact_event):
            return False
        if direction == "high":
            return min(pitches) >= 80 and max(pitches) >= 92
        return max(pitches) <= 36 and min(pitches) <= 24

    if noteheads < 6 and not (len(run) >= 3 and duration >= CANONICAL_DIVISIONS * 2):
        return False
    if direction == "high":
        return min(pitches) >= 74 and max(pitches) >= 84
    return max(pitches) <= 52 and min(pitches) <= 36


def _ottava_size(pitches: list[int], direction: str) -> int:
    if direction == "high" and min(pitches) >= 96 and max(pitches) >= 104:
        return 15
    if direction == "low" and max(pitches) <= 32 and min(pitches) <= 24:
        return 15
    return 8
