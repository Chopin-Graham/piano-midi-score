from __future__ import annotations

from xml.etree import ElementTree as ET

from app.core.dynamics import plan_dynamics
from app.core.models import (
    KeyEstimate,
    MeasureSpan,
    Meter,
    QuantizedNote,
    ScoreModel,
    Staff,
)
from app.core.musicxml import score_to_musicxml


def _measures(count: int) -> list[MeasureSpan]:
    meter = Meter(4, 4)
    return [
        MeasureSpan(index, index * meter.measure_length, meter.measure_length, meter)
        for index in range(count)
    ]


def _piece(velocities: list[int]) -> list[QuantizedNote]:
    notes: list[QuantizedNote] = []
    for measure_index, velocity in enumerate(velocities):
        for beat in range(4):
            for pitch in (60, 64, 67):
                notes.append(
                    QuantizedNote(
                        len(notes) + 1,
                        pitch,
                        measure_index * 1920 + beat * 480,
                        240,
                        velocity,
                        0,
                        0,
                        Staff.RIGHT,
                    )
                )
    return notes


def test_plan_dynamics_marks_quiet_then_loud() -> None:
    marks = plan_dynamics(_piece([50] * 4 + [95] * 4), _measures(8))

    assert marks
    assert marks[0].measure_index == 0
    assert marks[0].mark in {"p", "mp"}
    assert any(mark.mark == "f" for mark in marks[1:])


def test_plan_dynamics_skips_flat_velocities() -> None:
    assert plan_dynamics(_piece([70] * 8), _measures(8)) == []


def test_plan_dynamics_hysteresis_ignores_single_measure_blip() -> None:
    levels = [50, 50, 66, 50, 50, 50]
    marks = plan_dynamics(_piece(levels), _measures(6))

    assert len(marks) == 1  # only the opening mark


def test_plan_dynamics_commits_sustained_change() -> None:
    levels = [50, 50, 75, 75, 75, 50]
    marks = plan_dynamics(_piece(levels), _measures(6))

    assert any(mark.mark in {"mf", "f"} for mark in marks[1:])


def test_dynamics_written_into_musicxml() -> None:
    meter = Meter(4, 4)
    measures = [
        MeasureSpan(index, index * 1920, 1920, meter) for index in range(2)
    ]
    notes = [
        QuantizedNote(1, 60, 0, 480, 50, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 64, 1920, 480, 100, 0, 0, Staff.RIGHT),
    ]
    score = ScoreModel(
        title="Dynamics",
        notes=notes,
        meter=meter,
        key=KeyEstimate(0, "major", 0, 1.0),
        tempo_bpm=96,
        pedals=[],
        grid_decisions=[],
        measure_count=2,
        measures=measures,
        dynamics=plan_dynamics(notes, measures),
    )

    root = ET.fromstring(score_to_musicxml(score))

    dynamics = root.findall(".//direction/direction-type/dynamics")
    assert dynamics
    first = dynamics[0][0]
    assert first.tag in {"pp", "p", "mp", "mf", "f", "ff"}


def test_musescore_loads_score_with_dynamics(tmp_path) -> None:
    import json
    import subprocess

    import pytest

    from app.core.engraver import find_musescore

    executable = find_musescore()
    if executable is None:
        pytest.skip("MuseScore is not installed")
    meter = Meter(4, 4)
    measures = [MeasureSpan(index, index * 1920, 1920, meter) for index in range(4)]
    notes = [
        QuantizedNote(index + 1, 60 + index, index * 1920, 960, velocity, 0, 0, Staff.RIGHT)
        for index, velocity in enumerate((50, 60, 90, 95))
    ]
    score = ScoreModel(
        title="Dynamics load",
        notes=notes,
        meter=meter,
        key=KeyEstimate(0, "major", 0, 1.0),
        tempo_bpm=96,
        pedals=[],
        grid_decisions=[],
        measure_count=4,
        measures=measures,
        dynamics=plan_dynamics(notes, measures),
    )
    musicxml_path = tmp_path / "dynamics.musicxml"
    midi_path = tmp_path / "dynamics.mid"
    job_path = tmp_path / "job.json"
    musicxml_path.write_text(score_to_musicxml(score), encoding="utf-8")
    job_path.write_text(
        json.dumps([{"in": str(musicxml_path), "out": [str(midi_path)]}]),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(executable), "-j", str(job_path)],
        check=False,
        capture_output=True,
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0
    assert midi_path.is_file()
