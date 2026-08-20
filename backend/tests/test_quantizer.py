from app.core.midi_parser import parse_midi
from app.core.models import Meter, ParsedMidi, RawNote, TempoEvent, TimeSignatureEvent
from app.core.options import ConversionOptions
from app.core.quantizer import quantize_midi

from .midi_factory import compound_piano_midi_bytes, piano_midi_bytes


def test_quantizer_removes_human_jitter_without_creating_tiny_values() -> None:
    parsed = parse_midi(piano_midi_bytes(two_tracks=False, jitter=13, measures=1))
    notes, decisions, _, _ = quantize_midi(
        parsed,
        Meter(4, 4),
        ConversionOptions(style="balanced", allow_triplets=False),
    )

    assert decisions[0].name in {"eighth", "sixteenth"}
    assert all(note.onset % 120 == 0 for note in notes)
    assert min(note.duration for note in notes) >= 120


def test_clean_mode_avoids_thirty_second_grid() -> None:
    parsed = parse_midi(piano_midi_bytes(two_tracks=False, jitter=7, measures=2))
    _, decisions, _, _ = quantize_midi(
        parsed,
        Meter(4, 4),
        ConversionOptions(style="clean"),
    )

    assert all(decision.name != "thirty_second" for decision in decisions)


def test_compound_meter_uses_written_subdivisions_not_fake_triplets() -> None:
    parsed = parse_midi(compound_piano_midi_bytes())
    notes, decisions, _, _ = quantize_midi(
        parsed,
        Meter(6, 8),
        ConversionOptions(style="balanced", allow_triplets=True),
    )

    assert notes
    assert all(not decision.triplet for decision in decisions)
    assert all("triplet" not in decision.name for decision in decisions)


def test_clean_mode_collapses_tiny_playable_roll_into_one_chord_attack() -> None:
    parsed = ParsedMidi(
        ticks_per_beat=480,
        notes=[
            RawNote(index, pitch, onset, 1370 + index, 64, 0, 0)
            for index, (pitch, onset) in enumerate(
                [(60, 0), (64, 24), (67, 50), (72, 74)],
                start=1,
            )
        ],
        tempos=[TempoEvent(0, 500_000)],
        time_signatures=[TimeSignatureEvent(0, 6, 8)],
    )

    notes, _, _, warnings = quantize_midi(
        parsed,
        Meter(6, 8),
        ConversionOptions(style="clean"),
    )

    assert {note.onset for note in notes} == {0}
    assert len(notes) == 4
    assert any("微时差音归并为和弦起音" in warning for warning in warnings)


def test_clean_mode_keeps_deliberate_sixteenth_arpeggio_separate() -> None:
    parsed = ParsedMidi(
        ticks_per_beat=480,
        notes=[
            RawNote(index, pitch, onset, 1440, 64, 0, 0)
            for index, (pitch, onset) in enumerate(
                [(60, 0), (64, 120), (67, 240)],
                start=1,
            )
        ],
        tempos=[TempoEvent(0, 500_000)],
        time_signatures=[TimeSignatureEvent(0, 6, 8)],
    )

    notes, _, _, _ = quantize_midi(
        parsed,
        Meter(6, 8),
        ConversionOptions(style="clean"),
    )

    assert {note.onset for note in notes} == {0, 120, 240}


def _audio_parsed(notes: list[RawNote]) -> ParsedMidi:
    return ParsedMidi(
        ticks_per_beat=480,
        notes=notes,
        tempos=[TempoEvent(0, 500_000)],
        time_signatures=[TimeSignatureEvent(0, 4, 4)],
    )


def test_audio_auto_detects_genuine_triplet_measures() -> None:
    notes: list[RawNote] = []
    for measure in range(6):
        base = measure * 1920
        for beat in range(4):
            for member in range(3):
                onset = base + beat * 480 + member * 160
                notes.append(
                    RawNote(len(notes), 60 + member % 3, onset, onset + 140, 80, 0, 0)
                )
    options = ConversionOptions(
        style="clean",
        audio_transcription=True,
        allow_triplets=False,
    )

    _, decisions, _, warnings = quantize_midi(
        _audio_parsed(notes), Meter(4, 4), options
    )

    assert any(decision.triplet for decision in decisions)
    assert any("三连音" in warning for warning in warnings)


def test_audio_auto_keeps_binary_grid_for_noisy_binary_content() -> None:
    notes: list[RawNote] = []
    for measure in range(6):
        base = measure * 1920
        for step in range(16):
            jitter = 12 if (measure + step) % 2 else -12
            onset = base + step * 120 + jitter
            notes.append(
                RawNote(len(notes), 60 + step % 5, onset, onset + 100, 80, 0, 0)
            )
    options = ConversionOptions(
        style="clean",
        audio_transcription=True,
        allow_triplets=False,
    )

    _, decisions, _, _ = quantize_midi(_audio_parsed(notes), Meter(4, 4), options)

    assert all(not decision.triplet for decision in decisions)


