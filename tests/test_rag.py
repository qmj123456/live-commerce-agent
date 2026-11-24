from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from live_agent.knowledge import Knowledge
from live_agent.settings import Settings


@pytest.fixture
def kb(tmp_path):
    entries = [{"id": "size", "product_id": "one", "intent": "size", "question": "商品尺码怎么选择",
        "keywords": ["尺码"], "answer": "参照商品尺码表。", "source": "审核资料",
        "valid_until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}]
    (tmp_path / "faq.yaml").write_text(yaml.safe_dump({"entries": entries}, allow_unicode=True), encoding="utf-8")
    return Knowledge(tmp_path)


async def test_fts_recall_product_boundary(kb):
    assert (await kb.retrieve("商品怎么选择", "one"))[0].id == "size"
    assert not await kb.retrieve("商品怎么选择", "two")


async def test_semantic_recall_and_stale_fingerprint(kb):
    kb.settings = Settings(embedding_model="test", embedding_dimensions=2, _env_file=None)
    kb.embeddings = AsyncMock()
    kb.embeddings.aembed_documents.return_value = [[1.0, 0.0]]
    kb.embeddings.aembed_query.return_value = [1.0, 0.0]
    await kb.build_vectors()
    assert (await kb.retrieve("穿多大合适", "one"))[0].id == "size"
    kb.facts[0].answer = "更新后的审核答案"
    assert not await kb.retrieve("穿多大合适", "one")


def test_embedding_uses_separate_credentials(monkeypatch, tmp_path):
    constructor = MagicMock()
    monkeypatch.setattr("langchain_openai.OpenAIEmbeddings", constructor)
    settings = Settings(_env_file=None, api_key="chat-dummy", base_url="https://chat.example",
                        embedding_model="embedding-test", embedding_api_key="embedding-dummy",
                        embedding_base_url="https://embedding.example", embedding_dimensions=512,
                        vector_db_path=tmp_path / "vectors.db")
    Knowledge(tmp_path, settings=settings)
    kwargs = constructor.call_args.kwargs
    assert kwargs["api_key"] == "embedding-dummy"
    assert kwargs["base_url"] == "https://embedding.example"
    assert kwargs["dimensions"] == 512
    assert kwargs["check_embedding_ctx_length"] is False


def test_chat_key_is_never_used_as_embedding_key(tmp_path):
    settings = Settings(_env_file=None, api_key="chat-dummy", embedding_model="embedding-test",
                        embedding_base_url="https://embedding.example", vector_db_path=tmp_path / "vectors.db")
    assert Knowledge(tmp_path, settings=settings).embeddings is None
