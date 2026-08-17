from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean
from xml.etree import ElementTree as ET

from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.clefs import clef_kind_at  # noqa: E402
from app.core.engraver import render_a4_musicxml  # noqa: E402
from app.core.meter_map import measure_index_at  # noqa: E402
from app.core.models import Hand, KeyEstimate, ScoreModel, Staff  # noqa: E402
from app.core.options import ConversionOptions  # noqa: E402
from app.core.ottava import OttavaSpan, detect_ottava_spans  # noqa: E402
from app.core.pipeline import convert_midi_with_score  # noqa: E402


@dataclass(frozen=True, slots=True)
class ReferenceSpec:
    slug: str
    title: str
    midi: Path
    pdf: Path
    vector_dir: Path
    measures_per_system: tuple[int, ...]
    output_name: str


REFERENCE_ROOT = PROJECT_ROOT / "tmp" / "reference-inputs" / "actual-reference-set"
PDF_ROOT = PROJECT_ROOT / "tmp" / "pdfs" / "actual-reference-set"
VECTOR_ROOT = PROJECT_ROOT / "tmp" / "pdfs" / "reference-vector"

SPECS = (
    ReferenceSpec(
        slug="hanezeve",
        title="Hanezeve Caradhina",
        midi=REFERENCE_ROOT / "Hanezeve_Caradhina_Animenz.mid",
        pdf=PDF_ROOT / "source-hanezeve.pdf",
        vector_dir=VECTOR_ROOT / "hanezeve",
        measures_per_system=(
            8,
            4,
            4,
            4,
            4,
            4,
            3,
            2,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            4,
            5,
            8,
        ),
        output_name="Actual-Reference-Hanezeve-A4",
    ),
    ReferenceSpec(
        slug="styx",
        title="STYX HELIX",
        midi=REFERENCE_ROOT / "STYX_HELIX_Animenz.mid",
        pdf=PDF_ROOT / "source-styx.pdf",
        vector_dir=VECTOR_ROOT / "styx",
        measures_per_system=(
            5,
            5,
            3,
            3,
            3,
            4,
            4,
            4,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            3,
            4,
            3,
            3,
            3,
            2,
            3,
            3,
            2,
            3,
            3,
            4,
            3,
            3,
            2,
            3,
            3,
            2,
            3,
            3,
            5,
        ),
        output_name="Actual-Reference-STYX-HELIX-A4",
    ),
    ReferenceSpec(
        slug="unravel",
        title="Unravel",
        midi=REFERENCE_ROOT / "Unravel_Animenz_Reference.mid",
        pdf=PDF_ROOT / "source-unravel.pdf",
        vector_dir=VECTOR_ROOT / "unravel",
        measures_per_system=(
            5,
            4,
            4,
            3,
            3,
            3,
            3,
            5,
            3,
            3,
            3,
            3,
            3,
            2,
            3,
            3,
            3,
            3,
            2,
            2,
            3,
            2,
            2,
            2,
            2,
            3,
            2,
            2,
            2,
            3,
            3,
            3,
            3,
            3,
            2,
            3,
            3,
            3,
            3,
            4,
            4,
            4,
            4,
            4,
        ),
        output_name="Actual-Reference-Unravel-A4",
    ),
)

NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?"
SEGMENT_PATTERN = re.compile(
    rf"M\s*({NUMBER})[ ,]+({NUMBER})\s*L\s*({NUMBER})[ ,]+({NUMBER})"
)
MATRIX_PATTERN = re.compile(
    rf"matrix\(\s*({NUMBER})[ ,]+({NUMBER})[ ,]+({NUMBER})[ ,]+"
    rf"({NUMBER})[ ,]+({NUMBER})[ ,]+({NUMBER})\s*\)"
)
SVG_NAMESPACE = "{http://www.w3.org/2000/svg}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare the three real reference scores measure by measure"
    )
    parser.add_argument(
        "--style",
        choices=("clean", "balanced", "faithful"),
        default="clean",
    )
    parser.add_argument(
        "--engraving-style",
        choices=("classic", "modern", "compact"),
        default="classic",
    )
    parser.add_argument(
        "--render-dir",
        type=Path,
        default=PROJECT_ROOT / "tmp" / "pdfs" / "actual-reference-comparison",
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=PROJECT_ROOT / "artifacts",
    )
    args = parser.parse_args()

    _validate_inputs()
    args.render_dir.mkdir(parents=True, exist_ok=True)
    args.artifacts_dir.mkdir(parents=True, exist_ok=True)

    pieces: list[dict[str, object]] = []
    for index, spec in enumerate(SPECS, start=1):
        print(f"[{index}/{len(SPECS)}] {spec.title}", flush=True)
        pieces.append(
            _compare_piece(
                spec,
                args.render_dir,
                args.style,
                args.engraving_style,
            )
        )

    report = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "methodology": {
            "reference_staff_evidence": (
                "The paired MIDI tracks exported with each reference score are used as "
                "the upper/lower staff baseline."
            ),
            "reference_layout_evidence": (
                "Vector staff lines determine systems per page; verified measure counts "
                "per system map all 354 measures."
            ),
            "output_semantics": (
                "The final ScoreModel and MusicXML provide hands, staves, voices, rests, "
                "clefs, key changes, ties, and ottava spans."
            ),
            "limitation": (
                "Dynamics, fingering, articulation, and phrase marks are not encoded in "
                "the supplied MIDI, so the report does not claim to reconstruct them."
            ),
        },
        "pieces": pieces,
    }
    json_path = args.artifacts_dir / "actual-reference-comparison.json"
    markdown_path = args.artifacts_dir / "actual-reference-comparison.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(_markdown_report(report), encoding="utf-8")
    print(f"JSON: {json_path}", flush=True)
    print(f"Markdown: {markdown_path}", flush=True)


def _validate_inputs() -> None:
    missing = [
        path
        for spec in SPECS
        for path in (spec.midi, spec.pdf, spec.vector_dir)
        if not path.exists()
    ]
    if missing:
        raise SystemExit("Missing reference inputs:\n" + "\n".join(map(str, missing)))


def _compare_piece(
    spec: ReferenceSpec,
    render_dir: Path,
    style: str,
    engraving_style: str,
) -> dict[str, object]:
    options = ConversionOptions(
        style=style,
        engraving_style=engraving_style,
        title=spec.title,
    )
    musicxml, analysis, warnings, score = convert_midi_with_score(
        spec.midi.read_bytes(),
        spec.midi.name,
        options,
    )
    engraving = render_a4_musicxml(musicxml, engraving_style)
    if engraving.pdf_bytes is None:
        raise RuntimeError(f"MuseScore failed for {spec.title}: {engraving.warnings}")

    xml_path = render_dir / f"{spec.output_name}.musicxml"
    pdf_path = render_dir / f"{spec.output_name}.pdf"
    xml_path.write_text(musicxml, encoding="utf-8")
    pdf_path.write_bytes(engraving.pdf_bytes)

    reference_layout = _reference_layout(spec)
    output_layout = _output_layout(engraving.analysis, score.measure_count)
    xml_metrics = _xml_measure_metrics(musicxml)
    ottava_spans = detect_ottava_spans(score.notes)
    track_staff_map = _track_staff_map(analysis)
    measures = [
        _measure_comparison(
            score,
            measure_index,
            track_staff_map,
            reference_layout["measure_map"],
            output_layout["measure_map"],
            xml_metrics,
            ottava_spans,
        )
        for measure_index in range(score.measure_count)
    ]

    staff_mismatches = sum(int(row["staff_reassigned_notes"]) for row in measures)
    hand_mismatches = sum(int(row["hand_reassigned_notes"]) for row in measures)
    note_count = len(score.notes)
    review_measures = [
        int(row["measure"])
        for row in measures
        if row["verdict"] == "复核"
    ]
    output_system_counts = list(engraving.analysis.get("measures_per_system", []))
    summary = {
        "measure_count": score.measure_count,
        "note_count": note_count,
        "reference_pdf_pages": len(PdfReader(str(spec.pdf)).pages),
        "reference_content_pages": int(reference_layout["content_pages"]),
        "reference_system_count": len(spec.measures_per_system),
        "reference_mean_measures_per_system": round(mean(spec.measures_per_system), 2),
        "output_pages": int(engraving.analysis.get("page_count", 0)),
        "output_system_count": len(output_system_counts),
        "output_mean_measures_per_system": round(mean(output_system_counts), 2),
        "output_singleton_systems": int(
            engraving.analysis.get("singleton_systems", 0)
        ),
        "staff_agreement_percent": round(
            (note_count - staff_mismatches) / max(1, note_count) * 100,
            2,
        ),
        "hand_agreement_percent": round(
            (note_count - hand_mismatches) / max(1, note_count) * 100,
            2,
        ),
        "visible_rests": sum(int(row["visible_rests"]) for row in measures),
        "visible_rests_per_100_notes": round(
            sum(int(row["visible_rests"]) for row in measures)
            / max(1, note_count)
            * 100,
            2,
        ),
        "ottava_spans": len(ottava_spans),
        "inferred_arpeggiated_chords": sum(
            int(row["hand_shape"]["arpeggiated_chords"]) for row in measures
        ),
        "measures_over_two_voices": sum(
            max(map(int, row["voices_by_staff"].values())) > 2 for row in measures
        ),
        "review_measure_count": len(review_measures),
        "review_measures": review_measures,
        "key_changes": analysis["key_signatures"],
        "clefs": analysis["staves"]["clefs"],
        "layout_passes": engraving.analysis.get("layout_passes"),
        "a4": engraving.analysis.get("a4"),
    }
    return {
        "slug": spec.slug,
        "title": spec.title,
        "source_midi": str(spec.midi),
        "reference_pdf": str(spec.pdf),
        "output_musicxml": str(xml_path),
        "output_pdf": str(pdf_path),
        "conversion_warnings": warnings,
        "engraving_warnings": engraving.warnings,
        "summary": summary,
        "reference_systems": reference_layout["systems"],
        "output_systems": engraving.analysis.get("systems", []),
        "measures": measures,
    }


