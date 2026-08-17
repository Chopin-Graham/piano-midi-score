from __future__ import annotations

from collections import defaultdict
from statistics import median

from .clefs import clef_kind_at
from .meter_map import measure_index_at
from .models import (
    CANONICAL_DIVISIONS,
    ClefChange,
    Hand,
    MeasureSpan,
    PedalEvent,
    QuantizedNote,
    Staff,
)
from .piano_rules import (
    COMFORTABLE_HAND_SPAN_MM,
    COMFORTABLE_HAND_SPAN_SEMITONES,
    EXTREME_HAND_SPAN_MM,
    EXTREME_HAND_SPAN_SEMITONES,
    MAX_HAND_SPAN_MM,
    MAX_HAND_SPAN_SEMITONES,
    MAX_SIMULTANEOUS_KEYS_PER_HAND,
    PIANO_HIGHEST_MIDI,
    PIANO_LOWEST_MIDI,
    ChordReach,
    PedalCoverage,
    chord_fingering_feasible,
    chord_fingering_natural,
    chord_reach,
)

FAST_LEAP_SECONDS = 0.35
FAST_LEAP_SEMITONES = 16


def evaluate_notation_quality(
    notes: list[QuantizedNote],
    *,
    expected_note_count: int,
    tempo_bpm: float = 120.0,
    pedals: list[PedalEvent] | None = None,
    playability_notes: list[QuantizedNote] | None = None,
    clef_changes: list[ClefChange] | None = None,
    measures: list[MeasureSpan] | None = None,
) -> tuple[dict[str, object], list[str]]:
    """Run deterministic notation and physical-playability checks."""

    warnings: list[str] = []
    overlap_count = _count_voice_overlaps(notes)
    mixed_hand_groups = _count_mixed_hand_voice_groups(notes)
    clef_kinds = [
        _effective_clef(note, clef_changes or [], measures or []) for note in notes
    ]
    treble_extremes = sum(
        kind == "treble" and note.pitch < 52
        for note, kind in zip(notes, clef_kinds, strict=True)
    )
    bass_extremes = sum(
        kind == "bass" and note.pitch > 72
        for note, kind in zip(notes, clef_kinds, strict=True)
    )
    ledger_pressure = sum(
        (kind == "treble" and note.pitch < 60)
        or (kind == "bass" and note.pitch > 60)
        for note, kind in zip(notes, clef_kinds, strict=True)
    )
    playability = _analyze_playability(
        playability_notes if playability_notes is not None else notes,
        tempo_bpm,
        pedals or [],
    )
    voices = {
        staff.name.lower(): max(
            (note.voice for note in notes if note.staff == staff),
            default=1,
        )
        for staff in (Staff.RIGHT, Staff.LEFT)
    }
    preserved = len(notes) == expected_note_count

    if not preserved:
        warnings.append(
            f"声部整理前后音符数不一致（{expected_note_count} → {len(notes)}），请检查输入"
        )
    if overlap_count:
        warnings.append(f"检测到 {overlap_count} 处同声部时间重叠，已阻止静默丢音")
    if mixed_hand_groups:
        warnings.append(
            f"检测到 {mixed_hand_groups} 个同一谱表声部混合左右手的和弦组，需重新分声部"
        )
    if treble_extremes or bass_extremes:
        warnings.append(
            f"检测到疑似跨错谱表的极端音：高音谱表 {treble_extremes} 个，低音谱表 {bass_extremes} 个"
        )
    if playability["out_of_piano_range_notes"]:
        warnings.append(
            f"检测到 {playability['out_of_piano_range_notes']} 个音超出标准 88 键钢琴 A0–C8 音域"
        )
    if playability["oversized_chords"]:
        warnings.append(
            f"检测到 {playability['oversized_chords']} 个单手起音超过大十度上限"
            f"（{MAX_HAND_SPAN_SEMITONES} 半音；键盘实际距离另行报告，参考上限约 {MAX_HAND_SPAN_MM:.0f} mm）"
        )
    if playability["arpeggiated_wide_chords"]:
        warnings.append(
            f"有 {playability['arpeggiated_wide_chords']} 个无法重新分手的超宽和弦已写成滚奏琶音，"
            "不再要求单手同时按下"
        )
    if playability["held_oversized_spans"]:
        warnings.append(
            f"检测到 {playability['held_oversized_spans']} 处单手在保持旧音时又触键，实际同时按键跨度超过大十度"
        )
    if playability["too_many_notes_chords"]:
        warnings.append(
            f"检测到 {playability['too_many_notes_chords']} 个单手起音需要同时按下超过 5 个琴键"
        )
    if playability["awkward_chord_shapes"]:
        warnings.append(
            f"有 {playability['awkward_chord_shapes']} 个和弦超出中等手型的自然指距，"
            "但仍在较大手型可达范围内，已标记为高难手型"
        )
    if playability["unplayable_chord_shapes"]:
        warnings.append(
            f"检测到 {playability['unplayable_chord_shapes']} 个和弦虽然未超过五音或大十度，"
            "但相邻手指仍无法自然成形，需换手、滚奏或人工改编"
        )
    if playability["held_too_many_keys"]:
        warnings.append(
            f"检测到 {playability['held_too_many_keys']} 处单手因延音叠加需要同时保持超过 5 个琴键"
        )
    if playability["held_awkward_shapes"]:
        warnings.append(
            f"有 {playability['held_awkward_shapes']} 处单手保持旧音后形成高难五指手型"
        )
    if playability["held_unplayable_shapes"]:
        warnings.append(
            f"检测到 {playability['held_unplayable_shapes']} 处单手保持旧音后无法形成可达五指手型"
        )
    if playability["pedal_supported_wide_sustains"]:
        warnings.append(
            f"有 {playability['pedal_supported_wide_sustains']} 处超宽持续声部由延音踏板覆盖；"
            "演奏时应在踏板保持共鸣后及时释放手指"
        )
    if playability["over_ten_total_keys"]:
        warnings.append(
            f"检测到 {playability['over_ten_total_keys']} 个起音需要两手合计同时按下超过 10 个琴键"
        )
    if playability["extended_chords"]:
        warnings.append(
            f"有 {playability['extended_chords']} 个单手和弦超过舒适八度、但尚未达到十度极限，"
            "已标记为伸展演奏"
        )
    if playability["extreme_stretch_chords"]:
        warnings.append(
            f"有 {playability['extreme_stretch_chords']} 个单手和弦达到小十度或大十度，"
            "属于极限伸展；许多演奏者需要滚奏或重新分手，程序不会擅自改变同时起音"
        )
    if playability["fast_large_leaps"]:
        warnings.append(
            f"检测到 {playability['fast_large_leaps']} 个 0.35 秒内超过大十度的快速手位移动"
        )
    if playability["hand_crossings"]:
        warnings.append(
            f"检测到 {playability['hand_crossings']} 处左右手音域交叉；可能是有意跨手，也可能需要重新分手"
        )

    hard_failures = (
        int(not preserved)
        + overlap_count
        + mixed_hand_groups
        + treble_extremes
        + bass_extremes
        + int(playability["out_of_piano_range_notes"])
        + int(playability["oversized_chords"])
        + int(playability["held_oversized_spans"])
        + int(playability["too_many_notes_chords"])
        + int(playability["unplayable_chord_shapes"])
        + int(playability["held_too_many_keys"])
        + int(playability["held_unplayable_shapes"])
        + int(playability["over_ten_total_keys"])
    )
    demanding = (
        int(playability["stretched_chords"])
        + int(playability["awkward_chord_shapes"])
        + int(playability["held_awkward_shapes"])
        + int(playability["fast_large_leaps"])
        + int(playability["hand_crossings"])
    )
    if hard_failures:
        status = "needs_review"
    elif demanding:
        status = "playable_but_demanding"
    else:
        status = "excellent"
    return (
        {
            "status": status,
            "note_count_preserved": preserved,
            "voice_overlap_count": overlap_count,
            "mixed_hand_voice_groups": mixed_hand_groups,
            "extreme_staff_misplacements": {
                # Kept for API compatibility: RIGHT/LEFT are the historic enum
                # names for treble/bass staff, not physical hands.
                "right_below_e3": treble_extremes,
                "left_above_c5": bass_extremes,
            },
            "ledger_pressure_notes": ledger_pressure,
            "wide_onset_spans": playability["oversized_chords"],
            "playability": playability,
            "voices": voices,
        },
        warnings,
    )


