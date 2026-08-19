from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from statistics import fmean, median

import numpy as np
from partitura.musicanalysis import estimate_voices

from .models import CANONICAL_DIVISIONS, Hand, QuantizedNote, Staff


@dataclass(slots=True)
class _Event:
    notes: list[QuantizedNote]
    onset: int
    duration: int
    center: float
    preferred_voice: int
    hand: Hand | None
    voice: int = 1

    @property
    def end(self) -> int:
        return self.onset + self.duration


@dataclass(slots=True)
class _VoiceState:
    end: int
    center: float
    preferred_voice: int
    hand: Hand | None


def assign_voices(
    notes: list[QuantizedNote], max_voices: int
) -> tuple[list[QuantizedNote], dict[str, int], list[str]]:
    """Separate voices without ever shortening or discarding a note.

    Partitura's Chew-Wu implementation supplies melodic-path hints. A final
    interval packing pass merges non-overlapping paths where possible and opens
    an extra notated voice when the music genuinely requires it. The configured
    maximum is therefore a readability target, not a destructive hard cap.
    """

    warnings: list[str] = []
    result: list[QuantizedNote] = []
    voice_counts: dict[str, int] = {}

    for staff in (Staff.RIGHT, Staff.LEFT):
        staff_notes = sorted(
            (note for note in notes if note.staff == staff),
            key=lambda note: (note.onset, -note.duration, -note.pitch, note.source_id),
        )
        events, event_warnings = _make_events(staff_notes)
        warnings.extend(event_warnings)
        states: list[_VoiceState] = []

        for event in events:
            available = [index for index, state in enumerate(states) if event.onset >= state.end]
            same_hand = [
                index
                for index in available
                if event.hand is None
                or states[index].hand is None
                or states[index].hand == event.hand
            ]
            reusable = same_hand or available
            if reusable:
                voice_index = min(
                    reusable,
                    key=lambda index: _packing_cost(states[index], event),
                )
                state = states[voice_index]
                state.end = event.end
                state.center = event.center
                state.preferred_voice = event.preferred_voice
                if event.hand is not None:
                    state.hand = event.hand
            else:
                voice_index = len(states)
                states.append(
                    _VoiceState(
                        end=event.end,
                        center=event.center,
                        preferred_voice=event.preferred_voice,
                        hand=event.hand,
                    )
                )
            event.voice = voice_index + 1

        _renumber_by_register(events)
        required = max((event.voice for event in events), default=1)
        voice_counts[staff.name.lower()] = required
        if required > max_voices:
            warnings.append(
                f"{_staff_label(staff)}实际需要 {required} 个独立声部；为避免截短或丢音已完整保留"
            )
        for event in events:
            result.extend(note.with_voice(event.voice) for note in event.notes)

    return (
        sorted(
            result,
            key=lambda note: (note.onset, int(note.staff or 1), note.voice, note.pitch),
        ),
        voice_counts,
        list(dict.fromkeys(warnings)),
    )


def _make_events(notes: list[QuantizedNote]) -> tuple[list[_Event], list[str]]:
    if not notes:
        return [], []

    preferred, warnings = _partitura_voice_hints(notes)
    grouped: dict[
        tuple[int, int, Hand | None],
        list[tuple[QuantizedNote, int]],
    ] = defaultdict(list)
    for note, preferred_voice in zip(notes, preferred, strict=True):
        grouped[(note.onset, note.duration, note.hand)].append((note, preferred_voice))

    events: list[_Event] = []
    for (onset, duration, hand), group in grouped.items():
        group_notes = [note for note, _ in group]
        preferred_counts = Counter(preferred_voice for _, preferred_voice in group)
        preferred_voice = min(
            preferred_counts,
            key=lambda voice: (-preferred_counts[voice], voice),
        )
        # Equal-onset, equal-duration notes played by the same hand are one
        # notated chord even when Chew-Wu assigns their melodic continuations to
        # different paths.  Keeping those hints as separate events creates extra
        # voices, padding rests, and avoidable horizontal collisions.
        events.append(
            _Event(
                notes=group_notes,
                onset=onset,
                duration=duration,
                center=fmean(note.pitch for note in group_notes),
                preferred_voice=preferred_voice,
                hand=hand,
            )
        )
    return (
        sorted(
            events,
            key=lambda event: (
                event.onset,
                event.preferred_voice,
                -event.duration,
                -event.center,
            ),
        ),
        warnings,
    )