def _reference_layout(spec: ReferenceSpec) -> dict[str, object]:
    page_system_counts = [
        _svg_system_count(path) for path in sorted(spec.vector_dir.glob("*.svg"))
    ]
    content_page_counts = [count for count in page_system_counts if count]
    if sum(content_page_counts) != len(spec.measures_per_system):
        raise ValueError(
            f"{spec.title}: vector pages expose {sum(content_page_counts)} systems, "
            f"but {len(spec.measures_per_system)} verified systems were supplied"
        )

    systems: list[dict[str, object]] = []
    measure_map: dict[int, dict[str, int]] = {}
    measure = 1
    system_index = 0
    for page, system_count in enumerate(page_system_counts, start=1):
        for system_on_page in range(1, system_count + 1):
            count = spec.measures_per_system[system_index]
            measure_numbers = list(range(measure, measure + count))
            system = {
                "page": page,
                "system": system_index + 1,
                "system_on_page": system_on_page,
                "measure_count": count,
                "measures": measure_numbers,
            }
            systems.append(system)
            for position, number in enumerate(measure_numbers, start=1):
                measure_map[number] = {
                    "page": page,
                    "system": system_index + 1,
                    "system_on_page": system_on_page,
                    "position": position,
                    "system_measure_count": count,
                }
            measure += count
            system_index += 1

    if measure - 1 != sum(spec.measures_per_system):
        raise ValueError(f"{spec.title}: reference measure map is incomplete")
    return {
        "content_pages": sum(bool(count) for count in page_system_counts),
        "page_system_counts": page_system_counts,
        "systems": systems,
        "measure_map": measure_map,
    }


def _svg_system_count(path: Path) -> int:
    root = ET.parse(path).getroot()
    width = float(root.get("width", "0"))
    maximum_staff_lines = 0
    for element in root.findall(f".//{SVG_NAMESPACE}path"):
        transform = _matrix(element.get("transform", ""))
        long_horizontal_lines = 0
        for match in SEGMENT_PATTERN.finditer(element.get("d", "")):
            x1, y1, x2, y2 = map(float, match.groups())
            transformed = _transform_segment(x1, y1, x2, y2, transform)
            tx1, ty1, tx2, ty2 = transformed
            if abs(ty1 - ty2) < 0.2 and abs(tx2 - tx1) > width * 0.65:
                long_horizontal_lines += 1
        maximum_staff_lines = max(maximum_staff_lines, long_horizontal_lines)
    return maximum_staff_lines // 10


