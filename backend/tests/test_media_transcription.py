from __future__ import annotations

from io import BytesIO

import mido

from app.core.media_transcription import (
    _align_attack_columns,
    _attack_column_window,
    _beat_mapper,
    _clean_notes,
    _estimate_meter_and_downbeat,
    _postprocess_midi,
    _select_timeline_mapper,
    _TimedNote,
)

from .midi_factory import piano_midi_bytes


def test_clean_notes_removes_artifacts_without_losing_repeated_attacks() -> None:
    notes = [
        _TimedNote(10, 0.0, 0.5, 80),
        _TimedNote(60, 0.0, 0.03, 25),
        _TimedNote(60, 0.0, 0.50, 80),
        _TimedNote(60, 0.01, 0.60, 90),
        _TimedNote(61, 0.0, 0.50, 75),
        _TimedNote(61, 0.30, 0.70, 78),
    ]

    cleaned, analysis = _clean_notes(notes, 0.05)

    assert analysis == {
        "removed_out_of_range_notes": 1,
        "removed_short_notes": 1,
        "normalized_short_notes": 0,
        "merged_duplicate_notes": 1,
        "same_pitch_overlap_repairs": 1,
    }
    assert [(note.pitch, round(note.start, 3), round(note.end, 3)) for note in cleaned] == [
        (60, 0.0, 0.6),
        (61, 0.0, 0.295),
        (61, 0.3, 0.7),
    ]


def test_clean_notes_preserves_strong_short_attacks_by_normalizing_release() -> None:
    cleaned, analysis = _clean_notes(
        [
            _TimedNote(64, 1.0, 1.035, 82),
            _TimedNote(67, 1.0, 1.030, 24),
        ],
        0.055,
    )

    assert [(note.pitch, round(note.end - note.start, 3)) for note in cleaned] == [
        (64, 0.055)
    ]
    assert analysis["removed_short_notes"] == 1
    assert analysis["normalized_short_notes"] == 1


def test_dynamic_beat_mapper_places_detected_beats_on_integer_positions() -> None:
    notes = [_TimedNote(60, 0.2, 2.2, 80)]
    mapper, tempo = _beat_mapper([0.0, 0.5, 1.05, 1.55, 2.1, 2.6], notes)

    assert abs(mapper(0.5) - 1.0) < 1e-9
    assert abs(mapper(1.05) - 2.0) < 1e-9
    assert 105 < tempo < 125


def test_attack_columns_are_capped_and_preserve_note_durations() -> None:
    notes = [
        _TimedNote(60, 0.0, 0.5, 80),
        _TimedNote(64, 0.030, 0.330, 78),
        _TimedNote(60, 0.050, 0.450, 84),
        _TimedNote(67, 0.059, 0.259, 76),
        _TimedNote(72, 0.110, 0.310, 82),
    ]

    aligned, analysis = _align_attack_columns(notes, 0.060)

    assert [(note.pitch, round(note.start, 3)) for note in aligned] == [
        (60, 0.0),
        (64, 0.0),
        (67, 0.0),
        (72, 0.11),
    ]
    assert round(next(note for note in aligned if note.pitch == 64).end, 3) == 0.3
    assert analysis == {
        "attack_window_ms": 60.0,
        "attack_columns_before": 5,
        "attack_columns_after": 2,
        "aligned_attack_notes": 3,
        "merged_duplicate_attacks": 1,
    }


def test_tempo_selector_rejects_a_misleading_fast_beat_layer() -> None:
    notes = [
        _TimedNote(60 + index % 5, index * 0.125, index * 0.125 + 0.1, 80)
        for index in range(40)
    ]
    fast_eighth_pulse = [index * 0.417 for index in range(14)]

    mapper, tempo, method, analysis = _select_timeline_mapper(
        fast_eighth_pulse,
        notes,
        source_tempo=120,
    )

    assert method == "constant_tempo_source"
    assert tempo == 120
    assert abs(mapper(0.5) - round(mapper(0.5) * 4) / 4) < 1e-9
    assert any(
        candidate["method"] == "constant_tempo_beats_half"
        for candidate in analysis["tempo_candidates"]
    )


def test_tempo_selector_accepts_a_confident_dynamic_beat_grid() -> None:
    beat_times = [0.0, 0.42, 0.87, 1.27, 1.75, 2.16, 2.62, 3.03]
    notes = []
    for index, start in enumerate(beat_times[:-1]):
        midpoint = (start + beat_times[index + 1]) / 2
        notes.extend(
            [
                _TimedNote(48 + index % 4, start, start + 0.16, 76),
                _TimedNote(64 + index % 5, midpoint, midpoint + 0.15, 82),
            ]
        )

    mapper, tempo, method, analysis = _select_timeline_mapper(
        beat_times,
        notes,
        source_tempo=120,
    )

    assert method == "librosa_dynamic_beat_warp"
    assert tempo > 130
    assert abs(mapper(beat_times[4]) - 4.0) < 1e-9
    selected = next(
        candidate for candidate in analysis["tempo_candidates"] if candidate["selected"]
    )
    assert selected["grid_hit_rate"] > 0.9


