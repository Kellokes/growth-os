import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith("postgresql"),
    reason="defina TEST_DATABASE_URL com um Postgres para rodar estes testes",
)


def test_new_rows_after_migration_do_not_collide(logged):
    """Sequências de id funcionam: criar várias conversas seguidas não repete chave."""
    ids = [logged.post("/api/conversations").json()["id"] for _ in range(3)]
    assert len(set(ids)) == 3
