from itertools import pairwise

from app.core.models import Hand, QuantizedNote, Staff
from app.core.voices import assign_voices


def test_voice_assignment_never_exceeds_two_voices() -> None:
    notes = [
        QuantizedNote(1, 72, 0, 960, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 76, 240, 240, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 79, 480, 240, 80, 0, 0, Staff.RIGHT),
    ]
    assigned, counts, _ = assign_voices(notes, 2)

    assert counts["right"] <= 2
    assert max(note.voice for note in assigned) <= 2

    for voice in {note.voice for note in assigned}:
        voice_notes = sorted(
            [note for note in assigned if note.voice == voice], key=lambda note: note.onset
        )
        assert all(left.end <= right.onset for left, right in pairwise(voice_notes))


def test_voice_assignment_preserves_notes_when_three_layers_are_required() -> None:
    notes = [
        QuantizedNote(1, 84, 0, 960, 90, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 76, 240, 600, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(3, 72, 480, 240, 75, 0, 0, Staff.RIGHT),
    ]

    assigned, counts, warnings = assign_voices(notes, 2)

    assert len(assigned) == len(notes)
    assert sorted(note.duration for note in assigned) == [240, 600, 960]
    assert counts["right"] == 3
    assert any("避免截短或丢音" in warning for warning in warnings)

    for voice in {note.voice for note in assigned}:
        voice_notes = sorted(
            [note for note in assigned if note.voice == voice], key=lambda note: note.onset
        )
        assert all(left.end <= right.onset for left, right in pairwise(voice_notes))


def test_voice_assignment_never_merges_two_physical_hands_into_one_chord() -> None:
    notes = [
        QuantizedNote(1, 67, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.LEFT),
        QuantizedNote(2, 79, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]

    assigned, counts, _ = assign_voices(notes, 2)

    assert counts["right"] == 2
    assert len({note.voice for note in assigned}) == 2


def test_equal_duration_same_hand_notes_form_one_chord_despite_path_hints(
    monkeypatch,
) -> None:
    notes = [
        QuantizedNote(1, 72, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        QuantizedNote(2, 79, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]
    monkeypatch.setattr(
        "app.core.voices._partitura_voice_hints",
        lambda current: ([1, 2], []),
    )

    assigned, counts, _ = assign_voices(notes, 2)

    assert counts["right"] == 1
    assert {note.voice for note in assigned} == {1}


def test_non_overlapping_hands_can_reuse_one_notated_voice() -> None:
    notes = [
        QuantizedNote(1, 67, 0, 240, 80, 0, 0, Staff.RIGHT, hand=Hand.LEFT),
        QuantizedNote(2, 79, 240, 240, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]

    assigned, counts, _ = assign_voices(notes, 2)

    assert counts["right"] == 1
    assert {note.voice for note in assigned} == {1}
