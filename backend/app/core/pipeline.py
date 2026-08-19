from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from pathlib import Path

from .clefs import plan_clefs
from .duration_simplifier import (
    absorb_articulation_gaps,
    normalize_short_gate_slots,
    simplify_polyphonic_durations,
)
from .dynamics import plan_dynamics
from .hand_splitter import (
    assign_hands,
    mark_unredistributable_chords_for_arpeggiation,
)
from .key_detection import estimate_key, estimate_key_timeline, key_from_midi_name
from .meter_map import build_measure_map, measure_index_at, reframe_audio_pickup
from .midi_parser import parse_midi
from .models import (
    CANONICAL_DIVISIONS,
    Hand,
    KeyChange,
    KeyEstimate,
    KeySignatureEvent,
    MeasureSpan,
    Meter,
    PedalEvent,
    QuantizedNote,
    ScoreModel,
    Staff,
    TempoChange,
    TempoText,
)
from .musicxml import musicxml_readability_metrics, score_to_musicxml
from .options import ConversionOptions
from .ornaments import collapse_tremolos, collapse_trills, convert_grace_notes
from .quality import evaluate_notation_quality
from .quantizer import quantize_midi
from .spelling import apply_pitch_spelling
from .staff_assigner import assign_staves, repair_staves_for_planned_clefs
from .voices import assign_voices, resolve_voice_overlaps


def convert_midi(
    data: bytes,
    filename: str,
    options: ConversionOptions | None = None,
):
    musicxml, analysis, warnings, _ = convert_midi_with_score(
        data,
        filename,
        options,
    )
    return musicxml, analysis, warnings


