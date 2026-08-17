from __future__ import annotations

from .models import CANONICAL_DIVISIONS, MeasureSpan, Meter, ParsedMidi


def build_measure_map(
    parsed: ParsedMidi,
    override: Meter | None = None,
) -> tuple[list[MeasureSpan], int, list[str]]:
    """Build variable-length measure boundaries, including an encoded pickup."""

    scale = CANONICAL_DIVISIONS / parsed.ticks_per_beat
    score_end = max(1, max(round(note.end_tick * scale) for note in parsed.notes))
    warnings: list[str] = []

    if override is not None:
        signatures = [(0, override)]
    else:
        by_tick = {
            round(event.tick * scale): Meter(event.numerator, event.denominator)
            for event in parsed.time_signatures
        }
        signatures = sorted(by_tick.items())
        if not signatures or signatures[0][0] > 0:
            signatures.insert(0, (0, Meter()))

    measures: list[MeasureSpan] = []
    current_start = 0
    event_index = 1
    current_meter = signatures[0][1]

    if _is_encoded_pickup(signatures):
        pickup_length = signatures[1][0]
        current_meter = signatures[1][1]
        measures.append(
            MeasureSpan(
                index=0,
                start=0,
                duration=pickup_length,
                meter=current_meter,
                implicit=True,
            )
        )
        current_start = pickup_length
        event_index = 2
        warnings.append(
            f"已识别 {signatures[0][1].numerator}/{signatures[0][1].denominator} 弱起，"
            f"正文拍号为 {current_meter.numerator}/{current_meter.denominator}"
        )

    while current_start < score_end:
        while event_index < len(signatures) and signatures[event_index][0] <= current_start:
            current_meter = signatures[event_index][1]
            event_index += 1

        regular_end = current_start + current_meter.measure_length
        next_change = signatures[event_index][0] if event_index < len(signatures) else None
        if next_change is not None and current_start < next_change < regular_end:
            duration = next_change - current_start
            warnings.append(
                f"拍号在非完整小节边界 {next_change} 处变化，已保留 {duration} divisions 的过渡小节"
            )
        else:
            duration = current_meter.measure_length

        measures.append(
            MeasureSpan(
                index=len(measures),
                start=current_start,
                duration=duration,
                meter=current_meter,
            )
        )
        current_start += duration

    first_onset = min(round(note.start_tick * scale) for note in parsed.notes)
    leading_count = 0
    while leading_count < len(measures) and measures[leading_count].end <= first_onset:
        leading_count += 1
    shift = measures[leading_count].start if leading_count < len(measures) else 0
    if leading_count:
        warnings.append(f"已移除开头 {leading_count} 个完整空小节")
        measures = [
            MeasureSpan(
                index=index,
                start=measure.start - shift,
                duration=measure.duration,
                meter=measure.meter,
                implicit=measure.implicit,
            )
            for index, measure in enumerate(measures[leading_count:])
        ]

    return measures, shift, warnings


def measure_index_at(measures: list[MeasureSpan], onset: int) -> int:
    if not measures:
        raise ValueError("measure map is empty")
    low = 0
    high = len(measures)
    while low < high:
        middle = (low + high) // 2
        measure = measures[middle]
        if onset < measure.start:
            high = middle
        elif onset >= measure.end:
            low = middle + 1
        else:
            return middle
    return max(0, min(len(measures) - 1, low))


def _is_encoded_pickup(signatures: list[tuple[int, Meter]]) -> bool:
    if len(signatures) < 2 or signatures[0][0] != 0:
        return False
    first_meter = signatures[0][1]
    second_tick, second_meter = signatures[1]
    return (
        second_tick == first_meter.measure_length
        and first_meter.measure_length < second_meter.measure_length
        and first_meter.numerator <= 3
    )
