"""Real Embedding -> isolated sqlite-vec -> semantic KNN smoke test.

Uses temporary demonstration knowledge; never imports it into the business database.
"""
import asyncio
import json
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from live_agent.knowledge import Knowledge
from live_agent.settings import Settings


async def main():
    settings = Settings()
    Path("data").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vec-smoke-", dir=Path("data").resolve()) as directory:
        root = Path(directory)
        settings.vector_db_path = root / "knowledge.sqlite3"
        entries = []
        for key, product, question, keyword in [
            ("shipping", "demo-one", "这款衣服什么时候发货", "发货"),
            ("size", "demo-one", "尺码怎么选择", "尺码"),
            ("other-product", "demo-two", "商品预计多久发出", "发出"),
        ]:
            entries.append({"id": key, "product_id": product, "intent": key, "question": question,
                "keywords": [keyword], "answer": question + "（隔离测试，非经营知识）",
                "source": "测试样本", "valid_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        (root / "faq.yaml").write_text(yaml.safe_dump({"entries": entries}, allow_unicode=True), encoding="utf-8")
        kb = Knowledge(root, settings=settings)
        try:
            built = await kb.build_vectors()
            # No keyword/FTS overlap: verify the actual semantic path and native product filtering.
            start = time.perf_counter()
            results = await kb.retrieve("商品预计多久发出？", "demo-one")
            elapsed = round((time.perf_counter() - start) * 1000)
            assert results and results[0].id == "shipping", [r.id for r in results]
            assert all(r.product_id == "demo-one" for r in results)
            cached = await kb.build_vectors()
            assert cached["embedded"] == 0
            print(json.dumps({"ok": True, "build": built, "retrieved": [r.id for r in results],
                              "retrieval_ms_including_embedding": elapsed, "repeat_build_embedded": 0},
                             ensure_ascii=False))
        finally:
            kb.close()


if __name__ == "__main__":
    asyncio.run(main())
