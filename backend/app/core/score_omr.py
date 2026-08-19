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
