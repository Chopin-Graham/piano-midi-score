from app.core.duration_simplifier import simplify_polyphonic_durations
from app.core.models import MeasureSpan, Meter, PedalEvent, QuantizedNote, Staff
from app.core.voices import assign_voices


def test_simplifies_performance_overlap_without_dropping_note_attacks() -> None:
    notes = [
        QuantizedNote(1, 84, 0, 960, 90, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 72, 0, 120, 72, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 80, 480, 480, 84, 0, 0, Staff.RIGHT),
        QuantizedNote(4, 74, 480, 120, 76, 0, 0, Staff.RIGHT),
    ]

    simplified, analysis, warnings = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="balanced",
    )
    voiced, counts, _ = assign_voices(simplified, 2)

    assert len(simplified) == len(notes)
    assert len(voiced) == len(notes)
    assert analysis["adjusted_note_count"] > 0
    assert warnings
    assert counts["right"] <= 2


def test_pedal_covered_release_gap_extends_to_compound_measure_boundary() -> None:
    meter = Meter(6, 8)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [QuantizedNote(1, 60, 0, 1320, 80, 0, 0, Staff.LEFT)]

    simplified, analysis, warnings = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        pedals=[PedalEvent(0, 0, True), PedalEvent(1441, 0, False)],
        measures=measures,
    )

    assert simplified[0].duration == meter.measure_length
    assert analysis["pedal_extended_note_count"] == 1
    assert any("消除碎休止符" in warning for warning in warnings)


def test_release_gap_is_not_extended_without_continuous_pedal() -> None:
    meter = Meter(6, 8)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [QuantizedNote(1, 60, 0, 1320, 80, 0, 0, Staff.LEFT)]

    simplified, analysis, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        pedals=[PedalEvent(0, 0, True), PedalEvent(1380, 0, False)],
        measures=measures,
    )

    assert simplified[0].duration == 1320
    assert analysis["pedal_extended_note_count"] == 0


def test_release_gap_is_not_extended_across_an_intervening_attack() -> None:
    meter = Meter(6, 8)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 60, 0, 1320, 80, 0, 0, Staff.LEFT),
        QuantizedNote(2, 64, 1380, 60, 80, 0, 0, Staff.LEFT),
    ]

    simplified, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        pedals=[PedalEvent(0, 0, True), PedalEvent(1441, 0, False)],
        measures=measures,
    )

    assert next(note for note in simplified if note.source_id == 1).duration == 1320


def test_long_metric_legato_closes_tiny_release_gap_without_pedal() -> None:
    meter = Meter(6, 8)
    measures = [
        MeasureSpan(index, index * meter.measure_length, meter.measure_length, meter)
        for index in range(2)
    ]
    notes = [QuantizedNote(1, 48, 0, 2760, 72, 0, 0, Staff.LEFT)]

    simplified, analysis, warnings = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
    )

    assert simplified[0].duration == meter.measure_length * 2
    assert analysis["legato_extended_note_count"] == 1
    assert any("提前十六分音符松键" in warning for warning in warnings)


def test_audio_transcription_closes_short_release_gap_without_deleting_attack() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 60, 0, 360, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 64, 480, 480, 82, 0, 0, Staff.RIGHT),
    ]

    normal, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
    )
    transcribed, analysis, warnings = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    assert normal[0].duration == 360
    assert transcribed[0].duration == 480
    assert len(transcribed) == len(notes)
    assert analysis["transcription_release_extended_note_count"] == 1
    assert any("音频转录" in warning for warning in warnings)


def test_audio_transcription_uses_first_strictly_later_staff_onset() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 60, 0, 360, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 67, 0, 240, 78, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 64, 480, 120, 82, 0, 0, Staff.RIGHT),
        QuantizedNote(4, 69, 720, 240, 84, 0, 0, Staff.RIGHT),
    ]

    transcribed, analysis, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    by_id = {note.source_id: note for note in transcribed}
    assert by_id[1].duration == 480
    assert by_id[2].duration == 240
    assert by_id[3].duration == 240
    assert analysis["transcription_release_extended_note_count"] == 2
