from __future__ import annotations

import base64
import json
import re
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
    normalize_omr_musicxml,
    omr_notes_to_midi_bytes,
    omr_status,
    parse_omr_notes,
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

    musicxml, analysis, warnings, engraving = await _engrave_with_safe_fallback(
        data, filename, musicxml, analysis, warnings, conversion_options
    )
    analysis["engraving"] = engraving.analysis
    warnings.extend(engraving.warnings)
    stem = _safe_output_stem(filename, conversion_options.output_filename, "score")
    analysis["output_filename"] = stem
    return ConversionResponse(
        filename=f"{stem}.musicxml",
        musicxml=musicxml,
        pdf_filename=f"{stem}-A4.pdf" if engraving.pdf_bytes else None,
        pdf_base64=_encode_bytes(engraving.pdf_bytes),
        preview_png_base64=_encode_bytes(engraving.preview_png),
        analysis=analysis,
        warnings=list(dict.fromkeys(warnings)),
    )


def _engraving_failed(engraving) -> bool:
    """MuseScore is installed but could not load/render the score."""

    return (
        engraving.pdf_bytes is None
        and engraving.analysis.get("engine") == "MuseScore Studio 4"
        and not engraving.analysis.get("available", True)
    )


async def _engrave_with_safe_fallback(
    data: bytes,
    filename: str,
    musicxml: str,
    analysis: dict,
    warnings: list[str],
    conversion_options: ConversionOptions,
):
    """Guarantee a renderable result: retry once in safe notation mode.

    Dense or rhythmically free transcriptions can produce notation constructs
    that MuseScore's importer rejects.  The safe mode converts again with
    binary-only grids and no triplets — plain but always loadable — so the
    user still gets a full result instead of a failure.
    """

    engraving = await run_in_threadpool(
        render_a4_musicxml,
        musicxml,
        conversion_options.engraving_style,
    )
    if not _engraving_failed(engraving):
        return musicxml, analysis, warnings, engraving

    safe_options = conversion_options.model_copy(
        update={"style": "clean", "allow_triplets": False}
    )
    safe_musicxml, safe_analysis, safe_warnings = await run_in_threadpool(
        convert_midi,
        data,
        filename,
        safe_options,
    )
    safe_engraving = await run_in_threadpool(
        render_a4_musicxml,
        safe_musicxml,
        safe_options.engraving_style,
    )
    if safe_engraving.pdf_bytes is None:
        return musicxml, analysis, warnings, engraving
    safe_warnings.append(
        "完整记谱触发了 MuseScore 导入兼容性风险，已自动切换为保守记谱模式重试并成功"
    )
    return safe_musicxml, safe_analysis, safe_warnings, safe_engraving


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
    musicxml = _apply_score_metadata(
        musicxml,
        title=conversion_options.title,
        author=conversion_options.author,
    )
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

    stem = _safe_output_stem(filename, conversion_options.output_filename, "score")
    analysis = _score_upload_analysis(musicxml, filename, engraving.analysis)
    analysis["output_filename"] = stem
    return ConversionResponse(
        filename=f"{stem}.musicxml",
        musicxml=musicxml,
        midi_filename=f"{stem}.mid" if midi_bytes else None,
        midi_base64=_encode_bytes(midi_bytes),
        pdf_filename=f"{stem}-A4.pdf" if engraving.pdf_bytes else None,
        pdf_base64=_encode_bytes(engraving.pdf_bytes),
        preview_png_base64=_encode_bytes(engraving.preview_png),
        analysis=analysis,
        warnings=list(dict.fromkeys(warnings)),
    )


