"""
End-to-end evaluation test for the extraction + normalization pipeline.

Requires a running LLM backend. Skipped by default — run explicitly with:
    python -m pytest tests/matching_pipeline/metadata_extraction_normalization
    python -m pytest -q tests/matching_pipeline/metadata_extraction_normalization -m llm

Or as a plain script (original behaviour):
    python tests/matching_pipeline/metadata_extraction_normalization/test_extraction_normalization.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent

try:
    from matching_pipeline.shared.env import get_model_config
    from matching_pipeline.metadata_extraction.qwen_extract_information import extract_metadata
    from matching_pipeline.metadata_normalization.artist_normalization.run_artist_normalization import run_artist_normalization
    from matching_pipeline.metadata_normalization.dating_normalization.run_dating_normalization import run_dating_normalization
    from matching_pipeline.metadata_normalization.dimension_normalization.run_dimension_normalization import run_dimension_normalization
    BACKEND = get_model_config().backend
    _IMPORT_ERROR = None
except Exception as _exc:
    _IMPORT_ERROR = _exc
    BACKEND = "unavailable"

pytestmark = pytest.mark.llm

if _IMPORT_ERROR is not None:
    pytest.skip(
        f"LLM backend unavailable ({_IMPORT_ERROR}); run with -m llm on a machine with all deps",
        allow_module_level=True,
    )

DESCRIPTIONS_FILE = _ROOT / "test_descriptions.jsonl"
UNMATCHED_FILE = _ROOT / "test_descriptions_dating_unmatched.jsonl"

TEST_CASES = [
    # ── Clean cases ──────────────────────────────────────────────────────────
    {
        "id": "test_001",
        "description": (
            "Max Liebermann (1847 Berlin - 1935 Berlin). Badende Knaben, 1898. "
            "Öl auf Leinwand. 85 x 120 cm. Signiert und datiert unten rechts. "
            "Provenienz: Privatsammlung Berlin. Lit.: Eberle 1898/14."
        ),
        "expected": {
            "title": "Badende Knaben",
            "author": "Max Liebermann",
            "dating": "1898",
            "dating_start": 1898,
            "dating_end": 1898,
            "material": "Leinwand",
            "technique": "Öl",
            "dimensions": "85 x 120 cm",
            "dim_width": 120.0,
            "dim_height": 85.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_002",
        "description": (
            "Lovis Corinth (1858 Tapiau - 1925 Zandvoort). Selbstporträt mit Palette, um 1910. "
            "Öl auf Lwd. 75,5 x 60 cm (Rahmen: 95 x 79 cm). Verso bezeichnet. "
            "Zustand: Minimale Retuschen, gut erhalten."
        ),
        "expected": {
            "title": "Selbstporträt mit Palette",
            "author": "Lovis Corinth",
            "dating": "um 1910",
            "dating_start": 1905,
            "dating_end": 1915,
            "material": "Lwd.",
            "technique": "Öl",
            "dimensions": "75,5 x 60 cm (Rahmen: 95 x 79 cm)",
            "dim_width": 60.0,
            "dim_height": 75.5,
            "dim_width_frame": 79.0,
            "dim_height_frame": 95.0,
        },
    },
    {
        "id": "test_003",
        "description": (
            "Follower of Rembrandt van Rijn. Alter Mann mit Turban, 17. Jh. "
            "Öl auf Holz. Dm. 28 cm. Rahmen mit kleinen Fehlstellen."
        ),
        "expected": {
            "title": "Alter Mann mit Turban",
            "author": "Follower of Rembrandt van Rijn",
            "dating": "17. Jh.",
            "dating_start": 1600,
            "dating_end": 1700,
            "material": "Holz",
            "technique": "Öl",
            "dimensions": "Dm. 28 cm",
            "dim_width": 28.0,
            "dim_height": 28.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_004",
        "description": (
            "Ernst Ludwig Kirchner. Potsdamer Platz, erste Hälfte 20. Jh. "
            "Aquarell und Bleistift auf Papier. H.: 48 cm, B.: 36 cm. Monogrammiert unten links."
        ),
        "expected": {
            "title": "Potsdamer Platz",
            "author": "Ernst Ludwig Kirchner",
            "dating": "erste Hälfte 20. Jh.",
            "dating_start": 1900,
            "dating_end": 1950,
            "material": "Papier",
            "technique": "Aquarell und Bleistift",
            "dimensions": "H.: 48 cm, B.: 36 cm",
            "dim_width": 36.0,
            "dim_height": 48.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_005",
        "description": (
            "Attributed to Jan van Goyen (1596 Leiden - 1656 Den Haag). Flusslandschaft, dat. 1645. "
            "Öl auf Eichenholz. 38,5 x 55,2 cm. "
            "Provenienz: Auktion Christie's London, 12. Nov. 1988, Lot 42."
        ),
        "expected": {
            "title": "Flusslandschaft",
            "author": "Jan van Goyen",
            "dating": "dat. 1645",
            "dating_start": 1645,
            "dating_end": 1645,
            "material": "Eichenholz",
            "technique": "Öl",
            "dimensions": "38,5 x 55,2 cm",
            "dim_width": 55.2,
            "dim_height": 38.5,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_006",
        "description": (
            "Wohl Emil Nolde (1867-1956). Mohnblumen, 1920er Jahre. "
            "Aquarell auf Japanpapier. 34 x 45 cm. Nicht signiert. "
            "Rahmen: 52 x 63 cm."
        ),
        "expected": {
            "title": "Mohnblumen",
            "author": "Emil Nolde",
            "dating": "1920er Jahre",
            "dating_start": 1920,
            "dating_end": 1929,
            "material": "Japanpapier",
            "technique": "Aquarell",
            "dimensions": "34 x 45 cm; Rahmen: 52 x 63 cm",
            "dim_width": 45.0,
            "dim_height": 34.0,
            "dim_width_frame": 63.0,
            "dim_height_frame": 52.0,
        },
    },
    {
        "id": "test_007",
        "description": (
            "Käthe Kollwitz (1867 Königsberg - 1945 Moritzburg). Mutter mit Kind, 1903. "
            "Radierung, 3. Zustand. Blattgröße: 58 x 42 cm, Platte: 48 x 35 cm. "
            "Signiert bleistift unten rechts. "
            "Lit.: Klipstein 32 III."
        ),
        "expected": {
            "title": "Mutter mit Kind",
            "author": "Käthe Kollwitz",
            "dating": "1903",
            "dating_start": 1903,
            "dating_end": 1903,
            "technique": "Radierung",
            "dimensions": "Blattgröße: 58 x 42 cm, Platte: 48 x 35 cm",
            "dim_width": 42.0,
            "dim_height": 58.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_008",
        "description": (
            "Unbekannter Künstler, süddeutsch, 2. Hälfte 19. Jahrhundert. "
            "Gebirgslandschaft mit Hirsch. Öl auf Karton. Höhe: 22 cm; Breite: 30 cm. Unleserlich signiert."
        ),
        "expected": {
            "title": "Gebirgslandschaft mit Hirsch",
            "author": "",
            "dating": "2. Hälfte 19. Jahrhundert",
            "dating_start": 1850,
            "dating_end": 1900,
            "material": "Karton",
            "technique": "Öl",
            "dimensions": "Höhe: 22 cm; Breite: 30 cm",
            "dim_width": 30.0,
            "dim_height": 22.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_009",
        "description": (
            "Franz Marc (1880 München - 1916 Verdun). Blaue Pferde, 1911-1912. "
            "Mischtechnik auf Papier. 29,8 x 41,3 cm. "
            "Signiert mit Initialen F.M. unten links. "
            "Provenienz: Nachlass des Künstlers; Galerie Thannhauser, München 1925."
        ),
        "expected": {
            "title": "Blaue Pferde",
            "author": "Franz Marc",
            "dating": "1911-1912",
            "dating_start": 1911,
            "dating_end": 1912,
            "material": "Papier",
            "technique": "Mischtechnik",
            "dimensions": "29,8 x 41,3 cm",
            "dim_width": 41.3,
            "dim_height": 29.8,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_010",
        "description": (
            "Circle of Peter Paul Rubens. Heilige Familie, ca. 1620-1640. "
            "Öl auf Leinwand, doubliert. 110 x 84 cm (mit Rahmen: 132 x 106 cm). "
            "Zustand: Altersgemäße Craquelure, stellenweise Übermalungen."
        ),
        "expected": {
            "title": "Heilige Familie",
            "author": "Circle of Peter Paul Rubens",
            "dating": "ca. 1620-1640",
            "dating_start": 1620,
            "dating_end": 1640,
            "material": "Leinwand",
            "technique": "Öl",
            "dimensions": "110 x 84 cm (mit Rahmen: 132 x 106 cm)",
            "dim_width": 84.0,
            "dim_height": 110.0,
            "dim_width_frame": 106.0,
            "dim_height_frame": 132.0,
        },
    },
    # ── Hard cases ───────────────────────────────────────────────────────────
    {
        "id": "test_011",
        "description": (
            "BAISCH, Hermann (1846 Groß-Umstadt – 1894 Karlsruhe). "
            "Weidelandschaft mit Kühen. Öl/Lwd. 30:45. Sign. u.r."
        ),
        "expected": {
            "title": "Weidelandschaft mit Kühen",
            "author": "Hermann Baisch",
            "dating": None,
            "material": "Lwd.",
            "technique": "Öl",
            "dimensions": "30:45",
            "dim_width": 45.0,
            "dim_height": 30.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_013",
        "description": (
            "Unbekannt, deutsch. Stadtansicht. Ende 19./Anfang 20. Jh. "
            "Öl auf Karton. 18 × 26 cm. Nicht signiert."
        ),
        "expected": {
            "title": "Stadtansicht",
            "dating": "Ende 19./Anfang 20. Jh.",
            "dating_start": 1875,
            "dating_end": 1925,
            "material": "Karton",
            "technique": "Öl",
            "dimensions": "18 × 26 cm",
            "dim_width": 26.0,
            "dim_height": 18.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_014",
        "description": (
            "Max Beckmann (1884 Leipzig – 1950 New York). Selbstbildnis, 1922. "
            "Kaltnadel. Blatt: 50 × 38 cm; Platte: 30 × 22 cm. "
            "Signiert und datiert bleistift. Aufl. 30. Lit.: Hofmaier 189."
        ),
        "expected": {
            "title": "Selbstbildnis",
            "author": "Max Beckmann",
            "dating": "1922",
            "dating_start": 1922,
            "dating_end": 1922,
            "technique": "Kaltnadel",
            "dimensions": "Blatt: 50 × 38 cm; Platte: 30 × 22 cm",
            "dim_width": 38.0,
            "dim_height": 50.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_015",
        "description": (
            "Umkreis Lucas Cranach d.Ä. (1472–1553). "
            "Madonna mit Kind, 1. Hälfte 16. Jahrhundert. "
            "Temperafarben und Gold auf Holz. 38 × 28 cm. "
            "Rückseitig altes Etikett."
        ),
        "expected": {
            "title": "Madonna mit Kind",
            "author": "Lucas Cranach d.Ä.",
            "dating": "1. Hälfte 16. Jahrhundert",
            "dating_start": 1500,
            "dating_end": 1550,
            "material": "Holz",
            "technique": "Temperafarben und Gold",
            "dimensions": "38 × 28 cm",
            "dim_width": 28.0,
            "dim_height": 38.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_016",
        "description": (
            "Unbekannt, deutsch. Winterlandschaft mit Dorf. Öl auf Leinwand. "
            "nach 1880. 30 × 40 cm, o. R. Nicht signiert."
        ),
        "expected": {
            "title": "Winterlandschaft mit Dorf",
            "author": "",
            "dating": "nach 1880",
            "dating_start": 1880,
            "dating_end": None,
            "material": "Leinwand",
            "technique": "Öl",
            "dimensions": "30 × 40 cm, o. R.",
            "dim_width": 40.0,
            "dim_height": 30.0,
            "dim_width_frame": None,
            "dim_height_frame": None,
        },
    },
    {
        "id": "test_017",
        "description": (
            "Unbekannt. Blumenstillleben. Aquarell auf Papier. 35 × 48 cm. "
            "Hinter Glas gerahmt, Rahmen 50 × 63 cm."
        ),
        "expected": {
            "title": "Blumenstillleben",
            "dating": None,
            "material": "Papier",
            "technique": "Aquarell",
            "dimensions": "35 × 48 cm; Rahmen 50 × 63 cm",
            "dim_width": 48.0,
            "dim_height": 35.0,
            "dim_width_frame": 63.0,
            "dim_height_frame": 50.0,
        },
    },
]

_SKELETON = {
    "title": "",
    "author": "",
    "date_of_birth": "",
    "place_of_birth": "",
    "date_of_death": "",
    "place_of_death": "",
    "dimensions": "",
    "dating": "",
    "material": "",
    "technique": "",
    "provenance": "",
    "signature": "",
    "condition": "",
    "literature": "",
    "dating_start": None,
    "dating_end": None,
}


def _write_input_jsonl() -> None:
    with open(DESCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        for case in TEST_CASES:
            record = {"id": case["id"], "description": case["description"], **_SKELETON}
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _field_correct(actual, expected_val) -> bool:
    if expected_val is None or expected_val == "":
        return actual is None or str(actual).strip() == ""
    if isinstance(expected_val, str):
        if not actual:
            return False
        return str(actual).strip().lower() == expected_val.strip().lower()
    try:
        return abs(float(actual) - float(expected_val)) < 0.01
    except (TypeError, ValueError):
        return False


def _run_pipeline() -> dict[str, dict]:
    _write_input_jsonl()
    extract_metadata(DESCRIPTIONS_FILE, backend=BACKEND)
    run_dating_normalization(DESCRIPTIONS_FILE, UNMATCHED_FILE, backend=BACKEND)
    run_artist_normalization(DESCRIPTIONS_FILE)
    run_dimension_normalization(DESCRIPTIONS_FILE, backend=BACKEND)

    with open(DESCRIPTIONS_FILE, encoding="utf-8") as f:
        return {
            json.loads(line)["id"]: json.loads(line)
            for line in f
            if line.strip()
        }


@pytest.mark.llm
def test_pipeline_overall_score():
    records = _run_pipeline()
    total_fields = correct_fields = 0

    for case in TEST_CASES:
        rec = records.get(case["id"], {})
        for field, exp_val in case["expected"].items():
            total_fields += 1
            if _field_correct(rec.get(field), exp_val):
                correct_fields += 1

    pct = 100 * correct_fields / total_fields if total_fields else 0
    assert pct >= 80, f"Pipeline score too low: {correct_fields}/{total_fields} ({pct:.0f}%)"


@pytest.mark.llm
@pytest.mark.parametrize("case", TEST_CASES, ids=[c["id"] for c in TEST_CASES])
def test_pipeline_per_record(case):
    records = _run_pipeline()
    rec = records.get(case["id"], {})
    failures = []
    for field, exp_val in case["expected"].items():
        if not _field_correct(rec.get(field), exp_val):
            failures.append(f"{field}: got={rec.get(field)!r}  exp={exp_val!r}")
    assert not failures, "\n".join(failures)


# ── Original script entry point ──────────────────────────────────────────────


def _print_results(records: dict[str, dict]) -> None:
    total_fields = correct_fields = fully_correct = 0

    print(f"\n{'=' * 70}")
    print(f"EVALUATION ({len(TEST_CASES)} records)")
    print("=" * 70)

    for case in TEST_CASES:
        rec = records.get(case["id"], {})
        expected = case["expected"]
        rec_correct = sum(_field_correct(rec.get(f), v) for f, v in expected.items())
        rec_total = len(expected)
        total_fields += rec_total
        correct_fields += rec_correct
        if rec_correct == rec_total:
            fully_correct += 1

        print(f"\n--- {case['id']} --- [{rec_correct}/{rec_total}]")
        for field, exp_val in expected.items():
            actual = rec.get(field)
            status = "OK  " if _field_correct(actual, exp_val) else "FAIL"
            print(f"  {status}  {field:<15} got={str(actual):<35} exp={exp_val}")

    pct = 100 * correct_fields / total_fields if total_fields else 0
    print(f"\n{'=' * 70}")
    print(f"SCORE: {correct_fields}/{total_fields} fields  ({pct:.0f}%)")
    print(f"       {fully_correct}/{len(TEST_CASES)} records fully correct")
    print("=" * 70)


if __name__ == "__main__":
    _print_results(_run_pipeline())
