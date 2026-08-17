from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from .models import Hand, PedalEvent, QuantizedNote
from .options import ConversionOptions
from .piano_rules import (
    EXTREME_HAND_SPAN_SEMITONES,
    MAX_FINGER_SHAPE_EXCESS_MM,
    MAX_HAND_SPAN_MM,
    MAX_HAND_SPAN_SEMITONES,
    MAX_SIMULTANEOUS_KEYS_PER_HAND,
    NATURAL_FINGER_SHAPE_TOLERANCE_MM,
    ChordReach,
    PedalCoverage,
    chord_fingering_excess_mm,
    chord_fingering_feasible,
    chord_reach,
)


@dataclass(frozen=True, slots=True)
class _Assignment:
    left_ids: frozenset[int]
    right_ids: frozenset[int]
    left_center: float | None
    right_center: float | None
    local_cost: float


def assign_hands(
    notes: list[QuantizedNote],
    options: ConversionOptions,
    track_names: dict[int, str] | None = None,
    pedals: list[PedalEvent] | None = None,
) -> tuple[list[QuantizedNote], dict[str, object], list[str]]:
    warnings: list[str] = []
    pedal_coverage = PedalCoverage(pedals or [])
    if isinstance(options.hand_split, int):
        assigned = [
            note.with_hand(Hand.LEFT if note.pitch < options.hand_split else Hand.RIGHT)
            for note in notes
        ]
        return assigned, {"method": "fixed", "split_pitch": options.hand_split}, warnings

    if options.prefer_track_hints:
        track_result = _assign_using_tracks(notes, track_names or {})
        if track_result is not None:
            assigned, track_map, hint_quality = track_result
            assigned, rebalanced = _rebalance_unplayable_chords(
                assigned,
                improve_comfort=True,
            )
            assigned, active_rebalanced = _rebalance_active_constraints(
                assigned, pedal_coverage
            )
            assigned, held_rebalanced = _repair_held_conflicts(
                assigned, pedal_coverage
            )
            assigned, final_rebalanced = _rebalance_unplayable_chords(
                assigned,
                improve_comfort=False,
            )
            rebalanced += final_rebalanced
            if rebalanced:
                warnings.append(
                    f"为改善单手跨度并满足最多 5 音、最大大十度及自然五指手型约束，"
                    f"重新分配了 {rebalanced} 个和弦音"
                )
            if active_rebalanced:
                warnings.append(
                    f"考虑仍在按住的旧音后，又重新分配了 {active_rebalanced} 个音以降低持续手部跨度"
                )
            if held_rebalanced:
                warnings.append(
                    f"为解决后续持续音造成的手部冲突，回溯调整了 {held_rebalanced} 个音的演奏手"
                )
            return (
                assigned,
                {
                    "method": "tracks",
                    "track_map": track_map,
                    "track_hint_quality": hint_quality,
                    "rebalanced_chord_notes": rebalanced,
                    "rebalanced_active_notes": active_rebalanced,
                    "rebalanced_held_notes": held_rebalanced,
                },
                warnings,
            )

    assigned = _assign_using_dynamic_programming(notes)
    assigned, rebalanced = _rebalance_unplayable_chords(
        assigned,
        improve_comfort=True,
    )
    assigned, active_rebalanced = _rebalance_active_constraints(
        assigned, pedal_coverage
    )
    assigned, held_rebalanced = _repair_held_conflicts(assigned, pedal_coverage)
    assigned, final_rebalanced = _rebalance_unplayable_chords(
        assigned,
        improve_comfort=False,
    )
    rebalanced += final_rebalanced
    warnings.append("MIDI 未提供清晰的左右手轨道，已根据音域和连续性自动分配")
    if rebalanced:
        warnings.append(
            f"为改善单手跨度并满足最多 5 音、最大大十度及自然五指手型约束，"
            f"重新分配了 {rebalanced} 个和弦音"
        )
    if active_rebalanced:
        warnings.append(
            f"考虑仍在按住的旧音后，又重新分配了 {active_rebalanced} 个音以降低持续手部跨度"
        )
    if held_rebalanced:
        warnings.append(
            f"为解决后续持续音造成的手部冲突，回溯调整了 {held_rebalanced} 个音的演奏手"
        )
    return (
        assigned,
        {
            "method": "dynamic_programming",
            "split_pitch": "adaptive",
            "rebalanced_chord_notes": rebalanced,
            "rebalanced_active_notes": active_rebalanced,
            "rebalanced_held_notes": held_rebalanced,
        },
        warnings,
    )