async def _convert_score_pdf_upload(
    data: bytes,
    filename: str,
    conversion_options: ConversionOptions,
) -> ConversionResponse:
    """Recognize a PDF score via OMR, then handle it like a MusicXML upload.

    Structurally healthy OMR output is engraved directly. Orphaned endings or
    seriously malformed measure cursors trigger a note-level semantic rebuild
    even when MuseScore can technically open the raw export, because those
    defects otherwise cause skipped passages and cumulative rhythm drift.
    """

    try:
        omr = await run_in_threadpool(transcribe_score_pdf, data, filename)
    except ScoreOmrUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ScoreOmrError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    stem = Path(filename).stem or "score"
    output_stem = _safe_output_stem(filename, conversion_options.output_filename, "score")
    normalized_musicxml, structure_analysis = normalize_omr_musicxml(omr.musicxml)
    omr_analysis = {**omr.analysis, **structure_analysis}
    response = await _convert_score_upload(
        normalized_musicxml.encode("utf-8"),
        f"{stem}.musicxml",
        conversion_options,
    )

    removed_endings = int(structure_analysis["removed_orphan_endings"])
    severe_measures = int(
        structure_analysis["severe_measure_timing_anomalies"]
    )
    structure_warnings: list[str] = []
    if removed_endings:
        structure_warnings.append(
            f"检测到 {removed_endings} 个没有对应反复记号的结局线，"
            "疑似由横向括号误识别，已安全移除"
        )
    if severe_measures:
        structure_warnings.append(
            f"检测到 {severe_measures} 个小节的 MusicXML 时间轴严重异常，"
            "将按固定小节边界重建，避免节奏错误向后累积"
        )

    engraving_analysis = response.analysis.get("engraving", {})
    raw_render_failed = bool(
        response.pdf_base64 is None
        and isinstance(engraving_analysis, dict)
        and engraving_analysis.get("engine") == "MuseScore Studio 4"
        and not engraving_analysis.get("available", True)
    )
    should_rebuild = bool(
        structure_analysis["semantic_rebuild_recommended"] or raw_render_failed
    )
    semantic_rebuilt = False
    if should_rebuild:
        notes = parse_omr_notes(normalized_musicxml)
        if notes:
            try:
                synthetic_midi = omr_notes_to_midi_bytes(notes)
                musicxml, analysis, warnings = await run_in_threadpool(
                    convert_midi,
                    synthetic_midi,
                    f"{stem}.mid",
                    conversion_options,
                )
                (
                    musicxml,
                    analysis,
                    warnings,
                    engraving,
                ) = await _engrave_with_safe_fallback(
                    synthetic_midi,
                    f"{stem}.mid",
                    musicxml,
                    analysis,
                    warnings,
                    conversion_options,
                )
            except (MidiParseError, OSError, RuntimeError, ValueError, ZeroDivisionError) as exc:
                response.warnings.append(
                    f"OMR 结构异常，但语义重建失败，已保留清理后的识别结果：{exc}"
                )
            else:
                analysis["engraving"] = engraving.analysis
                analysis["output_filename"] = output_stem
                warnings.extend(engraving.warnings)
                warnings.append(
                    "OMR 结构异常，已按识别音符和固定小节边界重新制谱；"
                    "原始文字、力度和连线可能无法完整保留"
                )
                response = ConversionResponse(
                    filename=f"{output_stem}.musicxml",
                    musicxml=musicxml,
                    midi_filename=f"{output_stem}.mid",
                    midi_base64=_encode_bytes(synthetic_midi),
                    pdf_filename=(
                        f"{output_stem}-A4.pdf" if engraving.pdf_bytes else None
                    ),
                    pdf_base64=_encode_bytes(engraving.pdf_bytes),
                    preview_png_base64=_encode_bytes(engraving.preview_png),
                    analysis=analysis,
                    warnings=warnings,
                )
                semantic_rebuilt = True
        else:
            response.warnings.append(
                "OMR 结构异常，但没有提取到可重建的音符，已保留清理后的识别结果"
            )
    omr_analysis["semantic_rebuilt"] = semantic_rebuilt
    response.warnings = list(
        dict.fromkeys([*omr.warnings, *structure_warnings, *response.warnings])
    )
    response.analysis["omr"] = omr_analysis
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