def _count_voice_overlaps(notes: list[QuantizedNote]) -> int:
    events: dict[tuple[Staff, int], dict[tuple[int, int], list[QuantizedNote]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for note in notes:
        if note.staff is None or note.grace:
            # Grace notes occupy no written time; they legitimately share the
            # span their duration was returned to.
            continue
        events[(note.staff, note.voice)][(note.onset, note.duration)].append(note)

    overlaps = 0
    for voice_events in events.values():
        previous_end = 0
        for onset, duration in sorted(voice_events):
            if onset < previous_end:
                overlaps += 1
            previous_end = max(previous_end, onset + duration)
    return overlaps


def _count_mixed_hand_voice_groups(notes: list[QuantizedNote]) -> int:
    groups: dict[tuple[Staff, int, int, int], set[Hand]] = defaultdict(set)
    for note in notes:
        if note.staff is None or note.hand is None:
            continue
        groups[(note.staff, note.voice, note.onset, note.duration)].add(note.hand)
    return sum(len(hands) > 1 for hands in groups.values())


def _effective_clef(
    note: QuantizedNote,
    changes: list[ClefChange],
    measures: list[MeasureSpan],
) -> str:
    staff = note.staff or Staff.RIGHT
    if not changes or not measures:
        return "treble" if staff == Staff.RIGHT else "bass"
    measure_index = measure_index_at(measures, note.onset)
    return clef_kind_at(
        changes,
        staff,
        measure_index,
        note.onset - measures[measure_index].start,
    )


def _analyze_playability(
    notes: list[QuantizedNote],
    tempo_bpm: float,
    pedals: list[PedalEvent],
) -> dict[str, object]:
    attack_notes: dict[tuple[Hand, int], list[QuantizedNote]] = defaultdict(list)
    attacks: dict[tuple[Hand, int], set[int]] = defaultdict(set)
    all_attacks: dict[int, set[int]] = defaultdict(set)
    for note in notes:
        hand = _physical_hand(note)
        attack_notes[(hand, note.onset)].append(note)
        attacks[(hand, note.onset)].add(note.pitch)
        all_attacks[note.onset].add(note.pitch)

    rolled_attacks = {
        key
        for key, grouped_notes in attack_notes.items()
        if grouped_notes and all(note.arpeggiated for note in grouped_notes)
    }
    reaches_by_attack = {
        key: chord_reach(pitches) for key, pitches in attacks.items()
    }
    reaches = list(reaches_by_attack.values())
    oversized = sum(
        reach.exceeds_maximum_span and key not in rolled_attacks
        for key, reach in reaches_by_attack.items()
    )
    arpeggiated_wide = sum(
        reach.exceeds_maximum_span and key in rolled_attacks
        for key, reach in reaches_by_attack.items()
    )
    stretched = sum(
        reach.is_stretched and key not in rolled_attacks
        for key, reach in reaches_by_attack.items()
    )
    extended = sum(
        reach.is_extended_stretch and key not in rolled_attacks
        for key, reach in reaches_by_attack.items()
    )
    extreme_stretches = sum(
        reach.is_extreme_stretch and key not in rolled_attacks
        for key, reach in reaches_by_attack.items()
    )
    dense_stretches = sum(
        reach.is_stretched
        and reach.unique_keys >= MAX_SIMULTANEOUS_KEYS_PER_HAND
        and key not in rolled_attacks
        for key, reach in reaches_by_attack.items()
    )
    too_many = sum(
        reach.exceeds_finger_count and key not in rolled_attacks
        for key, reach in reaches_by_attack.items()
    )
    awkward_shapes = sum(
        chord_reach(pitches).playable
        and chord_fingering_feasible(pitches, hand)
        and not chord_fingering_natural(pitches, hand)
        and (hand, onset) not in rolled_attacks
        for (hand, onset), pitches in attacks.items()
    )
    unplayable_shapes = sum(
        chord_reach(pitches).playable
        and not chord_fingering_feasible(pitches, hand)
        and (hand, onset) not in rolled_attacks
        for (hand, onset), pitches in attacks.items()
    )
    maximum_span = max((reach.span_semitones for reach in reaches), default=0)
    maximum_span_mm = max((reach.span_mm for reach in reaches), default=0.0)

    (
        active_reaches,
        pedal_supported,
        held_awkward_shapes,
        held_unplayable_shapes,
    ) = _active_hand_reaches(notes, pedals)
    held_oversized = sum(reach.exceeds_maximum_span for reach in active_reaches)
    held_too_many = sum(reach.exceeds_finger_count for reach in active_reaches)
    maximum_held_span = max((reach.span_semitones for reach in active_reaches), default=0)
    maximum_held_span_mm = max((reach.span_mm for reach in active_reaches), default=0.0)

    by_onset_and_hand: dict[int, dict[Hand, list[int]]] = defaultdict(dict)
    for (hand, onset), pitches in attacks.items():
        by_onset_and_hand[onset][hand] = sorted(pitches)
    crossings = sum(
        1
        for hand_pitches in by_onset_and_hand.values()
        if Hand.LEFT in hand_pitches
        and Hand.RIGHT in hand_pitches
        and median(hand_pitches[Hand.LEFT]) > median(hand_pitches[Hand.RIGHT])
    )

    fast_leaps = 0
    seconds_per_division = 60.0 / max(20.0, min(300.0, tempo_bpm)) / CANONICAL_DIVISIONS
    for hand in (Hand.RIGHT, Hand.LEFT):
        events = sorted(
            (onset, float(median(pitches)))
            for (current_hand, onset), pitches in attacks.items()
            if current_hand == hand
        )
        for (previous_onset, previous_center), (onset, center) in zip(
            events, events[1:], strict=False
        ):
            elapsed_seconds = (onset - previous_onset) * seconds_per_division
            if elapsed_seconds <= FAST_LEAP_SECONDS and abs(center - previous_center) > FAST_LEAP_SEMITONES:
                fast_leaps += 1

    over_ten_total = sum(len(pitches) > 10 for pitches in all_attacks.values())
    out_of_range = sum(
        not PIANO_LOWEST_MIDI <= note.pitch <= PIANO_HIGHEST_MIDI for note in notes
    )
    if (
        oversized
        or held_oversized
        or too_many
        or unplayable_shapes
        or held_too_many
        or held_unplayable_shapes
        or over_ten_total
        or out_of_range
    ):
        playability_status = "unplayable_without_redistribution"
    elif stretched or awkward_shapes or held_awkward_shapes or fast_leaps or crossings:
        playability_status = "demanding"
    else:
        playability_status = "playable"

    return {
        "status": playability_status,
        "piano_range": {"lowest_midi": PIANO_LOWEST_MIDI, "highest_midi": PIANO_HIGHEST_MIDI},
        "out_of_piano_range_notes": out_of_range,
        "comfortable_span_semitones": COMFORTABLE_HAND_SPAN_SEMITONES,
        "comfortable_span_mm": COMFORTABLE_HAND_SPAN_MM,
        "extreme_stretch_span_semitones": EXTREME_HAND_SPAN_SEMITONES,
        "extreme_stretch_span_mm": EXTREME_HAND_SPAN_MM,
        "maximum_span_semitones": MAX_HAND_SPAN_SEMITONES,
        "maximum_span_mm": MAX_HAND_SPAN_MM,
        "maximum_observed_span_semitones": maximum_span,
        "maximum_observed_span_mm": maximum_span_mm,
        "maximum_held_span_semitones": maximum_held_span,
        "maximum_held_span_mm": maximum_held_span_mm,
        "max_simultaneous_notes_per_hand": MAX_SIMULTANEOUS_KEYS_PER_HAND,
        "oversized_chords": oversized,
        "arpeggiated_wide_chords": arpeggiated_wide,
        "held_oversized_spans": held_oversized,
        "stretched_chords": stretched,
        "extended_chords": extended,
        "extreme_stretch_chords": extreme_stretches,
        "dense_stretched_chords": dense_stretches,
        "too_many_notes_chords": too_many,
        "awkward_chord_shapes": awkward_shapes,
        "unplayable_chord_shapes": unplayable_shapes,
        "held_too_many_keys": held_too_many,
        "held_awkward_shapes": held_awkward_shapes,
        "held_unplayable_shapes": held_unplayable_shapes,
        "pedal_supported_wide_sustains": pedal_supported,
        "over_ten_total_keys": over_ten_total,
        "fast_large_leaps": fast_leaps,
        "hand_crossings": crossings,
    }


def _physical_hand(note: QuantizedNote) -> Hand:
    if note.hand is not None:
        return note.hand
    if note.staff == Staff.LEFT:
        return Hand.LEFT
    if note.staff == Staff.RIGHT:
        return Hand.RIGHT
    return Hand.LEFT if note.pitch < 60 else Hand.RIGHT


def _active_hand_reaches(
    notes: list[QuantizedNote],
    pedals: list[PedalEvent],
) -> tuple[list[ChordReach], int, int, int]:
    events: dict[Hand, dict[int, dict[str, list[QuantizedNote]]]] = defaultdict(
        lambda: defaultdict(lambda: {"start": [], "end": []})
    )
    for note in notes:
        hand = _physical_hand(note)
        events[hand][note.onset]["start"].append(note)
        events[hand][note.end]["end"].append(note)

    reaches: list[ChordReach] = []
    pedal_supported = 0
    held_awkward_shapes = 0
    held_unplayable_shapes = 0
    pedal_coverage = PedalCoverage(pedals)
    for hand, hand_events in events.items():
        active: dict[int, QuantizedNote] = {}
        for tick in sorted(hand_events):
            event = hand_events[tick]
            for note in event["end"]:
                active.pop(note.source_id, None)
            held_before_attack = bool(active)
            if event["start"] and held_before_attack:
                attack_pitches = {note.pitch for note in event["start"]}
                full_pitches = {note.pitch for note in active.values()} | attack_pitches
                required_pitches = {
                    note.pitch
                    for note in active.values()
                    if not pedal_coverage.covers(
                        note.channel,
                        tick,
                        note.end,
                    )
                } | attack_pitches
                reach = chord_reach(required_pitches)
                feasible_shape = chord_fingering_feasible(required_pitches, hand)
                awkward_shape = (
                    reach.playable
                    and feasible_shape
                    and not chord_fingering_natural(required_pitches, hand)
                )
                unplayable_shape = reach.playable and not feasible_shape
                full_reach = chord_reach(full_pitches)
                full_unplayable_shape = full_reach.playable and not chord_fingering_feasible(
                    full_pitches, hand
                )
                full_hard_failure = (
                    full_reach.exceeds_maximum_span
                    or full_reach.exceeds_finger_count
                    or full_unplayable_shape
                )
                required_hard_failure = (
                    reach.exceeds_maximum_span
                    or reach.exceeds_finger_count
                    or unplayable_shape
                )
                if full_hard_failure and not required_hard_failure:
                    pedal_supported += int(
                        required_pitches != full_pitches
                    )
                reaches.append(reach)
                held_awkward_shapes += int(awkward_shape)
                held_unplayable_shapes += int(unplayable_shape)
            for note in event["start"]:
                active[note.source_id] = note
    return (
        reaches,
        pedal_supported,
        held_awkward_shapes,
        held_unplayable_shapes,
    )