def test_postprocess_outputs_aligned_piano_midi() -> None:
    midi_bytes, analysis, warnings = _postprocess_midi(
        piano_midi_bytes(two_tracks=True, jitter=0, measures=2),
        [index * 0.5 for index in range(12)],
        minimum_note_ms=40,
        backend="transkun",
    )
    midi = mido.MidiFile(file=BytesIO(midi_bytes))

    assert midi_bytes.startswith(b"MThd")
    assert analysis["clean_note_count"] > 0
    assert analysis["alignment_method"] == "constant_tempo_source"
    assert analysis["attack_columns_after"] <= analysis["attack_columns_before"]
    assert any(
        message.type == "time_signature"
        for track in midi.tracks
        for message in track
        if message.is_meta
    )
    assert warnings
    assert any(
        f"stable {analysis['tempo_bpm']:.1f} BPM timeline" in warning
        for warning in warnings
    )


def _accented_pattern(
    measures: int,
    beats_per_measure: int,
    *,
    phase_beats: float = 0.0,
) -> list[_TimedNote]:
    seconds_per_beat = 0.5  # 120 BPM
    notes: list[_TimedNote] = []
    for measure in range(measures):
        base = (measure * beats_per_measure + phase_beats) * seconds_per_beat
        notes.append(_TimedNote(36, base, base + 1.8 * seconds_per_beat, 96))
        for beat in range(1, beats_per_measure):
            onset = base + beat * seconds_per_beat
            for pitch in (60, 64, 67):
                notes.append(_TimedNote(pitch, onset, onset + 0.4 * seconds_per_beat, 68))
    return notes


def test_estimate_meter_detects_three_four() -> None:
    notes = _accented_pattern(12, 3)

    numerator, denominator, phase = _estimate_meter_and_downbeat(
        notes, lambda seconds: seconds * 2.0
    )

    assert (numerator, denominator) == (3, 4)


def test_estimate_meter_keeps_four_four_for_common_time() -> None:
    notes = _accented_pattern(12, 4)

    numerator, denominator, phase = _estimate_meter_and_downbeat(
        notes, lambda seconds: seconds * 2.0
    )

    assert (numerator, denominator, phase) == (4, 4, 0.0)


def test_estimate_meter_shifts_barlines_onto_downbeats() -> None:
    notes = _accented_pattern(12, 4, phase_beats=1.0)

    numerator, denominator, phase = _estimate_meter_and_downbeat(
        notes, lambda seconds: seconds * 2.0
    )

    assert (numerator, denominator) == (4, 4)
    assert phase == 1.0


def test_estimate_meter_detects_compound_six_eight() -> None:
    # Two dotted-quarter beats per bar, each split into three eighths.
    seconds_per_beat = 0.5
    notes: list[_TimedNote] = []
    for measure in range(12):
        base = measure * 2 * seconds_per_beat
        notes.append(_TimedNote(36, base, base + 0.75 * seconds_per_beat * 2, 96))
        for beat in range(2):
            for third in range(3):
                onset = base + (beat + third / 3) * seconds_per_beat
                notes.append(
                    _TimedNote(60 + (beat * 3 + third) % 5, onset, onset + 0.12, 72)
                )

    numerator, denominator, phase = _estimate_meter_and_downbeat(
        notes, lambda seconds: seconds * 2.0
    )

    assert (numerator, denominator) == (6, 8)


def test_estimate_meter_keeps_swing_out_of_compound_meter() -> None:
    # Swung 4/4: pairs at 0 and 2/3 of each beat, nothing on the 1/3 slot.
    seconds_per_beat = 0.5
    notes: list[_TimedNote] = []
    for measure in range(12):
        base = measure * 4 * seconds_per_beat
        notes.append(_TimedNote(36, base, base + 1.5 * seconds_per_beat, 96))
        for beat in range(4):
            first = base + beat * seconds_per_beat
            notes.append(_TimedNote(60, first, first + 0.3, 78))
            swung = first + (2 / 3) * seconds_per_beat
            notes.append(_TimedNote(64, swung, swung + 0.15, 70))

    numerator, denominator, _ = _estimate_meter_and_downbeat(
        notes, lambda seconds: seconds * 2.0
    )

    assert denominator == 4


def test_attack_column_window_preserves_fast_thirty_seconds() -> None:
    # 144 BPM content: a 32nd is ~52 ms; chord jitter stays below 25 ms.
    seconds_per_beat = 60.0 / 144
    notes: list[_TimedNote] = []
    for index in range(16):
        onset = index * seconds_per_beat / 8
        notes.append(_TimedNote(60 + index % 5, onset, onset + 0.04, 80))
    # A genuine chord with 20 ms jitter on the third 32nd.
    notes.append(_TimedNote(64, 3 * seconds_per_beat / 8 + 0.02, 3 * seconds_per_beat / 8 + 0.06, 76))
    window = _attack_column_window(notes, 120.0)
    assert 0.025 <= window <= 0.045

    aligned, analysis = _align_attack_columns(notes, window)

    assert analysis["attack_columns_after"] >= 16
    assert analysis["attack_columns_after"] < analysis["attack_columns_before"]
