from __future__ import annotations

from dataclasses import replace
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
from app.core.ornaments import collapse_trills, convert_grace_notes


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


def test_collapse_trills_ignores_measured_eighth_alternation() -> None:
    # An eighth-speed two-pitch oscillation is a measured figure, not a
    # trill; only sixteenth-speed (or faster) alternation earns the mark.
    notes = [
        QuantizedNote(index + 1, 72 + (index % 2), index * 240, 240, 80, 0, 0, Staff.RIGHT)
        for index in range(10)
    ]

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


def _grace_scenario() -> list[QuantizedNote]:
    return [
        QuantizedNote(1, 72, 0, 360, 80, 0, 0, Staff.RIGHT, voice=1),
        QuantizedNote(2, 74, 360, 120, 70, 0, 0, Staff.RIGHT, voice=1),
        QuantizedNote(3, 76, 480, 480, 90, 0, 0, Staff.RIGHT, voice=1),
    ]


def test_grace_note_replaces_truncation_and_restores_clean_value() -> None:
    converted, count = convert_grace_notes(_grace_scenario())

    assert count == 1
    by_id = {note.source_id: note for note in converted}
    assert by_id[2].grace
    assert by_id[1].duration == 480  # quarter restored from the truncated 360
    assert by_id[3].duration == 480
    assert not by_id[3].grace


def test_grace_note_requires_stepwise_quieter_approach() -> None:
    notes = _grace_scenario()
    loud = [notes[0], replace(notes[1], velocity=120), notes[2]]
    _, count = convert_grace_notes(loud)
    assert count == 0

    wide = [notes[0], replace(notes[1], pitch=80), notes[2]]
    _, count = convert_grace_notes(wide)
    assert count == 0


def test_grace_note_absorbed_by_rest_when_voice_has_a_gap() -> None:
    notes = [
        QuantizedNote(1, 74, 1800, 120, 70, 0, 0, Staff.RIGHT, voice=1),
        QuantizedNote(2, 76, 1920, 480, 92, 0, 0, Staff.RIGHT, voice=1),
    ]

    converted, count = convert_grace_notes(notes)

    assert count == 1
    assert next(note for note in converted if note.source_id == 1).grace


def test_grace_note_writes_slashed_note_without_duration() -> None:
    meter = Meter(4, 4)
    measure = MeasureSpan(0, 0, meter.measure_length, meter)
    notes = [
        QuantizedNote(1, 72, 0, 480, 80, 0, 0, Staff.RIGHT, voice=1),
        QuantizedNote(2, 74, 360, 120, 70, 0, 0, Staff.RIGHT, voice=1, grace=True),
        QuantizedNote(3, 76, 480, 480, 90, 0, 0, Staff.RIGHT, voice=1),
    ]
    score = ScoreModel(
        title="Grace",
        notes=notes,
        meter=meter,
        key=KeyEstimate(0, "major", 0, 1.0),
        tempo_bpm=96,
        pedals=[],
        grid_decisions=[],
        measure_count=1,
        measures=[measure],
    )

    root = ET.fromstring(score_to_musicxml(score))

    graces = root.findall(".//note[grace]")
    assert len(graces) == 1
    assert graces[0].find("grace").get("slash") == "yes"
    assert graces[0].find("duration") is None
    assert graces[0].findtext("pitch/step") == "D"
    timed = [
        note
        for note in root.findall(".//measure/note")
        if note.findtext("staff") == "1" and note.find("chord") is None
    ]
    assert sum(int(note.findtext("duration", "0")) for note in timed) == 1920
