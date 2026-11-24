import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .vector_store import VectorStore

logger = logging.getLogger(__name__)


class Fact(BaseModel):
    id: str
    product_id: str
    intent: str
    question: str
    keywords: list[str] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    answer: str = Field(min_length=1, max_length=160)
    source: str = Field(min_length=1)
    valid_until: datetime

    def valid(self):
        return self.valid_until.tzinfo is not None and self.valid_until.timestamp() > time.time()

    def exact_question(self, query):
        def normalize(value):
            return ''.join(re.findall(r"[\w\u4e00-\u9fff]", value.lower()))
        return normalize(query) in {normalize(q) for q in [self.question, *self.aliases]}


class Knowledge:
    def __init__(self, directory="kb", settings=None):
        self.facts = []
        for path in Path(directory).glob("*.yaml"):
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            self.facts.extend(Fact(**entry) for entry in data.get("entries", []))
        if len({f.id for f in self.facts}) != len(self.facts):
            raise ValueError("知识条目 ID 重复")
        self.settings = settings
        self.embeddings = None
        self.index = sqlite3.connect(":memory:")
        self.index.execute("CREATE VIRTUAL TABLE facts USING fts5(id UNINDEXED, tokens, product_id UNINDEXED, expires UNINDEXED)")
        for f in self.facts:
            self.index.execute("INSERT INTO facts VALUES(?,?,?,?)", (f.id, self.tokens(
                f.question + ' ' + ' '.join(f.keywords)), f.product_id,
                f.valid_until.timestamp() if f.valid_until.tzinfo is not None else 0))
        if settings and settings.embedding_model and settings.embedding_api_key and settings.embedding_base_url:
            from langchain_openai import OpenAIEmbeddings
            options = {"model": settings.embedding_model, "api_key": settings.embedding_api_key,
                       "base_url": settings.embedding_base_url,
                       "dimensions": settings.embedding_dimensions, "model_kwargs": {"encoding_format": "float"},
                       "check_embedding_ctx_length": False, "chunk_size": 10,
                       "request_timeout": settings.model_timeout, "max_retries": 0}
            self.embeddings = OpenAIEmbeddings(**options)
        self.vectors = VectorStore(settings.vector_db_path if settings else ":memory:",
                                   settings.embedding_dimensions if settings else 1024)

    @staticmethod
    def tokens(text):
        normalized = ''.join(re.findall(r"[\w\u4e00-\u9fff]", text.lower()))
        return ' '.join(normalized[i:i+2] for i in range(max(1, len(normalized)-1)))

    def fingerprint(self):
        payload = [f.model_dump(mode="json") for f in self.facts]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def vector_config(self):
        return {"model": self.settings.embedding_model, "dimensions": self.settings.embedding_dimensions,
                "endpoint": self.settings.embedding_base_url}

    async def build_vectors(self):
        if not self.embeddings:
            raise ValueError("请配置 LIVE_EMBEDDING_MODEL、LIVE_EMBEDDING_API_KEY 和 LIVE_EMBEDDING_BASE_URL")
        config = self.vector_config()
        changed = self.vectors.changed(self.facts, config)
        embeddings = []
        # Bounded batches: only modified/new entries incur embedding API costs.
        for start in range(0, len(changed), 10):
            batch = changed[start:start + 10]
            result = await self.embeddings.aembed_documents([
                f.question + ' ' + ' '.join(f.aliases) + ' ' + f.answer for f in batch])
            if len(result) != len(batch):
                raise ValueError("Embedding 返回数量与知识条目数不符")
            embeddings.extend(result)
        self.vectors.replace(self.facts, changed, embeddings, config, self.fingerprint())
        return {**self.vectors.status(), "embedded": len(changed)}

    def vector_status(self):
        return {**self.vectors.status(), "ready": bool(self.settings and self.vectors.ready(
                self.vector_config(), self.fingerprint()))}

    def close(self):
        self.index.close()
        self.vectors.close()

    async def retrieve(self, query, product_id):
        exact = self.search(query, product_id)
        if exact:
            return exact
        # Chinese bigram FTS recall is only presented to the model; never a fast-path answer.
        terms = self.tokens(query).split()
        ids = []
        if terms:
            expression = ' OR '.join('"' + t + '"' for t in terms)
            ids = [r[0] for r in self.index.execute(
                "SELECT id FROM facts WHERE facts MATCH ? AND product_id IN (?,?) "
                "AND expires>? ORDER BY rank LIMIT 20", (expression, product_id, '*', time.time()))]
        eligible = {f.id: f for f in self.facts if f.valid() and f.product_id in (product_id, '*')}
        ranked = {key: 1 / (60 + n) for n, key in enumerate(ids) if key in eligible}
        if self.embeddings and self.vectors.ready(self.vector_config(), self.fingerprint()):
            try:
                q = await asyncio.wait_for(self.embeddings.aembed_query(query),
                                           timeout=self.settings.model_timeout)
                # Re-check after the await: a separate build process may have replaced the index.
                if self.vectors.ready(self.vector_config(), self.fingerprint()):
                    matches = self.vectors.search(q, product_id, self.settings.embedding_dimensions,
                                                  self.settings.semantic_threshold)
                    for n, key in enumerate(matches):
                        if key in eligible:
                            ranked[key] = ranked.get(key, 0) + 1 / (60 + n)
            except Exception as exc:
                # Report only type, avoiding API responses that might contain sensitive request details.
                logger.warning("Semantic retrieval unavailable (%s); using lexical recall", type(exc).__name__)
        return [eligible[key] for key in sorted(ranked, key=ranked.get, reverse=True)[:5]]

    def search(self, query, product_id):
        # Strict product boundary. Global rules must explicitly use product_id='*'.
        scored = []
        for fact in self.facts:
            if not fact.valid() or fact.product_id not in (product_id, "*"):
                continue
            score = sum(len(k) for k in fact.keywords if k in query)
            if score:
                scored.append((score, fact))
        return [f for _, f in sorted(scored, key=lambda pair: pair[0], reverse=True)[:5]]

    def approved(self, text, evidence_ids, query, product_id):
        # First release is extractive: model may choose an approved answer, not invent facts.
        hits = [f for f in self.facts if f.valid() and f.product_id in (product_id, '*')]
        return any(f.id in evidence_ids and f.answer == text for f in hits)


def is_complaint(text):
    return bool(re.search(r"差评|骗人|假货|质量.*差|投诉|垃圾|退款", text))


def is_question(text, kind):
    return (any(label in kind for label in ("问询", "求讲解", "求补货")) or bool(re.search(
        r"[？?]|多少钱|价位|发货|没券|有吗|有没有|显示一下|展示一下|讲解一下|"
        r"(?i:\b(?:p(?:ura)?|mate)\s*\d{2}(?:pro|max|ultra)*)", text)))