def convert_midi_with_score(
    data: bytes,
    filename: str,
    options: ConversionOptions | None = None,
) -> tuple[str, dict[str, object], list[str], ScoreModel]:
    """Convert MIDI and retain the final semantic score for diagnostics."""

    started = time.perf_counter()
    options = options or ConversionOptions()
    parsed = parse_midi(data)
    warnings = list(parsed.warnings)

    override_meter = None
    if options.time_numerator is not None and options.time_denominator is not None:
        override_meter = Meter(options.time_numerator, options.time_denominator)
    measures, timeline_shift, meter_warnings = build_measure_map(parsed, override_meter)
    warnings.extend(meter_warnings)
    meter = measures[0].meter

    notes, grid_decisions, shift, quantizer_warnings = quantize_midi(
        parsed,
        measures,
        options,
        timeline_shift=timeline_shift,
    )
    warnings.extend(quantizer_warnings)
    quantized_note_count = len(notes)
    if options.audio_transcription and notes:
        measures, notes, pickup_shift = reframe_audio_pickup(measures, notes)
        if pickup_shift:
            shift += pickup_shift
            warnings.append("检测到弱起进入，开头空拍已重组为弱起小节（不完全小节）")
    scale = CANONICAL_DIVISIONS / parsed.ticks_per_beat
    # Pedal is musical timing evidence even when the user chooses not to print
    # pedal lines. Keep it available for hand continuity, written-release
    # inference, and playability analysis; only suppress it in the exported
    # score model.
    analysis_pedals = [
        PedalEvent(round(event.tick * scale) - shift, event.channel, event.down)
        for event in parsed.pedals
        if round(event.tick * scale) - shift >= 0
    ]
    engraved_pedals = analysis_pedals if options.include_pedal else []
    notes, hand_analysis, hand_warnings = assign_hands(
        notes,
        options,
        parsed.track_names,
        analysis_pedals,
    )
    warnings.extend(hand_warnings)
    notes, inferred_arpeggiated_chords = (
        mark_unredistributable_chords_for_arpeggiation(notes)
    )
    hand_analysis["inferred_arpeggiated_chords"] = inferred_arpeggiated_chords
    if inferred_arpeggiated_chords:
        warnings.append(
            f"有 {inferred_arpeggiated_chords} 个单手同起和弦无法在大十度和五指上限内重新分配，"
            "已明确标记为滚奏琶音"
        )
    track_staff_hints = None
    if hand_analysis.get("method") == "tracks":
        track_staff_hints = {
            int(track): Staff.LEFT if label == "left" else Staff.RIGHT
            for track, label in dict(hand_analysis.get("track_map", {})).items()
        }
    # Score-export tracks are strong notation-staff evidence.  Preserve them
    # and let the clef planner choose treble or bass independently on each staff
    # instead of collapsing both hands into the same staff by pitch alone.
    lock_hands_to_staves = options.audio_transcription and track_staff_hints is None
    notes, staff_analysis, staff_warnings = assign_staves(
        notes,
        track_staff_hints,
        lock_hands_to_staves=lock_hands_to_staves,
    )
    warnings.extend(staff_warnings)
    clef_aware_repairs = 0
    if not lock_hands_to_staves:
        for _ in range(3):
            preliminary_clefs, _ = plan_clefs(
                notes,
                measures,
                responsive=options.audio_transcription,
            )
            notes, repaired = repair_staves_for_planned_clefs(
                notes,
                measures,
                preliminary_clefs,
            )
            clef_aware_repairs += repaired
            if not repaired:
                break
    staff_analysis["clef_aware_repairs"] = clef_aware_repairs
    if clef_aware_repairs:
        warnings.append(
            f"依据实际动态谱号，将 {clef_aware_repairs} 个仍会产生极端加线的音移到另一谱表"
        )
    physical_notes = notes
    trill_count = 0
    trill_absorbed = 0
    if options.audio_transcription:
        notes, trill_count, trill_absorbed = collapse_trills(notes)
        if trill_count:
            warnings.append(
                f"检测到 {trill_count} 处快速二度交替，已按颤音记号书写"
                f"（合并 {trill_absorbed} 个重复攻击音，总时值不变）"
            )
    tempo_bpm = parsed.tempos[0].bpm
    tempo_changes = _tempo_timeline(parsed, measures, scale=scale, timeline_shift=shift)
    tempo_ramps = _tempo_ramps(tempo_changes)
    tempo_texts = [TempoText(text_tick, kind) for text_tick, _start, _end, kind in tempo_ramps]
    tremolo_count = 0
    tremolo_absorbed = 0
    if not options.audio_transcription:
        # A ramp's free region reaches past its last event up to the next tempo
        # change: the easing tail of an accel./rit. is still cadenza time.
        event_ticks = [change.tick for change in tempo_changes]
        free_regions = []
        for _text_tick, ramp_start, ramp_end, _kind in tempo_ramps:
            following = [tick for tick in event_ticks if tick > ramp_end]
            region_end = min(following) if following else ramp_end + 1
            free_regions.append((ramp_start, region_end))
        notes, tremolo_count, tremolo_absorbed = collapse_tremolos(
            notes,
            free_regions,
            measures,
            event_ticks,
            {decision.measure_index for decision in grid_decisions if decision.step <= 30},
        )
        if tremolo_count:
            warnings.append(
                f"检测到 {tremolo_count} 处自由速度段落中的快速双音交替，已按震音记号书写（总时值不变）"
            )
    notes, duration_analysis, duration_warnings = simplify_polyphonic_durations(
        notes,
        max_voices=options.max_voices_per_staff,
        style=options.style,
        pedals=analysis_pedals,
        measures=measures,
        transcription_mode=options.audio_transcription,
        grid_decisions=grid_decisions,
    )
    warnings.extend(duration_warnings)
    notes, voice_counts, voice_warnings = assign_voices(notes, options.max_voices_per_staff)
    warnings.extend(voice_warnings)

    short_gate_normalized = 0
    if options.style != "faithful":
        notes, short_gate_normalized = normalize_short_gate_slots(notes, measures)
        if options.audio_transcription:
            notes, gaps_absorbed = absorb_articulation_gaps(notes)
            if gaps_absorbed:
                warnings.append(
                    f"已将 {gaps_absorbed} 处演奏缝隙并入前音时值（不再产生短休止符）"
                )

    grace_count = 0
    if options.audio_transcription and options.style != "faithful":
        notes, grace_count = convert_grace_notes(notes)
        if grace_count:
            warnings.append(
                f"将 {grace_count} 个拍前碎音按倚音记谱（时值已归还相邻音符，未删除任何音头）"
            )

    notes, overlap_clipped, overlap_dropped = resolve_voice_overlaps(notes)
    if overlap_clipped or overlap_dropped:
        warnings.append(
            f"为消除同声部时间重叠，调整了 {overlap_clipped} 个音的书写时值"
            + (f"，裁掉 {overlap_dropped} 个无法书写的极短音" if overlap_dropped else "")
        )

    key, key_changes, key_warnings = _key_timeline(
        parsed.key_signatures,
        notes,
        measures,
        scale=scale,
        timeline_shift=shift,
        infer_key=options.infer_key,
    )
    warnings.extend(key_warnings)
    if key.confidence < 0.18:
        warnings.append("调性估计置信度较低，可在后续编辑器中手动调整调号")

    notes, spelling_analysis, spelling_warnings = apply_pitch_spelling(
        notes,
        key,
        key_changes,
        measures,
    )
    warnings.extend(spelling_warnings)
    clef_changes, clef_analysis = plan_clefs(
        notes,
        measures,
        responsive=options.audio_transcription,
    )
    dynamics = plan_dynamics(notes, measures) if options.include_dynamics else []
    quality, quality_warnings = evaluate_notation_quality(
        notes,
        expected_note_count=quantized_note_count,
        tempo_bpm=parsed.tempos[0].bpm,
        pedals=analysis_pedals,
        playability_notes=physical_notes,
        clef_changes=clef_changes,
        measures=measures,
        intentional_reductions=trill_absorbed + tremolo_absorbed + overlap_dropped,
    )
    warnings.extend(quality_warnings)
    staff_analysis["clefs"] = clef_analysis
    staff_analysis["ledger_pressure_notes"] = quality["ledger_pressure_notes"]

    title = _clean_title(options.title or _title_from_midi(parsed.track_names, filename))

    measure_count = len(measures)
    score = ScoreModel(
        title=title,
        notes=notes,
        meter=meter,
        key=key,
        tempo_bpm=tempo_bpm,
        pedals=engraved_pedals,
        grid_decisions=grid_decisions,
        measure_count=measure_count,
        engraving_style=options.engraving_style,
        measures=measures,
        clef_changes=clef_changes,
        key_changes=key_changes,
        tempo_changes=tempo_changes,
        tempo_texts=tempo_texts,
        dynamics=dynamics,
        warnings=warnings,
    )
    musicxml = score_to_musicxml(score)
    notation_analysis = musicxml_readability_metrics(musicxml)

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    hand_counts = Counter(note.hand for note in notes)
    staff_counts = Counter(note.staff for note in notes)
    grid_counts = Counter(decision.name for decision in grid_decisions)
    duration_quarters = max(note.end for note in notes) / CANONICAL_DIVISIONS
    analysis = {
        "title": title,
        "note_count": len(notes),
        "measure_count": measure_count,
        "duration_quarters": round(duration_quarters, 2),
        "meter": _meter_summary(measures),
        "time_signatures": _time_signature_summary(measures),
        "tempo_bpm": round(tempo_bpm, 2),
        "tempo_changes": [
            {"measure": measure_index_at(measures, change.tick) + 1, "bpm": round(change.bpm, 2)}
            for change in tempo_changes
        ],
        "key": {
            "tonic_pitch_class": key.tonic_pc,
            "mode": key.mode,
            "fifths": key.fifths,
            "confidence": key.confidence,
        },
        "key_signatures": [
            {
                "measure": change.measure_index + 1,
                "tonic_pitch_class": change.key.tonic_pc,
                "mode": change.key.mode,
                "fifths": change.key.fifths,
                "confidence": change.key.confidence,
            }
            for change in key_changes
        ],
        "hands": {
            "right": hand_counts[Hand.RIGHT],
            "left": hand_counts[Hand.LEFT],
            **hand_analysis,
        },
        "staves": {
            "treble": staff_counts[Staff.RIGHT],
            "bass": staff_counts[Staff.LEFT],
            **staff_analysis,
        },
        "voices": voice_counts,
        "duration_simplification": duration_analysis,
        "ornaments": {
            "trills": trill_count,
            "trill_absorbed_attacks": trill_absorbed,
            "grace_notes": grace_count,
            "tremolos": tremolo_count,
            "tremolo_absorbed_attacks": tremolo_absorbed,
        },
        "spelling": spelling_analysis,
        "notation": notation_analysis,
        "dynamics": {
            "marks": len(dynamics),
            "sequence": [
                {"measure": mark.measure_index + 1, "mark": mark.mark}
                for mark in dynamics
            ],
        },
        "engraving_style": options.engraving_style,
        "quality": quality,
        "semantic_engine": {
            "voice_separation": "Partitura Chew-Wu",
            "pitch_spelling": "Partitura PS13",
        },
        "quantization_grids": dict(sorted(grid_counts.items())),
        "complexity_score": _complexity_score(score),
        "processing_ms": elapsed_ms,
        "source": {
            "ticks_per_beat": parsed.ticks_per_beat,
            "tracks_with_notes": len({note.track for note in parsed.notes}),
            "tempo_events": len(parsed.tempos),
            "time_signature_events": len(parsed.time_signatures),
            "pedal_events": len(parsed.pedals),
            "piano_note_on_events": parsed.piano_note_on_count,
            "parsed_piano_notes": len(parsed.notes),
            "quantized_physical_notes": quantized_note_count,
            "merged_coincident_events": max(0, len(parsed.notes) - quantized_note_count),
            "excluded_non_piano_notes": parsed.excluded_non_piano_note_count,
            "excluded_percussion_notes": parsed.excluded_percussion_note_count,
            "programs": parsed.programs,
        },
    }
    return musicxml, analysis, list(dict.fromkeys(warnings)), score


