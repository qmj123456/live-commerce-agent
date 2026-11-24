"""Smoke-test the running local panel and WebSocket, using an isolated Chrome."""
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel="chrome", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto("http://127.0.0.1:8765")
        await page.get_by_text("控制台已连接", exact=True).wait_for()
        await page.get_by_text("执行已暂停", exact=True).wait_for()
        response = await page.request.get("http://127.0.0.1:8765/api/tools")
        assert len(await response.json()) == 7
        assert not errors, errors
        Path("data").mkdir(exist_ok=True)
        await page.screenshot(path="data/panel.png", full_page=True)
        print("Panel rendered, WebSocket connected, seven tool schemas exposed, no page errors.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
