from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from .clefs import clef_kind_at
from .meter_map import measure_index_at
from .models import (
    CANONICAL_DIVISIONS,
    ClefChange,
    Hand,
    MeasureSpan,
    QuantizedNote,
    Staff,
)


@dataclass(frozen=True, slots=True)
class _StaffAssignment:
    onset: int
    bass_ids: frozenset[int]
    treble_ids: frozenset[int]
    bass_center: float | None
    treble_center: float | None
    dominant_staff: Staff
    local_cost: float


def assign_staves(
    notes: list[QuantizedNote],
    track_staff_hints: dict[int, Staff] | None = None,
    *,
    lock_hands_to_staves: bool = False,
) -> tuple[list[QuantizedNote], dict[str, object], list[str]]:
    """Assign notation staves independently from the physical hands.

    A pianist's right hand can legitimately play in the bass staff and the left
    hand can play in the treble staff. Keeping those two concepts separate avoids
    forcing low cross-hand notes onto an unreadable clef.
    """

    if not notes:
        return notes, {"method": "ledger_cost_dynamic_programming"}, []

    warnings: list[str] = []
    normalized: list[QuantizedNote] = []
    inferred_hands = 0
    for note in notes:
        if note.hand is None:
            inferred_hands += 1
            normalized.append(note.with_hand(Hand.LEFT if note.pitch < 60 else Hand.RIGHT))
        else:
            normalized.append(note)
    if inferred_hands:
        warnings.append(f"有 {inferred_hands} 个音缺少物理手标记，已按音域安全补全")

    assigned: list[QuantizedNote] = []
    hinted_count = 0
    fallback_notes: list[QuantizedNote] = []
    hints = track_staff_hints or {}
    for note in normalized:
        hinted_staff = hints.get(note.track)
        if hinted_staff is None:
            fallback_notes.append(note)
            continue
        assigned.append(note.with_staff(hinted_staff))
        hinted_count += 1

    for hand in (Hand.RIGHT, Hand.LEFT):
        hand_notes = [note for note in fallback_notes if note.hand == hand]
        if lock_hands_to_staves:
            target = Staff.RIGHT if hand == Hand.RIGHT else Staff.LEFT
            assigned.extend(note.with_staff(target) for note in hand_notes)
        else:
            assigned.extend(_assign_hand_staves(hand_notes, hand))

    clarified_hand_notes = 0
    if hinted_count or lock_hands_to_staves:
        # A score-export track represents a notation staff, not merely a pitch
        # band.  The Animenz references deliberately keep high accompaniment on
        # the lower staff under a treble clef (and occasionally low material on
        # the upper staff under a bass clef).  Moving those notes by pitch here
        # destroys that structure and fragments phrase-level ottava lines; the
        # measure-boundary clef planner is the correct place to make them easy
        # to read.
        extreme_repairs = 0
    else:
        assigned, extreme_repairs = _repair_extreme_staves(assigned)
        assigned, clarified_hand_notes = _separate_ambiguous_hand_runs(assigned)
    assigned.sort(key=lambda note: (note.onset, note.pitch, note.source_id))

    cross_staff = sum(
        (note.hand == Hand.RIGHT and note.staff == Staff.LEFT)
        or (note.hand == Hand.LEFT and note.staff == Staff.RIGHT)
        for note in assigned
    )
    ledger_pressure = sum(
        (note.staff == Staff.RIGHT and note.pitch < 60)
        or (note.staff == Staff.LEFT and note.pitch > 60)
        for note in assigned
    )
    switches = {
        hand.name.lower(): _count_staff_switches(assigned, hand)
        for hand in (Hand.RIGHT, Hand.LEFT)
    }
    onset_count = len({(note.hand, note.onset) for note in assigned})
    switch_count = sum(switches.values())
    if switch_count > max(8, round(onset_count * 0.20)):
        warnings.append(
            f"为减少加线已产生 {switch_count} 次谱表转换；该段音域频繁跨越中央 C，建议人工复核跨谱表写法"
        )
    if extreme_repairs:
        warnings.append(f"已修复 {extreme_repairs} 个会产生过多加线的极端谱表归属")
    if clarified_hand_notes:
        warnings.append(
            f"为避免两手长期挤在同一谱表，已将 {clarified_hand_notes} 个音恢复到对应上下谱表；"
            "后续由动态谱号减少加线"
        )

    return (
        assigned,
        {
            "method": (
                "source_track_hints_with_dynamic_clefs"
                if hinted_count
                else (
                    "hand_locked_dynamic_clefs"
                    if lock_hands_to_staves
                    else "ledger_cost_dynamic_programming"
                )
            ),
            "source_track_hint_notes": hinted_count,
            "treble_notes": sum(note.staff == Staff.RIGHT for note in assigned),
            "bass_notes": sum(note.staff == Staff.LEFT for note in assigned),
            "cross_staff_hand_notes": cross_staff,
            "ledger_pressure_notes": ledger_pressure,
            "staff_switches": switches,
            "extreme_repairs": extreme_repairs,
            "clarified_hand_notes": clarified_hand_notes,
        },
        warnings,
    )


