import asyncio
import time
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .models import Candidate


class BagPlan(BaseModel):
    id: str
    at: datetime
    template_id: str = Field(min_length=1)
    budget_cents: int = Field(gt=0)
    fields: dict[str, str]


class Executor:
    def __init__(self, settings, browser, store, knowledge):
        self.settings, self.browser, self.store, self.kb = settings, browser, store, knowledge
        self.paused = True
        self.lock = asyncio.Lock()
        data = yaml.safe_load(Path("config/bags.yaml").read_text(encoding="utf-8"))
        self.plans = {p["id"]: BagPlan(**p) for p in data["plans"]}
        if any(p.at.tzinfo is None for p in self.plans.values()):
            raise ValueError("福袋计划必须携带时区")
        self.phrases = yaml.safe_load(Path("config/replies.yaml").read_text(encoding="utf-8"))["bump"]

    def candidate(self, candidate_id):
        row = self.store.db.execute("SELECT payload FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            raise ValueError("候选不存在")
        return Candidate.model_validate_json(row[0])

    def guard(self, candidate=None):
        if self.paused:
            raise RuntimeError("执行器已暂停")
        if self.settings.mode == "shadow":
            raise RuntimeError("shadow 模式禁止写入")
        if candidate:
            if candidate.status != "pending":
                raise RuntimeError("候选已处理")
            ttl = self.settings.bump_ttl if candidate.decision.action == "bump" else self.settings.candidate_ttl
            if time.time() - candidate.created_at > ttl:
                raise RuntimeError("候选已过期")
            if self.settings.mode == "assist" and not candidate.approved:
                raise RuntimeError("候选尚未人工批准")

    def before_send(self, target, text):
        self.guard()
        self.store.record_send(self.settings.account_id, target, text)

    async def reply(self, candidate_id):
        async with self.lock, self.browser.lock:
            candidate = self.candidate(candidate_id)
            self.guard(candidate)
            if candidate.decision.action != "reply":
                raise RuntimeError("候选不是回复动作")
            product = await self.browser.product()
            if product["id"] != candidate.product_id or not self.kb.approved(
                    candidate.decision.text, candidate.decision.evidence_ids,
                    candidate.comment.text, candidate.product_id):
                raise RuntimeError("商品或知识已失效")
            wait = self.store.rate_wait(self.settings.account_id, candidate.comment.user,
                                        candidate.decision.text, self.settings.messages_per_minute)
            if wait > 0:
                return {"status": "deferred", "retry_after": wait}
            key = "reply:" + candidate.id
            if not self.store.claim(key):
                raise RuntimeError("该回复已执行或待核验，禁止重试")
            candidate.status = "executing"
            self.store.save(candidate)
            try:
                status = await self.browser.send(candidate.comment, candidate.decision.text,
                    lambda: self.before_send(candidate.comment.user, candidate.decision.text))
            except Exception as exc:
                status = "uncertain"
                self.paused = True
                self.store.event("send_error", {"id": candidate.id, "error": str(exc)})
            candidate.status = status
            self.store.finish(key, status)
            self.store.save(candidate)
            if status == "uncertain":
                self.paused = True
            return {"status": status}

    async def bump(self, candidate_id):
        # One bounded durable task; one message per scheduler visit, never a burst.
        async with self.lock, self.browser.lock:
            candidate = self.candidate(candidate_id)
            self.guard(candidate)
            if not self.settings.allow_public or candidate.decision.action != "bump":
                raise RuntimeError("未开启公屏顶帖或候选类型错误")
            public_count = self.store.db.execute(
                "SELECT COUNT(*) FROM sends WHERE account=? AND target='' AND at>?",
                (self.settings.account_id, time.time() - 3600)).fetchone()[0]
            if public_count >= self.settings.max_public_per_hour:
                candidate.status = "escalated"
                self.store.save(candidate)
                return {"status": "escalated", "reason": "公屏小时额度耗尽"}
            if not await self.browser.visible(candidate.comment):
                candidate.status = "out_of_view"
                self.store.save(candidate)
                return {"status": candidate.status}
            key = "bump-task:" + candidate.id
            prior = self.store.db.execute("SELECT state FROM claims WHERE key=?", (key,)).fetchone()
            count = int(prior[0]) if prior and prior[0].isdigit() else 0
            if prior and not prior[0].isdigit():
                raise RuntimeError("顶帖任务结果待核验")
            if count >= 5:
                candidate.status = "escalated"
                self.store.save(candidate)
                return {"status": "escalated"}
            choices = [p for p in self.phrases if self.store.rate_wait(
                self.settings.account_id, "", p, self.settings.messages_per_minute) <= 0]
            if not choices:
                return {"status": "deferred", "retry_after": 15}
            phrase = choices[0]
            if not prior:
                self.store.claim(key)
            self.store.finish(key, "executing")
            try:
                status = await self.browser.send(None, phrase, lambda: self.before_send("", phrase))
            except Exception as exc:
                self.store.event("bump_error", {"error": str(exc), "id": candidate_id})
                status = "uncertain"
            self.store.finish(key, str(count + 1) if status == "confirmed" else "uncertain")
            if status != "confirmed":
                self.paused = True
                candidate.status = "uncertain"
            elif not await self.browser.visible(candidate.comment):
                candidate.status = "out_of_view"
            self.store.save(candidate)
            return {"status": status, "count": count + 1}

    async def bag(self, plan_id):
        async with self.lock, self.browser.lock:
            self.guard()
            if not self.settings.allow_bags or self.settings.mode != "auto":
                raise RuntimeError("福袋自动执行未开启（需 auto 模式）")
            plan = self.plans[plan_id]
            now = time.time()
            if not 0 <= now - plan.at.timestamp() <= 60:
                raise RuntimeError("不在福袋执行时间窗口")
            key = "bag:" + plan.id
            if not self.store.claim(key):
                raise RuntimeError("该福袋计划已执行或待核验")
            try:
                self.store.reserve_bag(plan.id, plan.budget_cents,
                    self.settings.max_bag_budget_cents, self.settings.max_bag_runs)
                result = await self.browser.run_bag(plan, self.guard)
            except Exception as exc:
                self.store.finish(key, "uncertain")
                self.store.bag_state(plan.id, "uncertain")
                self.store.event("bag_error", {"id": plan.id, "error": str(exc)})
                self.paused = True
                raise
            self.store.finish(key, "confirmed")
            self.store.bag_state(plan.id, "confirmed")
            self.store.event("bag_confirmed", result)
            return result
