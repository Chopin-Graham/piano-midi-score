import time

import pytest

from app.core.options import ConversionOptions
from app.core.pipeline import convert_midi

from .midi_factory import dense_midi_bytes


@pytest.mark.performance
def test_converts_twelve_hundred_notes_within_budget() -> None:
    data = dense_midi_bytes(1200)
    started = time.perf_counter()
    xml, analysis, _ = convert_midi(data, "dense.mid", ConversionOptions(style="clean"))
    elapsed = time.perf_counter() - started

    assert analysis["note_count"] >= 1000
    assert len(xml) > 100_000
    assert elapsed < 5.0

