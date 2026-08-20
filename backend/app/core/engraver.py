from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from pypdf import PdfReader

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RESOURCE_ROOT = Path(__file__).resolve().parents[1] / "resources"
STYLE_PATHS = {
    "classic": RESOURCE_ROOT / "piano_a4.mss",
    "modern": RESOURCE_ROOT / "piano_a4_modern.mss",
    "compact": RESOURCE_ROOT / "piano_a4_compact.mss",
}
MUSESCORE_ENV = "PIANO_MIDI_SCORE_MUSESCORE"
RENDER_TIMEOUT_SECONDS = 90
LOW_DENSITY_SINGLETON_MAX_PITCHED_NOTES = 20


@dataclass(frozen=True, slots=True)
class EngravingResult:
    pdf_bytes: bytes | None
    preview_png: bytes | None
    preview_pngs: tuple[bytes, ...]
    analysis: dict[str, object]
    warnings: list[str]


def render_a4_musicxml(
    musicxml: str,
    engraving_style: str = "classic",
) -> EngravingResult:
    style_name = engraving_style if engraving_style in STYLE_PATHS else "classic"
    style_path = STYLE_PATHS[style_name]
    executable = find_musescore()
    if executable is None:
        return EngravingResult(
            pdf_bytes=None,
            preview_png=None,
            preview_pngs=(),
            analysis={
                "available": False,
                "engine": "MusicXML only",
                "style": style_name,
            },
            warnings=["未找到 MuseScore Studio 4，已保留 MusicXML，暂不生成出版级 PDF"],
        )

    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="piano-midi-score-") as temporary:
            temp_dir = Path(temporary)
            input_path = temp_dir / "score.musicxml"
            pdf_path = temp_dir / "score.pdf"
            mpos_path = temp_dir / "score.mpos"
            png_path = temp_dir / "preview.png"
            job_path = temp_dir / "render-job.json"
            input_path.write_text(musicxml, encoding="utf-8")
            job_path.write_text(
                json.dumps(
                    [
                        {
                            "in": str(input_path),
                            "out": [str(pdf_path), str(mpos_path), str(png_path)],
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            command = [str(executable), "-r", "140"]
            if style_path.is_file():
                command.extend(["-S", str(style_path)])
            command.extend(["-j", str(job_path)])
            _run_musescore(command)
            (
                pdf_bytes,
                preview_png,
                preview_pngs,
                preview_count,
                layout,
            ) = _read_render_outputs(
                temp_dir,
                pdf_path,
                mpos_path,
                png_path,
            )
            layout_passes = 1
            balanced_page_breaks = False
            singleton_rebalanced = False
            accepted_xml = musicxml

            if _needs_singleton_rebalance(layout, accepted_xml):
                singleton_xml = _with_rebalanced_singletons(accepted_xml, layout)
                if singleton_xml != accepted_xml:
                    try:
                        _clear_render_outputs(temp_dir, pdf_path, mpos_path, png_path)
                        input_path.write_text(singleton_xml, encoding="utf-8")
                        _run_musescore(command)
                        candidate = _read_render_outputs(
                            temp_dir,
                            pdf_path,
                            mpos_path,
                            png_path,
                        )
                        (
                            candidate_pdf,
                            candidate_preview,
                            candidate_previews,
                            candidate_count,
                            candidate_layout,
                        ) = candidate
                        layout_passes += 1
                        if (
                            int(candidate_layout.get("page_count", 0))
                            <= int(layout.get("page_count", 0))
                            and len(
                                _low_density_singleton_measures(
                                    candidate_layout, singleton_xml
                                )
                            )
                            < len(_low_density_singleton_measures(layout, accepted_xml))
                        ):
                            pdf_bytes = candidate_pdf
                            preview_png = candidate_preview
                            preview_pngs = candidate_previews
                            preview_count = candidate_count
                            layout = candidate_layout
                            accepted_xml = singleton_xml
                            singleton_rebalanced = True
                    except (
                        OSError,
                        RuntimeError,
                        ET.ParseError,
                        ValueError,
                        subprocess.TimeoutExpired,
                    ):
                        # The initial render remains the safe fallback.
                        pass

            if _needs_page_rebalance(layout):
                balanced_xml = _with_balanced_page_breaks(accepted_xml, layout)
                if balanced_xml != accepted_xml:
                    try:
                        _clear_render_outputs(temp_dir, pdf_path, mpos_path, png_path)
                        input_path.write_text(balanced_xml, encoding="utf-8")
                        _run_musescore(command)
                        candidate = _read_render_outputs(
                            temp_dir,
                            pdf_path,
                            mpos_path,
                            png_path,
                        )
                        (
                            candidate_pdf,
                            candidate_preview,
                            candidate_previews,
                            candidate_count,
                            candidate_layout,
                        ) = candidate
                        layout_passes += 1
                        if int(candidate_layout.get("page_count", 0)) <= int(
                            layout.get("page_count", 0)
                        ) and _layout_balance_score(candidate_layout) < _layout_balance_score(layout):
                            pdf_bytes = candidate_pdf
                            preview_png = candidate_preview
                            preview_pngs = candidate_previews
                            preview_count = candidate_count
                            layout = candidate_layout
                            accepted_xml = balanced_xml
                            balanced_page_breaks = True
                    except (OSError, RuntimeError, ET.ParseError, ValueError, subprocess.TimeoutExpired):
                        # The first render is already valid; a failed optional balance
                        # pass must never discard it.
                        pass

            layout.update(
                {
                    "available": True,
                    "engine": "MuseScore Studio 4",
                    "style": style_name,
                    "preview_page_count": preview_count,
                    "layout_passes": layout_passes,
                    "balanced_page_breaks": balanced_page_breaks,
                    "singleton_rebalanced": singleton_rebalanced,
                    "processing_ms": round((time.perf_counter() - started) * 1000, 2),
                }
            )
            warnings = _layout_warnings(layout)
            return EngravingResult(
                pdf_bytes,
                preview_png,
                preview_pngs,
                layout,
                warnings,
            )
    except subprocess.TimeoutExpired:
        return EngravingResult(
            pdf_bytes=None,
            preview_png=None,
            preview_pngs=(),
            analysis={
                "available": False,
                "engine": "MuseScore Studio 4",
                "style": style_name,
                "timeout": True,
            },
            warnings=[f"MuseScore 雕版超过 {RENDER_TIMEOUT_SECONDS} 秒，已返回 MusicXML 以避免请求挂起"],
        )
    except (OSError, RuntimeError, ET.ParseError, ValueError) as exc:
        return EngravingResult(
            pdf_bytes=None,
            preview_png=None,
            preview_pngs=(),
            analysis={
                "available": False,
                "engine": "MuseScore Studio 4",
                "style": style_name,
            },
            warnings=[f"A4 PDF 雕版失败，已保留 MusicXML：{exc}"],
        )


@lru_cache(maxsize=1)
def find_musescore() -> Path | None:
    configured = os.environ.get(MUSESCORE_ENV)
    candidates = [
        configured,
        shutil.which("MuseScore4"),
        shutil.which("musescore4"),
        shutil.which("mscore"),
        r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
        "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
        "/usr/bin/mscore4",
        "/usr/bin/musescore4",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    return None


def engraver_status() -> dict[str, object]:
    executable = find_musescore()
    return {
        "available": executable is not None,
        "engine": "MuseScore Studio 4" if executable else "MusicXML only",
        "styles": sorted(STYLE_PATHS),
    }


def _run_musescore(command: list[str]) -> None:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RENDER_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()
        raise RuntimeError(f"MuseScore 返回 {completed.returncode}：{detail[:500]}")


def _read_render_outputs(
    temp_dir: Path,
    pdf_path: Path,
    mpos_path: Path,
    png_path: Path,
) -> tuple[bytes, bytes | None, tuple[bytes, ...], int, dict[str, object]]:
    if not pdf_path.is_file():
        raise RuntimeError("MuseScore 未生成 PDF 文件")
    pdf_bytes = pdf_path.read_bytes()
    if not pdf_bytes.startswith(b"%PDF"):
        raise RuntimeError("MuseScore 输出不是有效 PDF")
    preview_paths = sorted(
        temp_dir.glob("preview-*.png"),
        key=_preview_page_sort_key,
    )
    if not preview_paths and png_path.is_file():
        preview_paths = [png_path]
    preview_pngs = tuple(path.read_bytes() for path in preview_paths)
    preview_png = preview_pngs[0] if preview_pngs else None
    layout = _pdf_layout(pdf_bytes)
    layout.update(_measure_layout(mpos_path))
    return pdf_bytes, preview_png, preview_pngs, len(preview_pngs), layout


def _preview_page_sort_key(path: Path) -> tuple[int, str]:
    suffix = path.stem.rsplit("-", 1)[-1]
    return (int(suffix), path.name) if suffix.isdigit() else (0, path.name)


def _clear_render_outputs(
    temp_dir: Path,
    pdf_path: Path,
    mpos_path: Path,
    png_path: Path,
) -> None:
    for path in [pdf_path, mpos_path, png_path, *temp_dir.glob("preview-*.png")]:
        if path.is_file():
            path.unlink()


def _pdf_layout(pdf_bytes: bytes) -> dict[str, object]:
    reader = PdfReader(BytesIO(pdf_bytes))
    if not reader.pages:
        raise ValueError("PDF 没有页面")
    first_page = reader.pages[0]
    width = float(first_page.mediabox.width)
    height = float(first_page.mediabox.height)
    a4 = abs(width - 595.28) <= 2.0 and abs(height - 841.89) <= 2.0
    return {
        "page_count": len(reader.pages),
        "page_size": "A4" if a4 else f"{round(width, 1)}×{round(height, 1)} pt",
        "page_width_pt": round(width, 2),
        "page_height_pt": round(height, 2),
        "a4": a4,
    }


def _measure_layout(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"measures_per_system": [], "singleton_systems": 0, "systems": []}
    root = ET.parse(path).getroot()
    systems: dict[tuple[int, float], list[int]] = {}
    for element in root.findall("./elements/element"):
        page = int(element.get("page", "0"))
        y = round(float(element.get("y", "0")), 3)
        measure_id = int(element.get("id", "0"))
        systems.setdefault((page, y), []).append(measure_id)
    ordered_systems = sorted(systems.items(), key=lambda item: (item[0][0], item[0][1]))
    ordered = [len(measures) for _, measures in ordered_systems]
    return {
        "measures_per_system": ordered,
        "system_count": len(ordered),
        "singleton_systems": sum(count == 1 for count in ordered),
        "systems": [
            {
                "page": page + 1,
                "y": y,
                "measures": sorted(measure + 1 for measure in measure_ids),
            }
            for (page, y), measure_ids in ordered_systems
        ],
    }


def _needs_page_rebalance(layout: dict[str, object]) -> bool:
    systems = list(layout.get("systems", []))
    if not systems:
        return False
    page_counts = Counter(int(system["page"]) for system in systems)
    page_count = max(page_counts)
    if page_count <= 1:
        return False
    average = len(systems) / page_count
    last_page = page_counts[page_count]
    previous_page = page_counts[page_count - 1]
    return (
        last_page > previous_page
        or last_page <= max(2, int(average) - 2)
    )


def _needs_singleton_rebalance(layout: dict[str, object], musicxml: str) -> bool:
    return bool(_low_density_singleton_measures(layout, musicxml))


def _low_density_singleton_measures(
    layout: dict[str, object],
    musicxml: str,
) -> list[int]:
    systems = list(layout.get("systems", []))
    if not systems:
        return []
    root = ET.fromstring(musicxml)
    counts = _pitched_note_counts(root)
    return [
        int(measures[0])
        for system in systems
        if len(measures := list(system.get("measures", []))) == 1
        and counts.get(int(measures[0]), 0)
        <= LOW_DENSITY_SINGLETON_MAX_PITCHED_NOTES
    ]


def _with_rebalanced_singletons(
    musicxml: str,
    layout: dict[str, object],
) -> str:
    systems = list(layout.get("systems", []))
    if len(systems) < 3:
        return musicxml

    root = ET.fromstring(musicxml)
    counts = _pitched_note_counts(root)
    changed = False
    for index, system in enumerate(systems):
        measures = [int(number) for number in system.get("measures", [])]
        if len(measures) != 1:
            continue
        singleton = measures[0]
        if counts.get(singleton, 0) > LOW_DENSITY_SINGLETON_MAX_PITCHED_NOTES:
            continue

        page = int(system.get("page", 0))
        candidates: list[tuple[int, str, int, int]] = []
        if index > 0:
            previous = systems[index - 1]
            previous_measures = [
                int(number) for number in previous.get("measures", [])
            ]
            if int(previous.get("page", 0)) == page and len(previous_measures) >= 3:
                moved = previous_measures[-1]
                candidates.append(
                    (counts.get(moved, 0) + counts.get(singleton, 0), "previous", moved, singleton)
                )
        if index + 1 < len(systems):
            following = systems[index + 1]
            following_measures = [
                int(number) for number in following.get("measures", [])
            ]
            if int(following.get("page", 0)) == page and len(following_measures) >= 3:
                candidates.append(
                    (
                        counts.get(singleton, 0)
                        + counts.get(following_measures[0], 0),
                        "following",
                        following_measures[0],
                        following_measures[1],
                    )
                )
        if not candidates:
            continue

        _, direction, first, second = min(candidates)
        if direction == "previous":
            _set_system_break(root, second, enabled=False)
            _set_system_break(root, first, enabled=True)
        else:
            _set_system_break(root, first, enabled=False)
            _set_system_break(root, second, enabled=True)
        changed = True

    if not changed:
        return musicxml
    return _serialize_musicxml(root)


def _pitched_note_counts(root: ET.Element) -> dict[int, int]:
    return {
        int(measure.get("number", "0")): sum(
            note.find("pitch") is not None for note in measure.findall("note")
        )
        for measure in root.findall("./part/measure")
    }


def _set_system_break(root: ET.Element, measure_number: int, *, enabled: bool) -> None:
    for measure in root.findall("./part/measure"):
        if int(measure.get("number", "0")) != measure_number:
            continue
        print_element = measure.find("print")
        if enabled:
            if print_element is None:
                print_element = ET.Element("print")
                measure.insert(0, print_element)
            print_element.set("new-system", "yes")
        elif print_element is not None:
            print_element.attrib.pop("new-system", None)
        return


def _with_balanced_page_breaks(
    musicxml: str,
    layout: dict[str, object],
) -> str:
    systems = list(layout.get("systems", []))
    if not systems:
        return musicxml
    page_count = max(int(system["page"]) for system in systems)
    if page_count <= 1:
        return musicxml

    tail_systems = [
        system for system in systems if int(system["page"]) >= page_count - 1
    ]
    if len(tail_systems) < 4:
        return musicxml
    first_tail_page_size = (len(tail_systems) + 1) // 2
    measures = list(tail_systems[first_tail_page_size]["measures"])
    if not measures:
        return musicxml
    break_measures = {int(measures[0])}

    root = ET.fromstring(musicxml)
    for measure in root.findall("./part/measure"):
        number = int(measure.get("number", "0"))
        print_element = measure.find("print")
        if print_element is not None:
            print_element.attrib.pop("new-page", None)
        if number not in break_measures:
            continue
        if print_element is None:
            print_element = ET.Element("print")
            measure.insert(0, print_element)
        print_element.attrib.pop("new-system", None)
        print_element.set("new-page", "yes")

    return _serialize_musicxml(root)


def _serialize_musicxml(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
        '"http://www.musicxml.org/dtds/partwise.dtd">\n'
        + body
    )


def _layout_balance_score(layout: dict[str, object]) -> float:
    systems = list(layout.get("systems", []))
    if not systems:
        return float("inf")
    page_counts = Counter(int(system["page"]) for system in systems)
    page_count = max(page_counts)
    counts = [page_counts[page] for page in range(1, page_count + 1)]
    average = sum(counts) / len(counts)
    imbalance = sum(abs(count - average) for count in counts)
    # A final page may naturally contain one fewer system. Penalize only a
    # conspicuously sparse ending, while also discouraging the last page from
    # being denser than the penultimate page.
    sparse_last_page = max(0.0, average - counts[-1] - 1.0) * 3.0
    dense_last_page = max(0, counts[-1] - counts[-2])
    return page_count * 0.05 + imbalance + sparse_last_page + dense_last_page


def _layout_warnings(layout: dict[str, object]) -> list[str]:
    warnings: list[str] = []
    if not layout.get("a4"):
        warnings.append(f"PDF 页面不是 A4：{layout.get('page_size')}")
    measures = list(layout.get("measures_per_system", []))
    singleton_count = int(layout.get("singleton_systems", 0))
    if singleton_count and sum(measures) > 1:
        warnings.append(f"版面仍有 {singleton_count} 行仅含一个小节，建议检查该段音符密度")
    return warnings
