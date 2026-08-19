"""Ornament detection for audio transcriptions.

Transcription models render trills as dozens of measured thirty-second notes.
Engravers write one sustained note with a trill mark instead.  The alternation
members merge into a single written note whose duration is the exact span of
the run, so voice-time accounting is preserved to the tick.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from statistics import median

from .models import CANONICAL_DIVISIONS, QuantizedNote, Staff

MIN_TRILL_ATTACKS = 6
MIN_TRILL_SPAN = (CANONICAL_DIVISIONS * 3) // 4  # about a dotted quarter of alternation
# A performed trill alternates at sixteenth-note speed or faster; an
# eighth-speed two-pitch figure is a measured ornament, not a trill, and
# writing "tr" over it puts a mark no editor expects.
MAX_TRILL_MEMBER = CANONICAL_DIVISIONS // 4  # members are sixteenth-note or faster

MIN_TREMOLO_ATTACKS = 8
MAX_TREMOLO_SPACING = CANONICAL_DIVISIONS // 8  # 32nd-note speed or faster
MIN_TREMOLO_SPAN = CANONICAL_DIVISIONS  # at least one beat of alternation
MIN_TREMOLO_INTERVAL = 3  # semitones; stepwise alternation is trill territory

# Durations with a single clean notehead/rest, mirroring musicxml.DURATION_SPECS.
_CLEAN_DURATIONS = frozenset(
    {
        1920,
        1440,
        960,
        720,
        480,
        360,
        320,
        240,
        180,
        160,
        120,
        90,
        80,
        60,
        40,
        30,
    }
)
MAX_GRACE_DURATION = CANONICAL_DIVISIONS // 4  # a sixteenth note


def convert_grace_notes(
    notes: list[QuantizedNote],
) -> tuple[list[QuantizedNote], int]:
    """Rewrite isolated crushed notes before a beat as slashed grace notes.

    Nakamura's performance-model measurements show that grace-note timing
    overlaps ordinary short-note timing, so conversion requires converging
    evidence: a single-cell note immediately before a longer, louder on-beat
    note a step or third away, alone at its onset in its voice, off the beat
    itself.  The grace's time is returned to the previous written note when
    that restores a clean value (the typical case: the voice simplifier had
    truncated that note to make room), otherwise a rest absorbs it.
    """

    by_voice: dict[tuple[Staff, int], list[QuantizedNote]] = defaultdict(list)
    passthrough: list[QuantizedNote] = []
    for note in notes:
        if note.staff is None or note.grace:
            passthrough.append(note)
        else:
            by_voice[(note.staff, note.voice)].append(note)

    converted = 0
    result = list(passthrough)
    for voice_notes in by_voice.values():
        ordered = sorted(voice_notes, key=lambda note: (note.onset, note.pitch))
        converted_ids: set[int] = set()
        extensions: dict[int, int] = {}
        for index, note in enumerate(ordered[:-1]):
            if note.source_id in converted_ids or note.trill or note.arpeggiated:
                continue
            if note.duration > MAX_GRACE_DURATION or note.onset % CANONICAL_DIVISIONS == 0:
                continue
            main = ordered[index + 1]
            if (
                main.onset != note.end
                or main.onset % CANONICAL_DIVISIONS != 0
                or main.trill
                or main.duration < note.duration * 2
                or note.velocity >= main.velocity
            ):
                continue
            interval = abs(note.pitch - main.pitch)
            if not 1 <= interval <= 3:
                continue
            if index and ordered[index - 1].onset == note.onset:
                continue  # chord member, not a lone grace
            previous = ordered[index - 1] if index else None
            if previous is not None and previous.end > note.onset:
                continue
            if previous is not None and previous.end == note.onset:
                restored = previous.duration + note.duration
                if restored not in _CLEAN_DURATIONS:
                    continue
                extensions[previous.source_id] = restored
            converted_ids.add(note.source_id)
        for note in ordered:
            if note.source_id in converted_ids:
                result.append(replace(note, grace=True))
                converted += 1
            elif note.source_id in extensions:
                result.append(replace(note, duration=extensions[note.source_id]))
            else:
                result.append(note)

    return (
        sorted(result, key=lambda note: (note.onset, note.pitch, note.source_id)),
        converted,
    )


def collapse_trills(
    notes: list[QuantizedNote],
) -> tuple[list[QuantizedNote], int, int]:
    """Merge measured two-pitch alternations into single trill-marked notes.

    Returns the rewritten notes, the number of trills found, and how many
    attack notes they absorbed.  Only the staff edge line is scanned (the top
    edge for the treble staff, the bottom edge for the bass staff), where
    performed trills actually live; inner-voice figuration stays untouched.
    """

    collapsed: list[QuantizedNote] = [
        note for note in notes if note.staff not in (Staff.RIGHT, Staff.LEFT)
    ]
    trill_count = 0
    absorbed = 0
    for staff in (Staff.RIGHT, Staff.LEFT):
        staff_notes = [note for note in notes if note.staff == staff]
        kept, found, merged = _collapse_staff_trills(staff_notes, staff)
        collapsed.extend(kept)
        trill_count += found
        absorbed += merged
    return (
        sorted(collapsed, key=lambda note: (note.onset, note.pitch, note.source_id)),
        trill_count,
        absorbed,
    )


def _collapse_staff_trills(
    notes: list[QuantizedNote],
    staff: Staff,
) -> tuple[list[QuantizedNote], int, int]:
    columns: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        columns[note.onset].append(note)

    edge_events: list[QuantizedNote] = []
    for onset in sorted(columns):
        column = columns[onset]
        edge = (
            max(column, key=lambda note: note.pitch)
            if staff == Staff.RIGHT
            else min(column, key=lambda note: note.pitch)
        )
        edge_events.append(edge)

    absorbed_ids: set[int] = set()
    trill_notes: list[QuantizedNote] = []
    trill_count = 0
    absorbed = 0
    index = 0
    while index < len(edge_events):
        run = _trill_run(edge_events, index)
        if run is None:
            index += 1
            continue
        members, stop_index = run
        first = members[0]
        last = members[-1]
        trill_notes.append(
            replace(
                first,
                duration=last.end - first.onset,
                velocity=max(note.velocity for note in members),
                trill=True,
            )
        )
        absorbed_ids.update(note.source_id for note in members)
        trill_count += 1
        absorbed += len(members) - 1
        index = stop_index

    if not absorbed_ids:
        return notes, 0, 0
    kept = [note for note in notes if note.source_id not in absorbed_ids]
    kept.extend(trill_notes)
    return kept, trill_count, absorbed


def _trill_run(
    edge_events: list[QuantizedNote],
    start: int,
) -> tuple[list[QuantizedNote], int] | None:
    first = edge_events[start]
    if start + 1 >= len(edge_events):
        return None
    second = edge_events[start + 1]
    interval = abs(first.pitch - second.pitch)
    if interval not in (1, 2):
        return None

    members = [first, second]
    spacings = [second.onset - first.onset]
    index = start + 2
    while index < len(edge_events):
        candidate = edge_events[index]
        if candidate.pitch != members[-2].pitch:
            break
        spacing = candidate.onset - members[-1].onset
        if not 0 < spacing <= MAX_TRILL_MEMBER:
            break
        members.append(candidate)
        spacings.append(spacing)
        index += 1

    if len(members) < MIN_TRILL_ATTACKS:
        return None
    if members[-1].end - members[0].onset < MIN_TRILL_SPAN:
        return None
    typical = median(spacings)
    if any(spacing > typical * 1.5 or spacing < typical * 0.5 for spacing in spacings):
        return None
    if any(note.duration > MAX_TRILL_MEMBER for note in members):
        return None
    return members, index


def collapse_tremolos(
    notes: list[QuantizedNote],
    free_regions: list[tuple[int, int]],
    measures: list,
    tempo_event_ticks: list[int],
    dense_measures: set[int],
) -> tuple[list[QuantizedNote], int, int]:
    """Rewrite rapid wide-interval alternations as two-note measured tremolos.

    In cadenza-like free regions (rit./accel. ramps), virtuoso writing often
    alternates two pitches an octave apart at 32nd-note speed — engravers
    print a tremolo, not pages of beamed notes.  Steady-tempo measured
    alternation stays written out, so detection is gated on *free_regions*
    (tempo-ramp spans).  Each run becomes two plain long notes carrying the
    tremolo marks; the pair's total time equals the original run's span.
    """

    if not free_regions:
        return notes, 0, 0

    collapsed: list[QuantizedNote] = []
    tremolo_count = 0
    absorbed_total = 0
    next_source_id = max((note.source_id for note in notes), default=0) + 1
    for staff in (Staff.RIGHT, Staff.LEFT):
        staff_notes = [note for note in notes if note.staff == staff]
        others = [note for note in notes if note.staff != staff]
        if not others and not staff_notes:
            continue
        columns: dict[int, list[QuantizedNote]] = defaultdict(list)
        for note in staff_notes:
            columns[note.onset].append(note)
        edge_events = []
        for onset in sorted(columns):
            column = columns[onset]
            edge = (
                max(column, key=lambda note: note.pitch)
                if staff == Staff.RIGHT
                else min(column, key=lambda note: note.pitch)
            )
            edge_events.append(edge)

        absorbed_ids: set[int] = set()
        pair_notes: list[QuantizedNote] = []
        index = 0
        while index < len(edge_events):
            run = _tremolo_run(
                edge_events, index, free_regions, measures, tempo_event_ticks, dense_measures
            )
            if run is None:
                index += 1
                continue
            members, stop_index = run
            first = members[0]
            last = members[-1]
            span = last.end - first.onset
            upper = max(first.pitch, members[1].pitch)
            lower = min(first.pitch, members[1].pitch)
            half = max(
                (value for value in _CLEAN_DURATIONS if value <= span / 2),
                default=span // 2,
            )
            pair_notes.append(
                replace(
                    first,
                    duration=half,
                    velocity=max(note.velocity for note in members),
                    tremolo_start=True,
                )
            )
            pair_notes.append(
                replace(
                    first,
                    source_id=next_source_id,
                    pitch=upper if first.pitch == lower else lower,
                    onset=first.onset + half,
                    duration=span - half,
                    velocity=max(note.velocity for note in members),
                    tremolo_start=False,
                    tremolo_stop=True,
                )
            )
            next_source_id += 1
            absorbed_ids.update(note.source_id for note in members)
            tremolo_count += 1
            absorbed_total += len(members) - 2
            index = stop_index

        kept = [note for note in staff_notes if note.source_id not in absorbed_ids]
        kept.extend(pair_notes)
        collapsed.extend(kept)
        collapsed.extend(others)
        notes = collapsed
        collapsed = []
    if tremolo_count == 0:
        return notes, 0, 0
    return (
        sorted(notes, key=lambda note: (note.onset, note.pitch, note.source_id)),
        tremolo_count,
        absorbed_total,
    )


def _tremolo_run(
    edge_events: list[QuantizedNote],
    start: int,
    free_regions: list[tuple[int, int]],
    measures: list,
    tempo_event_ticks: list[int],
    dense_measures: set[int],
) -> tuple[list[QuantizedNote], int] | None:
    first = edge_events[start]
    if start + 1 >= len(edge_events):
        return None
    second = edge_events[start + 1]
    if abs(first.pitch - second.pitch) < MIN_TREMOLO_INTERVAL:
        return None

    members = [first, second]
    spacings = [second.onset - first.onset]
    index = start + 2
    while index < len(edge_events):
        candidate = edge_events[index]
        if candidate.pitch != members[-2].pitch:
            break
        spacing = candidate.onset - members[-1].onset
        if not 0 < spacing <= MAX_TREMOLO_SPACING:
            break
        if candidate.duration > MAX_TREMOLO_SPACING * 2:
            # A held note ends the alternating figure instead of joining it.
            break
        members.append(candidate)
        spacings.append(spacing)
        index += 1

    if len(members) < MIN_TREMOLO_ATTACKS:
        return None
    span = members[-1].end - members[0].onset
    if span < MIN_TREMOLO_SPAN:
        return None
    typical = median(spacings)
    if any(spacing > typical * 1.5 or spacing < typical * 0.5 for spacing in spacings):
        return None
    onset = members[0].onset
    if not any(start <= onset < end for start, end in free_regions):
        return None
    # The pair must live inside one measure: a tremolo start/stop split across
    # a barline arrives unpaired for importers.
    measure_index = None
    measure_of = None
    for index_of, measure in enumerate(measures):
        if measure.start <= onset < measure.end:
            measure_index = index_of
            measure_of = measure
            break
    if measure_of is None or members[-1].end > measure_of.end:
        return None
    # The engraving convention only pays off in cadenza-density writing: the
    # measure must have earned the finest grid, and tempo events must be
    # actively passing through the run.  Steady measured alternation is real
    # beamed notation and stays written out.
    if measure_index not in dense_measures:
        return None
    events_inside = sum(1 for tick in tempo_event_ticks if onset <= tick < members[-1].end)
    if events_inside < 2:
        return None
    return members, index
