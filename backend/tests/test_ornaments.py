from __future__ import annotations

from xml.etree import ElementTree as ET

from app.core.models import (
    KeyEstimate,
    MeasureSpan,
    Meter,
    QuantizedNote,
    ScoreModel,
    Staff,
)
from app.core.musicxml import score_to_musicxml
from app.core.ornaments import collapse_trills


def _alternating_notes(
    count: int,
    *,
    spacing: int = 120,
    low: int = 72,
    high: int = 74,
) -> list[QuantizedNote]:
    return [
        QuantizedNote(
            index + 1,
            low if index % 2 == 0 else high,
            index * spacing,
            spacing,
            80,
            0,
            0,
            Staff.RIGHT,
        )
        for index in range(count)
    ]


def test_collapse_trills_merges_alternating_edge_line() -> None:
    notes = _alternating_notes(8)
    anchor = QuantizedNote(100, 48, 0, 960, 70, 0, 0, Staff.LEFT)

    collapsed, trills, absorbed = collapse_trills([*notes, anchor])

    assert trills == 1
    assert absorbed == 7
    written = [note for note in collapsed if note.staff == Staff.RIGHT]
    assert len(written) == 1
    assert written[0].trill
    assert written[0].pitch == 72
    assert written[0].onset == 0
    assert written[0].duration == 8 * 120
    assert anchor in collapsed


def test_collapse_trills_ignores_three_pitch_figures() -> None:
    notes = [
        QuantizedNote(index + 1, 72 + (index % 3), index * 120, 120, 80, 0, 0, Staff.RIGHT)
        for index in range(9)
    ]

    collapsed, trills, _ = collapse_trills(notes)

    assert trills == 0
    assert len(collapsed) == len(notes)


def test_collapse_trills_ignores_short_alternations() -> None:
    notes = _alternating_notes(5)

    collapsed, trills, _ = collapse_trills(notes)

    assert trills == 0
    assert len(collapsed) == len(notes)


def test_collapse_trills_ignores_wide_intervals() -> None:
    notes = _alternating_notes(10, high=79)

    collapsed, trills, _ = collapse_trills(notes)

    assert trills == 0
    assert len(collapsed) == len(notes)


def test_trill_note_writes_ornament_and_keeps_voice_time() -> None:
    meter = Meter(4, 4)
    measure = MeasureSpan(0, 0, meter.measure_length, meter)
    trill = QuantizedNote(1, 72, 480, 960, 84, 0, 0, Staff.RIGHT, trill=True)
    score = ScoreModel(
        title="Trill",
        notes=[trill],
        meter=meter,
        key=KeyEstimate(0, "major", 0, 1.0),
        tempo_bpm=96,
        pedals=[],
        grid_decisions=[],
        measure_count=1,
        measures=[measure],
    )

    root = ET.fromstring(score_to_musicxml(score))

    marks = root.findall(".//note/notations/ornaments/trill-mark")
    assert len(marks) == 1
    voice_notes = root.findall(".//measure/note")
    assert sum(
        int(note.findtext("duration", "0"))
        for note in voice_notes
        if note.findtext("staff") == "1" and note.find("chord") is None
    ) == meter.measure_length
