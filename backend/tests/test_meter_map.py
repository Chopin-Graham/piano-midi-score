from app.core.meter_map import build_measure_map
from app.core.models import Meter, ParsedMidi, RawNote, TimeSignatureEvent


def test_compound_and_additive_beat_groups() -> None:
    compound = Meter(6, 8)
    irregular = Meter(5, 8)

    assert compound.is_compound is True
    assert compound.beat_groups == (720, 720)
    assert compound.beat_group_boundaries == (0, 720, 1440)
    assert irregular.beat_groups == (720, 480)
    assert irregular.beat_group_boundaries == (0, 720, 1200)


def test_encoded_eighth_pickup_does_not_replace_the_main_meter() -> None:
    parsed = ParsedMidi(
        ticks_per_beat=480,
        notes=[RawNote(1, 60, 0, 2160, 80, 0, 0)],
        time_signatures=[
            TimeSignatureEvent(0, 1, 8),
            TimeSignatureEvent(240, 4, 4),
        ],
    )

    measures, shift, warnings = build_measure_map(parsed)

    assert shift == 0
    assert measures[0].implicit is True
    assert measures[0].duration == 240
    assert measures[0].meter == Meter(4, 4)
    assert measures[1].start == 240
    assert measures[1].duration == 1920
    assert warnings
