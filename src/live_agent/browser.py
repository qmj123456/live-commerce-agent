import asyncio
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from playwright.async_api import async_playwright

from .models import Comment

OBSERVER = r'''(cfg) => {
 if (window.__liveAgent) window.__liveAgent.observer.disconnect();
 const state = {queue: [], counter: 0, overflow: 0};
 const collect = () => {
   document.querySelectorAll(cfg.comments).forEach(row => {
     const user = row.querySelector(cfg.nickname)?.innerText?.trim() || '';
     const text = row.querySelector(cfg.content)?.innerText?.trim() || '';
     const signature = user + '\n' + text;
     if (!text || row.__liveSignature === signature) return;
     row.__liveSignature = signature;
     const id = crypto.randomUUID();
     row.setAttribute('data-live-agent-id', id);
     state.queue.push({id, user, text, kind: user, observed_at: Date.now()/1000});
     if (state.queue.length > 500) { state.queue.shift(); state.overflow++; }
   });
 };
 // Existing comments are marked without being replayed as new questions.
 document.querySelectorAll(cfg.comments).forEach(row => {
   row.__liveSignature = (row.querySelector(cfg.nickname)?.innerText?.trim() || '') + '\n' +
     (row.querySelector(cfg.content)?.innerText?.trim() || '');
 });
 state.observer = new MutationObserver(collect);
 state.observer.observe(document.body, {subtree:true, childList:true, characterData:true});
 window.__liveAgent = state;
}'''


