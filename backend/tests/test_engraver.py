from xml.etree import ElementTree as ET

from app.core.engraver import (
    _layout_balance_score,
    _low_density_singleton_measures,
    _needs_page_rebalance,
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
