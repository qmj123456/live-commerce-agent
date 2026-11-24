import asyncio

import pytest
from playwright.async_api import async_playwright

from live_agent.browser import OBSERVER, CDPBrowser
from live_agent.models import Comment
from live_agent.settings import Settings


@pytest.fixture
async def page():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page()
        await page.route("http://localhost/**", lambda route: route.fulfill(body="<html></html>"))
        await page.goto("http://localhost/fixture")
        await page.set_content('''<div id="comments"></div>
          <div id="input-comment-block-id"><textarea placeholder="回复观众"></textarea>
          <div id="send" class="inputSuffix-hash">发布</div></div>
          <script>
          function add(user,text,id){
            let row=document.createElement('div');row.className='commentItem-hash';
            let nick=document.createElement('span');nick.className='nickname-hash';nick.textContent=user;
            let content=document.createElement('span');content.className='description-hash';content.textContent=text;
            let reply=document.createElement('div');reply.className='iconWrapper';reply.textContent='回复';
            reply.onclick=()=>{document.querySelector('textarea').value='@'+user.replace(/^问询\\s*/, '')+' ';};
            row.append(nick,content,reply);if(id)row.setAttribute('data-live-agent-id',id);
            document.querySelector('#comments').append(row);
          }
          document.querySelector('#send').onclick=()=>{
            add('主播我',document.querySelector('textarea').value);
            document.querySelector('textarea').value='';
          };
          </script>''')
        yield page
        await browser.close()


async def adapter(page):
    settings = Settings(room_url=page.url, _env_file=None)
    browser = CDPBrowser(settings)
    browser.page = page
    return browser


async def test_observer_dedup_and_reused_node(page):
    browser = await adapter(page)
    await page.evaluate(OBSERVER, browser.cfg)
    await page.evaluate("add('问询 用户甲','什么时候发货')")
    await asyncio.sleep(.05)
    comments = await browser.read_comments()
    assert len(comments) == 1
    assert not await browser.read_comments()
    await page.locator(browser.cfg["content"]).evaluate("e=>e.textContent='另一个问题'")
    await asyncio.sleep(.05)
    newer = await browser.read_comments()
    assert len(newer) == 1 and newer[0].id != comments[0].id
    with pytest.raises(RuntimeError, match="消失"):
        await browser.target(comments[0])


async def test_send_and_readback(page):
    browser = await adapter(page)
    await page.evaluate("add('问询 用户甲','什么时候发货','row1')")
    comment = Comment(id="row1", user="问询 用户甲", text="什么时候发货")
    attempts = []
    result = await browser.send(comment, "请参考发货规则。", lambda: attempts.append(1))
    assert result == "confirmed" and attempts == [1]


async def test_wrong_mention_blocks_send(page):
    browser = await adapter(page)
    await page.evaluate("add('问询 用户甲','什么时候发货','row1')")
    await page.locator(browser.cfg["reply"]).evaluate(
        "e=>e.onclick=()=>document.querySelector('textarea').value='@其他观众 '")
    with pytest.raises(RuntimeError, match="不匹配"):
        await browser.send(Comment(id="row1", user="问询 用户甲", text="什么时候发货"), "规则", lambda: None)


async def test_busy_input_blocks_send(page):
    browser = await adapter(page)
    await page.locator(browser.cfg["input"]).fill("人工正在编辑")
    with pytest.raises(RuntimeError, match="正在使用"):
        await browser.send(None, "常用语", lambda: None)


async def test_calibrated_product_fields_are_scoped_to_explaining_item(page):
    browser = await adapter(page)
    await page.set_content('''
      <div class="rpa_lc__live-goods__goods-item" data-rbd-draggable-id="active-sku">
        <div class="coverTag-hash">讲解中</div>
        <div class="rightContainer-hash"><div class="title-hash">当前商品</div></div>
        <div class="rpa_lc__live-goods__goods-elastic"><div class="text-hash">页面卖点</div></div>
        <div class="dataItem-hash"><div class="data-hash">¥128.00</div>
          <div class="dataName-hash"><span>到手价</span></div></div>
        <div class="dataItem-hash"><div class="data-hash">1/369</div>
          <div class="dataName-hash"><span>售出/库存</span></div></div>
        <div class="dataItem-hash"><div class="data-hash">¥104.00</div>
          <div class="dataName-hash"><span>成交金额</span></div></div>
      </div>
      <div class="rpa_lc__live-goods__goods-item" data-rbd-draggable-id="other-sku">
        <div class="rightContainer-hash"><div class="title-hash">其他商品</div></div>
      </div>''')
    product = await browser.product()
    assert product["id"] == "active-sku"
    assert product["fields"]["price"] == "¥128.00"
    assert product["fields"]["title"] == "当前商品"
