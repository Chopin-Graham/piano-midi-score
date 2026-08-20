import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from mido import MidiFile

from app import __version__
from app.core.engraver import find_musescore
from app.core.models import (
    ClefChange,
    GridDecision,
    Hand,
    KeyChange,
    KeyEstimate,
    MeasureSpan,
    Meter,
    QuantizedNote,
    ScoreModel,
    Staff,
)
from app.core.musicxml import (
    _notation_atoms,
    musicxml_readability_metrics,
    score_to_musicxml,
)
from app.core.ottava import detect_ottava_spans


def _score(notes: list[QuantizedNote], meter: Meter | None = None) -> ScoreModel:
    meter = meter or Meter(4, 4)
    measure = MeasureSpan(0, 0, meter.measure_length, meter)
    return ScoreModel(
        title="Engraving Test",
        notes=notes,
        meter=meter,
        key=KeyEstimate(0, "major", 0, 1.0),
        tempo_bpm=96,
        pedals=[],
        grid_decisions=[],
        measure_count=1,
        measures=[measure],
    )


def test_chord_writes_one_staccato_mark() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 240, 80, 0, 0, Staff.RIGHT, staccato=True),
        QuantizedNote(2, 64, 0, 240, 80, 0, 0, Staff.RIGHT, staccato=True),
        QuantizedNote(3, 67, 0, 240, 80, 0, 0, Staff.RIGHT, staccato=True),
    ]

    root = ET.fromstring(score_to_musicxml(_score(notes)))

    assert len(root.findall(".//articulations/staccato")) == 1


def test_musicxml_identification_uses_release_version() -> None:
    root = ET.fromstring(score_to_musicxml(_score([])))

    assert root.findtext(".//identification/encoding/software") == (
        f"Piano MIDI Score {__version__}"
    )


def test_musicxml_writes_author_as_composer_credit() -> None:
    score = _score([])
    score.author = "F. Chopin"
    root = ET.fromstring(score_to_musicxml(score))

    assert root.findtext("./identification/creator[@type='composer']") == "F. Chopin"
    assert root.findtext("./credit[credit-type='composer']/credit-words") == "F. Chopin"


def test_empty_compound_staff_uses_one_measure_rest() -> None:
    meter = Meter(6, 8)
    notes = [
        QuantizedNote(
            1,
            72,
            0,
            meter.measure_length,
            80,
            0,
            0,
            Staff.RIGHT,
            voice=2,
            hand=Hand.RIGHT,
        )
    ]
    root = ET.fromstring(score_to_musicxml(_score(notes, meter)))

    assert not root.findall(".//note[staff='1'][voice='1']")
    lower_rests = root.findall(".//note[staff='2']/rest")
    assert len(lower_rests) == 1
    assert lower_rests[0].get("measure") == "yes"


def test_full_six_eight_measure_uses_one_dotted_half_without_internal_ties() -> None:
    meter = Meter(6, 8)
    notes = [
        QuantizedNote(
            1,
            60,
            0,
            meter.measure_length,
            80,
            0,
            0,
            Staff.LEFT,
            hand=Hand.LEFT,
        )
    ]

    atoms = _notation_atoms(notes, _score(notes, meter))

    assert [(atom.onset, atom.duration) for atom in atoms] == [(0, 1440)]
    assert not atoms[0].tie_start
    assert not atoms[0].tie_stop


def test_two_full_six_eight_measures_use_only_one_barline_tie() -> None:
    meter = Meter(6, 8)
    measures = [
        MeasureSpan(index, index * meter.measure_length, meter.measure_length, meter)
        for index in range(3)
    ]
    notes = [
        QuantizedNote(
            1,
            60,
            0,
            meter.measure_length * 2,
            80,
            0,
            0,
            Staff.LEFT,
            hand=Hand.LEFT,
        ),
        QuantizedNote(
            2,
            60,
            meter.measure_length * 2,
            meter.measure_length,
            80,
            0,
            0,
            Staff.LEFT,
            hand=Hand.LEFT,
        ),
    ]
    score = ScoreModel(
        title="Tie Test",
        notes=notes,
        meter=meter,
        key=KeyEstimate(0, "major", 0, 1.0),
        tempo_bpm=96,
        pedals=[],
        grid_decisions=[],
        measure_count=3,
        measures=measures,
    )

    atoms = _notation_atoms(notes, score)

    assert [
        (atom.onset, atom.duration, atom.tie_stop, atom.tie_start)
        for atom in atoms
    ] == [
        (0, 1440, False, True),
        (1440, 1440, True, False),
        (2880, 1440, False, False),
    ]


