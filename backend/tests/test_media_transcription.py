from __future__ import annotations

from io import BytesIO

import mido

from app.core.media_transcription import (
    _align_attack_columns,
    _beat_mapper,
    _clean_notes,
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
    assert len(warnings) == 1
    assert f"stable {analysis['tempo_bpm']:.1f} BPM timeline" in warnings[0]
