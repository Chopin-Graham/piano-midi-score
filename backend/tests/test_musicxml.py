import subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
from mido import MidiFile

from app.core.engraver import find_musescore
from app.core.models import (
    ClefChange,
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


def test_secondary_voice_padding_rests_are_hidden() -> None:
    notes = [
        QuantizedNote(1, 72, 0, 1920, 80, 0, 0, Staff.RIGHT, 1, hand=Hand.RIGHT),
        QuantizedNote(2, 60, 480, 480, 80, 0, 0, Staff.RIGHT, 2, hand=Hand.LEFT),
    ]
    xml = score_to_musicxml(_score(notes))
    metrics = musicxml_readability_metrics(xml)

    assert metrics["hidden_padding_rests"] >= 2
    root = ET.fromstring(xml)
    assert all(
        note.get("print-object") == "no"
        for note in root.findall(".//note[staff='1'][voice='2'][rest]")
    )


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
    # MusicXML keeps sounding pitch. MuseScore derives the one-octave-lower
    # written position from the 8va line; shifting both would double-count it.
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
    score_path = tmp_path / "ottava.mscz"
    midi_path = tmp_path / "ottava.mid"
    musicxml_path.write_text(score_to_musicxml(_score(notes)), encoding="utf-8")

    for source, target in ((musicxml_path, score_path), (score_path, midi_path)):
        completed = subprocess.run(
            [str(executable), "-o", str(target), str(source)],
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
