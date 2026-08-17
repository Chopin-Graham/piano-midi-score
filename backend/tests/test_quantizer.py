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
