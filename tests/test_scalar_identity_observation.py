import pytest
import lancedb
import auto_anythingllm_pipeline as pipeline

pytestmark = pytest.mark.offline_deterministic


def fixture_table(tmp_path, monkeypatch, identities):
    storage = tmp_path / "storage"
    storage.mkdir()
    connect = lancedb.connect
    db = connect(str(storage / "lancedb"))
    table = db.create_table(
        "selected",
        data=[
            {
                "id": str(i),
                "vector": [0.1, 0.2],
                "chunkSource": identity,
                "docSource": "local-pdf://sha256/source",
                "text": "sourceDocument: test\n[[page:1]] text",
            }
            for i, identity in enumerate(identities)
        ],
    )
    queries = []

    class Query:
        def __init__(self, query):
            self.query = query
            self.details = {}
            queries.append(self.details)

        def where(self, *args, **kwargs):
            self.query = self.query.where(*args, **kwargs)
            return self

        def select(self, fields):
            self.details["columns"] = fields
            self.query = self.query.select(fields)
            return self

        def limit(self, limit):
            self.details["limit"] = limit
            self.query = self.query.limit(limit)
            return self

        def to_pandas(self):
            return self.query.to_pandas()

    class Table:
        schema = table.schema

        def search(self):
            return Query(table.search())

        def count_rows(self, **kwargs):
            raise AssertionError(
                "a second count scan is redundant for known identities"
            )

    class Database:
        def table_names(self):
            return ["selected"]

        def open_table(self, name):
            assert name == "selected"
            return Table()

    monkeypatch.setattr(lancedb, "connect", lambda *_a, **_k: Database())
    return storage, queries


def test_duplicate_plus_missing_identity_cannot_pass_equal_counts(
    tmp_path, monkeypatch
):
    storage, queries = fixture_table(tmp_path, monkeypatch, ["a", "a", "c", "other"])
    result = pipeline.inspect_native_metadata_count(
        storage, "source", "selected", ["a", "b", "c"]
    )
    assert result["matching_rows"] == 3
    assert result["identity_set_checked"]
    assert result["identity_set_complete"] is False
    assert result["duplicate_chunk_source_count"] == 1
    assert result["missing_chunk_sources"] == ["b"]
    assert queries == [
        {"columns": ["chunkSource"], "limit": None},
        {"columns": ["text"], "limit": 1},
    ]


@pytest.mark.parametrize("mode,checked", [("on_completion", False), ("always", True)])
def test_partial_mapping_keeps_existing_completion_policy(
    tmp_path, monkeypatch, mode, checked
):
    storage, queries = fixture_table(tmp_path, monkeypatch, ["a", "unselected"])
    result = pipeline.inspect_native_metadata_count(
        storage, "source", "selected", ["a", "b"], identity_set_mode=mode
    )
    assert result["matching_rows"] == 1
    assert result["identity_set_checked"] is checked
    assert result["identity_set_complete"] is (False if checked else None)
    assert all("vector" not in q["columns"] for q in queries)


def test_complete_exact_set_still_requires_all_requested_identities(
    tmp_path, monkeypatch
):
    storage, queries = fixture_table(tmp_path, monkeypatch, ["a", "b", "unselected"])
    result = pipeline.inspect_native_metadata_count(
        storage, "source", "selected", ["a", "b"]
    )
    assert result["matching_rows"] == 2
    assert result["identity_set_complete"] is True
    assert result["observed_chunk_sources"] == ["a", "b"]
    assert len(queries) == 2
