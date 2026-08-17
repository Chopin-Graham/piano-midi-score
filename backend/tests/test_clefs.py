from app.core.clefs import clef_kind_at, plan_clefs
from app.core.models import Hand, MeasureSpan, Meter, QuantizedNote, Staff


def test_clef_planner_uses_double_treble_then_returns_to_bass() -> None:
    meter = Meter(4, 4)
    measures = [
        MeasureSpan(index, index * meter.measure_length, meter.measure_length, meter)
        for index in range(4)
    ]
    notes = []
    source_id = 1
    for measure_index, pitches in enumerate(((69, 72, 76), (71, 74, 78), (40, 45, 52), (38, 47, 55))):
        for offset, pitch in enumerate(pitches):
            notes.append(
                QuantizedNote(
                    source_id,
                    pitch,
                    measures[measure_index].start + offset * 480,
                    480,
                    80,
                    0,
                    0,
                    Staff.LEFT,
                    hand=Hand.LEFT,
                )
            )
            source_id += 1

    changes, analysis = plan_clefs(notes, measures)

    assert clef_kind_at(changes, Staff.LEFT, 0) == "treble"
    assert clef_kind_at(changes, Staff.LEFT, 3) == "bass"
    assert analysis["lower_changes"] == 1


def test_clef_planner_delays_change_until_cross_bar_low_notes_release() -> None:
    meter = Meter(4, 4)
    measures = [
        MeasureSpan(index, index * meter.measure_length, meter.measure_length, meter)
        for index in range(3)
    ]
    notes = [
        QuantizedNote(1, 43, 1680, 960, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT),
        QuantizedNote(2, 55, 1680, 960, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT),
        QuantizedNote(3, 69, 2640, 1200, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT),
        QuantizedNote(4, 67, 2640, 1200, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT),
        QuantizedNote(5, 70, 2880, 960, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT),
        QuantizedNote(6, 75, 3840, 960, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT),
        QuantizedNote(7, 79, 3840, 960, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT),
        QuantizedNote(8, 72, 4800, 960, 80, 0, 0, Staff.LEFT, hand=Hand.LEFT),
    ]

    changes, analysis = plan_clefs(notes, measures)
    transition = next(
        change
        for change in changes
        if change.staff == Staff.LEFT and change.measure_index == 1
    )

    assert transition.kind == "treble"
    assert transition.offset == 720
    assert clef_kind_at(changes, Staff.LEFT, 1, 719) == "bass"
    assert clef_kind_at(changes, Staff.LEFT, 1, 720) == "treble"
    assert analysis["mid_measure_changes"] == 1


def test_audio_responsive_clef_planner_uses_lower_change_penalty() -> None:
    meter = Meter(4, 4)
    measures = [
        MeasureSpan(index, index * meter.measure_length, meter.measure_length, meter)
        for index in range(3)
    ]
    notes = [
        QuantizedNote(
            source_id,
            pitch,
            measure_index * meter.measure_length + offset * 480,
            480,
            80,
            0,
            0,
            Staff.LEFT,
            hand=Hand.LEFT,
        )
        for source_id, (measure_index, offset, pitch) in enumerate(
            [
                (0, 0, 43),
                (0, 1, 48),
                (1, 0, 76),
                (1, 1, 79),
                (2, 0, 43),
                (2, 1, 48),
            ],
            start=1,
        )
    ]

    changes, analysis = plan_clefs(notes, measures, responsive=True)

    assert clef_kind_at(changes, Staff.LEFT, 0) == "bass"
    assert clef_kind_at(changes, Staff.LEFT, 1) == "treble"
    assert clef_kind_at(changes, Staff.LEFT, 2) == "bass"
    assert analysis["method"].startswith("responsive_")
