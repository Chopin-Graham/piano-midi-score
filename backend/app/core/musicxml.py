from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from functools import cache
from statistics import median
from xml.etree import ElementTree as ET

from .. import __version__
from .meter_map import measure_index_at
from .models import (
    CANONICAL_DIVISIONS,
    ClefChange,
    DynamicMark,
    Hand,
    KeyEstimate,
    MeasureSpan,
    Meter,
    QuantizedNote,
    ScoreModel,
    Staff,
)
from .ottava import OttavaSpan, detect_ottava_spans


@dataclass(frozen=True, slots=True)
class DurationSpec:
    value: int
    note_type: str
    dots: int = 0
    time_modification: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class NotationNote:
    pitch: int
    onset: int
    duration: int
    velocity: int
    staff: Staff
    voice: int
    tie_stop: bool
    tie_start: bool
    pitch_step: str | None = None
    pitch_alter: int = 0
    pitch_octave: int | None = None
    hand: Hand | None = None
    arpeggiated: bool = False
    trill: bool = False
    grace: bool = False
    staccato: bool = False
    tremolo_start: bool = False
    tremolo_stop: bool = False


@dataclass(slots=True)
class VoiceItem:
    onset: int
    duration: int
    spec: DurationSpec
    notes: list[NotationNote]
    is_rest: bool = False
    measure_rest: bool = False
    beam: str | None = None
    tuplet_start: bool = False
    tuplet_stop: bool = False
    in_tuplet: bool = False
    tuplet_hidden: bool = False
    boundary_rest: bool = False
    hidden_rest: bool = False
    grace_notes: list[NotationNote] = field(default_factory=list)


DURATION_SPECS = [
    DurationSpec(1920, "whole"),
    DurationSpec(1440, "half", 1),
    DurationSpec(960, "half"),
    DurationSpec(720, "quarter", 1),
    DurationSpec(480, "quarter"),
    DurationSpec(384, "quarter", 0, (5, 4)),
    DurationSpec(360, "eighth", 1),
    DurationSpec(320, "quarter", 0, (3, 2)),
    DurationSpec(288, "eighth", 1, (5, 4)),
    DurationSpec(240, "eighth"),
    DurationSpec(192, "eighth", 0, (5, 4)),
    DurationSpec(180, "16th", 1),
    DurationSpec(160, "eighth", 0, (3, 2)),
    DurationSpec(144, "16th", 1, (5, 4)),
    DurationSpec(120, "16th"),
    DurationSpec(96, "16th", 0, (5, 4)),
    DurationSpec(90, "32nd", 1),
    DurationSpec(80, "16th", 0, (6, 4)),
    DurationSpec(60, "32nd"),
    DurationSpec(48, "32nd", 0, (5, 4)),
    DurationSpec(40, "32nd", 0, (6, 4)),
    DurationSpec(30, "64th"),
]
SPEC_BY_VALUE = {spec.value: spec for spec in DURATION_SPECS}

# Plain (ratio-free) spec values, largest first: the only safe choices when a
# free note's duration must be coerced — a ratio spec on an unbracketed note
# hangs importers, and a mismatched spec corrupts the measure.
_PLAIN_SPEC_VALUES = tuple(
    sorted((spec.value for spec in DURATION_SPECS if spec.time_modification is None), reverse=True)
)

# Specs without a time ratio: the only safe choices for decomposing an
# arbitrary span.  Picking a tuplet spec for an unrelated binary duration
# prints a ratio where none belongs (a "5:4 quarter" rest for 400 ticks) and
# leaves unrepresentable remainders behind.
BINARY_SPECS = [spec for spec in DURATION_SPECS if spec.time_modification is None]

# Tuplet member tables, by (actual, normal) ratio.  Every entry satisfies the
# MusicXML consistency rule: duration == nominal(type, dots) * normal / actual.
# A group is bracketed only when its members' real durations sum to exactly
# span = first_member_duration * actual; groups that cannot be completed keep
# their exact (duration, type, time-modification) triplets without a bracket,
# which stays loadable instead of corrupting the measure with a type whose
# nominal length disagrees with the written duration.
TUPLET_MEMBER_SPECS: dict[tuple[int, int], dict[int, DurationSpec]] = {
    (3, 2): {
        80: DurationSpec(80, "16th", 0, (3, 2)),
        160: DurationSpec(160, "eighth", 0, (3, 2)),
        240: DurationSpec(240, "eighth", 1, (3, 2)),
        320: DurationSpec(320, "quarter", 0, (3, 2)),
    },
    (6, 4): {
        40: DurationSpec(40, "32nd", 0, (6, 4)),
        80: DurationSpec(80, "16th", 0, (6, 4)),
        160: DurationSpec(160, "eighth", 0, (6, 4)),
        240: DurationSpec(240, "eighth", 1, (6, 4)),
    },
    (5, 4): {
        48: DurationSpec(48, "32nd", 0, (5, 4)),
        96: DurationSpec(96, "16th", 0, (5, 4)),
        144: DurationSpec(144, "16th", 1, (5, 4)),
        192: DurationSpec(192, "eighth", 0, (5, 4)),
        288: DurationSpec(288, "eighth", 1, (5, 4)),
    },
}

SHARP_PITCHES = {
    0: ("C", 0),
    1: ("C", 1),
    2: ("D", 0),
    3: ("D", 1),
    4: ("E", 0),
    5: ("F", 0),
    6: ("F", 1),
    7: ("G", 0),
    8: ("G", 1),
    9: ("A", 0),
    10: ("A", 1),
    11: ("B", 0),
}
FLAT_PITCHES = {
    0: ("C", 0),
    1: ("D", -1),
    2: ("D", 0),
    3: ("E", -1),
    4: ("E", 0),
    5: ("F", 0),
    6: ("G", -1),
    7: ("G", 0),
    8: ("A", -1),
    9: ("A", 0),
    10: ("B", -1),
    11: ("B", 0),
}


def score_to_musicxml(score: ScoreModel) -> str:
    root = ET.Element("score-partwise", version="4.0")
    _add_work_and_identification(root, score)
    _add_defaults(root, score.engraving_style)
    _add_credit(root, score.title, score.author, score.engraving_style)
    _add_part_list(root)

    part = ET.SubElement(root, "part", id="P1")
    grid_steps = {
        decision.measure_index: decision.step for decision in score.grid_decisions
    }
    auto_tuplet_measures = {
        decision.measure_index
        for decision in score.grid_decisions
        if decision.auto_tuplet
    }
    ottava_spans = detect_ottava_spans(score.notes)
    atoms = _notation_atoms(score.notes, score, ottava_spans)
    atoms_by_location: dict[tuple[int, Staff, int], list[NotationNote]] = defaultdict(list)
    for atom in atoms:
        measure_index = measure_index_at(score.measures, atom.onset)
        atoms_by_location[(measure_index, atom.staff, atom.voice)].append(atom)

    # MuseScore already has a mature spacing and collision engine.  Earlier
    # versions injected estimated system breaks here, but real reference scores
    # showed that those hints could collide with MuseScore's own automatic
    # breaks and leave one-measure systems.  Keep the MusicXML semantic and let
    # the engraver decide horizontal layout from the final glyph geometry.
    break_before: set[int] = set()
    pedal_by_measure = _pedals_by_measure(score)
    dynamics_by_measure: dict[int, list[DynamicMark]] = defaultdict(list)
    for mark in score.dynamics:
        dynamics_by_measure[mark.measure_index].append(mark)
    clefs_by_measure: dict[int, list[ClefChange]] = defaultdict(list)
    for clef_change in score.clef_changes:
        clefs_by_measure[clef_change.measure_index].append(clef_change)
    ottava_by_measure = _ottava_directions_by_measure(
        score,
        ottava_spans,
    )
    keys_by_measure = {
        change.measure_index: change.key for change in score.key_changes
    }
    tempos_by_measure = _tempos_by_measure(score)
    tempo_texts_by_measure: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for tempo_text in score.tempo_texts:
        text_measure, text_offset = _score_time_location(
            score, min(tempo_text.tick, score.measures[-1].end)
        )
        tempo_texts_by_measure[text_measure].append((text_offset, tempo_text.text))

    previous_meter: Meter | None = None
    current_key = score.key
    for measure_index, measure_span in enumerate(score.measures):
        measure_attributes = {"number": str(measure_index + 1)}
        if measure_span.implicit:
            measure_attributes["implicit"] = "yes"
        measure = ET.SubElement(part, "measure", measure_attributes)
        if measure_index == 0 or measure_index in break_before:
            print_element = ET.SubElement(measure, "print")
            if measure_index in break_before:
                print_element.set("new-system", "yes")
            if measure_index == 0:
                _add_first_measure_layout(print_element)

        meter_changed = measure_index == 0 or measure_span.meter != previous_meter
        clef_changes = clefs_by_measure.get(measure_index, [])
        boundary_clef_changes = [change for change in clef_changes if change.offset == 0]
        positioned_clef_changes = [change for change in clef_changes if change.offset > 0]
        key_changed = measure_index in keys_by_measure
        if key_changed:
            current_key = keys_by_measure[measure_index]
        if meter_changed or boundary_clef_changes or key_changed:
            _add_attributes(
                measure,
                current_key,
                measure_span.meter,
                include_staff_setup=measure_index == 0,
                include_time=meter_changed,
                include_key=measure_index == 0 or key_changed,
                clef_changes=boundary_clef_changes,
            )
        for tempo_offset, tempo_bpm, tempo_visible in tempos_by_measure.get(measure_index, []):
            _add_tempo(measure, tempo_bpm, offset=tempo_offset, visible=tempo_visible)
        for text_offset, tempo_text in tempo_texts_by_measure.get(measure_index, []):
            _add_tempo_text(measure, tempo_text, offset=text_offset)
        previous_meter = measure_span.meter

        for offset, down in pedal_by_measure.get(measure_index, []):
            _add_pedal(measure, offset, down)

        for dynamic_mark in dynamics_by_measure.get(measure_index, []):
            _add_dynamic(measure, dynamic_mark)

        _add_positioned_octave_shifts(
            measure,
            ottava_by_measure.get(measure_index, []),
        )
        _add_positioned_clef_changes(measure, positioned_clef_changes)

        sequences: list[tuple[Staff, int, list[VoiceItem]]] = []
        for staff in (Staff.RIGHT, Staff.LEFT):
            present_voices = sorted(
                {
                    voice
                    for current_measure, current_staff, voice in atoms_by_location
                    if current_measure == measure_index and current_staff == staff
                }
            )
            if not present_voices:
                present_voices = [1]
            staff_sequences: list[tuple[Staff, int, list[VoiceItem]]] = []
            for voice in present_voices:
                voice_atoms = atoms_by_location.get((measure_index, staff, voice), [])
                staff_sequences.append(
                    (
                        staff,
                        voice,
                        _voice_items(
                            voice_atoms,
                            measure_span.start,
                            measure_span.duration,
                            measure_span.meter,
                            measure_span.implicit,
                            grid_steps.get(measure_index),
                            measure_index in auto_tuplet_measures,
                        ),
                    )
                )
            _mark_hidden_padding_rests(
                staff_sequences,
                measure_span.duration,
                measure_span.meter,
            )
            sequences.extend(staff_sequences)

        for sequence_index, (staff, voice, items) in enumerate(sequences):
            if sequence_index:
                backup = ET.SubElement(measure, "backup")
                ET.SubElement(backup, "duration").text = str(measure_span.duration)
            staff_sequences = [item for item in sequences if item[0] == staff]
            stem_directions = _stem_directions(staff_sequences)
            pending_skip = 0
            for item in items:
                # Voice padding covered by other voices is a time skip, not a
                # printed symbol.  `<forward>` keeps the voice's time accounting
                # exact without leaving invisible (gray-on-screen) rests in the
                # editor.  Tuplet-member rests must stay literal: a skip inside
                # a bracket breaks the tuplet ratio.
                # Rests whose duration has no exact note-type spec (sub-grid
                # cracks left where two beat-group grids meet) also become
                # skips: printing them would require a fake type whose nominal
                # length disagrees with the written duration, which importers
                # report as a corrupt measure.
                if (
                    item.is_rest
                    and not item.measure_rest
                    and item.duration not in SPEC_BY_VALUE
                ):
                    pending_skip += item.duration
                    continue
                # A ratio-marked rest outside any completed bracket hangs
                # MuseScore's importer; as pure padding it is a time skip.
                if item.is_rest and item.spec.time_modification and not item.in_tuplet:
                    pending_skip += item.duration
                    continue
                if item.is_rest and item.hidden_rest and not item.spec.time_modification:
                    pending_skip += item.duration
                    continue
                if pending_skip:
                    _write_forward(measure, pending_skip, staff, voice)
                    pending_skip = 0
                _write_item(
                    measure,
                    item,
                    staff,
                    voice,
                    current_key.fifths,
                    stem_directions.get(voice),
                )
            if pending_skip:
                _write_forward(measure, pending_skip, staff, voice)

        if measure_index == len(score.measures) - 1:
            barline = ET.SubElement(measure, "barline", location="right")
            ET.SubElement(barline, "bar-style").text = "light-heavy"

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
        '"http://www.musicxml.org/dtds/partwise.dtd">\n'
        + body
    )


