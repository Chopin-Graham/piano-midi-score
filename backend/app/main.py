from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from typing import Annotated
from xml.etree import ElementTree as ET

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__
from .core.demo import demo_midi_bytes
from .core.engraver import engraver_status, find_musescore, render_a4_musicxml
from .core.media_transcription import (
    MEDIA_EXTENSIONS,
    MediaTranscriptionError,
    transcribe_media,
    transcription_status,
)
from .core.midi_parser import MidiParseError
from .core.options import ConversionOptions, TranscriptionOptions
from .core.pipeline import convert_midi
from .core.roundtrip import musicxml_to_midi_bytes
from .core.score_omr import (
    ScoreOmrError,
    ScoreOmrUnavailableError,
    omr_status,
    transcribe_score_pdf,
)
from .schemas import ConversionResponse, HealthResponse, OptionsResponse

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_MEDIA_UPLOAD_BYTES = 250 * 1024 * 1024
MAX_PDF_UPLOAD_BYTES = 50 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".mid", ".midi"}
SCORE_EXTENSIONS = {".musicxml", ".xml", ".mxl"}
SCORE_PDF_EXTENSIONS = {".pdf"}

app = FastAPI(
    title="Piano MIDI Score",
    version=__version__,
    description="Convert piano MIDI performances into clean MusicXML notation.",
)
app.add_middleware(GZipMiddleware, minimum_size=1_000)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        version=__version__,
        engraver=engraver_status(),
        transcriber=transcription_status(),
        omr=omr_status(),
    )


@app.get("/api/options", response_model=OptionsResponse)
def options() -> OptionsResponse:
    return OptionsResponse(
        defaults=ConversionOptions(),
        transcription_defaults=TranscriptionOptions(),
        max_upload_bytes=MAX_UPLOAD_BYTES,
        max_media_upload_bytes=MAX_MEDIA_UPLOAD_BYTES,
        max_pdf_upload_bytes=MAX_PDF_UPLOAD_BYTES,
        supported_extensions=sorted(SUPPORTED_EXTENSIONS),
        supported_media_extensions=sorted(MEDIA_EXTENSIONS),
        supported_score_extensions=sorted(SCORE_EXTENSIONS | SCORE_PDF_EXTENSIONS),
    )


@app.post("/api/convert", response_model=ConversionResponse)
async def convert(
    file: Annotated[UploadFile, File()],
    options_json: Annotated[str, Form()] = "{}",
) -> ConversionResponse:
    filename = Path(file.filename or "score.mid").name
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS | SCORE_EXTENSIONS | SCORE_PDF_EXTENSIONS:
        raise HTTPException(
            status_code=415,
            detail="仅支持 .mid、.midi、.musicxml/.xml/.mxl 或 .pdf 文件",
        )

    size_limit = MAX_PDF_UPLOAD_BYTES if extension in SCORE_PDF_EXTENSIONS else MAX_UPLOAD_BYTES
    data = await file.read(size_limit + 1)
    await file.close()
    if len(data) > size_limit:
        limit_mb = size_limit // (1024 * 1024)
        raise HTTPException(status_code=413, detail=f"文件不能超过 {limit_mb} MB")

    try:
        raw_options = json.loads(options_json)
        conversion_options = ConversionOptions.model_validate(raw_options)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="options_json 不是有效 JSON") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc

    if extension in SCORE_PDF_EXTENSIONS:
        return await _convert_score_pdf_upload(data, filename, conversion_options)

    if extension in SCORE_EXTENSIONS:
        return await _convert_score_upload(data, filename, conversion_options)

    try:
        musicxml, analysis, warnings = await run_in_threadpool(
            convert_midi,
            data,
            filename,
            conversion_options,
        )
    except MidiParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, ZeroDivisionError) as exc:
        raise HTTPException(status_code=400, detail=f"转换失败：{exc}") from exc

    engraving = await run_in_threadpool(
        render_a4_musicxml,
        musicxml,
        conversion_options.engraving_style,
    )
    analysis["engraving"] = engraving.analysis
    warnings.extend(engraving.warnings)
    output_name = f"{Path(filename).stem or 'score'}.musicxml"
    pdf_name = f"{Path(filename).stem or 'score'}-A4.pdf" if engraving.pdf_bytes else None
    return ConversionResponse(
        filename=output_name,
        musicxml=musicxml,
        pdf_filename=pdf_name,
        pdf_base64=_encode_bytes(engraving.pdf_bytes),
        preview_png_base64=_encode_bytes(engraving.preview_png),
        analysis=analysis,
        warnings=list(dict.fromkeys(warnings)),
    )


