"""Offline evaluation. JSONL: {comment: {id,user,text,kind}, product_id, expected_action, expected_text?}."""
import argparse
import asyncio
import json
import time
from unittest.mock import AsyncMock

from live_agent.agent import Agent
from live_agent.knowledge import Knowledge
from live_agent.models import Comment
from live_agent.settings import Settings


async def run(path):
    settings = Settings(mode="shadow")
    browser = AsyncMock()
    agent = Agent(settings, browser, Knowledge(settings=settings), AsyncMock())
    count = correct = 0
    latencies = []
    with open(path, encoding="utf-8") as source:
        for line in source:
            item = json.loads(line)
            comment = Comment(**item["comment"])
            browser.product.return_value = {"id": item.get("product_id", ""), "fields": {}}
            start = time.perf_counter()
            result = await agent.graph.ainvoke({"comment": comment.model_dump()})
            latencies.append((time.perf_counter() - start) * 1000)
            decision = result["decision"]
            ok = decision["action"] == item["expected_action"]
            if "expected_text" in item:
                ok = ok and decision["text"] == item["expected_text"]
            count += 1
            correct += int(ok)
            print(json.dumps({"id": comment.id, "correct": ok, "decision": decision}, ensure_ascii=False))
    samples = sorted(latencies)
    print(json.dumps({"count": count, "correct": correct, "accuracy": correct / count if count else None,
        "candidate_p95_ms": samples[min(int(count * .95), count - 1)] if count else None}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl")
    asyncio.run(run(parser.parse_args().jsonl))