def _add_work_and_identification(root: ET.Element, score: ScoreModel) -> None:
    work = ET.SubElement(root, "work")
    ET.SubElement(work, "work-title").text = score.title
    identification = ET.SubElement(root, "identification")
    if score.author:
        ET.SubElement(identification, "creator", type="composer").text = score.author
    encoding = ET.SubElement(identification, "encoding")
    ET.SubElement(encoding, "software").text = f"Piano MIDI Score {__version__}"
    supports = ET.SubElement(encoding, "supports", element="print", type="yes")
    supports.set("attribute", "new-system")
    supports.set("value", "yes")


PAGE_GEOMETRY = {
    "classic": {
        "millimeters": "6.6",
        "width": "1273",
        "height": "1800",
        "side_margin": "73",
        "vertical_margin": "72",
        "credit_x": "636.5",
        "credit_y": "1718",
    },
    "modern": {
        "millimeters": "7.0",
        "width": "1200",
        "height": "1697",
        "side_margin": "80",
        "vertical_margin": "72",
        "credit_x": "600",
        "credit_y": "1620",
    },
    "compact": {
        "millimeters": "6.2",
        "width": "1355",
        "height": "1916",
        "side_margin": "72",
        "vertical_margin": "76",
        "credit_x": "677.5",
        "credit_y": "1829",
    },
}


def _page_geometry(engraving_style: str) -> dict[str, str]:
    return PAGE_GEOMETRY.get(engraving_style, PAGE_GEOMETRY["classic"])


def _add_defaults(root: ET.Element, engraving_style: str) -> None:
    geometry = _page_geometry(engraving_style)
    defaults = ET.SubElement(root, "defaults")
    scaling = ET.SubElement(defaults, "scaling")
    # MusicXML scaling overrides MuseScore's style-file Spatium.  Classic uses
    # a professional 1.65 mm piano-score staff; compact remains a genuine
    # 1.55 mm performer edition rather than a margin-only variant.
    ET.SubElement(scaling, "millimeters").text = geometry["millimeters"]
    ET.SubElement(scaling, "tenths").text = "40"
    page_layout = ET.SubElement(defaults, "page-layout")
    ET.SubElement(page_layout, "page-height").text = geometry["height"]
    ET.SubElement(page_layout, "page-width").text = geometry["width"]
    for page_type in ("odd", "even"):
        margins = ET.SubElement(page_layout, "page-margins", type=page_type)
        ET.SubElement(margins, "left-margin").text = geometry["side_margin"]
        ET.SubElement(margins, "right-margin").text = geometry["side_margin"]
        ET.SubElement(margins, "top-margin").text = geometry["vertical_margin"]
        ET.SubElement(margins, "bottom-margin").text = geometry["vertical_margin"]
    system_layout = ET.SubElement(defaults, "system-layout")
    system_margins = ET.SubElement(system_layout, "system-margins")
    ET.SubElement(system_margins, "left-margin").text = "0"
    ET.SubElement(system_margins, "right-margin").text = "0"
    ET.SubElement(system_layout, "system-distance").text = "120"
    ET.SubElement(system_layout, "top-system-distance").text = "90"
    appearance = ET.SubElement(defaults, "appearance")
    for line_type, width in (
        ("staff", "1.0"),
        ("stem", "0.8"),
        ("beam", "5.0"),
        ("light barline", "1.2"),
        ("heavy barline", "5.0"),
    ):
        ET.SubElement(appearance, "line-width", type=line_type).text = width
def _add_credit(
    root: ET.Element,
    title: str,
    author: str | None,
    engraving_style: str,
) -> None:
    geometry = _page_geometry(engraving_style)
    if len(title) <= 42:
        font_size = "22"
    elif len(title) <= 64:
        font_size = "18"
    else:
        font_size = "16"
    credit = ET.SubElement(root, "credit", page="1")
    ET.SubElement(credit, "credit-type").text = "title"
    ET.SubElement(
        credit,
        "credit-words",
        {
            "default-x": geometry["credit_x"],
            "default-y": geometry["credit_y"],
            "justify": "center",
            "valign": "top",
            "font-size": font_size,
        },
    ).text = title
    if author:
        author_credit = ET.SubElement(root, "credit", page="1")
        ET.SubElement(author_credit, "credit-type").text = "composer"
        ET.SubElement(
            author_credit,
            "credit-words",
            {
                "default-x": str(
                    int(geometry["width"]) - int(geometry["side_margin"])
                ),
                "default-y": str(int(geometry["credit_y"]) - 48),
                "justify": "right",
                "valign": "top",
                "font-size": "11",
            },
        ).text = author


def _add_part_list(root: ET.Element) -> None:
    part_list = ET.SubElement(root, "part-list")
    score_part = ET.SubElement(part_list, "score-part", id="P1")
    ET.SubElement(score_part, "part-name").text = "Piano"
    ET.SubElement(score_part, "part-abbreviation").text = "Pno."
    instrument = ET.SubElement(score_part, "score-instrument", id="P1-I1")
    ET.SubElement(instrument, "instrument-name").text = "Acoustic Grand Piano"
    midi_instrument = ET.SubElement(score_part, "midi-instrument", id="P1-I1")
    ET.SubElement(midi_instrument, "midi-channel").text = "1"
    ET.SubElement(midi_instrument, "midi-program").text = "1"


def _add_first_measure_layout(print_element: ET.Element) -> None:
    system_layout = ET.SubElement(print_element, "system-layout")
    ET.SubElement(system_layout, "top-system-distance").text = "150"
    staff_layout = ET.SubElement(print_element, "staff-layout", number="2")
    ET.SubElement(staff_layout, "staff-distance").text = "78"


def _add_attributes(
    measure: ET.Element,
    key_signature: KeyEstimate,
    meter: Meter,
    *,
    include_staff_setup: bool,
    include_time: bool,
    include_key: bool,
    clef_changes: list[ClefChange],
) -> None:
    attributes = ET.SubElement(measure, "attributes")
    if include_staff_setup:
        ET.SubElement(attributes, "divisions").text = str(CANONICAL_DIVISIONS)
    if include_key:
        key = ET.SubElement(attributes, "key")
        ET.SubElement(key, "fifths").text = str(key_signature.fifths)
        ET.SubElement(key, "mode").text = key_signature.mode
    if include_time:
        time = ET.SubElement(attributes, "time")
        ET.SubElement(time, "beats").text = str(meter.numerator)
        ET.SubElement(time, "beat-type").text = str(meter.denominator)
    if include_staff_setup:
        ET.SubElement(attributes, "staves").text = "2"
    if include_staff_setup and not clef_changes:
        clef_changes = [
            ClefChange(0, Staff.RIGHT, "treble"),
            ClefChange(0, Staff.LEFT, "bass"),
        ]
    for change in sorted(clef_changes, key=lambda item: int(item.staff)):
        clef = ET.SubElement(attributes, "clef", number=str(int(change.staff)))
        ET.SubElement(clef, "sign").text = change.sign
        ET.SubElement(clef, "line").text = str(change.line)


def _add_tempo(measure: ET.Element, bpm: float, offset: int = 0, visible: bool = True) -> None:
    rounded = max(20, min(300, round(bpm)))
    direction = ET.SubElement(measure, "direction", placement="above")
    if visible:
        direction_type = ET.SubElement(direction, "direction-type")
        metronome = ET.SubElement(direction_type, "metronome", parentheses="no")
        ET.SubElement(metronome, "beat-unit").text = "quarter"
        ET.SubElement(metronome, "per-minute").text = str(rounded)
    if offset:
        ET.SubElement(direction, "offset", sound="yes").text = str(offset)
    ET.SubElement(direction, "sound", tempo=str(rounded))
    ET.SubElement(direction, "staff").text = "1"


def _add_tempo_text(measure: ET.Element, text: str, offset: int = 0) -> None:
    direction = ET.SubElement(measure, "direction", placement="above")
    direction_type = ET.SubElement(direction, "direction-type")
    ET.SubElement(direction_type, "words", {"font-style": "italic"}).text = text
    if offset:
        ET.SubElement(direction, "offset", sound="yes").text = str(offset)
    ET.SubElement(direction, "staff").text = "1"


def _tempos_by_measure(score: ScoreModel) -> dict[int, list[tuple[int, float, bool]]]:
    """Place tempo changes on the measure grid and choose printable marks.

    Every MIDI tempo event becomes a `<sound tempo>` direction so playback
    follows the original tempo map (rit./accel. included).  A visible
    metronome mark is printed only for sustained plateaus — an event that
    stays in force for at least a whole note, or a final tempo reached after
    such a span — so gradual ritardandos do not litter the page with numbers.
    """

    if not score.tempo_changes:
        return {0: [(0, score.tempo_bpm, True)]}

    sustained_span = CANONICAL_DIVISIONS * 4
    changes = score.tempo_changes
    result: dict[int, list[tuple[int, float, bool]]] = defaultdict(list)
    last_visible_bpm: float | None = None
    for index, change in enumerate(changes):
        measure_index, offset = _score_time_location(
            score, min(change.tick, score.measures[-1].end)
        )
        sustained = (
            index + 1 < len(changes)
            and changes[index + 1].tick - change.tick >= sustained_span
        ) or (
            index + 1 == len(changes)
            and index > 0
            and change.tick - changes[index - 1].tick >= sustained_span
        )
        visible = index == 0 or (
            sustained
            and last_visible_bpm is not None
            and abs(change.bpm - last_visible_bpm) / last_visible_bpm >= 0.03
        )
        if visible:
            last_visible_bpm = change.bpm
        result[measure_index].append((offset, change.bpm, visible))
    return result


