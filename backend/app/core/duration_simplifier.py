from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import replace
from statistics import median

from .meter_map import measure_index_at
from .models import (
    CANONICAL_DIVISIONS,
    GridDecision,
    MeasureSpan,
    PedalEvent,
    QuantizedNote,
    Staff,
)
from .piano_rules import PedalCoverage


def simplify_polyphonic_durations(
    notes: list[QuantizedNote],
    *,
    max_voices: int,
    style: str,
    pedals: list[PedalEvent] | None = None,
    measures: list[MeasureSpan] | None = None,
    transcription_mode: bool = False,
    grid_decisions: list[GridDecision] | None = None,
) -> tuple[list[QuantizedNote], dict[str, object], list[str]]:
    """Reduce performance-only overlaps while preserving every note attack.

    A pianist often releases or overlaps keys for legato and pedal colour. Those
    durations are not independent notated voices. When existing sustained layers
    leave fewer voice slots than a new onset contains, new notes are snapped to a
    small set of shared durations. The faithful style deliberately skips this
    readability normalization.
    """

    if style == "faithful" or not notes:
        return (
            notes,
            {
                "adjusted_note_count": 0,
                "shortened_note_count": 0,
                "pedal_extended_note_count": 0,
                "legato_extended_note_count": 0,
                "transcription_release_extended_note_count": 0,
                "transcription_release_normalized_note_count": 0,
                "method": "disabled",
            },
            [],
        )

    coherent_attack_groups = (
        _coherent_attack_duration_groups(notes) if transcription_mode else []
    )
    notes, pedal_extended, legato_extended = _extend_release_gaps_to_metric_boundaries(
        notes,
        pedals or [],
        measures or [],
    )
    transcription_extended = 0
    transcription_normalized = 0
    if transcription_mode:
        notes, transcription_extended, transcription_normalized = (
            _extend_transcription_release_gaps(
                notes,
                measures or [],
                style,
                pedals or [],
                grid_decisions=grid_decisions,
            )
        )
    shortened = 0
    result: list[QuantizedNote] = []
    for staff in (Staff.RIGHT, Staff.LEFT):
        staff_notes = sorted(
            (note for note in notes if note.staff == staff),
            key=lambda note: (note.onset, note.duration, note.pitch, note.source_id),
        )
        by_onset: dict[int, list[int]] = defaultdict(list)
        for index, note in enumerate(staff_notes):
            by_onset[note.onset].append(index)

        active_indices: set[int] = set()
        for onset in sorted(by_onset):
            active_indices = {
                index for index in active_indices if staff_notes[index].end > onset
            }
            active_groups: dict[tuple[int, int], list[int]] = defaultdict(list)
            for active_index in active_indices:
                active_note = staff_notes[active_index]
                active_groups[(active_note.onset, active_note.duration)].append(active_index)

            if len(active_groups) >= max_voices:
                keep_count = max(0, max_voices - 1)
                ranked = sorted(
                    active_groups,
                    key=lambda key: _active_importance(
                        [staff_notes[index] for index in active_groups[key]],
                        staff,
                    ),
                    reverse=True,
                )
                keep = set(ranked[:keep_count])
                shortened_indices: set[int] = set()
                for key, active_group_indices in active_groups.items():
                    if key in keep:
                        continue
                    for active_index in active_group_indices:
                        note = staff_notes[active_index]
                        target = onset - note.onset
                        if 0 < target < note.duration:
                            staff_notes[active_index] = replace(note, duration=target)
                            shortened += 1
                            shortened_indices.add(active_index)
                active_indices.difference_update(shortened_indices)

            active_layers = {
                (staff_notes[index].onset, staff_notes[index].duration)
                for index in active_indices
            }
            indices = by_onset[onset]
            durations = sorted({staff_notes[index].duration for index in indices})
            allowed_new_layers = max(1, max_voices - len(active_layers))
            if len(durations) <= allowed_new_layers:
                active_indices.update(indices)
                continue

            targets = _duration_targets(durations, allowed_new_layers)
            for index in indices:
                note = staff_notes[index]
                target = min(targets, key=lambda value: (abs(value - note.duration), value))
                if target != note.duration:
                    staff_notes[index] = replace(note, duration=target)
                    shortened += 1
            active_indices.update(indices)
        result.extend(staff_notes)

    coherent_attack_repairs = 0
    if coherent_attack_groups:
        result, coherent_attack_repairs = _restore_coherent_attack_durations(
            result,
            coherent_attack_groups,
        )

    warnings = []
    if pedal_extended:
        warnings.append(
            f"踏板已覆盖松键空隙，已将 {pedal_extended} 个长音延至相邻强拍或小节边界，消除碎休止符"
        )
    if legato_extended:
        warnings.append(
            f"已将 {legato_extended} 个跨至少两个强拍、且仅提前十六分音符松键的长音对齐到边界"
        )
    if transcription_extended:
        warnings.append(
            f"音频转录的按键释放较碎，已将 {transcription_extended} 个短释放间隙延至下一起音或节拍边界（起音全部保留）"
        )
    if shortened:
        warnings.append(
            f"为消除演奏型重叠并保持最多 {max_voices} 层清晰声部，已规范化 {shortened} 个音符时值（音头全部保留）"
        )
    if coherent_attack_repairs:
        warnings.append(
            f"根据原始同起音与释放关系，统一了 {coherent_attack_repairs} 个八度或和弦音的书写时值"
        )
    adjusted = (
        pedal_extended
        + legato_extended
        + transcription_extended
        + transcription_normalized
        + shortened
        + coherent_attack_repairs
    )
    return (
        sorted(result, key=lambda note: (note.onset, note.pitch, note.source_id)),
        {
            "adjusted_note_count": adjusted,
            "shortened_note_count": shortened,
            "pedal_extended_note_count": pedal_extended,
            "legato_extended_note_count": legato_extended,
            "transcription_release_extended_note_count": transcription_extended,
            "transcription_release_normalized_note_count": transcription_normalized,
            "coherent_attack_duration_repair_count": coherent_attack_repairs,
            "method": "transcription_aware_shared_onset_duration_reduction",
        },
        warnings,
    )