def test_audio_triplet_probe_does_not_unlock_quintuplet_grids() -> None:
    notes: list[RawNote] = []
    for measure in range(5):
        base = measure * 1920
        for beat in range(4):
            for member in range(3):
                onset = base + beat * 480 + member * 160
                notes.append(
                    RawNote(len(notes), 60 + member, onset, onset + 140, 80, 0, 0)
                )

    quintuplet_measure = 5 * 1920
    for beat in range(4):
        for member in range(5):
            onset = quintuplet_measure + beat * 480 + member * 96
            notes.append(
                RawNote(len(notes), 67 + member, onset, onset + 86, 80, 0, 0)
            )

    _, decisions, _, warnings = quantize_midi(
        _audio_parsed(notes),
        Meter(4, 4),
        ConversionOptions(
            style="clean",
            audio_transcription=True,
            allow_triplets=False,
        ),
    )

    assert any("自动启用三连音" in warning for warning in warnings)
    assert all("quintuplet" not in decision.name for decision in decisions)


def test_audio_auto_rejects_weak_sextuplet_fit_in_favor_of_binary_grid() -> None:
    notes: list[RawNote] = []
    for measure in range(5):
        base = measure * 1920
        for beat in range(4):
            for member in range(3):
                onset = base + beat * 480 + member * 160
                notes.append(
                    RawNote(len(notes), 60 + member, onset, onset + 140, 80, 0, 0)
                )

    noisy_measure = 5 * 1920
    for member, offset in enumerate((8, 82, 145, 282, 346, 396)):
        onset = noisy_measure + offset
        notes.append(
            RawNote(len(notes), 72 + member, onset, onset + 67, 80, 0, 0)
        )

    quantized, decisions, _, _ = quantize_midi(
        _audio_parsed(notes),
        Meter(4, 4),
        ConversionOptions(
            style="clean",
            audio_transcription=True,
            allow_triplets=False,
        ),
    )

    assert decisions[5].triplet is False
    target = [note for note in quantized if note.onset >= noisy_measure]
    assert target
    assert all(note.onset % 60 == 0 for note in target)


def test_audio_auto_does_not_complete_sparse_tuplet_with_rests() -> None:
    notes: list[RawNote] = []
    for measure in range(5):
        base = measure * 1920
        for beat in range(4):
            for member in range(3):
                onset = base + beat * 480 + member * 160
                notes.append(
                    RawNote(len(notes), 60 + member, onset, onset + 140, 80, 0, 0)
                )

    sparse_measure = 5 * 1920
    for member, offset in enumerate((0, 160)):
        onset = sparse_measure + offset
        notes.append(
            RawNote(len(notes), 72 + member, onset, onset + 140, 80, 0, 0)
        )

    _, decisions, _, warnings = quantize_midi(
        _audio_parsed(notes),
        Meter(4, 4),
        ConversionOptions(
            style="clean",
            audio_transcription=True,
            allow_triplets=False,
        ),
    )

    assert any("自动启用三连音" in warning for warning in warnings)
    assert decisions[5].triplet is False


def test_per_lane_per_beat_grids_keep_quintuplet_run_exact() -> None:
    # Track 0 plays a five-per-beat run (96-tick spacing) while track 1 holds
    # a steady dotted-eighth pattern.  A measure-wide grid would crush one of
    # them; per-lane, per-beat selection must keep both intact.
    notes: list[RawNote] = []
    for index in range(5):
        onset = index * 96
        notes.append(RawNote(len(notes), 72 + index, onset, onset + 90, 75, 0, 0))
    for index, onset in enumerate((0, 360, 720, 960, 1440)):
        notes.append(RawNote(len(notes), 40 + index, onset, onset + 340, 80, 1, 0))

    quantized, decisions, _, _ = quantize_midi(
        _audio_parsed(notes), Meter(4, 4), ConversionOptions(style="clean")
    )

    run = sorted(
        (note for note in quantized if note.track == 0), key=lambda note: note.onset
    )
    assert [note.onset for note in run] == [0, 96, 192, 288, 384]
    assert all(note.duration == 96 for note in run)
    assert any(decision.triplet for decision in decisions)


def test_audio_sparse_timing_jitter_does_not_select_64th_grid() -> None:
    notes: list[RawNote] = []
    for measure in range(6):
        base = measure * 1920
        for beat in range(4):
            beat_start = base + beat * 480
            jitter = 18 if (measure + beat) % 2 else -14
            for onset in (beat_start + 16, beat_start + 240 + jitter):
                notes.append(
                    RawNote(len(notes), 60 + beat % 5, onset, onset + 105, 80, 0, 0)
                )

    quantized, decisions, _, _ = quantize_midi(
        _audio_parsed(notes),
        Meter(4, 4),
        ConversionOptions(
            style="clean",
            audio_transcription=True,
            allow_triplets=False,
        ),
    )

    assert all(note.onset % 120 == 0 for note in quantized)
    assert all(decision.step >= 120 for decision in decisions)


def test_audio_true_32nd_run_keeps_fine_grid() -> None:
    notes: list[RawNote] = []
    for measure in range(2):
        base = measure * 1920
        for beat in range(4):
            for member in range(8):
                onset = base + beat * 480 + member * 60
                notes.append(
                    RawNote(len(notes), 60 + member % 7, onset, onset + 50, 82, 0, 0)
                )

    quantized, decisions, _, _ = quantize_midi(
        _audio_parsed(notes),
        Meter(4, 4),
        ConversionOptions(
            style="clean",
            audio_transcription=True,
            allow_triplets=False,
        ),
    )

    assert len({note.onset for note in quantized}) == 64
    assert any(decision.step <= 60 for decision in decisions)
