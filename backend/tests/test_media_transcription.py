from __future__ import annotations

from io import BytesIO

import mido

from app.core.media_transcription import (
    _align_attack_columns,
    _attack_column_window,
    _beat_mapper,
    _BeatCandidate,
    _clean_notes,
    _estimate_meter_and_downbeat,
    _postprocess_midi,
    _select_timeline_mapper,
    _TimedNote,
    _write_aligned_midi,
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


def test_attack_columns_keep_fast_stepwise_run_in_sequence() -> None:
    notes = [
        _TimedNote(
            60 + index,
            index * 0.031,
            index * 0.031 + 0.052,
            82,
        )
        for index in range(7)
    ]

    aligned, analysis = _align_attack_columns(notes, 0.040)

    assert [round(note.start, 3) for note in aligned] == [
        0.0,
        0.031,
        0.062,
        0.093,
        0.124,
        0.155,
        0.186,
    ]
    assert analysis["aligned_attack_notes"] == 0
    assert analysis["attack_columns_after"] == 7


def test_attack_columns_still_align_isolated_short_interval_dyad() -> None:
    notes = [
        _TimedNote(48, 0.0, 0.4, 72),
        _TimedNote(60, 1.0, 1.5, 84),
        _TimedNote(64, 1.018, 1.49, 81),
        _TimedNote(72, 2.0, 2.3, 70),
    ]

    aligned, analysis = _align_attack_columns(notes, 0.040)

    dyad = [note for note in aligned if note.pitch in {60, 64}]
    assert {note.start for note in dyad} == {1.0}
    assert analysis["aligned_attack_notes"] == 1


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


def test_tempo_selector_prefers_continuous_adaptive_accelerando() -> None:
    adaptive_beats = [0.0]
    period = 0.68
    for _ in range(18):
        adaptive_beats.append(adaptive_beats[-1] + period)
        period *= 0.965
    notes: list[_TimedNote] = []
    for index, (left, right) in enumerate(
        zip(adaptive_beats, adaptive_beats[1:], strict=False)
    ):
        midpoint = (left + right) / 2
        notes.extend(
            [
                _TimedNote(48 + index % 4, left, left + 0.12, 78),
                _TimedNote(64 + index % 5, midpoint, midpoint + 0.1, 82),
            ]
        )
    stable_beats = [index * 0.5 for index in range(24)]

    mapper, _, method, analysis = _select_timeline_mapper(
        stable_beats,
        notes,
        source_tempo=120.0,
        beat_candidates=[
            _BeatCandidate(
                "librosa_adaptive_tempo_warp",
                tuple(adaptive_beats),
            )
        ],
    )

    assert method == "librosa_adaptive_tempo_warp"
    assert abs(mapper(adaptive_beats[12]) - 12.0) < 1e-9
    selected = next(
        candidate for candidate in analysis["tempo_candidates"] if candidate["selected"]
    )
    assert selected["tempo_continuity_penalty"] < 0.002


def test_tempo_selector_rejects_jittery_adaptive_layer_switch() -> None:
    stable_beats = [index * 0.5 for index in range(24)]
    jittery_beats = [0.0]
    for index in range(23):
        jittery_beats.append(jittery_beats[-1] + (0.4 if index % 2 == 0 else 0.6))
    notes: list[_TimedNote] = []
    for index, (left, right) in enumerate(
        zip(jittery_beats, jittery_beats[1:], strict=False)
    ):
        midpoint = (left + right) / 2
        notes.extend(
            [
                _TimedNote(48 + index % 4, left, left + 0.12, 78),
                _TimedNote(64 + index % 5, midpoint, midpoint + 0.1, 82),
            ]
        )

    _, _, method, analysis = _select_timeline_mapper(
        stable_beats,
        notes,
        source_tempo=120.0,
        beat_candidates=[
            _BeatCandidate(
                "librosa_adaptive_tempo_warp",
                tuple(jittery_beats),
            )
        ],
    )

    assert method != "librosa_adaptive_tempo_warp"
    adaptive = next(
        candidate
        for candidate in analysis["tempo_candidates"]
        if candidate["method"] == "librosa_adaptive_tempo_warp"
    )
    assert adaptive["tempo_continuity_penalty"] == 0.02


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


def test_estimate_meter_keeps_waltz_when_bass_moves_on_every_beat() -> None:
    notes: list[_TimedNote] = []
    for measure in range(24):
        base = measure * 3.0
        for beat, (bass_pitch, velocity, duration) in enumerate(
            ((36, 100, 1.55), (43, 76, 0.75), (47, 78, 0.75))
        ):
            onset = base + beat
            notes.append(
                _TimedNote(
                    bass_pitch,
                    onset,
                    onset + duration,
                    velocity,
                )
            )
            for pitch in (60 + beat, 64 + beat, 67 + beat):
                notes.append(
                    _TimedNote(
                        pitch,
                        onset,
                        onset + 0.42,
                        78 if beat == 0 else 68,
                    )
                )

    numerator, denominator, phase = _estimate_meter_and_downbeat(
        notes,
        lambda value: value,
    )

    assert (numerator, denominator, phase) == (3, 4, 0.0)


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


def test_estimate_meter_pulls_sixteenth_grid_slip_back() -> None:
    # A beat tracker that locked a sixteenth early leaves every onset a
    # quarter-of-a-beat off the true grid; integer-beat phase hypotheses can
    # never catch that, so the search must reach sixteenth resolution.
    seconds_per_beat = 0.5
    notes: list[_TimedNote] = []
    slip = 0.25 * seconds_per_beat
    for measure in range(12):
        base = measure * 4 * seconds_per_beat + slip
        notes.append(_TimedNote(36, base, base + 1.6 * seconds_per_beat, 96))
        for beat in range(1, 4):
            onset = base + beat * seconds_per_beat
            for pitch in (60, 64, 67):
                notes.append(_TimedNote(pitch, onset, onset + 0.4 * seconds_per_beat, 68))

    numerator, denominator, phase = _estimate_meter_and_downbeat(
        notes, lambda seconds: seconds * 2.0
    )

    assert (numerator, denominator) == (4, 4)
    assert abs(phase - 0.25) < 0.03


def test_estimate_meter_resolves_beat_three_bass_as_one_beat_pickup() -> None:
    notes: list[_TimedNote] = []
    for measure in range(48):
        base = measure * 4.0
        # The true downbeat sits one beat after the opening pickup.
        notes.append(_TimedNote(50, base + 1.0, base + 1.8, 90))
        notes.append(_TimedNote(62, base + 2.0, base + 2.35, 60))
        # Later beat-three bass attacks are slightly heavier overall, which
        # would otherwise create a false three-beat pickup.
        if measure < 16:
            notes.append(_TimedNote(60, base + 3.0, base + 3.35, 60))
        else:
            notes.append(_TimedNote(40, base + 3.0, base + 3.35, 75))
        notes.append(_TimedNote(64, base + 4.0, base + 4.35, 60))

    numerator, denominator, phase = _estimate_meter_and_downbeat(notes, lambda value: value)

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
    # A neighbouring semitone arrives 20 ms after the third 32nd.  Inside a
    # continuous scalar run this is another melodic attack, not a dyad whose
    # onset should be manufactured by alignment.
    notes.append(_TimedNote(64, 3 * seconds_per_beat / 8 + 0.02, 3 * seconds_per_beat / 8 + 0.06, 76))
    window = _attack_column_window(notes, 120.0)
    assert 0.025 <= window <= 0.045

    aligned, analysis = _align_attack_columns(notes, window)

    assert analysis["attack_columns_after"] >= 16
    assert analysis["attack_columns_after"] == analysis["attack_columns_before"]


def test_tempo_selector_does_not_treat_note_density_as_tempo() -> None:
    # The pulse stays at 120 BPM.  Only the written texture changes from
    # eighths to sixteenths; a note-IOI time warp would incorrectly halve the
    # first region or double the second one.
    notes: list[_TimedNote] = []
    for beat in range(16):
        subdivisions = 2 if beat < 8 else 4
        for subdivision in range(subdivisions):
            start = beat * 0.5 + subdivision * 0.5 / subdivisions
            notes.append(
                _TimedNote(60 + (beat + subdivision) % 8, start, start + 0.1, 76)
            )
    beat_times = [index * 0.5 for index in range(18)]

    mapper, tempo, method, analysis = _select_timeline_mapper(
        beat_times,
        notes,
        source_tempo=120.0,
    )

    first = min(note.start for note in notes)
    last = max(note.start for note in notes)
    assert method in {"constant_tempo_source", "librosa_dynamic_beat_warp"}
    assert 118.0 <= tempo <= 122.0
    assert abs((mapper(last) - mapper(first)) - (last - first) * 2.0) < 0.25
    assert all(
        candidate["method"] != "ioi_dynamic_tempo_map"
        for candidate in analysis["tempo_candidates"]
    )


def test_late_tempo_region_does_not_replace_initial_tempo() -> None:
    midi_bytes = _write_aligned_midi(
        [_TimedNote(60, 0.0, 1.0, 80)],
        [],
        lambda seconds: seconds * 2.0,
        120.0,
        "test",
        # The first tick of measure 2 is no longer part of the opening measure.
        tempo_events=[(4.0, 80.0)],
    )
    midi = mido.MidiFile(file=BytesIO(midi_bytes))
    tempo_messages = [
        message
        for message in midi.tracks[0]
        if message.type == "set_tempo"
    ]

    assert round(mido.tempo2bpm(tempo_messages[0].tempo)) == 120
    assert round(mido.tempo2bpm(tempo_messages[1].tempo)) == 80
