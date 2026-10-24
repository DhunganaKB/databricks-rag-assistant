import pytest

from rag_app.retriever import hit_from_row, parse_boxes

from conftest import make_settings


def test_hit_from_vector_search_row_paged():
    # Vector Search returns numbers as strings and NULLs as None/"None"
    row = {
        "chunk_id": "c1",
        "doc_id": "d1",
        "title": "QBR",
        "section_path": "QBR > 2. Results",
        "path": "/V/x.pdf",
        "doc_format": "paged",
        "page_start": "1.0",
        "chunk_text": "Revenue 48.6",
        "locations_json": '[{"page": 1, "x0": 0.13, "y0": 0.3, "x1": 0.64, "y1": 0.45, "element_id": 5,'
        ' "estimated": false}, {"page": 2, "x0": 0.1, "y0": 0.1, "x1": 0.9, "y1": 0.2,'
        ' "estimated": true}]',
        "char_start": None,
        "char_end": "None",
        "score": "0.99",
    }
    hit = hit_from_row(row)
    assert hit.doc_format == "paged" and hit.pages == [1, 2] and hit.span is None
    assert hit.boxes[1].estimated is True and hit.score == pytest.approx(0.99)
    assert hit.label() == "QBR > 2. Results (page 1, 2)"


def test_hit_from_row_text_span():
    hit = hit_from_row(
        {
            "chunk_id": "c",
            "doc_id": "d",
            "title": "PTO",
            "doc_format": "text",
            "chunk_text": "t",
            "locations_json": "[]",
            "char_start": "10.0",
            "char_end": "50",
            "line_start": "3",
            "line_end": "7",
        }
    )
    assert hit.boxes == [] and hit.span.char_start == 10 and hit.span.line_end == 7


def test_parse_boxes_is_tolerant():
    assert parse_boxes(None) == [] and parse_boxes("not json") == []
    assert parse_boxes('[{"page": 1, "x0": null, "y0": 0, "x1": 1, "y1": 1}, {"page": "x"}]') == []


def test_settings_validate_identifiers():
    with pytest.raises(ValueError):
        make_settings(vs_index="cat.gold.idx; DROP TABLE x")
    with pytest.raises(ValueError):
        make_settings(page_image_root="/etc/")
    with pytest.raises(ValueError):
        make_settings(history_turns=50)


def test_volume_path_from_apps_binding_or_local_root():
    from rag_app.config import volume_path

    assert volume_path({"PAGE_IMAGE_VOLUME": "cat.silver.page_images"}) == "/Volumes/cat/silver/page_images/"
    assert volume_path({"PAGE_IMAGE_ROOT": "/Volumes/c/s/v"}) == "/Volumes/c/s/v/"
    with pytest.raises(ValueError):
        volume_path({"PAGE_IMAGE_VOLUME": "bad name; DROP"})
    with pytest.raises(ValueError):
        volume_path({})


def test_from_env_requires_environment_specific_values(monkeypatch):
    from rag_app.config import Settings

    for name in (
        "VS_INDEX",
        "CITATIONS_VIEW",
        "DOCUMENTS_VIEW",
        "SQL_WAREHOUSE_ID",
        "PAGE_IMAGE_ROOT",
        "PAGE_IMAGE_VOLUME",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="VS_INDEX"):
        Settings.from_env()


def test_postgres_accepts_pghost_from_databricks_apps(monkeypatch):
    monkeypatch.setenv("PGHOST", "instance.database.azuredatabricks.net")
    s = make_settings(store_backend="postgres", lakebase_instance="")
    assert s.store_backend == "postgres"
    monkeypatch.delenv("PGHOST")
    with pytest.raises(ValueError, match="LAKEBASE_INSTANCE"):
        make_settings(store_backend="postgres", lakebase_instance="")
