from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from itertools import pairwise
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

AUTO_TRIPLET_MEASURE_SHARE = 0.05
AUTO_TUPLET_FIT_RATIO = 0.8


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
    auto_detected_tuplets = False
    if options.audio_transcription and not options.allow_triplets:
        probe = options.model_copy(update={"allow_triplets": True})
        votes = 0
        total = 0
        for measure_index, measure in enumerate(measures):
            measure_notes = notes_by_measure.get(measure_index, [])
            if len(measure_notes) < 3:
                continue
            # The vote compares ternary against standard binary grids only.
            # Ultra-fine grids fit *any* timing almost exactly, so including
            # them would destroy the calibrated margin that separates genuine
            # triplets from model timing noise.  Keep the probe on the grid
            # set the vote was calibrated with.
            probe_candidates = [
                grid
                for grid in _grid_candidates(probe, measure.meter)
                if grid.name
                not in {"thirty_second", "thirty_second_triplet", "sixty_fourth"}
            ]
            binary_error = min(
                (_grid_timing_error(
                    measure_notes,
                    measure.start,
                    grid.step,
                    onsets_only=True,
                )
                 for grid in probe_candidates if not grid.triplet),
                default=None,
            )
            triplet_error = min(
                (_grid_timing_error(
                    measure_notes,
                    measure.start,
                    grid.step,
                    onsets_only=True,
                )
                 for grid in probe_candidates if "triplet" in grid.name),
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
        if total >= 4 and votes / total >= AUTO_TRIPLET_MEASURE_SHARE:
            candidate_options = probe
            auto_detected_tuplets = True
            warnings.append(
                f"有 {votes}/{total} 个小节在三连音网格上的拟合显著更优，已自动启用三连音识别"
            )

    decisions: list[GridDecision] = []
    quantized: list[QuantizedNote] = []

    for measure_index, measure in enumerate(measures):
        candidates = _grid_candidates(candidate_options, measure.meter)
        if auto_detected_tuplets:
            # The global probe only establishes ternary evidence.  It must not
            # silently unlock quintuplets as well: five-way timing fits are too
            # easy to manufacture from transcription jitter and need an
            # explicit user choice instead of piggybacking on triplet votes.
            candidates = [
                grid for grid in candidates if "quintuplet" not in grid.name
            ]
        measure_notes = notes_by_measure.get(measure_index, [])
        if not measure_notes:
            decisions.append(
                GridDecision(
                    measure_index,
                    candidates[0].name,
                    candidates[0].step,
                    0.0,
                    auto_tuplet=auto_detected_tuplets,
                )
            )
            continue

        # Choose the grid per (hand lane, beat group).  A measure-wide grid
        # forces one shared subdivision on every voice, but real piano music
        # constantly mixes them — e.g. the left hand holds dotted figures
        # while the right hand plays a quintuplet run.  Per-lane, per-beat
        # selection lets each hand keep its own true subdivision.
        buckets: dict[tuple[int, tuple[int, int]], list[QuantizedNote]] = defaultdict(list)
        for note in measure_notes:
            beat_index = _beat_group_index(note.onset - measure.start, measure)
            buckets[(beat_index, (note.track, note.channel))].append(note)

        bucket_grids: dict[tuple[int, tuple[int, int]], GridSpec] = {}
        for bucket_key, bucket_notes in buckets.items():
            bucket_start = measure.start + measure.meter.beat_group_boundaries[bucket_key[0]]
            bucket_candidates = [
                grid
                for grid in candidates
                if _fine_grid_has_attack_evidence(bucket_notes, grid, options)
            ] or candidates
            best = min(
                bucket_candidates,
                key=lambda grid: _grid_cost(
                    bucket_notes,
                    bucket_start,
                    grid,
                    options.style,
                    fidelity_first=len(bucket_notes) <= 3,
                    onsets_only=options.audio_transcription,
                ),
            )
            if auto_detected_tuplets:
                binary = [grid for grid in bucket_candidates if not grid.triplet]
                tuplets = [grid for grid in bucket_candidates if grid.triplet]
                if binary:
                    best_binary = min(
                        binary,
                        key=lambda grid: _grid_cost(
                            bucket_notes,
                            bucket_start,
                            grid,
                            options.style,
                            fidelity_first=len(bucket_notes) <= 3,
                            onsets_only=options.audio_transcription,
                        ),
                    )
                    best = best_binary
                    if tuplets:
                        # Compare the observed timing fit separately from the
                        # readability prior.  The complexity term should break
                        # close calls, not turn a clearly ternary 80/160-tick
                        # run into syncopated binary notes merely because a
                        # triplet symbol costs ink.
                        best_tuplet = min(
                            tuplets,
                            key=lambda grid: (
                                _grid_cost(
                                    bucket_notes,
                                    bucket_start,
                                    grid,
                                    options.style,
                                    fidelity_first=len(bucket_notes) <= 3,
                                    onsets_only=options.audio_transcription,
                                )
                                - grid.complexity,
                                _grid_cost(
                                    bucket_notes,
                                    bucket_start,
                                    grid,
                                    options.style,
                                    fidelity_first=len(bucket_notes) <= 3,
                                    onsets_only=options.audio_transcription,
                                ),
                            ),
                        )
                        tuplet_cost = _grid_cost(
                            bucket_notes,
                            bucket_start,
                            best_tuplet,
                            options.style,
                            fidelity_first=len(bucket_notes) <= 3,
                            onsets_only=options.audio_transcription,
                        )
                        binary_cost = _grid_cost(
                            bucket_notes,
                            bucket_start,
                            best_binary,
                            options.style,
                            fidelity_first=len(bucket_notes) <= 3,
                            onsets_only=options.audio_transcription,
                        )
                        tuplet_fit = max(0.0, tuplet_cost - best_tuplet.complexity)
                        binary_fit = max(0.0, binary_cost - best_binary.complexity)
                        distinct_attacks = len({note.onset for note in bucket_notes})
                        if (
                            distinct_attacks >= 3
                            and _ratio_evidence(
                                bucket_notes,
                                bucket_start,
                                best_tuplet.step,
                            )
                            >= 2
                            and tuplet_fit <= binary_fit * AUTO_TUPLET_FIT_RATIO
                        ):
                            best = best_tuplet
            elif best.triplet:
                # A genuine tuplet figure has at least three members (notes or
                # internal gaps) on the ratio grid.  With less evidence the
                # choice would only strand isolated members no bracket can
                # complete — and no importer can print.
                binary = [grid for grid in bucket_candidates if not grid.triplet]
                if binary:
                    best_binary = min(
                        binary,
                        key=lambda grid: _grid_cost(
                            bucket_notes,
                            bucket_start,
                            grid,
                            options.style,
                            fidelity_first=len(bucket_notes) <= 3,
                            onsets_only=options.audio_transcription,
                        ),
                    )
                    if (
                        best.triplet
                        and _ratio_evidence(
                            bucket_notes,
                            bucket_start,
                            best.step,
                        )
                        < 2
                    ):
                        best = best_binary
            bucket_grids[bucket_key] = best

        finest = min(
            (bucket_grids[key] for key in bucket_grids),
            key=lambda grid: grid.step,
            default=candidates[0],
        )
        uses_tuplet_grid = any(grid.triplet for grid in bucket_grids.values())
        decisions.append(
            GridDecision(
                measure_index=measure_index,
                name=finest.name,
                step=finest.step,
                score=0.0,
                triplet=uses_tuplet_grid,
                # Once audio has enabled the automatic ternary probe, every
                # ratio-looking group in the score is speculative — even a
                # nominally binary measure can acquire 80/160-tick fragments
                # during release cleanup.  Mark the whole decision timeline
                # so MusicXML hides groups without real attack support.
                auto_tuplet=auto_detected_tuplets,
            )
        )

        for (beat_index, _lane), bucket_notes in buckets.items():
            grid = bucket_grids[(beat_index, _lane)]
            bucket_start = measure.start + measure.meter.beat_group_boundaries[beat_index]
            for note in bucket_notes:
                snapped_onset = bucket_start + _nearest_multiple(
                    note.onset - bucket_start, grid.step
                )
                if measure_index == len(measures) - 1 and snapped_onset >= measure.end:
                    snapped_onset = max(measure.start, measure.end - grid.step)
                snapped_end = bucket_start + _nearest_multiple(
                    note.end - bucket_start, grid.step
                )
                if snapped_end <= snapped_onset:
                    snapped_end = snapped_onset + grid.step
                snapped_duration = snapped_end - snapped_onset
                staccato = _detect_staccato(note, snapped_duration, options)
                quantized.append(
                    replace(
                        note,
                        onset=max(0, snapped_onset),
                        duration=snapped_duration,
                        staccato=staccato,
                    )
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
        GridSpec("eighth_quintuplet", CANONICAL_DIVISIONS * 2 // 5, True, 0.24),
        GridSpec("sixteenth_triplet", CANONICAL_DIVISIONS // 6, True, 0.20),
        GridSpec("sixteenth_quintuplet", CANONICAL_DIVISIONS // 5, True, 0.26),
        GridSpec("thirty_second", CANONICAL_DIVISIONS // 8, False, 0.30),
        GridSpec("thirty_second_quintuplet", CANONICAL_DIVISIONS // 10, True, 0.38),
        GridSpec("thirty_second_triplet", CANONICAL_DIVISIONS // 12, True, 0.34),
        GridSpec("sixty_fourth", CANONICAL_DIVISIONS // 16, False, 0.45),
    ]

    if options.minimum_note == "eighth":
        allowed = {"eighth", "eighth_triplet", "eighth_quintuplet"}
    elif options.minimum_note == "sixteenth":
        allowed = {
            "eighth",
            "sixteenth",
            "eighth_triplet",
            "eighth_quintuplet",
            "sixteenth_triplet",
            "sixteenth_quintuplet",
        }
    elif options.minimum_note == "thirty_second":
        allowed = {spec.name for spec in all_specs if spec.name != "sixty_fourth"}
    elif options.style == "clean":
        # Clean mode still needs fine grids to be *available*: without them a
        # genuine fast run (cadenza, glissando-like flourish) collapses onto a
        # coarse grid and several distinct pitches snap onto one attack, which
        # the writer then prints as stacked chords.  The style factor keeps
        # fine grids expensive, so they win only when the false-chord merge
        # penalty proves the coarse grid is destroying real melody notes.
        allowed = {
            spec.name
            for spec in all_specs
            if spec.name not in {"thirty_second_triplet", "thirty_second_quintuplet"}
        }
    else:
        allowed = {spec.name for spec in all_specs}

    if not options.allow_triplets:
        triplet_names = {spec.name for spec in all_specs if spec.triplet}
        allowed = {name for name in allowed if name not in triplet_names}
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


def _fine_grid_has_attack_evidence(
    notes: list[QuantizedNote],
    grid: GridSpec,
    options: ConversionOptions,
) -> bool:
    """Require an actual fast run before audio uses 32nd/64th grids.

    Audio releases and isolated attacks routinely drift by 20--40 ticks.  A
    sparse beat bucket can therefore fit a 64th grid perfectly even though its
    musical pattern is only eighths or sixteenths.  Fine grids remain available
    for real runs, but auto mode now requires at least two consecutive rapid
    gaps inside the beat.  An explicit minimum-note choice and faithful MIDI
    conversion continue to honor the user's requested detail level.
    """

    if (
        not options.audio_transcription
        or options.minimum_note != "auto"
        or options.style == "faithful"
        or grid.step >= CANONICAL_DIVISIONS // 4
    ):
        return True

    onsets = sorted({note.onset for note in notes})
    required_onsets = 4 if grid.step <= CANONICAL_DIVISIONS // 16 else 3
    if len(onsets) < required_onsets:
        return False

    rapid_limit = min(110, round(grid.step * 1.5))
    rapid_gaps = sum(
        second - first <= rapid_limit for first, second in pairwise(onsets)
    )
    return rapid_gaps >= 2


def _grid_cost(
    notes: list[QuantizedNote],
    measure_start: int,
    grid: GridSpec,
    style: str,
    fidelity_first: bool = False,
    onsets_only: bool = False,
) -> float:
    onset_errors: list[float] = []
    release_errors: list[float] = []
    collapsed = 0
    tiny_values = 0
    snapped_onsets: list[tuple[tuple[int, int], int, int, int]] = []
    for note in notes:
        relative_onset = note.onset - measure_start
        snapped_onset = measure_start + _nearest_multiple(relative_onset, grid.step)
        snapped_end = measure_start + _nearest_multiple(note.end - measure_start, grid.step)
        onset_errors.append(
            abs(snapped_onset - note.onset) / (CANONICAL_DIVISIONS / 4)
        )
        release_errors.append(
            abs(snapped_end - note.end) / (CANONICAL_DIVISIONS / 4)
        )
        if snapped_end <= snapped_onset:
            collapsed += 1
        if snapped_end - snapped_onset <= grid.step:
            tiny_values += 1
        snapped_onsets.append(((note.track, note.channel), note.pitch, note.onset, snapped_onset))

    timing_errors = onset_errors if onsets_only else [*onset_errors, *release_errors]
    timing_error = fmean(timing_errors) if timing_errors else 0.0
    merge_penalty = _false_chord_merges(snapped_onsets) * 1.0 / len(notes)
    if fidelity_first:
        # Sparse buckets (a sustained chord, a lone entrance) are readable on
        # any grid; complexity pricing must not push them onto a coarse grid
        # that mistimes the attack.  Compare onset fidelity only — releases
        # get normalized by the grid anyway, and counting them would let an
        # ultra-fine binary grid outrank a genuinely exact triplet grid.
        ratio_penalty = grid.complexity if grid.triplet else 0.0
        return (
            (fmean(onset_errors) if onset_errors else 0.0)
            + merge_penalty
            + ratio_penalty
        )
    collapse_penalty = 0.0 if onsets_only else collapsed * 1.25 / len(notes)
    tiny_factor = {"clean": 0.05, "balanced": 0.02, "faithful": 0.0}[style]
    tiny_penalty = 0.0 if onsets_only else tiny_values * tiny_factor / len(notes)
    return timing_error + grid.complexity + collapse_penalty + tiny_penalty + merge_penalty


def _false_chord_merges(
    snapped_onsets: list[tuple[tuple[int, int], int, int, int]],
) -> int:
    """Count distinct pitches forced onto one attack by the grid.

    Notes played at genuinely different times but snapped to the same grid
    point surface downstream as a stacked chord, erasing fast runs.  True
    chord members share the same raw onset, so they merge identically on every
    grid and never influence the choice.
    """

    merges = 0
    by_cell: dict[tuple[tuple[int, int], int], list[tuple[int, int]]] = defaultdict(list)
    for lane, pitch, raw_onset, snapped_onset in snapped_onsets:
        by_cell[(lane, snapped_onset)].append((pitch, raw_onset))
    for members in by_cell.values():
        if len(members) < 2:
            continue
        pitches = {pitch for pitch, _ in members}
        raw_onsets = {raw_onset for _, raw_onset in members}
        if len(pitches) > 1 and len(raw_onsets) > 1:
            merges += sum(1 for _, raw_onset in members if raw_onset != min(raw_onsets))
    return merges


def _grid_timing_error(
    notes: list[QuantizedNote],
    measure_start: int,
    step: int,
    *,
    onsets_only: bool = False,
) -> float:
    errors: list[float] = []
    for note in notes:
        snapped_onset = measure_start + _nearest_multiple(note.onset - measure_start, step)
        snapped_end = measure_start + _nearest_multiple(note.end - measure_start, step)
        errors.append(abs(snapped_onset - note.onset) / (CANONICAL_DIVISIONS / 4))
        if not onsets_only:
            errors.append(abs(snapped_end - note.end) / (CANONICAL_DIVISIONS / 4))
    return fmean(errors) if errors else 0.0


def _nearest_multiple(value: int, step: int) -> int:
    return int((value + step / 2) // step) * step


def _ratio_evidence(notes: list[QuantizedNote], bucket_start: int, step: int) -> int:
    """Count distinct attack evidence that only the ratio grid can express.

    A genuine tuplet figure places multiple attacks at positions the binary
    grid cannot hold (or separates them with non-binary gaps).  A lone
    sustained chord snapped onto a ratio grid produces at most one such
    position, so a threshold of two keeps genuine figures and rejects
    accidents.
    """

    onsets: set[int] = set()
    durations: list[int] = []
    for note in notes:
        snapped_onset = bucket_start + _nearest_multiple(note.onset - bucket_start, step)
        snapped_end = bucket_start + _nearest_multiple(note.end - bucket_start, step)
        if snapped_end <= snapped_onset:
            snapped_end = snapped_onset + step
        onsets.add(snapped_onset - bucket_start)
        durations.append(snapped_end - snapped_onset)

    evidence = sum(1 for onset in onsets if onset % 30)
    evidence += sum(1 for duration in durations if duration % 30)
    ordered = sorted(onsets)
    for first, second in pairwise(ordered):
        if (second - first) % 30:
            evidence += 1
    return evidence


def _beat_group_index(relative_onset: int, measure: MeasureSpan) -> int:
    boundaries = measure.meter.beat_group_boundaries
    index = 0
    for group_index, boundary in enumerate(boundaries[1:]):
        if relative_onset < boundary:
            index = group_index
            break
    else:
        index = len(boundaries) - 2
    return max(0, min(index, len(boundaries) - 2))


def _detect_staccato(
    note: QuantizedNote,
    snapped_duration: int,
    options: ConversionOptions,
) -> bool:
    """Mark notes whose played gate time is far shorter than the written value.

    Notation programs render staccato playback at roughly half the notated
    length, so a raw gate at or below 60% of the snapped written value is a
    deliberate staccato rather than a genuinely shorter note value.  Genuine
    fast-run members (32nd/64th grid values) keep a gate close to the written
    value and stay untouched, as does audio transcription output, where gate
    times are model noise rather than articulation evidence.
    """

    if options.audio_transcription:
        return False
    if snapped_duration < CANONICAL_DIVISIONS // 4:
        return False
    return note.duration * 5 <= snapped_duration * 3


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
    merged = _merge_subgrid_reattacks(sorted(selected.values(), key=lambda note: (note.onset, note.pitch, note.source_id)))
    return merged


def _merge_subgrid_reattacks(notes: list[QuantizedNote]) -> list[QuantizedNote]:
    """Merge same-pitch attacks closer together than the finest printable grid.

    Snapped onsets from different beat-group grids can leave the same key
    "re-struck" 10–20 ticks after its previous attack — a MIDI event artifact
    no staff can print (the finest grid is a 64th = 30 ticks).  Keeping both
    would force a sub-grid truncation downstream and an unwritable duration.
    The stronger of the two attacks survives.
    """

    by_pitch: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_pitch[note.pitch].append(note)

    minimum_onset_gap = CANONICAL_DIVISIONS // 16
    kept: list[QuantizedNote] = []
    for pitch_notes in by_pitch.values():
        survivor: QuantizedNote | None = None
        for note in pitch_notes:  # already onset-sorted
            if survivor is not None and note.onset - survivor.onset < minimum_onset_gap:
                if (note.duration, note.velocity) > (survivor.duration, survivor.velocity):
                    survivor = note
                continue
            if survivor is not None:
                kept.append(survivor)
            survivor = note
        if survivor is not None:
            kept.append(survivor)
    return sorted(kept, key=lambda note: (note.onset, note.pitch, note.source_id))


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
