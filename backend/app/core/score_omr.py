"""Optical music recognition for uploaded PDF scores via Audiveris.

Audiveris runs as an external Java application; this module only locates the
executable, feeds it a PDF in batch mode, and collects the exported MusicXML.
The OMR output is already engraved notation, so it re-enters the web pipeline
at the same point as an uploaded .musicxml file (direct engraving + MIDI
export), not at the semantic MIDI pipeline.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from pypdf import PdfReader, PdfWriter

AUDIVERIS_ENV = "PIANO_MIDI_SCORE_AUDIVERIS"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
OMR_TIMEOUT_SECONDS = 30 * 60  # dense scores take minutes per page
_LOG_TAIL = 1200
# Audiveris rasterizes PDF pages at a fixed 300 DPI and rejects sheets whose
# staff interline measures only a few pixels.  Small-page PDFs (partiture
# booklets are often near A5) therefore need an explicit upscale first.
_AUDIVERIS_RASTER_DPI = 300
_AUDIVERIS_MAX_SHEET_PIXELS = 19_000_000
_TARGET_MIN_WIDTH_PIXELS = 3000


class ScoreOmrError(RuntimeError):
    """Raised when a PDF score cannot be recognized."""


class ScoreOmrUnavailableError(ScoreOmrError):
    """Raised when no OMR engine is installed."""


@dataclass(frozen=True, slots=True)
class ScoreOmrResult:
    musicxml: str
    analysis: dict[str, object]
    warnings: list[str] = field(default_factory=list)


def find_audiveris() -> Path | None:
    configured = os.environ.get(AUDIVERIS_ENV)
    candidates = [
        configured,
        shutil.which("Audiveris"),
        shutil.which("audiveris"),
        str(PROJECT_ROOT / "tools" / "audiveris" / "Audiveris" / "Audiveris.exe"),
        r"C:\Program Files\Audiveris\Audiveris.exe",
        r"C:\Program Files (x86)\Audiveris\Audiveris.exe",
        "/Applications/Audiveris.app/Contents/MacOS/Audiveris",
        "/usr/bin/audiveris",
        "/usr/local/bin/audiveris",
        "/opt/audiveris/bin/Audiveris",
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    return None


def omr_status() -> dict[str, object]:
    executable = find_audiveris()
    return {
        "available": executable is not None,
        "engine": "Audiveris" if executable else None,
        "executable": str(executable) if executable else None,
        "install_hint": None if executable else "scripts/install_omr.ps1",
    }


def transcribe_score_pdf(data: bytes, filename: str) -> ScoreOmrResult:
    """Recognize a PDF score into MusicXML with Audiveris batch mode."""

    executable = find_audiveris()
    if executable is None:
        raise ScoreOmrUnavailableError(
            "未找到 Audiveris OMR 引擎。请先运行 scripts/install_omr.ps1 安装，"
            "或设置环境变量 PIANO_MIDI_SCORE_AUDIVERIS 指向 Audiveris 可执行文件"
        )

    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="piano-midi-score-omr-") as temporary:
        temp_dir = Path(temporary)
        stem = Path(filename).stem or "score"
        input_path = temp_dir / f"{stem}.pdf"
        upscale, dropped_pages = _prepare_pdf_for_omr(data, input_path)
        output_dir = temp_dir / "omr-out"
        output_dir.mkdir()

        command = [
            str(executable),
            "-batch",
            "-export",
            "-output",
            str(output_dir),
            str(input_path),
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=OMR_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScoreOmrError(
                f"Audiveris 识别超时（>{OMR_TIMEOUT_SECONDS // 60} 分钟），"
                "请拆分页数或改用更清晰的 PDF"
            ) from exc

        exports = sorted(output_dir.rglob("*.musicxml")) + sorted(
            output_dir.rglob("*.mxl")
        )
        if not exports:
            log_tail = ((completed.stdout or "") + (completed.stderr or ""))[-_LOG_TAIL:]
            raise ScoreOmrError(
                f"Audiveris 未导出 MusicXML（退出码 {completed.returncode}）。{log_tail.strip()}"
            )
        musicxml = _read_export(exports[0])

    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    warnings = [
        "PDF 经 Audiveris 光学识别（OMR）得到；复杂谱面可能存在识别误差，"
        "下载 MusicXML 后请在打谱软件中校对"
    ]
    if upscale > 1.0:
        warnings.append(
            f"PDF 页面物理尺寸较小，已放大 {upscale:.2f} 倍以满足 OMR 分辨率要求"
        )
    if dropped_pages:
        warnings.append(f"已跳过 {dropped_pages} 个空白页")
    return ScoreOmrResult(
        musicxml=musicxml,
        analysis={
            "engine": "Audiveris",
            "executable": str(executable),
            "pdf_upscale": upscale,
            "dropped_blank_pages": dropped_pages,
            "processing_ms": elapsed_ms,
        },
        warnings=warnings,
    )


def _prepare_pdf_for_omr(data: bytes, output_path: Path) -> tuple[float, int]:
    """Write the upload to *output_path*, ready for Audiveris.

    Two normalizations happen here:

    * Blank pages (near-empty content streams) are dropped — Audiveris
      aborts the whole book export when a sheet contains no staff lines.
    * Small pages are upscaled: Audiveris renders at 300 DPI and rejects
      sheets whose staff interline is only a few pixels; vector pages scale
      losslessly.  The scale factor stays below Audiveris's pixel cap.

    Returns the applied upscale factor and the number of dropped pages.
    """

    try:
        reader = PdfReader(BytesIO(data))
    except Exception as exc:  # pypdf raises several error types for bad PDFs
        raise ScoreOmrError(f"无法读取 PDF 文件：{exc}") from exc
    if not reader.pages:
        raise ScoreOmrError("PDF 没有页面")

    scale = 1.0
    for page in reader.pages:
        width_px = float(page.mediabox.width) * _AUDIVERIS_RASTER_DPI / 72
        height_px = float(page.mediabox.height) * _AUDIVERIS_RASTER_DPI / 72
        if width_px <= 0 or height_px <= 0:
            continue
        needed = _TARGET_MIN_WIDTH_PIXELS / width_px
        headroom = (_AUDIVERIS_MAX_SHEET_PIXELS / (width_px * height_px)) ** 0.5
        scale = max(scale, min(needed, headroom, 4.0))
    scale = round(scale, 2)

    writer = PdfWriter()
    dropped = 0
    for page in reader.pages:
        if _is_blank_page(page):
            dropped += 1
            continue
        if scale > 1.0:
            page.scale_by(scale)
        writer.add_page(page)
    if dropped >= len(reader.pages):
        raise ScoreOmrError("PDF 各页均未检测到乐谱内容（空白页）")
    if dropped == 0 and scale <= 1.0:
        output_path.write_bytes(data)
        return 1.0, 0

    with output_path.open("wb") as stream:
        writer.write(stream)
    return scale, dropped


def _is_blank_page(page) -> bool:
    """A page whose content stream is only a graphics-state preamble."""

    contents = page.get("/Contents")
    if contents is None:
        return True
    streams = contents if isinstance(contents, list) else [contents]
    try:
        total = sum(len(stream.get_object().get_data()) for stream in streams)
    except Exception:  # undecodable stream: keep the page, let Audiveris judge
        return False
    return total < 256


def _read_export(export_path: Path) -> str:
    if export_path.suffix.lower() == ".mxl":
        try:
            with zipfile.ZipFile(export_path) as archive:
                member = next(
                    name
                    for name in archive.namelist()
                    if name.lower().endswith((".musicxml", ".xml"))
                    and not name.startswith("META-INF")
                )
                text = archive.read(member).decode("utf-8-sig")
        except (zipfile.BadZipFile, KeyError, StopIteration, UnicodeDecodeError) as exc:
            raise ScoreOmrError(f"Audiveris 导出的 MXL 无法读取：{exc}") from exc
    else:
        text = export_path.read_text(encoding="utf-8-sig")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise ScoreOmrError(f"Audiveris 导出的 MusicXML 无效：{exc}") from exc
    if root.tag not in {"score-partwise", "score-timewise"}:
        raise ScoreOmrError(f"Audiveris 导出的不是 MusicXML 乐谱（根元素 {root.tag}）")
    return text


def normalize_omr_musicxml(text: str) -> tuple[str, dict[str, object]]:
    """Remove unsafe OMR structures and report whether notes need rebuilding.

    Audiveris can mistake long brackets beneath a system for first/second
    endings.  When a part contains no repeat barline at all, those endings are
    necessarily orphaned; MuseScore may then skip large ranges while exporting
    MIDI.  They are safe to remove.

    The function deliberately does *not* pad or truncate individual voices.
    MusicXML has one shared cursor per measure, so voice-by-voice duration
    edits corrupt otherwise valid polyphony.  Instead, serious cursor overflow
    is reported so the caller can rebuild the recognized notes on fixed bar
    boundaries.
    """

    analysis: dict[str, object] = {
        "removed_orphan_endings": 0,
        "measure_timing_anomalies": 0,
        "severe_measure_timing_anomalies": 0,
        "semantic_rebuild_recommended": False,
    }
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return text, analysis
    if root.tag != "score-partwise":
        return text, analysis

    removed_endings = 0
    timing_anomalies = 0
    severe_timing_anomalies = 0
    for part in root.findall("./part"):
        if not part.findall(".//repeat"):
            parents = {
                child: parent
                for parent in part.iter()
                for child in parent
            }
            for ending in list(part.iter("ending")):
                parent = parents.get(ending)
                if parent is not None:
                    parent.remove(ending)
                    removed_endings += 1

        part_anomalies, part_severe = _measure_timing_anomalies(part)
        timing_anomalies += part_anomalies
        severe_timing_anomalies += part_severe

    analysis.update(
        {
            "removed_orphan_endings": removed_endings,
            "measure_timing_anomalies": timing_anomalies,
            "severe_measure_timing_anomalies": severe_timing_anomalies,
            "semantic_rebuild_recommended": bool(
                removed_endings or severe_timing_anomalies
            ),
        }
    )
    if not removed_endings:
        return text, analysis
    return (
        ET.tostring(root, encoding="unicode", xml_declaration=True),
        analysis,
    )


def _measure_timing_anomalies(part: ET.Element) -> tuple[int, int]:
    """Count malformed measure spans using MusicXML's shared cursor model."""

    divisions = 1
    expected = Fraction(4, 1)
    anomalies = 0
    severe = 0
    measures = part.findall("./measure")
    for index, measure in enumerate(measures):
        cursor = Fraction(0, 1)
        minimum_cursor = cursor
        maximum_cursor = cursor
        for element in measure:
            if element.tag == "attributes":
                divisions = _read_divisions(element, divisions)
                expected = _read_time_duration(element, expected)
                continue

            duration = _duration_in_quarter_beats(element, divisions)
            if element.tag == "backup":
                cursor -= duration
            elif element.tag == "forward" or (
                element.tag == "note"
                and element.find("chord") is None
                and element.find("grace") is None
            ):
                cursor += duration
            minimum_cursor = min(minimum_cursor, cursor)
            maximum_cursor = max(maximum_cursor, cursor)

        implicit = measure.get("implicit") == "yes"
        if minimum_cursor < 0 or (not implicit and maximum_cursor != expected):
            anomalies += 1

        overflow = maximum_cursor - expected
        underflow = expected - maximum_cursor
        severe_overflow = max(Fraction(1, 2), expected / 8)
        severe_underflow = max(Fraction(1, 1), expected / 4)
        internal_measure = 0 < index < len(measures) - 1
        if (
            minimum_cursor < 0
            or overflow >= severe_overflow
            or (not implicit and internal_measure and underflow >= severe_underflow)
        ):
            severe += 1
    return anomalies, severe