def _matrix(value: str) -> tuple[float, float, float, float, float, float]:
    match = MATRIX_PATTERN.search(value)
    if match is None:
        return 1.0, 0.0, 0.0, 1.0, 0.0, 0.0
    return tuple(map(float, match.groups()))  # type: ignore[return-value]


def _transform_segment(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    matrix: tuple[float, float, float, float, float, float],
) -> tuple[float, float, float, float]:
    a, b, c, d, e, f = matrix
    return (
        a * x1 + c * y1 + e,
        b * x1 + d * y1 + f,
        a * x2 + c * y2 + e,
        b * x2 + d * y2 + f,
    )


def _output_layout(
    engraving: dict[str, object],
    measure_count: int,
) -> dict[str, object]:
    systems = list(engraving.get("systems", []))
    measure_map: dict[int, dict[str, int]] = {}
    page_positions: Counter[int] = Counter()
    for system_index, system in enumerate(systems, start=1):
        page = int(system["page"])
        page_positions[page] += 1
        measures = [int(number) for number in system.get("measures", [])]
        for position, number in enumerate(measures, start=1):
            measure_map[number] = {
                "page": page,
                "system": system_index,
                "system_on_page": page_positions[page],
                "position": position,
                "system_measure_count": len(measures),
            }
    missing = sorted(set(range(1, measure_count + 1)) - set(measure_map))
    if missing:
        raise ValueError(f"MuseScore layout is missing measures: {missing[:12]}")
    return {"systems": systems, "measure_map": measure_map}


def _xml_measure_metrics(musicxml: str) -> dict[int, dict[str, object]]:
    root = ET.fromstring(musicxml)
    result: dict[int, dict[str, object]] = {}
    for measure in root.findall("./part/measure"):
        number = int(measure.get("number", "0"))
        visible_by_staff: Counter[int] = Counter()
        hidden_by_staff: Counter[int] = Counter()
        measure_rests = 0
        tie_starts = 0
        arpeggiated_noteheads = 0
        for note in measure.findall("note"):
            staff = int(note.findtext("staff", "1"))
            rest = note.find("rest")
            if rest is not None:
                if note.get("print-object") == "no":
                    hidden_by_staff[staff] += 1
                else:
                    visible_by_staff[staff] += 1
                measure_rests += rest.get("measure") == "yes"
            tie_starts += any(tie.get("type") == "start" for tie in note.findall("tie"))
            arpeggiated_noteheads += len(note.findall("notations/arpeggiate"))
        octave_starts = [
            shift.get("type", "")
            for shift in measure.findall("direction/direction-type/octave-shift")
            if shift.get("type") in {"up", "down"}
        ]
        result[number] = {
            "visible_by_staff": dict(visible_by_staff),
            "hidden_by_staff": dict(hidden_by_staff),
            "measure_rests": measure_rests,
            "tie_starts": tie_starts,
            "arpeggiated_noteheads": arpeggiated_noteheads,
            "octave_starts": octave_starts,
        }
    return result


def _track_staff_map(analysis: dict[str, object]) -> dict[int, Staff]:
    hands = dict(analysis["hands"])
    if hands.get("method") != "tracks":
        return {}
    return {
        int(track): Staff.LEFT if label == "left" else Staff.RIGHT
        for track, label in dict(hands.get("track_map", {})).items()
    }


