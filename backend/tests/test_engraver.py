from xml.etree import ElementTree as ET

from pypdf import PdfWriter

from app.core.engraver import (
    _layout_balance_score,
    _low_density_singleton_measures,
    _needs_page_rebalance,
    _read_render_outputs,
    _with_rebalanced_singletons,
)


def _musicxml(note_counts: dict[int, int], breaks: set[int]) -> str:
    root = ET.Element("score-partwise", version="4.0")
    part = ET.SubElement(root, "part", id="P1")
    for number in sorted(note_counts):
        measure = ET.SubElement(part, "measure", number=str(number))
        if number in breaks:
            ET.SubElement(measure, "print", {"new-system": "yes"})
        for _ in range(note_counts[number]):
            note = ET.SubElement(measure, "note")
            pitch = ET.SubElement(note, "pitch")
            ET.SubElement(pitch, "step").text = "C"
            ET.SubElement(pitch, "octave").text = "4"
    return ET.tostring(root, encoding="unicode")


def _layout() -> dict[str, object]:
    return {
        "systems": [
            {"page": 1, "measures": [1, 2, 3]},
            {"page": 1, "measures": [4]},
            {"page": 1, "measures": [5, 6, 7]},
        ]
    }


def test_rebalances_a_low_density_three_one_three_pattern() -> None:
    xml = _musicxml(
        {1: 8, 2: 8, 3: 2, 4: 5, 5: 15, 6: 8, 7: 8},
        breaks={4, 5},
    )

    assert _low_density_singleton_measures(_layout(), xml) == [4]

    updated = ET.fromstring(_with_rebalanced_singletons(xml, _layout()))
    breaks = {
        int(measure.get("number", "0"))
        for measure in updated.findall("./part/measure")
        if (print_element := measure.find("print")) is not None
        and print_element.get("new-system") == "yes"
    }

    assert breaks == {3, 5}


def test_keeps_a_dense_single_measure_system() -> None:
    xml = _musicxml(
        {1: 8, 2: 8, 3: 8, 4: 32, 5: 8, 6: 8, 7: 8},
        breaks={4, 5},
    )

    assert _low_density_singleton_measures(_layout(), xml) == []
    assert _with_rebalanced_singletons(xml, _layout()) == xml


def test_rebalances_when_final_page_is_denser_than_penultimate() -> None:
    initial = {
        "systems": [
            *({"page": 1, "measures": [index]} for index in range(1, 6)),
            *({"page": 2, "measures": [index]} for index in range(6, 12)),
        ]
    }
    balanced = {
        "systems": [
            *({"page": 1, "measures": [index]} for index in range(1, 7)),
            *({"page": 2, "measures": [index]} for index in range(7, 12)),
        ]
    }

    assert _needs_page_rebalance(initial)
    assert _layout_balance_score(balanced) < _layout_balance_score(initial)


def test_read_render_outputs_keeps_every_preview_page_in_numeric_order(
    tmp_path,
) -> None:
    pdf_path = tmp_path / "score.pdf"
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=595, height=842)
    with pdf_path.open("wb") as stream:
        writer.write(stream)

    (tmp_path / "preview-10.png").write_bytes(b"page-10")
    (tmp_path / "preview-2.png").write_bytes(b"page-2")
    (tmp_path / "preview-1.png").write_bytes(b"page-1")

    _, first_preview, previews, preview_count, layout = _read_render_outputs(
        tmp_path,
        pdf_path,
        tmp_path / "score.mpos",
        tmp_path / "preview.png",
    )

    assert first_preview == b"page-1"
    assert previews == (b"page-1", b"page-2", b"page-10")
    assert preview_count == 3
    assert layout["page_count"] == 3