def _read_divisions(attributes: ET.Element, current: int) -> int:
    value = attributes.findtext("divisions")
    try:
        parsed = int(value) if value is not None else current
    except ValueError:
        return current
    return max(1, parsed)


def _read_time_duration(
    attributes: ET.Element,
    current: Fraction,
) -> Fraction:
    time_element = attributes.find("time")
    if time_element is None or time_element.find("senza-misura") is not None:
        return current

    beats_elements = time_element.findall("beats")
    beat_type_elements = time_element.findall("beat-type")
    if not beats_elements or len(beats_elements) != len(beat_type_elements):
        return current

    duration = Fraction(0, 1)
    try:
        for beats_element, beat_type_element in zip(
            beats_elements,
            beat_type_elements,
            strict=True,
        ):
            beat_type = int(beat_type_element.text or "0")
            if beat_type <= 0:
                return current
            beats = sum(
                int(component.strip())
                for component in (beats_element.text or "").split("+")
            )
            duration += Fraction(beats * 4, beat_type)
    except ValueError:
        return current
    return duration if duration > 0 else current


def _duration_in_quarter_beats(
    element: ET.Element,
    divisions: int,
) -> Fraction:
    value = element.findtext("duration")
    try:
        duration = int(value) if value is not None else 0
    except ValueError:
        duration = 0
    return Fraction(max(0, duration), max(1, divisions))


