import asyncio
import base64
from xml.etree import ElementTree as ET

import httpx

import app.main as main_module
from app import __version__
from app.core.media_transcription import MediaTranscriptionResult
from app.main import app

from .midi_factory import piano_midi_bytes


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, url, **kwargs)


def request(method: str, url: str, **kwargs) -> httpx.Response:
    return asyncio.run(_request(method, url, **kwargs))


def test_health() -> None:
    response = request("GET", "/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == __version__
    assert "engraver" in response.json()
    assert "transcriber" in response.json()


def test_convert_endpoint() -> None:
    response = request(
        "POST",
        "/api/convert",
        files={"file": ("piece.mid", piano_midi_bytes(), "audio/midi")},
        data={
            "options_json": (
                '{"style":"clean","engraving_style":"modern",'
                '"allow_triplets":false}'
            )
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["filename"] == "piece.musicxml"
    assert payload["musicxml"].startswith("<?xml")
    assert payload["analysis"]["hands"]["method"] == "tracks"
    assert payload["analysis"]["quality"]["note_count_preserved"] is True
    if payload["pdf_base64"]:
        assert base64.b64decode(payload["pdf_base64"]).startswith(b"%PDF")
        assert payload["analysis"]["engraving"]["a4"] is True
        assert payload["analysis"]["engraving"]["style"] == "modern"
    if payload["preview_png_base64"]:
        preview_pages = payload["preview_pngs_base64"]
        assert len(preview_pages) == payload["analysis"]["engraving"]["page_count"]
        assert preview_pages[0] == payload["preview_png_base64"]
        assert all(
            base64.b64decode(page).startswith(b"\x89PNG")
            for page in preview_pages
        )


def test_convert_endpoint_applies_custom_metadata_and_output_filename() -> None:
    response = request(
        "POST",
        "/api/convert",
        files={"file": ("source.mid", piano_midi_bytes(), "audio/midi")},
        data={
            "options_json": (
                '{"title":"Nocturne in C minor","author":"F. Chopin",'
                '"output_filename":"exports/final:score.pdf","allow_triplets":false}'
            )
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    root = ET.fromstring(payload["musicxml"])

    assert payload["filename"] == "final_score.musicxml"
    if payload["pdf_base64"]:
        assert payload["pdf_filename"] == "final_score-A4.pdf"
    assert payload["analysis"]["title"] == "Nocturne in C minor"
    assert payload["analysis"]["author"] == "F. Chopin"
    assert payload["analysis"]["output_filename"] == "final_score"
    assert root.findtext("./work/work-title") == "Nocturne in C minor"
    assert root.findtext("./identification/creator[@type='composer']") == "F. Chopin"
    assert root.findtext("./credit[credit-type='composer']/credit-words") == "F. Chopin"


def test_rejects_wrong_extension() -> None:
    response = request(
        "POST",
        "/api/convert",
        files={"file": ("piece.txt", b"not midi", "text/plain")},
    )
    assert response.status_code == 415


def test_convert_media_endpoint_returns_intermediate_midi(monkeypatch) -> None:
    midi = piano_midi_bytes(two_tracks=True, jitter=0, measures=1)
    monkeypatch.setattr(
        main_module,
        "transcribe_media",
        lambda *_args, **_kwargs: MediaTranscriptionResult(
            midi_bytes=midi,
            raw_midi_bytes=midi,
            analysis={"backend": "transkun", "beat_alignment": True},
            warnings=[],
        ),
    )

    response = request(
        "POST",
        "/api/convert-media",
        files={"file": ("recording.wav", b"synthetic audio", "audio/wav")},
        data={"options_json": '{"style":"clean"}'},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["midi_filename"] == "recording-transcribed.mid"
    assert base64.b64decode(payload["midi_base64"]).startswith(b"MThd")
    assert payload["analysis"]["transcription"]["backend"] == "transkun"
    assert "<pedal " not in payload["musicxml"]


def test_demo_endpoint() -> None:
    response = request("POST", "/api/demo", json={"style": "balanced"})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["analysis"]["note_count"] == 16
    assert payload["musicxml"].count("<pedal ") == 2


def test_convert_endpoint_accepts_musicxml_upload() -> None:
    from app.core.pipeline import convert_midi

    musicxml, _, _ = convert_midi(piano_midi_bytes(), "piece.mid")
    response = request(
        "POST",
        "/api/convert",
        files={"file": ("piece.musicxml", musicxml.encode("utf-8"), "text/xml")},
        data={"options_json": "{}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["filename"] == "piece.musicxml"
    assert payload["analysis"]["source"]["format"] == "musicxml"
    assert payload["analysis"]["note_count"] > 0
    if payload["pdf_base64"]:
        assert base64.b64decode(payload["pdf_base64"]).startswith(b"%PDF")
    if payload["midi_base64"]:
        assert base64.b64decode(payload["midi_base64"]).startswith(b"MThd")


def test_musicxml_upload_can_override_metadata_and_download_name() -> None:
    from app.core.pipeline import convert_midi

    musicxml, _, _ = convert_midi(piano_midi_bytes(), "piece.mid")
    response = request(
        "POST",
        "/api/convert",
        files={"file": ("piece.musicxml", musicxml.encode("utf-8"), "text/xml")},
        data={
            "options_json": (
                '{"title":"Edited title","author":"Custom arranger",'
                '"output_filename":"custom-edition"}'
            )
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    root = ET.fromstring(payload["musicxml"])

    assert payload["filename"] == "custom-edition.musicxml"
    assert payload["analysis"]["title"] == "Edited title"
    assert payload["analysis"]["author"] == "Custom arranger"
    assert root.findtext("./work/work-title") == "Edited title"
    assert root.findtext("./identification/creator[@type='composer']") == (
        "Custom arranger"
    )


def test_convert_endpoint_rejects_invalid_musicxml() -> None:
    response = request(
        "POST",
        "/api/convert",
        files={"file": ("piece.musicxml", b"not xml at all", "text/xml")},
        data={"options_json": "{}"},
    )

    assert response.status_code == 400


def test_convert_endpoint_accepts_pdf_upload(monkeypatch) -> None:
    from app.core.pipeline import convert_midi
    from app.core.score_omr import ScoreOmrResult

    musicxml, _, _ = convert_midi(piano_midi_bytes(), "piece.mid")
    monkeypatch.setattr(
        main_module,
        "transcribe_score_pdf",
        lambda *_args, **_kwargs: ScoreOmrResult(
            musicxml=musicxml,
            analysis={"engine": "Audiveris"},
        ),
    )

    response = request(
        "POST",
        "/api/convert",
        files={"file": ("piece.pdf", b"%PDF-1.7 fake", "application/pdf")},
        data={"options_json": "{}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["filename"] == "piece.musicxml"
    assert payload["analysis"]["source"]["format"] == "pdf"
    assert payload["analysis"]["omr"]["engine"] == "Audiveris"
    if payload["midi_base64"]:
        assert base64.b64decode(payload["midi_base64"]).startswith(b"MThd")


def test_pdf_upload_rebuilds_structurally_corrupt_omr(monkeypatch) -> None:
    from app.core.score_omr import ScoreOmrResult

    musicxml = """<score-partwise version="4.0">
      <part-list><score-part id="P1"><part-name>Piano</part-name></score-part></part-list>
      <part id="P1"><measure number="1">
        <attributes><divisions>1</divisions><time><beats>4</beats><beat-type>4</beat-type></time></attributes>
        <note><pitch><step>C</step><octave>4</octave></pitch><duration>4</duration><voice>1</voice><staff>1</staff></note>
        <barline location="right"><ending number="1" type="start"/></barline>
      </measure></part>
    </score-partwise>"""
    monkeypatch.setattr(
        main_module,
        "transcribe_score_pdf",
        lambda *_args, **_kwargs: ScoreOmrResult(
            musicxml=musicxml,
            analysis={"engine": "Audiveris"},
        ),
    )

    response = request(
        "POST",
        "/api/convert",
        files={"file": ("broken.pdf", b"%PDF-1.7 fake", "application/pdf")},
        data={"options_json": "{}"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["analysis"]["omr"]["removed_orphan_endings"] == 1
    assert payload["analysis"]["omr"]["semantic_rebuilt"] is True
    assert payload["analysis"]["source"]["format"] == "pdf"
    assert base64.b64decode(payload["midi_base64"]).startswith(b"MThd")


def test_convert_endpoint_pdf_without_omr_engine(monkeypatch) -> None:
    from app.core.score_omr import ScoreOmrUnavailableError

    def _unavailable(*_args, **_kwargs):
        raise ScoreOmrUnavailableError("未找到 Audiveris OMR 引擎")

    monkeypatch.setattr(main_module, "transcribe_score_pdf", _unavailable)

    response = request(
        "POST",
        "/api/convert",
        files={"file": ("piece.pdf", b"%PDF-1.7 fake", "application/pdf")},
        data={"options_json": "{}"},
    )

    assert response.status_code == 503
