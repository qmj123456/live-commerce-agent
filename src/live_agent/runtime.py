import asyncio
import time
from uuid import uuid4

from .agent import Agent
from .browser import CDPBrowser
from .executor import Executor
from .knowledge import Knowledge
from .models import Candidate, Comment, Decision
from .store import Store


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.kb = Knowledge(settings=settings)
        self.browser = CDPBrowser(settings)
        self.executor = Executor(settings, self.browser, self.store, self.kb)
        self.agent = Agent(settings, self.browser, self.kb, self.executor)
        self.queue = asyncio.Queue(maxsize=settings.max_queue)
        self.tasks = []
        self.connected = False
        self.error = ""
        self.latencies = []

    async def start(self):
        if self.connected:
            return
        await self.browser.connect()
        self.connected = True
        self.executor.paused = False
        self.error = ""
        self.tasks = [asyncio.create_task(self.collect()),
                      *[asyncio.create_task(self.process()) for _ in range(3)],
                      asyncio.create_task(self.dispatch())]

    def fail(self, exc):
        self.error = str(exc)
        self.executor.paused = True
        self.store.event("runtime_error", {"error": self.error})

    async def collect(self):
        while True:
            try:
                # Draining observer events does not touch the composer; keep collection live during sends.
                comments = await self.agent.tools["read_live_comments"].ainvoke({})
                for raw in comments:
                    self.store.event("comment", raw)
                    if self.queue.full():
                        self.fail(RuntimeError("采集队列已满，已暂停执行；请检查负载"))
                        self.store.event("overflow", raw)
                    else:
                        self.queue.put_nowait(Comment(**raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.fail(exc)
            await asyncio.sleep(0.1)

    async def process(self):
        while True:
            comment = await self.queue.get()
            try:
                result = await self.agent.graph.ainvoke({"comment": comment.model_dump()})
                candidate = Candidate(id=str(uuid4()), comment=comment,
                    decision=Decision(**result["decision"]), product_id=result["product"]["id"])
                # Expiry is measured from collection, not completion of model generation.
                candidate.created_at = comment.observed_at
                self.store.save(candidate)
                duration = time.time() - comment.observed_at
                self.latencies = (self.latencies + [duration])[-1000:]
                self.store.event("candidate", {"id": candidate.id, "latency_ms": duration * 1000})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.fail(exc)
            finally:
                self.queue.task_done()

    async def dispatch(self):
        while True:
            try:
                if not self.executor.paused and self.settings.mode != "shadow":
                    if self.settings.mode == "auto" and self.settings.allow_bags:
                        for plan in self.executor.plans.values():
                            if 0 <= time.time() - plan.at.timestamp() <= 60:
                                done = self.store.db.execute("SELECT 1 FROM claims WHERE key=?",
                                                             ("bag:" + plan.id,)).fetchone()
                                if not done:
                                    await self.agent.tools["start_lucky_bag"].ainvoke({"plan_id": plan.id})
                    items = [Candidate(**c) for c in self.store.candidates() if c["status"] == "pending"]
                    items.sort(key=lambda c: (c.decision.action != "reply", c.created_at))
                    # Only one complaint bump task may be active; merge other triggers.
                    bump_seen = False
                    for candidate in items:
                        if self.executor.paused:
                            break
                        ttl = self.settings.bump_ttl if candidate.decision.action == "bump" else self.settings.candidate_ttl
                        if time.time() - candidate.created_at > ttl:
                            candidate.status = "expired"
                            self.store.save(candidate)
                            continue
                        if self.settings.mode == "assist" and not candidate.approved:
                            continue
                        action = candidate.decision.action
                        if action == "reply":
                            await self.agent.tools["send_reply"].ainvoke({"candidate_id": candidate.id})
                        elif action == "bump" and self.settings.allow_public:
                            if bump_seen:
                                candidate.status = "merged"
                                self.store.save(candidate)
                                continue
                            bump_seen = True
                            await self.agent.tools["bump_comment"].ainvoke({"candidate_id": candidate.id})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.fail(exc)
            await asyncio.sleep(0.25)

    def status(self):
        samples = sorted(self.latencies)
        return {"mode": self.settings.mode, "connected": self.connected,
                "knowledge": self.kb.vector_status(),
                "paused": self.executor.paused, "error": self.error,
                "queue": self.queue.qsize(), "samples": len(samples),
                "candidate_p95_ms": samples[min(int(len(samples) * .95), len(samples) - 1)] * 1000
                if samples else None}

    async def stop(self):
        self.executor.paused = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        await self.browser.close()
        self.connected = False
