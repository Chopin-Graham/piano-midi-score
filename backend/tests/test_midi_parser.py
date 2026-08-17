import pytest

from app.core.midi_parser import MidiParseError, parse_midi

from .midi_factory import (
    dominant_ensemble_midi_bytes,
    mixed_ensemble_midi_bytes,
    piano_midi_bytes,
)


def test_parse_type_one_piano_midi() -> None:
    parsed = parse_midi(piano_midi_bytes(two_tracks=True, measures=2))

    assert parsed.ticks_per_beat == 480
    assert len(parsed.notes) == 24
    assert {note.track for note in parsed.notes} == {1, 2}
    assert parsed.tempos[0].bpm == 96
    assert parsed.time_signatures[0].numerator == 4
    assert parsed.time_signatures[0].denominator == 4
    assert len(parsed.pedals) == 2


def test_parser_keeps_key_release_separate_from_pedal() -> None:
    parsed = parse_midi(piano_midi_bytes(two_tracks=False, measures=1, include_pedal=True))
    longest_note = max(parsed.notes, key=lambda note: note.duration_tick)

    assert longest_note.duration_tick == 430
    assert parsed.pedals[0].down is True
    assert parsed.pedals[-1].down is False


def test_parser_excludes_non_piano_and_percussion_parts() -> None:
    parsed = parse_midi(mixed_ensemble_midi_bytes())

    assert [note.pitch for note in parsed.notes] == [60]
    assert parsed.excluded_non_piano_note_count == 1
    assert parsed.excluded_percussion_note_count == 1
    assert len(parsed.warnings) >= 2


def test_parser_rejects_a_band_score_with_only_a_token_piano_part() -> None:
    with pytest.raises(MidiParseError, match="多乐器"):
        parse_midi(dominant_ensemble_midi_bytes())
