from __future__ import annotations

from collections import defaultdict
from statistics import median

from .meter_map import measure_index_at
from .models import CANONICAL_DIVISIONS, ClefChange, MeasureSpan, QuantizedNote, Staff

CLEF_CHANGE_PENALTY = 6.5
MID_MEASURE_CLEF_PENALTY = 3.25
MID_MEASURE_MIN_IMPROVEMENT = 5.0


def plan_clefs(
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
    *,
    responsive: bool = False,
) -> tuple[list[ClefChange], dict[str, object]]:
    """Choose stable treble/bass clefs independently for the two piano staves.

    The dynamic program works at measure boundaries. That is deliberately more
    conservative than changing clef around every local extreme, while still
    supporting professional double-treble and double-bass passages.
    """

    pitches_by_location: dict[tuple[Staff, int], list[int]] = defaultdict(list)
    activity_by_location: dict[
        tuple[Staff, int],
        list[tuple[int, int, int]],
    ] = defaultdict(list)
    for note in notes:
        if note.staff is None:
            continue
        measure_index = measure_index_at(measures, note.onset)
        pitches_by_location[(note.staff, measure_index)].append(note.pitch)
        while measure_index < len(measures):
            measure = measures[measure_index]
            if measure.start >= note.end:
                break
            start = max(note.onset, measure.start) - measure.start
            end = min(note.end, measure.end) - measure.start
            if end > start:
                activity_by_location[(note.staff, measure_index)].append(
                    (start, end, note.pitch)
                )
            measure_index += 1

    changes: list[ClefChange] = []
    selected_by_staff: dict[Staff, list[str]] = {}
    for staff in (Staff.RIGHT, Staff.LEFT):
        default = "treble" if staff == Staff.RIGHT else "bass"
        selected = _plan_staff_clefs(
            [
                pitches_by_location.get((staff, index), [])
                or [
                    pitch
                    for _, _, pitch in activity_by_location.get((staff, index), [])
                ]
                for index in range(len(measures))
            ],
            default,
            change_penalty=4.0 if responsive else CLEF_CHANGE_PENALTY,
        )
        selected_by_staff[staff] = selected
        previous: str | None = None
        for measure_index, kind in enumerate(selected):
            if kind == previous:
                continue
            offset = 0
            if previous is not None:
                offset = _delayed_change_offset(
                    activity_by_location.get((staff, measure_index), []),
                    measures[measure_index].duration,
                    previous,
                    kind,
                )
            changes.append(ClefChange(measure_index, staff, kind, offset))
            previous = kind

    return (
        sorted(
            changes,
            key=lambda change: (
                change.measure_index,
                change.offset,
                int(change.staff),
            ),
        ),
        {
            "method": (
                "responsive_measure_dynamic_programming_with_in_measure_refinement"
                if responsive
                else "measure_dynamic_programming_with_in_measure_refinement"
            ),
            "upper_initial": selected_by_staff[Staff.RIGHT][0],
            "lower_initial": selected_by_staff[Staff.LEFT][0],
            "upper_changes": sum(
                change.staff == Staff.RIGHT and change.measure_index > 0 for change in changes
            ),
            "lower_changes": sum(
                change.staff == Staff.LEFT and change.measure_index > 0 for change in changes
            ),
            "mid_measure_changes": sum(change.offset > 0 for change in changes),
        },
    )


def clef_kind_at(
    changes: list[ClefChange],
    staff: Staff,
    measure_index: int,
    offset: int = 0,
) -> str:
    default = "treble" if staff == Staff.RIGHT else "bass"
    selected = default
    for change in sorted(
        (item for item in changes if item.staff == staff),
        key=lambda item: (item.measure_index, item.offset),
    ):
        if (change.measure_index, change.offset) > (measure_index, offset):
            break
        selected = change.kind
    return selected


def _beamed_run_spans(activity: list[tuple[int, int, int]]) -> list[tuple[int, int]]:
    """Spans of continuous runs of three or more short (<= eighth) notes."""

    segments = sorted(activity)
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    run_end = 0
    members = 0
    for start, end, _ in segments:
        if end - start > CANONICAL_DIVISIONS // 2:
            if members >= 3:
                runs.append((run_start, run_end))
            run_start = None
            members = 0
            continue
        if run_start is None or start > run_end:
            if members >= 3:
                runs.append((run_start, run_end))
            run_start = start
            members = 1
        else:
            members += 1
        run_end = max(run_end, end)
    if members >= 3 and run_start is not None:
        runs.append((run_start, run_end))
    return runs