def test_secondary_voice_padding_rests_become_time_skips() -> None:
    notes = [
        QuantizedNote(1, 72, 0, 1920, 80, 0, 0, Staff.RIGHT, 1, hand=Hand.RIGHT),
        QuantizedNote(2, 60, 480, 480, 80, 0, 0, Staff.RIGHT, 2, hand=Hand.LEFT),
    ]
    xml = score_to_musicxml(_score(notes))
    metrics = musicxml_readability_metrics(xml)

    assert metrics["hidden_padding_rests"] == 0
    root = ET.fromstring(xml)
    skips = root.findall(".//forward[voice='2'][staff='1']")
    assert [int(skip.findtext("duration")) for skip in skips] == [480, 960]


def test_short_sparse_inner_voice_padding_is_skipped_with_partial_coverage() -> None:
    notes = [
        QuantizedNote(1, 76, 0, 720, 80, 0, 0, Staff.RIGHT, 1, hand=Hand.RIGHT),
        QuantizedNote(2, 79, 960, 480, 80, 0, 0, Staff.RIGHT, 1, hand=Hand.RIGHT),
        QuantizedNote(3, 64, 240, 240, 76, 0, 0, Staff.RIGHT, 2, hand=Hand.RIGHT),
        QuantizedNote(4, 67, 960, 240, 76, 0, 0, Staff.RIGHT, 2, hand=Hand.RIGHT),
    ]

    root = ET.fromstring(score_to_musicxml(_score(notes)))
    voice_two_rests = root.findall(".//note[staff='1'][voice='2'][rest]")

    assert all(rest.get("print-object") != "no" for rest in voice_two_rests)
    skips = root.findall(".//forward[voice='2'][staff='1']")
    skipped = sum(int(skip.findtext("duration")) for skip in skips)
    notes_duration = sum(
        int(note.findtext("duration"))
        for note in root.findall(".//note[staff='1'][voice='2'][pitch]")
    )
    assert skipped + notes_duration == 1920


def test_inferred_rolled_chord_writes_musicxml_arpeggiation() -> None:
    notes = [
        QuantizedNote(
            1,
            48,
            0,
            480,
            80,
            0,
            0,
            Staff.LEFT,
            hand=Hand.LEFT,
            arpeggiated=True,
        ),
        QuantizedNote(
            2,
            60,
            0,
            480,
            80,
            0,
            0,
            Staff.LEFT,
            hand=Hand.LEFT,
            arpeggiated=True,
        ),
    ]

    root = ET.fromstring(score_to_musicxml(_score(notes)))

    assert len(root.findall(".//notations/arpeggiate")) == 2


def test_musicxml_title_defers_font_family_to_musescore_style() -> None:
    xml = score_to_musicxml(_score([]))
    root = ET.fromstring(xml)

    assert root.findtext("./credit/credit-type") == "title"
    assert root.find("./defaults/music-font") is None
    assert root.find("./defaults/word-font") is None


def test_phrase_level_ottava_requires_multiple_extreme_events() -> None:
    notes = [
        QuantizedNote(
            index,
            84 + index % 3,
            index * 120,
            120,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        )
        for index in range(6)
    ]

    spans = detect_ottava_spans(notes)

    assert len(spans) == 1
    assert spans[0].direction == "down"
    assert spans[0].size == 8


def test_phrase_level_ottava_bridges_notes_inside_the_same_high_band() -> None:
    pitches = [96, 82, 94, 79, 93, 81, 91]
    notes = [
        QuantizedNote(
            index,
            pitch,
            index * 240,
            240,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        )
        for index, pitch in enumerate(pitches)
    ]

    spans = detect_ottava_spans(notes)

    assert len(spans) == 1
    assert spans[0].start == 0
    assert spans[0].end == len(pitches) * 240