def _tempo_ramps(changes: list[TempoChange]) -> list[tuple[int, int, int, str]]:
    """Find sustained rit./accel. ramps in the tempo map.

    Returns (text_tick, region_start, region_end, kind) tuples.  A ramp is a
    monotone run of at least three tempo events spanning two beats or more
    with a total change of at least 8%.  When the run departs from a long
    plateau, the text anchor moves to the first event that actually moves —
    the mark belongs where the motion begins, not at the plateau's edge.
    """

    ramps: list[tuple[int, int, int, str]] = []
    index = 0
    while index < len(changes) - 2:
        direction = 0
        stop = index + 1
        while stop < len(changes):
            delta = changes[stop].bpm - changes[stop - 1].bpm
            step = (delta > 0) - (delta < 0)
            if step == 0 or (direction and step != direction):
                break
            direction = step
            stop += 1
        run = changes[index:stop]
        if len(run) >= 3:
            total = run[-1].bpm - run[0].bpm
            span = run[-1].tick - run[0].tick
            if span >= CANONICAL_DIVISIONS * 2 and abs(total) >= run[0].bpm * 0.08:
                kind = "accel." if total > 0 else "rit."
                plateau = run[1].tick - run[0].tick >= CANONICAL_DIVISIONS * 8
                text_tick = run[1].tick if plateau else run[0].tick
                ramps.append((text_tick, run[0].tick, run[-1].tick, kind))
        index = max(stop - 1, index + 1)
    return ramps