# ---------------------------------------------------------------------------
# Tolerant note extraction from OMR MusicXML
# ---------------------------------------------------------------------------

_STEP_TO_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


@dataclass(slots=True)
class OmrNote:
    pitch: int
    onset: float      # beats, continuous per voice
    duration: float   # beats
    staff: int
    voice: int


def parse_omr_notes(text: str) -> list[OmrNote]:
    """Pull notes from partwise MusicXML on stable measure boundaries.

    MusicXML uses one cursor shared by all voices inside each measure;
    ``<backup>`` rewinds that cursor before another voice or staff is written.
    Audiveris can overfill an isolated measure, so normal measures always
    advance by the current time signature instead of allowing one bad bar to
    shift the remainder of the piece.
    """

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    if root.tag != "score-partwise":
        return []

    notes: list[OmrNote] = []
    for part_index, part in enumerate(root.findall("./part")):
        divisions = 1
        measure_duration = Fraction(4, 1)
        measure_start = Fraction(0, 1)
        open_ties: dict[tuple[int, int, str, int], OmrNote] = {}
        for measure in part.findall("./measure"):
            cursor = Fraction(0, 1)
            maximum_cursor = cursor
            chord_onsets: dict[tuple[int, str], Fraction] = {}
            for element in measure:
                if element.tag == "attributes":
                    divisions = _read_divisions(element, divisions)
                    measure_duration = _read_time_duration(
                        element,
                        measure_duration,
                    )
                    continue
                if element.tag == "backup":
                    cursor = max(
                        Fraction(0, 1),
                        cursor - _duration_in_quarter_beats(element, divisions),
                    )
                    continue
                if element.tag == "forward":
                    cursor += _duration_in_quarter_beats(element, divisions)
                    maximum_cursor = max(maximum_cursor, cursor)
                    continue
                if element.tag != "note":
                    continue

                duration = _duration_in_quarter_beats(element, divisions)
                voice = element.findtext("voice") or "1"
                staff_text = element.findtext("staff")
                staff = int(staff_text) if staff_text and staff_text.isdigit() else 1
                is_rest = element.find("rest") is not None
                is_chord = element.find("chord") is not None
                is_grace = element.find("grace") is not None
                voice_key = (staff, voice)

                if is_chord:
                    local_onset = chord_onsets.get(
                        voice_key,
                        max(Fraction(0, 1), cursor - duration),
                    )
                else:
                    local_onset = cursor
                    chord_onsets[voice_key] = local_onset
                    if not is_grace:
                        cursor += duration
                maximum_cursor = max(
                    maximum_cursor,
                    cursor,
                    local_onset + duration,
                )
                if is_rest:
                    continue
                pitch_element = element.find("pitch")
                if pitch_element is None:
                    continue
                step = pitch_element.findtext("step")
                octave_text = pitch_element.findtext("octave")
                if not step or not octave_text or step.upper() not in _STEP_TO_PC:
                    continue
                alter_text = pitch_element.findtext("alter")
                try:
                    alter = round(float(alter_text)) if alter_text else 0
                    octave = int(octave_text)
                except ValueError:
                    continue
                pitch = (octave + 1) * 12 + _STEP_TO_PC[step.upper()] + alter
                onset = float(measure_start + local_onset)
                note_duration = max(float(duration), 0.05)

                tie_types = {
                    tie.get("type")
                    for tie in [
                        *element.findall("tie"),
                        *element.findall("notations/tied"),
                    ]
                }
                tie_stop = "stop" in tie_types
                tie_start = "start" in tie_types
                tie_key = (part_index, staff, voice, pitch)
                if tie_stop and tie_key in open_ties:
                    open_note = open_ties[tie_key]
                    open_note.duration = max(
                        open_note.duration,
                        onset + note_duration - open_note.onset,
                    )
                    if not tie_start:
                        del open_ties[tie_key]
                    continue

                note = OmrNote(
                    pitch=pitch,
                    onset=onset,
                    duration=note_duration,
                    staff=staff,
                    voice=int(voice) if voice.isdigit() else 1,
                )
                notes.append(note)
                if tie_start:
                    open_ties[tie_key] = note

            if measure.get("implicit") == "yes" and maximum_cursor > 0:
                measure_start += min(maximum_cursor, measure_duration)
            else:
                measure_start += measure_duration
    return notes