def _extend_transcription_release_gaps(
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
    style: str,
    pedals: list[PedalEvent],
    grid_decisions: list[GridDecision] | None = None,
) -> tuple[list[QuantizedNote], int, int]:
    """Infer conventional written releases from noisy audio offsets.

    Audio models estimate attacks much more reliably than key releases. In
    clean/balanced output, a sixteenth-note-or-smaller silence immediately
    before the next staff attack or metric boundary is normally articulation,
    not a request for another printed rest. Extending the written copy closes
    that gap without moving or deleting any attack; physical review still uses
    the untouched notes captured earlier in the pipeline.
    """

    if not notes:
        return notes, 0, 0

    # The legacy MIDIRearrange utility in the user's earlier workflow correctly
    # treated attacks as the reliable part of an audio transcription, but its
    # global "release at the next attack" rule truncated melody.  Audio models
    # also commonly release an eighth-note accompaniment after a sixteenth, so
    # extending every such note to the next detected attack can create hundreds
    # of false dotted eighths whenever an intervening attack was missed.
    #
    # Use a written attack cell instead: clean output infers ordinary releases
    # no farther than an eighth note, while balanced output stays at a
    # sixteenth.  Longer values require stronger evidence from the original
    # release, a melodic/bass edge, or continuous pedal under a low bass note.
    # The cell follows the measure's quantization grid: in triplet measures a
    # binary cell would manufacture mixed-grid items whose tuplet brackets
    # cannot close (MuseScore then reports the measure as corrupt).
    base_cell = CANONICAL_DIVISIONS // (2 if style == "clean" else 4)
    grid_steps = {
        decision.measure_index: decision.step for decision in grid_decisions or []
    }

    def cell_at(onset: int) -> int:
        if not measures or not grid_steps:
            return base_cell
        step = grid_steps.get(measure_index_at(measures, onset))
        if step == CANONICAL_DIVISIONS // 3:
            return CANONICAL_DIVISIONS // 3
        if step == CANONICAL_DIVISIONS // 6:
            return CANONICAL_DIVISIONS // (3 if style == "clean" else 6)
        return base_cell

    coverage = PedalCoverage(pedals)
    onsets_by_staff: dict[Staff, list[int]] = defaultdict(list)
    notes_by_attack: dict[tuple[Staff, int], list[QuantizedNote]] = defaultdict(list)
    next_same_pitch: dict[int, int | None] = {}
    notes_by_pitch: dict[tuple[int, int], list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        if note.staff is not None:
            onsets_by_staff[note.staff].append(note.onset)
            notes_by_attack[(note.staff, note.onset)].append(note)
        notes_by_pitch[(note.channel, note.pitch)].append(note)
    for staff in onsets_by_staff:
        onsets_by_staff[staff] = sorted(set(onsets_by_staff[staff]))
    for pitch_notes in notes_by_pitch.values():
        ordered = sorted(pitch_notes, key=lambda note: (note.onset, note.end, note.source_id))
        for index, note in enumerate(ordered):
            next_same_pitch[note.source_id] = (
                ordered[index + 1].onset if index + 1 < len(ordered) else None
            )

    extended = 0
    normalized = 0
    result: list[QuantizedNote] = []
    for note in notes:
        if note.staff is None:
            result.append(note)
            continue
        attack_cell = cell_at(note.onset)
        maximum_gap = attack_cell
        later_onsets = onsets_by_staff[note.staff]
        next_index = bisect_right(later_onsets, note.onset)
        next_onset = (
            later_onsets[next_index] if next_index < len(later_onsets) else None
        )
        attack_notes = notes_by_attack[(note.staff, note.onset)]
        next_attack_notes = (
            notes_by_attack[(note.staff, next_onset)]
            if next_onset is not None
            else []
        )
        column_edge = _is_staff_edge(note, attack_notes)
        held_edge = _is_held_melodic_edge(note, attack_notes, next_attack_notes)
        same_pitch_onset = next_same_pitch.get(note.source_id)
        release_onset = next_onset
        if held_edge:
            release_onset = _next_role_onset(
                note,
                later_onsets[next_index + 1 :],
                notes_by_attack,
            )

        written_duration = note.duration
        next_attack_distance = (
            next_onset - note.onset if next_onset is not None else None
        )
        column_has_attack_cell = any(
            other.duration == attack_cell for other in attack_notes
        )
        uniform_attack_durations = len(
            {other.duration for other in attack_notes}
        ) == 1
        overlaps_adjacent_attack = (
            next_attack_distance is not None
            and next_attack_distance <= attack_cell
            and written_duration > next_attack_distance
        )
        uneven_inner_dotted_value = (
            written_duration == attack_cell + attack_cell // 2
            and len(attack_notes) > 1
            and column_has_attack_cell
            and not column_edge
        )
        repeated_pitch_overlap = (
            same_pitch_onset is not None
            and same_pitch_onset < note.onset + written_duration
        )
        release_was_normalized = False
        if (
            (overlaps_adjacent_attack and not held_edge)
            or uneven_inner_dotted_value
            or repeated_pitch_overlap
        ):
            capped_duration = attack_cell
            if next_attack_distance is not None:
                capped_duration = min(capped_duration, next_attack_distance)
            if same_pitch_onset is not None:
                capped_duration = min(capped_duration, same_pitch_onset - note.onset)
            if 0 < capped_duration < written_duration:
                written_duration = capped_duration
                release_was_normalized = True
                normalized += 1

        boundary, _ = _next_metric_boundary(note.end, measures)
        current_end = note.onset + written_duration
        aligned_release_boundary = None
        if (
            written_duration >= attack_cell
            and note.onset % attack_cell == 0
            and current_end % attack_cell
        ):
            aligned_release_boundary = (
                current_end + attack_cell - current_end % attack_cell
            )
        structural_targets = [
            target
            for target in (release_onset, boundary, same_pitch_onset)
            if target is not None and target > note.onset
        ]
        if current_end in structural_targets:
            result.append(replace(note, duration=written_duration))
            continue
        future_targets = [target for target in structural_targets if target > current_end]
        if not future_targets:
            if aligned_release_boundary is None:
                result.append(replace(note, duration=written_duration))
                continue
            target = aligned_release_boundary
        else:
            target = min(future_targets)
        target_duration = target - note.onset
        gap = target_duration - written_duration
        release_gap_limit = maximum_gap + (
            attack_cell // 2 if len(attack_notes) >= 2 else 0
        )
        pedal_low_bass = (
            note.staff == Staff.LEFT
            and column_edge
            and note.onset % attack_cell == 0
            and target_duration % attack_cell == 0
            and coverage.covers(note.channel, note.onset + written_duration, target)
        )
        aligned_edge_value = (
            written_duration < attack_cell
            and column_edge
            and release_onset is not None
            and target == release_onset
            and note.onset % attack_cell == 0
            and target_duration % attack_cell == 0
            and target_duration <= attack_cell * 2
        )
        aligned_release_fill = (
            aligned_release_boundary is not None
            and aligned_release_boundary > current_end
            and aligned_release_boundary - current_end <= attack_cell // 2
        )
        monophonic_release = (
            written_duration >= attack_cell
            and not release_was_normalized
            and len(attack_notes) == 1
            and next_onset is not None
            and len(next_attack_notes) == 1
            and target == next_onset
            and gap <= attack_cell // 2
        )
        regular_duration = target_duration // attack_cell * attack_cell
        aligned_regular_fill = (
            note.onset % attack_cell == 0
            and regular_duration > written_duration
            and regular_duration - written_duration <= release_gap_limit
            and (
                column_edge
                or len(attack_notes) == 1
                or uniform_attack_durations
                or (
                    written_duration >= attack_cell
                    and not release_was_normalized
                )
            )
        )

        inferred_candidates = [written_duration]
        if written_duration < attack_cell:
            inferred_candidates.append(min(target_duration, attack_cell))
        if (
            pedal_low_bass
            or aligned_edge_value
            or aligned_release_fill
            or (held_edge and gap <= release_gap_limit)
            or monophonic_release
        ):
            inferred_candidates.append(
                aligned_release_boundary - note.onset
                if aligned_release_fill
                and not (
                    pedal_low_bass
                    or aligned_edge_value
                    or held_edge
                    or monophonic_release
                )
                else target_duration
            )
        if aligned_regular_fill:
            inferred_candidates.append(regular_duration)
        if (
            column_edge
            and target_duration % attack_cell == 0
            and gap <= maximum_gap
        ):
            inferred_candidates.append(target_duration)

        inferred_duration = max(inferred_candidates)
        if inferred_duration > written_duration:
            written_duration = inferred_duration
            extended += 1
        result.append(replace(note, duration=written_duration))
    return result, extended, normalized


def _is_staff_edge(note: QuantizedNote, attack_notes: list[QuantizedNote]) -> bool:
    pitches = [other.pitch for other in attack_notes]
    if note.staff == Staff.RIGHT:
        return note.pitch == max(pitches)
    return note.pitch == min(pitches)


def _is_held_melodic_edge(
    note: QuantizedNote,
    attack_notes: list[QuantizedNote],
    next_attack_notes: list[QuantizedNote],
) -> bool:
    """Preserve a registrally separate melody or bass over accompaniment."""

    if not next_attack_notes or not _is_staff_edge(note, attack_notes):
        return False
    if note.staff == Staff.RIGHT:
        return note.pitch >= max(other.pitch for other in next_attack_notes) + 5
    return note.pitch <= min(other.pitch for other in next_attack_notes) - 5


def _next_role_onset(
    note: QuantizedNote,
    later_onsets: list[int],
    notes_by_attack: dict[tuple[Staff, int], list[QuantizedNote]],
) -> int | None:
    """Find the next melody/bass attack while skipping separated accompaniment."""

    maximum_onset = note.onset + CANONICAL_DIVISIONS * 2
    for onset in later_onsets:
        if onset > maximum_onset:
            break
        attack_notes = notes_by_attack[(note.staff, onset)]
        if note.staff == Staff.RIGHT:
            edge_pitch = max(other.pitch for other in attack_notes)
            if note.pitch - 7 <= edge_pitch <= note.pitch + 12:
                return onset
        else:
            edge_pitch = min(other.pitch for other in attack_notes)
            if note.pitch - 12 <= edge_pitch <= note.pitch + 7:
                return onset
    return None


def _extend_release_gaps_to_metric_boundaries(
    notes: list[QuantizedNote],
    pedals: list[PedalEvent],
    measures: list[MeasureSpan],
) -> tuple[list[QuantizedNote], int, int]:
    """Remove tiny release gaps that do not deserve a printed rest.

    A key-up shortly before a strong metric boundary is performance articulation,
    not a request for a printed sixteenth rest, when either the pedal carries the
    sound or a metrically aligned note already spans two strong beats.  Extending
    only the written copy produces conventional long notes while the untouched
    pre-normalization notes remain available for physical playability checks.
    """

    if not notes or not measures:
        return notes, 0, 0

    coverage = PedalCoverage(pedals)
    maximum_gap = CANONICAL_DIVISIONS // 4
    onsets_by_staff: dict[Staff, set[int]] = defaultdict(set)
    next_same_pitch: dict[int, int | None] = {}
    by_pitch: dict[tuple[int, int], list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        if note.staff is not None:
            onsets_by_staff[note.staff].add(note.onset)
        by_pitch[(note.channel, note.pitch)].append(note)
    for pitch_notes in by_pitch.values():
        ordered = sorted(pitch_notes, key=lambda note: (note.onset, note.end, note.source_id))
        for index, note in enumerate(ordered):
            next_same_pitch[note.source_id] = (
                ordered[index + 1].onset if index + 1 < len(ordered) else None
            )

    pedal_extended = 0
    legato_extended = 0
    result: list[QuantizedNote] = []
    for note in notes:
        boundary, group_length = _next_metric_boundary(note.end, measures)
        gap = boundary - note.end if boundary is not None else 0
        pedal_carries_gap = (
            boundary is not None
            and coverage.covers(note.channel, note.end, boundary)
        )
        long_metric_legato = (
            boundary is not None
            and note.duration >= group_length * 2
            and _is_metric_boundary(note.onset, measures)
        )
        if (
            note.staff is None
            or boundary is None
            or not 0 < gap <= maximum_gap
            or note.duration < group_length
            or not (pedal_carries_gap or long_metric_legato)
            or any(
                note.end <= onset < boundary
                for onset in onsets_by_staff[note.staff]
            )
            or (
                next_same_pitch.get(note.source_id) is not None
                and next_same_pitch[note.source_id] < boundary
            )
        ):
            result.append(note)
            continue
        result.append(replace(note, duration=boundary - note.onset))
        if pedal_carries_gap:
            pedal_extended += 1
        else:
            legato_extended += 1
    return result, pedal_extended, legato_extended


def _next_metric_boundary(
    tick: int,
    measures: list[MeasureSpan],
) -> tuple[int | None, int]:
    for measure in measures:
        if not measure.start <= tick < measure.end:
            continue
        relative = tick - measure.start
        previous = 0
        for boundary in measure.meter.beat_group_boundaries[1:]:
            if boundary > relative:
                return measure.start + boundary, boundary - previous
            previous = boundary
        return measure.end, measure.duration
    return None, CANONICAL_DIVISIONS


def _is_metric_boundary(tick: int, measures: list[MeasureSpan]) -> bool:
    for measure in measures:
        if not measure.start <= tick <= measure.end:
            continue
        relative = tick - measure.start
        return relative in measure.meter.beat_group_boundaries
    return False


def _duration_targets(durations: list[int], allowed: int) -> list[int]:
    if allowed <= 1:
        return [durations[0]]
    if allowed >= len(durations):
        return durations
    if allowed == 2:
        return [durations[0], durations[-1]]
    return [
        durations[round(index * (len(durations) - 1) / (allowed - 1))]
        for index in range(allowed)
    ]


def _active_importance(notes: list[QuantizedNote], staff: Staff) -> float:
    duration = max(note.duration for note in notes)
    center = sum(note.pitch for note in notes) / len(notes)
    register = center if staff == Staff.RIGHT else -center
    return duration + register * 2.0


_PLAIN_DURATIONS = (1920, 1440, 960, 720, 480, 360, 240, 180, 120, 90, 60, 30)
_RATIO_DURATIONS = (384, 320, 288, 192, 160, 144, 96, 80, 48, 40)
_PLAIN_DURATION_SET = frozenset(_PLAIN_DURATIONS)
_ATOMIC_DURATION_SET = frozenset((*_PLAIN_DURATIONS, *_RATIO_DURATIONS))
_SHORT_SLOT_DURATIONS = tuple(value for value in _PLAIN_DURATIONS if value <= 480)


def repair_repeated_rhythm_durations(
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
    *,
    transcription_mode: bool,
    grid_decisions: list[GridDecision] | None = None,
) -> tuple[list[QuantizedNote], int]:
    """Repair short release outliers by comparing repeated beat patterns.

    Attacks are substantially more reliable than releases in both performed
    MIDI and audio transcription.  Piano writing also repeats accompaniment
    cells constantly.  Beat groups with the same attack offsets therefore act
    as mutual witnesses: a lone 210-tick release among repeated 240-tick cells
    is normalized to the consensus, while a consistently short *clean* value
    remains a real short note instead of being relabelled as staccato.

    Audio mode may also snap a coherent repeated non-atomic value to the nearest
    single notational duration.  Direct MIDI uses the more conservative exact-
    consensus path only.
    """

    if len(notes) < 6 or not measures:
        return notes, 0

    ratio_measures = {
        decision.measure_index
        for decision in grid_decisions or []
        if decision.triplet
    }
    occurrences: dict[
        tuple[Staff, int],
        dict[tuple[int, int, int, int], list[QuantizedNote]],
    ] = defaultdict(lambda: defaultdict(list))
    for note in notes:
        if note.staff is None or note.grace or note.trill:
            continue
        location = _metric_group_at(note.onset, measures)
        if location is not None:
            occurrences[(note.staff, note.voice)][location].append(note)

    replacements: dict[int, int] = {}
    for lane_occurrences in occurrences.values():
        patterns: dict[
            tuple[int, tuple[int, ...]],
            list[tuple[int, dict[int, list[QuantizedNote]]]],
        ] = defaultdict(list)
        for (measure_index, _group_index, group_start, group_length), group_notes in (
            lane_occurrences.items()
        ):
            columns: dict[int, list[QuantizedNote]] = defaultdict(list)
            for note in group_notes:
                columns[note.onset - group_start].append(note)
            offsets = tuple(sorted(columns))
            if len(offsets) >= 2:
                patterns[(group_length, offsets)].append((measure_index, columns))

        for (group_length, offsets), pattern_occurrences in patterns.items():
            if len(pattern_occurrences) < 3:
                continue
            uses_ratio_grid = any(
                measure_index in ratio_measures
                for measure_index, _columns in pattern_occurrences
            )
            allowed_set = _ATOMIC_DURATION_SET if uses_ratio_grid else _PLAIN_DURATION_SET
            for offset_index, offset in enumerate(offsets):
                next_offset = (
                    offsets[offset_index + 1]
                    if offset_index + 1 < len(offsets)
                    else group_length
                )
                slot = next_offset - offset
                representatives: list[tuple[list[QuantizedNote], int]] = []
                for _measure_index, columns in pattern_occurrences:
                    duration_counts = defaultdict(int)
                    for note in columns[offset]:
                        duration_counts[note.duration] += 1
                    current, support = max(
                        duration_counts.items(),
                        key=lambda item: (item[1], -item[0]),
                    )
                    if support * 2 >= len(columns[offset]):
                        representatives.append((columns[offset], current))
                values = [value for _column, value in representatives]
                if len(values) < 3:
                    continue

                value_counts: dict[int, int] = defaultdict(int)
                for value in values:
                    value_counts[value] += 1
                exact_target, exact_support = max(
                    value_counts.items(),
                    key=lambda item: (item[1], -item[0]),
                )
                center = float(median(values))
                target: int | None = None
                if (
                    exact_target in allowed_set
                    and exact_target <= slot
                    and exact_support >= 3
                    and exact_support * 5 >= len(values) * 3
                ):
                    target = exact_target
                elif transcription_mode:
                    coherent = sum(abs(value - center) <= 60 for value in values)
                    if coherent >= 3 and coherent * 10 >= len(values) * 7:
                        allowed = [value for value in allowed_set if value <= slot]
                        if allowed:
                            if (
                                slot in allowed_set
                                and center not in allowed_set
                                and 0 < slot - center <= 60
                            ):
                                target = slot
                            else:
                                target = min(
                                    allowed,
                                    key=lambda value: (abs(value - center), value),
                                )
                if target is None or abs(target - center) > 60:
                    continue

                for column, current in representatives:
                    if current == target or abs(current - target) > 120:
                        continue
                    if current in allowed_set:
                        strong_outlier = (
                            exact_target == target
                            and exact_support * 5 >= len(values) * 4
                            and value_counts[current] == 1
                        )
                        if not strong_outlier:
                            continue
                    elif abs(current - center) > 60:
                        continue
                    for note in column:
                        if (
                            note.duration == current
                            and not note.staccato
                            and not note.tremolo_start
                            and not note.tremolo_stop
                        ):
                            replacements[note.source_id] = target

    if not replacements:
        return notes, 0
    repaired = [
        replace(note, duration=replacements.get(note.source_id, note.duration))
        for note in notes
    ]
    return (
        sorted(repaired, key=lambda note: (note.onset, note.pitch, note.source_id)),
        len(replacements),
    )


def _coherent_attack_duration_groups(
    notes: list[QuantizedNote],
) -> list[frozenset[int]]:
    """Remember octave/chord columns whose original gates agreed.

    Audio release inference may legitimately hold a melodic edge over moving
    accompaniment.  It must not, however, split an octave or chord whose notes
    arrived together *and* had the same original gate: that manufactures two
    written voices and leaves visually mismatched noteheads.  Only octave
    doublings and three-note-or-larger chords are protected, so an intentional
    two-note contrapuntal attack can still retain independent durations.
    """

    columns: dict[tuple[Staff, object, int], list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        if (
            note.staff is not None
            and not note.grace
            and not note.trill
            and not note.arpeggiated
            and not note.tremolo_start
            and not note.tremolo_stop
        ):
            columns[(note.staff, note.hand, note.onset)].append(note)

    groups: list[frozenset[int]] = []
    maximum_gate_spread = CANONICAL_DIVISIONS // 8
    for column in columns.values():
        if len(column) < 2:
            continue
        pitches = {note.pitch for note in column}
        contains_octave = any(pitch + 12 in pitches for pitch in pitches)
        if not contains_octave and len(column) < 3:
            continue
        durations = [note.duration for note in column]
        if max(durations) - min(durations) > maximum_gate_spread:
            continue
        groups.append(frozenset(note.source_id for note in column))
    return groups


def _restore_coherent_attack_durations(
    notes: list[QuantizedNote],
    groups: list[frozenset[int]],
) -> tuple[list[QuantizedNote], int]:
    by_id = {note.source_id: note for note in notes}
    replacements: dict[int, int] = {}
    for source_ids in groups:
        members = [by_id[source_id] for source_id in source_ids if source_id in by_id]
        if len(members) < 2:
            continue
        target = min(note.duration for note in members)
        if target <= 0:
            continue
        for note in members:
            if note.duration != target:
                replacements[note.source_id] = target

    if not replacements:
        return notes, 0
    return (
        [
            replace(note, duration=replacements.get(note.source_id, note.duration))
            for note in notes
        ],
        len(replacements),
    )


def normalize_short_gate_slots(
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
) -> tuple[list[QuantizedNote], int]:
    """Normalize context-supported short gates to half-slot values + staccato.

    Some MIDI sources encode articulation as short gate times: a detached
    sixteenth arrives as a 32nd-long keypress.  Printed literally — or worse,
    split into a half-slot note plus a half-slot rest — the page drowns in
    16th/32nd rests no player ever reads.  A dot is added only when repeated
    rhythm cells or a nearby detached run support an articulation pattern.
    Isolated short notes keep their shorter written value and do not acquire a
    semantic staccato mark merely because the key was released early.
    """

    lane_attacks: dict[tuple[object, int], list[int]] = defaultdict(list)
    for note in notes:
        if note.staff is not None:
            lane_attacks[(note.staff, note.voice)].append(note.onset)
    for lane in lane_attacks:
        lane_attacks[lane] = sorted(set(lane_attacks[lane]))

    proposed_durations: dict[int, int] = {}
    candidate_columns: set[tuple[Staff, int, int]] = set()
    for note in notes:
        if note.staff is not None and note.staccato:
            candidate_columns.add((note.staff, note.voice, note.onset))
        if note.staff is None or note.grace or note.trill or note.tremolo_start or note.tremolo_stop:
            continue
        if note.duration % 30 or note.duration >= CANONICAL_DIVISIONS // 2:
            continue
        attacks = lane_attacks.get((note.staff, note.voice), [])
        next_index = bisect_right(attacks, note.onset)
        if next_index >= len(attacks):
            continue
        slot = attacks[next_index] - note.onset
        if slot <= note.duration:
            continue
        if note.duration * 20 > slot * 11:  # gate longer than ~55% of the slot
            continue
        # Preserve a real short note: the written value reaches at most half
        # the inter-attack slot.  The staccato dot communicates the remaining
        # articulation only when the surrounding pattern supports it.
        clean = [value for value in _SHORT_SLOT_DURATIONS if value <= slot // 2]
        if not clean:
            continue
        written = max(clean)
        if written <= note.duration or written % 30:
            continue
        proposed_durations[note.source_id] = written
        candidate_columns.add((note.staff, note.voice, note.onset))

    supported_columns = _supported_staccato_columns(
        notes,
        measures,
        candidate_columns,
    )
    adjusted = 0
    result: list[QuantizedNote] = []
    for note in notes:
        key = (
            (note.staff, note.voice, note.onset)
            if note.staff is not None
            else None
        )
        if key is not None and key in supported_columns:
            written = proposed_durations.get(note.source_id)
            if written is not None and written > note.duration:
                result.append(replace(note, duration=written, staccato=True))
                adjusted += 1
            else:
                result.append(note)
        elif note.staccato:
            result.append(replace(note, staccato=False))
        else:
            result.append(note)

    return (
        sorted(result, key=lambda note: (note.onset, note.pitch, note.source_id)),
        adjusted,
    )


def _supported_staccato_columns(
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
    candidates: set[tuple[Staff, int, int]],
) -> set[tuple[Staff, int, int]]:
    if not candidates:
        return set()

    supported: set[tuple[Staff, int, int]] = set()
    lane_attacks: dict[tuple[Staff, int], set[int]] = defaultdict(set)
    for note in notes:
        if note.staff is not None and not note.grace and not note.trill:
            lane_attacks[(note.staff, note.voice)].add(note.onset)
    for lane, attacks in lane_attacks.items():
        run: list[int] = []
        for onset in sorted(attacks):
            key = (lane[0], lane[1], onset)
            if key in candidates and (
                not run or onset - run[-1] <= CANONICAL_DIVISIONS
            ):
                run.append(onset)
                continue
            if len(run) >= 3:
                supported.update((lane[0], lane[1], value) for value in run)
            run = [onset] if key in candidates else []
        if len(run) >= 3:
            supported.update((lane[0], lane[1], value) for value in run)

    occurrences: dict[
        tuple[Staff, int],
        dict[tuple[int, int, int, int], list[QuantizedNote]],
    ] = defaultdict(lambda: defaultdict(list))
    for note in notes:
        if note.staff is None:
            continue
        location = _metric_group_at(note.onset, measures)
        if location is not None:
            occurrences[(note.staff, note.voice)][location].append(note)

    for lane, lane_occurrences in occurrences.items():
        patterns: dict[
            tuple[int, tuple[int, ...]],
            list[tuple[int, list[QuantizedNote]]],
        ] = defaultdict(list)
        for (_measure_index, _group_index, group_start, group_length), group_notes in (
            lane_occurrences.items()
        ):
            offsets = tuple(sorted({note.onset - group_start for note in group_notes}))
            if offsets:
                patterns[(group_length, offsets)].append((group_start, group_notes))
        for (_group_length, _offsets), pattern_occurrences in patterns.items():
            if len(pattern_occurrences) < 3:
                continue
            counts: dict[int, int] = defaultdict(int)
            for group_start, group_notes in pattern_occurrences:
                candidate_offsets = {
                    note.onset - group_start
                    for note in group_notes
                    if (note.staff, note.voice, note.onset) in candidates
                }
                for offset in candidate_offsets:
                    counts[offset] += 1
            repeated_offsets = {
                offset
                for offset, count in counts.items()
                if count >= 3 and count * 2 >= len(pattern_occurrences)
            }
            for group_start, _group_notes in pattern_occurrences:
                for offset in repeated_offsets:
                    key = (lane[0], lane[1], group_start + offset)
                    if key in candidates:
                        supported.add(key)
    return supported


def _metric_group_at(
    tick: int,
    measures: list[MeasureSpan],
) -> tuple[int, int, int, int] | None:
    if not measures:
        return None
    measure_index = measure_index_at(measures, tick)
    measure = measures[measure_index]
    if measure.implicit:
        return None
    relative = tick - measure.start
    boundaries = measure.meter.beat_group_boundaries
    for group_index, (start, end) in enumerate(zip(boundaries, boundaries[1:], strict=False)):
        if start <= relative < end:
            return (
                measure_index,
                group_index,
                measure.start + start,
                end - start,
            )
    return None


def absorb_articulation_gaps(
    notes: list[QuantizedNote],
    *,
    max_gap: int = 120,
    measures: list[MeasureSpan] | None = None,
    grid_decisions: list[GridDecision] | None = None,
) -> tuple[list[QuantizedNote], int]:
    """Swallow articulation gaps into the preceding note.

    A played attack never lands exactly on its written release: transcriptions
    are full of 30–120-tick silences that are just key noise, and printing
    each as a 16th/32nd rest makes the page unreadable.  Extending the
    previous note to the next attack turns the gap into ordinary legato
    spacing; the downstream grid snap re-notates the total cleanly.
    """

    lanes: dict[tuple[object, int], list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        if note.staff is not None:
            lanes[(note.staff, note.voice)].append(note)

    ratio_measures = {
        decision.measure_index
        for decision in grid_decisions or []
        if decision.triplet
    }
    absorbed = 0
    swallow: dict[int, int] = {}
    for lane_notes in lanes.values():
        lane_notes.sort(key=lambda note: (note.onset, note.pitch))
        for current, following in zip(lane_notes, lane_notes[1:], strict=False):
            if current.grace or current.trill or current.tremolo_start or current.tremolo_stop:
                continue
            gap = following.onset - (current.onset + current.duration)
            target_duration = following.onset - current.onset
            allowed = _PLAIN_DURATION_SET
            if measures is not None:
                measure_index = measure_index_at(measures, current.onset)
                if measure_index in ratio_measures:
                    allowed = _ATOMIC_DURATION_SET
            if 0 < gap <= max_gap and target_duration in allowed:
                swallow[id(current)] = target_duration
                absorbed += 1

    if not absorbed:
        return notes, 0
    return (
        [
            replace(note, duration=swallow[id(note)]) if id(note) in swallow else note
            for note in notes
        ],
        absorbed,
    )
