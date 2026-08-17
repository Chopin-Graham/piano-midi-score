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
