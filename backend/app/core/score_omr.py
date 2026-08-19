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


def _repair_omr_musicxml(text: str) -> str:
    """Make Audiveris exports loadable: balance every voice's measure total.

    Audiveris occasionally exports measures whose voices do not fill the bar
    (or overflow it), and strict importers reject the whole file over a single
    such measure.  MusicXML measures use one shared cursor with ``<backup>``
    rewinds; the repair recomputes that timeline and only touches a voice
    whose total genuinely disagrees with the bar: underfull voices gain a
    trailing ``<forward>``, overfull voices have their final element shortened.
    """

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return text
    if root.tag != "score-partwise":
        return text

    for part in root.iter("part"):
        divisions = 1
        beats = 4
        beat_type = 4
        for measure in part.iter("measure"):
            for attributes in measure.iter("attributes"):
                div_text = attributes.findtext("divisions")
                if div_text and div_text.isdigit():
                    divisions = max(1, int(div_text))
                beats_text = attributes.findtext("time/beats")
                beat_type_text = attributes.findtext("time/beat-type")
                if beats_text and beat_type_text:
                    try:
                        beats = int(beats_text)
                        beat_type = int(beat_type_text)
                    except ValueError:
                        pass
            expected = round(divisions * beats * 4 / beat_type)
            if expected <= 0:
                continue

            children = list(measure)
            # Single shared cursor; <backup> rewinds it.  A voice's end is the
            # cursor value right after its last written element.
            cursor = 0
            voice_end: dict[tuple[str, str], int] = {}
            voice_last_index: dict[tuple[str, str], int] = {}
            for index, element in enumerate(children):
                if element.tag == "backup":
                    duration_text = element.findtext("duration")
                    if duration_text and duration_text.lstrip("-").isdigit():
                        cursor -= int(duration_text)
                    continue
                if element.tag not in {"note", "forward"}:
                    continue
                duration_text = element.findtext("duration")
                duration = int(duration_text) if duration_text and duration_text.isdigit() else 0
                voice = element.findtext("voice") or "1"
                staff = element.findtext("staff") or "1"
                key = (staff, voice)
                if element.tag == "forward":
                    cursor += duration
                elif element.find("chord") is not None:
                    end = cursor + duration
                    if end > voice_end.get(key, 0):
                        voice_end[key] = end
                    continue
                else:
                    cursor += duration
                if cursor > voice_end.get(key, 0):
                    voice_end[key] = cursor
                voice_last_index[key] = index

            for (staff, voice), end in voice_end.items():
                gap = expected - end
                if gap == 0:
                    continue
                if gap > 0:
                    forward = ET.Element("forward")
                    ET.SubElement(forward, "duration").text = str(gap)
                    ET.SubElement(forward, "voice").text = voice
                    ET.SubElement(forward, "staff").text = staff
                    insert_at = voice_last_index.get((staff, voice), len(children) - 1) + 1
                    measure.insert(insert_at, forward)
                else:
                    last = children[voice_last_index.get((staff, voice), len(children) - 1)]
                    duration_element = last.find("duration")
                    if duration_element is not None and duration_element.text:
                        current = int(duration_element.text)
                        duration_element.text = str(max(1, current + gap))

    return ET.tostring(root, encoding="unicode", xml_declaration=True)


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
    """Pull every note out of an OMR MusicXML export, tolerating broken bars.

    Audiveris exports occasionally carry corrupt measures that strict
    importers reject; the notation pipeline below only needs pitches and a
    continuous timeline, which ``<duration>`` ticks provide.  Voices advance
    their own cursors; ``<backup>`` rewinds; ties merge into the open note.
    """

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    if root.tag not in {"score-partwise", "score-timewise"}:
        return []

    notes: list[OmrNote] = []
    for part in root.iter("part"):
        divisions = 1.0
        cursors: dict[str, float] = {}
        open_ties: dict[tuple[str, int], OmrNote] = {}
        for measure in part.iter("measure"):
            for element in measure:
                if element.tag == "attributes":
                    div_text = element.findtext("divisions")
                    if div_text and div_text.isdigit():
                        divisions = max(1.0, float(int(div_text)))
                    continue
                if element.tag == "backup":
                    duration_text = element.findtext("duration")
                    if duration_text and duration_text.lstrip("-").isdigit():
                        # Rewind every voice sharing the cursor position is
                        # wrong; Audiveris backups rewind the whole part
                        # timeline, so shift all voices that sit at the tip.
                        back = int(duration_text) / divisions
                        if cursors:
                            tip = max(cursors.values())
                            for voice in cursors:
                                if cursors[voice] == tip:
                                    cursors[voice] = tip - back
                        continue
                    continue
                if element.tag == "forward":
                    duration_text = element.findtext("duration")
                    if duration_text and duration_text.isdigit():
                        voice = element.findtext("voice") or "1"
                        cursors[voice] = cursors.get(voice, 0.0) + int(duration_text) / divisions
                    continue
                if element.tag != "note":
                    continue

                duration_text = element.findtext("duration")
                duration = (int(duration_text) / divisions) if duration_text and duration_text.isdigit() else 0.0
                voice = element.findtext("voice") or "1"
                staff_text = element.findtext("staff")
                staff = int(staff_text) if staff_text and staff_text.isdigit() else 1
                is_rest = element.find("rest") is not None
                is_chord = element.find("chord") is not None

                if is_chord and notes:
                    onset = notes[-1].onset
                else:
                    onset = cursors.get(voice, 0.0)
                    cursors[voice] = onset + duration
                if is_rest:
                    continue
                pitch_element = element.find("pitch")
                if pitch_element is None:
                    continue
                step = pitch_element.findtext("step")
                octave_text = pitch_element.findtext("octave")
                if not step or not octave_text:
                    continue
                alter_text = pitch_element.findtext("alter")
                alter = int(float(alter_text)) if alter_text else 0
                pitch = (int(octave_text) + 1) * 12 + _STEP_TO_PC.get(step.upper(), 0) + alter

                tie_stop = element.find("tie[@type='stop']") is not None
                tie_start = element.find("tie[@type='start']") is not None
                if tie_stop and (voice, pitch) in open_ties:
                    open_note = open_ties.pop((voice, pitch))
                    open_note.duration = max(open_note.duration, onset + duration - open_note.onset)
                    if not tie_start:
                        continue
                    # tie continues: fall through and re-open below
                    if tie_start:
                        open_ties[(voice, pitch)] = open_note
                        continue
                note = OmrNote(pitch=pitch, onset=onset, duration=max(duration, 0.05), staff=staff, voice=int(voice) if voice.isdigit() else 1)
                notes.append(note)
                if tie_start:
                    open_ties[(voice, pitch)] = note
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