async def _convert_score_upload(
    data: bytes,
    filename: str,
    conversion_options: ConversionOptions,
) -> ConversionResponse:
    """Render an uploaded MusicXML score to A4 PDF and export its MIDI.

    The file is already engraved notation, so the semantic MIDI pipeline
    (quantization, hand splitting, voice separation) is deliberately not
    applied; MuseScore handles both the PDF layout and the MIDI export.
    """

    musicxml = _decode_score_upload(data, filename)
    engraving = await run_in_threadpool(
        render_a4_musicxml,
        musicxml,
        conversion_options.engraving_style,
    )
    warnings = list(engraving.warnings)
    warnings.append(
        "MusicXML 输入按原谱直接雕版，未经过量化与分手流水线；需要语义清理时请上传 MIDI 源文件"
    )
    midi_bytes: bytes | None = None
    executable = find_musescore()
    if executable is not None:
        try:
            midi_bytes = await run_in_threadpool(
                musicxml_to_midi_bytes,
                musicxml,
                executable,
            )
        except (OSError, RuntimeError) as exc:
            warnings.append(f"MIDI 导出失败：{exc}")
    else:
        warnings.append("未找到 MuseScore Studio 4，无法从 MusicXML 导出 MIDI")

    stem = Path(filename).stem or "score"
    return ConversionResponse(
        filename=f"{stem}.musicxml",
        musicxml=musicxml,
        midi_filename=f"{stem}.mid" if midi_bytes else None,
        midi_base64=_encode_bytes(midi_bytes),
        pdf_filename=f"{stem}-A4.pdf" if engraving.pdf_bytes else None,
        pdf_base64=_encode_bytes(engraving.pdf_bytes),
        preview_png_base64=_encode_bytes(engraving.preview_png),
        analysis=_score_upload_analysis(musicxml, filename, engraving.analysis),
        warnings=list(dict.fromkeys(warnings)),
    )


async def _convert_score_pdf_upload(
    data: bytes,
    filename: str,
    conversion_options: ConversionOptions,
) -> ConversionResponse:
    """Recognize a PDF score via OMR, then handle it like a MusicXML upload."""

    try:
        omr = await run_in_threadpool(transcribe_score_pdf, data, filename)
    except ScoreOmrUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScoreOmrError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    stem = Path(filename).stem or "score"
    response = await _convert_score_upload(
        omr.musicxml.encode("utf-8"),
        f"{stem}.musicxml",
        conversion_options,
    )
    response.warnings = list(dict.fromkeys([*omr.warnings, *response.warnings]))
    response.analysis["omr"] = omr.analysis
    response.analysis["source"] = {
        **response.analysis.get("source", {}),
        "format": "pdf",
    }
    return response


def _decode_score_upload(data: bytes, filename: str) -> str:
    extension = Path(filename).suffix.lower()
    if extension == ".mxl":
        import zipfile

        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                container = None
                if "META-INF/container.xml" in archive.namelist():
                    container = ET.fromstring(archive.read("META-INF/container.xml"))
                    rootfile = container.find(".//rootfile")
                    if rootfile is not None and rootfile.get("full-path"):
                        member = rootfile.get("full-path")
                        assert member is not None
                        data = archive.read(member)
                    else:
                        container = None
                if container is None:
                    member = next(
                        name
                        for name in archive.namelist()
                        if name.lower().endswith((".musicxml", ".xml"))
                        and not name.startswith("META-INF")
                    )
                    data = archive.read(member)
        except (zipfile.BadZipFile, KeyError, StopIteration, ET.ParseError) as exc:
            raise HTTPException(status_code=400, detail=f"无法读取 compressed MusicXML：{exc}") from exc
    try:
        text = data.decode("utf-8-sig")
        root = ET.fromstring(text)
    except (UnicodeDecodeError, ET.ParseError) as exc:
        raise HTTPException(status_code=400, detail=f"不是有效的 MusicXML 文件：{exc}") from exc
    if root.tag not in {"score-partwise", "score-timewise"}:
        raise HTTPException(status_code=400, detail=f"不是 MusicXML 乐谱（根元素 {root.tag}）")
    return text


def _score_upload_analysis(
    musicxml: str,
    filename: str,
    engraving_analysis: dict[str, object],
) -> dict[str, object]:
    root = ET.fromstring(musicxml)
    title = (
        root.findtext("./work/work-title")
        or root.findtext("./movement-title")
        or Path(filename).stem
    )
    measures = root.findall(".//measure")
    return {
        "title": title.strip() or Path(filename).stem,
        "note_count": sum(
            note.find("pitch") is not None for note in root.findall(".//note")
        ),
        "measure_count": len(measures),
        "source": {"format": "musicxml", "semantic_pipeline": False},
        "engraving": engraving_analysis,
    }


