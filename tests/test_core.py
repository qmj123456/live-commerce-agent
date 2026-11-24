import asyncio
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from live_agent.agent import Agent
from live_agent.executor import Executor
from live_agent.knowledge import Fact, Knowledge, is_question
from live_agent.main import create_app
from live_agent.models import Candidate, Comment, Decision
from live_agent.settings import Settings
from live_agent.store import Store


@pytest.fixture
def system(tmp_path):
    settings = Settings(db_path=tmp_path / "test.db", _env_file=None)
    store = Store(settings.db_path)
    kb = Knowledge()
    kb.facts = [Fact(id="shipping", product_id="sku1", intent="shipping", question="什么时候发货",
        keywords=["发货"], answer="按商家确认的发货规则处理。", source="已审批规则",
        valid_until=datetime.now(timezone.utc) + timedelta(days=1))]
    browser = AsyncMock()
    browser.lock = asyncio.Lock()
    browser.product.return_value = {"id": "sku1", "fields": {}}
    browser.visible.return_value = True
    browser.send.return_value = "confirmed"
    executor = Executor(settings, browser, store, kb)
    executor.paused = False
    yield settings, store, kb, browser, executor
    store.close()


def make_candidate(store):
    candidate = Candidate(id="candidate1", comment=Comment(id="row1", user="问询 用户", text="什么时候发货", kind="问询"),
        product_id="sku1", decision=Decision(action="reply", text="按商家确认的发货规则处理。", evidence_ids=["shipping"]))
    store.save(candidate)
    return candidate


async def test_graph_fast_path_and_tools(system):
    settings, store, kb, browser, executor = system
    agent = Agent(settings, browser, kb, executor)
    candidate = make_candidate(store)
    result = await agent.graph.ainvoke({"comment": candidate.comment.model_dump()})
    assert result["decision"]["action"] == "reply"
    assert "send_reply" in agent.tools and "read_live_comments" in agent.tools
    browser.send.assert_not_called()


async def test_shadow_forbids_send(system):
    _, store, _, browser, executor = system
    make_candidate(store)
    with pytest.raises(RuntimeError, match="shadow"):
        await executor.reply("candidate1")
    browser.send.assert_not_called()


async def test_assist_requires_approval(system):
    settings, store, _, browser, executor = system
    settings.mode = "assist"
    make_candidate(store)
    with pytest.raises(RuntimeError, match="人工批准"):
        await executor.reply("candidate1")
    browser.send.assert_not_called()


async def test_uncertain_send_never_retries(system):
    settings, store, _, browser, executor = system
    settings.mode = "auto"
    make_candidate(store)
    browser.send.side_effect = TimeoutError("after click")
    assert (await executor.reply("candidate1"))["status"] == "uncertain"
    executor.paused = False
    with pytest.raises(RuntimeError):
        await executor.reply("candidate1")
    assert browser.send.call_count == 1


async def test_product_switch_blocks_send(system):
    settings, store, _, browser, executor = system
    settings.mode = "auto"
    make_candidate(store)
    browser.product.return_value = {"id": "sku2"}
    with pytest.raises(RuntimeError, match="商品"):
        await executor.reply("candidate1")
    browser.send.assert_not_called()


async def test_expired_candidate_blocks_send(system):
    settings, store, _, browser, executor = system
    settings.mode = "auto"
    candidate = make_candidate(store)
    candidate.created_at = time.time() - 100
    store.save(candidate)
    with pytest.raises(RuntimeError, match="过期"):
        await executor.reply(candidate.id)
    browser.send.assert_not_called()


def test_expired_and_wrong_product_knowledge(system):
    _, _, kb, _, _ = system
    assert not kb.search("发货", "sku2")
    kb.facts[0].valid_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert not kb.search("发货", "sku1")


def test_hallucinated_answer_rejected(system):
    _, _, kb, _, _ = system
    assert not kb.approved("今天一定发货", ["shipping"], "发货", "sku1")


def test_rate_limit_persisted(system):
    settings, store, _, _, _ = system
    for i in range(4):
        store.record_send(settings.account_id, str(i), str(i))
    assert store.rate_wait(settings.account_id, "new", "different", 4) > 50
    assert store.rate_wait(settings.account_id, "0", "0", 4) > 1700


def test_budget_and_idempotency(system):
    _, store, _, _, _ = system
    store.reserve_bag("first", 600, 1000, 10)
    with pytest.raises(ValueError):
        store.reserve_bag("second", 600, 1000, 10)
    assert store.claim("one")
    assert not store.claim("one")


async def test_bump_stops_when_out_of_view(system):
    settings, store, _, browser, executor = system
    settings.mode = "auto"
    settings.allow_public = True
    candidate = make_candidate(store)
    candidate.decision = Decision(action="bump")
    store.save(candidate)
    browser.visible.return_value = False
    assert (await executor.bump(candidate.id))["status"] == "out_of_view"
    browser.send.assert_not_called()


def test_api_and_cross_origin(tmp_path):
    settings = Settings(db_path=tmp_path / "api.db", vector_db_path=tmp_path / "vectors.db", _env_file=None)
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/status").json()["mode"] == "shadow"
        assert len(client.get("/api/tools").json()) == 7
        assert client.post("/api/pause", headers={"Origin": "https://other.example"}).status_code == 403
        assert client.post("/api/pause").status_code == 200


def test_real_douyin_question_labels():
    assert is_question("透明的 有吗？", "求补货 观众")
    assert is_question("P90Max", "潜在新客 观众")
    assert is_question("显示一下主播", "潜在新客 观众")
    assert not is_question("已拍", "已购买 观众")