def _measure_comparison(
    score: ScoreModel,
    measure_index: int,
    track_staff_map: dict[int, Staff],
    reference_map: dict[int, dict[str, int]],
    output_map: dict[int, dict[str, int]],
    xml_metrics: dict[int, dict[str, object]],
    ottava_spans: list[OttavaSpan],
) -> dict[str, object]:
    number = measure_index + 1
    measure = score.measures[measure_index]
    notes = [
        note
        for note in score.notes
        if measure_index_at(score.measures, note.onset) == measure_index
    ]
    expected_upper = sum(track_staff_map.get(note.track) == Staff.RIGHT for note in notes)
    expected_lower = sum(track_staff_map.get(note.track) == Staff.LEFT for note in notes)
    output_upper = sum(note.staff == Staff.RIGHT for note in notes)
    output_lower = sum(note.staff == Staff.LEFT for note in notes)
    staff_reassigned = sum(
        note.staff is not None
        and track_staff_map.get(note.track) is not None
        and note.staff != track_staff_map[note.track]
        for note in notes
    )
    hand_reassigned = sum(
        note.hand is not None
        and track_staff_map.get(note.track) is not None
        and note.hand
        != (Hand.RIGHT if track_staff_map[note.track] == Staff.RIGHT else Hand.LEFT)
        for note in notes
    )
    voices_by_staff = {
        "upper": max(
            1,
            len({note.voice for note in notes if note.staff == Staff.RIGHT}),
        ),
        "lower": max(
            1,
            len({note.voice for note in notes if note.staff == Staff.LEFT}),
        ),
    }
    hand_shape = _measure_hand_shape(notes)
    metric = xml_metrics[number]
    visible_by_staff = {
        "upper": int(dict(metric["visible_by_staff"]).get(1, 0)),
        "lower": int(dict(metric["visible_by_staff"]).get(2, 0)),
    }
    visible_rests = sum(visible_by_staff.values())
    overlapping_ottavas = [
        span
        for span in ottava_spans
        if span.start < measure.end and span.end > measure.start
    ]
    starting_ottavas = [
        span for span in ottava_spans if measure.start <= span.start < measure.end
    ]
    key = _key_at(score, measure_index)
    clefs = {
        "upper": clef_kind_at(score.clef_changes, Staff.RIGHT, measure_index),
        "lower": clef_kind_at(score.clef_changes, Staff.LEFT, measure_index),
    }
    extreme_clef_notes = sum(
        (
            clef_kind_at(score.clef_changes, note.staff or Staff.RIGHT, measure_index)
            == "treble"
            and note.pitch < 50
        )
        or (
            clef_kind_at(score.clef_changes, note.staff or Staff.RIGHT, measure_index)
            == "bass"
            and note.pitch > 74
        )
        for note in notes
    )
    reference_position = reference_map[number]
    output_position = output_map[number]

    issues: list[str] = []
    serious = False
    if hand_reassigned > max(2, round(len(notes) * 0.12)):
        issues.append(f"换手{hand_reassigned}")
    if staff_reassigned > max(2, round(len(notes) * 0.16)):
        issues.append(f"跨谱表{staff_reassigned}")
    if max(voices_by_staff.values()) > 2:
        issues.append("超过2声部")
    if visible_rests > max(5, round(len(notes) * 0.30)):
        issues.append(f"休止符{visible_rests}")
    if len(starting_ottavas) > 1:
        issues.append("八度线碎片")
    if hand_shape["maximum_unrolled_keys"] > 5:
        issues.append(f"单手{hand_shape['maximum_unrolled_keys']}键")
        serious = True
    if hand_shape["maximum_unrolled_span_semitones"] > 16:
        issues.append(f"单手跨度{hand_shape['maximum_unrolled_span_semitones']}")
        serious = True
    if extreme_clef_notes >= 3:
        issues.append(f"加线压力{extreme_clef_notes}")
    if (
        output_position["system_measure_count"] + 2
        < reference_position["system_measure_count"]
    ):
        issues.append("断行偏疏")
    if max(voices_by_staff.values()) >= 4 or len(starting_ottavas) >= 3:
        serious = True
    verdict = "复核" if serious else "可接受" if issues else "清晰"
    return {
        "measure": number,
        "meter": (
            f"{measure.meter.numerator}/{measure.meter.denominator}"
        ),
        "key_fifths": key.fifths,
        "clefs": clefs,
        "reference_layout": reference_position,
        "output_layout": output_position,
        "source_track_notes": {
            "upper": expected_upper,
            "lower": expected_lower,
        },
        "output_staff_notes": {
            "upper": output_upper,
            "lower": output_lower,
        },
        "staff_reassigned_notes": staff_reassigned,
        "hand_reassigned_notes": hand_reassigned,
        "voices_by_staff": voices_by_staff,
        "visible_rests_by_staff": visible_by_staff,
        "visible_rests": visible_rests,
        "hidden_rests": sum(map(int, dict(metric["hidden_by_staff"]).values())),
        "measure_rests": int(metric["measure_rests"]),
        "tie_starts": int(metric["tie_starts"]),
        "arpeggiated_noteheads": int(metric["arpeggiated_noteheads"]),
        "ottava_spans": [
            {
                "staff": "upper" if span.staff == Staff.RIGHT else "lower",
                "direction": span.direction,
                "size": span.size,
                "starts_here": span in starting_ottavas,
            }
            for span in overlapping_ottavas
        ],
        "hand_shape": hand_shape,
        "extreme_clef_notes": extreme_clef_notes,
        "issues": issues,
        "verdict": verdict,
    }