def test_phrase_level_ottava_starts_at_safe_register_lead_in() -> None:
    pitches = [80, 82, 85, 87, 84, 86, 82, 85]
    notes = [
        QuantizedNote(
            index,
            pitch,
            index * 120,
            120,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        )
        for index, pitch in enumerate(pitches)
    ]

    spans = detect_ottava_spans(notes)

    assert len(spans) == 1
    assert spans[0].start == 0
    assert spans[0].end == len(pitches) * 120


def test_repeated_short_ottava_fragments_use_ledger_lines() -> None:
    notes: list[QuantizedNote] = []
    index = 0
    for phrase_start in (0, 1920, 3840):
        for offset, pitch in enumerate((86, 94, 86, 94, 86, 94)):
            notes.append(
                QuantizedNote(
                    index,
                    pitch,
                    phrase_start + offset * 120,
                    120,
                    80,
                    0,
                    0,
                    Staff.RIGHT,
                    hand=Hand.RIGHT,
                )
            )
            index += 1
        notes.append(
            QuantizedNote(
                index,
                67,
                phrase_start + 960,
                480,
                80,
                0,
                0,
                Staff.RIGHT,
                hand=Hand.RIGHT,
            )
        )
        index += 1

    assert detect_ottava_spans(notes) == []


def test_sustained_extreme_quarter_octave_uses_short_ottava() -> None:
    notes = [
        QuantizedNote(
            1,
            80,
            0,
            480,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        ),
        QuantizedNote(
            2,
            92,
            0,
            480,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        )
    ]

    spans = detect_ottava_spans(notes)

    assert len(spans) == 1
    assert spans[0].direction == "down"
    assert spans[0].size == 8

    root = ET.fromstring(score_to_musicxml(_score(notes)))
    sounding_octaves = [
        int(value.text or "0")
        for value in root.findall(".//note/pitch/octave")
    ]
    assert sounding_octaves == [5, 6]
    assert root.find(".//octave-shift").get("type") == "down"


def test_low_ottava_keeps_sounding_pitch_for_musescore_import() -> None:
    notes = [
        QuantizedNote(
            index,
            pitch,
            0,
            480,
            80,
            0,
            0,
            Staff.LEFT,
            hand=Hand.LEFT,
        )
        for index, pitch in enumerate((24, 36), start=1)
    ]

    root = ET.fromstring(score_to_musicxml(_score(notes)))

    assert [
        int(value.text or "0")
        for value in root.findall(".//note/pitch/octave")
    ] == [1, 2]
    shift = root.find(".//octave-shift")
    assert shift is not None
    assert (shift.get("type"), shift.get("size")) == ("up", "8")


def test_double_ottavas_keep_sounding_pitch_for_musescore_import() -> None:
    cases = [
        ((96, 104), Staff.RIGHT, Hand.RIGHT, "down", [7, 7]),
        ((20, 32), Staff.LEFT, Hand.LEFT, "up", [0, 1]),
    ]
    for pitches, staff, hand, shift_type, expected_octaves in cases:
        notes = [
            QuantizedNote(
                index,
                pitch,
                0,
                480,
                80,
                0,
                0,
                staff,
                hand=hand,
            )
            for index, pitch in enumerate(pitches, start=1)
        ]

        root = ET.fromstring(score_to_musicxml(_score(notes)))

        assert [
            int(value.text or "0")
            for value in root.findall(".//note/pitch/octave")
        ] == expected_octaves
        shift = root.find(".//octave-shift")
        assert shift is not None
        assert (shift.get("type"), shift.get("size")) == (shift_type, "15")