def _tempo_timeline(
    parsed,
    measures: list[MeasureSpan],
    *,
    scale: float,
    timeline_shift: int,
) -> list[TempoChange]:
    """Carry the MIDI tempo map onto the canonical score timeline.

    Events are scaled, shifted with the pickup reframe, clamped into the
    score, and deduplicated: consecutive events at (nearly) the same bpm or
    the same tick carry no new information.
    """

    if not parsed.tempos:
        return []
    score_end = measures[-1].end
    changes: list[TempoChange] = []
    for event in parsed.tempos:
        tick = max(0, round(event.tick * scale) - timeline_shift)
        if tick > score_end:
            continue
        bpm = event.bpm
        if changes and changes[-1].tick == tick:
            changes[-1] = TempoChange(tick, bpm)
            continue
        if changes and abs(changes[-1].bpm - bpm) < 0.5:
            continue
        changes.append(TempoChange(tick, bpm))
    if not changes or changes[0].tick != 0:
        changes.insert(0, TempoChange(0, parsed.tempos[0].bpm))
    return changes


def _key_timeline(
    events: list[KeySignatureEvent],
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
    *,
    scale: float,
    timeline_shift: int,
    infer_key: bool = True,
) -> tuple[KeyEstimate, list[KeyChange], list[str]]:
    """Preserve explicit MIDI key changes and only infer when they are absent."""

    warnings: list[str] = []
    by_measure: dict[int, KeyEstimate] = {}
    score_end = measures[-1].end
    for event in events:
        parsed_key = key_from_midi_name(event.key)
        if parsed_key is None:
            warnings.append(f"MIDI 调号 {event.key!r} 无法解析，已忽略该事件")
            continue
        tick = round(event.tick * scale) - timeline_shift
        if tick >= score_end:
            continue
        tick = max(0, tick)
        measure_index = measure_index_at(measures, tick)
        measure = measures[measure_index]
        if tick != measure.start:
            nearest = min(
                range(len(measures)),
                key=lambda index: abs(measures[index].start - tick),
            )
            if abs(measures[nearest].start - tick) <= CANONICAL_DIVISIONS // 4:
                measure_index = nearest
            else:
                warnings.append(
                    f"调号变化位于第 {measure_index + 1} 小节内部，已对齐到该小节起点"
                )
        by_measure[measure_index] = parsed_key

    if not by_measure:
        if not infer_key:
            default_key = KeyEstimate(0, "major", 0, 1.0)
            return default_key, [KeyChange(0, default_key)], warnings
        timeline = estimate_key_timeline(notes, measures)
        if len(timeline) > 1:
            warnings.append(
                f"检测到 {len(timeline) - 1} 次转调，已按小节标注调号变化"
            )
        return timeline[0].key, timeline, warnings

    if 0 not in by_measure:
        opening_notes = [
            note for note in notes if note.onset < measures[min(by_measure)].start
        ]
        by_measure[0] = estimate_key(opening_notes or notes)

    changes: list[KeyChange] = []
    previous: tuple[int, str] | None = None
    for measure_index, key in sorted(by_measure.items()):
        signature = (key.fifths, key.mode)
        if signature == previous:
            continue
        changes.append(KeyChange(measure_index, key))
        previous = signature
    return changes[0].key, changes, warnings