def _apply_score_metadata(
    musicxml: str,
    *,
    title: str | None,
    author: str | None,
) -> str:
    if title is None and author is None:
        return musicxml

    root = ET.fromstring(musicxml)
    if title is not None:
        work = root.find("./work")
        if work is None:
            work = ET.Element("work")
            _insert_root_metadata(root, work, before={
                "movement-number",
                "movement-title",
                "identification",
                "defaults",
                "credit",
                "part-list",
                "part",
                "measure",
            })
        work_title = work.find("./work-title")
        if work_title is None:
            work_title = ET.SubElement(work, "work-title")
        work_title.text = title
        _set_score_credit(root, "title", title)

    if author is not None:
        identification = root.find("./identification")
        if identification is None:
            identification = ET.Element("identification")
            _insert_root_metadata(
                root,
                identification,
                before={"defaults", "credit", "part-list", "part", "measure"},
            )
        creators = identification.findall("./creator[@type='composer']")
        if creators:
            creators[0].text = author
            for duplicate in creators[1:]:
                identification.remove(duplicate)
        else:
            creator = ET.Element("creator", type="composer")
            creator.text = author
            identification.insert(0, creator)
        _set_score_credit(root, "composer", author)

    ET.indent(root, space="  ")
    score_kind = "Partwise" if root.tag == "score-partwise" else "Timewise"
    dtd_name = "partwise" if root.tag == "score-partwise" else "timewise"
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        f'<!DOCTYPE {root.tag} PUBLIC "-//Recordare//DTD MusicXML 4.0 {score_kind}//EN" '
        f'"http://www.musicxml.org/dtds/{dtd_name}.dtd">\n'
        + ET.tostring(root, encoding="unicode", short_empty_elements=True)
    )


def _insert_root_metadata(
    root: ET.Element,
    element: ET.Element,
    *,
    before: set[str],
) -> None:
    for index, child in enumerate(root):
        if child.tag in before:
            root.insert(index, element)
            return
    root.append(element)


def _set_score_credit(root: ET.Element, credit_type: str, value: str) -> None:
    for credit in root.findall("./credit"):
        if credit.findtext("./credit-type") == credit_type:
            words = credit.find("./credit-words")
            if words is None:
                words = ET.SubElement(credit, "credit-words")
            words.text = value
            return

    credit = ET.Element("credit", page="1")
    ET.SubElement(credit, "credit-type").text = credit_type
    attributes = {
        "default-x": "600" if credit_type == "title" else "1120",
        "default-y": "1600" if credit_type == "title" else "1550",
        "justify": "center" if credit_type == "title" else "right",
        "valign": "top",
        "font-size": "22" if credit_type == "title" else "11",
    }
    ET.SubElement(credit, "credit-words", attributes).text = value
    _insert_root_metadata(root, credit, before={"part-list", "part", "measure"})


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
    author = root.findtext("./identification/creator[@type='composer']")
    measures = root.findall(".//measure")
    return {
        "title": title.strip() or Path(filename).stem,
        "author": author.strip() if author and author.strip() else None,
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

    musicxml, analysis, warnings, engraving = await _engrave_with_safe_fallback(
        transcription.midi_bytes,
        midi_name,
        musicxml,
        analysis,
        warnings,
        conversion_options,
    )
    analysis["transcription"] = transcription.analysis
    analysis["engraving"] = engraving.analysis
    warnings = [*transcription.warnings, *warnings, *engraving.warnings]
    stem = _safe_output_stem(filename, conversion_options.output_filename, "recording")
    analysis["output_filename"] = stem
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
    stem = _safe_output_stem(
        "demo-piano.mid",
        conversion_options.output_filename,
        "demo-piano",
    )
    analysis["output_filename"] = stem
    return ConversionResponse(
        filename=f"{stem}.musicxml",
        musicxml=musicxml,
        pdf_filename=f"{stem}-A4.pdf" if engraving.pdf_bytes else None,
        pdf_base64=_encode_bytes(engraving.pdf_bytes),
        preview_png_base64=_encode_bytes(engraving.preview_png),
        analysis=analysis,
        warnings=list(dict.fromkeys(warnings)),
    )


def _encode_bytes(value: bytes | None) -> str | None:
    if value is None:
        return None
    return base64.b64encode(value).decode("ascii")


def _safe_output_stem(
    source_filename: str,
    requested: str | None,
    fallback: str,
) -> str:
    candidate = requested or Path(source_filename).stem or fallback
    candidate = re.split(r"[/\\]", candidate)[-1].strip()
    candidate = re.sub(
        r"\.(?:mid|midi|musicxml|xml|mxl|pdf|wav|mp3|flac|m4a|aac|ogg|opus|mp4|mkv|mov|webm)$",
        "",
        candidate,
        flags=re.IGNORECASE,
    )
    candidate = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip(" .")
    candidate = candidate[:100].rstrip(" .")
    return candidate or fallback


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