class CDPBrowser:
    def __init__(self, settings):
        self.settings = settings
        self.cfg = yaml.safe_load(Path("config/platform.yaml").read_text(encoding="utf-8"))
        self.page = None
        self.lock = asyncio.Lock()
        self.pw = None

    async def connect(self):
        wanted = urlsplit(self.settings.room_url)
        if wanted.scheme != "https" or not wanted.netloc:
            raise ValueError("请设置精确的 HTTPS LIVE_ROOM_URL")
        self.pw = await async_playwright().start()
        try:
            self.browser = await self.pw.chromium.connect_over_cdp(self.settings.cdp_url)
            pages = [p for ctx in self.browser.contexts for p in ctx.pages
                     if p.url.rstrip("/") == self.settings.room_url.rstrip("/")]
            if len(pages) != 1:
                raise RuntimeError("必须且只能找到一个精确匹配的直播间标签页")
            self.page = pages[0]
            self.page.set_default_timeout(3000)
            await self.ensure_observer()
        except Exception:
            await self.close()
            raise

    async def ensure_observer(self):
        await self.healthy()
        if not await self.page.evaluate("!!window.__liveAgent"):
            await self.page.evaluate(OBSERVER, self.cfg)

    async def healthy(self):
        if not self.page or self.page.is_closed():
            raise RuntimeError("浏览器连接已关闭")
        if self.page.url.rstrip("/") != self.settings.room_url.rstrip("/"):
            raise RuntimeError("页面地址已改变，请重新连接")
        risk = self.cfg.get("risk")
        if risk and await self.page.locator(risk).count():
            raise RuntimeError("检测到风险或验证码状态")

    async def read_comments(self):
        await self.ensure_observer()
        raw = await self.page.evaluate("window.__liveAgent.queue.splice(0)")
        return [Comment(**item) for item in raw]

    async def product(self):
        await self.healthy()
        selector = self.cfg.get("product_root")
        if not selector:
            return {"id": "", "fields": {}, "observed_at": time.time()}
        root = self.page.locator(selector)
        if await root.count() != 1:
            raise RuntimeError("当前讲解商品定位不唯一")
        return {"id": await root.get_attribute(self.cfg["product_id_attribute"]) or "",
                "fields": {name: await root.locator(sel).inner_text()
                           for name, sel in self.cfg["product_fields"].items()},
                "observed_at": time.time()}

    async def target(self, comment):
        row = self.page.locator(f'[data-live-agent-id="{comment.id}"]')
        if await row.count() != 1:
            raise RuntimeError("原评论已消失")
        if ((await row.locator(self.cfg["content"]).inner_text()).strip() != comment.text or
                (await row.locator(self.cfg["nickname"]).inner_text()).strip() != comment.user):
            raise RuntimeError("评论节点已被复用")
        return row

    async def visible(self, comment):
        try:
            row = await self.target(comment)
            return await row.evaluate('''el => {
              let r=el.getBoundingClientRect();
              if(r.bottom<=0 || r.top>=innerHeight || !r.width || !r.height) return false;
              for(let p=el.parentElement;p;p=p.parentElement){
                const s=getComputedStyle(p);
                if(/auto|scroll|hidden/.test(s.overflowY)){
                  const b=p.getBoundingClientRect(); if(r.bottom<=b.top || r.top>=b.bottom)return false;
                }
              } return true;
            }''')
        except RuntimeError:
            return False

    async def send(self, comment, text, before_click):
        await self.healthy()
        box = self.page.locator(self.cfg["input"])
        if await box.input_value():
            raise RuntimeError("输入框正在使用")
        prefix = ""
        if comment:
            row = await self.target(comment)
            await row.hover()
            await row.locator(self.cfg["reply"]).first.click()
            prefix = await box.input_value()
            if not prefix.strip().startswith("@") or len(prefix.strip()) < 2:
                raise RuntimeError("未形成有效 @，禁止发送")
            expected_user = re.sub(r"^(问询|求讲解|已购买|观众)\s*", "", comment.user).strip()
            if prefix.strip().removeprefix("@").strip() != expected_user:
                raise RuntimeError("@ 对象与原评论昵称不匹配")
        if "@" in text:
            raise RuntimeError("回复正文禁止携带额外 @")
        expected = prefix + text
        existing = await self.page.locator(self.cfg["comments"]).filter(
            has_text=self.cfg["own_marker"]).filter(has_text=expected).count()
        await box.fill(expected)
        if comment:
            await self.target(comment)
        if await box.input_value() != expected:
            raise RuntimeError("输入内容发生变化")
        # Persist attempt before clicking: timeouts must not trigger duplicate retries.
        before_click()
        await self.page.locator(self.cfg["send"]).click()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = self.page.locator(self.cfg["comments"]).filter(has_text=self.cfg["own_marker"])
            if await rows.filter(has_text=expected).count() > existing:
                return "confirmed"
            await asyncio.sleep(0.2)
        return "uncertain"

    async def bag_status(self):
        await self.healthy()
        if not self.cfg["bag_root"]:
            raise RuntimeError("福袋 DOM 尚未标定")
        roots = self.page.locator(self.cfg["bag_root"])
        if await roots.count() != 1:
            raise RuntimeError("福袋状态定位不唯一")
        return {"state": await roots.get_attribute(self.cfg["bag_status_attribute"]),
                "template_id": await roots.get_attribute(self.cfg["bag_template_attribute"]),
                "instance_id": await roots.get_attribute(self.cfg["bag_instance_attribute"])}

    async def run_bag(self, plan, before_action):
        cfg = self.cfg
        if not all(cfg.get(k) for k in ("bag_root", "bag_create", "bag_start", "bag_submit", "bag_cost")):
            raise RuntimeError("福袋操作选择器未配置")
        status = await self.bag_status()
        if status["state"] != "idle":
            raise RuntimeError("福袋不是空闲状态，请人工核验")
        if not plan.fields or set(plan.fields) != set(cfg["bag_fields"]):
            raise RuntimeError("福袋模板字段与 DOM 配置不匹配")
        before_action()
        await self.page.locator(cfg["bag_create"]).click()
        for name, value in plan.fields.items():
            field = self.page.locator(cfg["bag_fields"][name])
            await field.fill(value)
            if await field.input_value() != value:
                raise RuntimeError("福袋字段校验失败")
        cost = await self.page.locator(cfg["bag_cost"]).get_attribute(cfg["bag_cost_attribute"])
        if cost is None or not cost.isdigit() or int(cost) != plan.budget_cents:
            raise RuntimeError("页面福袋成本与审批预算不一致")
        before_action()
        await self.page.locator(cfg["bag_submit"]).click()
        await self.page.wait_for_function('''c => {
          const e=document.querySelector(c.bag_root);
          return e?.getAttribute(c.bag_status_attribute)==='pending';
        }''', arg=cfg, timeout=5000)
        status = await self.bag_status()
        if status["template_id"] != plan.template_id or not status["instance_id"]:
            raise RuntimeError("福袋模板或活动标识不匹配")
        before_action()
        await self.page.locator(cfg["bag_start"]).click()
        await self.page.wait_for_function('''c => {
          const e=document.querySelector(c.bag_root);
          return e?.getAttribute(c.bag_status_attribute)==='running';
        }''', arg=cfg, timeout=5000)
        final = await self.bag_status()
        if final["instance_id"] != status["instance_id"]:
            raise RuntimeError("福袋活动标识发生变化")
        return final

    async def close(self):
        # Stop the CDP client, never close the user's Chrome process/context.
        if self.pw:
            await self.pw.stop()
        self.page = None