def repair_staves_for_planned_clefs(
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
    clef_changes: list[ClefChange],
) -> tuple[list[QuantizedNote], int]:
    """Move only notes that remain extreme under the already planned clefs.

    Trusted score tracks stay intact through initial assignment so a lower
    staff can legitimately use treble clef.  After clefs are known, isolated
    notes that still sit beyond the practical extreme threshold move toward the
    register-appropriate staff; the following clef-planning pass then adapts to
    that repaired material.
    """

    changed = 0
    result: list[QuantizedNote] = []
    for note in notes:
        if note.staff is None:
            result.append(note)
            continue
        measure_index = measure_index_at(measures, note.onset)
        current_kind = clef_kind_at(
            clef_changes,
            note.staff,
            measure_index,
            note.onset - measures[measure_index].start,
        )
        current_extreme = (
            (current_kind == "treble" and note.pitch < 52)
            or (current_kind == "bass" and note.pitch > 72)
        )
        register_staff = (
            Staff.LEFT
            if note.pitch < 52
            else Staff.RIGHT
            if note.pitch > 72
            else note.staff
        )
        if current_extreme and register_staff != note.staff:
            result.append(note.with_staff(register_staff))
            changed += 1
        else:
            result.append(note)
    return result, changed


def _assign_hand_staves(notes: list[QuantizedNote], hand: Hand) -> list[QuantizedNote]:
    if not notes:
        return []

    by_onset: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_onset[note.onset].append(note)
    clusters = [sorted(by_onset[onset], key=lambda note: (note.pitch, note.source_id)) for onset in sorted(by_onset)]
    candidates = [_cluster_candidates(cluster, hand) for cluster in clusters]

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
            possibilities = [
                costs[cluster_index - 1][previous_index]
                + candidate.local_cost
                + _transition_cost(previous, candidate)
                for previous_index, previous in enumerate(candidates[cluster_index - 1])
            ]
            best_previous = min(range(len(possibilities)), key=possibilities.__getitem__)
            cluster_costs.append(possibilities[best_previous])
            cluster_back.append(best_previous)
        costs.append(cluster_costs)
        backpointers.append(cluster_back)

    state_index = min(range(len(costs[-1])), key=costs[-1].__getitem__)
    selected: list[_StaffAssignment] = []
    for cluster_index in range(len(clusters) - 1, -1, -1):
        selected.append(candidates[cluster_index][state_index])
        previous_index = backpointers[cluster_index][state_index]
        if previous_index is not None:
            state_index = previous_index
    selected.reverse()

    bass_ids = set().union(*(candidate.bass_ids for candidate in selected))
    return [
        note.with_staff(Staff.LEFT if note.source_id in bass_ids else Staff.RIGHT)
        for note in notes
    ]


def _cluster_candidates(cluster: list[QuantizedNote], hand: Hand) -> list[_StaffAssignment]:
    candidates: list[_StaffAssignment] = []
    onset = cluster[0].onset
    for split_index in range(len(cluster) + 1):
        bass = cluster[:split_index]
        treble = cluster[split_index:]
        cost = sum(_bass_ledger_cost(note.pitch) for note in bass)
        cost += sum(_treble_ledger_cost(note.pitch) for note in treble)

        if bass and treble:
            cost += 1.10
            boundary_gap = treble[0].pitch - bass[-1].pitch
            if boundary_gap <= 2:
                cost += 0.80
        elif (hand == Hand.RIGHT and bass) or (hand == Hand.LEFT and treble):
            cost += 0.12

        # Extremely remote clef choices remain possible in the candidate graph so
        # the algorithm never drops a note, but they are prohibitively expensive.
        cost += sum(45 + (52 - note.pitch) ** 2 for note in treble if note.pitch < 52)
        cost += sum(45 + (note.pitch - 72) ** 2 for note in bass if note.pitch > 72)

        if len(bass) > len(treble):
            dominant = Staff.LEFT
        elif len(treble) > len(bass):
            dominant = Staff.RIGHT
        else:
            center = median(note.pitch for note in cluster)
            dominant = Staff.LEFT if center < 60 else Staff.RIGHT

        candidates.append(
            _StaffAssignment(
                onset=onset,
                bass_ids=frozenset(note.source_id for note in bass),
                treble_ids=frozenset(note.source_id for note in treble),
                bass_center=median(note.pitch for note in bass) if bass else None,
                treble_center=median(note.pitch for note in treble) if treble else None,
                dominant_staff=dominant,
                local_cost=cost,
            )
        )
    return candidates