def _measure_hand_shape(notes) -> dict[str, int]:
    maximum_keys = 0
    maximum_span = 0
    maximum_unrolled_keys = 0
    maximum_unrolled_span = 0
    arpeggiated_chords = 0
    grouped: dict[tuple[int, Hand], list] = defaultdict(list)
    for note in notes:
        if note.hand is not None:
            grouped[(note.onset, note.hand)].append(note)
    for grouped_notes in grouped.values():
        unique = sorted({note.pitch for note in grouped_notes})
        maximum_keys = max(maximum_keys, len(unique))
        if unique:
            maximum_span = max(maximum_span, unique[-1] - unique[0])
        if grouped_notes and all(note.arpeggiated for note in grouped_notes):
            arpeggiated_chords += 1
            continue
        maximum_unrolled_keys = max(maximum_unrolled_keys, len(unique))
        if unique:
            maximum_unrolled_span = max(
                maximum_unrolled_span,
                unique[-1] - unique[0],
            )
    return {
        "maximum_keys": maximum_keys,
        "maximum_span_semitones": maximum_span,
        "maximum_unrolled_keys": maximum_unrolled_keys,
        "maximum_unrolled_span_semitones": maximum_unrolled_span,
        "arpeggiated_chords": arpeggiated_chords,
    }


def _key_at(score: ScoreModel, measure_index: int) -> KeyEstimate:
    key = score.key
    for change in score.key_changes:
        if change.measure_index > measure_index:
            break
        key = change.key
    return key


