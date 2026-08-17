from app.core.models import ClefChange, Hand, MeasureSpan, Meter, QuantizedNote, Staff
from app.core.staff_assigner import assign_staves, repair_staves_for_planned_clefs


def _note(source_id: int, pitch: int, hand: Hand) -> QuantizedNote:
    return QuantizedNote(source_id, pitch, 0, 480, 80, 0, 0, hand=hand)


def test_physical_right_hand_can_be_notated_in_bass_staff() -> None:
    notes = [
        _note(1, 26, Hand.LEFT),
        _note(2, 38, Hand.LEFT),
        _note(3, 50, Hand.RIGHT),
        _note(4, 62, Hand.RIGHT),
    ]

    assigned, analysis, _ = assign_staves(notes)

    assert all(note.hand == Hand.RIGHT for note in assigned if note.pitch in {50, 62})
    assert all(note.staff == Staff.LEFT for note in assigned if note.pitch in {50, 62})
    assert analysis["cross_staff_hand_notes"] >= 2
    assert analysis["extreme_repairs"] == 0


def test_long_two_hand_passage_is_restored_to_two_staves() -> None:
    notes = []
    source_id = 1
    for onset in range(0, 960, 120):
        notes.append(
            QuantizedNote(
                source_id,
                72,
                onset,
                120,
                80,
                0,
                0,
                hand=Hand.LEFT,
            )
        )
        source_id += 1
        notes.append(
            QuantizedNote(
                source_id,
                84,
                onset,
                120,
                80,
                1,
                0,
                hand=Hand.RIGHT,
            )
        )
        source_id += 1

    assigned, analysis, _ = assign_staves(notes)

    assert all(note.staff == Staff.LEFT for note in assigned if note.hand == Hand.LEFT)
    assert all(note.staff == Staff.RIGHT for note in assigned if note.hand == Hand.RIGHT)
    assert analysis["clarified_hand_notes"] == 8


def test_trusted_score_tracks_anchor_notes_to_their_reference_staves() -> None:
    notes = [
        QuantizedNote(1, 69, 0, 480, 80, 1, 0, hand=Hand.LEFT),
        QuantizedNote(2, 76, 0, 480, 80, 0, 0, hand=Hand.RIGHT),
        QuantizedNote(3, 50, 480, 480, 80, 0, 0, hand=Hand.RIGHT),
    ]

    assigned, analysis, _ = assign_staves(
        notes,
        {0: Staff.RIGHT, 1: Staff.LEFT},
    )

    assert analysis["method"] == "source_track_hints_with_dynamic_clefs"
    assert next(note for note in assigned if note.source_id == 1).staff == Staff.LEFT
    assert next(note for note in assigned if note.source_id == 2).staff == Staff.RIGHT
    # Trusted score tracks stay intact; a later clef-planning pass makes an
    # extreme passage readable without erasing the source staff structure.
    assert next(note for note in assigned if note.source_id == 3).staff == Staff.RIGHT
    assert analysis["extreme_repairs"] == 0


def test_audio_mode_can_lock_hands_to_staves_and_delegate_register_to_clefs() -> None:
    notes = [
        _note(1, 43, Hand.RIGHT),
        _note(2, 76, Hand.LEFT),
    ]

    assigned, analysis, _ = assign_staves(notes, lock_hands_to_staves=True)

    assert next(note for note in assigned if note.hand == Hand.RIGHT).staff == Staff.RIGHT
    assert next(note for note in assigned if note.hand == Hand.LEFT).staff == Staff.LEFT
    assert analysis["method"] == "hand_locked_dynamic_clefs"
    assert analysis["cross_staff_hand_notes"] == 0


def test_clef_aware_repair_keeps_high_notes_on_lower_treble_staff() -> None:
    notes = [
        QuantizedNote(1, 79, 0, 480, 80, 1, 0, Staff.LEFT, hand=Hand.LEFT),
    ]
    measures = [MeasureSpan(0, 0, 1920, Meter())]
    clefs = [
        ClefChange(0, Staff.RIGHT, "treble"),
        ClefChange(0, Staff.LEFT, "treble"),
    ]

    repaired, count = repair_staves_for_planned_clefs(notes, measures, clefs)

    assert repaired[0].staff == Staff.LEFT
    assert count == 0


def test_clef_aware_repair_moves_a_high_outlier_off_lower_bass_staff() -> None:
    notes = [
        QuantizedNote(1, 79, 0, 480, 80, 1, 0, Staff.LEFT, hand=Hand.LEFT),
    ]
    measures = [MeasureSpan(0, 0, 1920, Meter())]
    clefs = [
        ClefChange(0, Staff.RIGHT, "treble"),
        ClefChange(0, Staff.LEFT, "bass"),
    ]

    repaired, count = repair_staves_for_planned_clefs(notes, measures, clefs)

    assert repaired[0].staff == Staff.RIGHT
    assert count == 1
