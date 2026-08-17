from app.core.models import Hand, PedalEvent, QuantizedNote, Staff
from app.core.quality import evaluate_notation_quality


def test_quality_gate_flags_extreme_staff_misplacement() -> None:
    notes = [
        QuantizedNote(1, 40, 0, 480, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 80, 0, 480, 80, 0, 0, Staff.LEFT),
    ]

    quality, warnings = evaluate_notation_quality(notes, expected_note_count=2)

    assert quality["status"] == "needs_review"
    assert quality["extreme_staff_misplacements"] == {
        "right_below_e3": 1,
        "left_above_c5": 1,
    }
    assert warnings


def test_playability_uses_physical_hand_not_notation_staff() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 480, 80, 0, 0, Staff.LEFT, hand=Hand.RIGHT),
        QuantizedNote(2, 77, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=2)

    assert quality["status"] == "needs_review"
    assert quality["playability"]["oversized_chords"] == 1


def test_major_tenth_is_demanding_but_not_rejected() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        QuantizedNote(2, 76, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=2)

    assert quality["status"] == "playable_but_demanding"
    assert quality["playability"]["oversized_chords"] == 0
    assert quality["playability"]["stretched_chords"] == 1
    assert quality["playability"]["extreme_stretch_chords"] == 1


def test_ninth_is_extended_but_not_an_extreme_tenth() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        QuantizedNote(2, 74, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=2)

    assert quality["status"] == "playable_but_demanding"
    assert quality["playability"]["extended_chords"] == 1
    assert quality["playability"]["extreme_stretch_chords"] == 0


def test_eleventh_is_rejected_as_beyond_one_hand() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        QuantizedNote(2, 77, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=2)

    assert quality["status"] == "needs_review"
    assert quality["playability"]["oversized_chords"] == 1


def test_explicitly_rolled_eleventh_is_not_treated_as_simultaneous() -> None:
    notes = [
        QuantizedNote(
            1,
            60,
            0,
            480,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
            arpeggiated=True,
        ),
        QuantizedNote(
            2,
            77,
            0,
            480,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
            arpeggiated=True,
        ),
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=2)

    assert quality["playability"]["oversized_chords"] == 0
    assert quality["playability"]["arpeggiated_wide_chords"] == 1


def test_more_than_five_keys_in_one_hand_is_rejected() -> None:
    notes = [
        QuantizedNote(index, 60 + index, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT)
        for index in range(6)
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=6)

    assert quality["status"] == "needs_review"
    assert quality["playability"]["too_many_notes_chords"] == 1


def test_inner_finger_shape_can_fail_inside_an_octave() -> None:
    notes = [
        QuantizedNote(index, pitch, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT)
        for index, pitch in enumerate((60, 61, 62, 72), start=1)
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=4)

    assert quality["status"] == "needs_review"
    assert quality["playability"]["maximum_observed_span_semitones"] == 12
    assert quality["playability"]["oversized_chords"] == 0
    assert quality["playability"]["too_many_notes_chords"] == 0
    assert quality["playability"]["awkward_chord_shapes"] == 0
    assert quality["playability"]["unplayable_chord_shapes"] == 1


def test_ordinary_four_note_octave_chord_has_a_five_finger_shape() -> None:
    notes = [
        QuantizedNote(index, pitch, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT)
        for index, pitch in enumerate((60, 64, 67, 72), start=1)
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=4)

    assert quality["status"] == "excellent"
    assert quality["playability"]["awkward_chord_shapes"] == 0
    assert quality["playability"]["unplayable_chord_shapes"] == 0


def test_large_hand_shape_is_demanding_not_rejected() -> None:
    notes = [
        QuantizedNote(index, pitch, 0, 480, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT)
        for index, pitch in enumerate((44, 51, 54, 56), start=1)
    ]

    quality, _ = evaluate_notation_quality(notes, expected_note_count=4)

    assert quality["status"] == "playable_but_demanding"
    assert quality["playability"]["awkward_chord_shapes"] == 1
    assert quality["playability"]["unplayable_chord_shapes"] == 0


def test_pedal_can_release_fingers_during_a_written_sustain() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 960, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        QuantizedNote(2, 77, 480, 240, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]
    pedals = [PedalEvent(0, 0, True), PedalEvent(960, 0, False)]

    quality, _ = evaluate_notation_quality(
        notes,
        expected_note_count=2,
        tempo_bpm=120,
        pedals=pedals,
    )

    assert quality["playability"]["held_oversized_spans"] == 0
    assert quality["playability"]["pedal_supported_wide_sustains"] == 1


def test_pedal_on_another_channel_cannot_release_the_held_finger() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 960, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        QuantizedNote(2, 77, 480, 240, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]
    pedals = [PedalEvent(0, 1, True), PedalEvent(960, 1, False)]

    quality, _ = evaluate_notation_quality(
        notes,
        expected_note_count=2,
        pedals=pedals,
    )

    assert quality["status"] == "needs_review"
    assert quality["playability"]["held_oversized_spans"] == 1
    assert quality["playability"]["pedal_supported_wide_sustains"] == 0


def test_pedal_must_remain_down_until_the_held_note_ends() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 960, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        QuantizedNote(2, 77, 480, 240, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]
    pedals = [PedalEvent(0, 0, True), PedalEvent(720, 0, False)]

    quality, _ = evaluate_notation_quality(
        notes,
        expected_note_count=2,
        pedals=pedals,
    )

    assert quality["status"] == "needs_review"
    assert quality["playability"]["held_oversized_spans"] == 1
    assert quality["playability"]["pedal_supported_wide_sustains"] == 0


def test_readability_duration_cleanup_cannot_hide_a_physical_hold() -> None:
    physical_notes = [
        QuantizedNote(1, 60, 0, 960, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        QuantizedNote(2, 77, 480, 240, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
    ]
    notated_notes = [
        QuantizedNote(1, 60, 0, 480, 80, 0, 0, Staff.RIGHT, hand=Hand.RIGHT),
        physical_notes[1],
    ]

    quality, _ = evaluate_notation_quality(
        notated_notes,
        expected_note_count=2,
        playability_notes=physical_notes,
    )

    assert quality["status"] == "needs_review"
    assert quality["playability"]["held_oversized_spans"] == 1
