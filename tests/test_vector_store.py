from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from live_agent.knowledge import Fact, Knowledge
from live_agent.settings import Settings
from live_agent.vector_store import VectorStore


def fact(key, product="one", expired=False):
    return Fact(id=key, product_id=product, intent="size", question="尺码怎么选",
                keywords=["尺码"], answer="查阅已核验的尺码表。", source="审核资料",
                valid_until=datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1))


@pytest.fixture
def config():
    return {"model": "mock", "dimensions": 2, "endpoint": "https://embedding.example"}


def test_native_knn_filters_product_and_expiry_before_top_k(tmp_path, config):
    store = VectorStore(tmp_path / "vec.db", 2)
    facts = [fact("other", "two"), fact("expired", expired=True), fact("target"), fact("global", "*")]
    vectors = [[1., 0.], [1., 0.], [.9, .1], [.8, .2]]
    store.replace(facts, facts, vectors, config, "v1")
    assert store.search([1., 0.], "one", 2, .5, top_k=1) == ["target"]
    assert store.search([1., 0.], "unknown", 2, .5) == ["global"]
    store.close()


def test_persistent_restart_and_config_invalidation(tmp_path, config):
    path = tmp_path / "vec.db"
    store = VectorStore(path, 2)
    items = [fact("one")]
    store.replace(items, items, [[1., 0.]], config, "snapshot")
    store.close()
    reopened = VectorStore(path, 2)
    assert reopened.ready(config, "snapshot")
    assert reopened.search([1., 0.], "one", 2, .5) == ["one"]
    assert not reopened.ready({**config, "model": "different"}, "snapshot")
    assert not reopened.ready(config, "new-content")
    reopened.close()


def test_failed_rebuild_keeps_last_snapshot(tmp_path, config):
    store = VectorStore(tmp_path / "vec.db", 2)
    items = [fact("one")]
    store.replace(items, items, [[1., 0.]], config, "old")
    with pytest.raises(ValueError, match="维度"):
        store.replace(items, items, [[1., 0., 0.]], config, "new")
    assert store.ready(config, "old")
    assert store.search([1., 0.], "one", 2, .5) == ["one"]
    store.close()


def test_transaction_rolls_back_after_partial_write(tmp_path, config):
    store = VectorStore(tmp_path / "vec.db", 2)
    items = [fact("one")]
    store.replace(items, items, [[1., 0.]], config, "old")
    # Duplicate IDs trigger a second insertion with the same vec rowid only if input is invalid.
    store.db.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON documents BEGIN SELECT RAISE(ABORT,'test'); END")
    store.db.commit()
    with pytest.raises(Exception, match="test"):
        store.replace(items, items, [[0., 1.]], config, "new")
    assert store.ready(config, "old")
    assert store.search([1., 0.], "one", 2, .5) == ["one"]
    store.close()


def test_dimension_change_rebuilds_index(tmp_path, config):
    store = VectorStore(tmp_path / "vec.db", 2)
    items = [fact("one")]
    store.replace(items, items, [[1., 0.]], config, "old")
    updated = {**config, "dimensions": 3}
    assert store.changed(items, updated) == items
    store.replace(items, items, [[1., 0., 0.]], updated, "new")
    assert store.search([1., 0., 0.], "one", 3, .5) == ["one"]
    store.close()


async def test_incremental_build_update_and_delete(tmp_path):
    settings = Settings(_env_file=None, embedding_model="mock", embedding_dimensions=2,
                        vector_db_path=tmp_path / "vec.db")
    kb = Knowledge(tmp_path, settings=settings)
    kb.facts = [fact("one"), fact("two")]
    kb.embeddings = AsyncMock()
    kb.embeddings.aembed_documents.return_value = [[1., 0.], [0., 1.]]
    assert (await kb.build_vectors())["embedded"] == 2
    kb.embeddings.reset_mock()
    assert (await kb.build_vectors())["embedded"] == 0
    kb.embeddings.aembed_documents.assert_not_called()
    kb.facts[0].answer = "更新后的尺码说明。"
    kb.embeddings.aembed_documents.return_value = [[.9, .1]]
    assert (await kb.build_vectors())["embedded"] == 1
    kb.facts = [kb.facts[0]]
    result = await kb.build_vectors()
    assert result["embedded"] == 0 and result["vectors"] == 1
    kb.facts = []
    assert (await kb.build_vectors())["vectors"] == 0
    kb.close()


async def test_embedding_failure_does_not_modify_index(tmp_path):
    settings = Settings(_env_file=None, embedding_model="mock", embedding_dimensions=2,
                        vector_db_path=tmp_path / "vec.db")
    kb = Knowledge(tmp_path, settings=settings)
    kb.facts = [fact("one")]
    kb.embeddings = AsyncMock()
    kb.embeddings.aembed_documents.return_value = [[1., 0.]]
    await kb.build_vectors()
    old_fingerprint = kb.fingerprint()
    kb.facts.append(fact("two"))
    kb.embeddings.aembed_documents.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await kb.build_vectors()
    assert kb.vectors.ready(kb.vector_config(), old_fingerprint)
    assert kb.vectors.status()["vectors"] == 1
    kb.close()
