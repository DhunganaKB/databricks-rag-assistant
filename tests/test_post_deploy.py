import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from post_deploy import quote_ident, uc_grant_statements  # noqa: E402


def test_uc_grant_statements_cover_catalog_schemas_tables_and_volume():
    stmts = uc_grant_statements(
        "0d8e7a1c-1111-2222-3333-444455556666", "steampunk.dev_rag_gold", "steampunk.dev_rag_silver.page_images"
    )
    p = "`0d8e7a1c-1111-2222-3333-444455556666`"
    assert stmts == [
        f"GRANT USE CATALOG ON CATALOG `steampunk` TO {p}",
        f"GRANT USE SCHEMA ON SCHEMA `steampunk`.`dev_rag_gold` TO {p}",
        f"GRANT SELECT ON TABLE `steampunk`.`dev_rag_gold`.`chunks_index` TO {p}",
        f"GRANT SELECT ON TABLE `steampunk`.`dev_rag_gold`.`citation_locations` TO {p}",
        f"GRANT SELECT ON TABLE `steampunk`.`dev_rag_gold`.`rag_documents` TO {p}",
        f"GRANT USE SCHEMA ON SCHEMA `steampunk`.`dev_rag_silver` TO {p}",
        f"GRANT READ VOLUME ON VOLUME `steampunk`.`dev_rag_silver`.`page_images` TO {p}",
    ]  # the duplicate USE CATALOG for the volume's catalog is removed


def test_uc_grant_statements_reject_injection():
    with pytest.raises(ValueError):
        uc_grant_statements("sp", "steampunk.gold; DROP TABLE x", "a.b.c")
    with pytest.raises(ValueError):
        uc_grant_statements("sp` TO `admins", "a.b", "a.b.c")


def test_quote_ident_escapes_quotes():
    assert quote_ident('a"b') == '"a""b"'