def _delayed_change_offset(
    activity: list[tuple[int, int, int]],
    measure_duration: int,
    previous_kind: str,
    next_kind: str,
) -> int:
    """Delay a barline clef change when a carried sonority still needs the old clef.

    The measure-level dynamic program chooses the main register of each bar.
    This refinement then examines every sounding interval, including notes tied
    across the barline, and places the clef at a clean attack/release boundary.
    """

    if not activity or previous_kind == next_kind:
        return 0
    minimum_side = min(240, max(1, measure_duration // 4))
    candidates = sorted(
        {
            point
            for start, end, _ in activity
            for point in (start, end)
            if minimum_side <= point <= measure_duration - minimum_side
        }
    )
    if not candidates:
        return 0
    run_spans = _beamed_run_spans(activity)
    if run_spans:
        # A clef change inside a tuplet or a continuous beamed run splits the
        # figure visually; move the change to the run's end instead.
        sheltered = [
            point
            for point in candidates
            if not any(start < point < end for start, end in run_spans)
        ]
        run_ends = {
            end
            for start, end in run_spans
            if minimum_side <= end <= measure_duration - minimum_side
        }
        candidates = sorted(set(sheltered) | run_ends)
        if not candidates:
            return 0

    boundary_cost = _timed_clef_cost(activity, next_kind, 0, measure_duration)
    best_offset = 0
    best_cost = boundary_cost
    for candidate in candidates:
        before_old = _timed_clef_cost(activity, previous_kind, 0, candidate)
        before_new = _timed_clef_cost(activity, next_kind, 0, candidate)
        after_old = _timed_clef_cost(
            activity,
            previous_kind,
            candidate,
            measure_duration,
        )
        after_new = _timed_clef_cost(
            activity,
            next_kind,
            candidate,
            measure_duration,
        )
        if before_old + 1.0 >= before_new or after_new > after_old + 1.0:
            continue
        crossing = sum(start < candidate < end for start, end, _ in activity)
        candidate_cost = (
            before_old
            + after_new
            + MID_MEASURE_CLEF_PENALTY
            + crossing * 2.5
        )
        if candidate_cost < best_cost:
            best_cost = candidate_cost
            best_offset = candidate
    if boundary_cost - best_cost < MID_MEASURE_MIN_IMPROVEMENT:
        return 0
    return best_offset


def _timed_clef_cost(
    activity: list[tuple[int, int, int]],
    kind: str,
    start: int,
    end: int,
) -> float:
    cost = 0.0
    for segment_start, segment_end, pitch in activity:
        overlap = min(segment_end, end) - max(segment_start, start)
        if overlap <= 0:
            continue
        pitch_cost = _pitch_clef_cost(pitch, kind)
        if kind == "treble" and pitch < 52:
            pitch_cost += (52 - pitch) * 1.5
        elif kind == "bass" and pitch > 72:
            pitch_cost += (pitch - 72) * 1.5
        cost += pitch_cost * overlap / 480
    return cost


def _plan_staff_clefs(
    pitches_by_measure: list[list[int]],
    default: str,
    *,
    change_penalty: float,
) -> list[str]:
    states = ("treble", "bass")
    costs: list[dict[str, float]] = []
    backpointers: list[dict[str, str | None]] = []

    for measure_index, pitches in enumerate(pitches_by_measure):
        local = {state: _measure_clef_cost(pitches, state) for state in states}
        measure_costs: dict[str, float] = {}
        measure_back: dict[str, str | None] = {}
        for state in states:
            if measure_index == 0:
                initial_bias = 0.45 if state != default else 0.0
                measure_costs[state] = local[state] + initial_bias
                measure_back[state] = None
                continue
            candidates = {
                previous: costs[-1][previous]
                + local[state]
                + (change_penalty if previous != state else 0.0)
                for previous in states
            }
            previous = min(candidates, key=candidates.__getitem__)
            measure_costs[state] = candidates[previous]
            measure_back[state] = previous
        costs.append(measure_costs)
        backpointers.append(measure_back)

    state = min(costs[-1], key=costs[-1].__getitem__)
    selected: list[str] = []
    for measure_index in range(len(pitches_by_measure) - 1, -1, -1):
        selected.append(state)
        previous = backpointers[measure_index][state]
        if previous is not None:
            state = previous
    selected.reverse()
    return selected


def _measure_clef_cost(pitches: list[int], kind: str) -> float:
    if not pitches:
        return 0.0
    costs = sorted(_pitch_clef_cost(pitch, kind) for pitch in pitches)
    extreme_penalty = 0.0
    if kind == "treble" and min(pitches) < 52:
        extreme_penalty = (52 - min(pitches)) * 2.0
    elif kind == "bass" and max(pitches) > 72:
        extreme_penalty = (max(pitches) - 72) * 2.0
    return (
        median(costs)
        + costs[-1] * 0.45
        + sum(costs) / len(costs) * 0.25
        + extreme_penalty
    )


def _pitch_clef_cost(pitch: int, kind: str) -> float:
    if kind == "treble":
        if 55 <= pitch <= 88:
            return 0.0
        if pitch < 55:
            return ((55 - pitch) / 2) ** 2
        return ((pitch - 88) / 2) ** 2 * 0.45
    if 33 <= pitch <= 67:
        return 0.0
    if pitch < 33:
        return ((33 - pitch) / 2) ** 2 * 0.45
    return ((pitch - 67) / 2) ** 2