def _treble_ledger_cost(pitch: int) -> float:
    if pitch >= 64:
        return 0.0
    if pitch >= 60:
        return (64 - pitch) * 0.12
    return 0.48 + ((60 - pitch) / 2) ** 2 * 0.75


def _bass_ledger_cost(pitch: int) -> float:
    if pitch <= 57:
        return 0.0
    if pitch <= 60:
        return (pitch - 57) * 0.12
    return 0.36 + ((pitch - 60) / 2) ** 2 * 0.75


def _transition_cost(previous: _StaffAssignment, current: _StaffAssignment) -> float:
    elapsed = current.onset - previous.onset
    if elapsed <= 480:
        switch_penalty = 1.50
    elif elapsed <= 1920:
        switch_penalty = 0.90
    else:
        switch_penalty = 0.30
    cost = switch_penalty if previous.dominant_staff != current.dominant_staff else 0.0
    previous_split = bool(previous.bass_ids and previous.treble_ids)
    current_split = bool(current.bass_ids and current.treble_ids)
    if previous_split != current_split:
        cost += 0.30
    return cost


def _repair_extreme_staves(notes: list[QuantizedNote]) -> tuple[list[QuantizedNote], int]:
    repaired: list[QuantizedNote] = []
    changes = 0
    for note in notes:
        target = note.staff
        if note.pitch < 52:
            target = Staff.LEFT
        elif note.pitch > 72:
            target = Staff.RIGHT
        if target != note.staff:
            changes += 1
        repaired.append(note.with_staff(target or Staff.RIGHT))
    return repaired, changes


def _separate_ambiguous_hand_runs(
    notes: list[QuantizedNote],
) -> tuple[list[QuantizedNote], int]:
    """Restore long two-hand passages from one shared staff to the grand staff.

    Short cross-staff gestures remain untouched. Repeated passages where both
    physical hands were collapsed into one staff are separated, allowing the
    clef planner to use double treble or double bass when appropriate.
    """

    by_onset: dict[int, list[QuantizedNote]] = defaultdict(list)
    for note in notes:
        by_onset[note.onset].append(note)

    candidates: list[tuple[int, Staff, Hand]] = []
    for onset, onset_notes in sorted(by_onset.items()):
        hands = {note.hand for note in onset_notes if note.hand is not None}
        staves = {note.staff for note in onset_notes if note.staff is not None}
        if hands != {Hand.LEFT, Hand.RIGHT} or len(staves) != 1:
            continue
        shared_staff = next(iter(staves))
        if shared_staff is None:
            continue
        moved_hand = Hand.LEFT if shared_staff == Staff.RIGHT else Hand.RIGHT
        if any(note.hand == moved_hand for note in onset_notes):
            candidates.append((onset, shared_staff, moved_hand))

    move_windows: list[tuple[int, int, Staff, Hand]] = []
    run: list[tuple[int, Staff, Hand]] = []
    for candidate in candidates:
        compatible = (
            not run
            or (
                candidate[1:] == run[-1][1:]
                and candidate[0] - run[-1][0] <= CANONICAL_DIVISIONS
            )
        )
        if compatible:
            run.append(candidate)
            continue
        _append_clarity_window(move_windows, run)
        run = [candidate]
    _append_clarity_window(move_windows, run)

    changed = 0
    result: list[QuantizedNote] = []
    for note in notes:
        target = note.staff
        for start, end, shared_staff, moved_hand in move_windows:
            if (
                start <= note.onset <= end
                and note.staff == shared_staff
                and note.hand == moved_hand
            ):
                target = Staff.LEFT if moved_hand == Hand.LEFT else Staff.RIGHT
                break
        if target != note.staff:
            changed += 1
        result.append(note.with_staff(target or Staff.RIGHT))
    return result, changed


def _append_clarity_window(
    windows: list[tuple[int, int, Staff, Hand]],
    run: list[tuple[int, Staff, Hand]],
) -> None:
    if len(run) < 4:
        return
    start = run[0][0]
    end = run[-1][0]
    if end - start < CANONICAL_DIVISIONS:
        return
    windows.append((start, end, run[0][1], run[0][2]))


def _count_staff_switches(notes: list[QuantizedNote], hand: Hand) -> int:
    by_onset: dict[int, list[Staff]] = defaultdict(list)
    for note in notes:
        if note.hand == hand and note.staff is not None:
            by_onset[note.onset].append(note.staff)
    sequence: list[Staff] = []
    for onset in sorted(by_onset):
        staves = by_onset[onset]
        treble = sum(staff == Staff.RIGHT for staff in staves)
        bass = len(staves) - treble
        if treble == bass:
            sequence.append(Staff.RIGHT if hand == Hand.RIGHT else Staff.LEFT)
        else:
            sequence.append(Staff.RIGHT if treble > bass else Staff.LEFT)
    return sum(previous != current for previous, current in zip(sequence, sequence[1:], strict=False))