def mark_unredistributable_chords_for_arpeggiation(
    notes: list[QuantizedNote],
) -> tuple[list[QuantizedNote], int]:
    """Mark simultaneous hand shapes that must be rolled to remain playable."""

    by_attack: dict[tuple[int, Hand], list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        if note.hand is not None:
            by_attack[(note.onset, note.hand)].append(note)

    arpeggiated_ids: set[int] = set()
    chord_count = 0
    for attack_notes in by_attack.values():
        reach = chord_reach(note.pitch for note in attack_notes)
        if not (reach.exceeds_maximum_span or reach.exceeds_finger_count):
            continue
        chord_count += 1
        arpeggiated_ids.update(note.source_id for note in attack_notes)
    return (
        [
            note.with_arpeggiation()
            if note.source_id in arpeggiated_ids
            else note
            for note in notes
        ],
        chord_count,
    )


def _assign_using_tracks(
    notes: list[QuantizedNote],
    track_names: dict[int, str],
) -> tuple[list[QuantizedNote], dict[int, str], str] | None:
    by_track: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_track[note.track].append(note)

    minimum_count = max(4, round(len(notes) * 0.08))
    substantial = [
        (track, track_notes)
        for track, track_notes in by_track.items()
        if len(track_notes) >= minimum_count
    ]
    if len(substantial) != 2:
        return None

    explicit_map: dict[int, Hand] = {}
    for track, _ in substantial:
        label = _hand_label(track_names.get(track, ""))
        if label is not None:
            explicit_map[track] = label
    if set(explicit_map.values()) == {Hand.LEFT, Hand.RIGHT}:
        assigned = _assign_with_track_map(notes, explicit_map)
        readable_map = {track: staff.name.lower() for track, staff in explicit_map.items()}
        return assigned, readable_map, "explicit_names"

    substantial.sort(key=lambda item: median(note.pitch for note in item[1]))
    low_track, low_notes = substantial[0]
    high_track, high_notes = substantial[1]
    low_pitches = sorted(note.pitch for note in low_notes)
    high_pitches = sorted(note.pitch for note in high_notes)
    median_gap = median(high_pitches) - median(low_pitches)
    allowed_middle_overlap = min(8.0, max(5.0, median_gap * 0.30))
    if not (
        median_gap >= 10
        and median(low_pitches) <= 58
        and median(high_pitches) >= 62
        # Score-exported grand staves often overlap around middle C because of
        # cross-staff gestures.  Quantization and coincident-note merging can
        # widen that overlap by another semitone, so scale the allowance with
        # the robust median separation while keeping an eight-semitone ceiling.
        # This accepts the six-semitone middle-C overlap in the real Unravel
        # reference export without treating two interleaved piano tracks as
        # reliable hand labels.
        and _percentile(low_pitches, 0.90)
        <= _percentile(high_pitches, 0.10) + allowed_middle_overlap
    ):
        return None

    track_map = {low_track: Hand.LEFT, high_track: Hand.RIGHT}
    assigned = _assign_with_track_map(notes, track_map)
    readable_map = {track: staff.name.lower() for track, staff in track_map.items()}
    return assigned, readable_map, "separated_ranges"


def _assign_with_track_map(
    notes: list[QuantizedNote],
    track_map: dict[int, Hand],
) -> list[QuantizedNote]:
    assigned = [note.with_hand(track_map[note.track]) for note in notes if note.track in track_map]
    unmapped = [note for note in notes if note.track not in track_map]
    if unmapped:
        assigned.extend(_assign_using_dynamic_programming(unmapped))
    return sorted(assigned, key=lambda note: (note.onset, note.pitch, note.source_id))


def _assign_using_dynamic_programming(notes: list[QuantizedNote]) -> list[QuantizedNote]:
    clusters: list[list[QuantizedNote]] = []
    by_onset: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_onset[note.onset].append(note)
    for onset in sorted(by_onset):
        clusters.append(sorted(by_onset[onset], key=lambda note: note.pitch))

    candidates = [_cluster_assignments(cluster) for cluster in clusters]
    costs: list[list[float]] = []
    backpointers: list[list[int | None]] = []

    for cluster_index, cluster_candidates in enumerate(candidates):
        cluster_costs: list[float] = []
        cluster_back: list[int | None] = []
        for candidate in cluster_candidates:
            if cluster_index == 0:
                cluster_costs.append(candidate.local_cost)
                cluster_back.append(None)
                continue

            previous_candidates = candidates[cluster_index - 1]
            possibilities = [
                costs[cluster_index - 1][previous_index]
                + candidate.local_cost
                + _transition_cost(previous, candidate)
                for previous_index, previous in enumerate(previous_candidates)
            ]
            best_previous = min(range(len(possibilities)), key=possibilities.__getitem__)
            cluster_costs.append(possibilities[best_previous])
            cluster_back.append(best_previous)
        costs.append(cluster_costs)
        backpointers.append(cluster_back)

    state_index = min(range(len(costs[-1])), key=costs[-1].__getitem__)
    chosen: list[_Assignment] = []
    for cluster_index in range(len(clusters) - 1, -1, -1):
        chosen.append(candidates[cluster_index][state_index])
        previous_index = backpointers[cluster_index][state_index]
        if previous_index is not None:
            state_index = previous_index
    chosen.reverse()

    left_ids = set().union(*(assignment.left_ids for assignment in chosen))
    return [
        note.with_hand(Hand.LEFT if note.source_id in left_ids else Hand.RIGHT)
        for note in notes
    ]


def _cluster_assignments(cluster: list[QuantizedNote]) -> list[_Assignment]:
    assignments: list[_Assignment] = []
    pitch_range = cluster[-1].pitch - cluster[0].pitch if len(cluster) > 1 else 0

    for split_index in range(len(cluster) + 1):
        left = cluster[:split_index]
        right = cluster[split_index:]
        left_pitches = [note.pitch for note in left]
        right_pitches = [note.pitch for note in right]
        cost = 0.0

        cost += sum(max(0, pitch - 64) ** 2 * 0.16 for pitch in left_pitches)
        cost += sum(max(0, 56 - pitch) ** 2 * 0.16 for pitch in right_pitches)
        cost += sum(max(0, pitch - 60) * 0.28 for pitch in left_pitches)
        cost += sum(max(0, 60 - pitch) * 0.28 for pitch in right_pitches)
        cost += sum((52 - pitch) ** 2 * 0.75 + 12 for pitch in right_pitches if pitch < 52)
        cost += sum((pitch - 72) ** 2 * 0.75 + 12 for pitch in left_pitches if pitch > 72)

        for hand, pitches in (
            (Hand.LEFT, left_pitches),
            (Hand.RIGHT, right_pitches),
        ):
            reach = chord_reach(pitches)
            cost += _reach_violation_cost(reach) * 8.0
            cost += _shape_violation_cost(pitches, hand) * 8.0
            cost += _reach_effort_cost(reach)
            cost += _shape_effort_cost(pitches, hand)

        if pitch_range >= 12 and (not left or not right):
            cost += 5.5
        if not left and min(note.pitch for note in cluster) < 52:
            cost += 4.0
        if not right and max(note.pitch for note in cluster) > 67:
            cost += 4.0

        assignments.append(
            _Assignment(
                left_ids=frozenset(note.source_id for note in left),
                right_ids=frozenset(note.source_id for note in right),
                left_center=median(left_pitches) if left_pitches else None,
                right_center=median(right_pitches) if right_pitches else None,
                local_cost=cost,
            )
        )
    return assignments


def _transition_cost(previous: _Assignment, current: _Assignment) -> float:
    cost = 0.0
    for previous_center, current_center in (
        (previous.left_center, current.left_center),
        (previous.right_center, current.right_center),
    ):
        if previous_center is None or current_center is None:
            cost += 0.35
            continue
        leap = abs(current_center - previous_center)
        cost += leap * 0.055 + max(0, leap - 12) ** 2 * 0.025
    return cost


def _rebalance_unplayable_chords(
    notes: list[QuantizedNote],
    *,
    improve_comfort: bool,
) -> tuple[list[QuantizedNote], int]:
    by_onset: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_onset[note.onset].append(note)

    changed_ids: set[int] = set()
    result: list[QuantizedNote] = []
    for onset_notes in by_onset.values():
        current_left = [note for note in onset_notes if note.hand == Hand.LEFT]
        current_right = [note for note in onset_notes if note.hand == Hand.RIGHT]
        current_hard_cost = _reach_violation_cost(chord_reach(_pitches(current_left)))
        current_hard_cost += _reach_violation_cost(chord_reach(_pitches(current_right)))
        current_hard_cost += _shape_violation_cost(_pitches(current_left), Hand.LEFT)
        current_hard_cost += _shape_violation_cost(_pitches(current_right), Hand.RIGHT)
        current_effort_cost = 0.0
        if improve_comfort:
            current_effort_cost = _reach_effort_cost(chord_reach(_pitches(current_left)))
            current_effort_cost += _reach_effort_cost(chord_reach(_pitches(current_right)))
            current_effort_cost += _shape_effort_cost(
                _pitches(current_left), Hand.LEFT
            )
            current_effort_cost += _shape_effort_cost(
                _pitches(current_right), Hand.RIGHT
            )
        if current_hard_cost == 0 and current_effort_cost == 0:
            result.extend(onset_notes)
            continue
        current_cost = _attack_assignment_cost(
            current_left,
            current_right,
            changes=0,
            note_count=len(onset_notes),
            include_effort=improve_comfort,
        )

        ordered = sorted(onset_notes, key=lambda note: (note.pitch, note.source_id))
        candidates: list[tuple[float, int]] = []
        for split_index in range(len(ordered) + 1):
            left = ordered[:split_index]
            right = ordered[split_index:]
            if not _playable_attack(left, Hand.LEFT) or not _playable_attack(
                right, Hand.RIGHT
            ):
                continue
            changes = sum(note.hand != Hand.LEFT for note in left) + sum(
                note.hand != Hand.RIGHT for note in right
            )
            candidate_cost = _attack_assignment_cost(
                left,
                right,
                changes=changes,
                note_count=len(ordered),
                include_effort=improve_comfort,
            )
            candidates.append((candidate_cost, split_index))

        if not candidates:
            result.extend(onset_notes)
            continue

        candidate_cost, split_index = min(candidates)
        if candidate_cost + 1e-9 >= current_cost:
            result.extend(onset_notes)
            continue
        left_ids = {note.source_id for note in ordered[:split_index]}
        for note in onset_notes:
            next_hand = Hand.LEFT if note.source_id in left_ids else Hand.RIGHT
            if note.hand != next_hand:
                changed_ids.add(note.source_id)
            result.append(note.with_hand(next_hand))

    return (
        sorted(result, key=lambda note: (note.onset, note.pitch, note.source_id)),
        len(changed_ids),
    )


def _playable_attack(notes: list[QuantizedNote], hand: Hand) -> bool:
    pitches = {note.pitch for note in notes}
    return chord_reach(pitches).playable and chord_fingering_feasible(pitches, hand)


def _pitches(notes: list[QuantizedNote]) -> set[int]:
    return {note.pitch for note in notes}


def _attack_assignment_cost(
    left: list[QuantizedNote],
    right: list[QuantizedNote],
    *,
    changes: int,
    note_count: int,
    include_effort: bool,
) -> float:
    left_reach = chord_reach(_pitches(left))
    right_reach = chord_reach(_pitches(right))
    hard_cost = _reach_violation_cost(left_reach) + _reach_violation_cost(right_reach)
    hard_cost += _shape_violation_cost(_pitches(left), Hand.LEFT)
    hard_cost += _shape_violation_cost(_pitches(right), Hand.RIGHT)
    effort_cost = 0.0
    if include_effort:
        effort_cost = _reach_effort_cost(left_reach) + _reach_effort_cost(right_reach)
        effort_cost += _shape_effort_cost(_pitches(left), Hand.LEFT)
        effort_cost += _shape_effort_cost(_pitches(right), Hand.RIGHT)
    register_cost = sum(max(0, note.pitch - 60) * 0.12 for note in left)
    register_cost += sum(max(0, 60 - note.pitch) * 0.12 for note in right)
    empty_cost = 2.0 if note_count > 1 and (not left or not right) else 0.0
    return hard_cost * 1000.0 + effort_cost + changes * 4.0 + register_cost + empty_cost


def _rebalance_active_constraints(
    notes: list[QuantizedNote],
    pedal_coverage: PedalCoverage,
) -> tuple[list[QuantizedNote], int]:
    """Greedily respect keys that remain physically depressed at a new onset."""

    by_onset: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_onset[note.onset].append(note)

    result: list[QuantizedNote] = []
    active_by_hand: dict[Hand, list[QuantizedNote]] = {
        Hand.LEFT: [],
        Hand.RIGHT: [],
    }
    changed_ids: set[int] = set()
    for onset in sorted(by_onset):
        for hand in (Hand.LEFT, Hand.RIGHT):
            active_by_hand[hand] = [
                note for note in active_by_hand[hand] if note.end > onset
            ]
        active_left = {
            note.pitch
            for note in active_by_hand[Hand.LEFT]
            if not pedal_coverage.covers(note.channel, onset, note.end)
        }
        active_right = {
            note.pitch
            for note in active_by_hand[Hand.RIGHT]
            if not pedal_coverage.covers(note.channel, onset, note.end)
        }
        attack = sorted(by_onset[onset], key=lambda note: (note.pitch, note.source_id))
        candidates: list[tuple[float, int]] = []
        for split_index in range(len(attack) + 1):
            left = attack[:split_index]
            right = attack[split_index:]
            left_reach = chord_reach(active_left | {note.pitch for note in left})
            right_reach = chord_reach(active_right | {note.pitch for note in right})
            violation_cost = _reach_violation_cost(left_reach) + _reach_violation_cost(
                right_reach
            )
            violation_cost += _shape_violation_cost(
                active_left | {note.pitch for note in left}, Hand.LEFT
            )
            violation_cost += _shape_violation_cost(
                active_right | {note.pitch for note in right}, Hand.RIGHT
            )
            attack_violation = _reach_violation_cost(
                chord_reach({note.pitch for note in left})
            ) + _reach_violation_cost(chord_reach({note.pitch for note in right}))
            attack_violation += _shape_violation_cost(
                {note.pitch for note in left}, Hand.LEFT
            )
            attack_violation += _shape_violation_cost(
                {note.pitch for note in right}, Hand.RIGHT
            )
            effort_cost = _reach_effort_cost(left_reach) + _reach_effort_cost(right_reach)
            effort_cost += _shape_effort_cost(
                active_left | {note.pitch for note in left}, Hand.LEFT
            )
            effort_cost += _shape_effort_cost(
                active_right | {note.pitch for note in right}, Hand.RIGHT
            )
            changes = sum(note.hand != Hand.LEFT for note in left) + sum(
                note.hand != Hand.RIGHT for note in right
            )
            register_cost = sum(max(0, note.pitch - 60) * 0.10 for note in left)
            register_cost += sum(max(0, 60 - note.pitch) * 0.10 for note in right)
            candidates.append(
                (
                    attack_violation * 1000.0
                    + violation_cost
                    + effort_cost
                    + changes * 4.0
                    + register_cost,
                    split_index,
                )
            )

        _, split_index = min(candidates)
        left_ids = {note.source_id for note in attack[:split_index]}
        for note in by_onset[onset]:
            hand = Hand.LEFT if note.source_id in left_ids else Hand.RIGHT
            if hand != note.hand:
                changed_ids.add(note.source_id)
            assigned_note = note.with_hand(hand)
            result.append(assigned_note)
            active_by_hand[hand].append(assigned_note)

    return (
        sorted(result, key=lambda note: (note.onset, note.pitch, note.source_id)),
        len(changed_ids),
    )


def _reach_violation_cost(reach: ChordReach) -> float:
    key_excess = max(0, reach.unique_keys - MAX_SIMULTANEOUS_KEYS_PER_HAND)
    semitone_excess = max(0, reach.span_semitones - MAX_HAND_SPAN_SEMITONES)
    millimeter_excess = max(0.0, reach.span_mm - MAX_HAND_SPAN_MM)
    return key_excess * 120.0 + semitone_excess * 45.0 + millimeter_excess * 2.0


def _shape_violation_cost(pitches: set[int] | list[int], hand: Hand) -> float:
    reach = chord_reach(pitches)
    if not reach.playable:
        return 0.0
    excess = chord_fingering_excess_mm(pitches, hand)
    if excess <= MAX_FINGER_SHAPE_EXCESS_MM:
        return 0.0
    return 80.0 + (excess - MAX_FINGER_SHAPE_EXCESS_MM) * 4.0


def _shape_effort_cost(pitches: set[int] | list[int], hand: Hand) -> float:
    reach = chord_reach(pitches)
    if not reach.playable:
        return 0.0
    excess = chord_fingering_excess_mm(pitches, hand)
    if excess <= NATURAL_FINGER_SHAPE_TOLERANCE_MM:
        return 0.0
    if excess > MAX_FINGER_SHAPE_EXCESS_MM:
        return 0.0
    return 4.0 + (excess - NATURAL_FINGER_SHAPE_TOLERANCE_MM) * 0.6


def _reach_effort_cost(reach: ChordReach) -> float:
    """Prefer comfortable hand shapes even when a tenth is technically possible."""

    if reach.exceeds_finger_count or reach.exceeds_maximum_span:
        return 0.0
    if reach.is_extreme_stretch:
        return 12.0 + max(0, reach.span_semitones - EXTREME_HAND_SPAN_SEMITONES) * 3.0
    if reach.is_extended_stretch:
        return 3.0
    return 0.0


def _repair_held_conflicts(
    notes: list[QuantizedNote],
    pedal_coverage: PedalCoverage,
) -> tuple[list[QuantizedNote], int]:
    """Retroactively move a held note when a later attack exposes an impossible reach."""

    hand_by_id = {note.source_id: note.hand or Hand.RIGHT for note in notes}
    note_by_id = {note.source_id: note for note in notes}
    by_onset: dict[int, list[QuantizedNote]] = defaultdict(list)
    timeline: dict[int, dict[str, list[int]]] = defaultdict(
        lambda: {"start": [], "end": []}
    )
    for note in notes:
        by_onset[note.onset].append(note)
        timeline[note.onset]["start"].append(note.source_id)
        timeline[note.end]["end"].append(note.source_id)
    locked: set[int] = set()

    for _ in range(3):
        changed_this_pass = False
        active_ids: set[int] = set()
        for tick in sorted(timeline):
            event = timeline[tick]
            active_ids.difference_update(event["end"])
            active_ids.update(event["start"])
            if not event["start"]:
                continue
            attack_ids = set(event["start"])
            active = [
                note_by_id[source_id]
                for source_id in active_ids
                if source_id in attack_ids
                or not pedal_coverage.covers(
                    note_by_id[source_id].channel,
                    tick,
                    note_by_id[source_id].end,
                )
            ]
            current_cost = _mapped_active_violation(active, hand_by_id)
            if current_cost <= 0:
                continue

            best: tuple[float, int, Hand] | None = None
            for note in active:
                if note.source_id in locked:
                    continue
                target = Hand.LEFT if hand_by_id[note.source_id] == Hand.RIGHT else Hand.RIGHT
                if not _mapped_attack_is_playable(by_onset[note.onset], hand_by_id, note, target):
                    continue
                previous = hand_by_id[note.source_id]
                hand_by_id[note.source_id] = target
                candidate_cost = _mapped_active_violation(active, hand_by_id)
                hand_by_id[note.source_id] = previous
                if candidate_cost + 1e-9 >= current_cost:
                    continue
                candidate = (candidate_cost, note.source_id, target)
                if best is None or candidate < best:
                    best = candidate

            if best is None:
                continue
            _, source_id, target = best
            hand_by_id[source_id] = target
            locked.add(source_id)
            changed_this_pass = True
        if not changed_this_pass:
            break

    changed = sum(hand_by_id[note.source_id] != note.hand for note in notes)
    return (
        [note.with_hand(hand_by_id[note.source_id]) for note in notes],
        changed,
    )


def _mapped_active_violation(
    active: list[QuantizedNote],
    hand_by_id: dict[int, Hand],
) -> float:
    cost = 0.0
    for hand in (Hand.LEFT, Hand.RIGHT):
        pitches = {
            note.pitch for note in active if hand_by_id[note.source_id] == hand
        }
        cost += _reach_violation_cost(chord_reach(pitches))
        cost += _shape_violation_cost(pitches, hand)
    return cost


def _mapped_attack_is_playable(
    attack: list[QuantizedNote],
    hand_by_id: dict[int, Hand],
    moved_note: QuantizedNote,
    target: Hand,
) -> bool:
    for hand in (Hand.LEFT, Hand.RIGHT):
        pitches = {
            note.pitch
            for note in attack
            if (target if note.source_id == moved_note.source_id else hand_by_id[note.source_id])
            == hand
        }
        if not chord_reach(pitches).playable or not chord_fingering_feasible(
            pitches, hand
        ):
            return False
    return True


def _hand_label(name: str) -> Hand | None:
    normalized = re.sub(r"[_\-]+", " ", name.strip().lower())
    right_patterns = (r"\bright\s*hand\b", r"\brh\b", r"\bupper\b", r"右手", r"高音")
    left_patterns = (r"\bleft\s*hand\b", r"\blh\b", r"\blower\b", r"左手", r"低音")
    if any(re.search(pattern, normalized) for pattern in right_patterns):
        return Hand.RIGHT
    if any(re.search(pattern, normalized) for pattern in left_patterns):
        return Hand.LEFT
    return None


def _percentile(values: list[int], fraction: float) -> int:
    if not values:
        raise ValueError("percentile requires at least one value")
    index = round((len(values) - 1) * fraction)
    return values[max(0, min(len(values) - 1, index))]
