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
    assert by_id[1].duration == 240
    assert by_id[2].duration == 480
    assert by_id[3].duration == 240
    assert analysis["transcription_release_extended_note_count"] == 2


def test_audio_transcription_extends_short_chord_to_quarter_note_cell() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 60, 0, 120, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 64, 0, 120, 82, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 67, 480, 480, 84, 0, 0, Staff.RIGHT),
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
    assert by_id[2].duration == 480
    assert analysis["transcription_release_extended_note_count"] == 2


def test_audio_transcription_normalizes_repeated_eighth_accompaniment() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 48, 0, 360, 72, 0, 0, Staff.LEFT),
        QuantizedNote(2, 50, 240, 360, 74, 0, 0, Staff.LEFT),
        QuantizedNote(3, 52, 480, 120, 76, 0, 0, Staff.LEFT),
    ]

    transcribed, analysis, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    by_id = {note.source_id: note for note in transcribed}
    assert [by_id[source_id].duration for source_id in (1, 2, 3)] == [240, 240, 240]
    assert analysis["transcription_release_normalized_note_count"] == 2


def test_audio_transcription_preserves_melody_over_accompaniment() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 84, 0, 960, 88, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 67, 240, 240, 70, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 69, 480, 240, 72, 0, 0, Staff.RIGHT),
    ]

    transcribed, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    assert next(note for note in transcribed if note.source_id == 1).duration == 960


def test_audio_transcription_extends_short_melody_across_lower_accompaniment() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 84, 0, 120, 88, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 67, 240, 240, 70, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 86, 480, 240, 90, 0, 0, Staff.RIGHT),
    ]

    transcribed, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    assert next(note for note in transcribed if note.source_id == 1).duration == 480


def test_audio_transcription_allows_pedal_supported_low_bass() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 40, 0, 120, 72, 0, 0, Staff.LEFT),
        QuantizedNote(2, 60, 240, 240, 76, 0, 0, Staff.LEFT),
        QuantizedNote(3, 43, 480, 240, 74, 0, 0, Staff.LEFT),
    ]

    transcribed, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        pedals=[PedalEvent(0, 0, True), PedalEvent(481, 0, False)],
        measures=measures,
        transcription_mode=True,
    )

    assert next(note for note in transcribed if note.source_id == 1).duration == 480


def test_audio_transcription_ends_before_repeated_same_pitch_attack() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 60, 0, 360, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 60, 240, 240, 82, 0, 0, Staff.RIGHT),
    ]

    transcribed, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    by_id = {note.source_id: note for note in transcribed}
    assert by_id[1].end == by_id[2].onset


def test_audio_transcription_keeps_melodic_chord_tone_longer_than_inner_tones() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 60, 0, 120, 76, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 64, 0, 360, 78, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 67, 0, 240, 84, 0, 0, Staff.RIGHT),
        QuantizedNote(4, 69, 480, 240, 86, 0, 0, Staff.RIGHT),
    ]

    transcribed, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    by_id = {note.source_id: note for note in transcribed}
    assert by_id[1].duration == 240
    assert by_id[2].duration == 240
    assert by_id[3].duration == 480


def test_audio_transcription_does_not_invent_dotted_eighths_for_missing_attack() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 60, 0, 120, 76, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 64, 0, 120, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 62, 360, 120, 78, 0, 0, Staff.RIGHT),
        QuantizedNote(4, 65, 360, 120, 82, 0, 0, Staff.RIGHT),
    ]

    transcribed, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    assert sorted(note.duration for note in transcribed) == [240, 240, 240, 240]


def test_audio_transcription_closes_aligned_long_release_to_eighth_boundary() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [QuantizedNote(1, 60, 0, 600, 76, 0, 0, Staff.RIGHT)]

    transcribed, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    assert transcribed[0].duration == 720


def test_audio_transcription_closes_monophonic_offbeat_release_to_next_attack() -> None:
    meter = Meter(4, 4)
    measures = [MeasureSpan(0, 0, meter.measure_length, meter)]
    notes = [
        QuantizedNote(1, 72, 120, 240, 82, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 74, 480, 240, 84, 0, 0, Staff.RIGHT),
    ]

    transcribed, _, _ = simplify_polyphonic_durations(
        notes,
        max_voices=2,
        style="clean",
        measures=measures,
        transcription_mode=True,
    )

    assert next(note for note in transcribed if note.source_id == 1).duration == 360