def _markdown_report(report: dict[str, object]) -> str:
    pieces = list(report["pieces"])
    lines = [
        "# 三份真实参考谱逐小节对比与算法经验",
        "",
        "本报告把参考 PDF 的页面/系统布局、同套参考 MIDI 的上下谱表轨道，以及程序最终 "
        "ScoreModel/MusicXML 逐小节对齐。三首曲目共 354 小节，均进入下方明细。",
        "",
        "## 对比边界",
        "",
        "- MIDI 可以可靠提供音高、时值、轨道、调号、拍号和踏板，但不包含参考 PDF 中的力度、"
        "指法、奏法、连线与人工乐句意图；这些内容不能假装被完整还原。",
        "- 上下轨道是原谱谱表的强证据；少量跨谱表移动只在动态谱号仍会留下极端加线时进行。",
        "- 参考 PDF 的 STYX HELIX 第 9、10 页为空白，因此报告同时列出 PDF 总页数和有效内容页数。",
        "",
        "## 总览",
        "",
        "| 曲目 | 参考有效页/系统 | 输出页/系统 | 平均小节/系统 参考→输出 | 谱表一致率 | 手一致率 | 可见休止/100音 | 8度线 | 推断滚奏 | 需复核小节 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for piece in pieces:
        summary = piece["summary"]
        lines.append(
            "| {title} | {rp}/{rs} | {op}/{os} | {rm:.2f}→{om:.2f} | "
            "{sa:.2f}% | {ha:.2f}% | {rests:.2f} | {ottava} | {rolled} | {review} |".format(
                title=piece["title"],
                rp=summary["reference_content_pages"],
                rs=summary["reference_system_count"],
                op=summary["output_pages"],
                os=summary["output_system_count"],
                rm=summary["reference_mean_measures_per_system"],
                om=summary["output_mean_measures_per_system"],
                sa=summary["staff_agreement_percent"],
                ha=summary["hand_agreement_percent"],
                rests=summary["visible_rests_per_100_notes"],
                ottava=summary["ottava_spans"],
                rolled=summary["inferred_arpeggiated_chords"],
                review=summary["review_measure_count"],
            )
        )

    lines.extend(
        [
            "",
            "## 从参考谱吸收的规则",
            "",
            "1. Hanezeve Caradhina 证明调号必须是一条时间线，而不是全曲单一估计：第 33、34、42、43 "
            "小节附近的 G→C→A♭→C→G 必须写成真实转调。开头下谱表使用高音谱号也说明谱表和谱号不能绑定。",
            "2. STYX HELIX 的主体通常每系统 3 小节，密集段落允许 2 小节，稀疏段落可到 4–5 小节；"
            "应让 MuseScore 根据最终符号碰撞自动换行，而不是用 MIDI 音符数硬编码断行。",
            "3. Unravel 开头将高旋律放在连续 8va 下，同时让伴奏保留在下方高音谱表。八度线应允许同一高音带内的"
            "过渡音连接成乐句，不能每遇到一个非峰值音就重新起线。",
            "4. 同起点、同时值、同一只手的音首先是一个和弦；旋律声部算法只能提供连续性提示，不能把一个和弦拆成"
            "多个声部并制造成排休止符。",
            "5. 可信双轨在中央 C 附近允许约 6 个半音的重叠，只要两轨中位音高仍相隔至少十度；否则 Unravel 会被"
            "错误降级为纯音高分手。",
            "",
        ]
    )

    for piece in pieces:
        summary = piece["summary"]
        lines.extend(
            [
                f"## {piece['title']}",
                "",
                (
                    f"参考 {summary['reference_content_pages']} 个有效页面、"
                    f"{summary['reference_system_count']} 个系统；输出 "
                    f"{summary['output_pages']} 页、{summary['output_system_count']} 个系统。"
                    f"谱表一致率 {summary['staff_agreement_percent']:.2f}%，"
                    f"手一致率 {summary['hand_agreement_percent']:.2f}%，"
                    f"共 {summary['visible_rests']} 个可见休止符、"
                    f"{summary['ottava_spans']} 条八度线、"
                    f"{summary['inferred_arpeggiated_chords']} 个推断滚奏和弦。"
                ),
                "",
                "| 小节 | 参考位置(容量) | 输出位置(容量) | 原轨U/L→输出U/L | 换手/跨谱表 | 声部U/L | 休止U/L | 调号·谱号 | 8度线 | 手型 键/半音/滚奏 | 结论 |",
                "|---:|---|---|---|---:|---|---|---|---|---|---|",
            ]
        )
        for row in piece["measures"]:
            reference = row["reference_layout"]
            output = row["output_layout"]
            source = row["source_track_notes"]
            staff = row["output_staff_notes"]
            voices = row["voices_by_staff"]
            rests = row["visible_rests_by_staff"]
            shape = row["hand_shape"]
            clefs = row["clefs"]
            ottava = ",".join(
                f"{span['size']}{'↑' if span['direction'] == 'down' else '↓'}"
                for span in row["ottava_spans"]
            ) or "-"
            issues = "、".join(row["issues"])
            conclusion = row["verdict"] + (f"：{issues}" if issues else "")
            lines.append(
                "| {m} | P{rp}-S{rs}({rc}) | P{op}-S{os}({oc}) | "
                "{su}/{sl}→{ou}/{ol} | {hand}/{staff_move} | {vu}/{vl} | "
                "{ru}/{rl} | {key}·{cu}/{cl} | {ottava} | "
                "{keys}/{span}/r{rolled} | {conclusion} |".format(
                    m=row["measure"],
                    rp=reference["page"],
                    rs=reference["system_on_page"],
                    rc=reference["system_measure_count"],
                    op=output["page"],
                    os=output["system_on_page"],
                    oc=output["system_measure_count"],
                    su=source["upper"],
                    sl=source["lower"],
                    ou=staff["upper"],
                    ol=staff["lower"],
                    hand=row["hand_reassigned_notes"],
                    staff_move=row["staff_reassigned_notes"],
                    vu=voices["upper"],
                    vl=voices["lower"],
                    ru=rests["upper"],
                    rl=rests["lower"],
                    key=row["key_fifths"],
                    cu=clefs["upper"][0].upper(),
                    cl=clefs["lower"][0].upper(),
                    ottava=ottava,
                    keys=shape["maximum_keys"],
                    span=shape["maximum_span_semitones"],
                    rolled=shape["arpeggiated_chords"],
                    conclusion=conclusion,
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    main()