@app.post("/api/convert-media", response_model=ConversionResponse)
async def convert_media(
    file: Annotated[UploadFile, File()],
    options_json: Annotated[str, Form()] = "{}",
    transcription_options_json: Annotated[str, Form()] = "{}",
) -> ConversionResponse:
    filename = Path(file.filename or "recording.wav").name
    extension = Path(filename).suffix.lower()
    if extension not in MEDIA_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported audio/video file type")

    data = await file.read(MAX_MEDIA_UPLOAD_BYTES + 1)
    await file.close()
    if len(data) > MAX_MEDIA_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio/video files cannot exceed 250 MB")

    try:
        conversion_payload = json.loads(options_json)
        conversion_options = ConversionOptions.model_validate(conversion_payload)
        conversion_updates: dict[str, object] = {"audio_transcription": True}
        if "include_pedal" not in conversion_payload:
            conversion_updates["include_pedal"] = False
        conversion_options = conversion_options.model_copy(
            update=conversion_updates
        )
        if conversion_options.title is None:
            conversion_options = conversion_options.model_copy(
                update={"title": Path(filename).stem}
            )
        transcription_options = TranscriptionOptions.model_validate(
            json.loads(transcription_options_json)
        )
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail="Options must be valid JSON") from exc
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc

    try:
        transcription = await run_in_threadpool(
            transcribe_media,
            data,
            filename,
            transcription_options,
        )
        midi_name = f"{Path(filename).stem or 'recording'}-transcribed.mid"
        musicxml, analysis, warnings = await run_in_threadpool(
            convert_midi,
            transcription.midi_bytes,
            midi_name,
            conversion_options,
        )
    except MediaTranscriptionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except MidiParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, ZeroDivisionError) as exc:
        raise HTTPException(status_code=400, detail=f"Conversion failed: {exc}") from exc

    engraving = await run_in_threadpool(
        render_a4_musicxml,
        musicxml,
        conversion_options.engraving_style,
    )
    analysis["transcription"] = transcription.analysis
    analysis["engraving"] = engraving.analysis
    warnings = [*transcription.warnings, *warnings, *engraving.warnings]
    stem = Path(filename).stem or "recording"
    return ConversionResponse(
        filename=f"{stem}.musicxml",
        musicxml=musicxml,
        midi_filename=f"{stem}-transcribed.mid",
        midi_base64=_encode_bytes(transcription.midi_bytes),
        pdf_filename=f"{stem}-A4.pdf" if engraving.pdf_bytes else None,
        pdf_base64=_encode_bytes(engraving.pdf_bytes),
        preview_png_base64=_encode_bytes(engraving.preview_png),
        analysis=analysis,
        warnings=list(dict.fromkeys(warnings)),
    )


@app.post("/api/demo", response_model=ConversionResponse)
async def demo(conversion_options: ConversionOptions) -> ConversionResponse:
    musicxml, analysis, warnings = await run_in_threadpool(
        convert_midi,
        demo_midi_bytes(),
        "demo-piano.mid",
        conversion_options,
    )
    engraving = await run_in_threadpool(
        render_a4_musicxml,
        musicxml,
        conversion_options.engraving_style,
    )
    analysis["engraving"] = engraving.analysis
    warnings.extend(engraving.warnings)
    return ConversionResponse(
        filename="demo-piano.musicxml",
        musicxml=musicxml,
        pdf_filename="demo-piano-A4.pdf" if engraving.pdf_bytes else None,
        pdf_base64=_encode_bytes(engraving.pdf_bytes),
        preview_png_base64=_encode_bytes(engraving.preview_png),
        analysis=analysis,
        warnings=list(dict.fromkeys(warnings)),
    )


def _encode_bytes(value: bytes | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


PROJECT_ROOT = Path(__file__).resolve().parents[2]
FRONTEND_DIST = PROJECT_ROOT / "frontend" / "dist"
if (FRONTEND_DIST / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")


@app.get("/{full_path:path}", include_in_schema=False)
def frontend(full_path: str):
    if FRONTEND_DIST.is_dir():
        requested = (FRONTEND_DIST / full_path).resolve()
        if requested.is_file() and FRONTEND_DIST.resolve() in requested.parents:
            return FileResponse(requested)
        return FileResponse(FRONTEND_DIST / "index.html")
    raise HTTPException(
        status_code=404,
        detail="前端尚未构建，请在 frontend 目录运行 npm install && npm run build",
    )
