from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from statistics import fmean

from .meter_map import measure_index_at
from .models import (
    CANONICAL_DIVISIONS,
    GridDecision,
    MeasureSpan,
    Meter,
    ParsedMidi,
    QuantizedNote,
)
from .options import ConversionOptions
from .piano_rules import MAX_HAND_SPAN_SEMITONES, MAX_SIMULTANEOUS_KEYS_PER_HAND


@dataclass(frozen=True, slots=True)
class GridSpec:
    name: str
    step: int
    triplet: bool
    complexity: float


def quantize_midi(
    parsed: ParsedMidi,
    meter_or_measures: Meter | list[MeasureSpan],
    options: ConversionOptions,
    *,
    timeline_shift: int = 0,
) -> tuple[list[QuantizedNote], list[GridDecision], int, list[str]]:
    scale = CANONICAL_DIVISIONS / parsed.ticks_per_beat
    canonical = [
        QuantizedNote(
            source_id=note.source_id,
            pitch=note.pitch,
            onset=round(note.start_tick * scale),
            duration=max(1, round(note.duration_tick * scale)),
            velocity=note.velocity,
            track=note.track,
            channel=note.channel,
        )
        for note in parsed.notes
    ]

    warnings: list[str] = []
    if isinstance(meter_or_measures, Meter):
        meter = meter_or_measures
        first_onset = min(note.onset for note in canonical)
        leading_measures = first_onset // meter.measure_length
        timeline_shift = leading_measures * meter.measure_length
        if timeline_shift:
            warnings.append(f"已移除开头 {leading_measures} 个完整空小节")
        final_end = max(note.end for note in canonical) - timeline_shift
        measure_count = max(1, (final_end + meter.measure_length - 1) // meter.measure_length)
        measures = [
            MeasureSpan(
                index=index,
                start=index * meter.measure_length,
                duration=meter.measure_length,
                meter=meter,
            )
            for index in range(measure_count)
        ]
    else:
        measures = meter_or_measures

    if timeline_shift:
        canonical = [replace(note, onset=note.onset - timeline_shift) for note in canonical]

    canonical, rolled_note_count = _collapse_playable_rolled_chords(
        canonical,
        options.style,
    )
    if rolled_note_count:
        warnings.append(
            f"将 {rolled_note_count} 个释放同步且单手可演奏的微时差音归并为和弦起音（音头全部保留）"
        )

    notes_by_measure: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in canonical:
        notes_by_measure[measure_index_at(measures, note.onset)].append(note)

    # Audio transcriptions default to "triplets off", which crushes genuinely
    # ternary passages onto a binary grid.  Treat that switch as "auto" in
    # transcription mode: a piece earns triplet grids only when a solid share
    # of measures fit a triplet grid far better than any binary one — random
    # model timing noise cannot produce that margin because the finer binary
    # grid fits noise at least as well.
    candidate_options = options
    if options.audio_transcription and not options.allow_triplets:
        probe = options.model_copy(update={"allow_triplets": True})
        votes = 0
        total = 0
        for measure_index, measure in enumerate(measures):
            measure_notes = notes_by_measure.get(measure_index, [])
            if len(measure_notes) < 3:
                continue
            probe_candidates = _grid_candidates(probe, measure.meter)
            binary_error = min(
                (_grid_timing_error(measure_notes, measure.start, grid.step)
                 for grid in probe_candidates if not grid.triplet),
                default=None,
            )
            triplet_error = min(
                (_grid_timing_error(measure_notes, measure.start, grid.step)
                 for grid in probe_candidates if grid.triplet),
                default=None,
            )
            if binary_error is None or triplet_error is None:
                continue
            total += 1
            # Swing feels (long-short pairs at ~2:1) fit a triplet grid
            # perfectly at the 0 and 2/3 positions, but only genuine triplets
            # also populate the middle tatum.  Requiring one middle-tatum
            # onset per voting measure keeps swung eighths out of the vote.
            has_middle_tatum = any(
                abs((note.onset - measure.start) % CANONICAL_DIVISIONS - 160) <= 40
                for note in measure_notes
            )
            if has_middle_tatum and triplet_error < binary_error * 0.6:
                votes += 1
        if total >= 4 and votes / total >= 0.12:
            candidate_options = probe
            warnings.append(
                f"有 {votes}/{total} 个小节在三连音网格上的拟合显著更优，已自动启用三连音识别"
            )

    decisions: list[GridDecision] = []
    quantized: list[QuantizedNote] = []

    for measure_index, measure in enumerate(measures):
        candidates = _grid_candidates(candidate_options, measure.meter)
        measure_notes = notes_by_measure.get(measure_index, [])
        if not measure_notes:
            decisions.append(
                GridDecision(measure_index, candidates[0].name, candidates[0].step, 0.0)
            )
            continue

        measure_start = measure.start
        best = min(
            candidates,
            key=lambda grid: _grid_cost(measure_notes, measure_start, grid, options.style),
        )
        score = _grid_cost(measure_notes, measure_start, best, options.style)
        decisions.append(
            GridDecision(
                measure_index=measure_index,
                name=best.name,
                step=best.step,
                score=round(score, 4),
                triplet=best.triplet,
            )
        )

        for note in measure_notes:
            relative_onset = note.onset - measure_start
            snapped_onset = measure_start + _nearest_multiple(relative_onset, best.step)
            if measure_index == len(measures) - 1 and snapped_onset >= measure.end:
                snapped_onset = max(measure.start, measure.end - best.step)
            relative_end = note.end - measure_start
            snapped_end = measure_start + _nearest_multiple(relative_end, best.step)
            if snapped_end <= snapped_onset:
                snapped_end = snapped_onset + best.step
            quantized.append(
                replace(note, onset=max(0, snapped_onset), duration=snapped_end - snapped_onset)
            )

    before_deduplication = len(quantized)
    quantized = _deduplicate_notes(quantized)
    duplicate_count = before_deduplication - len(quantized)
    if duplicate_count:
        warnings.append(
            f"合并了 {duplicate_count} 个同一时刻、同一琴键的重复 MIDI 事件"
        )
    quantized = _resolve_repeated_pitch_overlaps(quantized)
    return quantized, decisions, timeline_shift, warnings


def _grid_candidates(options: ConversionOptions, meter: Meter | None = None) -> list[GridSpec]:
    all_specs = [
        GridSpec("eighth", CANONICAL_DIVISIONS // 2, False, 0.00),
        GridSpec("sixteenth", CANONICAL_DIVISIONS // 4, False, 0.05),
        GridSpec("eighth_triplet", CANONICAL_DIVISIONS // 3, True, 0.11),
        GridSpec("sixteenth_triplet", CANONICAL_DIVISIONS // 6, True, 0.20),
        GridSpec("thirty_second", CANONICAL_DIVISIONS // 8, False, 0.30),
    ]

    if options.minimum_note == "eighth":
        allowed = {"eighth", "eighth_triplet"}
    elif options.minimum_note == "sixteenth":
        allowed = {"eighth", "sixteenth", "eighth_triplet", "sixteenth_triplet"}
    elif options.minimum_note == "thirty_second":
        allowed = {spec.name for spec in all_specs}
    elif options.style == "clean":
        allowed = {"eighth", "sixteenth", "eighth_triplet"}
    else:
        allowed = {spec.name for spec in all_specs}

    if not options.allow_triplets:
        allowed = {name for name in allowed if "triplet" not in name}
    if meter is not None and meter.is_compound:
        # In 6/8, 9/8 and 12/8 the written dotted beat already expresses ternary
        # subdivision. Treating ordinary eighths as quarter-note triplets creates
        # misleading and, for large values, invalid-looking tuplets.
        allowed = {name for name in allowed if "triplet" not in name}

    style_factor = {"clean": 1.6, "balanced": 1.0, "faithful": 0.35}[options.style]
    return [
        replace(spec, complexity=spec.complexity * style_factor)
        for spec in all_specs
        if spec.name in allowed
    ]


def _grid_cost(
    notes: list[QuantizedNote],
    measure_start: int,
    grid: GridSpec,
    style: str,
) -> float:
    errors: list[float] = []
    collapsed = 0
    tiny_values = 0
    for note in notes:
        relative_onset = note.onset - measure_start
        snapped_onset = measure_start + _nearest_multiple(relative_onset, grid.step)
        snapped_end = measure_start + _nearest_multiple(note.end - measure_start, grid.step)
        errors.append(abs(snapped_onset - note.onset) / (CANONICAL_DIVISIONS / 4))
        errors.append(abs(snapped_end - note.end) / (CANONICAL_DIVISIONS / 4))
        if snapped_end <= snapped_onset:
            collapsed += 1
        if snapped_end - snapped_onset <= grid.step:
            tiny_values += 1

    timing_error = fmean(errors) if errors else 0.0
    collapse_penalty = collapsed * 1.25 / len(notes)
    tiny_factor = {"clean": 0.05, "balanced": 0.02, "faithful": 0.0}[style]
    tiny_penalty = tiny_values * tiny_factor / len(notes)
    return timing_error + grid.complexity + collapse_penalty + tiny_penalty


def _grid_timing_error(
    notes: list[QuantizedNote],
    measure_start: int,
    step: int,
) -> float:
    errors: list[float] = []
    for note in notes:
        snapped_onset = measure_start + _nearest_multiple(note.onset - measure_start, step)
        snapped_end = measure_start + _nearest_multiple(note.end - measure_start, step)
        errors.append(abs(snapped_onset - note.onset) / (CANONICAL_DIVISIONS / 4))
        errors.append(abs(snapped_end - note.end) / (CANONICAL_DIVISIONS / 4))
    return fmean(errors) if errors else 0.0


def _nearest_multiple(value: int, step: int) -> int:
    return int((value + step / 2) // step) * step


def _collapse_playable_rolled_chords(
    notes: list[QuantizedNote],
    style: str,
) -> tuple[list[QuantizedNote], int]:
    """Turn tiny performance rolls into one readable, playable chord attack.

    Pianists rarely strike every key of a chord on the exact same MIDI tick.
    When at least three notes enter within a very small window, release together,
    and fit one hand, the clean/balanced score should show one chord rather than a
    spurious extra voice and a chain of ties.  Wider or deliberately paced
    arpeggios remain untouched, as does the faithful style.
    """

    if style == "faithful" or len(notes) < 3:
        return notes, 0

    onset_window = 90
    release_window = 90
    minimum_duration = CANONICAL_DIVISIONS
    by_lane: dict[tuple[int, int], list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_lane[(note.track, note.channel)].append(note)

    collapsed: list[QuantizedNote] = []
    shifted_count = 0
    for lane_notes in by_lane.values():
        ordered = sorted(lane_notes, key=lambda note: (note.onset, note.pitch, note.source_id))
        index = 0
        while index < len(ordered):
            first = ordered[index]
            stop = index + 1
            while stop < len(ordered) and ordered[stop].onset - first.onset <= onset_window:
                stop += 1
            cluster = ordered[index:stop]
            pitches = {note.pitch for note in cluster}
            releases = [note.end for note in cluster]
            velocities = [note.velocity for note in cluster]
            playable_roll = (
                len(cluster) >= 3
                and len(pitches) == len(cluster)
                and len(pitches) <= MAX_SIMULTANEOUS_KEYS_PER_HAND
                and max(pitches) - min(pitches) <= MAX_HAND_SPAN_SEMITONES
                and max(releases) - min(releases) <= release_window
                and max(velocities) - min(velocities) <= 24
                and min(note.duration for note in cluster) >= minimum_duration
            )
            if playable_roll:
                chord_onset = first.onset
                for note in cluster:
                    if note.onset != chord_onset:
                        shifted_count += 1
                    collapsed.append(
                        replace(
                            note,
                            onset=chord_onset,
                            duration=max(1, note.end - chord_onset),
                        )
                    )
            else:
                collapsed.extend(cluster)
            index = stop

    return (
        sorted(collapsed, key=lambda note: (note.onset, note.pitch, note.source_id)),
        shifted_count,
    )


def _deduplicate_notes(notes: list[QuantizedNote]) -> list[QuantizedNote]:
    selected: dict[tuple[int, int], QuantizedNote] = {}
    for note in notes:
        key = (note.onset, note.pitch)
        previous = selected.get(key)
        if previous is None or (note.duration, note.velocity) > (previous.duration, previous.velocity):
            selected[key] = note
    return sorted(selected.values(), key=lambda note: (note.onset, note.pitch, note.source_id))


def _resolve_repeated_pitch_overlaps(notes: list[QuantizedNote]) -> list[QuantizedNote]:
    by_pitch: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_pitch[note.pitch].append(note)

    resolved: list[QuantizedNote] = []
    for pitch_notes in by_pitch.values():
        pitch_notes.sort(key=lambda note: (note.onset, note.end))
        for index, note in enumerate(pitch_notes):
            if index + 1 < len(pitch_notes):
                next_note = pitch_notes[index + 1]
                if note.end > next_note.onset:
                    note = replace(note, duration=max(1, next_note.onset - note.onset))
            if note.duration > 0:
                resolved.append(note)
    return sorted(resolved, key=lambda note: (note.onset, note.pitch, note.source_id))
