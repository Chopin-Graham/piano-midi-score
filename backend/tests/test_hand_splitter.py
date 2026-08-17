from app.core.hand_splitter import (
    assign_hands,
    mark_unredistributable_chords_for_arpeggiation,
)
from app.core.midi_parser import parse_midi
from app.core.models import Hand, Meter, PedalEvent, QuantizedNote
from app.core.options import ConversionOptions
from app.core.quantizer import quantize_midi

from .midi_factory import piano_midi_bytes


def _quantized(two_tracks: bool):
    parsed = parse_midi(piano_midi_bytes(two_tracks=two_tracks, measures=2))
    notes, _, _, _ = quantize_midi(parsed, Meter(), ConversionOptions())
    return notes


def test_prefers_two_clear_hand_tracks() -> None:
    assigned, analysis, _ = assign_hands(_quantized(True), ConversionOptions())

    assert analysis["method"] == "tracks"
    assert max(note.pitch for note in assigned if note.hand == Hand.LEFT) < min(
        note.pitch for note in assigned if note.hand == Hand.RIGHT
    )


def test_dynamic_programming_splits_single_track() -> None:
    assigned, analysis, warnings = assign_hands(_quantized(False), ConversionOptions())

    assert analysis["method"] == "dynamic_programming"
    assert {note.hand for note in assigned} == {Hand.RIGHT, Hand.LEFT}
    assert warnings


def test_ambiguous_tracks_do_not_override_extreme_registers() -> None:
    notes = [
        QuantizedNote(1, 40, 0, 480, 70, 1, 0),
        QuantizedNote(2, 76, 0, 480, 80, 1, 0),
        QuantizedNote(3, 45, 480, 480, 70, 2, 0),
        QuantizedNote(4, 80, 480, 480, 80, 2, 0),
    ]

    assigned, analysis, _ = assign_hands(
        notes,
        ConversionOptions(),
        {1: "Piano A", 2: "Piano B"},
    )

    assert analysis["method"] == "dynamic_programming"
    assert all(note.hand == Hand.LEFT for note in assigned if note.pitch < 52)
    assert all(note.hand == Hand.RIGHT for note in assigned if note.pitch > 72)


def test_score_export_tracks_allow_small_middle_c_overlap() -> None:
    low_pitches = [45, 48, 50, 52, 54, 55, 57, 60, 66, 69]
    high_pitches = [66, 68, 72, 74, 76, 80, 82, 84, 88, 92]
    notes = [
        QuantizedNote(index, pitch, index * 120, 100, 72, 1, 0)
        for index, pitch in enumerate(low_pitches)
    ]
    notes.extend(
        QuantizedNote(100 + index, pitch, index * 120, 100, 82, 2, 0)
        for index, pitch in enumerate(high_pitches)
    )

    assigned, analysis, _ = assign_hands(
        notes,
        ConversionOptions(),
        {1: "Piano", 2: "Piano"},
    )

    assert analysis["method"] == "tracks"
    assert sum(note.hand == Hand.LEFT for note in assigned if note.track == 1) >= 8
    assert sum(note.hand == Hand.RIGHT for note in assigned if note.track == 2) >= 8


def test_score_export_tracks_allow_six_semitone_overlap_when_medians_are_distant() -> None:
    low_pitches = [41, 45, 48, 52, 55, 57, 60, 64, 68, 69]
    high_pitches = [63, 66, 70, 74, 77, 80, 84, 88, 91, 95]
    notes = [
        QuantizedNote(index, pitch, index * 120, 100, 72, 1, 0)
        for index, pitch in enumerate(low_pitches)
    ]
    notes.extend(
        QuantizedNote(100 + index, pitch, index * 120, 100, 82, 2, 0)
        for index, pitch in enumerate(high_pitches)
    )

    _, analysis, _ = assign_hands(
        notes,
        ConversionOptions(),
        {1: "Piano", 2: "Piano"},
    )

    assert analysis["method"] == "tracks"