def omr_notes_to_midi_bytes(notes: list[OmrNote]) -> bytes:
    """Synthesize a two-track MIDI (treble/bass by staff) from parsed notes."""

    import mido

    ticks_per_beat = 480
    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    meta.append(mido.MetaMessage("track_name", name="OMR", time=0))
    meta.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(meta)
    for staff, name in ((1, "Right"), (2, "Left")):
        events: list[tuple[int, int, int, int]] = []
        for note in notes:
            if note.staff != staff:
                continue
            tick = max(0, round(note.onset * ticks_per_beat))
            duration = max(1, round(note.duration * ticks_per_beat))
            events.append((tick, 1, note.pitch, 80))
            events.append((tick + duration, 0, note.pitch, 0))
        events.sort(key=lambda event: (event[0], event[1], event[2]))
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=name, time=0))
        last = 0
        for tick, kind, pitch, velocity in events:
            pitch = max(0, min(127, pitch))
            if kind:
                track.append(mido.Message("note_on", note=pitch, velocity=velocity, time=max(0, tick - last)))
            else:
                track.append(mido.Message("note_off", note=pitch, velocity=0, time=max(0, tick - last)))
            last = max(last, tick)
        track.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(track)
    buffer = BytesIO()
    mid.save(file=buffer)
    return buffer.getvalue()
