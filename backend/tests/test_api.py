import asyncio
import base64

import httpx

import app.main as main_module
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


def test_convert_endpoint_rejects_invalid_musicxml() -> None:
    response = request(
        "POST",
        "/api/convert",
        files={"file": ("piece.musicxml", b"not xml at all", "text/xml")},
        data={"options_json": "{}"},
    )

    assert response.status_code == 400