@pytest.mark.parametrize(
    ("pitches", "staff", "hand"),
    [
        ((80, 92), Staff.RIGHT, Hand.RIGHT),
        ((24, 36), Staff.LEFT, Hand.LEFT),
        ((96, 104), Staff.RIGHT, Hand.RIGHT),
        ((20, 32), Staff.LEFT, Hand.LEFT),
    ],
)
def test_musescore_ottava_round_trip_preserves_sounding_pitch(
    tmp_path: Path,
    pitches: tuple[int, int],
    staff: Staff,
    hand: Hand,
) -> None:
    executable = find_musescore()
    if executable is None:
        pytest.skip("MuseScore is not installed")
    notes = [
        QuantizedNote(
            index,
            pitch,
            0,
            480,
            80,
            0,
            0,
            staff,
            hand=hand,
        )
        for index, pitch in enumerate(pitches, start=1)
    ]
    musicxml_path = tmp_path / "ottava.musicxml"
    midi_path = tmp_path / "ottava.mid"
    job_path = tmp_path / "musescore-job.json"
    musicxml_path.write_text(score_to_musicxml(_score(notes)), encoding="utf-8")
    job_path.write_text(
        json.dumps([{"in": str(musicxml_path), "out": [str(midi_path)]}]),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(executable), "-j", str(job_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    exported_pitches = sorted(
        {
            message.note
            for track in MidiFile(midi_path).tracks
            for message in track
            if message.type == "note_on" and message.velocity > 0
        }
    )
    assert exported_pitches == sorted(pitches)


def test_overlapping_staff_ottavas_use_distinct_numbers_and_preserve_pitch(
    tmp_path: Path,
) -> None:
    executable = find_musescore()
    if executable is None:
        pytest.skip("MuseScore is not installed")

    high = (84, 88, 92, 88, 84, 88)
    low = (36, 32, 28, 24, 28, 32)
    notes = [
        QuantizedNote(
            index + 1,
            pitch,
            index * 240,
            240,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        )
        for index, pitch in enumerate(high)
    ]
    notes.extend(
        QuantizedNote(
            len(high) + index + 1,
            pitch,
            index * 240,
            240,
            80,
            0,
            0,
            Staff.LEFT,
            hand=Hand.LEFT,
        )
        for index, pitch in enumerate(low)
    )

    musicxml = score_to_musicxml(_score(notes))
    root = ET.fromstring(musicxml)
    numbered_shifts = {
        (
            direction.findtext("staff"),
            shift.get("type"),
            shift.get("number"),
        )
        for direction in root.findall(".//direction")
        if (shift := direction.find("direction-type/octave-shift")) is not None
    }
    assert ("1", "down", "1") in numbered_shifts
    assert ("1", "stop", "1") in numbered_shifts
    assert ("2", "up", "2") in numbered_shifts
    assert ("2", "stop", "2") in numbered_shifts

    musicxml_path = tmp_path / "overlapping-ottavas.musicxml"
    midi_path = tmp_path / "overlapping-ottavas.mid"
    job_path = tmp_path / "musescore-job.json"
    musicxml_path.write_text(musicxml, encoding="utf-8")
    job_path.write_text(
        json.dumps([{"in": str(musicxml_path), "out": [str(midi_path)]}]),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [str(executable), "-j", str(job_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout

    exported_pitches = sorted(
        message.note
        for track in MidiFile(midi_path).tracks
        for message in track
        if message.type == "note_on" and message.velocity > 0
    )
    assert exported_pitches == sorted((*high, *low))


def test_very_short_extreme_note_keeps_ledger_lines() -> None:
    notes = [
        QuantizedNote(
            1,
            92,
            0,
            120,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        )
    ]

    assert detect_ottava_spans(notes) == []

    root = ET.fromstring(score_to_musicxml(_score(notes)))
    assert root.findtext(".//note/pitch/octave") == "6"
    assert root.find(".//octave-shift") is None


def test_extreme_eighth_octave_keeps_ledger_lines_for_clear_ottava_semantics() -> None:
    notes = [
        QuantizedNote(
            1,
            80,
            0,
            240,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        ),
        QuantizedNote(
            2,
            92,
            0,
            240,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        ),
    ]

    assert detect_ottava_spans(notes) == []

    root = ET.fromstring(score_to_musicxml(_score(notes)))
    assert [
        int(value.text or "0")
        for value in root.findall(".//note/pitch/octave")
    ] == [5, 6]
    assert root.find(".//octave-shift") is None


def test_mid_measure_ottava_uses_time_cursor_for_both_anchors() -> None:
    notes = [
        QuantizedNote(
            index,
            84 + index % 3,
            480 + index * 120,
            120,
            80,
            0,
            0,
            Staff.RIGHT,
            hand=Hand.RIGHT,
        )
        for index in range(6)
    ]

    root = ET.fromstring(score_to_musicxml(_score(notes)))
    measure = root.find("./part/measure")
    assert measure is not None
    assert [
        int(duration.text or "0")
        for duration in measure.findall("forward/duration")
    ] == [480, 720]
    assert measure.findtext("backup/duration") == "1200"
    assert measure.findall("direction/offset") == []
    assert [
        shift.get("type")
        for shift in measure.findall("direction/direction-type/octave-shift")
    ] == ["down", "stop"]


def _multi_measure_score(
    *,
    measures: int,
    columns: int,
    engraving_style: str = "classic",
) -> ScoreModel:
    meter = Meter(4, 4)
    measure_spans = [
        MeasureSpan(index, index * meter.measure_length, meter.measure_length, meter)
        for index in range(measures)
    ]
    notes: list[QuantizedNote] = []
    source_id = 1
    for measure in measure_spans:
        for column in range(columns):
            onset = measure.start + column * 120
            for pitch in (60, 64, 67, 72):
                notes.append(
                    QuantizedNote(
                        source_id,
                        pitch,
                        onset,
                        120,
                        80,
                        0,
                        0,
                        Staff.RIGHT,
                        voice=1,
                        hand=Hand.RIGHT,
                    )
                )
                source_id += 1
    return ScoreModel(
        title="System Planning",
        notes=notes,
        meter=meter,
        key=KeyEstimate(0, "major", 0, 1.0),
        tempo_bpm=96,
        pedals=[],
        grid_decisions=[],
        measure_count=measures,
        engraving_style=engraving_style,
        measures=measure_spans,
    )


def test_musicxml_delegates_system_breaks_to_musescore() -> None:
    score = _multi_measure_score(measures=10, columns=12)

    root = ET.fromstring(score_to_musicxml(score))

    assert not root.findall("./part/measure/print[@new-system='yes']")


def test_compact_style_uses_smaller_physical_staff_scaling() -> None:
    classic = ET.fromstring(
        score_to_musicxml(
            _multi_measure_score(measures=1, columns=1, engraving_style="classic")
        )
    )
    compact = ET.fromstring(
        score_to_musicxml(
            _multi_measure_score(measures=1, columns=1, engraving_style="compact")
        )
    )

    assert classic.findtext("./defaults/scaling/millimeters") == "6.6"
    assert compact.findtext("./defaults/scaling/millimeters") == "6.2"
    assert classic.findtext("./defaults/page-layout/page-width") == "1273"
    assert compact.findtext("./defaults/page-layout/page-width") == "1355"
    assert compact.find("./credit/credit-words").get("default-x") == "677.5"


def test_musicxml_writes_key_signature_changes_at_measure_boundaries() -> None:
    score = _multi_measure_score(measures=3, columns=1)
    a_flat = KeyEstimate(8, "major", -4, 1.0)
    score.key_changes = [KeyChange(0, score.key), KeyChange(2, a_flat)]

    root = ET.fromstring(score_to_musicxml(score))
    fifths = [int(node.text or "0") for node in root.findall("./part/measure/attributes/key/fifths")]

    assert fifths == [0, -4]


def test_musicxml_positions_mid_measure_clef_with_time_cursor() -> None:
    score = _multi_measure_score(measures=1, columns=1)
    score.clef_changes = [
        ClefChange(0, Staff.RIGHT, "treble"),
        ClefChange(0, Staff.LEFT, "bass"),
        ClefChange(0, Staff.LEFT, "treble", 720),
    ]

    root = ET.fromstring(score_to_musicxml(score))
    measure = root.find("./part/measure")
    assert measure is not None
    assert [
        int(node.text or "0") for node in measure.findall("forward/duration")
    ] == [720]
    assert measure.findtext("backup/duration") == "720"
    positioned = measure.findall("attributes/clef[@number='2']")
    assert [clef.findtext("sign") for clef in positioned] == ["F", "G"]


def test_musicxml_ends_with_professional_final_barline() -> None:
    root = ET.fromstring(score_to_musicxml(_multi_measure_score(measures=2, columns=1)))
    measures = root.findall("./part/measure")

    assert measures[0].find("barline") is None
    assert measures[-1].findtext("barline/bar-style") == "light-heavy"


def test_same_hand_polyphony_uses_local_register_for_stem_directions() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 480, 80, 0, 0, Staff.RIGHT, 1, hand=Hand.RIGHT),
        QuantizedNote(2, 76, 0, 480, 80, 0, 0, Staff.RIGHT, 2, hand=Hand.RIGHT),
    ]

    root = ET.fromstring(score_to_musicxml(_score(notes)))
    stems = {
        int(note.findtext("voice") or "0"): note.findtext("stem")
        for note in root.findall(".//note[staff='1']")
        if note.find("pitch") is not None
    }

    assert stems == {1: "down", 2: "up"}


def test_triplet_rests_stay_inside_tuplet_brackets() -> None:
    notes = [
        QuantizedNote(1, 60, 1120, 240, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 64, 1760, 160, 80, 0, 0, Staff.RIGHT),
    ]
    musicxml = score_to_musicxml(_score(notes))
    root = ET.fromstring(musicxml)

    starts = root.findall(".//notations/tuplet[@type='start']")
    stops = root.findall(".//notations/tuplet[@type='stop']")
    assert len(starts) == len(stops) == 2

    # The triplet run opens on a rest: the rest must carry the start bracket.
    first_member = root.findall(".//note")[1]
    assert first_member.find("rest") is not None
    assert first_member.find("notations/tuplet[@type='start']") is not None

    assert musicxml_readability_metrics(musicxml)["unbalanced_tuplet_brackets"] == 0


def test_auto_tuplet_hides_two_note_padding_group() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 160, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 64, 160, 160, 80, 0, 0, Staff.RIGHT),
    ]
    score = _score(notes)
    score.grid_decisions = [
        GridDecision(0, "eighth_triplet", 160, 0.0, True, True)
    ]

    musicxml = score_to_musicxml(score)
    root = ET.fromstring(musicxml)

    starts = root.findall(".//notations/tuplet[@type='start']")
    assert len(starts) == 1
    assert starts[0].get("bracket") == "no"
    assert starts[0].get("show-number") == "none"
    assert all(
        note.get("print-object") == "no"
        for note in root.findall(".//note")
        if note.find("rest") is not None and note.find("time-modification") is not None
    )
    metrics = musicxml_readability_metrics(musicxml)
    assert metrics["visible_tuplet_spans"] == 0
    assert metrics["hidden_tuplet_spans"] == 1
    assert metrics["unbalanced_tuplet_brackets"] == 0


def test_auto_tuplet_brackets_three_real_attacks() -> None:
    notes = [
        QuantizedNote(index + 1, 60 + index, index * 160, 160, 80, 0, 0, Staff.RIGHT)
        for index in range(3)
    ]
    score = _score(notes)
    score.grid_decisions = [
        GridDecision(0, "eighth_triplet", 160, 0.0, True, True)
    ]

    musicxml = score_to_musicxml(score)
    root = ET.fromstring(musicxml)

    assert len(root.findall(".//notations/tuplet[@type='start']")) == 1
    assert len(root.findall(".//notations/tuplet[@type='stop']")) == 1
    assert len(root.findall(".//time-modification")) == 3
    assert root.find(".//notations/tuplet[@type='start']").get("bracket") == "auto"


def test_compound_meter_eighths_are_not_rebranded_as_triplets() -> None:
    meter = Meter(6, 8)
    notes = [
        QuantizedNote(index + 1, 60 + index, index * 240, 240, 80, 0, 0, Staff.RIGHT)
        for index in range(6)
    ]
    root = ET.fromstring(score_to_musicxml(_score(notes, meter)))

    assert not root.findall(".//time-modification")
    assert not root.findall(".//notations/tuplet")


def test_four_hundred_tick_triplet_member_splits_into_tied_members() -> None:
    notes = [
        QuantizedNote(1, 60, 0, 80, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 62, 80, 400, 80, 0, 0, Staff.RIGHT),
    ]
    musicxml = score_to_musicxml(_score(notes))
    root = ET.fromstring(musicxml)

    split = [
        note
        for note in root.findall(".//note")
        if note.findtext("pitch/step") == "D" and note.find("rest") is None
    ]
    assert len(split) == 2
    assert split[0].find("tie[@type='start']") is not None
    assert split[1].find("tie[@type='stop']") is not None
    assert sum(int(note.findtext("duration") or "0") for note in split) == 400
    assert musicxml_readability_metrics(musicxml)["unbalanced_tuplet_brackets"] == 0


def test_musescore_loads_triplet_measures_with_rests(tmp_path: Path) -> None:
    executable = find_musescore()
    if executable is None:
        pytest.skip("MuseScore is not installed")
    notes = [
        QuantizedNote(1, 60, 1120, 240, 80, 0, 0, Staff.RIGHT),
        QuantizedNote(2, 64, 1760, 160, 80, 0, 0, Staff.RIGHT),
    ]
    musicxml_path = tmp_path / "triplet-rests.musicxml"
    midi_path = tmp_path / "triplet-rests.mid"
    job_path = tmp_path / "musescore-job.json"
    musicxml_path.write_text(score_to_musicxml(_score(notes)), encoding="utf-8")
    job_path.write_text(
        json.dumps([{"in": str(musicxml_path), "out": [str(midi_path)]}]),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(executable), "-j", str(job_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert midi_path.is_file()

    exported_pitches = sorted(
        {
            message.note
            for track in MidiFile(midi_path).tracks
            for message in track
            if message.type == "note_on" and message.velocity > 0
        }
    )
    assert exported_pitches == [60, 64]


_NOMINAL_TICKS = {
    "whole": 1920,
    "half": 960,
    "quarter": 480,
    "eighth": 240,
    "16th": 120,
    "32nd": 60,
    "64th": 30,
}


def _assert_all_durations_consistent(root: ET.Element) -> None:
    for note in root.findall(".//note"):
        note_type = note.findtext("type")
        duration_text = note.findtext("duration")
        if note_type is None or duration_text is None:
            continue
        if note.find("rest") is not None and note.find("rest").get("measure") == "yes":
            continue
        expected = _NOMINAL_TICKS[note_type]
        extra = expected
        for _ in range(len(note.findall("dot"))):
            extra //= 2
            expected += extra
        time_mod = note.find("time-modification")
        if time_mod is not None:
            actual = int(time_mod.findtext("actual-notes"))
            normal = int(time_mod.findtext("normal-notes"))
            assert expected * normal % actual == 0
            expected = expected * normal // actual
        assert int(duration_text) == expected


def test_quintuplet_run_brackets_five_members() -> None:
    notes = [
        QuantizedNote(index + 1, 60 + index, index * 96, 96, 80, 0, 0, Staff.RIGHT)
        for index in range(5)
    ]
    root = ET.fromstring(score_to_musicxml(_score(notes)))

    modifications = root.findall(".//time-modification")
    assert modifications
    assert all(
        mod.findtext("actual-notes") == "5" and mod.findtext("normal-notes") == "4"
        for mod in modifications
    )
    starts = root.findall(".//notations/tuplet[@type='start']")
    stops = root.findall(".//notations/tuplet[@type='stop']")
    assert len(starts) == len(stops) == 1
    _assert_all_durations_consistent(root)


def test_sextuplet_run_brackets_six_members() -> None:
    notes = [
        QuantizedNote(index + 1, 60 + index, index * 80, 80, 80, 0, 0, Staff.RIGHT)
        for index in range(6)
    ]
    root = ET.fromstring(score_to_musicxml(_score(notes)))

    first = root.find(".//time-modification")
    assert first is not None
    assert first.findtext("actual-notes") == "6"
    starts = root.findall(".//notations/tuplet[@type='start']")
    stops = root.findall(".//notations/tuplet[@type='stop']")
    assert len(starts) == len(stops) == 1
    _assert_all_durations_consistent(root)


def test_incomplete_tuplet_fragment_keeps_consistent_durations() -> None:
    # An isolated quintuplet member pulls padding rests into its bracket; the
    # group may only close on a complete ratio span, every bracket must be
    # balanced, and every note keeps a matching (duration, type, ratio) so
    # importers never see a corrupt measure.
    notes = [
        QuantizedNote(index + 1, 60 + index, index * 96, 96, 80, 0, 0, Staff.RIGHT)
        for index in range(2)
    ]
    notes.append(QuantizedNote(3, 65, 240, 120, 80, 0, 0, Staff.RIGHT))
    root = ET.fromstring(score_to_musicxml(_score(notes)))

    starts = root.findall(".//notations/tuplet[@type='start']")
    stops = root.findall(".//notations/tuplet[@type='stop']")
    assert len(starts) == len(stops)
    _assert_all_durations_consistent(root)