def _partitura_voice_hints(notes: list[QuantizedNote]) -> tuple[list[int], list[str]]:
    note_array = np.zeros(
        len(notes),
        dtype=[("pitch", "i4"), ("onset_beat", "f8"), ("duration_beat", "f8")],
    )
    note_array["pitch"] = [note.pitch for note in notes]
    note_array["onset_beat"] = [note.onset / CANONICAL_DIVISIONS for note in notes]
    note_array["duration_beat"] = [note.duration / CANONICAL_DIVISIONS for note in notes]
    try:
        voices = estimate_voices(note_array, monophonic_voices=False)
        return [int(voice) for voice in voices], []
    except (TypeError, ValueError, ArithmeticError) as exc:
        return [1] * len(notes), [f"Chew–Wu 声部分离未能完成，已使用安全区间分配：{exc}"]


def _packing_cost(state: _VoiceState, event: _Event) -> float:
    path_penalty = 0.0 if state.preferred_voice == event.preferred_voice else 0.9
    continuity = abs(state.center - event.center) * 0.055
    hand_penalty = 0.0
    if state.hand is not None and event.hand is not None and state.hand != event.hand:
        hand_penalty = 12.0
    return path_penalty + continuity + hand_penalty


def _renumber_by_register(events: list[_Event]) -> None:
    centers: dict[int, list[float]] = defaultdict(list)
    for event in events:
        centers[event.voice].append(event.center)
    order = sorted(centers, key=lambda voice: median(centers[voice]), reverse=True)
    mapping = {old_voice: index + 1 for index, old_voice in enumerate(order)}
    for event in events:
        event.voice = mapping[event.voice]


def _staff_label(staff: Staff) -> str:
    return "右手谱表" if staff == Staff.RIGHT else "左手谱表"


def resolve_voice_overlaps(
    notes: list[QuantizedNote],
) -> tuple[list[QuantizedNote], int, int]:
    """Guarantee at most one event stream per (staff, voice).

    Audio transcriptions and grace-note rewrites can leave same-voice notes
    overlapping: attacks are the reliable half of a transcription, offsets the
    noisy one.  Same-attack groups collapse to one notated chord (uniform
    duration); strict overlaps clip the earlier note's tail; stubs shorter
    than a 64th note cannot be printed and are dropped, counted for warnings.
    """

    grouped: dict[tuple[object, int], list[QuantizedNote]] = defaultdict(list)
    passthrough: list[QuantizedNote] = []
    for note in notes:
        if note.staff is None or note.grace:
            passthrough.append(note)
        else:
            grouped[(note.staff, note.voice)].append(note)

    clipped = 0
    dropped = 0
    result = list(passthrough)
    minimum = CANONICAL_DIVISIONS // 16
    for group in grouped.values():
        ordered = sorted(group, key=lambda note: (note.onset, note.pitch, note.source_id))
        # Micro-staggered arrivals (transcription noise below a 64th) belong to
        # the same attack: snap them onto it instead of dropping noteheads.
        snapped: list[QuantizedNote] = []
        for note in ordered:
            if snapped and 0 < note.onset - snapped[-1].onset < minimum:
                note = replace(note, onset=snapped[-1].onset)
                clipped += 1
            snapped.append(note)
        uniform: list[QuantizedNote] = []
        index = 0
        while index < len(snapped):
            stop = index + 1
            while stop < len(snapped) and snapped[stop].onset == snapped[index].onset:
                stop += 1
            same = list(snapped[index:stop])
            durations = {note.duration for note in same}
            if len(durations) > 1:
                longest = max(durations)
                same = [replace(note, duration=longest) for note in same]
                clipped += len(same) - 1
            uniform.extend(same)
            index = stop

        events: list[list[QuantizedNote]] = []
        for note in uniform:
            if events and note.onset == events[-1][0].onset:
                events[-1].append(note)
            else:
                events.append([note])
        cleaned: list[list[QuantizedNote]] = []
        for event in events:
            while cleaned and event[0].onset < max(n.end for n in cleaned[-1]):
                previous = cleaned[-1]
                shortened = event[0].onset - previous[0].onset
                if shortened >= minimum:
                    cleaned[-1] = [replace(n, duration=shortened) for n in previous]
                    clipped += len(previous)
                    break
                cleaned.pop()
                dropped += len(previous)
            cleaned.append(event)
        # A free-standing note shorter than a 64th has no printable value:
        # lengthen it onto the grid when the lane has room, drop it otherwise.
        final: list[list[QuantizedNote]] = []
        for index, event in enumerate(cleaned):
            if all(note.duration < minimum for note in event):
                next_onset = cleaned[index + 1][0].onset if index + 1 < len(cleaned) else None
                room = (next_onset if next_onset is not None else event[0].onset + minimum) - event[0].onset
                if room >= minimum:
                    event = [replace(note, duration=minimum) for note in event]
                    clipped += len(event)
                else:
                    dropped += len(event)
                    continue
            final.append(event)
        for event in final:
            result.extend(event)

    return (
        sorted(result, key=lambda note: (note.onset, note.pitch, note.source_id)),
        clipped,
        dropped,
    )