def _add_pedal(measure: ET.Element, offset: int, down: bool) -> None:
    direction = ET.SubElement(measure, "direction", placement="below")
    direction_type = ET.SubElement(direction, "direction-type")
    pedal_type = "start" if down else "stop"
    ET.SubElement(direction_type, "pedal", type=pedal_type, line="yes", sign="no")
    if offset:
        ET.SubElement(direction, "offset", sound="yes").text = str(offset)
    ET.SubElement(direction, "staff").text = "2"


def _add_dynamic(measure: ET.Element, mark: DynamicMark) -> None:
    # Between the staves is the conventional piano-score dynamic position.
    direction = ET.SubElement(measure, "direction", placement="below")
    direction_type = ET.SubElement(direction, "direction-type")
    dynamics = ET.SubElement(direction_type, "dynamics")
    ET.SubElement(dynamics, mark.mark)
    ET.SubElement(direction, "sound", dynamics=str(mark.velocity_percent))
    ET.SubElement(direction, "staff").text = "1"


def _add_octave_shift(
    measure: ET.Element,
    offset: int,
    staff: Staff,
    shift_type: str,
    size: int,
    placement: str,
) -> None:
    direction = ET.SubElement(measure, "direction", placement=placement)
    direction_type = ET.SubElement(direction, "direction-type")
    ET.SubElement(
        direction_type,
        "octave-shift",
        type=shift_type,
        size=str(size),
        # MusicXML octave-shift numbers are part-wide, not staff-local.  Using
        # number 1 for simultaneous 8va and 8vb lines lets importers pair a
        # stop from one staff with the start on the other, leaking the octave
        # transposition into later notes.  The grand staff has stable 1/2 IDs.
        number=str(int(staff)),
    )
    if offset:
        ET.SubElement(direction, "offset").text = str(offset)
    ET.SubElement(direction, "staff").text = str(int(staff))


def _add_positioned_octave_shifts(
    measure: ET.Element,
    directions: list[tuple[int, Staff, str, int, str]],
) -> None:
    """Write mid-measure ottavas at the MusicXML time cursor.

    MuseScore imports a direction's start offset but can ignore the stop offset
    when all directions precede the note stream. Explicit forward/backup moves
    preserve both anchors and therefore the full dashed-line span.
    """

    cursor = 0
    for offset, staff, shift_type, size, placement in directions:
        if offset > cursor:
            forward = ET.SubElement(measure, "forward")
            ET.SubElement(forward, "duration").text = str(offset - cursor)
            cursor = offset
        _add_octave_shift(
            measure,
            0,
            staff,
            shift_type,
            size,
            placement,
        )
    if cursor:
        backup = ET.SubElement(measure, "backup")
        ET.SubElement(backup, "duration").text = str(cursor)


def _add_positioned_clef_changes(
    measure: ET.Element,
    changes: list[ClefChange],
) -> None:
    """Place clef changes at an exact MusicXML time cursor inside a measure."""

    cursor = 0
    grouped: dict[int, list[ClefChange]] = defaultdict(list)
    for change in changes:
        grouped[change.offset].append(change)
    for offset, offset_changes in sorted(grouped.items()):
        if offset > cursor:
            forward = ET.SubElement(measure, "forward")
            ET.SubElement(forward, "duration").text = str(offset - cursor)
            cursor = offset
        attributes = ET.SubElement(measure, "attributes")
        for change in sorted(offset_changes, key=lambda item: int(item.staff)):
            clef = ET.SubElement(
                attributes,
                "clef",
                number=str(int(change.staff)),
            )
            ET.SubElement(clef, "sign").text = change.sign
            ET.SubElement(clef, "line").text = str(change.line)
    if cursor:
        backup = ET.SubElement(measure, "backup")
        ET.SubElement(backup, "duration").text = str(cursor)


def _notation_atoms(
    notes: list[QuantizedNote],
    score: ScoreModel,
    ottava_spans: list[OttavaSpan] | None = None,
) -> list[NotationNote]:
    atoms: list[NotationNote] = []
    for note in notes:
        if note.staff is None:
            continue
        # MusicXML pitch values remain at concert/sounding pitch under an
        # octave-shift. MuseScore uses the direction for notation and must not
        # receive a second pitch displacement in the note element.
        # Grace notes are always short and never cross a barline, so they skip
        # the measure splitter (a split grace would lose its attachment).
        if note.grace:
            pieces = [(note.onset, note.duration)]
        else:
            pieces = _split_note_across_measures(note.onset, note.duration, score)
        for index, (onset, duration) in enumerate(pieces):
            atoms.append(
                NotationNote(
                    pitch=note.pitch,
                    onset=onset,
                    duration=duration,
                    velocity=note.velocity,
                    staff=note.staff,
                    voice=note.voice,
                    tie_stop=index > 0,
                    tie_start=index + 1 < len(pieces),
                    pitch_step=note.pitch_step,
                    pitch_alter=note.pitch_alter,
                    pitch_octave=note.pitch_octave,
                    hand=note.hand,
                    arpeggiated=note.arpeggiated,
                    trill=note.trill and index == 0,
                    grace=note.grace,
                    staccato=note.staccato and index == 0,
                    tremolo_start=note.tremolo_start and index == 0,
                    tremolo_stop=note.tremolo_stop and index == 0,
                )
            )
    return sorted(atoms, key=lambda atom: (atom.onset, atom.staff, atom.voice, atom.pitch))


def _gap_fillable(duration: int) -> bool:
    return duration == 0 or _exactly_decomposable(duration)


def _grid_step_at(score: ScoreModel, tick: int) -> int | None:
    if not score.grid_decisions:
        return None
    index = measure_index_at(score.measures, tick)
    for decision in score.grid_decisions:
        if decision.measure_index == index:
            return decision.step
    return None


def _within_beat(relative: int, measure: MeasureSpan) -> int:
    """Position within the current beat, end-aligned for pickup measures.

    A pickup (implicit) measure's beats hang off its end — the first full
    barline is the phase reference — not off its start.
    """

    beat = measure.meter.beat_length
    if measure.implicit:
        return (relative - (measure.start + measure.duration)) % beat
    return relative % beat