def test_small_third_track_is_assigned_without_key_error() -> None:
    notes = [
        *[
            QuantizedNote(index, 72 + index % 3, index * 120, 100, 80, 1, 0)
            for index in range(12)
        ],
        *[
            QuantizedNote(100 + index, 43 + index % 3, index * 120, 100, 70, 2, 0)
            for index in range(12)
        ],
        QuantizedNote(999, 60, 360, 120, 75, 3, 0),
    ]

    assigned, analysis, _ = assign_hands(
        notes,
        ConversionOptions(),
        {1: "Right Hand", 2: "Left Hand", 3: "Guide"},
    )

    assert analysis["method"] == "tracks"
    assert len(assigned) == len(notes)
    assert all(note.hand is not None for note in assigned)


def test_track_hints_can_be_overridden_to_avoid_an_unnecessary_tenth() -> None:
    notes = []
    source_id = 0
    for onset in range(0, 1920, 480):
        notes.extend(
            [
                QuantizedNote(source_id, 60, onset, 360, 80, 1, 0),
                QuantizedNote(source_id + 1, 76, onset, 360, 80, 1, 0),
                QuantizedNote(source_id + 2, 48, onset, 360, 70, 2, 0),
            ]
        )
        source_id += 3

    assigned, analysis, _ = assign_hands(
        notes,
        ConversionOptions(),
        {1: "Right Hand", 2: "Left Hand"},
    )

    assert analysis["method"] == "tracks"
    assert analysis["rebalanced_chord_notes"] >= 4
    for onset in range(0, 1920, 480):
        left = [note.pitch for note in assigned if note.onset == onset and note.hand == Hand.LEFT]
        right = [note.pitch for note in assigned if note.onset == onset and note.hand == Hand.RIGHT]
        assert max(left) - min(left) <= 12
        assert max(right) - min(right) <= 12


def test_track_hints_can_be_overridden_for_an_impossible_inner_finger_shape() -> None:
    notes = []
    source_id = 0
    for onset in range(0, 1920, 480):
        for pitch in (60, 61, 62, 72):
            notes.append(QuantizedNote(source_id, pitch, onset, 360, 80, 1, 0))
            source_id += 1
        notes.append(QuantizedNote(source_id, 55, onset, 360, 70, 2, 0))
        source_id += 1

    assigned, analysis, _ = assign_hands(
        notes,
        ConversionOptions(),
        {1: "Right Hand", 2: "Left Hand"},
    )

    assert analysis["method"] == "tracks"
    assert analysis["rebalanced_chord_notes"] >= 4
    for onset in range(0, 1920, 480):
        left = [note.pitch for note in assigned if note.onset == onset and note.hand == Hand.LEFT]
        right = [note.pitch for note in assigned if note.onset == onset and note.hand == Hand.RIGHT]
        assert {60, 61}.issubset(left)
        assert max(left) - min(left) <= 12
        assert max(right) - min(right) <= 12


def test_channel_pedal_prevents_unnecessary_reassignment_of_a_held_note() -> None:
    notes = []
    pedals = []
    source_id = 0
    for cycle in range(4):
        start = cycle * 1920
        notes.extend(
            [
                QuantizedNote(source_id, 60, start, 960, 80, 1, 0),
                QuantizedNote(source_id + 1, 48, start, 360, 70, 2, 0),
                QuantizedNote(source_id + 2, 77, start + 480, 240, 80, 1, 0),
                QuantizedNote(source_id + 3, 48, start + 480, 240, 70, 2, 0),
            ]
        )
        pedals.extend(
            [
                PedalEvent(start, 0, True),
                PedalEvent(start + 960, 0, False),
            ]
        )
        source_id += 4

    assigned, analysis, _ = assign_hands(
        notes,
        ConversionOptions(),
        {1: "Right Hand", 2: "Left Hand"},
        pedals,
    )

    assert analysis["method"] == "tracks"
    assert analysis["rebalanced_held_notes"] == 0
    assert all(
        note.hand == Hand.RIGHT
        for note in assigned
        if note.track == 1 and note.pitch == 60
    )


def test_unredistributable_wide_chord_is_marked_for_rolling() -> None:
    notes = [
        QuantizedNote(1, 29, 0, 480, 80, 0, 0, hand=Hand.LEFT),
        QuantizedNote(2, 41, 0, 480, 80, 0, 0, hand=Hand.LEFT),
        QuantizedNote(3, 46, 0, 480, 80, 0, 0, hand=Hand.LEFT),
    ]

    marked, count = mark_unredistributable_chords_for_arpeggiation(notes)

    assert count == 1
    assert all(note.arpeggiated for note in marked)
