"""Dynamics planning: turn note velocities into printed dynamic marks.

Performance velocities arrive free in every MIDI and transcription, yet the
engraved score used to ignore them entirely.  This plans one dynamics series
placed between the staves.  Thresholds are relative to the piece's own
velocity distribution (a piece scaled into 40–80 must not read as pp
throughout), marks commit only through a two-measure hysteresis or a genuine
two-step jump, and files with flat (likely synthetic) velocities get no marks
at all.
"""

from __future__ import annotations

from statistics import median

from .models import DynamicMark, MeasureSpan, QuantizedNote

MARKS = ("pp", "p", "mp", "mf", "f", "ff")
NEUTRAL_INDEX = 3  # mf
# Roughly 16 velocity points per mark: a quiet-50/loud-95 piece reads p..f,
# while a full-range performance still reaches pp and ff at the extremes.
STEP_SIZE = 16.0
FLAT_RANGE_GATE = 12.0


def plan_dynamics(
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
) -> list[DynamicMark]:
    if not notes or not measures:
        return []

    velocities = sorted(note.velocity for note in notes)
    p10 = velocities[max(0, round(len(velocities) * 0.1) - 1)]
    p90 = velocities[min(len(velocities) - 1, round(len(velocities) * 0.9))]
    if p90 - p10 < FLAT_RANGE_GATE:
        return []

    per_measure: list[float | None] = []
    for measure in measures:
        weighted = 0.0
        total = 0.0
        for note in notes:
            if measure.start <= note.onset < measure.end:
                weighted += note.velocity * max(1, note.duration)
                total += max(1, note.duration)
        per_measure.append(weighted / total if total else None)

    present = [value for value in per_measure if value is not None]
    if not present:
        return []
    center = median(present)

    def mark_index(velocity: float) -> int:
        step = round((velocity - center) / STEP_SIZE)
        return max(0, min(len(MARKS) - 1, NEUTRAL_INDEX + step))

    levels = [mark_index(value) if value is not None else None for value in per_measure]

    # Run-length smoothing: a printed dynamic must hold for at least two
    # measures.  One-measure spikes (a subito-looking blip in the velocity
    # estimate) merge back into the preceding region instead of committing
    # and immediately reverting.
    filled: list[int] = []
    current_level = NEUTRAL_INDEX
    for level in levels:
        if level is not None:
            current_level = level
        filled.append(current_level)

    runs: list[list[int]] = []  # [level, start_measure, length]
    for index, level in enumerate(filled):
        if runs and runs[-1][0] == level:
            runs[-1][2] += 1
        else:
            runs.append([level, index, 1])
    smoothed: list[list[int]] = []
    for run in runs:
        if run[2] < 2 and smoothed:
            # Blip: absorb into the preceding region.
            smoothed[-1][2] += run[2]
        elif smoothed and smoothed[-1][0] == run[0]:
            smoothed[-1][2] += run[2]
        else:
            smoothed.append(list(run))

    planned: list[DynamicMark] = []
    for level, start, _length in smoothed:
        velocity = per_measure[start]
        planned.append(DynamicMark(start, MARKS[level], _velocity_percent(velocity, center)))
    return planned


def _velocity_percent(value: float | None, center: float) -> float:
    velocity = center if value is None else value
    return round(max(5.0, min(100.0, velocity / 127 * 100)), 2)