def _split_note_across_measures(
    onset: int,
    duration: int,
    score: ScoreModel,
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    current = onset
    remaining = duration
    while remaining > 0:
        measure = score.measures[measure_index_at(score.measures, current)]
        relative = current - measure.start
        to_measure_end = measure.end - current
        if to_measure_end <= 0:
            raise ValueError(f"音符位置 {current} 超出小节时间轴")
        if remaining > to_measure_end > 0 and to_measure_end < CANONICAL_DIVISIONS // 16:
            # A fragment this small before the barline cannot be printed:
            # start the written note on the barline instead.
            remaining -= to_measure_end
            current = measure.end
            continue
        if relative == 0 and 0 < remaining < CANONICAL_DIVISIONS // 16:
            # Likewise for a hair of sound trailing into the next measure.
            break
        available = min(remaining, to_measure_end)

        # A complete 6/8 bar is conventionally one dotted half, not two dotted
        # quarters tied together.  Prefer a single exact duration when the note
        # begins at the barline and fills the available measure segment; metric
        # group splitting remains in force for genuine syncopations.
        if relative == 0 and available == to_measure_end and available in SPEC_BY_VALUE:
            pieces.append((current, available))
            current += available
            remaining -= available
            continue

        if _uses_additive_groups(measure.meter):
            available = min(
                available,
                _distance_to_next_group(relative, measure.duration, measure.meter),
            )
        else:
            within_beat = _within_beat(relative, measure)
            if within_beat:
                available = min(available, measure.meter.beat_length - within_beat)

        grid_step = _grid_step_at(score, current)
        piece = _choose_duration_clean(available, grid_step)
        # A leftover smaller than a 64th note cannot be printed.  Let the note
        # cross this beat (never barline) boundary by that hair instead of
        # splitting off a fragment no note type can display.
        tail = remaining - piece
        if (
            0 < tail < CANONICAL_DIVISIONS // 16
            and piece + tail in SPEC_BY_VALUE
            and to_measure_end > available
        ):
            piece += tail
        pieces.append((current, piece))
        current += piece
        remaining -= piece
    return pieces


def _split_readable_span(
    onset: int,
    duration: int,
    measure_length: int,
    meter: Meter,
    implicit: bool = False,
    grid_step: int | None = None,
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    current = onset
    remaining = duration
    while remaining > 0:
        relative = current % measure_length
        to_measure_end = measure_length - relative
        available = min(remaining, to_measure_end)

        if _uses_additive_groups(meter):
            available = min(
                available,
                _distance_to_next_group(relative, measure_length, meter),
            )
        else:
            within_beat = relative % meter.beat_length
            if implicit:
                beat = meter.beat_length
                within_beat = (relative - measure_length % beat) % beat
            if within_beat:
                available = min(available, meter.beat_length - within_beat)

        piece = _choose_duration_clean(available, grid_step)
        pieces.append((current, piece))
        current += piece
        remaining -= piece
    return pieces


def _choose_duration(available: int) -> int:
    if available in SPEC_BY_VALUE:
        return available
    for spec in BINARY_SPECS:
        if spec.value <= available:
            return spec.value
    # Quantized input should never reach this branch; retaining the exact value makes
    # the failure visible to format validation instead of silently dropping time.
    return available


_SPEC_VALUES_DESC = tuple(sorted(SPEC_BY_VALUE, reverse=True))


@cache
def _exactly_decomposable(duration: int) -> bool:
    """Whether *duration* splits into one or more exact note-type specs."""

    if duration in SPEC_BY_VALUE:
        return True
    if duration < _SPEC_VALUES_DESC[-1]:
        return False
    return any(
        _exactly_decomposable(duration - value)
        for value in _SPEC_VALUES_DESC
        if value < duration
    )


def _choose_duration_clean(available: int, grid_step: int | None = None) -> int:
    """Largest exact piece, shrunk until the remaining tail also decomposes.

    Greedy largest-first selection can strand a sub-grid tail (100 = 90 + 10,
    and 10 has no note type).  Shrinking the chosen piece one step (60 + 40)
    keeps every piece exactly representable.  The candidate pool follows the
    measure's grid: ratio members split into their own kind, plain content
    stays binary.
    """

    pool = _spec_values_for_grid(grid_step)

    def choose(avail: int) -> int:
        if avail in SPEC_BY_VALUE:
            return avail
        for value in pool:
            if value <= avail:
                return value
        return avail

    piece = choose(available)
    if piece == available or piece not in SPEC_BY_VALUE:
        return piece
    tail = available - piece
    while tail and not _exactly_decomposable(tail):
        smaller = next((value for value in pool if value < piece), None)
        if smaller is None:
            break
        piece = smaller
        tail = available - piece
    return piece


def _voice_items(
    atoms: list[NotationNote],
    measure_start: int,
    measure_length: int,
    meter: Meter,
    implicit: bool = False,
    grid_step: int | None = None,
    auto_tuplet: bool = False,
) -> list[VoiceItem]:
    if not atoms:
        return [
            VoiceItem(
                onset=0,
                duration=measure_length,
                spec=DurationSpec(measure_length, "whole"),
                notes=[],
                is_rest=True,
                measure_rest=True,
            )
        ]

    grace_atoms = [atom for atom in atoms if atom.grace]
    timed_atoms = [atom for atom in atoms if not atom.grace]
    timed_onsets = {atom.onset for atom in timed_atoms}
    attachable: list[NotationNote] = []
    demoted: list[NotationNote] = []
    for grace in grace_atoms:
        target = grace.onset + grace.duration
        if target in timed_onsets:
            attachable.append(grace)
        else:
            # A demoted grace becomes a plain note; keep its duration printable.
            demoted.append(
                replace(
                    grace,
                    grace=False,
                    duration=max(grace.duration, CANONICAL_DIVISIONS // 16),
                )
            )
    if demoted:
        timed_atoms = sorted(
            [*timed_atoms, *demoted],
            key=lambda atom: (atom.onset, atom.pitch),
        )

    grouped: dict[tuple[int, int], list[NotationNote]] = defaultdict(list)
    for atom in timed_atoms:
        grouped[(atom.onset - measure_start, atom.duration)].append(atom)

    items: list[VoiceItem] = []
    cursor = 0
    for (onset, duration), chord_notes in sorted(grouped.items()):
        gap = onset - cursor
        if gap > 0 and not _gap_fillable(gap):
            # A sub-grid seam between buckets would become an unprintable
            # rest; pull the attack a hair earlier so the gap fills cleanly.
            for shift in range(1, min(gap, CANONICAL_DIVISIONS // 16) + 1):
                if _gap_fillable(gap - shift):
                    onset -= shift
                    break
            else:
                onset = cursor
        if onset > cursor:
            items.extend(_rest_items(cursor, onset - cursor, measure_length, meter, implicit, grid_step))
        if onset < cursor:
            # Never fail the whole conversion on a same-voice overlap, and
            # never lose timeline coverage either: every path keeps the
            # voice's total time intact.
            previous = items[-1] if items else None
            if previous is not None and previous.is_rest:
                # Shrink the gap-fill to end exactly at this attack.
                shrunk = onset - previous.onset
                if shrunk > 0:
                    previous.duration = shrunk
                    previous.spec = SPEC_BY_VALUE.get(
                        shrunk, DurationSpec(shrunk, "64th")
                    )
                else:
                    items.pop()
                cursor = onset
            elif previous is not None and previous.onset == onset:
                # Same attack, another length: fold into the open chord.
                previous.notes.extend(chord_notes)
                previous.notes.sort(key=lambda note: note.pitch)
                continue
            elif previous is not None:
                clipped = onset - previous.onset
                exact = max(
                    (value for value in SPEC_BY_VALUE if value <= clipped),
                    default=0,
                )
                if exact:
                    previous.duration = exact
                    previous.spec = SPEC_BY_VALUE[exact]
                    cursor = previous.onset + exact
                else:
                    # Overlap shorter than any note type: merge into the chord.
                    previous.notes.extend(chord_notes)
                    previous.notes.sort(key=lambda note: note.pitch)
                    continue
        if onset < cursor:
            # Unreachable by construction; kept as a final guard so a bad
            # overlap can never abort the conversion.
            continue
        spec = _spec_for_value(duration, grid_step)
        items.append(
            VoiceItem(
                onset=onset,
                duration=duration,
                spec=spec,
                notes=sorted(chord_notes, key=lambda note: note.pitch),
            )
        )
        cursor = onset + duration

    if cursor < measure_length:
        items.extend(_rest_items(cursor, measure_length - cursor, measure_length, meter, implicit, grid_step))
    for item in items:
        if item.is_rest and (item.onset == 0 or item.onset + item.duration == measure_length):
            item.boundary_rest = True

    if sum(item.duration for item in items) != measure_length:
        raise ValueError(
            f"声部时值总和不等于小节长度：{sum(item.duration for item in items)} != {measure_length}"
        )
    items = _complete_tuplet_groups(items, meter, auto_tuplet=auto_tuplet)
    items = _rescue_stranded_ratio_notes(items)
    if _stream_coherent(items, measure_length):
        items = _snap_free_onsets(items, measure_length, meter, implicit, grid_step)
    if _stream_coherent(items, measure_length):
        items = _enforce_tuplet_group_integrity(items, measure_length, meter, implicit, grid_step)
    items = _reconcile_voice_stream(items, measure_length, meter, implicit, grid_step)
    _mark_beams(items, meter)
    for grace in attachable:
        target = grace.onset + grace.duration - measure_start
        for item in items:
            if not item.is_rest and item.onset == target:
                item.grace_notes.append(grace)
                break
    return items


def _stream_coherent(items: list[VoiceItem], measure_length: int) -> bool:
    """Sorted, contiguous, and inside the measure — the layout assumption the
    snapping/integrity passes work under.  Donor moves in the rescue passes
    can occasionally leave a voice disordered; such a stream must go straight
    to the reconciler, which tolerates any input."""

    cursor = 0
    for item in items:
        if item.onset != cursor or item.duration <= 0:
            return False
        cursor += item.duration
    return cursor == measure_length


def _rescue_stranded_ratio_notes(items: list[VoiceItem]) -> list[VoiceItem]:
    """Re-notate ratio-marked notes that no complete tuplet group claimed.

    A lone 80-tick note (meant as a sextuplet member) cannot be printed as-is:
    bare ratio members hang MuseScore's importer, and no plain note type has
    that length.  When an adjacent padding rest can donate exactly enough time
    to reach a plain, exactly representable value (80 + 40 = a plain 16th),
    the note keeps its attack and the padding shrinks or disappears — total
    voice time is preserved and the result imports cleanly.
    """

    result = list(items)
    for index, item in enumerate(result):
        if item.is_rest or not item.spec.time_modification or item.in_tuplet:
            continue
        candidates = sorted(
            (spec.value for spec in BINARY_SPECS if spec.value != item.duration),
            key=lambda value: (abs(value - item.duration), value),
        )
        for target in candidates:
            delta = target - item.duration
            if delta > 0 and index + 1 < len(result):
                following = result[index + 1]
                if (
                    following.is_rest
                    and not following.measure_rest
                    and not following.in_tuplet
                    and not following.tuplet_start
                    and not following.tuplet_stop
                ):
                    leftover = following.duration - delta
                    if leftover == 0 or leftover in SPEC_BY_VALUE:
                        item.duration = target
                        item.spec = SPEC_BY_VALUE[target]
                        if leftover:
                            result[index + 1] = replace(
                                following,
                                onset=following.onset + delta,
                                duration=leftover,
                                spec=SPEC_BY_VALUE[leftover],
                            )
                        else:
                            result.pop(index + 1)
                        break
            if delta > 0 and index > 0:
                preceding = result[index - 1]
                if (
                    preceding.is_rest
                    and not preceding.measure_rest
                    and not preceding.in_tuplet
                    and not preceding.tuplet_start
                    and not preceding.tuplet_stop
                    and preceding.duration <= delta
                    and item.duration + preceding.duration in SPEC_BY_VALUE
                ):
                    item.onset -= preceding.duration
                    item.duration += preceding.duration
                    item.spec = SPEC_BY_VALUE[item.duration]
                    result.pop(index - 1)
                    break
        # If no adjacent rest can donate, the note stays as written; the batch
        # render gate flags any score where that still happens.
    return _rescue_stranded_ratio_runs(_rescue_with_distant_donors(result))


def _rescue_with_distant_donors(items: list[VoiceItem]) -> list[VoiceItem]:
    """Donate a stranded member's re-notation delta from any rest in the voice.

    The adjacent-rest rescue has nothing to work with when a stranded note
    sits between two notes — so search the whole voice for a padding rest
    that can absorb the delta and stay exactly representable.  All edits go
    through indexed replacement (never value-based removal): equal rests
    occur naturally and removing the wrong one would open a time hole.
    """

    def free_donor(entry: VoiceItem) -> bool:
        return (
            entry.is_rest
            and not entry.measure_rest
            and not entry.in_tuplet
            and not entry.tuplet_start
            and not entry.tuplet_stop
        )

    work = list(items)
    index = 0
    while index < len(work):
        item = work[index]
        if item.is_rest or not item.spec.time_modification or item.in_tuplet:
            index += 1
            continue
        next_note_end = next(
            (
                entry.onset
                for entry in work[index + 1 :]
                if not entry.is_rest
            ),
            None,
        )
        candidates = sorted(
            (spec.value for spec in BINARY_SPECS if spec.value != item.duration),
            key=lambda value: (abs(value - item.duration), value),
        )
        for target in candidates:
            delta = target - item.duration
            if delta > 0 and next_note_end is not None and item.onset + target > next_note_end:
                # Extending into the next attack would overlap it — try a
                # shorter plain value instead.
                continue
            best = None
            for donor_index, donor in enumerate(work):
                if donor_index == index or not free_donor(donor):
                    continue
                if delta > 0:
                    leftover = donor.duration - delta
                    if leftover != 0 and leftover not in SPEC_BY_VALUE:
                        continue
                    score = (abs(donor.onset - item.onset), donor_index)
                    if best is None or score < best[0]:
                        best = (score, donor_index, leftover)
                else:
                    widened = donor.duration - delta
                    if widened not in SPEC_BY_VALUE:
                        continue
                    score = (abs(donor.onset - item.onset), donor_index)
                    if best is None or score < best[0]:
                        best = (score, donor_index, widened)
            if best is None:
                continue
            _, donor_index, remainder = best
            donor = work[donor_index]
            if delta > 0:
                if remainder:
                    work[donor_index] = replace(
                        donor,
                        onset=donor.onset + delta,
                        duration=remainder,
                        spec=SPEC_BY_VALUE[remainder],
                    )
                else:
                    work.pop(donor_index)
                    if donor_index < index:
                        index -= 1
            else:
                work[donor_index] = replace(
                    donor, duration=remainder, spec=SPEC_BY_VALUE[remainder]
                )
            item.duration = target
            item.spec = SPEC_BY_VALUE[target]
            break
        index += 1
    return work


def _rescue_stranded_ratio_runs(items: list[VoiceItem]) -> list[VoiceItem]:
    """Last resort: re-notate a whole stranded run on plain binary values.

    Two triplet eighths at a measure tail (2/3 of a triplet) can never close
    a bracket, and no neighbor may have free time to donate.  Each note drops
    to the nearest shorter plain value and the freed ticks collect in one
    padding rest, keeping the voice's total time and every printed duration
    exact.
    """

    result: list[VoiceItem] = []
    index = 0
    while index < len(items):
        item = items[index]
        if item.is_rest or not item.spec.time_modification or item.in_tuplet:
            result.append(item)
            index += 1
            continue
        run: list[VoiceItem] = []
        stop = index
        while stop < len(items):
            current = items[stop]
            if current.in_tuplet or not current.spec.time_modification:
                break
            run.append(current)
            stop += 1

        freed = 0
        converted: list[VoiceItem] = []
        # Repack the converted members contiguously from the run's attack:
        # shrinking each member in place would leave a sub-grid hole between
        # neighbours, and no rest type can fill those cracks.
        pack_cursor = run[0].onset
        for member in run:
            if member.is_rest:
                freed += member.duration
                continue
            plain = max(
                (spec.value for spec in BINARY_SPECS if spec.value <= member.duration),
                default=None,
            )
            if plain is None:
                plain = min(spec.value for spec in BINARY_SPECS)
            freed += member.duration - plain
            member.onset = pack_cursor
            member.duration = plain
            member.spec = SPEC_BY_VALUE[plain]
            member.beam = None
            converted.append(member)
            pack_cursor += plain
        if freed < 0:
            donor = result[-1] if result else None
            if (
                donor is not None
                and donor.is_rest
                and not donor.in_tuplet
                and donor.duration + freed > 0
            ):
                donor.duration += freed
                donor.spec = SPEC_BY_VALUE.get(
                    donor.duration, DurationSpec(donor.duration, "64th")
                )
                freed = 0
        if freed < 0:
            # No padding anywhere can cover the extension: keep the run as-is.
            result.extend(run)
        else:
            # Keep the padding rest on a plain binary value: shrink one
            # converted note another step until the remainder is clean.
            binary_values = sorted({spec.value for spec in BINARY_SPECS})
            while converted and freed and freed not in binary_values:
                last = converted[-1]
                smaller = max(
                    (value for value in binary_values if value < last.duration),
                    default=None,
                )
                if smaller is None:
                    break
                freed += last.duration - smaller
                last.duration = smaller
                last.spec = SPEC_BY_VALUE[smaller]
            result.extend(converted)
            if freed:
                onset = run[0].onset
                if converted:
                    onset = converted[-1].onset + converted[-1].duration
                result.append(
                    VoiceItem(
                        onset=onset,
                        duration=freed,
                        spec=SPEC_BY_VALUE.get(freed, DurationSpec(freed, "64th")),
                        notes=[],
                        is_rest=True,
                    )
                )
        index = stop
    return result


def _mark_hidden_padding_rests(
    sequences: list[tuple[Staff, int, list[VoiceItem]]],
    measure_length: int,
    meter: Meter,
) -> None:
    sounding_sequences = [
        (voice, items)
        for _, voice, items in sequences
        if any(not item.is_rest for item in items)
    ]
    if len(sounding_sequences) <= 1:
        return

    for voice, items in sounding_sequences:
        other_intervals = [
            (item.onset, item.onset + item.duration)
            for other_voice, other_items in sounding_sequences
            if other_voice != voice
            for item in other_items
            if not item.is_rest
        ]
        note_items = [item for item in items if not item.is_rest]
        sparse_voice = (
            len(note_items) <= 2
            or sum(item.duration for item in note_items) <= measure_length * 0.45
        )
        other_coverage = _covered_duration(other_intervals)
        for item in items:
            if not item.is_rest or item.measure_rest:
                continue
            rest_end = item.onset + item.duration
            fully_covered = _interval_is_covered(item.onset, rest_end, other_intervals)
            boundary_padding = item.onset == 0 or rest_end == measure_length
            overlap = _overlap_duration(item.onset, rest_end, other_intervals)
            short_voice_padding = item.duration <= meter.beat_length and (
                overlap * 2 >= item.duration
                or other_coverage >= measure_length * 0.45
            )
            if (
                fully_covered
                or boundary_padding
                or (sparse_voice and other_coverage >= measure_length * 0.65)
                or (sparse_voice and short_voice_padding)
                or (
                    voice > 1
                    and short_voice_padding
                    and other_coverage >= measure_length * 0.55
                )
            ):
                item.hidden_rest = True


def _interval_is_covered(
    start: int,
    end: int,
    intervals: list[tuple[int, int]],
) -> bool:
    cursor = start
    for interval_start, interval_end in sorted(intervals):
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return True
    return cursor >= end


def _covered_duration(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _overlap_duration(
    start: int,
    end: int,
    intervals: list[tuple[int, int]],
) -> int:
    return _covered_duration(
        [
            (max(start, interval_start), min(end, interval_end))
            for interval_start, interval_end in intervals
            if interval_start < end and interval_end > start
        ]
    )


def _stem_directions(
    sequences: list[tuple[Staff, int, list[VoiceItem]]],
) -> dict[int, str]:
    sounding = [
        (voice, items)
        for _, voice, items in sequences
        if any(not item.is_rest for item in items)
    ]
    if len(sounding) <= 1:
        return {}

    hands: dict[int, Hand | None] = {}
    for voice, items in sounding:
        known = [note.hand for item in items for note in item.notes if note.hand is not None]
        hands[voice] = Counter(known).most_common(1)[0][0] if known else None
    known_hands = {hand for hand in hands.values() if hand is not None}
    if known_hands == {Hand.LEFT, Hand.RIGHT}:
        return {
            voice: "up" if hand == Hand.RIGHT else "down"
            for voice, hand in hands.items()
            if hand is not None
        }

    centers = {
        voice: median(
            note.pitch
            for item in items
            if not item.is_rest
            for note in item.notes
        )
        for voice, items in sounding
    }
    ordered = sorted(centers, key=lambda voice: (centers[voice], -voice), reverse=True)
    return {
        voice: "up" if index == 0 else "down"
        for index, voice in enumerate(ordered)
    }


def _rest_items(
    onset: int,
    duration: int,
    measure_length: int,
    meter: Meter,
    implicit: bool = False,
    grid_step: int | None = None,
) -> list[VoiceItem]:
    return [
        VoiceItem(
            onset=piece_onset,
            duration=piece_duration,
            spec=_spec_for_value(piece_duration, grid_step),
            notes=[],
            is_rest=True,
        )
        for piece_onset, piece_duration in _split_readable_span(
            onset, duration, measure_length, meter, implicit, grid_step
        )
    ]


def _spec_values_for_grid(grid_step: int | None) -> list[int]:
    """Duration values legitimate on the measure's grid: binary values always,
    plus the member units of the grid's ratio family.  Without grid context,
    triplet and sextuplet members stay available (a 400-tick note only splits
    cleanly as 320+80); the exotic quintuplet family joins only on its own
    grid, where it is actually meant.
    """

    values = [spec.value for spec in BINARY_SPECS]
    ratio_by_step = {
        CANONICAL_DIVISIONS // 3: [(3, 2)],
        CANONICAL_DIVISIONS // 6: [(6, 4)],
        CANONICAL_DIVISIONS // 12: [(6, 4)],
        CANONICAL_DIVISIONS // 5: [(5, 4)],
        CANONICAL_DIVISIONS // 10: [(5, 4)],
    }
    families = ratio_by_step.get(grid_step or 0)
    if families is None:
        families = [(3, 2), (6, 4)] if grid_step is None else []
    for ratio in families:
        values += list(TUPLET_MEMBER_SPECS[ratio])
    return sorted(set(values), reverse=True)


def _spec_for_value(duration: int, grid_step: int | None) -> DurationSpec:
    """Pick the duration spec, resolving ratio ambiguity by the local grid.

    Values 80/160/240 live in more than one ratio world: an 80 is a 16th
    sextuplet member on a sextuplet grid but a 16th-triplet member on an
    eighth-triplet grid.  The measure's chosen grid decides.  A value with a
    plain spec (240 = a plain eighth) keeps it: tuplet group completion
    re-specs genuine members, and a stray ratio spec would only send the
    rescue passes hunting for time that was never missing.
    """

    plain = SPEC_BY_VALUE.get(duration)
    if plain is not None and plain.time_modification is None:
        return plain
    if duration in TUPLET_MEMBER_SPECS[(3, 2)] and duration in TUPLET_MEMBER_SPECS[(6, 4)]:
        if grid_step == CANONICAL_DIVISIONS // 3:
            return TUPLET_MEMBER_SPECS[(3, 2)][duration]
        if grid_step in (CANONICAL_DIVISIONS // 6, CANONICAL_DIVISIONS // 12):
            return TUPLET_MEMBER_SPECS[(6, 4)][duration]
    return SPEC_BY_VALUE.get(duration, DurationSpec(duration, "64th"))


def _complete_tuplet_groups(
    items: list[VoiceItem],
    meter: Meter,
    *,
    auto_tuplet: bool = False,
) -> list[VoiceItem]:
    """Bracket tuplet groups, completing them by absorbing padding rests.

    MuseScore's importer hangs on a time-modified item whose measure-mates do
    not fill a complete ratio span, so no unbracketed ratio member may ever
    reach the output.  Complete spans are the ratios' natural power-of-two
    totals (16th triplets close at 240, sextuplets only at 480+, and so on).
    Short groups pull in adjacent padding rests — split into member units when
    necessary, before the group as well as after it — until the span closes
    exactly; absorbed rests keep their hidden/invisible role, so the bracket
    simply spans padding the voice already owned.  For tuplets recovered
    automatically from noisy audio, only groups supported by at least three
    real attacks and more sounding than resting members display a bracket and
    number.  We retain weaker complete groups as invisible timing containers
    so MusicXML playback stays exact without showing speculative tuplets.
    Explicit/direct-MIDI tuplets keep the permissive visible legacy rule.
    """

    expanded: list[VoiceItem] = []
    for item in items:
        expanded.extend(_split_odd_triplet_member(item))

    work = list(expanded)
    result: list[VoiceItem] = []
    group: list[VoiceItem] = []
    group_mod: tuple[int, int] | None = None
    group_sum = 0

    def complete_spans() -> tuple[int, ...]:
        return (480, 960) if group_mod == (6, 4) else (240, 480, 960)

    def is_complete() -> bool:
        if not group or group_sum not in complete_spans():
            return False
        smallest = min(member.duration for member in group)
        if group_sum % smallest:
            return False
        count = group_sum // smallest
        valid_counts = {(3, 2): (3, 6, 12), (6, 4): (6, 12), (5, 4): (5, 10)}
        return count in valid_counts[group_mod]

    def real_attack_count() -> int:
        return len(
            {
                member.onset
                for member in group
                if not member.is_rest
                and any(not note.tie_stop for note in member.notes)
            }
        )

    def auto_group_has_enough_evidence() -> bool:
        if not auto_tuplet:
            return True
        sounding_members = sum(not member.is_rest for member in group)
        rest_members = len(group) - sounding_members
        return real_attack_count() >= 3 and sounding_members > rest_members

    def absorb_backward() -> None:
        nonlocal group_sum
        while not is_complete() and result and result[-1].is_rest:
            tail = result[-1]
            # Never cannibalize rests that already belong to a closed bracket.
            if tail.in_tuplet or tail.tuplet_start or tail.tuplet_stop:
                return
            target = next((span for span in complete_spans() if span > group_sum), None)
            if target is None:
                return
            needed = target - group_sum
            if tail.duration in TUPLET_MEMBER_SPECS[group_mod] and tail.duration <= needed:
                result.pop()
                group.insert(0, tail)
                if tail.spec.time_modification != group_mod:
                    tail.spec = TUPLET_MEMBER_SPECS[group_mod][tail.duration]
                group_sum += tail.duration
                continue
            absorbed = _member_unit_split(min(needed, tail.duration), group_mod)
            if absorbed is None:
                return
            # Carve the absorbed time off the tail's END, right against the
            # group: the new members then sit adjacent to the group's first
            # item, and the untouched remainder keeps the tail's onset.
            onset = tail.onset + tail.duration - sum(absorbed)
            pieces = []
            for part in absorbed:
                pieces.append(
                    VoiceItem(
                        onset=onset,
                        duration=part,
                        spec=TUPLET_MEMBER_SPECS[group_mod][part],
                        notes=[],
                        is_rest=True,
                        hidden_rest=tail.hidden_rest,
                    )
                )
                onset += part
            remainder = tail.duration - sum(absorbed)
            if remainder:
                result[-1] = replace(
                    tail,
                    duration=remainder,
                    spec=SPEC_BY_VALUE.get(remainder, DurationSpec(remainder, "64th")),
                )
            else:
                result.pop()
            group[:0] = pieces
            group_sum += sum(absorbed)

    def try_alternate_ratios() -> None:
        """Re-spec a stalled group under a different complete ratio.

        Three sextuplet sixteenths stalling at 240 ticks are, read as 16th
        triplets, a complete 3:2 group — the written durations are identical,
        only the ratio label changes.  Trying the aliases rescues groups that
        absorption could not complete.
        """

        nonlocal group_mod
        durations = [member.duration for member in group]
        for alternative in ((3, 2), (6, 4), (5, 4)):
            if alternative == group_mod:
                continue
            table = TUPLET_MEMBER_SPECS[alternative]
            if not all(duration in table for duration in durations):
                continue
            previous = group_mod
            group_mod = alternative
            if is_complete():
                for member in group:
                    member.spec = table[member.duration]
                return
            group_mod = previous

    def close_group() -> None:
        nonlocal group, group_mod, group_sum
        if group and not is_complete():
            absorb_backward()
        if group and not is_complete():
            try_alternate_ratios()
        if is_complete() and len(group) >= 2:
            hide_auto_group = not auto_group_has_enough_evidence()
            group[0].tuplet_start = True
            group[-1].tuplet_stop = True
            for member in group:
                member.in_tuplet = True
                if hide_auto_group:
                    member.tuplet_hidden = True
                    if member.is_rest:
                        member.hidden_rest = True
        result.extend(group)
        group = []
        group_mod = None
        group_sum = 0

    index = 0
    while index < len(work):
        item = work[index]
        if group:
            contiguous = group[-1].onset + group[-1].duration == item.onset
            if contiguous and not is_complete():
                target = next((span for span in complete_spans() if span > group_sum), None)
                needed = (target - group_sum) if target else 0
                if item.duration in TUPLET_MEMBER_SPECS[group_mod]:
                    if item.spec.time_modification != group_mod:
                        item.spec = TUPLET_MEMBER_SPECS[group_mod][item.duration]
                    group.append(item)
                    group_sum += item.duration
                    index += 1
                    continue
                if item.is_rest and needed > 0:
                    absorbed = _member_unit_split(min(needed, item.duration), group_mod)
                    if absorbed is not None:
                        onset = item.onset
                        for part in absorbed:
                            group.append(
                                VoiceItem(
                                    onset=onset,
                                    duration=part,
                                    spec=TUPLET_MEMBER_SPECS[group_mod][part],
                                    notes=[],
                                    is_rest=True,
                                    hidden_rest=item.hidden_rest,
                                )
                            )
                            onset += part
                        remainder = item.duration - sum(absorbed)
                        if remainder:
                            work[index] = replace(
                                item,
                                onset=item.onset + sum(absorbed),
                                duration=remainder,
                                spec=SPEC_BY_VALUE.get(
                                    remainder, DurationSpec(remainder, "64th")
                                ),
                            )
                        else:
                            work.pop(index)
                        group_sum += sum(absorbed)
                        continue
            close_group()
            continue

        mod = item.spec.time_modification
        if mod is not None and item.duration in TUPLET_MEMBER_SPECS.get(mod, {}):
            group = [item]
            group_mod = mod
            group_sum = item.duration
            index += 1
            continue
        result.append(item)
        index += 1

    close_group()
    # Backward absorption can reorder items; the measure stream must stay
    # onset-monotonic or importers lose the timeline.
    result.sort(key=lambda item: item.onset)
    return result


def _member_unit_split(duration: int, mod: tuple[int, int]) -> list[int] | None:
    """Split *duration* into member units of the given tuplet ratio."""

    members = sorted(TUPLET_MEMBER_SPECS[mod], reverse=True)
    pieces: list[int] = []
    remaining = duration
    for unit in members:
        while remaining >= unit:
            pieces.append(unit)
            remaining -= unit
    if remaining:
        return None
    return pieces


def _split_odd_triplet_member(item: VoiceItem) -> list[VoiceItem]:
    """Split a triplet-grid value with no single notehead (400 = 320+80)."""

    if item.is_rest or item.spec.time_modification or item.duration != 400:
        return [item]
    if any(note.tie_start or note.tie_stop for note in item.notes):
        # Already part of a tie chain; leave the exact value untouched rather
        # than disturbing the surrounding notation.
        return [item]
    first_spec = TUPLET_MEMBER_SPECS[(3, 2)][320]
    second_spec = TUPLET_MEMBER_SPECS[(3, 2)][80]
    first_notes = [replace(note, tie_start=True) for note in item.notes]
    second_notes = [replace(note, tie_stop=True) for note in item.notes]
    return [
        VoiceItem(item.onset, 320, first_spec, first_notes),
        VoiceItem(item.onset + 320, 80, second_spec, second_notes),
    ]


def _enforce_tuplet_group_integrity(
    items: list[VoiceItem],
    measure_length: int,
    meter: Meter,
    implicit: bool,
    grid_step: int | None,
) -> list[VoiceItem]:
    """Final guarantee: every emitted bracket is balanced and complete.

    A bracket whose members no longer sum to a complete ratio span — or whose
    span hides a non-member item — hangs or corrupts importers.  Strip such a
    group's flags and let the plain re-notation pass re-write the members.
    """

    out = list(items)
    changed = False
    index = 0
    while index < len(out):
        if not out[index].tuplet_start:
            index += 1
            continue
        depth = 0
        stop = index
        while stop < len(out):
            current = out[stop]
            if current.tuplet_start:
                depth += 1
            if current.tuplet_stop:
                depth -= 1
                if depth == 0:
                    break
            stop += 1
        members = out[index : stop + 1] if stop < len(out) else out[index:]
        mod = out[index].spec.time_modification
        total = sum(member.duration for member in members)
        all_members = all(member.in_tuplet for member in members)
        valid = (
            depth == 0
            and mod is not None
            and all_members
            and total in (240, 480, 960)
            and total % min(member.duration for member in members) == 0
            and total // min(member.duration for member in members)
            in {(3, 2): (3, 6, 12), (6, 4): (6, 12), (5, 4): (5, 10)}.get(mod, ())
        )
        if not valid:
            for member in members:
                member.tuplet_start = False
                member.tuplet_stop = False
                member.in_tuplet = False
            changed = True
        index = stop + 1

    if changed:
        out = _rescue_stranded_ratio_runs(out)
        out = _eliminate_micro_rests(out, measure_length, meter, implicit, grid_step)
    return out


def _snap_free_onsets(
    items: list[VoiceItem],
    measure_length: int,
    meter: Meter,
    implicit: bool,
    grid_step: int | None,
) -> list[VoiceItem]:
    """Align free-floating note onsets to the finest grid (30 ticks).

    Bucket seams and rescue moves leave some notes a few ticks off any
    writable position; the gaps around them then need rests no note type can
    print, and importers reject the resulting underfull measure.  Snapping a
    free note (an inaudible <=15-tick move) keeps the voice stream exactly
    recomputable.  Tuplet members — notes *and* their member rests — keep
    their exact ratio positions, and a note adjacent to a member stays put.
    """

    def is_member(item: VoiceItem) -> bool:
        return item.in_tuplet or item.spec.time_modification is not None

    member_spans = [
        (item.onset, item.onset + item.duration) for item in items if is_member(item)
    ]

    def collides(onset: int, duration: int) -> bool:
        return any(onset < end and start < onset + duration for start, end in member_spans)

    result: list[VoiceItem] = []
    for index, item in enumerate(items):
        if item.is_rest or is_member(item):
            result.append(item)
            continue
        previous = items[index - 1] if index else None
        following = items[index + 1] if index + 1 < len(items) else None
        snapped = round(item.onset / (CANONICAL_DIVISIONS // 16)) * (
            CANONICAL_DIVISIONS // 16
        )
        low = previous.onset + previous.duration if previous is not None else 0
        high = following.onset if following is not None else measure_length
        onset = min(max(snapped, low), max(low, high - item.duration))
        if collides(onset, item.duration):
            onset = item.onset
        # Round the duration onto the grid too: with every note boundary on
        # the 64th grid, the rest fill between them is always recomputable
        # and no sub-grid crack can survive.  Never round past the next item.
        duration = max(
            CANONICAL_DIVISIONS // 16,
            round(item.duration / (CANONICAL_DIVISIONS // 16)) * (
                CANONICAL_DIVISIONS // 16
            ),
        )
        duration = min(duration, high - onset, measure_length - onset)
        # The coerced duration must stay exactly writable: a non-spec value
        # (or a ratio spec from the local grid) would print a note whose type
        # disagrees with its duration.  Fall to the largest plain value that
        # fits; the refill below covers the freed ticks.
        spec = SPEC_BY_VALUE.get(duration)
        if spec is None or spec.time_modification is not None:
            plain = next(
                (value for value in _PLAIN_SPEC_VALUES if value <= duration),
                None,
            )
            if plain is None or plain < CANONICAL_DIVISIONS // 16:
                result.append(item)
                continue
            duration = plain
            spec = SPEC_BY_VALUE[duration]
        if duration != item.duration or onset != item.onset or spec != item.spec:
            result.append(replace(item, onset=onset, duration=duration, spec=spec))
        else:
            result.append(item)

    # Rebuild the rest fill around the final note positions, keeping
    # tuplet-member rests at their exact spots.  Nothing is ever dropped:
    # an item displaced by snapping is clamped into the available span, and
    # one with no room left folds into the previous chord — the voice's total
    # time and its noteheads both survive.
    stream: list[VoiceItem] = []
    cursor = 0
    remaining = list(result)
    for position, item in enumerate(remaining):
        if item.is_rest and not is_member(item):
            continue
        next_boundary = measure_length
        for later in remaining[position + 1 :]:
            if not later.is_rest or is_member(later):
                next_boundary = later.onset
                break
        onset = max(item.onset, cursor)
        if onset > cursor:
            stream.extend(
                _rest_items(
                    cursor, onset - cursor, measure_length, meter, implicit, grid_step
                )
            )
        room = next_boundary - onset
        if room >= item.duration or is_member(item):
            stream.append(replace(item, onset=onset))
            cursor = onset + item.duration
            continue
        if room >= CANONICAL_DIVISIONS // 16:
            # The clipped duration must stay exactly writable: an arbitrary
            # room value (or a ratio spec from the local grid) would print a
            # note whose type disagrees with its duration.
            plain = max(
                (value for value in _PLAIN_SPEC_VALUES if value <= room),
                default=CANONICAL_DIVISIONS // 16,
            )
            stream.append(
                replace(item, onset=onset, duration=plain, spec=SPEC_BY_VALUE[plain])
            )
            cursor = onset + plain
            continue
        # Less than a 64th of room: fold the pitches into the open chord, or
        # steal a 64th from the padding rest so the attack is never dropped.
        if stream and not stream[-1].is_rest:
            stream[-1].notes.extend(item.notes)
            stream[-1].notes.sort(key=lambda note: note.pitch)
        elif (
            stream
            and stream[-1].is_rest
            and not is_member(stream[-1])
            and stream[-1].duration > CANONICAL_DIVISIONS // 16
        ):
            stream[-1].duration -= CANONICAL_DIVISIONS // 16
            stream[-1].spec = SPEC_BY_VALUE.get(
                stream[-1].duration, DurationSpec(stream[-1].duration, "64th")
            )
            stolen = stream[-1].onset + stream[-1].duration
            stream.append(
                replace(
                    item,
                    onset=stolen,
                    duration=CANONICAL_DIVISIONS // 16,
                    spec=SPEC_BY_VALUE[CANONICAL_DIVISIONS // 16],
                )
            )
            cursor = stolen + CANONICAL_DIVISIONS // 16
    if cursor < measure_length:
        stream.extend(
            _rest_items(cursor, measure_length - cursor, measure_length, meter, implicit, grid_step)
        )
    return _eliminate_micro_rests(stream, measure_length, meter, implicit, grid_step)


def _eliminate_micro_rests(
    stream: list[VoiceItem],
    measure_length: int,
    meter: Meter,
    implicit: bool,
    grid_step: int | None,
) -> list[VoiceItem]:
    """Absorb rests no note type can print into their neighbourhood.

    Importers drop or misread tiny `<forward>` skips, which leaves the measure
    underfull, so no sub-grid free rest may survive.  A crack merges into the
    free rest run before it (re-split into clean values), or shifts a
    following free note earlier, or slides a whole tuplet group as one block
    (member spacing is preserved), or extends a previous note's clean value.
    Total voice time never changes.
    """

    def free_rest(entry: VoiceItem) -> bool:
        return (
            entry.is_rest
            and not entry.in_tuplet
            and entry.spec.time_modification is None
        )

    out: list[VoiceItem] = []
    index = 0
    while index < len(stream):
        item = stream[index]
        if not item.is_rest or item.in_tuplet or item.spec.time_modification is not None or item.duration in SPEC_BY_VALUE:
            out.append(item)
            index += 1
            continue

        crack = item.duration
        # 1) merge into the free rest run before the crack
        if out and free_rest(out[-1]):
            merged = out[-1].duration + crack
            if _exactly_decomposable(merged):
                onset = out[-1].onset
                out[-1:] = _rest_items(onset, merged, measure_length, meter, implicit, grid_step)
                index += 1
                continue
        following = stream[index + 1] if index + 1 < len(stream) else None
        # 2) shift a following free note earlier to close the crack — but only
        # when the displaced ticks land in a free rest right after the note.
        # Moving the note also moves its tail; without a rest there to absorb
        # the crack, the hole simply re-opens behind the note (and a tail hole
        # at the measure end silently shortens the voice).
        if (
            following is not None
            and not following.is_rest
            and not following.in_tuplet
            and following.spec.time_modification is None
        ):
            after = stream[index + 2] if index + 2 < len(stream) else None
            if after is not None and free_rest(after):
                grown = after.duration + crack
                if _exactly_decomposable(grown):
                    following.onset -= crack
                    stream[index + 2 : index + 3] = _rest_items(
                        following.onset + following.duration,
                        grown,
                        measure_length,
                        meter,
                        implicit,
                        grid_step,
                    )
                    index += 1
                    continue
        # 3) slide a following tuplet group earlier as one block
        if following is not None and (following.in_tuplet or following.spec.time_modification is not None):
            group_end = index + 1
            while group_end < len(stream) and (
                stream[group_end].in_tuplet
                or stream[group_end].spec.time_modification is not None
            ):
                group_end += 1
            can_slide = True
            if out:
                last_end = out[-1].onset + out[-1].duration
                if following.onset - crack < last_end and not free_rest(out[-1]):
                    can_slide = False
            if can_slide:
                if out and free_rest(out[-1]):
                    # The rest before the crack donates the space: shrink it
                    # (or drop it) and slide the group into the freed span.
                    shrunk = out[-1].duration - crack
                    if shrunk >= CANONICAL_DIVISIONS // 16:
                        out[-1].duration = shrunk
                        out[-1].spec = SPEC_BY_VALUE.get(shrunk, DurationSpec(shrunk, "64th"))
                    else:
                        out.pop()
                    for member_index in range(index + 1, group_end):
                        stream[member_index].onset -= crack
                    index += 1
                    continue
                # A note precedes the crack: sliding the group earlier leaves
                # the crack behind the group, so it must land in a free rest
                # right after the group — otherwise the slide just moves the
                # hole downstream and shortens the voice.
                landing = stream[group_end] if group_end < len(stream) else None
                if (
                    landing is not None
                    and free_rest(landing)
                    and _exactly_decomposable(landing.duration + crack)
                ):
                    grown = landing.duration + crack
                    for member_index in range(index + 1, group_end):
                        stream[member_index].onset -= crack
                    group_tail = stream[group_end - 1]
                    stream[group_end : group_end + 1] = _rest_items(
                        group_tail.onset + group_tail.duration,
                        grown,
                        measure_length,
                        meter,
                        implicit,
                        grid_step,
                    )
                    index += 1
                    continue
        # 4) extend the previous free note by a clean value.  Only a plain
        # binary spec may result: extending into a ratio-member value would
        # re-create the bare time-modified note the rescue passes just
        # removed, and extending a tuplet member would break its group span.
        previous_note = next((entry for entry in reversed(out) if not entry.is_rest), None)
        if previous_note is not None:
            extended = previous_note.duration + crack
            spec = SPEC_BY_VALUE.get(extended)
            if (
                spec is not None
                and spec.time_modification is None
                and not previous_note.in_tuplet
                and previous_note.spec.time_modification is None
            ):
                previous_note.duration = extended
                previous_note.spec = spec
                index += 1
                continue
        # 5) no landing spot: keep the crack as an untyped skip
        out.append(item)
        index += 1
    return out


def _reconcile_voice_stream(
    items: list[VoiceItem],
    measure_length: int,
    meter: Meter,
    implicit: bool,
    grid_step: int | None,
) -> list[VoiceItem]:
    """Hard guarantee: the voice covers 0..measure_length contiguously and
    every item is writable.

    The re-notation passes above are careful, but a pathological voice can
    still slip a hole or a sub-grid fragment through them, and an importer
    then reports the measure corrupt.  Stray ratio rests first merge into
    plain rest runs where their total allows it; anything still unwritable
    re-lays the whole voice on the 64th grid: attacks move by less than a
    32nd note, no notehead is dropped, and the stream always loads.
    """

    quantum = CANONICAL_DIVISIONS // 16

    # Rests that lost their tuplet bracket leave the file as time skips, and
    # skips only account correctly on the 64th grid.  A consecutive run of
    # them whose total is on the grid re-splits into plain rests instead.
    normalized: list[VoiceItem] = []
    index = 0
    while index < len(items):
        item = items[index]
        if item.is_rest and not item.in_tuplet and item.spec.time_modification is not None:
            run_end = index
            total = 0
            while run_end < len(items):
                current = items[run_end]
                if not (
                    current.is_rest
                    and not current.in_tuplet
                    and current.spec.time_modification is not None
                ):
                    break
                total += current.duration
                run_end += 1
            if total and total % quantum == 0:
                normalized.extend(_plain_rest_items(item.onset, total))
            else:
                normalized.extend(items[index:run_end])
            index = run_end
            continue
        normalized.append(item)
        index += 1
    items = normalized

    def writable(item: VoiceItem) -> bool:
        tm = item.spec.time_modification
        if item.is_rest:
            if item.measure_rest:
                return True
            if tm is not None:
                # Member rests print literally inside the bracket; a stray
                # ratio rest becomes an off-grid time skip.
                return item.in_tuplet
            if item.duration in SPEC_BY_VALUE:
                return item.spec.value == item.duration
            return item.duration >= quantum and item.duration % quantum == 0
        # A note must print exactly what it is: the spec's value matches the
        # duration, and a ratio spec only survives inside a bracketed group.
        return (
            item.spec.value == item.duration
            and (tm is None or item.in_tuplet)
        )

    cursor = 0
    clean = True
    for item in items:
        if item.onset != cursor or not writable(item):
            clean = False
            break
        cursor += item.duration
    if clean and cursor == measure_length:
        return items

    # Last-resort rebuild.  Valid tuplet groups keep their exact spans (their
    # member spacing is musical information); only the free content around
    # them is re-laid on the 64th grid.  A trailing hole a group cannot start
    # after is absorbed by sliding the whole group earlier by its sub-grid
    # part, so every gap stays on the grid and no note time is lost.
    segments: list[list[VoiceItem]] = []
    groups: list[list[VoiceItem]] = []
    current: list[VoiceItem] = []
    index = 0
    while index < len(items):
        item = items[index]
        if item.in_tuplet:
            group: list[VoiceItem] = []
            while index < len(items) and items[index].in_tuplet:
                group.append(items[index])
                index += 1
            segments.append(current)
            groups.append(group)
            current = []
            continue
        if not item.is_rest and item.notes:
            current.append(item)
        index += 1
    segments.append(current)

    if not groups and not any(segments):
        filled = _rest_items(0, measure_length, measure_length, meter, implicit, grid_step)
        if filled:
            return filled
        return [
            VoiceItem(
                onset=0,
                duration=measure_length,
                spec=DurationSpec(measure_length, "whole"),
                notes=[],
                is_rest=True,
                measure_rest=True,
            )
        ]

    def plain_duration(duration: int) -> int:
        value = max(quantum, round(duration / quantum) * quantum)
        return next(v for v in _PLAIN_SPEC_VALUES if v <= value)

    out: list[VoiceItem] = []
    cursor = 0
    for seg_index, segment in enumerate(segments):
        group = groups[seg_index] if seg_index < len(groups) else None
        limit = group[0].onset if group else measure_length
        for item in segment:
            duration = plain_duration(item.duration)
            onset = max(cursor, round(item.onset / quantum) * quantum)
            if onset + duration > limit:
                # Prefer an earlier grid slot that keeps the note whole.
                onset = max(cursor, (limit - duration) // quantum * quantum)
            if onset + duration > limit:
                # No grid slot fits the full note: clip to the room left.
                room = limit - onset
                if room < quantum:
                    if (
                        out
                        and out[-1].is_rest
                        and out[-1].duration > quantum
                    ):
                        # Steal a 64th of padding so the attack survives; the
                        # stolen slot always ends at or before the limit.
                        out[-1].duration -= quantum
                        out[-1].spec = SPEC_BY_VALUE.get(
                            out[-1].duration, DurationSpec(out[-1].duration, "64th")
                        )
                        stolen = out[-1].onset + out[-1].duration
                        out.append(
                            replace(
                                item,
                                onset=stolen,
                                duration=quantum,
                                spec=SPEC_BY_VALUE[quantum],
                            )
                        )
                    else:
                        # Fold the pitches into the nearest open chord rather
                        # than dropping noteheads.
                        target = next(
                            (entry for entry in reversed(out) if not entry.is_rest),
                            None,
                        )
                        if target is not None:
                            target.notes.extend(item.notes)
                            target.notes.sort(key=lambda note: note.pitch)
                    continue
                duration = next(v for v in _PLAIN_SPEC_VALUES if v <= room)
            if onset > cursor:
                out.extend(_plain_rest_items(cursor, onset - cursor))
            out.append(
                replace(item, onset=onset, duration=duration, spec=SPEC_BY_VALUE[duration])
            )
            cursor = onset + duration
        if group is None:
            break
        # The hole between the laid-out content and the group must stay on the
        # grid; slide the whole group earlier by the hole's sub-grid part.
        hole = group[0].onset - cursor
        slide = hole % quantum
        if slide and group[0].onset - slide >= cursor:
            group = [replace(member, onset=member.onset - slide) for member in group]
            hole -= slide
        if hole > 0:
            out.extend(_plain_rest_items(cursor, hole))
            cursor += hole
        out.extend(group)
        cursor = group[-1].onset + group[-1].duration
    if cursor < measure_length:
        out.extend(_plain_rest_items(cursor, measure_length - cursor))
    return out


def _plain_rest_items(onset: int, duration: int) -> list[VoiceItem]:
    """Split a gap into plain (ratio-free) rests.

    Every gap handled here is a 64th-grid multiple, which always decomposes
    into plain values — ratio-member rests would only leave the file again as
    potentially off-grid skips.
    """

    pieces: list[VoiceItem] = []
    remaining = duration
    while remaining >= CANONICAL_DIVISIONS // 16:
        value = next(v for v in _PLAIN_SPEC_VALUES if v <= remaining)
        pieces.append(
            VoiceItem(
                onset=onset,
                duration=value,
                spec=SPEC_BY_VALUE[value],
                notes=[],
                is_rest=True,
            )
        )
        onset += value
        remaining -= value
    return pieces


def _mark_beams(items: list[VoiceItem], meter: Meter) -> None:
    run: list[VoiceItem] = []
    for item in items + [VoiceItem(0, 0, DurationSpec(0, "quarter"), [], True)]:
        beamable = not item.is_rest and item.duration <= CANONICAL_DIVISIONS // 2
        same_beat = bool(run) and _metric_group_index(
            item.onset, meter
        ) == _metric_group_index(run[0].onset, meter)
        contiguous = bool(run) and run[-1].onset + run[-1].duration == item.onset
        if beamable and (not run or (same_beat and contiguous)):
            run.append(item)
            continue
        if len(run) >= 2:
            run[0].beam = "begin"
            for middle in run[1:-1]:
                middle.beam = "continue"
            run[-1].beam = "end"
        run = [item] if beamable else []


def _uses_additive_groups(meter: Meter) -> bool:
    return any(group != meter.beat_length for group in meter.beat_groups)


def _distance_to_next_group(relative: int, measure_length: int, meter: Meter) -> int:
    for boundary in meter.beat_group_boundaries[1:]:
        if boundary > relative:
            return max(1, min(boundary, measure_length) - relative)
    return max(1, measure_length - relative)


def _metric_group_index(onset: int, meter: Meter) -> int:
    for index, boundary in enumerate(meter.beat_group_boundaries[1:]):
        if onset < boundary:
            return index
    return len(meter.beat_groups) - 1


def _write_forward(measure: ET.Element, duration: int, staff: Staff, voice: int) -> None:
    forward = ET.SubElement(measure, "forward")
    ET.SubElement(forward, "duration").text = str(duration)
    ET.SubElement(forward, "voice").text = str(voice)
    ET.SubElement(forward, "staff").text = str(int(staff))


def _write_item(
    measure: ET.Element,
    item: VoiceItem,
    staff: Staff,
    voice: int,
    fifths: int,
    stem_direction: str | None,
) -> None:
    if item.is_rest:
        note_attributes = {"print-object": "no"} if item.hidden_rest else {}
        note_element = ET.SubElement(measure, "note", note_attributes)
        rest_attributes = {"measure": "yes"} if item.measure_rest else {}
        ET.SubElement(note_element, "rest", rest_attributes)
        _write_duration_details(
            note_element,
            item,
            staff,
            voice,
            stem_direction,
            first_in_chord=True,
        )
        # Rests are full tuplet members in MusicXML; without the bracket a
        # triplet group starting or ending on a rest corrupts the measure for
        # stricter importers such as MuseScore.
        if item.tuplet_start or item.tuplet_stop:
            notations = ET.SubElement(note_element, "notations")
            if item.tuplet_start:
                _write_tuplet_start(notations, item)
            if item.tuplet_stop:
                ET.SubElement(notations, "tuplet", type="stop")
        return

    for grace in item.grace_notes:
        grace_element = ET.SubElement(measure, "note")
        ET.SubElement(grace_element, "grace", slash="yes")
        _write_pitch(grace_element, grace, fifths)
        ET.SubElement(grace_element, "voice").text = str(voice)
        ET.SubElement(grace_element, "type").text = "eighth"
        if stem_direction:
            ET.SubElement(grace_element, "stem").text = stem_direction
        ET.SubElement(grace_element, "staff").text = str(int(staff))

    for note_index, notation_note in enumerate(item.notes):
        note_element = ET.SubElement(measure, "note")
        if note_index:
            ET.SubElement(note_element, "chord")
        _write_pitch(note_element, notation_note, fifths)
        _write_duration_details(
            note_element,
            item,
            staff,
            voice,
            stem_direction,
            first_in_chord=note_index == 0,
            tie_stop=notation_note.tie_stop,
            tie_start=notation_note.tie_start,
        )

        chord_staccato = note_index == 0 and any(note.staccato for note in item.notes)
        if (
            notation_note.tie_stop
            or notation_note.tie_start
            or item.tuplet_start
            or item.tuplet_stop
            or notation_note.arpeggiated
            or notation_note.trill
            or chord_staccato
            or notation_note.tremolo_start
            or notation_note.tremolo_stop
        ):
            notations = ET.SubElement(note_element, "notations")
            if notation_note.tie_stop:
                ET.SubElement(notations, "tied", type="stop")
            if notation_note.tie_start:
                ET.SubElement(notations, "tied", type="start")
            if note_index == 0 and item.tuplet_start:
                _write_tuplet_start(notations, item)
            if note_index == 0 and item.tuplet_stop:
                ET.SubElement(notations, "tuplet", type="stop")
            if notation_note.arpeggiated:
                ET.SubElement(notations, "arpeggiate", number="1")
            if notation_note.trill:
                ornaments = ET.SubElement(notations, "ornaments")
                ET.SubElement(ornaments, "trill-mark")
            if chord_staccato:
                articulations = ET.SubElement(notations, "articulations")
                ET.SubElement(articulations, "staccato")
            if notation_note.tremolo_start or notation_note.tremolo_stop:
                ornaments = ET.SubElement(notations, "ornaments")
                tremolo_type = "start" if notation_note.tremolo_start else "stop"
                ET.SubElement(ornaments, "tremolo", type=tremolo_type).text = "3"


def _write_tuplet_start(notations: ET.Element, item: VoiceItem) -> None:
    attributes = {"type": "start"}
    if item.tuplet_hidden:
        attributes.update({"bracket": "no", "show-number": "none"})
    else:
        attributes["bracket"] = "auto"
    ET.SubElement(notations, "tuplet", attributes)


def _write_pitch(
    note_element: ET.Element,
    notation_note: NotationNote,
    fifths: int,
) -> None:
    pitch = ET.SubElement(note_element, "pitch")
    if notation_note.pitch_step is not None and notation_note.pitch_octave is not None:
        step = notation_note.pitch_step
        alter = notation_note.pitch_alter
        octave = notation_note.pitch_octave
    else:
        spelling = FLAT_PITCHES if fifths < 0 else SHARP_PITCHES
        step, alter = spelling[notation_note.pitch % 12]
        octave = notation_note.pitch // 12 - 1
    ET.SubElement(pitch, "step").text = step
    if alter:
        ET.SubElement(pitch, "alter").text = str(alter)
    ET.SubElement(pitch, "octave").text = str(octave)


def _write_duration_details(
    note_element: ET.Element,
    item: VoiceItem,
    staff: Staff,
    voice: int,
    stem_direction: str | None,
    first_in_chord: bool,
    tie_stop: bool = False,
    tie_start: bool = False,
) -> None:
    ET.SubElement(note_element, "duration").text = str(item.duration)
    if tie_stop:
        ET.SubElement(note_element, "tie", type="stop")
    if tie_start:
        ET.SubElement(note_element, "tie", type="start")
    ET.SubElement(note_element, "voice").text = str(voice)
    ET.SubElement(note_element, "type").text = "whole" if item.measure_rest else item.spec.note_type
    if not item.measure_rest:
        for _ in range(item.spec.dots):
            ET.SubElement(note_element, "dot")
        if item.spec.time_modification:
            actual, normal = item.spec.time_modification
            time_modification = ET.SubElement(note_element, "time-modification")
            ET.SubElement(time_modification, "actual-notes").text = str(actual)
            ET.SubElement(time_modification, "normal-notes").text = str(normal)
    if stem_direction and not item.is_rest:
        ET.SubElement(note_element, "stem").text = stem_direction
    if first_in_chord and item.beam:
        ET.SubElement(note_element, "beam", number="1").text = item.beam
    ET.SubElement(note_element, "staff").text = str(int(staff))


def _ottava_directions_by_measure(
    score: ScoreModel,
    spans: list[OttavaSpan],
) -> dict[int, list[tuple[int, Staff, str, int, str]]]:
    result: dict[int, list[tuple[int, Staff, str, int, str]]] = defaultdict(list)
    for span in spans:
        placement = "above" if span.direction == "down" else "below"
        start_measure, start_offset = _score_time_location(score, span.start)
        stop_measure, stop_offset = _score_time_location(score, span.end)
        result[start_measure].append(
            (start_offset, span.staff, span.direction, span.size, placement)
        )
        result[stop_measure].append(
            (stop_offset, span.staff, "stop", span.size, placement)
        )
    for directions in result.values():
        directions.sort(key=lambda item: (item[0], item[2] != "stop", int(item[1])))
    return result


def _score_time_location(score: ScoreModel, tick: int) -> tuple[int, int]:
    for index, measure in enumerate(score.measures):
        if tick == measure.start:
            return index, 0
        if measure.start < tick < measure.end:
            return index, tick - measure.start
        if tick == measure.end:
            if index + 1 < len(score.measures):
                return index + 1, 0
            return index, measure.duration
    raise ValueError(f"八度线位置 {tick} 超出乐谱时间轴")


def _pedals_by_measure(score: ScoreModel) -> dict[int, list[tuple[int, bool]]]:
    result: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    previous: bool | None = None
    score_end = score.measures[-1].end
    for pedal in sorted(score.pedals, key=lambda item: item.tick):
        if pedal.down == previous or pedal.tick < 0:
            continue
        if pedal.tick > score_end:
            continue
        if pedal.tick == score_end:
            measure_index = score.measure_count - 1
            offset = score.measures[-1].duration
        else:
            measure_index = measure_index_at(score.measures, pedal.tick)
            offset = pedal.tick - score.measures[measure_index].start
        result[measure_index].append((offset, pedal.down))
        previous = pedal.down
    return result


def musicxml_readability_metrics(musicxml: str) -> dict[str, object]:
    root = ET.fromstring(musicxml)
    rests = [note for note in root.findall(".//note") if note.find("rest") is not None]
    octave_shifts = root.findall(".//direction-type/octave-shift")
    starts = [
        shift
        for shift in octave_shifts
        if shift.get("type") in {"up", "down"}
    ]
    tuplet_starts = root.findall(".//notations/tuplet[@type='start']")
    tuplet_stops = root.findall(".//notations/tuplet[@type='stop']")
    hidden_tuplet_starts = [
        start for start in tuplet_starts if start.get("show-number") == "none"
    ]
    return {
        "hidden_padding_rests": sum(note.get("print-object") == "no" for note in rests),
        "visible_rests": sum(note.get("print-object") != "no" for note in rests),
        "voice_time_skips": len(root.findall(".//forward")),
        "measure_rests": sum(
            (rest := note.find("rest")) is not None and rest.get("measure") == "yes"
            for note in rests
        ),
        "ottava_spans": len(starts),
        "ottava_sizes": dict(
            sorted(Counter(int(shift.get("size", "8")) for shift in starts).items())
        ),
        "arpeggiated_noteheads": len(root.findall(".//notations/arpeggiate")),
        "tuplet_spans": len(tuplet_starts),
        "visible_tuplet_spans": len(tuplet_starts) - len(hidden_tuplet_starts),
        "hidden_tuplet_spans": len(hidden_tuplet_starts),
        "unbalanced_tuplet_brackets": abs(len(tuplet_starts) - len(tuplet_stops)),
    }