def _title_from_midi(track_names: dict[int, str], filename: str) -> str:
    useful_names = [
        name
        for name in track_names.values()
        if _is_useful_track_title(name)
    ]
    if useful_names:
        return useful_names[0]
    stem = Path(filename).stem
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    stem = re.sub(r"_+", " ", stem)
    stem = re.sub(r"\s*-\s*", " - ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem or "Untitled Piano Score"


def _is_useful_track_title(name: str) -> bool:
    normalized = re.sub(r"[\x00-\x1f]+", " ", name).strip().lower()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized or re.fullmatch(r"[<\[].*(?:>|])", normalized):
        return False
    if any(marker in normalized for marker in ("�", "□", "\ufffd")):
        return False
    readable = sum(
        character.isspace()
        or unicodedata.category(character)[0] in {"L", "N"}
        or character in "-–—_:;,.!?()[]'\""
        for character in normalized
    )
    if readable / len(normalized) < 0.75:
        return False
    if re.fullmatch(r"(?:track|piano|instrument|staff)(?:\s+\d+)?", normalized):
        return False
    if normalized.startswith("piano transcription"):
        return False
    return normalized not in {
        "title",
        "untitled",
        "acoustic grand piano",
        "sequence name",
        "tempo and meter",
        "tempo map",
    }


def _clean_title(title: str) -> str:
    title = re.sub(r"[\x00-\x1f]+", " ", title).strip()
    return title[:120] or "Untitled Piano Score"


def _complexity_score(score: ScoreModel) -> int:
    if not score.notes:
        return 0
    short = sum(note.duration <= CANONICAL_DIVISIONS // 4 for note in score.notes)
    very_short = sum(note.duration <= CANONICAL_DIVISIONS // 8 for note in score.notes)
    secondary = sum(note.voice > 1 for note in score.notes)
    triplet_measures = sum(decision.triplet for decision in score.grid_decisions)
    raw = (
        short / len(score.notes) * 34
        + very_short / len(score.notes) * 24
        + secondary / len(score.notes) * 24
        + triplet_measures / max(1, score.measure_count) * 18
    )
    return max(0, min(100, round(raw)))


def _time_signature_summary(measures) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    previous: Meter | None = None
    for measure in measures:
        if measure.meter != previous:
            changes.append(
                {
                    "measure": measure.index + 1,
                    "signature": f"{measure.meter.numerator}/{measure.meter.denominator}",
                    "implicit": measure.implicit,
                }
            )
            previous = measure.meter
    return changes


def _meter_summary(measures) -> str:
    signatures = [item["signature"] for item in _time_signature_summary(measures)]
    return " → ".join(dict.fromkeys(signatures))
