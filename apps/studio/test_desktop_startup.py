"""Desktop bundle regression: run with Python + Playwright Chromium installed.

All requests are intercepted; no production accounts or data are touched.
"""
import asyncio
import json
import mimetypes
import shutil
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]


async def check(browser, bundle, missing_vendor=False):
    page = await browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    await page.add_init_script("""
        globalThis.AMPHI_API = 'https://api.test';
        globalThis.__TAURI__ = {core: {invoke: async (command) => {
            if (command === 'host_info') return {native_asr: true, model_present: false};
            return 0;
        }}};
    """)

    async def serve(route):
        url = route.request.url
        if url.startswith("https://api.test/"):
            path = url.split("https://api.test", 1)[1]
            status = 200
            if path == "/auth/login":
                payload = {"token": "synthetic-test-token", "user": {"displayName": "Test"}}
            elif path == "/auth/me":
                status = 200 if route.request.headers.get("authorization") == "Bearer synthetic-test-token" else 401
                payload = {"user": {"displayName": "Test"}}
            elif path == "/docs":
                payload = {"docs": []}
            elif path.startswith("/sessions"):
                payload = {"sessions": []}
            elif path.startswith("/schedule"):
                payload = {"now": []}
            elif path.startswith("/load"):
                payload = {"empty": True}
            elif path.startswith("/compatibility"):
                payload = {"compatible": True}
            else:
                payload = {"ok": True}
            await route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
            return
        relative = url.split("http://desktop.test/", 1)[-1]
        target = bundle / (relative or "index.html")
        if missing_vendor and relative.startswith("vendor/"):
            await route.fulfill(status=404, body="missing")
        elif target.is_file():
            await route.fulfill(body=target.read_bytes(), content_type=mimetypes.guess_type(str(target))[0] or "application/octet-stream")
        else:
            await route.fulfill(status=404, body="missing")

    await page.route("**/*", serve)
    await page.goto("http://desktop.test/")
    await page.locator("#engine").click()
    await page.locator("#loginUser").fill("test")
    await page.locator("#loginPass").fill("synthetic-test-password")
    await page.locator("#loginSubmit").click()
    await page.wait_for_function("!document.querySelector('#loginSubmit')")
    if not missing_vendor:
        assert await page.evaluate("typeof globalThis.mermaid.initialize") == "function"
        assert await page.evaluate("typeof globalThis.katex.render") == "function"
    await page.locator("#newBtn").click()
    await page.locator("#nManual").click()
    await page.locator("#nC").fill("Synthetic lecture")
    await page.locator("#nOk").click()
    await page.wait_for_function("!document.getElementById('viewSes').classList.contains('hidden')")
    await page.locator("#tabLib").click()
    assert await page.locator("#viewLib").is_visible()
    await page.locator("#tabSes").click()
    assert await page.locator("#viewSes").is_visible()
    await page.locator("#tabLib").click()
    await page.locator("#newBtn").click()
    await page.locator("#nIcs").click()
    assert "premier lien ical" in (await page.locator(".modal").inner_text()).lower()
    assert not errors, errors
    print("PASS", "missing-vendor fallback" if missing_vendor else "complete bundle", "login / session creation / tabs / iCal dialog")
    await page.close()


async def main():
    with tempfile.TemporaryDirectory(prefix="amphi-bundle-test-") as temp:
        bundle = Path(temp) / "dist"
        shutil.copytree(ROOT / "apps/studio/ui", bundle)
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                await check(browser, bundle)
                await check(browser, bundle, missing_vendor=True)
            finally:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
